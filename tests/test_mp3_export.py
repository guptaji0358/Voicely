"""MP3 export tests. Need 'lameenc'; use 'miniaudio' as an independent decoder when installed."""

import errno
import json
import re
import sys

import numpy as np
import pytest

from speech_engine import SpeechEngine, cli
from speech_engine.audio_pipeline import float_to_pcm16
from speech_engine.backends.base import AudioChunk, Backend, BackendStatus, SynthParams
from speech_engine.encoders import (parse_bitrate, resolve_format, resolve_mp3_settings,
                                    validate_mp3)
from speech_engine.exceptions import (AudioOutputError, EncoderUnavailableError, InvalidInputError,
                                      SynthesisError)
from speech_engine.voice_profiles import get_profile

pytest.importorskip("lameenc")
miniaudio = pytest.importorskip("miniaudio", reason="independent MP3 decoder not installed")

RATE = 24000


class ToneBackend(Backend):
    """Each synthesized chunk is a 0.5 s sine whose frequency encodes its order: 400, 600, 800 Hz..."""
    name = "tone"
    max_chunk_chars = 30

    def __init__(self, fail_at=None):
        self.n, self.fail_at = 0, fail_at

    def status(self):
        return BackendStatus("tone", True, False)

    def synthesize(self, text, profile, params, cancel):
        if self.fail_at is not None and self.n == self.fail_at:
            raise SynthesisError("backend exploded")
        f = 400 + 200 * self.n
        self.n += 1
        t = np.arange(RATE // 2) / RATE
        return AudioChunk(float_to_pcm16((0.4 * np.sin(2 * np.pi * f * t)).astype(np.float32)), RATE)


TEXT = "Alpha sentence one. Bravo sentence two. Charlie sentence three."


def engine(**kw):
    return SpeechEngine(ToneBackend(**kw), get_profile("original").with_overrides())


def decode(path):
    d = miniaudio.mp3_read_file_f32(str(path))
    return np.array(d.samples, dtype=np.float32), d.sample_rate


def dominant_freq(x, rate):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / rate)[spec.argmax()]


def leftovers(d):
    return [p.name for p in d.iterdir() if p.name.endswith(".part")]


# 1-3, 10: genuine, independently decodable MP3 --------------------------------
def test_export_is_real_mp3_decodable_by_independent_decoder(tmp_path):
    out = tmp_path / "s.mp3"
    info = engine().synthesize_to_file(TEXT, out, "mp3")
    raw = out.read_bytes()
    assert not raw.startswith(b"RIFF") and (raw[0] == 0xFF and raw[1] & 0xE0 == 0xE0 or raw[:3] == b"ID3")
    x, rate = decode(out)
    assert rate == 44100 and len(x) / rate == pytest.approx(info["duration_s"], abs=0.1)
    assert np.abs(x).max() > 0.2  # audible, not silence
    assert validate_mp3(out)["frames"] == info["mp3_frames"]


def test_renamed_wav_and_garbage_are_rejected_by_validator(tmp_path):
    wav = tmp_path / "x.wav"
    engine().synthesize_to_file(TEXT, wav, "wav")
    fake = tmp_path / "fake.mp3"
    fake.write_bytes(wav.read_bytes())
    with pytest.raises(SynthesisError):
        validate_mp3(fake)
    with pytest.raises(Exception):
        miniaudio.mp3_read_file_f32(str(fake))  # an independent decoder agrees it is not MP3
    real = tmp_path / "r.mp3"
    engine().synthesize_to_file(TEXT, real, "mp3")
    trunc = tmp_path / "t.mp3"
    trunc.write_bytes(real.read_bytes()[:-500])
    with pytest.raises(SynthesisError):
        validate_mp3(trunc)


# 4: bitrate ---------------------------------------------------------------------
@pytest.mark.parametrize("kbps", [64, 128, 192, 320])
def test_selected_bitrate_is_applied(tmp_path, kbps):
    out = tmp_path / f"{kbps}.mp3"
    info = engine().synthesize_to_file(TEXT, out, "mp3", bitrate_kbps=kbps)
    assert validate_mp3(out)["bitrate_kbps"] == kbps == info["bitrate_kbps"]
    assert out.stat().st_size == pytest.approx(kbps * 1000 / 8 * info["duration_s"], rel=0.1)


