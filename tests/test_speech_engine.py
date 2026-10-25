"""Unit tests. No network, no model weights, no audio device required."""

import base64
import io
import json
import threading
import time
import wave
from types import SimpleNamespace

import numpy as np
import pytest

from speech_engine import SpeechEngine, cli
from speech_engine.audio_pipeline import (Player, WavFileSink, collect, decode_audio_bytes,
                                          float_to_pcm16, resample)
from speech_engine.backends.base import AudioChunk, Backend, BackendStatus, SynthParams
from speech_engine.backends.google_backend import GoogleBackend
from speech_engine.exceptions import (ConfigurationError, InvalidInputError, RateLimitError,
                                      SynthesisCancelled, SynthesisError)
from speech_engine.text_preprocessor import iter_chunks, normalize
from speech_engine.voice_profiles import get_profile, load_profiles

RATE = 24000


class FakeBackend(Backend):
    name = "fake"
    max_chunk_chars = 60

    def __init__(self, fail_on=None, delay=0.0):
        self.calls, self.fail_on, self.delay = [], fail_on, delay

    def status(self):
        return BackendStatus("fake", True, False)

    def synthesize(self, text, profile, params, cancel):
        if cancel.is_set():
            raise SynthesisCancelled("c")
        time.sleep(self.delay)
        self.calls.append((text, profile.name))
        if self.fail_on and self.fail_on in text:
            raise SynthesisError("boom")
        return AudioChunk(float_to_pcm16(np.full(2400, 0.2, np.float32)), RATE)


def wav_bytes(n=2400, rate=RATE, width=2):
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1); w.setsampwidth(width); w.setframerate(rate)
        w.writeframes(b"\x01\x00" * n if width == 2 else b"\x01" * n)
    return b.getvalue()


# ---- text ---------------------------------------------------------------
def test_empty_and_symbol_only_rejected():
    for t in ("", "   \n\n ", "...!!!", "​\x00"):
        with pytest.raises(InvalidInputError):
            list(iter_chunks(t))


def test_ocr_hyphen_and_wrap_repair():
    assert normalize("exam-\nple text") == "example text"
    chunks = list(iter_chunks("one line\nwrapped line.\n\nSecond para."))
    assert [c.text for c in chunks] == ["one line wrapped line.", "Second para."]
    assert chunks[0].paragraph_end and chunks[0].pause_ms == 650


def test_chunks_bounded_and_lossless():
    text = " ".join(f"Sentence number {i} is here." for i in range(200)) + " " + "x" * 1000
    chunks = list(iter_chunks(text, max_chars=100))
    assert all(len(c.text) <= 100 for c in chunks)
    joined = "".join(c.text for c in chunks).replace(" ", "")
    assert joined == text.replace(" ", "")


def test_unicode_multilingual_preserved():
    chunks = list(iter_chunks("नमस्ते दुनिया। 你好，世界。 Привет мир."))
    out = " ".join(c.text for c in chunks)
    assert "नमस्ते" in out and "Привет" in out and "你好" in out


def test_abbreviation_does_not_split_sentence():
    assert list(iter_chunks("Dr. Patel arrived."))[0].text == "Doctor Patel arrived."


def test_generator_is_lazy():
    def gen():
        yield from iter_chunks("A sentence here. " * 10_000, max_chars=50)
    g = gen(); next(g)  # must not materialise everything


# ---- audio --------------------------------------------------------------
def test_wav_decoded_not_treated_as_raw():
    c = decode_audio_bytes(wav_bytes(100))
    assert len(c.pcm) == 200 and c.sample_rate == RATE


def test_mp3_bytes_rejected():
    with pytest.raises(SynthesisError):
        decode_audio_bytes(b"ID3\x04\x00" + b"\x00" * 100)
    with pytest.raises(SynthesisError):
        decode_audio_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)


def test_unknown_payload_rejected_but_declared_l16_ok():
    with pytest.raises(SynthesisError):
        decode_audio_bytes(b"\x01\x02\x03\x04")
    c = decode_audio_bytes(b"\x01\x02\x03\x04", "audio/l16", 16000)
    assert c.sample_rate == 16000 and len(c.pcm) == 4


