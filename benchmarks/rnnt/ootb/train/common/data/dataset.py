# Copyright (c) 2019, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Modifications Copyright (c) 2022 Advanced Micro Devices, Inc. All rights reserved.
# Notified per clause 4(b) of the license

import json
from pathlib import Path

import numpy as np

import torch
from torch.utils.data import Dataset, DataLoader
from torch.utils.data.distributed import DistributedSampler
import torchaudio
from common.audio import (audio_from_file, AudioSegment, SpeedPerturbation)
from common.text import _clean_text, punctuation_map

from common.helpers import print_once
from common.sampler import BucketingSampler


def normalize_string(s, charset, punct_map):
    """Normalizes string.

    Example:
        'call me at 8:00 pm!' -> 'call me at eight zero pm'
    """
    charset = set(charset)
    try:
        text = _clean_text(s, ["english_cleaners"], punct_map).strip()
        return ''.join([tok for tok in text if all(t in charset for t in tok)])
    except:
        print(f"WARNING: Normalizing failed: {s}")
        return None


class FilelistDataset(Dataset):
    def __init__(self, filelist_fpath):
        self.samples = [line.strip() for line in open(filelist_fpath, 'r')]

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        audio, audio_len = audio_from_file(self.samples[index])
        return (audio.squeeze(0), audio_len, torch.LongTensor([0]),
                torch.LongTensor([0]))


class SingleAudioDataset(FilelistDataset):
    def __init__(self, audio_fpath):
        self.samples = [audio_fpath]


class AudioDataset(Dataset):
    def __init__(self, data_dir, manifest_fpaths, gpu_id,
                 tokenizer,
                 sample_rate=16000, min_duration=0.1, max_duration=float("inf"),
                 max_utts=0, normalize_transcripts=True,
                 trim_silence=False,
                 speed_perturbation=None,
                 ignore_offline_speed_perturbation=False,
                 n_filt=80, n_fft=512,
                device_type="gpu"):
        """Loads audio, transcript and durations listed in a .json file.

        Args:
            data_dir: absolute path to dataset folder
            manifest_filepath: relative path from dataset folder
                to manifest json as described above. Can be coma-separated paths.
            tokenizer: class converting transcript to tokens
            min_duration (int): skip audio shorter than threshold
            max_duration (int): skip audio longer than threshold
            max_utts (int): limit number of utterances
            normalize_transcripts (bool): normalize transcript text
            trim_silence (bool): trim leading and trailing silence from audio
            ignore_offline_speed_perturbation (bool): use precomputed speed perturbation

        Returns:
            tuple of Tensors
        """
        self.data_dir = data_dir
        self.gpu_id = gpu_id
        self.tokenizer = tokenizer
        self.punctuation_map = punctuation_map(self.tokenizer.charset)

        self.max_utts = max_utts
        self.normalize_transcripts = normalize_transcripts
        self.ignore_offline_speed_perturbation = ignore_offline_speed_perturbation

        self.min_duration = min_duration
        self.max_duration = max_duration
        self.trim_silence = trim_silence
        self.sample_rate = sample_rate
        self.n_filt = n_filt
        self.n_fft = n_fft
        self.device_type = device_type
        self.device = "cuda:"+str(self.gpu_id) if self.device_type == "gpu" else "cpu"

        perturbations = []
        if speed_perturbation is not None:
            perturbations.append(SpeedPerturbation(**speed_perturbation))
        self.perturbations = perturbations

        self.max_duration = max_duration

        self.transformMel = torchaudio.transforms.MelSpectrogram(sample_rate=self.sample_rate, n_mels=self.n_filt, n_fft=self.n_fft).to(self.device)
        self.transformToDB = torchaudio.transforms.AmplitudeToDB(top_db=20).to(self.device)

        self.samples = []
        self.duration = 0.0
        self.duration_filtered = 0.0

        for fpath in manifest_fpaths:
            self._load_json_manifest(fpath)

    def __getitem__(self, index):
        s = self.samples[index]
        rn_indx = np.random.randint(len(s['audio_filepath']))
        duration = s['audio_duration'][rn_indx] if 'audio_duration' in s else 0
        offset = s.get('offset', 0)

        segment = AudioSegment(
            s['audio_filepath'][rn_indx], target_sr=self.sample_rate,
            offset=offset, duration=duration, trim=self.trim_silence, trim_db=-60)

        for p in self.perturbations:
            p.maybe_apply(segment, self.sample_rate)

        segment = torch.FloatTensor(segment.samples).to(self.device)
        segment = self.transformMel(segment)
        segment = self.transformToDB(segment)


        return (segment,
                torch.tensor(segment.shape[1]).int(),
                torch.tensor(s["transcript"]),
                torch.tensor(len(s["transcript"])).int())

    def __len__(self):
        return len(self.samples)

    def _load_json_manifest(self, fpath):
        j = json.load(open(fpath, "r", encoding="utf-8"))
        for i, s in enumerate(j):
            if i % 1000 == 0:
                print(f'{i:>10}/{len(j):<10}', end='\r')

            s_max_duration = s['original_duration']

            s['duration'] = s.pop('original_duration')
            if not (self.min_duration <= s_max_duration <= self.max_duration):
                self.duration_filtered += s['duration']
                continue

            # Prune and normalize according to transcript
            tr = (s.get('transcript', None) or
                  self.load_transcript(s['text_filepath']))

            if not isinstance(tr, str):
                print(f'WARNING: Skipped sample (transcript not a str): {tr}.')
                self.duration_filtered += s['duration']
                continue

            if self.normalize_transcripts:
                tr = normalize_string(tr, self.tokenizer.charset, self.punctuation_map)

            s["transcript"] = self.tokenizer.tokenize(tr)
            files = s.pop('files')
            if self.ignore_offline_speed_perturbation:
                files = [f for f in files if f['speed'] == 1.0]

            s['audio_duration'] = [f['duration'] for f in files]
            s['audio_filepath'] = [str(Path(self.data_dir, f['fname']))
                                   for f in files]
            self.samples.append(s)
            self.duration += s['duration']

            if self.max_utts > 0 and len(self.samples) >= self.max_utts:
                print(f'Reached max_utts={self.max_utts}. Finished parsing {fpath}.')
                break

    def load_transcript(self, transcript_path):
        with open(transcript_path, 'r', encoding="utf-8") as transcript_file:
            transcript = transcript_file.read().replace('\n', '')
        return transcript

