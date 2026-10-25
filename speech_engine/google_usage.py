"""Local usage tracking for the Gemini models, used for automatic model switching and the usage bar.

Google's API does not report remaining quota, so requests are counted here. Two files live in the data folder:

* ``google_models.json`` - EDITABLE. Fallback order, per-model request limit per window (0 = unlimited) and the window
  length in hours. Created with PLACEHOLDER values on first use: correct them from your AI Studio rate-limit page.
* ``google_usage.json`` - counters. The window starts at the first request and every counter resets
  ``window_hours`` (default 10) later. A model that returns HTTP 429 is treated as used up until then.

The counts are an estimate of Google's real accounting; a 429 from Google always wins over the local count.
"""

from __future__ import annotations

import threading
import time

from .paths import data_dir, read_json, write_json_atomic

CONFIG_NAME = "google_models.json"
USAGE_NAME = "google_usage.json"
DEFAULT_WINDOW_HOURS = 10.0
# PLACEHOLDERS - not verified against Google's real limits. Edit google_models.json in the data folder.
DEFAULT_MODELS = [
    {"id": "gemini-3.8-flash-tts", "limit": 100},
    {"id": "gemini-2.5-flash-preview-tts", "limit": 100},
    {"id": "gemini-2.5-pro-preview-tts", "limit": 50},
]
_lock = threading.Lock()


def load_config() -> dict:
    """Return ``{"window_hours": float, "models": [{"id", "limit"}]}``, writing the placeholder file if missing."""
    path = data_dir() / CONFIG_NAME
    raw = read_json(path, None) if path.exists() else None
    if raw is None:
        raw = {"_note": "PLACEHOLDER limits - correct them from your AI Studio rate-limit page. limit 0 = unlimited.",
               "window_hours": DEFAULT_WINDOW_HOURS, "models": DEFAULT_MODELS}
        try:
            write_json_atomic(path, raw)
        except OSError:
            pass
    models = []
    for m in raw.get("models", []) if isinstance(raw, dict) else []:
        try:
            mid, limit = str(m["id"]).strip(), int(m.get("limit", 0))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if mid and mid not in [x["id"] for x in models]:
            models.append({"id": mid, "limit": max(0, limit)})
    try:
        hours = float(raw.get("window_hours", DEFAULT_WINDOW_HOURS))
    except (TypeError, ValueError, AttributeError):
        hours = DEFAULT_WINDOW_HOURS
    return {"window_hours": hours if hours > 0 else DEFAULT_WINDOW_HOURS, "models": models or [dict(m) for m in DEFAULT_MODELS]}


class UsageTracker:
    def __init__(self, clock=time.time):
        self._clock = clock

    # ---- state ----------------------------------------------------------
    def _state(self, cfg: dict) -> dict:
        st = read_json(data_dir() / USAGE_NAME, None)
        if not isinstance(st, dict):
            st = {}
        start = st.get("window_start")
        if not isinstance(start, (int, float)) or self._clock() - start >= cfg["window_hours"] * 3600:
            return {"window_start": None, "counts": {}, "exhausted": []}
        st.setdefault("counts", {})
        st.setdefault("exhausted", [])
        return st

    def _save(self, st: dict) -> None:
        try:
            write_json_atomic(data_dir() / USAGE_NAME, st)
        except OSError:
            pass

    def _touch(self, st: dict) -> None:
        if st["window_start"] is None:
            st["window_start"] = self._clock()

    # ---- queries / updates ---------------------------------------------
    def order(self, preferred: str | None = None) -> list[str]:
        """Model ids to try, in order: ``preferred`` (if given) first, then the configured fallback list."""
        ids = [m["id"] for m in load_config()["models"]]
        if preferred:
            ids = [preferred] + [i for i in ids if i != preferred]
        return ids

    def available(self, model: str) -> bool:
        with _lock:
            cfg = load_config()
            st = self._state(cfg)
            if model in st["exhausted"]:
                return False
            limit = next((m["limit"] for m in cfg["models"] if m["id"] == model), 0)
            return not (limit and st["counts"].get(model, 0) >= limit)

    def record(self, model: str) -> None:
        with _lock:
            st = self._state(load_config())
            self._touch(st)
            st["counts"][model] = st["counts"].get(model, 0) + 1
            self._save(st)

    def mark_exhausted(self, model: str) -> None:
        """Google answered 429 for this model: skip it until the window resets."""
        with _lock:
            cfg = load_config()
            st = self._state(cfg)
            self._touch(st)
            if model not in st["exhausted"]:
                st["exhausted"].append(model)
            limit = next((m["limit"] for m in cfg["models"] if m["id"] == model), 0)
            if limit:
                st["counts"][model] = max(st["counts"].get(model, 0), limit)
            self._save(st)

    def snapshot(self) -> dict:
        with _lock:
            cfg = load_config()
            st = self._state(cfg)
        now = self._clock()
        models = []
        for m in cfg["models"]:
            used = int(st["counts"].get(m["id"], 0))
            models.append({"id": m["id"], "used": used, "limit": m["limit"],
                           "exhausted": m["id"] in st["exhausted"] or bool(m["limit"] and used >= m["limit"])})
        start = st["window_start"]
        reset_in = max(0, int(start + cfg["window_hours"] * 3600 - now)) if start is not None else int(cfg["window_hours"] * 3600)
        limited = [m for m in models if m["limit"]]
        return {"models": models, "window_hours": cfg["window_hours"], "window_started": start is not None,
                "reset_in_s": reset_in,
                "total_used": sum(min(m["used"], m["limit"]) for m in limited),
                "total_limit": sum(m["limit"] for m in limited)}
