"""REAL (non-mocked) checks. Each one is skipped, not faked, when its prerequisite is missing:
  * local Kokoro model installed  -> IPA overrides really change the synthesized audio
  * faster-whisper + a downloaded model -> a real speech -> ASR -> comparison round trip
"""

import threading
import wave

import pytest

from speech_engine.backends.base import SynthParams
from speech_engine.backends.local_backend import LocalBackend, find_model_files
from speech_engine.voice_profiles import ORIGINAL

needs_kokoro = pytest.mark.skipif(not all(find_model_files()), reason="Kokoro model files not installed")


def real_asr():
    from speech_engine.asr import FasterWhisperASR
    a = FasterWhisperASR()
    return a if a.status().available else None


@needs_kokoro
def test_ipa_override_changes_real_audio():
    b = LocalBackend()
    try:
        ev = threading.Event()
        plain = b.synthesize("Please call Nguyen now.", ORIGINAL, SynthParams(), ev)
        win = b.synthesize("Please call Nguyen now.", ORIGINAL, SynthParams(overrides=(("Nguyen", "wˈɪn"),)), ev)
        assert plain.pcm != win.pcm and abs(plain.duration - win.duration) < 1.0
        assert b.ipa_vocabulary() and "ˈ" in b.ipa_vocabulary()
    finally:
        b.close()


@needs_kokoro
def test_real_asr_round_trip_detects_wrong_text(tmp_path):
    asr = real_asr()
    if asr is None:
        pytest.skip("faster-whisper and a downloaded ASR model are not installed")
    from speech_engine.session import write_wav
    from speech_engine.verification import Thresholds, verify_chunk
    b = LocalBackend()
    try:
        audio = b.synthesize("The quick brown fox jumps over the lazy dog.", ORIGINAL, SynthParams(), threading.Event())
    finally:
        b.close()
    wav = tmp_path / "c.wav"
    write_wav(wav, audio)
    th = Thresholds()
    good = verify_chunk(0, "g0", wav, "The quick brown fox jumps over the lazy dog.", [], asr, "en-us", th)
    assert good.asr["mocked"] is False and good.status in ("verified", "needs_review")
    wrong = verify_chunk(0, "g0", wav, "The quick brown cat sits under the busy road.", [], asr, "en-us", th)
    assert wrong.status in ("needs_review", "failed") and wrong.issues
