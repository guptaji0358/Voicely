"""Sessions, targeted regeneration, original-audio preservation and the repair workflow.
MOCKED: TTS (sine tones) and ASR (scripted text). The control flow, file handling and honesty rules are real."""

import hashlib
import threading
import wave

import pytest

from speech_engine.asr import MockASR
from speech_engine.backends.base import SynthParams
from speech_engine.backends.mock_backend import MockBackend
from speech_engine.pron_dictionary import PronDictionary
from speech_engine.qa import QA
from speech_engine.session import Session, locate_chunks
from speech_engine.voice_profiles import ORIGINAL

TEXT = "Alpha beta gamma.\n\nPlease call Nguyen now.\n\nDelta epsilon zeta."
GOOD = {"0000.g0.wav": "alpha beta gamma", "0001.g0.wav": "please call win now", "0002.g0.wav": "delta epsilon zeta"}


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make(tmp_path, dictionary=None):
    return Session.create(tmp_path / "sess", TEXT, MockBackend(), ORIGINAL, SynthParams(), "en-us", dictionary,
                          threading.Event())


def wav_info(p):
    with wave.open(str(p)) as w:
        return w.getnframes(), w.getframerate()


def test_session_layout_and_assembly(tmp_path):
    s = make(tmp_path)
    assert [c["text"] for c in s.chunks] == ["Alpha beta gamma.", "Please call Nguyen now.", "Delta epsilon zeta."]
    for c in s.chunks:
        a, b = c["source_span"]
        assert TEXT[a:b] == c["text"] and c["source_span_exact"]
    orig = s.assemble("original")
    frames, rate = wav_info(orig)
    expect = sum(wav_info(s.path_of(c["index"], "g0"))[0] + int(rate * c["pause_ms"] / 1000) for c in s.chunks)
    assert frames == expect
    assert Session.open(tmp_path / "sess").m["session_id"] == s.m["session_id"]


def test_locate_chunks_tolerates_expanded_abbreviations():
    spans = locate_chunks("Dr. Smith arrived.\n\nThen left.", ["Doctor Smith arrived.", "Then left."])
    assert spans[0] is not None and spans[0][2] is False and spans[1][2] is True


def test_targeted_regeneration_leaves_everything_else_and_original_untouched(tmp_path, loose):
    s = make(tmp_path)
    original = s.assemble("original")
    h_orig = sha(original)
    h_chunks = [sha(s.path_of(i, "g0")) for i in range(3)]
    d = PronDictionary(memory=True)
    v = s.regenerate_chunk(1, MockBackend(), ORIGINAL, SynthParams(), d, None, None, "test", threading.Event())
    assert v["id"] == "g1" and [len(c["versions"]) for c in s.chunks] == [1, 2, 1]   # only chunk 1 regenerated
    assert [sha(s.path_of(i, "g0")) for i in range(3)] == h_chunks
    s.set_candidate({1: "g1"})
    cand = s.assemble("candidate")
    assert cand != original and sha(original) == h_orig                    # original preserved byte-for-byte
    assert s.selected("accepted")[1] == "g0" and s.selected("candidate")[1] == "g1"
    acc = s.accept_candidate()
    assert s.selected("accepted")[1] == "g1" and acc.name == "accepted.wav"
    assert sha(original) == h_orig and sha(s.path_of(1, "g0")) == h_chunks[1]


def run_repair(tmp_path, loose, script, candidates, mode="review", dictionary_entries=()):
    qa = QA()
    for e in dictionary_entries:
        qa.dictionary.add(**e)
    asr = MockASR(script)
    qa.make_asr = lambda name=None: asr
    s = make(tmp_path, qa.dictionary)
    s.assemble("original")
    summary = qa.recheck(s, "accepted", None, asr, loose, threading.Event(), lambda p, m: None)
    return qa, s, asr, summary


def test_recheck_report_and_repair_success_is_only_claimed_after_recheck(tmp_path, loose):
    script = dict(GOOD, **{"0001.g1.wav": "please call nwin now"})
    qa, s, asr, summary = run_repair(tmp_path, loose, script, [])
    assert summary["status"] == "needs_review" and summary["issue_count"] == 1
    assert summary["chunk_status"] == {"verified": 2, "needs_review": 1, "failed": 0}
    asr.calls.clear()
    res = qa.regenerate(s, MockBackend(), ORIGINAL, None, "review",
                        [{"chunk": 1, "written": "Nguyen", "hint": "nwin"}], 3, False, False, asr, loose,
                        threading.Event(), lambda p, m: None)
    out = res["outcomes"][0]
    assert out["status"] == "repaired" and out["attempts"][0]["term_resolved"] and out["attempts"][0]["recheck_status"] == "verified"
    assert asr.calls == ["0001.g1.wav"]                                    # only the affected chunk was regenerated + rechecked
    assert res["original_preserved"] and res["repaired_chunks"] == [1]
    assert s.m["selection"]["accepted"]["1"] == "g0" and s.m["selection"]["candidate"] == {"1": "g1"}
    assert out["suggested_entry"]["hint"] == "nwin"                        # offered to the user, NOT stored automatically
    assert qa.dictionary.entries == {}


