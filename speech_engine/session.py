"""A speech *session*: the generated audio kept as per-chunk WAV files plus a manifest, so that
  * a problem found by Recheck Speech can be traced to one chunk and the exact source text,
  * only the affected chunk is regenerated (not the document),
  * the ORIGINAL audio is never modified: every regeneration adds a new version file; ``assembled/original.wav``
    is written once and stays until the session folder is deleted,
  * a repaired mix is a separate candidate file until the user accepts it.

Layout (inside the folder the GUI hands us):
    manifest.json
    chunks/0003.g0.wav      version g0 = original synthesis, g1.. = later regenerations
    assembled/original.wav  all g0 chunks            (immutable)
    assembled/accepted.wav  the accepted selection   (== original until a repair is accepted)
    assembled/candidate.wav the proposed repair      (until accepted or discarded)
Only file paths cross the worker protocol; audio bytes never do.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
import wave
from dataclasses import asdict
from pathlib import Path

from .audio_pipeline import crossfade_edges, resample, silence
from .backends.base import AudioChunk, Backend, SynthParams
from .exceptions import InvalidInputError, SynthesisCancelled
from .pron_dictionary import Application, PronDictionary
from .text_preprocessor import iter_chunks
from .voice_profiles import VoiceProfile

SCHEMA = 1


def write_wav(path: Path, chunk: AudioChunk) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(chunk.sample_rate)
            w.writeframes(chunk.pcm)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_wav(path: Path) -> AudioChunk:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise InvalidInputError(f"{path.name} is not 16-bit mono WAV")
        return AudioChunk(w.readframes(w.getnframes()), w.getframerate())


def locate_chunks(original: str, chunk_texts: list[str]) -> list[tuple[int, int, bool] | None]:
    """Best-effort (start, end, exact) offsets of each chunk in the ORIGINAL text. Chunking normalises whitespace,
    hyphen wraps and abbreviations, so matching ignores whitespace; a chunk that cannot be found gets None."""
    keep = [i for i, c in enumerate(original) if not c.isspace()]
    squashed = "".join(original[i] for i in keep).casefold()
    out, cursor = [], 0
    for ct in chunk_texts:
        q = re.sub(r"\s+", "", ct).casefold()
        pos = squashed.find(q, cursor) if q else -1
        if pos >= 0:
            out.append((keep[pos], keep[pos + len(q) - 1] + 1, True))
            cursor = pos + len(q)
            continue
        # tolerate expanded abbreviations / removed hyphens: match on the first and last 12 characters
        head, tail = q[:12], q[-12:]
        h = squashed.find(head, cursor) if head else -1
        t = squashed.find(tail, max(cursor, h) if h >= 0 else cursor) if tail else -1
        if h < 0 and t >= 0:                       # head was altered (e.g. "Dr." -> "Doctor"): anchor on the tail
            h = max(cursor, t + len(tail) - len(q))
        elif t < 0 and h >= 0:
            t = min(len(squashed) - len(tail), h + len(q) - len(tail))
        if h >= 0 and t >= 0:
            end = min(t + len(tail), len(keep))
            out.append((keep[h], keep[end - 1] + 1, False))
            cursor = end
        else:
            out.append(None)
    return out


class Session:
    def __init__(self, folder: str | Path):
        self.dir = Path(folder)
        self.manifest_path = self.dir / "manifest.json"
        self.m: dict = {}

    # ---- persistence --------------------------------------------------------------
    @classmethod
    def open(cls, folder: str | Path) -> "Session":
        s = cls(folder)
        if not s.manifest_path.is_file():
            raise InvalidInputError(f"no speech session in {folder} (generate speech first)")
        import json
        try:
            s.m = json.loads(s.manifest_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise InvalidInputError(f"session manifest is damaged: {exc}") from exc
        if s.m.get("schema") != SCHEMA:
            raise InvalidInputError("unsupported session schema")
        return s

    def save(self) -> None:
        import json
        tmp = self.manifest_path.with_name(".manifest.json.part")
        tmp.write_text(json.dumps(self.m, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.manifest_path)

    # ---- creation -----------------------------------------------------------------
    @classmethod
    def create(cls, folder: str | Path, text: str, backend: Backend, profile: VoiceProfile, params: SynthParams,
               language: str, dictionary: PronDictionary | None, cancel: threading.Event,
               progress=None, versions: dict | None = None, emotion: tuple = (None, None)) -> "Session":
        folder = Path(folder)
        if folder.exists() and not folder.is_dir():
            raise InvalidInputError(f"{folder} is not a folder")
        folder.mkdir(parents=True, exist_ok=True)
        s = cls(folder)
        chunks = list(iter_chunks(text, max_chars=backend.max_chunk_chars,
                                  paragraph_pause_ms=profile.paragraph_pause_ms,
                                  sentence_pause_ms=profile.sentence_pause_ms))
        spans = locate_chunks(text, [c.text for c in chunks])
        s.m = {"schema": SCHEMA, "session_id": uuid.uuid4().hex[:12], "created": time.time(),
               "original_text": text, "language": language, "backend": backend.name, "voice": profile.name,
               "speed": params.speed, "style": params.style,
               "emotion": emotion[0], "emotion_intensity": emotion[1], "dictionary_version": dictionary.version if dictionary else 0,
               "versions": versions or {}, "sample_rate": None, "chunks": [],
               "selection": {"accepted": {}, "candidate": None}}
        for i, ch in enumerate(chunks):
            if cancel.is_set():
                raise SynthesisCancelled("cancelled")
            apps = dictionary.find(ch.text, language, ipa_ok=backend.supports_ipa) if dictionary else []
            v = s._synth_version(i, "g0", ch.text, apps, backend, profile, params, language, "initial", cancel)
            sp = spans[i]
            s.m["chunks"].append({"index": i, "text": ch.text, "pause_ms": ch.pause_ms, "paragraph_end": ch.paragraph_end,
                                  "source_span": list(sp[:2]) if sp else None, "source_span_exact": bool(sp and sp[2]),
                                  "versions": [v]})
            s.m["selection"]["accepted"][str(i)] = "g0"
            if progress:
                progress(i + 1, len(chunks), ch.text)
        s.save()
        return s

    def _synth_version(self, idx: int, vid: str, chunk_text: str, apps: list[Application], backend: Backend,
                       profile: VoiceProfile, params: SynthParams, language: str, method: str,
                       cancel: threading.Event, extra_overrides: list[tuple[str, str]] | None = None) -> dict:
        spoken = PronDictionary.apply(chunk_text, apps)
        overrides = [(a.original, a.ipa) for a in apps if a.mode == "ipa" and a.ipa] + list(extra_overrides or [])
        p = SynthParams(speed=params.speed, style=params.style, timeout=params.timeout,
                        overrides=tuple(overrides) if backend.supports_ipa else ())
        audio = crossfade_edges(backend.synthesize(spoken, profile, p, cancel))
        rate = self.m.get("sample_rate")
        if rate is None:
            self.m["sample_rate"] = rate = audio.sample_rate
        elif audio.sample_rate != rate:
            audio = resample(audio, rate)
        rel = f"chunks/{idx:04d}.{vid}.wav"
        write_wav(self.dir / rel, audio)
        return {"id": vid, "file": rel, "spoken": spoken, "method": method, "duration_s": round(audio.duration, 3),
                "applications": [asdict(a) for a in apps], "overrides": [list(o) for o in overrides], "created": time.time()}

    # ---- access -------------------------------------------------------------------
    @property
    def chunks(self) -> list[dict]:
        return self.m["chunks"]

    def version(self, idx: int, vid: str) -> dict:
        try:
            return next(v for v in self.chunks[idx]["versions"] if v["id"] == vid)
        except (IndexError, StopIteration):
            raise InvalidInputError(f"no chunk {idx} version {vid}") from None

    def selected(self, which: str = "accepted") -> dict[int, str]:
        sel = dict(self.m["selection"]["accepted"])
        if which == "candidate" and self.m["selection"]["candidate"]:
            sel.update(self.m["selection"]["candidate"])
        return {int(k): v for k, v in sel.items()}

    def path_of(self, idx: int, vid: str) -> Path:
        return self.dir / self.version(idx, vid)["file"]

    def application_objects(self, idx: int, vid: str) -> list[Application]:
        return [Application(**a) for a in self.version(idx, vid)["applications"]]

    # ---- assembly -----------------------------------------------------------------
    def assemble(self, which: str, out: Path | None = None, cancel: threading.Event | None = None) -> Path:
        """Concatenate the selected chunk versions (+ the same pauses as the original) into one WAV."""
        sel = self.selected(which)
        target = out or self.dir / "assembled" / f"{which}.wav"
        if which == "original":
            sel = {c["index"]: "g0" for c in self.chunks}
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{os.getpid()}.part")
        rate = self.m["sample_rate"]
        try:
            with wave.open(str(tmp), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                for c in self.chunks:
                    if cancel is not None and cancel.is_set():
                        raise SynthesisCancelled("cancelled")
                    a = read_wav(self.path_of(c["index"], sel[c["index"]]))
                    w.writeframes(a.pcm)
                    if c["pause_ms"]:
                        w.writeframes(silence(c["pause_ms"], rate).pcm)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return target

    # ---- targeted regeneration --------------------------------------------------------
    def regenerate_chunk(self, idx: int, backend: Backend, profile: VoiceProfile, params: SynthParams,
                         dictionary: PronDictionary | None, overlay: list[Application] | None,
                         extra_overrides: list[tuple[str, str]] | None, method: str,
                         cancel: threading.Event) -> dict:
        """Synthesize ONE chunk again into a NEW version file. Dictionary applications can be extended by
        ``overlay`` (temporary, e.g. a proposed respelling) without touching the persistent dictionary."""
        c = self.chunks[idx]
        language = self.m["language"]
        apps = dictionary.find(c["text"], language, ipa_ok=backend.supports_ipa) if dictionary else []
        if overlay:
            taken = [(a.start, a.end) for a in overlay]
            apps = [a for a in apps if not any(a.start < e and a.end > s for s, e in taken)] + list(overlay)
            apps.sort(key=lambda a: a.start)
        vid = f"g{len(c['versions'])}"
        v = self._synth_version(idx, vid, c["text"], apps, backend, profile, params, language, method, cancel, extra_overrides)
        c["versions"].append(v)
        self.save()
        return v

    def set_candidate(self, selection: dict[int, str] | None) -> None:
        self.m["selection"]["candidate"] = {str(k): v for k, v in selection.items()} if selection else None
        self.save()

    def accept_candidate(self) -> Path:
        cand = self.m["selection"]["candidate"]
        if not cand:
            raise InvalidInputError("there is no repaired version to accept")
        self.m["selection"]["accepted"].update(cand)
        self.m["selection"]["candidate"] = None
        self.save()
        return self.assemble("accepted")

    def discard_candidate(self) -> None:
        self.m["selection"]["candidate"] = None
        self.save()
