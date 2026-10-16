"""Deterministic TEST backend: a sine tone, not speech. Only registered when
SPEECH_ENGINE_ALLOW_MOCK=1, so it can never be selected (or silently substituted) in normal use.

SPEECH_ENGINE_MOCK_DELAY (seconds per chunk) simulates slow synthesis for cancellation / responsiveness tests.
Text containing "[[fail]]" forces a synthesis error.
"""

from __future__ import annotations

import math
import os
import struct
import threading
import time

from ..exceptions import SynthesisCancelled, SynthesisError
from ..voice_profiles import VoiceProfile
from .base import AudioChunk, Backend, BackendStatus, SynthParams


class MockBackend(Backend):
    name = "mock"
    requires_network = False
    native_sample_rate = 24000
    max_chunk_chars = 60

    def status(self) -> BackendStatus:
        return BackendStatus("mock", True, False, ["TEST BACKEND: emits a sine tone, not speech"])

    def list_voices(self) -> list[str]:
        return ["tone"]

    def synthesize(self, text: str, profile: VoiceProfile, params: SynthParams,
                   cancel: threading.Event) -> AudioChunk:
        if "[[fail]]" in text:
            raise SynthesisError("mock backend: forced failure")
        delay = float(os.environ.get("SPEECH_ENGINE_MOCK_DELAY", "0") or 0)
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if cancel.is_set():
                raise SynthesisCancelled("cancelled")
            time.sleep(0.01)
        if cancel.is_set():
            raise SynthesisCancelled("cancelled")
        n = int(self.native_sample_rate * max(0.2, min(2.0, len(text) * 0.03)))
        pcm = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * i / 24000))) for i in range(n))
        return AudioChunk(pcm, self.native_sample_rate)
