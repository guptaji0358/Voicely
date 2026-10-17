"""Post-synthesis speech verification ("Recheck Speech").

Evidence combined per chunk (each one reported, none presented as proof on its own):
  1. ASR transcript vs the intended text, after removing harmless differences (compare.py)
  2. word-level recogniser confidence
  3. phoneme-level comparison (espeak) of every disagreeing word: homophones are not errors, near-misses are
     reported as "suspected pronunciation discrepancy"
  4. acoustic sanity of the audio itself: silence, clipping, implausible speech rate (dropped / garbled words)
  5. categories a transcript CANNOT confirm (heteronyms: "read" is spelled the same either way) are never
     auto-verified

Statuses: not_checked | verified | needs_review | failed.  "verified" means: every automated check that exists
passed. It is NOT a calibrated quality score and NOT a guarantee of correct pronunciation; ``limitations`` in the
report says so, and ASR agreement alone never produces it.
"""

from __future__ import annotations

import difflib
import time
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import phonetics
from .asr import ASRProvider, Transcript
from .compare import Tok, canonical_tokens
from .exceptions import BackendUnavailableError, SynthesisCancelled
from .pron_dictionary import Application, PronDictionary
from .text_analysis import analyze_text

STATUSES = ("not_checked", "verified", "needs_review", "failed")
LIMITATIONS = [
    "ASR agreement is not phonetic proof: a recogniser can transcribe a mispronounced word with the expected spelling.",
    "Words that are spelled identically but pronounced differently (heteronyms, e.g. 'read') cannot be confirmed from a transcript.",
    "The status is a rule-based summary of the checks listed in 'evidence', not a calibrated speech-quality score.",
]
SENSITIVE_KINDS = {"name", "technical", "number", "date", "unit", "acronym", "rare", "multilingual", "dictionary"}
UNCONFIRMABLE_KINDS = {"heteronym"}


@dataclass
class Thresholds:
    min_word_prob: float = 0.55            # any recognised word below this -> low confidence issue
    sensitive_word_prob: float = 0.85      # names / numbers / technical terms need stronger evidence
    fail_missing_ratio: float = 0.5        # this share of intended words absent -> failed
    min_rate: float = 3.0                  # phonemes per second of active speech: below = suspiciously slow
    max_rate: float = 28.0                 # above = words probably skipped / garbled
    phoneme_near: float = 0.7              # similarity at/above which a mismatch is "pronunciation discrepancy"
    max_inner_silence_s: float = 2.5

    @classmethod
    def from_dict(cls, d: dict | None) -> "Thresholds":
        t = cls()
        for k, v in (d or {}).items():
            if hasattr(t, k) and isinstance(v, (int, float)) and not isinstance(v, bool):
                setattr(t, k, float(v))
        return t


@dataclass
class Issue:
    id: str
    type: str          # missing|insertion|substitution|pronunciation_discrepancy|repetition|low_confidence|unconfirmable|homophone|acoustic
    severity: str      # "review" | "info"
    chunk: int
    intended: str
    recognized: str
    orig_start: int | None      # span in the chunk's ORIGINAL text (traceable)
    orig_end: int | None
    sensitive: bool
    reason: str
    confidence: float | None = None      # recogniser probability of the involved words (min)
    time_start: float | None = None      # seconds inside the chunk audio, for replay
    time_end: float | None = None
    phoneme_similarity: float | None = None
    suggestions: list[str] = field(default_factory=list)


@dataclass
class ChunkResult:
    chunk: int
    version: str
    status: str
    intended: str
    spoken: str
    recognized: str
    issues: list[Issue]
    confidence: dict
    acoustic: dict
    evidence: list[str]
    asr: dict
    elapsed_s: float
    error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ------------------------------------------------------------------------------------ helpers
def _spoken_mapping(text: str, apps: list[Application]):
    """Spoken text for a chunk plus a function mapping spoken offsets back to offsets in the original text."""
    spans, out, pos, shift = [], [], 0, 0
    for a in sorted((a for a in apps if a.mode == "text"), key=lambda a: a.start):
        out.append(text[pos:a.start])
        s0 = a.start + shift
        out.append(a.spoken)
        spans.append((s0, s0 + len(a.spoken), a.start, a.end))
        shift += len(a.spoken) - (a.end - a.start)
        pos = a.end
    out.append(text[pos:])
    spoken = "".join(out)

    def to_orig(s: int, e: int) -> tuple[int, int]:
        for ss, se, os_, oe in spans:
            if s < se and e > ss:                 # overlaps a replaced span -> the whole original span
                return os_, oe
        def conv(p):
            d = 0
            for ss, se, os_, oe in spans:
                if p >= se:
                    d += (se - ss) - (oe - os_)
            return p - d
        return conv(s), conv(e)
    return spoken, to_orig


