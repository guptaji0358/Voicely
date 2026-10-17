"""Automatic speech recognition providers used by Recheck Speech.

ASR is an INDEPENDENT second opinion on what the audio says. It is not a pronunciation oracle: a recogniser can
make its own mistakes and can "autocorrect" a mispronounced word to the expected spelling. verification.py combines
it with other checks and reports that limitation.

Providers:
  * ``faster-whisper``  (optional dependency, MIT; model weights are NOT bundled and are never downloaded
                         implicitly - see ``download_model``). Runs offline on CPU once the model exists.
  * ``mock``            test double, only when SPEECH_ENGINE_ALLOW_MOCK=1; scripted, never real recognition.

No text from the document is given to the recogniser as a prompt (that would bias it toward the expected words
and hide mispronunciations).
"""

from __future__ import annotations

import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .exceptions import BackendUnavailableError, ConfigurationError, SynthesisCancelled
from .paths import data_dir

DEFAULT_MODEL = "base.en"           # ~140 MB; multilingual alternative: "base" / "small"
MODEL_SIZES = {"tiny.en": "~75 MB", "base.en": "~145 MB", "small.en": "~480 MB", "tiny": "~75 MB", "base": "~145 MB",
               "small": "~480 MB"}


@dataclass
class Word:
    text: str
    start: float
    end: float
    prob: float | None = None          # recogniser's own word probability, when it provides one


@dataclass
class Transcript:
    text: str
    words: list[Word]
    language: str | None
    provider: str
    model: str
    duration_s: float
    language_prob: float | None = None
    mocked: bool = False

    def to_dict(self) -> dict:
        return {"text": self.text, "language": self.language, "provider": self.provider, "model": self.model,
                "duration_s": self.duration_s, "mocked": self.mocked,
                "words": [{"text": w.text, "start": w.start, "end": w.end, "prob": w.prob} for w in self.words]}


@dataclass
class ASRStatus:
    provider: str
    available: bool
    model: str
    details: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    network: bool = False


class ASRProvider(ABC):
    name = "base"

    @abstractmethod
    def status(self) -> ASRStatus: ...

    @abstractmethod
    def transcribe(self, wav_path: str | Path, language: str | None, cancel: threading.Event | None = None) -> Transcript: ...

    def close(self) -> None:
        pass


def asr_model_dir() -> Path:
    env = os.environ.get("SPEECH_ENGINE_ASR_MODEL_DIR")
    d = Path(env) if env else data_dir() / "asr_models"
    return d


def asr_model_name() -> str:
    return os.environ.get("SPEECH_ENGINE_ASR_MODEL", DEFAULT_MODEL)


def _model_cached(name: str, root: Path) -> bool:
    """faster-whisper stores models in a Hugging Face cache layout: models--<org>--faster-whisper-<name>."""
    return root.is_dir() and any(root.glob(f"models--*faster-whisper-{name}"))


class FasterWhisperASR(ASRProvider):
    name = "faster-whisper"

    def __init__(self, model: str | None = None, model_dir: Path | None = None):
        self.model_name = model or asr_model_name()
        self.model_dir = model_dir or asr_model_dir()
        self._model = None
        self._lock = threading.Lock()

    def status(self) -> ASRStatus:
        st = ASRStatus(self.name, True, self.model_name)
        try:
            import faster_whisper  # noqa: F401
            st.details.append("faster-whisper: installed")
        except ImportError:
            st.available = False
            st.problems.append("package 'faster-whisper' is not installed (pip install faster-whisper)")
            return st
        if _model_cached(self.model_name, self.model_dir):
            st.details.append(f"model {self.model_name}: present in {self.model_dir}")
        else:
            st.available = False
            st.problems.append(f"model {self.model_name!r} is not downloaded ({MODEL_SIZES.get(self.model_name, 'size varies')}). "
                               "Download it from the Pronunciation settings (requires your consent; fetches from Hugging Face).")
        st.details.append("network: none at run time (offline)")
        return st

    def _load(self):
        if self._model is not None:
            return self._model
        st = self.status()
        if not st.available:
            raise BackendUnavailableError("; ".join(st.problems))
        from faster_whisper import WhisperModel
        try:
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8",
                                       download_root=str(self.model_dir), local_files_only=True)
        except Exception as exc:
            raise BackendUnavailableError(f"could not load ASR model {self.model_name}: {exc}") from exc
        return self._model

    def transcribe(self, wav_path, language, cancel=None) -> Transcript:
        with self._lock:
            model = self._load()
            lang = (language or "en-us").split("-")[0] if language else None
            if self.model_name.endswith(".en"):
                lang = "en"
            segments, info = model.transcribe(_load_wav_16k(wav_path), language=lang, word_timestamps=True, beam_size=5,
                                              condition_on_previous_text=False, vad_filter=False,
                                              initial_prompt=None, temperature=0.0)
            words: list[Word] = []
            parts: list[str] = []
            for seg in segments:                        # generator: decoding happens here
                if cancel is not None and cancel.is_set():
                    raise SynthesisCancelled("cancelled")
                parts.append(seg.text.strip())
                for w in (seg.words or []):
                    words.append(Word(w.word.strip(), float(w.start), float(w.end), float(w.probability)))
            return Transcript(" ".join(p for p in parts if p), words, getattr(info, "language", lang),
                              self.name, self.model_name, float(getattr(info, "duration", 0.0)),
                              float(getattr(info, "language_probability", 0.0) or 0.0))


