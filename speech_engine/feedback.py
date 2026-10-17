"""Explicit, consent-gated storage of pronunciation feedback, plus the regression set built from it.

What this is - and is not (see docs/LEARNING.md):
  * It stores corrections a person chose to save. Nothing is collected automatically and nothing is uploaded;
    the folder lives under the user's data directory, can be inspected, exported and deleted from the GUI.
  * "Learning" means: approved corrections become dictionary entries (persistent mechanism #1), become regression
    cases (#2) that gate future dictionary changes (#3), and may be offered as few-shot examples to the LLM prompt if
    the user enables it (#4, sends them to the provider). No foundation model is trained or fine-tuned here (#5 is a
    separate, documented workflow).
"""

from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .exceptions import InvalidInputError, SpeechEngineError
from .paths import data_dir, read_json, write_json_atomic
from .pron_dictionary import Entry, PronDictionary

SCHEMA = 1
MAX_TEXT = 4000


class ConsentRequired(SpeechEngineError):
    exit_code = 3


def _text(name: str, v, maxlen=MAX_TEXT, required=False) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str) or len(v) > maxlen:
        raise InvalidInputError(f"{name} must be text of at most {maxlen} characters")
    if required and not v.strip():
        raise InvalidInputError(f"{name} is required")
    return v


class FeedbackStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else data_dir() / "feedback"
        self.records_path = self.root / "records.json"
        self.consent_path = self.root / "consent.json"

    # ---- consent ---------------------------------------------------------------------
    def consent(self) -> dict:
        c = read_json(self.consent_path, {}) or {}
        return {"store_feedback": bool(c.get("store_feedback", False)), "store_audio": bool(c.get("store_audio", False)),
                "updated": c.get("updated")}

    def set_consent(self, store_feedback: bool | None = None, store_audio: bool | None = None) -> dict:
        c = self.consent()
        if store_feedback is not None:
            c["store_feedback"] = bool(store_feedback)
        if store_audio is not None:
            c["store_audio"] = bool(store_audio)
        if not c["store_feedback"]:
            c["store_audio"] = False                     # audio consent never outlives feedback consent
        c["updated"] = time.time()
        write_json_atomic(self.consent_path, c)
        return c

    # ---- records ------------------------------------------------------------------------
    def _all(self) -> list[dict]:
        doc = read_json(self.records_path, {"schema": SCHEMA, "records": []})
        return doc.get("records", [])

    def _save(self, recs: list[dict]) -> None:
        write_json_atomic(self.records_path, {"schema": SCHEMA, "records": recs})

    def record(self, rec: dict, audio_path: str | None = None) -> dict:
        c = self.consent()
        if not c["store_feedback"]:
            raise ConsentRequired("saving feedback is off. Turn on 'Save my approved corrections locally' first.")
        corr = rec.get("correction")
        if not isinstance(corr, dict) or not any(corr.get(k) for k in ("hint", "ipa", "correction")):
            raise InvalidInputError("a correction (hint, ipa or correction) is required")
        clean = {
            "id": uuid.uuid4().hex[:12], "created": time.time(), "schema": SCHEMA,
            "original_text": _text("original_text", rec.get("original_text"), required=True),
            "term": _text("term", rec.get("term"), 120, required=True),
            "language": _text("language", rec.get("language"), 20),
            "voice": _text("voice", rec.get("voice"), 60), "backend": _text("backend", rec.get("backend"), 20),
            "pronunciation_settings": rec.get("pronunciation_settings") if isinstance(rec.get("pronunciation_settings"), dict) else {},
            "verification": rec.get("verification") if isinstance(rec.get("verification"), dict) else {},
            "correction": {k: _text(k, corr.get(k), 200) for k in ("written", "hint", "ipa", "correction", "category")},
            "succeeded": rec.get("succeeded") if isinstance(rec.get("succeeded"), bool) else None,
            "versions": rec.get("versions") if isinstance(rec.get("versions"), dict) else {},
            "note": _text("note", rec.get("note"), 500), "audio": None,
        }
        clean["correction"]["written"] = clean["correction"]["written"] or clean["term"]
        if audio_path:
            if not c["store_audio"]:
                raise ConsentRequired("saving audio samples is off. Turn on 'Also keep audio samples' to allow it.")
            src = Path(audio_path)
            if not src.is_file() or src.suffix.lower() != ".wav":
                raise InvalidInputError("audio sample must be an existing .wav file")
            dest = self.root / "audio" / f"{clean['id']}.wav"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            clean["audio"] = f"audio/{clean['id']}.wav"
        recs = self._all()
        recs.append(clean)
        self._save(recs)
        return clean

    def list(self, query: str = "") -> list[dict]:
        q = query.strip().lower()
        return [r for r in self._all() if not q or q in r["term"].lower() or q in r["original_text"].lower()]

    def delete(self, rec_id: str) -> None:
        recs = self._all()
        keep = [r for r in recs if r["id"] != rec_id]
        if len(keep) == len(recs):
            raise InvalidInputError(f"no feedback record {rec_id!r}")
        for r in recs:
            if r["id"] == rec_id and r.get("audio"):
                (self.root / r["audio"]).unlink(missing_ok=True)
        self._save(keep)

    def delete_all(self) -> int:
        n = len(self._all())
        shutil.rmtree(self.root / "audio", ignore_errors=True)
        self._save([])
        return n

    def export(self, dest_dir: str | Path, include_audio: bool = True) -> dict:
        """Copy records (and audio) to a folder the user chose. Export is explicit and local."""
        dest = Path(dest_dir)
        if not dest.is_dir():
            raise InvalidInputError(f"export folder does not exist: {dest}")
        out = dest / f"pronunciation_feedback_{time.strftime('%Y%m%d_%H%M%S')}"
        out.mkdir()
        recs = self._all()
        write_json_atomic(out / "records.json", {"schema": SCHEMA, "records": recs})
        n_audio = 0
        if include_audio:
            for r in recs:
                if r.get("audio") and (self.root / r["audio"]).is_file():
                    (out / "audio").mkdir(exist_ok=True)
                    shutil.copyfile(self.root / r["audio"], out / r["audio"])
                    n_audio += 1
        return {"path": str(out), "records": len(recs), "audio_files": n_audio}

    def stats(self) -> dict:
        recs = self._all()
        return {"records": len(recs), "audio": sum(1 for r in recs if r.get("audio")),
                "succeeded": sum(1 for r in recs if r.get("succeeded") is True),
                "failed": sum(1 for r in recs if r.get("succeeded") is False),
                "location": str(self.root)}

    def approved_examples(self, limit: int = 10) -> list[dict]:
        """Corrections that were verified as successful: the only ones used as prompt examples."""
        out = [r["correction"] for r in self._all() if r.get("succeeded") is True]
        return out[-limit:]