def _transcript_tokens(tr: Transcript) -> tuple[list[Tok], list[dict]]:
    """Canonical tokens of the transcript plus, per token, the recogniser words it came from."""
    text, ranges = "", []
    for i, w in enumerate(tr.words):
        s = len(text)
        text += w.text + " "
        ranges.append((s, s + len(w.text), i))
    if not tr.words:
        text = tr.text
    toks = canonical_tokens(text)
    meta = []
    for t in toks:
        ws = [tr.words[i] for (s, e, i) in ranges if s < t.end and e > t.start] if tr.words else []
        probs = [w.prob for w in ws if w.prob is not None]
        meta.append({"prob": min(probs) if probs else None,
                     "start": min((w.start for w in ws), default=None), "end": max((w.end for w in ws), default=None)})
    return toks, meta


def _repeated(norms: list[str]) -> list[tuple[int, int]]:
    """(start, length) of immediately repeated n-grams (n 1..4) in a token list."""
    out, i = [], 0
    while i < len(norms):
        hit = False
        for n in (4, 3, 2, 1):
            if i + 2 * n <= len(norms) and norms[i:i + n] == norms[i + n:i + 2 * n]:
                if n == 1 and len(norms[i]) <= 2:       # "so so", "no no" can be natural; ignore very short words
                    continue
                out.append((i + n, n))
                i += 2 * n
                hit = True
                break
        if not hit:
            i += 1
    return out


def analyze_audio(path: str | Path, expected_text: str, language: str, th: Thresholds) -> dict:
    """Signal-level sanity checks on one chunk file (16-bit mono WAV)."""
    with wave.open(str(path), "rb") as w:
        rate, n = w.getframerate(), w.getnframes()
        pcm = w.readframes(n)
    x = np.frombuffer(pcm, "<i2").astype(np.float32)
    dur = len(x) / rate if rate else 0.0
    flags: list[str] = []
    if len(x) == 0:
        return {"duration_s": 0.0, "flags": ["empty"], "rate_phones_per_s": None, "active_s": 0.0}
    peak = float(np.max(np.abs(x)))
    win = max(1, int(rate * 0.02))
    frames = len(x) // win
    env = np.sqrt(np.mean(x[:frames * win].reshape(frames, win) ** 2, axis=1)) if frames else np.array([0.0])
    thr = max(60.0, peak * 0.02)
    active = env > thr
    active_s = float(active.sum() * win / rate)
    if peak < 200 or active_s < 0.1:
        flags.append("silent")
    clip = float(np.mean(np.abs(x) >= 32760))
    if clip > 0.001:
        flags.append("clipping")
    if active.any():
        idx = np.flatnonzero(active)
        inner = active[idx[0]:idx[-1] + 1]
        run = best = 0
        for a in inner:
            run = 0 if a else run + 1
            best = max(best, run)
        if best * win / rate > th.max_inner_silence_s:
            flags.append("long_silence_inside")
    rate_pps = None
    pc = phonetics.phone_count(expected_text, "en-us" if language.startswith("en") else language)
    if pc and active_s > 0.2:
        rate_pps = round(pc / active_s, 2)
        if rate_pps > th.max_rate:
            flags.append("speech_too_fast_possible_skipped_words")
        elif rate_pps < th.min_rate:
            flags.append("speech_too_slow_possible_artifacts")
    return {"duration_s": round(dur, 3), "active_s": round(active_s, 3), "peak": int(peak),
            "clip_fraction": round(clip, 5), "rate_phones_per_s": rate_pps, "flags": flags}


