"""Real Kokoro synthesis -> MP3. Skipped unless kokoro-onnx, lameenc, miniaudio and the model files exist."""
import pytest

from speech_engine import SpeechEngine, get_profile, make_backend
from speech_engine.encoders import validate_mp3

miniaudio = pytest.importorskip("miniaudio")
pytest.importorskip("lameenc")
backend = make_backend("local")
if not backend.status().available:
    pytest.skip("local backend (kokoro-onnx + model files) not available", allow_module_level=True)

PARAS = "\n\n".join(
    f"Paragraph {i}. The quick brown fox jumps over the lazy dog, and then it rests for a while. "
    "Is it done yet? Not quite - there is more to say." for i in range(1, 6))


@pytest.mark.parametrize("voice", ["original", "boy"])
def test_real_voice_exports_decodable_mp3(tmp_path, voice):
    out = tmp_path / f"{voice}.mp3"
    info = SpeechEngine(backend, get_profile(voice)).synthesize_to_file(PARAS, out, "mp3")
    d = miniaudio.mp3_read_file_f32(str(out))
    assert d.sample_rate == 44100 and d.duration == pytest.approx(info["duration_s"], abs=0.1)
    assert d.duration > 15 and max(abs(v) for v in d.samples) > 0.1
    assert validate_mp3(out)["bitrate_kbps"] == 192
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".part")]
