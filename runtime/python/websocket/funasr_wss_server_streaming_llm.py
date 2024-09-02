import os
import asyncio
import json
import websockets
import time
import argparse
import ssl
import numpy as np
from threading import Thread
from transformers import TextIteratorStreamer
from funasr import AutoModel
from modelscope.hub.api import HubApi
from modelscope.hub.snapshot_download import snapshot_download

parser = argparse.ArgumentParser()
parser.add_argument(
    "--host", type=str, default="127.0.0.1", required=False, help="host ip, localhost, 0.0.0.0"
)
parser.add_argument("--port", type=int, default=10095, required=False, help="grpc server port")
parser.add_argument(
    "--vad_model",
    type=str,
    default="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    help="model from modelscope",
)
parser.add_argument("--vad_model_revision", type=str, default="master", help="")
parser.add_argument("--ngpu", type=int, default=1, help="0 for cpu, 1 for gpu")
parser.add_argument("--device", type=str, default="cuda", help="cuda, cpu")
parser.add_argument("--ncpu", type=int, default=4, help="cpu cores")
parser.add_argument(
    "--certfile",
    type=str,
    default="../../ssl_key/server.crt",
    required=False,
    help="certfile for ssl",
)
parser.add_argument(
    "--keyfile",
    type=str,
    default="../../ssl_key/server.key",
    required=False,
    help="keyfile for ssl",
)
args = parser.parse_args()

websocket_users = set()

print("model loading")
# vad
model_vad = AutoModel(
    model=args.vad_model,
    model_revision=args.vad_model_revision,
    ngpu=args.ngpu,
    ncpu=args.ncpu,
    device=args.device,
    disable_pbar=True,
    disable_log=True,
    max_single_segment_time=40000,
    max_end_silence_time=580,
    # chunk_size=60,
)

api = HubApi()
if "key" in os.environ:
    key = os.environ["key"]
    api.login(key)

# os.environ["MODELSCOPE_CACHE"] = "/nfs/zhifu.gzf/modelscope"
# llm_dir = snapshot_download('qwen/Qwen2-7B-Instruct', cache_dir=None, revision='master')
# audio_encoder_dir = snapshot_download('iic/SenseVoice', cache_dir=None, revision='master')

llm_dir = "/cpfs_speech/zhifu.gzf/init_model/qwen/Qwen2-7B-Instruct"
audio_encoder_dir = "/nfs/yangyexin.yyx/init_model/iic/SenseVoiceModelscope_0712"
device = "cuda:0"
all_file_paths = [
    "/nfs/yangyexin.yyx/init_model/audiolm_v14_20240824_train_encoder_all_20240822_lr1e-4_warmup2350/"
]

llm_kwargs = {"num_beams": 1, "do_sample": False}
UNFIX_LEN = 5
MIN_LEN_PER_PARAGRAPH = 10
MIN_LEN_SEC_AUDIO_FIX = 1.1
MAX_ITER_PER_CHUNK = 20

ckpt_dir = all_file_paths[0]

model_llm = AutoModel(
    model=ckpt_dir,
    device=device,
    fp16=False,
    bf16=False,
    llm_dtype="bf16",
    max_length=1024,
    llm_kwargs=llm_kwargs,
    llm_conf={"init_param_path": llm_dir, "load_kwargs": {"attn_implementation": "eager"}},
    tokenizer_conf={"init_param_path": llm_dir},
    audio_encoder=audio_encoder_dir,
)

model = model_llm.model
frontend = model_llm.kwargs["frontend"]
tokenizer = model_llm.kwargs["tokenizer"]

model_dict = {"model": model, "frontend": frontend, "tokenizer": tokenizer}

print("model loaded! only support one client at the same time now!!!!")

def load_bytes(input):
    middle_data = np.frombuffer(input, dtype=np.int16)
    middle_data = np.asarray(middle_data)
    if middle_data.dtype.kind not in "iu":
        raise TypeError("'middle_data' must be an array of integers")
    dtype = np.dtype("float32")
    if dtype.kind != "f":
        raise TypeError("'dtype' must be a floating point type")

    i = np.iinfo(middle_data.dtype)
    abs_max = 2 ** (i.bits - 1)
    offset = i.min + abs_max
    array = np.frombuffer((middle_data.astype(dtype) - offset) / abs_max, dtype=np.float32)
    return array

