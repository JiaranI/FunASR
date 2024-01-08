import os
import torch
import json
import torch.distributed as dist
import numpy as np
import kaldiio
import librosa
import torchaudio
import time
import logging
from torch.nn.utils.rnn import pad_sequence
try:
	from urllib.parse import urlparse
	from funasr.download.file import HTTPStorage
	import tempfile
except:
	print("urllib is not installed, if you infer from url, please install it first.")
# def load_audio(data_or_path_or_list, fs: int=16000, audio_fs: int=16000):
#
# 	if isinstance(data_or_path_or_list, (list, tuple)):
# 		return [load_audio(audio, fs=fs, audio_fs=audio_fs) for audio in data_or_path_or_list]
#
# 	if isinstance(data_or_path_or_list, str) and os.path.exists(data_or_path_or_list):
# 		data_or_path_or_list, audio_fs = torchaudio.load(data_or_path_or_list)
# 		data_or_path_or_list = data_or_path_or_list[0, :]
# 	elif isinstance(data_or_path_or_list, np.ndarray): # audio sample point
# 		data_or_path_or_list = np.squeeze(data_or_path_or_list) #[n_samples,]
#
# 	if audio_fs != fs:
# 		resampler = torchaudio.transforms.Resample(audio_fs, fs)
# 		data_or_path_or_list = resampler(data_or_path_or_list[None, :])[0, :]
# 	return data_or_path_or_list


def load_audio_text_image_video(data_or_path_or_list, fs: int = 16000, audio_fs: int = 16000, data_type=None, tokenizer=None):
	if isinstance(data_or_path_or_list, (list, tuple)):
		if data_type is not None and isinstance(data_type, (list, tuple)):

			data_types = [data_type] * len(data_or_path_or_list)
			data_or_path_or_list_ret = [[] for d in data_type]
			for i, (data_type_i, data_or_path_or_list_i) in enumerate(zip(data_types, data_or_path_or_list)):
				
				for j, (data_type_j, data_or_path_or_list_j) in enumerate(zip(data_type_i, data_or_path_or_list_i)):
					
					data_or_path_or_list_j = load_audio_text_image_video(data_or_path_or_list_j, fs=fs, audio_fs=audio_fs, data_type=data_type_j, tokenizer=tokenizer)
					data_or_path_or_list_ret[j].append(data_or_path_or_list_j)

			return data_or_path_or_list_ret
		else:
			return [load_audio_text_image_video(audio, fs=fs, audio_fs=audio_fs) for audio in data_or_path_or_list]
	if isinstance(data_or_path_or_list, str) and data_or_path_or_list.startswith('http'):
		data_or_path_or_list = download_from_url(data_or_path_or_list)
	if isinstance(data_or_path_or_list, str) and os.path.exists(data_or_path_or_list):
		data_or_path_or_list, audio_fs = torchaudio.load(data_or_path_or_list)
		data_or_path_or_list = data_or_path_or_list[0, :]
	elif isinstance(data_or_path_or_list, np.ndarray):  # audio sample point
		data_or_path_or_list = np.squeeze(data_or_path_or_list)  # [n_samples,]
	elif isinstance(data_or_path_or_list, str) and data_type is not None and data_type == "text" and tokenizer is not None:
		data_or_path_or_list = tokenizer.encode(data_or_path_or_list)
		
	if audio_fs != fs and data_type != "text":
		resampler = torchaudio.transforms.Resample(audio_fs, fs)
		data_or_path_or_list = resampler(data_or_path_or_list[None, :])[0, :]
	return data_or_path_or_list

def load_bytes(input):
	middle_data = np.frombuffer(input, dtype=np.int16)
	middle_data = np.asarray(middle_data)
	if middle_data.dtype.kind not in 'iu':
		raise TypeError("'middle_data' must be an array of integers")
	dtype = np.dtype('float32')
	if dtype.kind != 'f':
		raise TypeError("'dtype' must be a floating point type")
	
	i = np.iinfo(middle_data.dtype)
	abs_max = 2 ** (i.bits - 1)
	offset = i.min + abs_max
	array = np.frombuffer((middle_data.astype(dtype) - offset) / abs_max, dtype=np.float32)
	return array

def extract_fbank(data, data_len = None, data_type: str="sound", frontend=None):
	# import pdb;
	# pdb.set_trace()
	if isinstance(data, np.ndarray):
		data = torch.from_numpy(data)
		if len(data.shape) < 2:
			data = data[None, :] # data: [batch, N]
		data_len = [data.shape[1]] if data_len is None else data_len
	elif isinstance(data, torch.Tensor):
		if len(data.shape) < 2:
			data = data[None, :] # data: [batch, N]
		data_len = [data.shape[1]] if data_len is None else data_len
	elif isinstance(data, (list, tuple)):
		data_list, data_len = [], []
		for data_i in data:
			if isinstance(data, np.ndarray):
				data_i = torch.from_numpy(data_i)
			data_list.append(data_i)
			data_len.append(data_i.shape[0])
		data = pad_sequence(data_list, batch_first=True) # data: [batch, N]
	# import pdb;
	# pdb.set_trace()
	# if data_type == "sound":
	data, data_len = frontend(data, data_len)
	
	if isinstance(data_len, (list, tuple)):
		data_len = torch.tensor([data_len])
	return data.to(torch.float32), data_len.to(torch.int32)

def download_from_url(url):
	
	result = urlparse(url)
	file_path = None
	if result.scheme is not None and len(result.scheme) > 0:
		storage = HTTPStorage()
		# bytes
		data = storage.read(url)
		work_dir = tempfile.TemporaryDirectory().name
		if not os.path.exists(work_dir):
			os.makedirs(work_dir)
		file_path = os.path.join(work_dir, os.path.basename(url))
		with open(file_path, 'wb') as fb:
			fb.write(data)
	assert file_path is not None, f"failed to download: {url}"
	return file_path