# ------------------------------------------------------------------------------------ core
def verify_chunk(index: int, version: str, wav_path: Path, chunk_text: str, apps: list[Application],
                 asr: ASRProvider, language: str, th: Thresholds, dictionary: PronDictionary | None = None,
                 cancel=None) -> ChunkResult:
    t0 = time.monotonic()
    spoken, to_orig = _spoken_mapping(chunk_text, apps)
    evidence: list[str] = []
    try:
        tr = asr.transcribe(wav_path, language, cancel)
    except SynthesisCancelled:
        raise
    except Exception as exc:
        return ChunkResult(index, version, "failed", chunk_text, spoken, "", [], {}, {}, [],
                           {"provider": asr.name}, time.monotonic() - t0, error=f"{type(exc).__name__}: {exc}")
    acoustic = analyze_audio(wav_path, spoken, language, th)
    evidence.append(f"ASR transcript compared ({tr.provider}/{tr.model})" + (" [MOCKED]" if tr.mocked else ""))
    evidence.append("acoustic sanity: " + ("passed" if not acoustic["flags"] else ", ".join(acoustic["flags"])))

    intended = canonical_tokens(spoken)
    rec, meta = _transcript_tokens(tr)
    rnorm = [t.norm for t in rec]
    # sensitivity per intended token (analysis runs on the ORIGINAL chunk text; offsets map back through to_orig)
    findings = analyze_text(chunk_text, language, dictionary, ipa_ok=True).findings
    def kind_at(s: int, e: int) -> set[str]:
        os_, oe = to_orig(s, e)
        return {f.kind for f in findings if f.start < oe and f.end > os_}

    issues: list[Issue] = []
    n_issue = 0

    def add(type_, sev, i1, i2, j1, j2, reason, sens=False, sim=None, sugg=None):
        nonlocal n_issue
        n_issue += 1
        if i2 > i1:
            os_, oe = to_orig(intended[i1].start, intended[i2 - 1].end)
            itext = chunk_text[os_:oe]
        else:
            os_ = oe = None
            itext = ""
        rtext = " ".join(t.norm.lstrip("#") for t in rec[j1:j2]) if j2 > j1 else ""
        ms = [m for m in meta[j1:j2]]
        probs = [m["prob"] for m in ms if m["prob"] is not None]
        ts = [m["start"] for m in ms if m["start"] is not None]
        te = [m["end"] for m in ms if m["end"] is not None]
        issues.append(Issue(f"c{index}-i{n_issue}", type_, sev, index, itext, rtext, os_, oe, sens, reason,
                            min(probs) if probs else None, min(ts) if ts else None, max(te) if te else None,
                            sim, sugg or []))

    # repetition: recognised stream repeats words the intended text does not
    inorm = [t.norm for t in intended]
    drop: set[int] = set()
    for start, n in _repeated(rnorm):
        if not any(inorm[k:k + 2 * n] == rnorm[start - n:start + n] for k in range(len(inorm))):
            add("repetition", "review", 0, 0, start, start + n, "the recognised speech repeats these words; the text does not", sens=False)
            drop.update(range(start, start + n))
    keep = [k for k in range(len(rec)) if k not in drop]
    rec_k = [rec[k] for k in keep]
    meta_k = [meta[k] for k in keep]
    rec, meta = rec_k, meta_k
    rnorm = [t.norm for t in rec]

    sm = difflib.SequenceMatcher(None, inorm, rnorm, autojunk=False)
    missing_tokens = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        kinds = set().union(*[kind_at(intended[i].start, intended[i].end) for i in range(i1, i2)]) if i2 > i1 else set()
        sens = bool(kinds & SENSITIVE_KINDS) or any(inorm[i].startswith("#") for i in range(i1, i2))
        if tag == "equal":
            for off in range(i2 - i1):
                p = meta[j1 + off]["prob"]
                k2 = kind_at(intended[i1 + off].start, intended[i1 + off].end)
                is_sens = bool(k2 & SENSITIVE_KINDS) or inorm[i1 + off].startswith("#")
                if k2 & UNCONFIRMABLE_KINDS:
                    add("unconfirmable", "review", i1 + off, i1 + off + 1, j1 + off, j1 + off + 1,
                        "spelled the same however it is pronounced, so a transcript cannot confirm it; listen to this word",
                        sens=True)
                elif p is not None and p < (th.sensitive_word_prob if is_sens else th.min_word_prob):
                    add("low_confidence", "review", i1 + off, i1 + off + 1, j1 + off, j1 + off + 1,
                        f"recogniser confidence {p:.2f} is below {th.sensitive_word_prob if is_sens else th.min_word_prob:.2f}"
                        + (" for a name/number/technical term" if is_sens else ""), sens=is_sens)
        elif tag == "delete":
            missing_tokens += i2 - i1
            add("missing", "review", i1, i2, j1, j1, "intended words were not recognised in the speech", sens=sens)
        elif tag == "insert":
            add("insertion", "review", i1, i1, j1, j2, "recognised words that are not in the text", sens=False)
        else:                                                  # replace
            lang_p = "en-us" if language.startswith("en") else language
            ia = "".join(inorm[i1:i2]).replace("#", "")
            ra = "".join(rnorm[j1:j2]).replace("#", "")
            pa, pb = phonetics.phonemize_word(ia, lang_p), phonetics.phonemize_word(ra, lang_p)
            sim = phonetics.similarity(pa, pb) if pa and pb else (phonetics.similarity(ia, ra) if not (pa and pb) else None)
            how = "phonemes" if pa and pb else "spelling"
            if pa and pb and pa == pb:
                add("homophone", "info", i1, i2, j1, j2, "different spelling, identical phonemes: not a pronunciation error", sim=1.0)
            elif sim is not None and sim >= th.phoneme_near:
                add("pronunciation_discrepancy", "review", i1, i2, j1, j2,
                    f"heard as a different word with similar sounds ({how} similarity {sim:.2f}): suspected mispronunciation, or a recogniser error",
                    sens=sens, sim=sim, sugg=["add a respelling / IPA to the dictionary"])
            else:
                missing_tokens += max(0, (i2 - i1) - (j2 - j1))
                add("substitution", "review", i1, i2, j1, j2,
                    "the speech was recognised as different words" + (f" ({how} similarity {sim:.2f})" if sim is not None else ""),
                    sens=sens, sim=sim, sugg=["listen to the segment", "add a respelling / IPA to the dictionary"] if sens else ["listen to the segment"])

    for fl in acoustic["flags"]:
        add_i = Issue(f"c{index}-a{fl}", "acoustic", "review", index, "", "", None, None, False,
                      {"silent": "audio is silent or almost silent", "clipping": "audio is clipped (distorted)",
                       "long_silence_inside": "unexpected long silence inside the segment",
                       "speech_too_fast_possible_skipped_words": "speech rate is implausibly fast for this text (words may be skipped or garbled)",
                       "speech_too_slow_possible_artifacts": "speech rate is implausibly slow for this text (stretched or artefacts)",
                       "empty": "audio is empty"}.get(fl, fl))
        issues.append(add_i)

    probs = [m["prob"] for m in meta if m["prob"] is not None]
    conf = {"mean_word_prob": round(float(np.mean(probs)), 3) if probs else None,
            "min_word_prob": round(float(min(probs)), 3) if probs else None, "words": len(rec),
            "available": bool(probs)}
    if not probs:
        evidence.append("recogniser gave no word confidence (confidence unknown)")
    n_int = max(1, len(intended))
    review = [i for i in issues if i.severity == "review"]
    if "silent" in acoustic["flags"] or "empty" in acoustic["flags"] or missing_tokens / n_int >= th.fail_missing_ratio:
        status = "failed"
    elif review:
        status = "needs_review"
    elif not probs and not tr.mocked:
        status = "needs_review"           # no confidence information at all: cannot call it verified
        issues.append(Issue(f"c{index}-noconf", "low_confidence", "review", index, "", "", None, None, False,
                            "the recogniser provided no confidence values, so the match cannot be called verified"))
    else:
        status = "verified"
    return ChunkResult(index, version, status, chunk_text, spoken, tr.text, issues, conf, acoustic, evidence,
                       {"provider": tr.provider, "model": tr.model, "mocked": tr.mocked, "language": tr.language},
                       round(time.monotonic() - t0, 3))


