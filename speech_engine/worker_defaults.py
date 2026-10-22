"""Which backend is used when a request names none: online (Gemini) unless the offline one is re-enabled."""

import os

LOCAL_ENABLED = os.environ.get("SPEECH_ENGINE_ALLOW_LOCAL") == "1"
DEFAULT_BACKEND = "local" if LOCAL_ENABLED else "google"
