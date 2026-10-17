"""Where user data lives (dictionary, feedback, LLM config, ASR models) plus atomic file helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def data_dir() -> Path:
    """Per-user data folder. Override with SPEECH_ENGINE_DATA_DIR (tests, portable installs)."""
    env = os.environ.get("SPEECH_ENGINE_DATA_DIR")
    if env:
        d = Path(env)
    else:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
        d = Path(base) / "speech_engine"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