def test_8bit_wav_rejected():
    with pytest.raises(SynthesisError):
        decode_audio_bytes(wav_bytes(10, width=1))


def test_float_to_pcm_clips_and_handles_nan():
    pcm = np.frombuffer(float_to_pcm16(np.array([2.0, -2.0, np.nan], np.float32)), "<i2")
    assert list(pcm) == [32767, -32767, 0]


def test_resample_length():
    c = AudioChunk(b"\x00\x01" * 24000, 24000)
    assert len(resample(c, 16000).pcm) // 2 == 16000


# ---- engine -------------------------------------------------------------
def test_engine_chunks_and_pauses():
    be = FakeBackend()
    eng = SpeechEngine(be, get_profile("original"))
    out = list(eng.iter_audio("First sentence is here. Second one follows.\n\nNew paragraph now."))
    assert len(be.calls) >= 2
    assert any(set(c.pcm) == {0} for c in out)  # silence segments exist
    assert {p for _, p in be.calls} == {"original"}


def test_failure_raises_and_never_substitutes():
    be = FakeBackend(fail_on="bad")
    eng = SpeechEngine(be, get_profile("original"))
    with pytest.raises(SynthesisError):
        list(eng.iter_audio("good text here. bad text here."))
    assert {p for _, p in be.calls} == {"original"}


def test_cancel_stops_generation():
    be = FakeBackend()
    ev = threading.Event()
    gen = SpeechEngine(be, get_profile("original")).iter_audio("One. Two. Three. " * 20, cancel=ev)
    next(gen); ev.set()
    with pytest.raises(SynthesisCancelled):
        list(gen)


def test_write_wav_file_streaming(tmp_path):
    out = tmp_path / "o.wav"
    info = SpeechEngine(FakeBackend(), get_profile("original")).synthesize_to_file("Hello there. Bye now.", out)
    with wave.open(str(out)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getnframes() / RATE == pytest.approx(info["duration_s"], abs=0.01)


def test_player_cancel_cleans_up(monkeypatch):
    played = []

    class FakeStream:
        samplerate = RATE
        def __init__(self, **kw): self.samplerate = kw["samplerate"]
        def start(self): pass
        def write(self, b): played.append(len(b)); time.sleep(0.01)
        def stop(self): pass
        def abort(self): played.append("abort")
        def close(self): played.append("closed")

    monkeypatch.setitem(__import__("sys").modules, "sounddevice", SimpleNamespace(RawOutputStream=FakeStream))
    ev = threading.Event()
    gen = SpeechEngine(FakeBackend(), get_profile("original")).iter_audio("Sentence. " * 100, cancel=ev)
    p = Player(gen, ev).start()
    time.sleep(0.15); ev.set(); p.join(3)
    assert p.done and played[-1] == "closed" and "abort" in played


def test_new_speak_interrupts_previous(monkeypatch):
    class FakeStream:
        def __init__(self, **kw): self.samplerate = kw["samplerate"]
        def start(self): pass
        def write(self, b): time.sleep(0.02)
        def stop(self): pass
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", SimpleNamespace(RawOutputStream=FakeStream))
    eng = SpeechEngine(FakeBackend(), get_profile("original"))
    h1 = eng.speak_async("Long text. " * 200)
    h2 = eng.speak_async("Short.")
    assert h1.cancel_event.is_set() and h1.done
    h2.wait(5); eng.close()


# ---- profiles -----------------------------------------------------------
def test_original_profile_has_no_system_voice_and_is_configurable(tmp_path):
    p = get_profile("original")
    assert p.local_blend and abs(sum(p.local_blend.values()) - 1.0) < 1e-9
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"original": {"speed": 1.1}, "mine": {"local_blend": {"af_heart": 1}}}))
    ps = load_profiles(f)
    assert ps["original"].speed == 1.1 and ps["original"].local_blend == p.local_blend
    assert "mine" in ps


