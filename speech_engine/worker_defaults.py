"""Which backend is used when a request names none: online (Gemini) unless the offline one is re-enabled."""

import os

LOCAL_ENABLED = os.environ.get("SPEECH_ENGINE_ALLOW_LOCAL") == "1"
DEFAULT_BACKEND = "local" if LOCAL_ENABLED else "google"


def stretch_of(req: dict) -> float:
    """Elongated-word hold from a request: 0 (off) .. 3, default 1."""
    try:
        return max(0.0, min(float(req.get("stretch", 1.0)), 3.0))
    except (TypeError, ValueError):
        return 1.0