def _load_wav_16k(path):
    """Decode our own PCM WAV to mono float32 @16 kHz, so recognition never depends on PyAV/ffmpeg
    (a newer ``av`` release broke faster-whisper's own decoder on this machine)."""
    import wave

    import numpy as np
    with wave.open(str(path), "rb") as w:
        n, ch, width, rate = w.getnframes(), w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(n)
    if width != 2:
        raise ConfigurationError(f"ASR input must be 16-bit PCM WAV (got {width * 8}-bit): {path}")
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if rate != 16000 and len(x):
        m = max(1, int(round(len(x) * 16000 / rate)))
        x = np.interp(np.linspace(0, len(x) - 1, m), np.arange(len(x)), x).astype(np.float32)
    return x


def download_model(model: str | None = None, model_dir: Path | None = None) -> str:
    """Explicit, consent-gated download (network). Returns the local path."""
    name = model or asr_model_name()
    if name not in MODEL_SIZES:
        raise ConfigurationError(f"unsupported ASR model {name!r}; choose one of {', '.join(MODEL_SIZES)}")
    try:
        from faster_whisper.utils import download_model as fw_download
    except ImportError as exc:
        raise BackendUnavailableError("package 'faster-whisper' is not installed (pip install faster-whisper)") from exc
    root = model_dir or asr_model_dir()
    root.mkdir(parents=True, exist_ok=True)
    try:
        return fw_download(name, cache_dir=str(root))
    except Exception as exc:
        raise BackendUnavailableError(f"ASR model download failed: {exc}") from exc


# ------------------------------------------------------------------------ test double
class MockASR(ASRProvider):
    """Deterministic stand-in for tests ONLY. ``script`` maps a WAV file name (or '*') to a transcript string
    or a callable(path)->str. Every result is flagged ``mocked=True`` and surfaced as such in reports."""
    name = "mock"

    def __init__(self, script=None, prob: float = 0.95, fail: Exception | None = None):
        self.script = script if script is not None else {}
        self.prob = prob
        self.fail = fail
        self.calls: list[str] = []

    def status(self) -> ASRStatus:
        return ASRStatus("mock", True, "scripted", ["MOCK ASR: returns scripted text, not real recognition"])

    def transcribe(self, wav_path, language, cancel=None) -> Transcript:
        if self.fail:
            raise self.fail
        p = Path(wav_path)
        self.calls.append(p.name)
        if cancel is not None and cancel.is_set():
            raise SynthesisCancelled("cancelled")
        v = self.script.get(p.name, self.script.get("*", ""))
        text = v(p) if callable(v) else v
        words, t = [], 0.0
        probs = {}
        if isinstance(text, tuple):                       # (text, {word: prob}) lets tests inject low confidence
            text, probs = text
        for w in text.split():
            words.append(Word(w, t, t + 0.3, probs.get(w.strip(".,!?").lower(), self.prob)))
            t += 0.3
        import wave
        try:
            with wave.open(str(p)) as f:
                dur = f.getnframes() / f.getframerate()
        except Exception:
            dur = t
        return Transcript(text, words, language, "mock", "scripted", dur, 1.0, mocked=True)


def make_asr(name: str | None = None) -> ASRProvider:
    name = name or os.environ.get("SPEECH_ENGINE_ASR", "faster-whisper")
    if name == "faster-whisper":
        return FasterWhisperASR()
    if name == "mock" and os.environ.get("SPEECH_ENGINE_ALLOW_MOCK") == "1":
        # Env-driven script for end-to-end worker tests: SPEECH_ENGINE_MOCK_ASR_FILE=<json {filename|*: text}>
        import json
        f = os.environ.get("SPEECH_ENGINE_MOCK_ASR_FILE")
        script = json.loads(Path(f).read_text(encoding="utf-8")) if f and Path(f).is_file() else {"*": ""}
        return MockASR(script)
    raise ConfigurationError(f"unknown ASR provider {name!r}")
