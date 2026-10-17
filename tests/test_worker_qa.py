"""Protocol-2 tests against the REAL worker process. MOCKED parts: the TTS backend (sine tones), the ASR
(SPEECH_ENGINE_ASR=mock reads scripted transcripts from a JSON file) and the LLM (scripted replies)."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEXT = "Alpha beta gamma.\n\nPlease call Nguyen now.\n\nDelta epsilon zeta."
SCRIPT = {"0000.g0.wav": "alpha beta gamma", "0001.g0.wav": "please call win now", "0002.g0.wav": "delta epsilon zeta",
          "0001.g1.wav": "please call nwin now"}
LOOSE = {"min_rate": 0, "max_rate": 1e9}


class W:
    def __init__(self, tmp, **env):
        (tmp / "asr.json").write_text(json.dumps(SCRIPT), encoding="utf-8")
        (tmp / "llm.json").write_text(json.dumps([json.dumps(
            {"suggestions": [{"word": "Nguyen", "kind": "name", "reason": "surname", "respelling": "nwin", "confidence": .8,
                              "priority": 1}]})]), encoding="utf-8")
        e = dict(os.environ, SPEECH_ENGINE_ALLOW_MOCK="1", PYTHONIOENCODING="utf-8", SPEECH_ENGINE_ASR="mock",
                 SPEECH_ENGINE_MOCK_ASR_FILE=str(tmp / "asr.json"), SPEECH_ENGINE_MOCK_LLM_FILE=str(tmp / "llm.json"),
                 SPEECH_ENGINE_DATA_DIR=str(tmp / "data"), **env)
        self.p = subprocess.Popen([sys.executable, "-m", "speech_engine.worker"], cwd=ROOT, env=e, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.ready = self.read()
        self.n = 0

    def send(self, action, version=2, **kw):
        self.n += 1
        rid = f"r{self.n}"
        self.p.stdin.write(json.dumps(dict(protocol_version=version, request_id=rid, action=action, **kw)).encode() + b"\n")
        self.p.stdin.flush()
        return rid

    def read(self):
        line = self.p.stdout.readline()
        assert line, "worker closed stdout: " + self.p.stderr.read().decode(errors="replace")
        return json.loads(line)

    def call(self, action, version=2, **kw):
        rid = self.send(action, version, **kw)
        events = []
        while True:
            m = self.read()
            if m.get("request_id") == rid and "status" in m:
                return m, events
            events.append(m)


@pytest.fixture
def w(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    x = W(tmp_path)
    yield x
    if x.p.poll() is None:
        x.p.kill()


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def synth(w, tmp_path, **kw):
    return w.call("synthesize", text=TEXT, backend="mock", voice="original", output_path=str(tmp_path / "out.wav"),
                  output_format="wav", overwrite=True, session_dir=str(tmp_path / "sess"), **kw)


def test_versions_health_and_v1_compatibility(w):
    assert w.ready["protocol_version"] == 2
    m, _ = w.call("health")
    assert m["protocol_version"] == 2 and m["asr"]["provider"] == "mock" and "dictionary" in m and m["llm"]["enabled"] is False
    m1, _ = w.call("health", version=1)
    assert m1["protocol_version"] == 1 and "asr" not in m1                  # a v1 client sees exactly the old shape
    bad, _ = w.call("recheck", version=1, session_dir="x")
    assert bad["code"] == "UNSUPPORTED_PROTOCOL"
    caps, _ = w.call("capabilities")
    assert caps["pronunciation"]["local"]["ipa"] is True and caps["pronunciation"]["google"]["ipa"] is False


def test_full_workflow_over_the_protocol(w, tmp_path):
    a, _ = w.call("analyze_text", text=TEXT, backend="mock", language="en-us")
    assert any(f["text"] == "Nguyen" and f["kind"] == "name" for f in a["findings"])

    m, events = synth(w, tmp_path)
    assert m["status"] == "success" and m["chunks"] == 3 and Path(m["output_path"]).is_file()
    assert [e["progress"] for e in events if e.get("event") == "progress"][-1] == 100
    sess = m["session"]
    original_hash = sha(sess["original_path"])
    assert sha(m["output_path"]) == original_hash

    r, ev = w.call("recheck", session_dir=sess["dir"], thresholds=LOOSE)
    assert r["status"] == "success" and r["summary"]["status"] == "needs_review" and r["summary"]["issue_count"] == 1
    assert r["summary"]["mocked"] is True and r["summary"]["asr"]["provider"] == "mock"
    issue = r["summary"]["issues"][0]
    assert issue["intended"] == "Nguyen" and issue["chunk"] == 1

    seg, _ = w.call("segment", session_dir=sess["dir"], chunk=1)
    assert Path(seg["path"]).is_file() and seg["text"].startswith("Please call")

    g, _ = w.call("regenerate", session_dir=sess["dir"], mode="review", chunks=[1], thresholds=LOOSE,
                  candidates=[{"chunk": 1, "written": "Nguyen", "hint": "nwin"}])
    assert g["status"] == "success" and g["outcomes"][0]["status"] == "repaired" and g["original_preserved"]
    assert sha(sess["original_path"]) == original_hash                       # untouched
    assert Path(g["candidate_path"]).is_file()

    info, _ = w.call("session_info", session_dir=sess["dir"])
    assert info["chunks"][1]["candidate"] == "g1" and info["candidate_path"]
    acc, _ = w.call("accept_repair", session_dir=sess["dir"])
    assert Path(acc["accepted_path"]).is_file() and sha(sess["original_path"]) == original_hash
    r2, _ = w.call("recheck", session_dir=sess["dir"], thresholds=LOOSE)
    assert r2["summary"]["status"] == "verified"                             # rechecked after the repair


def test_dictionary_and_feedback_over_the_protocol(w, tmp_path):
    bad, _ = w.call("dictionary", op="add", entry={"written": "x"})
    assert bad["status"] == "error" and bad["code"] == "INVALID_INPUT"
    add, _ = w.call("dictionary", op="add", entry={"written": "Nguyen", "hint": "nwin", "category": "name"})
    assert add["added"] and add["version"] == 1
    lst, _ = w.call("dictionary", op="search", query="ngu")
    assert lst["count"] == 1 and lst["entries"][0]["hint"] == "nwin"
    m, _ = synth(w, tmp_path)
    assert m["session"]["dictionary_applications"] == 1 and m["session"]["dictionary_version"] == 1

    no, _ = w.call("feedback", op="record", record={"original_text": TEXT, "term": "Nguyen",
                                                    "correction": {"hint": "nwin"}, "succeeded": True})
    assert no["code"] == "CONSENT_REQUIRED"
    w.call("feedback", op="set_consent", store_feedback=True)
    ok, _ = w.call("feedback", op="record", record={"original_text": TEXT, "term": "Nguyen",
                                                    "correction": {"hint": "nwin"}, "succeeded": True})
    assert ok["status"] == "success"
    # now the dictionary change that would break the approved correction is refused by the regression gate
    upd, _ = w.call("dictionary", op="update", id=add["entry"]["id"], entry={"hint": "win"})
    assert upd["updated"] is False and upd["regression"]["ok"] is False
    exp, _ = w.call("feedback", op="export", dest_dir=str(tmp_path))
    assert exp["records"] == 1 and Path(exp["path"]).is_dir()
    assert w.call("feedback", op="delete_all")[0]["deleted"] == 1


def test_llm_over_the_protocol_disabled_then_enabled(w):
    m, _ = w.call("llm_analyze", text="Please call Nguyen.", backend="mock", allow_network=False)
    assert m["code"] == "LLM_DISABLED"
    st, _ = w.call("llm_configure", settings={"enabled": True, "provider": "mock", "min_interval_s": 0.0})
    assert st["enabled"] and st["provider"] == "mock"
    m, _ = w.call("llm_analyze", text="Please call Nguyen.", backend="mock")
    assert m["status"] == "success" and m["suggestions"][0]["respelling"] == "nwin" and "approve" in m["meta"]["note"]
    bad, _ = w.call("llm_configure", settings={"timeout_s": 0.01})
    assert bad["status"] == "error"


def test_validation_busy_cancel_and_malformed(w, tmp_path):
    assert w.call("recheck", session_dir=str(tmp_path / "missing"))[0]["code"] == "INVALID_INPUT"
    assert w.call("segment", session_dir="")[0]["code"] == "INVALID_INPUT"
    assert w.call("dictionary", op="nope")[0]["code"] == "INVALID_INPUT"
    assert w.call("asr_download", model="base.en")[0]["code"] == "INVALID_INPUT"        # needs confirm:true
    m, _ = synth(w, tmp_path)
    r = w.call("recheck", session_dir=m["session"]["dir"], chunks="abc")[0]
    assert r["code"] == "INVALID_INPUT"
    # a second job while one runs is rejected, and the running one can be cancelled
    w.p.stdin.write(b"")  # no-op
    slow = W(tmp_path, SPEECH_ENGINE_MOCK_DELAY="0.4")
    try:
        rid = slow.send("synthesize", text=TEXT, backend="mock", voice="original", output_path=str(tmp_path / "s.wav"),
                        output_format="wav", overwrite=True, session_dir=str(tmp_path / "sess2"))
        busy = slow.send("recheck", session_dir=str(tmp_path / "sess2"))
        seen = {}
        while busy not in seen:
            msg = slow.read()
            if msg.get("request_id") == busy and "status" in msg:
                seen[busy] = msg
        assert seen[busy]["code"] == "BUSY"
        slow.call("cancel", target=rid)
        while True:
            msg = slow.read()
            if msg.get("request_id") == rid and "status" in msg:
                assert msg["status"] == "cancelled"
                break
    finally:
        slow.p.kill()
