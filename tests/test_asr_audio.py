"""ASR input decoding (no model needed): our own WAV loader must not depend on PyAV/ffmpeg."""
import wave

import numpy as np
import pytest

from speech_engine.asr import _load_wav_16k
from speech_engine.exceptions import ConfigurationError


def _wav(path, rate, ch=1, width=2, secs=0.5):
    t = np.arange(int(rate * secs)) / rate
    x = (np.sin(2 * np.pi * 440 * t) * 12000).astype("<i2")
    if ch > 1:
        x = np.repeat(x[:, None], ch, axis=1).ravel()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch); w.setsampwidth(width); w.setframerate(rate)
        w.writeframes(x.tobytes() if width == 2 else (x >> 8).astype("i1").tobytes())


def test_resamples_to_16k_mono_float(tmp_path):
    _wav(tmp_path / "a.wav", 24000)
    x = _load_wav_16k(tmp_path / "a.wav")
    assert x.dtype == np.float32 and abs(len(x) - 8000) <= 1 and 0.3 < np.abs(x).max() < 0.5


def test_stereo_is_downmixed(tmp_path):
    _wav(tmp_path / "s.wav", 16000, ch=2)
    assert _load_wav_16k(tmp_path / "s.wav").ndim == 1


def test_rejects_non_16bit(tmp_path):
    _wav(tmp_path / "b.wav", 16000, width=1)
    with pytest.raises(ConfigurationError):
        _load_wav_16k(tmp_path / "b.wav")