def test_profiles_reject_bad_input(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"x": {"bogus": 1}}))
    with pytest.raises(ConfigurationError):
        load_profiles(f)
    with pytest.raises(InvalidInputError):
        get_profile("original").with_overrides(speed=9)
    with pytest.raises(InvalidInputError):
        get_profile("nope")


# ---- google backend (mocked) -------------------------------------------
class FakeInteractions:
    def __init__(self, results):
        self.results, self.requests = list(results), []

    def create(self, **kw):
        self.requests.append(kw)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(output_audio=SimpleNamespace(data=base64.b64encode(r).decode(), mime_type="audio/wav"))


def google(results):
    inter = FakeInteractions(results)
    return GoogleBackend(client=SimpleNamespace(interactions=inter), sleep=lambda d: False), inter


def api_error(code):
    e = RuntimeError(f"HTTP {code}"); e.code = code
    return e


def test_google_request_shape_and_decode():
    be, inter = google([wav_bytes(480)])
    audio = be.synthesize("Hi there", get_profile("original"), SynthParams(), threading.Event())
    assert audio.sample_rate == RATE and len(audio.pcm) == 960
    req = inter.requests[0]
    ann = req["input"][0]["content"][0]["annotations"][0]
    assert req["generation_config"]["speech_config"][0]["voice"] == "Sulafat"
    assert ann["type"] == "speech_metadata" and "warm" in ann["style"]


def test_google_retries_then_succeeds_and_rate_limit_surfaces():
    be, inter = google([api_error(503), wav_bytes()])
    assert be.synthesize("x y", get_profile("original"), SynthParams(), threading.Event()).pcm
    assert len(inter.requests) == 2
    be, _ = google([api_error(429)] * 3)
    with pytest.raises(RateLimitError):
        be.synthesize("x y", get_profile("original"), SynthParams(), threading.Event())


