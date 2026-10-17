"""Concrete LLM providers. Credentials come from the environment only and are never logged or returned."""

from __future__ import annotations

import os
import threading

from .base import (LLMAuthError, LLMError, LLMNotConfigured, LLMProvider, LLMRateLimit, LLMTimeout)


def _key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


class GoogleProvider(LLMProvider):
    """Google Gemini via the ``google-genai`` SDK (text -> JSON). Sends the prompt text to Google.

    Not verified here: the model name (``LLMConfig.model``) and quota/cost depend on your Google account and
    Google's current model list - check https://ai.google.dev/gemini-api/docs/models and the pricing page.
    """
    name = "google"
    network = True

    def __init__(self, model: str, client=None):
        self.model = model
        self._client = client            # injectable for tests

    def status(self) -> dict:
        problems, details = [], []
        try:
            import google.genai  # noqa: F401
            details.append("google-genai: installed")
        except ImportError:
            problems.append("package 'google-genai' not installed (pip install google-genai)")
        if _key():
            details.append("API key: set via environment (value hidden)")
        else:
            problems.append("no API key: set GEMINI_API_KEY (or GOOGLE_API_KEY)")
        details.append(f"model: {self.model} (not verified offline)")
        details.append("network: REQUIRED - selected text is sent to Google")
        return {"provider": self.name, "available": not problems, "network": True, "details": details, "problems": problems}

    def complete_json(self, system, prompt, timeout_s, cancel) -> str:
        if cancel is not None and cancel.is_set():
            raise LLMError("cancelled")
        client = self._client
        if client is None:
            key = _key()
            if not key:
                raise LLMNotConfigured("no API key: set GEMINI_API_KEY (or GOOGLE_API_KEY)")
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:
                raise LLMNotConfigured("google-genai is not installed (pip install google-genai)") from exc
            client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=int(timeout_s * 1000)))
        try:
            resp = client.models.generate_content(
                model=self.model, contents=prompt,
                config={"system_instruction": system, "response_mime_type": "application/json", "temperature": 0.1})
            return resp.text or ""
        except Exception as exc:                               # SDK / network errors -> structured errors
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            msg = str(exc)
            if _key():
                msg = msg.replace(_key(), "***")
            msg = msg[:300]
            if code in (401, 403):
                raise LLMAuthError(f"Google rejected the credentials or access (HTTP {code}): {msg}") from exc
            if code == 429:
                raise LLMRateLimit(f"Google rate limit / quota exceeded: {msg}") from exc
            if code in (500, 502, 503, 504):
                raise LLMError(f"Google service error (HTTP {code}): {msg}", retryable=True) from exc
            if isinstance(exc, TimeoutError) or "timed out" in msg.lower() or "timeout" in type(exc).__name__.lower():
                raise LLMTimeout(f"Google request timed out after {timeout_s:.0f}s") from exc
            raise LLMError(f"Google LLM request failed: {msg}") from exc


class MockProvider(LLMProvider):
    """Deterministic provider for tests. ``replies`` is a list of raw reply strings (or exceptions to raise)."""
    name = "mock"
    network = False

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.prompts: list[str] = []

    def status(self) -> dict:
        return {"provider": "mock", "available": True, "network": False, "details": ["MOCK LLM (scripted replies)"], "problems": []}

    def complete_json(self, system, prompt, timeout_s, cancel) -> str:
        self.prompts.append(prompt)
        if not self.replies:
            return "{}"
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
