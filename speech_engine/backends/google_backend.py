"""Optional cloud backend: Gemini speech generation (google-genai SDK, Interactions API).

* Opt-in only: text is sent to Google when, and only when, this backend is selected.
* Credentials come from GEMINI_API_KEY (or GOOGLE_API_KEY); never from source, never printed.
* The voice is one of Google's prebuilt voices; ``google_style`` is a natural-language
  delivery instruction (``speech_metadata.style``). This does not create a new voice.
* Model id is configurable (SPEECH_ENGINE_GOOGLE_MODEL); default follows Google's current
  docs and may change - check https://ai.google.dev/gemini-api/docs/speech-generation.
* Unary responses are WAV (24 kHz mono 16-bit); they are decoded, never played as raw bytes.
"""

from __future__ import annotations

import base64
import os
import re
import threading

from ..audio_pipeline import decode_audio_bytes, time_stretch
from ..google_usage import UsageTracker
from ..exceptions import (BackendUnavailableError, ConfigurationError, RateLimitError,
                          SynthesisCancelled, SynthesisError)
from .base import AudioChunk, Backend, BackendStatus, SynthParams

DEFAULT_MODEL = "gemini-3.8-flash-tts"
PREBUILT_VOICES = [
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe",
    "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib",
    "Rasalgethi", "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima",
    "Achird", "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
]
MAX_ATTEMPTS = 3


def _api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


class GoogleBackend(Backend):
    name = "google"
    requires_network = True
    native_sample_rate = 24000
    max_chunk_chars = 1500

    def __init__(self, model: str | None = None, client=None, sleep=None):
        env = os.environ.get("SPEECH_ENGINE_GOOGLE_MODEL")
        self._explicit_model = bool(model or env)    # an explicitly chosen model is tried first
        self.model = model or env or DEFAULT_MODEL
        self.usage = UsageTracker()
        self._client = client  # injectable for tests
        self._sleep = sleep

    def status(self) -> BackendStatus:
        st = BackendStatus(self.name, True, True)
        try:
            import google.genai  # noqa: F401
            st.details.append("google-genai: installed")
        except ImportError:
            st.available = False
            st.problems.append("package 'google-genai' not installed (pip install google-genai)")
        if _api_key():
            st.details.append("API key: set via environment (value hidden)")
        else:
            st.available = False
            st.problems.append("no API key: set GEMINI_API_KEY (or GOOGLE_API_KEY)")
        st.details.append(f"model: {self.model}")
        st.details.append("network: REQUIRED - text is sent to Google")
        return st

    def list_voices(self) -> list[str]:
        return list(PREBUILT_VOICES)

    def _get_client(self, timeout: float):
        if self._client is not None:
            return self._client
        key = _api_key()
        if not key:
            raise BackendUnavailableError("no API key: set GEMINI_API_KEY (or GOOGLE_API_KEY)")
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise BackendUnavailableError("google-genai is not installed (pip install google-genai)") from exc
        return genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(timeout * 1000)))

    def synthesize(self, text, profile, params, cancel: threading.Event) -> AudioChunk:
        # The text normaliser writes "AI" as "A.I" for the local model; Gemini reads plain "AI" correctly and
        # would spell out the dot.
        text = re.sub(r"(?<!\w)A\.I(?![\w.])", "AI", text)
        voice = profile.google_voice
        if voice not in PREBUILT_VOICES:
            raise ConfigurationError(f"unknown Gemini voice {voice!r}")
        style = params.style if params.style is not None else profile.google_style
        annotations = [{"type": "speech_metadata", "style": style}] if style else []
        content = {"type": "text", "text": text}
        if annotations:
            content["annotations"] = annotations

        # Models are tried in order; one that is over its local limit, answers 429 or does not exist is skipped
        # until the usage window resets, and the next model takes over.
        last: Exception | None = None
        tried = 0
        for model in self.usage.order(self.model if self._explicit_model else None):
            if not self.usage.available(model):
                continue
            tried += 1
            outcome = self._try_model(model, content, voice, params, cancel)
            if not isinstance(outcome, Exception):
                self.model = model
                return outcome
            last = outcome
            if cancel.is_set():
                raise SynthesisCancelled("cancelled")
        if tried == 0:
            raise RateLimitError("every configured Gemini model has reached its limit; usage resets when the window ends")
        code = getattr(last, "code", None) or getattr(last, "status_code", None)
        if code == 429:
            raise RateLimitError("Google rate limit / quota exceeded on every model; retry later") from last
        raise SynthesisError(f"Google synthesis failed: {_short(last)}") from last

    def _try_model(self, model, content, voice, params, cancel):
        """Audio on success, else the exception that means 'try the next model'. Other errors are raised."""
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            if cancel.is_set():
                raise SynthesisCancelled("cancelled")
            client = self._get_client(params.timeout)
            try:
                inter = client.interactions.create(
                    model=model,
                    input=[{"type": "user_input", "content": [content]}],
                    response_format={"type": "audio"},
                    generation_config={"speech_config": [{"voice": voice}]},
                )
                audio = inter.output_audio
                raw = base64.b64decode(audio.data) if isinstance(audio.data, str) else bytes(audio.data)
                chunk = decode_audio_bytes(raw, getattr(audio, "mime_type", None), self.native_sample_rate)
                self.usage.record(model)
                return time_stretch(chunk, params.speed) if params.speed else chunk
            except SynthesisError:
                raise
            except Exception as exc:  # SDK/network errors
                last = exc
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                if code == 429:                      # quota gone for this model: switch instead of waiting
                    self.usage.mark_exhausted(model)
                    return exc
                if code == 404:                      # unknown / retired model id: try the next one
                    self.usage.mark_exhausted(model)
                    return exc
                retryable = code in (500, 502, 503, 504) or isinstance(exc, (TimeoutError, ConnectionError))
                if code in (400, 401, 403):
                    raise ConfigurationError(f"Google rejected the request (HTTP {code}): {_short(exc)}") from exc
                if not retryable or attempt == MAX_ATTEMPTS - 1:
                    break
                delay = 2 ** attempt
                if cancel.wait(delay) if self._sleep is None else self._sleep(delay):
                    raise SynthesisCancelled("cancelled")
        raise SynthesisError(f"Google synthesis failed: {_short(last)}") from last


def _short(exc: Exception | None) -> str:
    msg = str(exc)
    key = _api_key()
    if key:
        msg = msg.replace(key, "***")
    return msg[:300]