# ----------------------------------------------------------------------------------- regression
@dataclass
class Case:
    id: str
    text: str
    language: str
    term: str
    hint: str
    ipa: str
    correction: str


def build_cases(store: FeedbackStore) -> list[Case]:
    cases = []
    for r in store._all():
        if r.get("succeeded") is True:                      # only corrections that were verified to work
            c = r["correction"]
            cases.append(Case(r["id"], r["original_text"], r.get("language") or "en-us", c.get("written") or r["term"],
                              c.get("hint", ""), c.get("ipa", ""), c.get("correction", "")))
    return cases


def evaluate(entries: list[Entry], cases: list[Case]) -> dict:
    """Does this dictionary still produce every approved correction, and nothing else?
    Deterministic and offline: it checks the text-level result of the dictionary, not audio."""
    d = PronDictionary.in_memory(entries)
    approved = {c.term.lower() for c in cases}
    failures = []
    for c in cases:
        apps = d.find(c.text, c.language, ipa_ok=True)
        want_spoken = c.correction or c.hint
        mine = [a for a in apps if a.original.lower() == c.term.lower()]
        collateral = [a for a in apps if a.original.lower() not in approved]
        if not mine:
            failures.append({"case": c.id, "term": c.term, "why": "no dictionary entry applies to the approved term any more"})
        elif want_spoken and mine[0].mode == "text" and mine[0].spoken != want_spoken:
            failures.append({"case": c.id, "term": c.term, "why": f"now spoken as {mine[0].spoken!r}, approved was {want_spoken!r}"})
        elif not want_spoken and c.ipa and not (mine[0].mode == "ipa" and mine[0].ipa == c.ipa) and mine[0].mode != "text":
            failures.append({"case": c.id, "term": c.term, "why": "approved IPA is no longer applied"})
        for a in collateral:
            failures.append({"case": c.id, "term": a.original, "why": f"unrelated word {a.original!r} would now be changed to {a.spoken!r}"})
    return {"cases": len(cases), "passed": len(cases) - len({f['case'] for f in failures}), "failures": failures,
            "ok": not failures}


def gate_change(dictionary: PronDictionary, store: FeedbackStore, add: Entry | None = None,
                remove_id: str | None = None) -> dict:
    """Evaluate a proposed dictionary change against the regression set BEFORE it is made live."""
    entries = [e for e in dictionary.entries.values() if e.id != remove_id]
    if add:
        entries = [e for e in entries if e.id != add.id] + [add]
    return evaluate(entries, build_cases(store))
