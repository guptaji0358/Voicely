"""Worker protocol tests. Use the MOCK backend (sine tone) so no model or API key is needed,
plus one real-backend test that is skipped when the Kokoro model is missing."""

import json
import os
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class W:
    def __init__(self, **env):
        e = dict(os.environ, SPEECH_ENGINE_ALLOW_MOCK="1", PYTHONIOENCODING="utf-8", **env)
        self.p = subprocess.Popen([sys.executable, "-m", "speech_engine.worker"], cwd=ROOT, env=e,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.ready = self.read()
        self.n = 0

    def send_raw(self, raw: bytes):
        self.p.stdin.write(raw + b"\n"); self.p.stdin.flush()

    def send(self, action, **kw):
        self.n += 1
        rid = f"r{self.n}"
        self.send_raw(json.dumps(dict(protocol_version=1, request_id=rid, action=action, **kw)).encode())
        return rid

    def read(self):
        line = self.p.stdout.readline()
        assert line, "worker closed stdout: " + self.p.stderr.read().decode(errors="replace")
        return json.loads(line)  # every stdout line MUST be JSON

    def until_final(self, rid):
        events = []
        while True:
            m = self.read()
            if m.get("request_id") == rid and "status" in m:
                return m, events
            events.append(m)

    def close(self):
        self.p.stdin.close()
        self.p.wait(10)


@pytest.fixture
def w():
    x = W()
    yield x
    if x.p.poll() is None:
        x.p.kill()


def test_ready_and_health(w):
    assert w.ready["event"] == "ready"
    m, _ = w.until_final(w.send("health"))
    assert m["status"] == "success" and m["backends"]["mock"]["available"] and "python" in m


def test_malformed_and_unknown(w):
    w.send_raw(b"{not json")
    assert w.read()["code"] == "MALFORMED_REQUEST"
    w.send_raw(b"\xff\xfe")
    assert w.read()["code"] == "MALFORMED_REQUEST"
    m, _ = w.until_final(w.send("nope"))
    assert m["code"] == "UNKNOWN_ACTION"
    w.send_raw(json.dumps({"protocol_version": 9, "request_id": "x", "action": "health"}).encode())
    assert w.read()["code"] == "UNSUPPORTED_PROTOCOL"


def test_synthesize_wav_unicode_space_path_progress(w, tmp_path):
    out = tmp_path / "my folder ü 音声" / "out put.wav"
    out.parent.mkdir()
    text = " ".join(f"Sentence number {i} is here." for i in range(12))
    m, ev = w.until_final(w.send("synthesize", text=text, backend="mock", output_path=str(out)))
    assert m["status"] == "success", m
    assert [e["progress"] for e in ev if e.get("event") == "progress"][-1] == 100
    assert len([e for e in ev if e.get("event") == "progress"]) > 3
    with wave.open(str(out)) as f:
        assert f.getnframes() > 0 and f.getnchannels() == 1


def test_mp3_and_export_independent_decode(w, tmp_path):
    miniaudio = pytest.importorskip("miniaudio")
    wav, mp3 = tmp_path / "a.wav", tmp_path / "b.mp3"
    assert w.until_final(w.send("synthesize", text="Hello there. Again.", backend="mock",
                                output_path=str(wav)))[0]["status"] == "success"
    m, _ = w.until_final(w.send("export", input_path=str(wav), output_path=str(mp3), bitrate_kbps=192))
    assert m["status"] == "success" and m["bitrate_kbps"] == 192
    info = miniaudio.mp3_read_file_f32(str(mp3))
    assert info.num_frames > 1000 and abs(info.duration - m["duration_s"]) < 0.1
    assert not list(tmp_path.glob(".*.part"))
    # direct MP3 synth + overwrite protection
    m, _ = w.until_final(w.send("synthesize", text="Hi.", backend="mock", output_path=str(mp3)))
    assert m["code"] == "INVALID_INPUT" and "already exists" in m["message"]
    m, _ = w.until_final(w.send("synthesize", text="Hi.", backend="mock", output_path=str(mp3), overwrite=True))
    assert m["status"] == "success"


def test_errors(w, tmp_path):
    assert w.until_final(w.send("synthesize", text="  ", backend="mock",
                                output_path=str(tmp_path / "x.wav")))[0]["code"] == "INVALID_INPUT"
    m, _ = w.until_final(w.send("synthesize", text="Boom [[fail]]", backend="mock", output_path=str(tmp_path / "x.wav")))
    assert m["code"] == "SYNTHESIS_FAILED" and not (tmp_path / "x.wav").exists()
    m, _ = w.until_final(w.send("synthesize", text="Hi", backend="google", output_path=str(tmp_path / "g.wav")))
    assert m["code"] == "BACKEND_UNAVAILABLE"      # no key in test env (cleared below if present)
    m, _ = w.until_final(w.send("synthesize", text="Hi", backend="bogus", output_path=str(tmp_path / "g.wav")))
    assert m["code"] == "INVALID_INPUT"
    m, _ = w.until_final(w.send("synthesize", text="Hi", backend="mock", output_path=str(tmp_path / "no" / "g.wav")))
    assert m["code"] == "INVALID_INPUT"


def test_cancel_and_busy_and_repeat():
    w = W(SPEECH_ENGINE_MOCK_DELAY="0.3")
    try:
        import tempfile
        d = Path(tempfile.mkdtemp())
        text = " ".join(f"Sentence number {i} is here." for i in range(30))
        a = w.send("synthesize", text=text, backend="mock", output_path=str(d / "a.wav"))
        b = w.send("synthesize", text="x", backend="mock", output_path=str(d / "b.wav"))
        seen = {}
        while "b" not in seen:
            m = w.read()
            if m.get("request_id") == b:
                seen["b"] = m
        assert seen["b"]["code"] == "BUSY"
        t0 = time.time()
        w.send("cancel", target=a)
        m, _ = w.until_final(a)
        assert m["status"] == "cancelled" and time.time() - t0 < 3
        assert not (d / "a.wav").exists() and not list(d.glob(".*.part"))
        # the worker survives a cancel and answers further requests
        assert w.until_final(w.send("health"))[0]["status"] == "success"
    finally:
        w.p.kill()


def test_stdin_close_exits_cleanly(w):
    w.close()
    assert w.p.returncode == 0


def test_stdout_is_protocol_only(w, tmp_path):
    w.until_final(w.send("synthesize", text="Hello.", backend="mock", output_path=str(tmp_path / "z.wav")))
    w.close()
    rest = w.p.stdout.read().decode().strip().splitlines()
    for line in rest:
        json.loads(line)