def test_google_auth_error_not_retried_and_key_not_leaked(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-KEY-123")
    e = api_error(403); e.args = ("bad key SECRET-KEY-123",)
    be, inter = google([e])
    with pytest.raises(ConfigurationError) as ei:
        be.synthesize("x y", get_profile("original"), SynthParams(), threading.Event())
    assert "SECRET-KEY-123" not in str(ei.value) and len(inter.requests) == 1


def test_google_status_reports_missing_key_without_value(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False); monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    st = GoogleBackend().status()
    assert not st.available and any("GEMINI_API_KEY" in p for p in st.problems)
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-KEY-123")
    assert "SECRET-KEY-123" not in json.dumps(GoogleBackend().status().__dict__)


# ---- CLI ----------------------------------------------------------------
def test_cli_voices_json(capsys):
    assert cli.main(["voices", "--json"]) == 0
    assert "original" in json.loads(capsys.readouterr().out)["profiles"]


def test_cli_speak_unavailable_backend_exit_code(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False); monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert cli.main(["speak", "--backend", "google", "--text", "hi", "-o", "x.wav"]) == 4
    assert "GEMINI_API_KEY" in capsys.readouterr().err


def test_cli_empty_text_and_missing_file(capsys):
    assert cli.main(["speak", "--text", "   ", "-o", "x.wav"]) in (2, 4)
    assert cli.main(["speak", "--input", "does-not-exist.txt"]) == 2


def test_cli_speak_to_file_with_fake_backend(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "make_backend", lambda name: FakeBackend())
    src = tmp_path / "in.txt"; src.write_text("Héllo wörld. Second sentence.", encoding="utf-8")
    out = tmp_path / "o.wav"
    assert cli.main(["speak", "--input", str(src), "-o", str(out), "--json", "-q"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["ok"] and out.stat().st_size > 44


def test_cli_stdin(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "make_backend", lambda name: FakeBackend())
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False, buffer=io.BytesIO("piped text.".encode())))
    assert cli.main(["speak", "-o", str(tmp_path / "s.wav"), "-q"]) == 0


def test_cli_invalid_utf8_file(tmp_path, capsys):
    f = tmp_path / "bad.txt"; f.write_bytes(b"\xff\xfe\xfa bad")
    assert cli.main(["speak", "--input", str(f)]) == 2


def test_local_backend_reports_missing_model(monkeypatch, tmp_path):
    from speech_engine.backends.local_backend import LocalBackend
    monkeypatch.delenv("SPEECH_ENGINE_KOKORO_MODEL", raising=False)
    st = LocalBackend(model_dir=tmp_path).status()
    assert not st.available and any("download-model" in p for p in st.problems)


# ---- boy profile / pitch -------------------------------------------------
def test_boy_profile_exists_and_uses_no_system_voice():
    p = get_profile("boy")
    assert p.pitch_semitones > 0 and p.google_voice == "Puck" and p.local_blend


def test_local_pitch_shift_shortens_audio_and_slows_synthesis():
    from speech_engine.backends.local_backend import LocalBackend
    seen = {}

    class FakeKokoro:
        def get_voices(self): return ["am_puck", "am_liam"]
        def get_voice_style(self, n): return np.ones((10, 1, 4), np.float32)
        def create(self, text, voice, speed, **kw):
            seen["speed"] = speed
            return np.full(24000, 0.1, np.float32), 24000

    be = LocalBackend(); be._kokoro = FakeKokoro()
    boy = get_profile("boy")
    a = be.synthesize("hi", boy, SynthParams(), threading.Event())
    ratio = 2 ** (boy.pitch_semitones / 12)
    assert seen["speed"] == pytest.approx(boy.speed / ratio)
    assert a.sample_rate == 24000 and len(a.pcm) // 2 == pytest.approx(24000 / ratio, abs=2)


def test_elongated_words_hold_their_phoneme():
    from speech_engine.backends.local_backend import LocalBackend

    class Tok:
        def phonemize(self, w, lang):
            return {"hello": "həlˈoʊ", "please": "plˈiːz", "yes": "jˈɛs"}.get(w.lower(), w)

    class K: tokenizer = Tok()

    be = LocalBackend()
    got = dict(be._elongation_overrides(K(), "Helloooooo, pleeeease say yessss. mmmm and hello.", "en-us"))
    assert got["Helloooooo"].startswith("həlˈooo") and got["Helloooooo"].endswith("ʊ")
    assert got["Helloooooo"].count("o") == 9                              # 6 o's written -> 5 extra letters x 1.5 = 8 extra tokens
    assert got["pleeeease"].count("i") == 6 and got["pleeeease"].endswith("ːz")
    assert got["yessss"].count("s") == 6
    assert "mmmm" not in got and "hello" not in got                     # nothing to hold / not elongated
    assert be._elongation_overrides(K(), "Helloooo", "fr") == ()


# ---- Gemini model fallback + usage tracking (fake client; limits are the placeholder config) -------------
def test_google_switches_model_on_429_and_counts_usage():
    be, inter = google([api_error(429), wav_bytes()])
    assert be.synthesize("x y", get_profile("original"), SynthParams(), threading.Event()).pcm
    assert [r["model"] for r in inter.requests][0] != [r["model"] for r in inter.requests][1]
    snap = be.usage.snapshot()
    assert snap["models"][0]["exhausted"] and snap["models"][1]["used"] == 1
    assert snap["total_limit"] == 250 and snap["window_started"]


def test_google_skips_model_at_local_limit_and_window_resets():
    from speech_engine.google_usage import UsageTracker
    now = [1000.0]
    be, inter = google([wav_bytes(), wav_bytes()])
    be.usage = UsageTracker(clock=lambda: now[0])
    for _ in range(100):
        be.usage.record("gemini-3.8-flash-tts")
    be.synthesize("x y", get_profile("original"), SynthParams(), threading.Event())
    assert inter.requests[0]["model"] == "gemini-2.5-flash-preview-tts"
    now[0] += 10 * 3600 + 1                         # 10 h later every counter is back to zero
    be.synthesize("x y", get_profile("original"), SynthParams(), threading.Event())
    assert inter.requests[1]["model"] == "gemini-3.8-flash-tts"
    assert be.usage.snapshot()["total_used"] == 1