async def streaming_transcribe(websocket, audio_in, his_state=None, asr_prompt=None, s2tt_prompt=None):
    if his_state is None:
        his_state = model_dict
    model = his_state["model"]
    tokenizer = his_state["tokenizer"]

    if websocket.streaming_state is None:
        previous_asr_text = ""
        previous_s2tt_text = ""
        previous_vad_onscreen_asr_text = ""
        previous_vad_onscreen_s2tt_text = ""
    else:
        previous_asr_text = websocket.streaming_state.get("previous_asr_text", "")
        previous_s2tt_text = websocket.streaming_state.get("previous_s2tt_text", "")
        previous_vad_onscreen_asr_text = websocket.streaming_state.get("previous_vad_onscreen_asr_text", "")
        previous_vad_onscreen_s2tt_text = websocket.streaming_state.get("previous_vad_onscreen_s2tt_text", "")
    
    if asr_prompt is None or asr_prompt == "":
        asr_prompt = "Copy:"
    if s2tt_prompt is None or s2tt_prompt == "":
        s2tt_prompt = "Translate the following sentence into English:"

    audio_seconds = load_bytes(audio_in).shape[0] / 16000
    print(f"Streaming audio length: {audio_seconds} seconds")
    
    asr_content = []
    system_prompt = "You are a helpful assistant."
    asr_content.append({"role": "system", "content": system_prompt})
    s2tt_content = []
    system_prompt = "You are a helpful assistant."
    s2tt_content.append({"role": "system", "content": system_prompt})

    user_asr_prompt = f"{asr_prompt}<|startofspeech|>!!<|endofspeech|><|im_end|>\n<|im_start|>assistant\n{previous_asr_text}"
    user_s2tt_prompt = f"{s2tt_prompt}<|startofspeech|>!!<|endofspeech|><|im_end|>\n<|im_start|>assistant\n{previous_s2tt_text}"

    asr_content.append({"role": "user", "content": user_asr_prompt, "audio": audio_in})
    asr_content.append({"role": "assistant", "content": "target_out"})
    s2tt_content.append({"role": "user", "content": user_s2tt_prompt, "audio": audio_in})
    s2tt_content.append({"role": "assistant", "content": "target_out"})

    streaming_time_beg = time.time()
    inputs_asr_embeds, contents, batch, source_ids, meta_data = model.inference_prepare(
        [asr_content], None, "test_demo", tokenizer, frontend, device=device, infer_with_assistant_input=True
    )
    model_asr_inputs = {}
    model_asr_inputs["inputs_embeds"] = inputs_asr_embeds
    inputs_s2tt_embeds, contents, batch, source_ids, meta_data = model.inference_prepare(
        [s2tt_content], None, "test_demo", tokenizer, frontend, device=device, infer_with_assistant_input=True
    )
    model_s2tt_inputs = {}
    model_s2tt_inputs["inputs_embeds"] = inputs_s2tt_embeds

    print("previous_asr_text:", previous_asr_text)
    print("previous_s2tt_text:", previous_s2tt_text)

    asr_streamer = TextIteratorStreamer(tokenizer)
    asr_generation_kwargs = dict(model_asr_inputs, streamer=asr_streamer, max_new_tokens=1024)
    asr_generation_kwargs.update(llm_kwargs)
    asr_thread = Thread(target=model.llm.generate, kwargs=asr_generation_kwargs)
    asr_thread.start()
    s2tt_streamer = TextIteratorStreamer(tokenizer)
    s2tt_generation_kwargs = dict(model_s2tt_inputs, streamer=s2tt_streamer, max_new_tokens=1024)
    s2tt_generation_kwargs.update(llm_kwargs)
    s2tt_thread = Thread(target=model.llm.generate, kwargs=s2tt_generation_kwargs)
    s2tt_thread.start()
    
    onscreen_asr_res = previous_asr_text
    onscreen_s2tt_res = previous_s2tt_text

    remain_s2tt_text = True

    asr_iter_cnt = 0
    s2tt_iter_cnt = 0
    is_asr_repetition = False
    is_s2tt_repetition = False

    for new_asr_text in asr_streamer:
        print(f"generated new asr text： {new_asr_text}")
        asr_iter_cnt += 1
        if asr_iter_cnt > MAX_ITER_PER_CHUNK:
            is_asr_repetition = True
            break
        if len(new_asr_text) > 0:
            onscreen_asr_res += new_asr_text.replace("<|im_end|>", "")

        if remain_s2tt_text:
            try:
                new_s2tt_text = next(s2tt_streamer)
                print(f"generated new s2tt text： {new_s2tt_text}")
                s2tt_iter_cnt += 1
                if len(new_s2tt_text) > 0:
                    onscreen_s2tt_res += new_s2tt_text.replace("<|im_end|>", "")
            except StopIteration:
                new_s2tt_text = ""
                remain_s2tt_text = False
                pass
        
        if len(new_asr_text) > 0 or len(new_s2tt_text) > 0:
            all_asr_res = previous_vad_onscreen_asr_text + onscreen_asr_res
            fix_asr_part = previous_vad_onscreen_asr_text + previous_asr_text
            unfix_asr_part = all_asr_res[len(fix_asr_part):]
            return_asr_res = fix_asr_part + "<em>"+ unfix_asr_part + "</em>"
            all_s2tt_res = previous_vad_onscreen_s2tt_text + onscreen_s2tt_res
            fix_s2tt_part = previous_vad_onscreen_s2tt_text + previous_s2tt_text
            unfix_s2tt_part = all_s2tt_res[len(fix_s2tt_part):]
            return_s2tt_res = fix_s2tt_part + "<em>"+ unfix_s2tt_part + "</em>"
            message = json.dumps(
                {
                    "mode": "online",
                    "asr_text": return_asr_res,
                    "s2tt_text": return_s2tt_res,
                    "wav_name": websocket.wav_name,
                    "is_final": websocket.is_speaking,
                }
            )
            await websocket.send(message)
        websocket.streaming_state["onscreen_asr_res"] = previous_vad_onscreen_asr_text + onscreen_asr_res
        websocket.streaming_state["onscreen_s2tt_res"] = previous_vad_onscreen_s2tt_text + onscreen_s2tt_res

        
    if remain_s2tt_text:
        for new_s2tt_text in s2tt_streamer:
            print(f"generated new s2tt text： {new_s2tt_text}")
            s2tt_iter_cnt += 1
            if s2tt_iter_cnt > MAX_ITER_PER_CHUNK:
                is_s2tt_repetition = True
                break
            if len(new_s2tt_text) > 0:
                onscreen_s2tt_res += new_s2tt_text.replace("<|im_end|>", "")
            
            if len(new_s2tt_text) > 0:
                all_asr_res = previous_vad_onscreen_asr_text + onscreen_asr_res
                fix_asr_part = previous_vad_onscreen_asr_text + previous_asr_text
                unfix_asr_part = all_asr_res[len(fix_asr_part):]
                return_asr_res = fix_asr_part + "<em>"+ unfix_asr_part + "</em>"
                all_s2tt_res = previous_vad_onscreen_s2tt_text + onscreen_s2tt_res
                fix_s2tt_part = previous_vad_onscreen_s2tt_text + previous_s2tt_text
                unfix_s2tt_part = all_s2tt_res[len(fix_s2tt_part):]
                return_s2tt_res = fix_s2tt_part + "<em>"+ unfix_s2tt_part + "</em>"
                message = json.dumps(
                    {
                        "mode": "online",
                        "asr_text": return_asr_res,
                        "s2tt_text": return_s2tt_res,
                        "wav_name": websocket.wav_name,
                        "is_final": websocket.is_speaking,
                    }
                )
                await websocket.send(message)
            websocket.streaming_state["onscreen_asr_res"] = previous_vad_onscreen_asr_text + onscreen_asr_res
            websocket.streaming_state["onscreen_s2tt_res"] = previous_vad_onscreen_s2tt_text + onscreen_s2tt_res

    streaming_time_end = time.time()
    print(f"Streaming inference time: {streaming_time_end - streaming_time_beg}")

    asr_text_len = len(tokenizer.encode(onscreen_asr_res))
    s2tt_text_len = len(tokenizer.encode(onscreen_s2tt_res))

    if asr_text_len > UNFIX_LEN and audio_seconds > MIN_LEN_SEC_AUDIO_FIX and not is_asr_repetition:
        pre_previous_asr_text = previous_asr_text
        previous_asr_text = tokenizer.decode(tokenizer.encode(onscreen_asr_res)[:-UNFIX_LEN])
        if len(previous_asr_text) < len(pre_previous_asr_text):
            previous_asr_text = pre_previous_asr_text
    elif is_asr_repetition:
        pass
    else:
        previous_asr_text = ""
    if s2tt_text_len > UNFIX_LEN and audio_seconds > MIN_LEN_SEC_AUDIO_FIX and not is_s2tt_repetition:
        pre_previous_s2tt_text = previous_s2tt_text
        previous_s2tt_text = tokenizer.decode(tokenizer.encode(onscreen_s2tt_res)[:-UNFIX_LEN])
        if len(previous_s2tt_text) < len(pre_previous_s2tt_text):
            previous_s2tt_text = pre_previous_s2tt_text
    elif is_s2tt_repetition:
        pass
    else:
        previous_s2tt_text = ""

    websocket.streaming_state["previous_asr_text"] = previous_asr_text
    websocket.streaming_state["onscreen_asr_res"] = previous_vad_onscreen_asr_text + onscreen_asr_res
    websocket.streaming_state["previous_s2tt_text"] = previous_s2tt_text
    websocket.streaming_state["onscreen_s2tt_res"] = previous_vad_onscreen_s2tt_text + onscreen_s2tt_res

    print("fix asr part:", previous_asr_text)
    print("fix s2tt part:", previous_s2tt_text)