def summarize(results: list[ChunkResult], meta: dict | None = None) -> dict:
    """Quality report over chunk results. Counts and statuses only: no invented 0-100 score."""
    if not results:
        status = "not_checked"
    elif any(r.status == "failed" for r in results):
        status = "failed"
    elif any(r.status == "needs_review" for r in results):
        status = "needs_review"
    else:
        status = "verified"
    issues = [i for r in results for i in r.issues]
    review = [i for i in issues if i.severity == "review"]
    probs = [r.confidence.get("mean_word_prob") for r in results if r.confidence.get("mean_word_prob") is not None]
    return {
        "status": status,
        "chunks_checked": len(results),
        "chunk_status": {s: sum(1 for r in results if r.status == s) for s in STATUSES if s != "not_checked"},
        "issue_count": len(review),
        "info_count": len(issues) - len(review),
        "issues": [asdict(i) for i in sorted(review + [i for i in issues if i.severity != "review"],
                                             key=lambda i: (i.chunk, i.orig_start if i.orig_start is not None else -1))],
        "chunks": [{"chunk": r.chunk, "version": r.version, "status": r.status, "intended": r.intended, "spoken": r.spoken,
                    "recognized": r.recognized, "mean_word_prob": r.confidence.get("mean_word_prob"),
                    "min_word_prob": r.confidence.get("min_word_prob"), "error": r.error,
                    "acoustic_flags": r.acoustic.get("flags", [])} for r in results],
        "mean_word_confidence": round(float(np.mean(probs)), 3) if probs else None,
        "confidence_note": "recogniser word probabilities, not calibrated pronunciation accuracy",
        "elapsed_s": round(sum(r.elapsed_s for r in results), 3),
        "asr": results[0].asr if results else {},
        "mocked": any(r.asr.get("mocked") for r in results),
        "limitations": LIMITATIONS,
        "evidence": sorted({e.split(" (")[0] if e.startswith("ASR") else e.split(":")[0] for r in results for e in r.evidence}),
        **(meta or {}),
    }
