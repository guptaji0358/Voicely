"""LLM provider interface, structured errors and persisted configuration (never the credentials)."""

from __future__ import annotations

import json
import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, fields

from ..paths import data_dir, read_json, write_json_atomic


class LLMError(Exception):
    """Structured failure. ``code`` is stable for the GUI; ``retryable`` drives the service's retry loop."""
    code = "LLM_ERROR"
    retryable = False

    def __init__(self, message: str, retryable: bool | None = None):
        super().__init__(message)
        if retryable is not None:
            self.retryable = retryable


class LLMDisabled(LLMError):
    code = "LLM_DISABLED"


class LLMNotConfigured(LLMError):
    code = "LLM_NOT_CONFIGURED"


class LLMNetworkNotAllowed(LLMError):
    code = "LLM_NETWORK_NOT_ALLOWED"


class LLMAuthError(LLMError):
    code = "LLM_AUTH"


class LLMRateLimit(LLMError):
    code = "LLM_RATE_LIMIT"
    retryable = True


class LLMTimeout(LLMError):
    code = "LLM_TIMEOUT"
    retryable = True


class LLMInvalidOutput(LLMError):
    """The model answered, but not with data that passed validation. Nothing from it is applied."""
    code = "LLM_INVALID_OUTPUT"


@dataclass
class LLMConfig:
    enabled: bool = False                  # OFF by default: basic speech never depends on the LLM
    provider: str = "google"               # "google" | "mock"; a failing provider is never swapped for another
    model: str = "gemini-2.5-flash"        # configurable; verify against the provider's current model list
    timeout_s: float = 30.0
    max_retries: int = 2
    min_interval_s: float = 1.0            # client-side rate limit between requests
    max_input_chars: int = 6000            # text sent per request (limits cost and exposure)
    use_examples: bool = False             # include approved local corrections as few-shot examples (sent to the provider!)
    prompt_version: str = "pron-1"

    @classmethod
    def load(cls) -> "LLMConfig":
        raw = read_json(data_dir() / "llm_config.json", {}) or {}
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        write_json_atomic(data_dir() / "llm_config.json", asdict(self))

    def update(self, **kw) -> "LLMConfig":
        known = {f.name: f.type for f in fields(self)}
        for k, v in kw.items():
            if k not in known:
                raise LLMError(f"unknown LLM setting {k!r}")
            cur = getattr(self, k)
            if isinstance(cur, bool) != isinstance(v, bool) or (not isinstance(cur, bool) and not isinstance(v, type(cur))
                                                                  and not (isinstance(cur, float) and isinstance(v, int))):
                raise LLMError(f"setting {k!r} has the wrong type")
            setattr(self, k, v)
        if not 1 <= self.timeout_s <= 300 or not 0 <= self.max_retries <= 5 or not 0 <= self.min_interval_s <= 60 \
                or not 200 <= self.max_input_chars <= 50000:
            raise LLMError("LLM settings out of range")
        self.save()
        return self


class LLMProvider(ABC):
    name = "base"
    network = False

    @abstractmethod
    def status(self) -> dict:
        """Cheap, offline readiness check (no request is made, no secrets are returned)."""

    @abstractmethod
    def complete_json(self, system: str, prompt: str, timeout_s: float, cancel: threading.Event | None) -> str:
        """Return the raw text of the model's reply (expected to be JSON). May raise LLMError subclasses."""


def parse_json_object(raw: str) -> dict:
    """Strict JSON object parse (tolerates a ```json fence, nothing else)."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        s = s[4:] if s.lower().startswith("json") else s
    try:
        obj = json.loads(s)
    except ValueError as exc:
        raise LLMInvalidOutput(f"reply is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise LLMInvalidOutput("reply must be a JSON object")
    return obj
