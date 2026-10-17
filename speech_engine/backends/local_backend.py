"""Local neural TTS: Kokoro-82M (Apache-2.0 weights) via kokoro-onnx (MIT).

Fully offline once the two model files exist. Runs on CPU; audio stays in RAM.
Weights are NEVER downloaded implicitly - see ``download_model`` (explicit, CLI-gated).

Voice customisation: Kokoro voices are style-embedding arrays; ``local_blend`` in
the profile mixes several of them by weight, producing a timbre that is not any
single shipped voice. It is not a trained voice.
"""

from __future__ import annotations

import os
import threading
import urllib.request
from pathlib import Path

import numpy as np

from ..audio_pipeline import float_to_pcm16, resample
from ..exceptions import (BackendUnavailableError, ConfigurationError, SynthesisCancelled,
                          SynthesisError)
from ..voice_profiles import VoiceProfile
from .base import AudioChunk, Backend, BackendStatus, SynthParams

MODEL_BASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
# (filename, approx size) - sizes are approximate, from the project's release notes/README.
MODEL_FILES = {
    "kokoro-v1.0.onnx": "~310 MB (fp32)",
    "kokoro-v1.0.int8.onnx": "~80 MB (int8 quantised, slightly lower quality)",
    "voices-v1.0.bin": "~27 MB (required with either model)",
}
VOICES_FILE = "voices-v1.0.bin"