async def ws_reset(websocket):
    print("ws reset now, total num is ", len(websocket_users))

    websocket.streaming_state = {}
    websocket.streaming_state["is_final"] = True
    websocket.streaming_state["previous_asr_text"] = ""
    websocket.streaming_state["previous_s2tt_text"] = ""
    websocket.streaming_state["onscreen_asr_res"] = ""
    websocket.streaming_state["onscreen_s2tt_res"] = ""
    websocket.streaming_state["previous_vad_onscreen_asr_text"] = ""
    websocket.streaming_state["previous_vad_onscreen_s2tt_text"] = ""

    websocket.status_dict_vad["cache"] = {}
    websocket.status_dict_vad["is_final"] = True

    await websocket.close()


async def clear_websocket():
    for websocket in websocket_users:
        await ws_reset(websocket)
    websocket_users.clear()


async def ws_serve(websocket, path):
    frames = []
    frames_asr = []
    global websocket_users
    # await clear_websocket()
    websocket_users.add(websocket)
    websocket.streaming_state = {
        "previous_asr_text": "",
        "previous_s2tt_text": "",
        "onscreen_asr_res": "",
        "onscreen_s2tt_res": "",
        "previous_vad_onscreen_asr_text": "",
        "previous_vad_onscreen_s2tt_text": "",
        "is_final": False,
    }
    websocket.status_dict_vad = {"cache": {}, "is_final": False}

    websocket.chunk_interval = 10
    websocket.vad_pre_idx = 0
    speech_start = False
    speech_end_i = -1
    websocket.wav_name = "microphone"
    print("new user connected", flush=True)

    try:
        async for message in websocket:
            if isinstance(message, str):
                messagejson = json.loads(message)

                if "is_speaking" in messagejson:
                    websocket.is_speaking = messagejson["is_speaking"]
                    websocket.streaming_state["is_final"] = not websocket.is_speaking
                    if not messagejson["is_speaking"]:
                        await clear_websocket()
                if "chunk_interval" in messagejson:
                    websocket.chunk_interval = messagejson["chunk_interval"]
                if "wav_name" in messagejson:
                    websocket.wav_name = messagejson.get("wav_name")
                if "chunk_size" in messagejson:
                    chunk_size = messagejson["chunk_size"]
                    if isinstance(chunk_size, str):
                        chunk_size = chunk_size.split(",")
                    chunk_size = [int(x) for x in chunk_size]
                if "asr_prompt" in messagejson:
                    asr_prompt = messagejson["asr_prompt"]
                else:
                    asr_prompt = "Copy:"
                if "s2tt_prompt" in messagejson:
                    s2tt_prompt = messagejson["s2tt_prompt"]
                else:
                    s2tt_prompt = "Translate the following sentence into English:"

            websocket.status_dict_vad["chunk_size"] = int(
                chunk_size[1] * 60 / websocket.chunk_interval
            )
            if len(frames_asr) > 0 or not isinstance(message, str):
                if not isinstance(message, str):
                    frames.append(message)
                    duration_ms = len(message) // 32
                    websocket.vad_pre_idx += duration_ms

                    # asr online
                    websocket.streaming_state["is_final"] = speech_end_i != -1
                    if (
                        (len(frames_asr) % websocket.chunk_interval == 0
                        or websocket.streaming_state["is_final"]) 
                        and len(frames_asr) != 0
                    ):
                        audio_in = b"".join(frames_asr)
                        try:
                            await streaming_transcribe(websocket, audio_in, asr_prompt=asr_prompt, s2tt_prompt=s2tt_prompt)
                        except Exception as e:
                            print(f"error in streaming, {e}")
                            print(f"error in streaming, {websocket.streaming_state}")
                    if speech_start:
                        frames_asr.append(message)
                    
                    # vad online
                    try:
                        speech_start_i, speech_end_i = await async_vad(websocket, message)
                    except:
                        print("error in vad")
                    if speech_start_i != -1:
                        speech_start = True
                        beg_bias = (websocket.vad_pre_idx - speech_start_i) // duration_ms
                        frames_pre = frames[-beg_bias:]
                        frames_asr = []
                        frames_asr.extend(frames_pre)

                # vad end
                if speech_end_i != -1 or not websocket.is_speaking:
                    audio_in = b"".join(frames_asr)
                    try:
                        await streaming_transcribe(websocket, audio_in, asr_prompt=asr_prompt, s2tt_prompt=s2tt_prompt)
                    except Exception as e:
                        print(f"error in streaming, {e}")
                        print(f"error in streaming, {websocket.streaming_state}")
                    frames_asr = []
                    speech_start = False
                    websocket.streaming_state["previous_asr_text"] = ""
                    websocket.streaming_state["previous_s2tt_text"] = ""
                    now_onscreen_asr_res = websocket.streaming_state.get("onscreen_asr_res", "")
                    now_onscreen_s2tt_res = websocket.streaming_state.get("onscreen_s2tt_res", "")
                    if len(tokenizer.encode(now_onscreen_asr_res.split("\n\n")[-1])) < MIN_LEN_PER_PARAGRAPH or len(tokenizer.encode(now_onscreen_s2tt_res.split("\n\n")[-1])) < MIN_LEN_PER_PARAGRAPH:
                        if now_onscreen_asr_res.endswith(".") or now_onscreen_asr_res.endswith("?") or now_onscreen_asr_res.endswith("!"):
                            now_onscreen_asr_res += " "
                        if now_onscreen_s2tt_res.endswith(".") or now_onscreen_s2tt_res.endswith("?") or now_onscreen_s2tt_res.endswith("!"):
                            now_onscreen_s2tt_res += " "
                        websocket.streaming_state["previous_vad_onscreen_asr_text"] = now_onscreen_asr_res
                        websocket.streaming_state["previous_vad_onscreen_s2tt_text"] = now_onscreen_s2tt_res
                    else:
                        websocket.streaming_state["previous_vad_onscreen_asr_text"] = now_onscreen_asr_res + "\n\n"
                        websocket.streaming_state["previous_vad_onscreen_s2tt_text"] = now_onscreen_s2tt_res + "\n\n"
                    if not websocket.is_speaking:
                        websocket.vad_pre_idx = 0
                        frames = []
                        websocket.status_dict_vad["cache"] = {}
                        websocket.streaming_state["previous_asr_text"] = ""
                        websocket.streaming_state["previous_s2tt_text"] = ""
                    else:
                        frames = frames[-20:]
            else:
                print(f"message: {message}")
    except websockets.ConnectionClosed:
        print("ConnectionClosed...", websocket_users, flush=True)
        await ws_reset(websocket)
        websocket_users.remove(websocket)
    except websockets.InvalidState:
        print("InvalidState...")
    except Exception as e:
        print("Exception:", e)


async def async_vad(websocket, audio_in):
    segments_result = model_vad.generate(input=audio_in, **websocket.status_dict_vad)[0]["value"]
    # print(segments_result)

    speech_start = -1
    speech_end = -1

    if len(segments_result) == 0 or len(segments_result) > 1:
        return speech_start, speech_end
    if segments_result[0][0] != -1:
        speech_start = segments_result[0][0]
    if segments_result[0][1] != -1:
        speech_end = segments_result[0][1]
    return speech_start, speech_end


if len(args.certfile) > 0:
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    # Generate with Lets Encrypt, copied to this location, chown to current user and 400 permissions
    ssl_cert = args.certfile
    ssl_key = args.keyfile

    ssl_context.load_cert_chain(ssl_cert, keyfile=ssl_key)
    start_server = websockets.serve(
        ws_serve, args.host, args.port, subprotocols=["binary"], ping_interval=None, ssl=ssl_context
    )
else:
    start_server = websockets.serve(
        ws_serve, args.host, args.port, subprotocols=["binary"], ping_interval=None
    )
asyncio.get_event_loop().run_until_complete(start_server)
asyncio.get_event_loop().run_forever()
