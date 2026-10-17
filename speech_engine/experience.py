"""Opt-in, local-only record of what the user generated and how the recheck judged it - the raw material the Model Lab learns from.

Why text + recheck outcome, and not the audio itself: a text model cannot learn from waveforms, and a generation on its own has no
label. The label comes from an independent check - the speech recogniser + phoneme comparison in Recheck Speech - so a segment is
labelled ``yes`` (the recheck found something to review / failed) or ``no`` (every automated check passed). It is NOT ground truth
(the recogniser can be wrong) and the record says where each label came from.

Rules enforced here:
  * nothing is stored unless the person switched it on (default off); switching it off stops capture immediately
  * everything stays in the user's data folder; nothing is uploaded; records can be listed, exported and deleted
  * results from a MOCK recogniser, or from a run where the recogniser itself failed, are never turned into labels
  * a label is set once, from the first check of the ORIGINAL audio (a later repair must not rewrite what the original looked like)
This module deliberately does not import torch, so the speech worker can use it without the optional Model Lab.
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from .paths import data_dir, read_json, write_json_atomic

SCHEMA = 1
MAX_TEXT = 1000
MAX_RECORDS = 50_000
POSITIVE = ("needs_review", "failed")


class ExperienceStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else data_dir() / "experience"
        self.records_path = self.root / "records.json"
        self.consent_path = self.root / "consent.json"

    # ---- consent ---------------------------------------------------------------------------------
    def consent(self) -> dict:
        c = read_json(self.consent_path, {}) or {}
        return {"learn_from_generations": bool(c.get("learn_from_generations", False)), "updated": c.get("updated")}

    def set_consent(self, enabled: bool) -> dict:
        write_json_atomic(self.consent_path, {"learn_from_generations": bool(enabled), "updated": time.time()})
        return self.consent()

    @property
    def enabled(self) -> bool:
        return self.consent()["learn_from_generations"]

    # ---- storage ---------------------------------------------------------------------------------
    def _all(self) -> list[dict]:
        d = read_json(self.records_path, None)
        return d["records"] if isinstance(d, dict) and isinstance(d.get("records"), list) else []

    def _save(self, recs: list[dict]) -> None:
        write_json_atomic(self.records_path, {"schema": SCHEMA, "records": recs})

    def add_generation(self, session_id: str, chunks: list[dict], language: str, voice: str, backend: str) -> int:
        """Remember the text chunks of one generation (unlabelled until a recheck). Returns how many were stored (0 when off)."""
        if not self.enabled or not session_id:
            return 0
        recs = self._all()
        have = {r["key"] for r in recs}
        added = 0
        for c in chunks:
            text, idx = c.get("text"), c.get("index")
            key = f"{session_id}:{idx}"
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or key in have or len(recs) >= MAX_RECORDS:
                continue
            recs.append({"id": uuid.uuid4().hex[:12], "key": key, "text": text, "language": language or "", "voice": voice or "",
                         "backend": backend or "", "label": None, "label_source": None, "issue_types": [], "created": time.time()})
            added += 1
        if added:
            self._save(recs)
        return added

    def label_from_recheck(self, session_id: str, index: int, status: str, mocked: bool, issue_types: list[str],
                           recogniser_error: bool = False) -> bool:
        """Attach the recheck outcome to a stored chunk. Returns True when a label was set."""
        if not self.enabled or mocked or recogniser_error or status not in ("verified",) + POSITIVE:
            return False
        recs = self._all()
        for r in recs:
            if r["key"] == f"{session_id}:{index}":
                if r["label"] is not None:
                    return False                                   # first check of the original audio wins
                r["label"] = "no" if status == "verified" else "yes"
                r["label_source"] = "asr-recheck"
                r["issue_types"] = sorted(set(issue_types))[:12]
                self._save(recs)
                return True
        return False

    # ---- inspection ------------------------------------------------------------------------------
    def stats(self) -> dict:
        recs = self._all()
        yes = sum(r["label"] == "yes" for r in recs)
        no = sum(r["label"] == "no" for r in recs)
        return {"total": len(recs), "labelled": yes + no, "yes": yes, "no": no, "unlabelled": len(recs) - yes - no,
                "consent": self.enabled, "path": str(self.root)}

    def list(self, limit: int = 200) -> list[dict]:
        return self._all()[-limit:]

    def labelled(self) -> list[dict]:
        return [r for r in self._all() if r["label"] in ("yes", "no")]

    def delete(self, rec_id: str) -> None:
        recs = self._all()
        kept = [r for r in recs if r["id"] != rec_id]
        if len(kept) == len(recs):
            raise KeyError(rec_id)
        self._save(kept)

    def delete_all(self) -> int:
        n = len(self._all())
        if self.records_path.exists():
            self.records_path.unlink()
        return n

    def export(self, dest_dir: str | Path) -> dict:
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / "experience.json"
        if out.exists():
            raise FileExistsError(str(out))
        shutil.copyfile(self.records_path, out) if self.records_path.exists() else write_json_atomic(out, {"schema": SCHEMA, "records": []})
        return {"path": str(out), "records": len(self._all())}
