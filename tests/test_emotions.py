import pytest

from speech_engine.emotions import EMOTIONS, apply_emotion
from speech_engine.exceptions import InvalidInputError
from speech_engine.voice_profiles import get_profile


def test_neutral_and_zero_intensity_are_identity():
    p = get_profile("original")
    assert apply_emotion(p, None) is p
    assert apply_emotion(p, "neutral") is p
    assert apply_emotion(p, "sad", 0) is p


def test_sad_is_slower_and_lower_with_longer_pauses():
    p = get_profile("original")
    s = apply_emotion(p, "sad", 1.0)
    assert s.speed < p.speed and s.pitch_semitones < p.pitch_semitones
    assert s.sentence_pause_ms > p.sentence_pause_ms


def test_intensity_scales_effect():
    p = get_profile("original")
    assert p.speed < apply_emotion(p, "excited", 0.3).speed < apply_emotion(p, "excited", 1.0).speed


def test_google_style_gets_instruction():
    assert "sadly" in apply_emotion(get_profile("original"), "sad", 1.0).google_style


@pytest.mark.parametrize("name", list(EMOTIONS))
def test_every_preset_stays_valid_for_every_voice(name):
    from speech_engine.voice_profiles import load_profiles
    for p in load_profiles().values():
        apply_emotion(p.with_overrides(speed=2.0), name, 1.0)
        apply_emotion(p.with_overrides(speed=0.5), name, 1.0)


def test_bad_input():
    p = get_profile("original")
    with pytest.raises(InvalidInputError):
        apply_emotion(p, "bored")
    with pytest.raises(InvalidInputError):
        apply_emotion(p, "sad", 2)


def test_time_stretch_changes_length_not_pitch():
    import numpy as np
    from speech_engine.audio_pipeline import time_stretch
    from speech_engine.backends.base import AudioChunk
    r = 24000
    t = np.arange(r * 2) / r
    pcm = (np.sin(2 * np.pi * 220 * t) * 12000).astype("<i2").tobytes()
    for sp in (0.7, 1.5, 2.0):
        out = time_stretch(AudioChunk(pcm, r), sp)
        assert abs(out.duration - 2 / sp) < 0.15
        y = np.frombuffer(out.pcm, "<i2").astype(float)
        zc = np.sum(np.diff(np.sign(y)) != 0) / 2 / out.duration
        assert abs(zc - 220) < 12
