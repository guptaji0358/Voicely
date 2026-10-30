"""Optional cloud backend: Azure AI Speech (neural voices, REST text-to-speech, SSML).

* Opt-in only: text is sent to Microsoft when, and only when, this backend is selected.
* Credentials come from AZURE_SPEECH_KEY and AZURE_SPEECH_REGION; never from source, never printed.
* The voice is an Azure neural voice (``profile.azure_voice``; blank = a multilingual neural voice chosen from
  the profile's gender, overridable with SPEECH_ENGINE_AZURE_VOICE). It does not create a new voice.
* Speed is sent as an SSML ``prosody`` rate, so Azure changes the tempo itself instead of us stretching audio.
* Output is requested as 24 kHz mono 16-bit WAV and decoded, never played as raw bytes.
* Azure allows about 20 requests per 60 s on the free tier; requests are spaced to stay under that
  (SPEECH_ENGINE_AZURE_RPM overrides the figure). Check https://learn.microsoft.com/azure/ai-services/speech-service/
  for the current limits and voice list.
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from xml.sax.saxutils import escape

from ..audio_pipeline import decode_audio_bytes
from ..exceptions import (BackendUnavailableError, ConfigurationError, RateLimitError,
                          SynthesisCancelled, SynthesisError)
from .base import AudioChunk, Backend, BackendStatus, SynthParams

DEFAULT_VOICES = {"female": "en-US-AvaMultilingualNeural", "male": "en-US-AndrewMultilingualNeural"}
FALLBACK_VOICE = DEFAULT_VOICES["female"]
OUTPUT_FORMAT = "riff-24khz-16bit-mono-pcm"
DEFAULT_RPM = 20
WINDOW_S = 60.0
MAX_ATTEMPTS = 3
_VOICE_RE = re.compile(r"^[A-Za-z]{2,3}-[A-Za-z0-9]{2,8}-[A-Za-z0-9:_.-]+$")


def _key() -> str | None:
    return os.environ.get("AZURE_SPEECH_KEY")


def _region() -> str | None:
    return os.environ.get("AZURE_SPEECH_REGION")


def _urllib_post(url: str, headers: dict, body: bytes, timeout: float):
    """(status, body, retry_after_seconds_or_None). HTTP errors are returned, not raised."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), None
    except urllib.error.HTTPError as exc:
        ra = exc.headers.get("Retry-After") if exc.headers else None
        try:
            retry = float(ra) if ra else None
        except ValueError:
            retry = None
        return exc.code, exc.read()[:500], retry


class AzureBackend(Backend):
    name = "azure"
    requires_network = True
    native_sample_rate = 24000
    max_chunk_chars = 1500

    def __init__(self, post=None, clock=None, rpm: int | None = None):
        self._post = post or _urllib_post              # injectable for tests
        self._clock = clock or time.monotonic
        try:
            self._rpm = max(1, rpm or int(os.environ.get("SPEECH_ENGINE_AZURE_RPM", DEFAULT_RPM)))
        except ValueError:
            self._rpm = DEFAULT_RPM
        self._sent: deque[float] = deque()
        self._lock = threading.Lock()

    def status(self) -> BackendStatus:
        st = BackendStatus(self.name, True, True)
        if _key():
            st.details.append("AZURE_SPEECH_KEY: set via environment (value hidden)")
        else:
            st.available = False
            st.problems.append("no API key: set AZURE_SPEECH_KEY")
        if _region():
            st.details.append(f"region: {_region()}")
        else:
            st.available = False
            st.problems.append("no region: set AZURE_SPEECH_REGION (for example eastus)")
        st.details.append(f"rate limit: {self._rpm} requests per {int(WINDOW_S)} s (requests are spaced to fit)")
        st.details.append("network: REQUIRED - text is sent to Microsoft Azure")
        return st

    def list_voices(self) -> list[str]:
        return sorted(set(DEFAULT_VOICES.values()))

    def _voice(self, profile) -> str:
        voice = (getattr(profile, "azure_voice", "") or os.environ.get("SPEECH_ENGINE_AZURE_VOICE", "")
                 or DEFAULT_VOICES.get(getattr(profile, "gender", ""), FALLBACK_VOICE))
        if not _VOICE_RE.match(voice):
            raise ConfigurationError(f"invalid Azure voice name {voice!r}")
        return voice

    @staticmethod
    def _ssml(text: str, voice: str, speed: float | None) -> bytes:
        lang = "-".join(voice.split("-")[:2])
        body = escape(text)
        if speed and abs(speed - 1.0) > 0.01:
            body = f'<prosody rate="{round((speed - 1.0) * 100):+d}%">{body}</prosody>'
        xml = (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='{lang}'>"
               f"<voice name='{voice}'>{body}</voice></speak>")
        return xml.encode("utf-8")

    def _wait_for_slot(self, cancel: threading.Event) -> None:
        """Block (cancellably) until another request fits in the rolling window."""
        while True:
            with self._lock:
                now = self._clock()
                while self._sent and now - self._sent[0] >= WINDOW_S:
                    self._sent.popleft()
                if len(self._sent) < self._rpm:
                    self._sent.append(now)
                    return
                delay = WINDOW_S - (now - self._sent[0])
            if cancel.wait(max(0.05, delay)):
                raise SynthesisCancelled("cancelled")

    def synthesize(self, text, profile, params: SynthParams, cancel: threading.Event) -> AudioChunk:
        key, region = _key(), _region()
        if not key or not region:
            raise BackendUnavailableError("set AZURE_SPEECH_KEY and AZURE_SPEECH_REGION")
        if not re.fullmatch(r"[A-Za-z0-9-]{2,40}", region):
            raise ConfigurationError("AZURE_SPEECH_REGION must be a region id such as eastus")
        text = re.sub(r"(?<!\w)A\.I(?![\w.])", "AI", text)       # the normaliser's "A.I" would be spelt out
        voice = self._voice(profile)
        body = self._ssml(text, voice, params.speed)
        url = f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1"
        headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                   "X-Microsoft-OutputFormat": OUTPUT_FORMAT, "User-Agent": "Voicely"}

        last = ""
        for attempt in range(MAX_ATTEMPTS):
            if cancel.is_set():
                raise SynthesisCancelled("cancelled")
            self._wait_for_slot(cancel)
            try:
                status, data, retry_after = self._post(url, headers, body, params.timeout)
            except (TimeoutError, ConnectionError, OSError) as exc:
                status, data, retry_after, last = None, b"", None, type(exc).__name__
            if status == 200:
                return decode_audio_bytes(data, None, self.native_sample_rate)
            if status is not None:
                last = f"HTTP {status}: {_short(data)}"
            if status in (400, 401, 403):
                raise ConfigurationError(f"Azure rejected the request ({last})")
            retryable = status in (None, 429, 500, 502, 503, 504)
            if not retryable:
                raise SynthesisError(f"Azure synthesis failed ({last})")
            if attempt == MAX_ATTEMPTS - 1:
                break
            delay = min(retry_after if retry_after else 2 ** attempt, 30)
            if cancel.wait(delay):
                raise SynthesisCancelled("cancelled")
        if status == 429:
            raise RateLimitError("Azure rate limit / quota exceeded; retry later")
        raise SynthesisError(f"Azure synthesis failed ({last})")


def _short(data: bytes) -> str:
    msg = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
    key = _key()
    if key:
        msg = msg.replace(key, "***")
    return msg[:200]
