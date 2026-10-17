"""Versioned, validated, leakage-safe datasets (JSONL on disk, memory-mapped token arrays for training).

A dataset version is an immutable folder:  datasets/<name>/vNNNN-<hash>/{train,val,test,regression}.jsonl + manifest.json
  * records are validated (task, label set, length) and de-duplicated; contradictory labels for the same text are EXCLUDED and reported
  * train/val/test are split by a hash of the normalised text, so the same text can never be in two splits
  * the regression split is supplied separately (held-out triggers / user-approved cases) and may not overlap the other splits
  * manifest records provenance, counts, label distribution, seed and a SHA-256 for every file; loading verifies them
Model output is never written here as ground truth; only supplied targets are.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import BOS, EOS, PAD
from .tokenizer import Tokenizer

SPLITS = ("train", "val", "test", "regression")
MAX_TEXT = 400
SEP = " => "
IGNORE = -100


class DatasetError(Exception):
    pass


def norm_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def validate_record(r: dict, labels: list[str], task: str) -> str | None:
    """Return a reason string if the record is unusable, else None."""
    if not isinstance(r, dict):
        return "not an object"
    t, y = r.get("text"), r.get("target")
    if not isinstance(t, str) or not t.strip():
        return "empty text"
    if len(t) > MAX_TEXT:
        return f"text longer than {MAX_TEXT} characters"
    if r.get("task", task) != task:
        return f"task is not {task}"
    if y not in labels:
        return f"target {y!r} not in label set"
    return None


def clean_records(records: list[dict], labels: list[str], task: str) -> tuple[list[dict], dict]:
    """Validate, de-duplicate and drop contradictions. Returns (kept, report)."""
    rep = {"input": len(records), "invalid": [], "duplicates": 0, "contradictions": []}
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        why = validate_record(r, labels, task)
        if why:
            rep["invalid"].append({"text": str(r.get("text"))[:80] if isinstance(r, dict) else "", "reason": why})
            continue
        by_key[norm_key(r["text"])].append(r)
    kept = []
    for key, rs in by_key.items():
        targets = {r["target"] for r in rs}
        if len(targets) > 1:
            rep["contradictions"].append({"text": rs[0]["text"][:80], "targets": sorted(targets)})
            continue
        rep["duplicates"] += len(rs) - 1
        r = dict(rs[0])
        r["id"] = _h(task + "|" + key)[:12]
        r["task"] = task
        kept.append(r)
    rep["kept"] = len(kept)
    return kept, rep


def split_of(text: str, seed: int, val_pct: int = 10, test_pct: int = 10) -> str:
    """Deterministic split by hash of the normalised text (same text -> same split, always)."""
    b = int(_h(f"{seed}|{norm_key(text)}")[:8], 16) % 100
    return "test" if b < test_pct else "val" if b < test_pct + val_pct else "train"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def build_dataset(root: Path, name: str, records: list[dict], labels: list[str], task: str, seed: int, *,
                  regression: list[dict] | None = None, provenance: str, parent: str | None = None) -> dict:
    """Create a NEW immutable dataset version. Returns its manifest (with ``path``)."""
    kept, rep = clean_records(records, labels, task)
    reg, reg_rep = clean_records(regression or [], labels, task)
    splits: dict[str, list[dict]] = {s: [] for s in SPLITS}
    for r in kept:
        splits[split_of(r["text"], seed)].append(r)
    main_keys = {norm_key(r["text"]) for s in ("train", "val", "test") for r in splits[s]}
    leaked = [r for r in reg if norm_key(r["text"]) in main_keys]
    splits["regression"] = [r for r in reg if norm_key(r["text"]) not in main_keys]
    for s in SPLITS:
        splits[s].sort(key=lambda r: r["id"])
    if not splits["train"] or not splits["val"]:
        raise DatasetError(f"too little data after cleaning (train={len(splits['train'])}, val={len(splits['val'])})")
    check_no_overlap(splits)

    base = Path(root) / name
    base.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=base, prefix=".build-"))
    try:
        files = {}
        for s in SPLITS:
            _write_jsonl(tmp / f"{s}.jsonl", splits[s])
            files[f"{s}.jsonl"] = hashlib.sha256((tmp / f"{s}.jsonl").read_bytes()).hexdigest()
        content_hash = _h("".join(files[k] for k in sorted(files)))[:8]
        seq = 1 + len([p for p in base.iterdir() if p.is_dir() and p.name.startswith("v")])
        version = f"v{seq:04d}-{content_hash}"
        dist = {s: {l: sum(1 for r in splits[s] if r["target"] == l) for l in labels} for s in SPLITS}
        manifest = {"schema": 1, "name": name, "version": version, "task": task, "labels": labels, "seed": seed,
                    "created": time.time(), "provenance": provenance, "parent": parent,
                    "counts": {s: len(splits[s]) for s in SPLITS}, "label_distribution": dist, "files": files,
                    "cleaning": {k: (v if not isinstance(v, list) else len(v)) for k, v in rep.items()},
                    "contradictions": rep["contradictions"][:50], "invalid_examples": rep["invalid"][:50],
                    "regression_leaked_removed": len(leaked), "regression_cleaning": {k: (len(v) if isinstance(v, list) else v)
                                                                                     for k, v in reg_rep.items()}}
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        final = base / version
        if final.exists():
            raise DatasetError(f"{final} already exists (datasets are immutable)")
        tmp.rename(final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    manifest["path"] = str(final)
    return manifest


def check_no_overlap(splits: dict[str, list[dict]]) -> None:
    seen: dict[str, str] = {}
    for s, rs in splits.items():
        for r in rs:
            k = norm_key(r["text"])
            if k in seen and seen[k] != s:
                raise DatasetError(f"text appears in both {seen[k]} and {s}: {r['text'][:60]!r}")
            seen[k] = s


def load_dataset(path: Path, verify: bool = True) -> tuple[dict, dict[str, list[dict]]]:
    path = Path(path)
    mf = path / "manifest.json"
    if not mf.is_file():
        raise DatasetError(f"{path} is not a dataset version (no manifest.json)")
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    if verify:
        for fname, digest in manifest["files"].items():
            p = path / fname
            if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
                raise DatasetError(f"{fname} does not match the manifest checksum (modified or corrupted)")
    splits = {s: _read_jsonl(path / f"{s}.jsonl") for s in SPLITS}
    check_no_overlap(splits)
    manifest["path"] = str(path)
    return manifest, splits


def list_datasets(root: Path) -> list[dict]:
    out = []
    for mf in sorted(Path(root).glob("*/v*/manifest.json")):
        m = json.loads(mf.read_text(encoding="utf-8"))
        out.append({"name": m["name"], "version": m["version"], "path": str(mf.parent), "counts": m["counts"],
                    "task": m["task"], "provenance": m["provenance"], "created": m["created"]})
    return out


def delete_dataset(path: Path) -> None:
    path = Path(path)
    if not (path / "manifest.json").is_file():
        raise DatasetError(f"{path} is not a dataset version")
    shutil.rmtree(path)


def export_dataset(path: Path, dest: Path) -> str:
    dest = Path(dest)
    if dest.exists():
        raise DatasetError(f"{dest} already exists")
    shutil.copytree(path, dest, ignore=shutil.ignore_patterns("tokens_*", "plen_*"))
    return str(dest)


# --------------------------------------------------------------------------- feedback import
FEEDBACK_CATEGORY_MAP = {"acronym": "acronym", "abbreviation": "abbreviation", "number": "number", "date": "date", "unit": "unit",
                         "heteronym": "heteronym"}


def import_feedback_records(fb_records: list[dict], labels: list[str], task: str = "review_category") -> tuple[list[dict], list[dict]]:
    """Turn user-approved feedback (speech_engine.feedback) into dataset records.

    Only corrections whose recheck SUCCEEDED and whose category maps onto this task's label set are used; everything else is
    returned in the second list with the reason. The text is the user's own sentence; it stays local.
    """
    used, skipped = [], []
    for r in fb_records:
        cat = (r.get("correction") or {}).get("category", "")
        if r.get("succeeded") is not True:
            skipped.append({"id": r.get("id"), "reason": "correction was not confirmed by a recheck"})
        elif cat not in FEEDBACK_CATEGORY_MAP or FEEDBACK_CATEGORY_MAP[cat] not in labels:
            skipped.append({"id": r.get("id"), "reason": f"category {cat!r} has no label in this task"})
        else:
            used.append({"text": r["original_text"][:MAX_TEXT], "target": FEEDBACK_CATEGORY_MAP[cat], "language": r.get("language", ""),
                         "source": "feedback", "task": task, "feedback_id": r.get("id")})
    return used, skipped


# --------------------------------------------------------------------------- tokenised, memory-mapped training arrays
def encode_example(tok: Tokenizer, r: dict) -> tuple[list[int], int]:
    """ids = BOS text SEP label EOS ; returns (ids, prompt_len) where ids[prompt_len:] is what the model must produce."""
    prompt = tok.encode(r["text"], bos=True) + tok.encode(SEP)
    return prompt + tok.encode(r["target"]) + [EOS], len(prompt)


def tokenize_split(tok: Tokenizer, records: list[dict], max_seq: int, out_dir: Path, split: str, tag: str = "") -> dict:
    suffix = f"{tag}_" if tag else ""
    rows, plens, dropped = [], [], 0
    for r in records:
        ids, pl = encode_example(tok, r)
        if len(ids) > max_seq:
            dropped += 1
            continue
        rows.append(ids + [PAD] * (max_seq - len(ids)))
        plens.append(pl)
    dtype = np.int16 if tok.size < 32000 else np.int32
    arr = np.asarray(rows, dtype=dtype).reshape(len(rows), max_seq)
    np.save(Path(out_dir) / f"tokens_{suffix}{split}.npy", arr)
    np.save(Path(out_dir) / f"plen_{suffix}{split}.npy", np.asarray(plens, dtype=np.int16))
    return {"rows": len(rows), "dropped_too_long": dropped}


class TokenSet:
    """Memory-mapped view of one tokenised split: the file stays on disk, only the batch being used is read."""

    def __init__(self, dataset_dir: Path, split: str, tag: str = ""):
        suffix = f"{tag}_" if tag else ""
        self.tokens = np.load(Path(dataset_dir) / f"tokens_{suffix}{split}.npy", mmap_mode="r")
        self.plen = np.load(Path(dataset_dir) / f"plen_{suffix}{split}.npy", mmap_mode="r")

    def __len__(self) -> int:
        return int(self.tokens.shape[0])

    def batch(self, idx, label_only: bool = True):
        """x, y (next-token targets) as int64 arrays. y is IGNORE where it should not count (padding, and the prompt if label_only)."""
        import torch
        rows = np.asarray(self.tokens[np.sort(idx)], dtype=np.int64)
        pl = np.asarray(self.plen[np.sort(idx)], dtype=np.int64)
        x, y = rows[:, :-1].copy(), rows[:, 1:].copy()
        y[rows[:, 1:] == PAD] = IGNORE
        if label_only:
            pos = np.arange(y.shape[1])[None, :]
            y[pos < (pl[:, None] - 1)] = IGNORE            # predictions of prompt tokens do not count
        return torch.from_numpy(x), torch.from_numpy(y)
