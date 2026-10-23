"""Local neural TTS: Kokoro-82M (Apache-2.0 weights) via kokoro-onnx (MIT).

Fully offline once the two model files exist. Runs on CPU; audio stays in RAM.
Weights are NEVER downloaded implicitly - see ``download_model`` (explicit, CLI-gated).

Voice customisation: Kokoro voices are style-embedding arrays; ``local_blend`` in
the profile mixes several of them by weight, producing a timbre that is not any
single shipped voice. It is not a trained voice.
"""

from __future__ import annotations

import os
import re
import threading
import urllib.request
from pathlib import Path

import numpy as np

from ..audio_pipeline import float_to_pcm16, resample, time_stretch
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


def _tilt(chunk: AudioChunk, db: float, corner_hz: float = 1500.0) -> AudioChunk:
    """Spectral tilt: boost (db > 0) or cut the highs and do the opposite to the lows around ``corner_hz``.
    Two complementary first-order shelves, so overall level stays about the same."""
    from scipy.signal import lfilter
    x = np.frombuffer(chunk.pcm, "<i2").astype(np.float32)
    a = np.exp(-2 * np.pi * corner_hz / chunk.sample_rate)
    low = lfilter([1 - a], [1, -a], x)                   # one-pole low-pass
    g = 10 ** (db / 40)                                  # +-db/2 at each end
    y = low / g + (x - low) * g
    return AudioChunk(np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes(), chunk.sample_rate)


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
            self._kokoro = self._open(Kokoro, model, voices)
        except Exception as exc:  # onnxruntime / file errors
            raise BackendUnavailableError(f"could not load Kokoro model: {exc}") from exc
        return self._kokoro

    @staticmethod
    def _mapped_model(model: Path) -> Path:
        """A copy of the model whose weights live in a separate ``.data`` file. onnxruntime memory-maps such a file, so the
        ~310 MB of weights are file-backed pages that Windows can drop and re-read from disk at any time instead of private
        memory (measured: private commit 796 -> 581 MB; audio bit-identical, the graph is only re-saved, not changed).
        Built once next to the model (costs ~310 MB of disk); any failure falls back to the original file."""
        import shutil
        import tempfile
        import onnxruntime as rt
        name = model.stem + ".mapped.onnx"                  # one copy per model file (fp32 / int8 never get mixed up)
        d = model.parent / ("mapped-" + model.stem)
        out = d / name
        data = d / (name + ".data")
        if out.is_file() and data.is_file() and out.stat().st_mtime >= model.stat().st_mtime:
            return out
        tmp = Path(tempfile.mkdtemp(prefix=".mapped-", dir=str(model.parent)))
        try:
            so = rt.SessionOptions()
            so.graph_optimization_level = rt.GraphOptimizationLevel.ORT_DISABLE_ALL     # re-save only, no fusions
            so.optimized_model_filepath = str(tmp / name)
            so.add_session_config_entry("session.optimized_model_external_initializers_file_name", name + ".data")
            so.add_session_config_entry("session.optimized_model_external_initializers_min_size_in_bytes", "1024")
            rt.InferenceSession(str(model), sess_options=so, providers=["CPUExecutionProvider"])
            if not ((tmp / name).is_file() and (tmp / (name + ".data")).is_file()):
                raise RuntimeError("external-weights copy was not written")
            shutil.rmtree(d, ignore_errors=True)
            os.replace(tmp, d)
            return out
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    @staticmethod
    def _open(Kokoro, model: Path, voices: Path):
        """Open the model with a low-memory onnxruntime session. By default its CPU memory arena keeps every allocation made
        for every utterance length (the worker grew from ~420 MB to ~1.1 GB while speaking a few paragraphs). Here the
        arena is shrunk back after every run and memory-pattern planning is off. Same model, same maths: the audio is
        bit-identical, only the allocator behaviour changes (measured: ~430 MB steady instead of ~1.1 GB, similar speed).
        TTS_LOW_MEMORY=0 restores the library default."""
        if os.environ.get("TTS_LOW_MEMORY", "1") != "0" and hasattr(Kokoro, "from_session"):
            try:
                import onnxruntime as rt
                from kokoro_onnx.session import resolve_providers
                so = rt.SessionOptions()
                so.enable_mem_pattern = False
                run_opts = rt.RunOptions()
                run_opts.add_run_config_entry("memory.enable_memory_arena_shrinkage", "cpu:0")
                path = model
                if os.environ.get("TTS_MAPPED_MODEL", "1") != "0":
                    try:
                        path = LocalBackend._mapped_model(model)
                    except Exception:
                        path = model                # cannot write next to the model, etc.: use the original
                sess = rt.InferenceSession(str(path), sess_options=so, providers=resolve_providers())

                class _Shrinking:                   # Kokoro only calls .run(None, inputs); everything else passes through
                    _model_path = str(model)

                    def run(self, outputs, inputs, run_options=None):
                        return sess.run(outputs, inputs, run_options=run_opts)

                    def __getattr__(self, name):
                        return getattr(sess, name)

                return Kokoro.from_session(_Shrinking(), str(voices))
            except Exception:                       # older kokoro-onnx / unexpected API: fall back to the default session
                pass
        return Kokoro(str(model), str(voices))

    def unload(self) -> bool:
        """Free the model (about 400 MB). It is reloaded from disk on the next request. Returns True if it was loaded."""
        with self._lock:
            had = self._kokoro is not None
            self._kokoro = None
            self._style_cache.clear()
        if had:
            import gc
            gc.collect()
        return had

    def _style(self, k, profile: VoiceProfile):
        if not profile.local_blend:
            raise ConfigurationError(f"profile {profile.name!r} has no 'local_blend' voices")
        key = (tuple(sorted(profile.local_blend.items())), profile.style_push)
        if key in self._style_cache:
            return self._style_cache[key]
        known = set(k.get_voices())
        bad = [v for v in profile.local_blend if v not in known]
        if bad:
            raise ConfigurationError(f"unknown Kokoro voice(s) {bad}; available: {sorted(known)}")
        total = sum(profile.local_blend.values())
        style = sum(k.get_voice_style(v) * (w / total) for v, w in profile.local_blend.items())
        if profile.style_push:
            # Averaging voices pulls every blend toward the same "typical" voice. Pushing away from the mean of all
            # voices of the same sex (2nd letter of the Kokoro name) restores what makes this blend individual.
            sex = {v[1] for v in profile.local_blend}
            ref = [k.get_voice_style(v) for v in known if v[0] in "ab" and v[1] in sex] or [style]
            style = style + profile.style_push * (style - np.mean(ref, axis=0))
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

    # The text normaliser writes the acronym "AI" as "A.I". Kokoro's own phonemes for it ("ˈeɪ.ˈaɪ") are heard as
    # "eight eye", so the letter names are given directly. A chunk that is nothing but the acronym is too short for
    # the model to anchor the first vowel ("day I"), so a comma is added there (checked with speech recognition).
    _MIN_NATIVE_SPEED = 0.85

    _AI = re.compile(r"(?<!\w)A\.I(?!\w)", re.IGNORECASE)
    _AI_ALONE = re.compile(r"\W*A\.I\W*", re.IGNORECASE)

    def _builtin_overrides(self, text: str, lang: str) -> tuple:
        if not (lang or "").lower().startswith("en") or not self._AI.search(text):
            return ()
        return (("A.I", "ˈeɪ, ˈaɪ" if self._AI_ALONE.fullmatch(text) else "ˈeɪ ˈaɪ"),)

    # Elongated words ("helloooooo", "bachooooo", "yesssss", "nooo"): a letter written 3+ times in a row is held for
    # longer. The word is read as its normal spelling and the matching phoneme is repeated; Kokoro gives each repeated
    # token its own ~50 ms, so the sound is sustained smoothly (about 1.5 tokens per extra letter, capped).
    _ELONG = re.compile(r"\b\w*?([A-Za-z])\1{2,}\w*\b")
    _RUN = re.compile(r"([A-Za-z])\1{2,}", re.IGNORECASE)
    _TOKENS_PER_LETTER = 1.5
    _MAX_EXTRA_TOKENS = 48
    _VOWELS = set("aeiouɑɐɒæɔəɛɜɪɵʊʌɚɝᵻɤ")
    _HELD = {"s": "s", "z": "z", "f": "f", "v": "v", "m": "m", "n": "n", "l": "l", "r": "ɹ", "h": "h"}
    _DOUBLED = {"please", "sweet", "see", "feel", "need", "good", "cool", "soon", "too", "look", "free", "tree", "keep",
                "sleep", "deep", "sheep", "meet", "week", "green", "queen", "sorry", "happy", "yellow", "hello", "all",
                "well", "tell", "still", "kiss", "miss", "boss", "less", "wow", "oops", "zoo", "moon", "food", "room",
                "mood", "cheese", "freeze", "squeeze", "between", "peek", "seek", "speed", "agree", "coffee", "balloon"}

    def _elongation_overrides(self, k, text: str, lang: str) -> tuple:
        if not (lang or "").lower().startswith("en") or not self._ELONG.search(text):
            return ()
        out = []
        for m in self._ELONG.finditer(text):
            word = m.group(0)
            if len(set(word.lower())) == 1:                 # "mmmm", "zzzz": no word to hold, leave to the model
                continue
            run = max(self._RUN.finditer(word), key=lambda g: len(g.group(0)))
            ch, n = run.group(1).lower(), len(run.group(0))
            at_end = run.end() == len(word) or not word[run.end():].strip("s'’")
            head, tail = word[:run.start()], word[run.end():]
            base = head + ch + tail
            if ch in "eo" and (head + ch * 2 + tail).lower() in self._DOUBLED:
                base = head + ch * 2 + tail
            try:
                ipa = k.tokenizer.phonemize(base, lang).strip()
            except Exception:
                continue
            extra = min(int((n - 1) * self._TOKENS_PER_LETTER + 0.5), self._MAX_EXTRA_TOKENS)
            out.append((word, self._hold_phoneme(ipa, ch, extra, at_end)))
        return tuple(out)

    @classmethod
    def _hold_phoneme(cls, ipa: str, letter: str, extra: int, at_end: bool) -> str:
        """Repeat the phoneme that ``letter`` stands for ``extra`` more times inside ``ipa``."""
        if letter in cls._HELD:
            i = ipa.rfind(cls._HELD[letter])
            return ipa if i < 0 else ipa[:i + 1] + ipa[i] * extra + ipa[i + 1:]
        if letter not in "aeiouy":
            return ipa
        groups, i = [], 0                                   # vowel groups: (start, stress-marked)
        while i < len(ipa):
            if ipa[i] in cls._VOWELS:
                j = i
                while j < len(ipa) and ipa[j] in cls._VOWELS:
                    j += 1
                groups.append((i, i > 0 and ipa[i - 1] == "ˈ"))
                i = j
            else:
                i += 1
        if not groups:
            return ipa
        pick = groups[-1] if at_end else next((g for g in groups if g[1]), groups[0])
        return ipa[:pick[0] + 1] + ipa[pick[0]] * extra + ipa[pick[0] + 1:]

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
                overrides = (tuple(params.overrides) + self._builtin_overrides(text, profile.local_lang)
                             + self._elongation_overrides(k, text, profile.local_lang))
                use_ph = bool(overrides)
                payload = self._phonemes_with_overrides(k, text, overrides, profile.local_lang) if use_ph else text
                # Kokoro's duration model glitches (repeats the first sound) below ~0.85x, so slower speech is produced at the
                # model's floor and the remainder is applied as a pitch-preserving time stretch afterwards.
                want = (params.speed or profile.speed) / ratio
                native = min(max(want, self._MIN_NATIVE_SPEED), 2.0)
                samples, rate = k.create(
                    payload, voice=style,
                    speed=native,
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
        if want < native - 0.005:
            chunk = time_stretch(chunk, want / native)
        if ratio != 1.0:
            shortened = resample(chunk, int(chunk.sample_rate / ratio))
            chunk = AudioChunk(shortened.pcm, chunk.sample_rate)
        if profile.tilt_db:
            chunk = _tilt(chunk, profile.tilt_db)
        return chunk

    def close(self) -> None:
        self._kokoro = None
        self._style_cache.clear()
