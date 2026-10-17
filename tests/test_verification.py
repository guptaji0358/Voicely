"""ASR-transcript comparison and acoustic checks.

MOCKED: the recogniser (MockASR returns scripted text - it does not listen) and the audio (sine tones from the
mock backend / synthetic arrays). These tests verify the COMPARISON LOGIC deterministically. They do not show that
any real ASR model detects real mispronunciations; see test_real_asr_optional.py for the real-model test.
"""

import wave

import numpy as np
import pytest

from speech_engine.asr import ASRProvider, ASRStatus, MockASR, Transcript, Word
from speech_engine.pron_dictionary import Application
from speech_engine.verification import Thresholds, analyze_audio, summarize, verify_chunk


def tone(path, seconds=1.0, amp=6000, rate=24000):
    t = np.arange(int(rate * seconds)) / rate
    x = (amp * np.sin(2 * np.pi * 220 * t)).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(x.tobytes())
    return path


def check(tmp_path, intended, recognized, th, probs=None, apps=(), name="c.wav", dictionary=None):
    wav = tone(tmp_path / name)
    asr = MockASR({name: (recognized, probs or {})})
    return verify_chunk(0, "g0", wav, intended, list(apps), asr, "en-us", th, dictionary)


def types(r):
    return sorted(i.type for i in r.issues if i.severity == "review")


def test_exact_match_is_verified_and_marked_mocked(tmp_path, loose):
    r = check(tmp_path, "Alpha beta gamma.", "alpha beta gamma", loose)
    assert r.status == "verified" and r.issues == []
    assert r.asr["mocked"] is True and r.confidence["mean_word_prob"] == pytest.approx(0.95)


@pytest.mark.parametrize("intended,recognized", [
    ("In 2024, we paid $5.50 (50% off).", "In twenty twenty four we paid five dollars and fifty cents fifty percent off"),
    ("It's the 1st of May, 3:30 pm", "it is the first of may three thirty pm"),
    ("Hello — “quoted”, world!", "hello quoted world"),
    ("Don't stop, can't stop.", "do not stop can not stop"),
])
def test_punctuation_and_number_format_are_not_errors(tmp_path, loose, intended, recognized):
    r = check(tmp_path, intended, recognized, loose)
    assert r.status == "verified", [i.reason for i in r.issues]


def test_wrong_number_is_caught(tmp_path, loose):
    r = check(tmp_path, "We paid 5 dollars.", "we paid six dollars", loose)
    assert r.status == "needs_review" and types(r) == ["substitution"]
    assert r.issues[0].sensitive is True


def test_name_mispronunciation_is_substitution_with_trace_back(tmp_path, loose):
    text = "Please call Nguyen now."
    r = check(tmp_path, text, "please call win now", loose)
    bad = [i for i in r.issues if i.severity == "review"]
    assert r.status == "needs_review" and len(bad) == 1
    assert text[bad[0].orig_start:bad[0].orig_end] == "Nguyen"            # traceable to the source text
    assert bad[0].sensitive and bad[0].type in ("substitution", "pronunciation_discrepancy")
    assert bad[0].recognized == "win"


def test_missing_and_failed(tmp_path, loose):
    r = check(tmp_path, "Alpha beta gamma delta.", "alpha gamma delta", loose)
    assert r.status == "needs_review" and types(r) == ["missing"]
    r = check(tmp_path, "Alpha beta gamma delta.", "alpha", loose, name="d.wav")
    assert r.status == "failed"                                            # most of the text is absent


def test_repetition_and_insertion(tmp_path, loose):
    r = check(tmp_path, "Alpha beta gamma.", "alpha beta beta gamma", loose)
    assert "repetition" in types(r)
    r = check(tmp_path, "Alpha beta gamma.", "alpha beta extra gamma", loose, name="e.wav")
    assert "insertion" in types(r)