def test_repair_that_does_not_work_is_reported_not_claimed(tmp_path, loose):
    # the voice keeps saying "win": regenerated chunks are rechecked and still wrong
    script = dict(GOOD, **{"0001.g1.wav": "please call win now", "0001.g2.wav": "please call win now"})
    qa, s, asr, _ = run_repair(tmp_path, loose, script, [])
    res = qa.regenerate(s, MockBackend(), ORIGINAL, None, "review",
                        [{"chunk": 1, "written": "Nguyen", "hint": "nwin"}, {"chunk": 1, "written": "Nguyen", "hint": "noowin"}],
                        4, False, False, asr, loose, threading.Event(), lambda p, m: None)
    out = res["outcomes"][0]
    assert out["status"] == "unresolved" and not any(a["term_resolved"] for a in out["attempts"])
    assert len(out["attempts"]) == 1                                       # stopped: identical result = backend ignores the hint
    assert any("same result" in l for l in out["limitations"])
    assert res["candidate_path"] is None and s.m["selection"]["candidate"] is None


def test_auto_mode_without_any_approved_source_reports_limitation(tmp_path, loose):
    qa, s, asr, _ = run_repair(tmp_path, loose, GOOD, [])
    res = qa.regenerate(s, MockBackend(), ORIGINAL, None, "auto", [], 3, False, False, asr, loose,
                        threading.Event(), lambda p, m: None)
    out = res["outcomes"][0]
    assert out["status"] == "unresolved" and out["attempts"] == []
    assert any("No supported correction" in l for l in out["limitations"])


def test_auto_mode_uses_existing_dictionary_candidate_and_ipa_is_refused_without_support(tmp_path, loose):
    script = dict(GOOD, **{"0001.g1.wav": "please call nwin now"})
    qa, s, asr, _ = run_repair(tmp_path, loose, script, [])
    qa.dictionary.add(written="Nguyen", hint="nwin")                       # added AFTER the audio was generated
    res = qa.regenerate(s, MockBackend(), ORIGINAL, None, "auto", [], 3, False, False, asr, loose,
                        threading.Event(), lambda p, m: None)
    assert res["outcomes"][0]["status"] == "repaired"
    assert res["outcomes"][0]["attempts"][0]["candidate"]["source"] == "dictionary"
    # IPA-only candidate on a backend without IPA support: refused, nothing regenerated
    for eid in list(qa.dictionary.entries):
        qa.dictionary.delete(eid)
    qa2, s2, asr2, _ = run_repair(tmp_path / "b", loose, GOOD, [])
    r2 = qa2.regenerate(s2, MockBackend(), ORIGINAL, None, "review", [{"chunk": 1, "written": "Nguyen", "ipa": "wɪn"}], 3,
                        False, False, asr2, loose, threading.Event(), lambda p, m: None)
    assert r2["outcomes"][0]["attempts"] == []


def test_cancelled_recheck_raises(tmp_path, loose):
    from speech_engine.exceptions import SynthesisCancelled
    qa, s, asr, _ = run_repair(tmp_path, loose, GOOD, [])
    ev = threading.Event(); ev.set()
    with pytest.raises(SynthesisCancelled):
        qa.recheck(s, "accepted", None, asr, loose, ev, lambda p, m: None)


def test_unavailable_asr_gives_a_clear_error(tmp_path, loose):
    from speech_engine.asr import FasterWhisperASR
    from speech_engine.exceptions import BackendUnavailableError
    qa = QA()
    s = make(tmp_path)
    asr = FasterWhisperASR(model="base.en", model_dir=tmp_path / "nomodels")
    if asr.status().available:
        pytest.skip("a real ASR model is installed")
    with pytest.raises(BackendUnavailableError, match="unavailable"):
        qa.recheck(s, "accepted", None, asr, loose, threading.Event(), lambda p, m: None)
