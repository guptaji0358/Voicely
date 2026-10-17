"""Optional espeak-ng phonemisation used for phoneme-level comparison. Everything degrades gracefully:
when espeak is unavailable ``phonemize_word`` returns None and callers fall back to spelling similarity."""

from __future__ import annotations

import functools
import threading

_lock = threading.Lock()
_backends: dict[str, object] = {}
_unavailable: str | None = None


def _backend(lang: str):
    global _unavailable
    if _unavailable:
        return None
    with _lock:
        if lang in _backends:
            return _backends[lang]
        try:
            import espeakng_loader
            from phonemizer.backend import EspeakBackend
            from phonemizer.backend.espeak.wrapper import EspeakWrapper
            EspeakWrapper.set_library(espeakng_loader.get_library_path())
            EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
            b = EspeakBackend(lang, preserve_punctuation=False, with_stress=False, language_switch="remove-flags")
        except Exception as exc:                       # missing package, bad language code, DLL problems
            if lang == "en-us":
                _unavailable = f"{type(exc).__name__}: {exc}"
            _backends[lang] = None
            return None
        _backends[lang] = b
        return b


def available(lang: str = "en-us") -> bool:
    return _backend(lang) is not None


@functools.lru_cache(maxsize=4096)
def phonemize_word(word: str, lang: str = "en-us") -> str | None:
    b = _backend(lang)
    if b is None or not word.strip():
        return None
    try:
        out = b.phonemize([word], strip=True)
    except Exception:
        return None
    return (out[0] if out else "").replace(" ", "") or None


def edit_distance(a: str | list, b: str | list) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    """1.0 identical .. 0.0 unrelated, over characters (or phonemes)."""
    if not a and not b:
        return 1.0
    return 1.0 - edit_distance(a, b) / max(len(a), len(b))


def phone_count(text: str, lang: str = "en-us") -> int | None:
    """Rough phoneme count of a sentence (None if espeak is unavailable)."""
    b = _backend(lang)
    if b is None or not text.strip():
        return None
    try:
        out = b.phonemize([text], strip=True)
    except Exception:
        return None
    return len((out[0] if out else "").replace(" ", "")) or None