def test_bitrate_rules():
    assert resolve_mp3_settings(None, None) == (44100, 192)
    assert resolve_mp3_settings(24000, None) == (24000, 160)  # 192k illegal at 24 kHz -> best valid default
    with pytest.raises(InvalidInputError, match="valid MP3 bitrate"):
        resolve_mp3_settings(24000, 192)
    with pytest.raises(InvalidInputError):
        resolve_mp3_settings(44100, 100)
    with pytest.raises(InvalidInputError):
        resolve_mp3_settings(10000, None)
    assert parse_bitrate("192k") == parse_bitrate(192) == parse_bitrate("192") == 192
    with pytest.raises(InvalidInputError):
        parse_bitrate("fast")


# 5: ordering / continuity ---------------------------------------------------------
def test_chunks_are_combined_in_order_without_gaps_or_duplicates(tmp_path):
    out = tmp_path / "o.mp3"
    eng = engine()
    eng.synthesize_to_file(TEXT, out, "mp3")
    assert eng.backend.n == 3
    x, rate = decode(out)
    # locate the three tone bursts (pauses between them are silence) and check ascending frequency
    env = np.convolve(np.abs(x), np.ones(441) / 441, mode="same")  # ~10 ms envelope
    active = env > 0.1
    edges = np.flatnonzero(np.diff(active.astype(int)))
    starts, ends = edges[::2] + 1, edges[1::2] + 1
    bursts = [(s, e) for s, e in zip(starts, ends) if e - s > rate * 0.2]
    assert len(bursts) == 3
    freqs = [dominant_freq(x[s + 2000:e - 2000], rate) for s, e in bursts]
    assert freqs == pytest.approx([400, 600, 800], abs=15)
    # no click: largest sample-to-sample step inside a burst stays small relative to a 800 Hz tone
    assert np.abs(np.diff(x)).max() < 0.25


# 6, 7: failures -------------------------------------------------------------------
def test_synthesis_failure_leaves_nothing_and_raises(tmp_path):
    out = tmp_path / "f.mp3"
    with pytest.raises(SynthesisError):
        engine(fail_at=2).synthesize_to_file(TEXT, out, "mp3")
    assert not out.exists() and not leftovers(tmp_path)


def test_failure_does_not_destroy_existing_file(tmp_path):
    out = tmp_path / "keep.mp3"
    engine().synthesize_to_file(TEXT, out, "mp3")
    before = out.read_bytes()
    with pytest.raises(SynthesisError):
        engine(fail_at=1).synthesize_to_file(TEXT, out, "mp3")
    assert out.read_bytes() == before and not leftovers(tmp_path)


def test_encoder_failure_midway_cleans_up(tmp_path, monkeypatch):
    import lameenc
    real = lameenc.Encoder

    class Bad:
        calls = 0
        def __init__(self): self._e = real()
        def __getattr__(self, name): return getattr(self._e, name)
        def encode(self, pcm):
            Bad.calls += 1
            if Bad.calls == 2:
                raise RuntimeError("lame failed")
            return self._e.encode(pcm)

    monkeypatch.setattr(lameenc, "Encoder", Bad)
    with pytest.raises(SynthesisError, match="MP3 encoding failed"):
        engine().synthesize_to_file(TEXT, tmp_path / "e.mp3", "mp3")
    assert not (tmp_path / "e.mp3").exists() and not leftovers(tmp_path)


def test_disk_full_is_actionable_and_cleans_up(tmp_path, monkeypatch):
    from speech_engine import audio_pipeline
    from speech_engine.encoders import Mp3FileSink

    def full(self, chunk):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(Mp3FileSink, "_write", full)
    with pytest.raises(AudioOutputError, match="insufficient disk space"):
        engine().synthesize_to_file(TEXT, tmp_path / "d.mp3", "mp3")
    assert not leftovers(tmp_path) and not (tmp_path / "d.mp3").exists()


def test_empty_audio_is_not_a_success(tmp_path):
    from speech_engine.encoders import Mp3FileSink
    s = Mp3FileSink(tmp_path / "z.mp3")
    with pytest.raises(SynthesisError, match="no audio"):
        s.commit()
    assert not (tmp_path / "z.mp3").exists() and not leftovers(tmp_path)


