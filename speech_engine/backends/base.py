"""Backend interface. Providers implement this; the engine never imports them directly.

Extension point for a custom-trained voice
------------------------------------------
A trained model would be a third ``Backend`` subclass (e.g. ``custom_backend.py``)
that loads your own checkpoint (ONNX/PyTorch) and implements ``synthesize``.
Nothing else in the package needs to change. What that project requires:
  * a licensed, consented speech dataset (typically 10-30+ h of one studio
    speaker for fine-tuning a pretrained model such as StyleTTS2/VITS; far more
    for training from scratch), with transcripts and written speaker consent;
  * GPU compute for training (days on one modern GPU for fine-tuning);
  * an evaluation protocol (held-out text, MOS-style listening tests, WER via
    ASR round trip, speaker-similarity drift on long text);
  * an exportable inference graph (ONNX) and a license review of the base model.
No such training is performed or implied by this repository.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..voice_profiles import VoiceProfile


@dataclass(frozen=True)
class AudioChunk:
    """Mono signed 16-bit little-endian PCM. Never compressed bytes."""

    pcm: bytes
    sample_rate: int

    @property
    def duration(self) -> float:
        return len(self.pcm) / 2 / self.sample_rate


@dataclass
class BackendStatus:
    name: str
    available: bool
    network: bool
    details: list[str] = field(default_factory=list)  # never contains secrets
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SynthParams:
    speed: float | None = None      # overrides profile speed where supported
    style: str | None = None        # overrides profile style where supported (google)
    timeout: float = 60.0
    # (written word, IPA) pairs the backend should pronounce exactly; ignored unless ``supports_ipa``.
    overrides: tuple = ()


class Backend(ABC):
    name = "base"
    requires_network = False
    native_sample_rate = 24000
    max_chunk_chars = 400
    supports_ipa = False            # can take IPA/phoneme overrides for individual words
    supports_respelling = True      # text respellings are read as ordinary text by every backend

    def ipa_vocabulary(self) -> set[str] | None:
        """Characters this backend can pronounce when given IPA (None = unknown / IPA unsupported)."""
        return None

    @abstractmethod
    def status(self) -> BackendStatus:
        """Cheap check of configuration/availability. Must not synthesize or leak secrets."""

    @abstractmethod
    def synthesize(
        self,
        text: str,
        profile: VoiceProfile,
        params: SynthParams,
        cancel: threading.Event,
    ) -> AudioChunk:
        """Synthesize one chunk. Raise a SpeechEngineError subclass on failure;
        never substitute another voice."""

    def list_voices(self) -> list[str]:
        return []

    def close(self) -> None:
        """Release resources. Idempotent."""