def test_homophone_is_info_not_error(tmp_path, loose):
    r = check(tmp_path, "I will meet you there.", "i will meat you there", loose)
    # "meet"/"meat" share phonemes -> informational at most; never a review issue
    assert not [i for i in r.issues if i.severity == "review" and i.type == "substitution" and i.recognized == "meat"]


def test_low_confidence_is_reported_honestly(tmp_path, loose):
    r = check(tmp_path, "Please call Nguyen now.", "please call nguyen now", loose, probs={"nguyen": 0.4})
    assert r.status == "needs_review"
    assert [i.type for i in r.issues if i.severity == "review"] == ["low_confidence"]
    assert r.issues[0].confidence == pytest.approx(0.4)
    r = check(tmp_path, "Please call Nguyen now.", "please call nguyen now", loose, probs={"nguyen": 0.9}, name="f.wav")
    assert r.status == "verified"


def test_transcript_match_alone_is_not_verified_when_unconfirmable_or_no_confidence(tmp_path, loose):
    # heteronym: the spelling is identical however it is pronounced
    r = check(tmp_path, "I read books daily.", "i read books daily", loose)
    assert r.status == "needs_review" and "unconfirmable" in types(r)

    class NoConfidence(ASRProvider):
        name = "noconf"
        def status(self): return ASRStatus("noconf", True, "m")
        def transcribe(self, p, lang, cancel=None):
            return Transcript("alpha beta gamma", [Word("alpha", 0, .3), Word("beta", .3, .6), Word("gamma", .6, .9)],
                              "en", "noconf", "m", 1.0)
    r = verify_chunk(0, "g0", tone(tmp_path / "g.wav"), "Alpha beta gamma.", [], NoConfidence(), "en-us", loose)
    assert r.status == "needs_review" and r.confidence["available"] is False


def test_asr_failure_is_failed_not_verified(tmp_path, loose):
    wav = tone(tmp_path / "h.wav")
    r = verify_chunk(0, "g0", wav, "Alpha.", [], MockASR(fail=RuntimeError("model crashed")), "en-us", loose)
    assert r.status == "failed" and "model crashed" in r.error


def test_dictionary_applications_are_compared_against_what_was_spoken(tmp_path, loose):
    text = "Please call Nguyen now."
    app = Application(12, 18, "Nguyen", "e1", "win", "text")
    r = check(tmp_path, text, "please call win now", loose, apps=[app])
    assert r.spoken == "Please call win now." and r.status == "verified"


def test_acoustic_sanity(tmp_path):
    th = Thresholds()
    silent = tmp_path / "s.wav"
    with wave.open(str(silent), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\x00\x00" * 24000)
    assert "silent" in analyze_audio(silent, "Hello there", "en-us", th)["flags"]
    clipped = tone(tmp_path / "c.wav", amp=32767)
    assert "clipping" in analyze_audio(clipped, "Hello there", "en-us", th)["flags"]
    fast = tone(tmp_path / "f.wav", seconds=0.3)
    assert "speech_too_fast_possible_skipped_words" in analyze_audio(
        fast, "This is a long sentence that cannot be spoken in a third of a second.", "en-us", th)["flags"]
    ok = tone(tmp_path / "ok.wav", seconds=2.5)
    assert analyze_audio(ok, "This is a sentence of normal length.", "en-us", th)["flags"] == []


def test_summary_statuses_and_limitations(tmp_path, loose):
    a = check(tmp_path, "Alpha beta.", "alpha beta", loose, name="a.wav")
    b = check(tmp_path, "Please call Nguyen.", "please call win", loose, name="b.wav")
    assert summarize([])["status"] == "not_checked"
    assert summarize([a])["status"] == "verified"
    s = summarize([a, b])
    assert s["status"] == "needs_review" and s["issue_count"] == 1 and s["mocked"] is True
    assert any("not phonetic proof" in l for l in s["limitations"])
    assert "score" not in "".join(k for k in s if k not in ("limitations",)).replace("confidence_note", "")