def default_model_dir() -> Path:
    env = os.environ.get("SPEECH_ENGINE_MODEL_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "speech_engine" / "models"


def find_model_files(model_dir: Path | None = None) -> tuple[Path | None, Path | None]:
    d = model_dir or default_model_dir()
    explicit = os.environ.get("SPEECH_ENGINE_KOKORO_MODEL")
    model = Path(explicit) if explicit else None
    if model is None:
        for name in ("kokoro-v1.0.onnx", "kokoro-v1.0.int8.onnx"):
            if (d / name).is_file():
                model = d / name
                break
    voices = d / VOICES_FILE
    return (model if model and model.is_file() else None, voices if voices.is_file() else None)


def download_model(which: str = "kokoro-v1.0.onnx", model_dir: Path | None = None, progress=None) -> list[Path]:
    """Download model + voices to ``model_dir`` via .part files. Call only after user consent."""
    if which not in MODEL_FILES or which == VOICES_FILE:
        raise ConfigurationError(f"model must be one of: kokoro-v1.0.onnx, kokoro-v1.0.int8.onnx")
    d = model_dir or default_model_dir()
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for name in (which, VOICES_FILE):
        dest = d / name
        if dest.is_file():
            out.append(dest)
            continue
        part = dest.with_suffix(dest.suffix + ".part")
        try:
            with urllib.request.urlopen(f"{MODEL_BASE_URL}/{name}", timeout=60) as r, open(part, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while block := r.read(1 << 20):
                    f.write(block)
                    got += len(block)
                    if progress:
                        progress(name, got, total)
            part.replace(dest)
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise BackendUnavailableError(f"download of {name} failed: {exc}") from exc
        out.append(dest)
    return out


class LocalBackend(Backend):
    name = "local"
    requires_network = False
    native_sample_rate = 24000
    max_chunk_chars = 350
    supports_ipa = True

    def __init__(self, model_dir: Path | None = None):
        self._dir = model_dir
        self._kokoro = None
        self._lock = threading.Lock()
        self._style_cache: dict[tuple, np.ndarray] = {}

    # -- status -----------------------------------------------------------
    def status(self) -> BackendStatus:
        st = BackendStatus(self.name, True, False)
        try:
            import kokoro_onnx  # noqa: F401
            st.details.append("kokoro-onnx: installed")
        except ImportError:
            st.available = False
            st.problems.append("package 'kokoro-onnx' not installed (pip install kokoro-onnx)")
        model, voices = find_model_files(self._dir)
        d = self._dir or default_model_dir()
        if model and voices:
            st.details.append(f"model: {model.name} ({model.stat().st_size // (1 << 20)} MB)")
        else:
            st.available = False
            missing = [n for n, p in (("model .onnx", model), (VOICES_FILE, voices)) if p is None]
            st.problems.append(
                f"missing {', '.join(missing)} in {d}. Run: python -m speech_engine.cli download-model --yes"
            )
        st.details.append("network: none (offline)")
        return st

    # -- internals --------------------------------------------------------
    def _load(self):
        if self._kokoro is not None:
            return self._kokoro
        try:
            from kokoro_onnx import Kokoro
        except ImportError as exc:
            raise BackendUnavailableError("kokoro-onnx is not installed (pip install kokoro-onnx)") from exc
        model, voices = find_model_files(self._dir)
        if not (model and voices):
            raise BackendUnavailableError(self.status().problems[-1] if self.status().problems else "model files missing")
        try:
            self._kokoro = Kokoro(str(model), str(voices))
        except Exception as exc:  # onnxruntime / file errors
            raise BackendUnavailableError(f"could not load Kokoro model: {exc}") from exc
        return self._kokoro

    def _style(self, k, profile: VoiceProfile):
        if not profile.local_blend:
            raise ConfigurationError(f"profile {profile.name!r} has no 'local_blend' voices")
        key = tuple(sorted(profile.local_blend.items()))
        if key in self._style_cache:
            return self._style_cache[key]
        known = set(k.get_voices())
        bad = [v for v in profile.local_blend if v not in known]
        if bad:
            raise ConfigurationError(f"unknown Kokoro voice(s) {bad}; available: {sorted(known)}")
        total = sum(profile.local_blend.values())
        style = sum(k.get_voice_style(v) * (w / total) for v, w in profile.local_blend.items())
        self._style_cache[key] = style.astype(np.float32)
        return self._style_cache[key]

    def ipa_vocabulary(self) -> set[str] | None:
        try:
            from kokoro_onnx.tokenizer import Tokenizer
            return set(Tokenizer().vocab)
        except Exception:
            return None

    def _phonemes_with_overrides(self, k, text: str, overrides, lang: str) -> str:
        """Phonemise ``text`` normally, except that each override word is replaced by its IPA."""
        import re
        pats = [(re.compile(r"(?<!\w)" + re.escape(w) + r"(?!\w)", re.IGNORECASE), ipa) for w, ipa in overrides if w and ipa]
        marks = []
        for rx, ipa in pats:
            for m in rx.finditer(text):
                marks.append((m.start(), m.end(), ipa))
        marks.sort()
        out, pos, last_end = [], 0, -1
        for s0, e0, ipa in marks:
            if s0 < last_end:
                continue
            seg = text[pos:s0]
            if seg.strip():
                out.append(k.tokenizer.phonemize(seg, lang).strip())
            out.append(ipa)
            pos, last_end = e0, e0
        tail = text[pos:]
        if tail.strip():
            out.append(k.tokenizer.phonemize(tail, lang).strip())
        return " ".join(p for p in out if p)

    def list_voices(self) -> list[str]:
        try:
            return list(self._load().get_voices())
        except BackendUnavailableError:
            return []

    # -- synthesis --------------------------------------------------------
    def synthesize(self, text, profile, params, cancel) -> AudioChunk:
        if cancel.is_set():
            raise SynthesisCancelled("cancelled")
        # Pitch up by synthesising slower, then shortening by the same ratio (tempo preserved).
        ratio = 2 ** (profile.pitch_semitones / 12)
        with self._lock:
            k = self._load()
            style = self._style(k, profile)
            try:
                use_ph = bool(params.overrides)
                payload = self._phonemes_with_overrides(k, text, params.overrides, profile.local_lang) if use_ph else text
                samples, rate = k.create(
                    payload, voice=style,
                    speed=(params.speed or profile.speed) / ratio,
                    lang=profile.local_lang,
                    is_phonemes=use_ph,
                    sentence_pause=profile.sentence_pause,
                    clause_pause=profile.clause_pause,
                )
            except Exception as exc:
                raise SynthesisError(f"local synthesis failed: {exc}") from exc
        if samples is None or len(samples) == 0:
            raise SynthesisError("local backend produced no audio for this chunk")
        chunk = AudioChunk(float_to_pcm16(samples), int(rate))
        if ratio != 1.0:
            shortened = resample(chunk, int(chunk.sample_rate / ratio))
            chunk = AudioChunk(shortened.pcm, chunk.sample_rate)
        return chunk

    def close(self) -> None:
        self._kokoro = None
        self._style_cache.clear()