def test_cli_failure_prints_no_success_and_returns_error_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_backend", lambda n: ToneBackend(fail_at=1))
    rc = cli.main(["speak", "--text", TEXT, "-o", str(tmp_path / "c.mp3"), "--json", "-q"])
    cap = capsys.readouterr()
    assert rc == 5 and '"ok": true' not in cap.out.lower() and not (tmp_path / "c.mp3").exists()
    assert not leftovers(tmp_path)


# 8: other formats still work, format inference/validation -------------------------
def test_wav_and_pcm_still_work(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_backend", lambda n: ToneBackend())
    for name in ("a.wav", "a.pcm"):
        assert cli.main(["speak", "--text", TEXT, "-o", str(tmp_path / name), "-q", "--json"]) == 0
        capsys.readouterr()
    import wave
    with wave.open(str(tmp_path / "a.wav")) as w:
        assert w.getframerate() == RATE and w.getnframes() > 0
    assert (tmp_path / "a.pcm").stat().st_size > 0 and not leftovers(tmp_path)


def test_format_resolution():
    assert resolve_format("x.MP3", None) == "mp3"
    assert resolve_format("x.mp3", "mp3") == "mp3"
    assert resolve_format("x", "mp3") == "mp3"
    with pytest.raises(InvalidInputError, match="does not match"):
        resolve_format("x.wav", "mp3")
    with pytest.raises(InvalidInputError, match="cannot infer"):
        resolve_format("x.bin", None)
    with pytest.raises(InvalidInputError):
        resolve_format("x.ogg", "ogg")


def test_cli_mp3_end_to_end_and_flags(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_backend", lambda n: ToneBackend())
    out = tmp_path / "n.mp3"
    assert cli.main(["speak", "--text", TEXT, "--format", "mp3", "--bitrate", "128k", "-o", str(out),
                     "--json", "-q"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["bitrate_kbps"] == 128 and validate_mp3(out)["bitrate_kbps"] == 128
    # mismatched extension / bitrate on wav / bad bitrate / missing output
    assert cli.main(["speak", "--text", TEXT, "--format", "mp3", "-o", str(tmp_path / "m.wav")]) == 2
    assert cli.main(["speak", "--text", TEXT, "-o", str(tmp_path / "w.wav"), "--bitrate", "128k"]) == 2
    assert cli.main(["speak", "--text", TEXT, "-o", str(tmp_path / "b.mp3"), "--bitrate", "100k", "--force"]) == 2
    assert cli.main(["speak", "--text", TEXT, "--bitrate", "128k"]) == 2
    assert not list(tmp_path.glob("m.*")) + list(tmp_path.glob("w.*")) + list(tmp_path.glob("b.*"))


def test_no_overwrite_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_backend", lambda n: ToneBackend())
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": lambda self: False})())
    out = tmp_path / "e.mp3"
    out.write_bytes(b"precious")
    assert cli.main(["speak", "--text", TEXT, "-o", str(out), "-q"]) == 2
    assert out.read_bytes() == b"precious"
    assert cli.main(["speak", "--text", TEXT, "-o", str(out), "-q", "--force"]) == 0
    assert validate_mp3(out)["frames"] > 0


# 9: missing encoder ------------------------------------------------------------------
def test_missing_encoder_gives_install_instructions(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "lameenc", None)  # makes `import lameenc` raise ImportError
    with pytest.raises(EncoderUnavailableError, match="pip install lameenc"):
        engine().synthesize_to_file(TEXT, tmp_path / "x.mp3", "mp3")
    monkeypatch.setattr(cli, "make_backend", lambda n: ToneBackend())
    assert cli.main(["speak", "--text", TEXT, "-o", str(tmp_path / "y.mp3")]) == 8
    assert "pip install lameenc" in capsys.readouterr().err
    # WAV unaffected by the missing MP3 encoder
    assert cli.main(["speak", "--text", TEXT, "-o", str(tmp_path / "y.wav"), "-q"]) == 0


def test_test_command_reports_mp3_encoder(capsys):
    assert cli.main(["test", "--backend", "local", "--json"]) in (0, 4)
    assert json.loads(capsys.readouterr().out)["backends"]["encoders"]["mp3"] is True
