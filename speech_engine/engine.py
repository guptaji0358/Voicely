"""High-level speech engine: chunking -> backend -> PCM stream -> speaker / file.

Guarantees:
  * one voice per request; a backend failure raises, it never switches voice/backend;
  * a new ``speak_async`` cancels any speech still playing (no overlap);
  * generation is lazy and bounded (generator + small prefetch queue).
"""

from __future__ import annotations

import os
import os
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Iterator

from .audio_pipeline import Player, crossfade_edges, make_sink, resample, silence
from .backends.base import AudioChunk, Backend, SynthParams
from .exceptions import InvalidInputError, SynthesisCancelled
from .text_preprocessor import Chunk, iter_chunks
from .voice_profiles import VoiceProfile

ProgressFn = Callable[[int, int | None, str], None]  # (done, total_or_None, text)


def make_backend(name: str) -> Backend:
    if name == "local":
        from .backends.local_backend import LocalBackend
        return LocalBackend()
    if name == "google":
        from .backends.google_backend import GoogleBackend
        return GoogleBackend()
    if name == "mock" and os.environ.get("SPEECH_ENGINE_ALLOW_MOCK") == "1":
        from .backends.mock_backend import MockBackend
        return MockBackend()
    if name == "mock" and os.environ.get("SPEECH_ENGINE_ALLOW_MOCK") == "1":
        from .backends.mock_backend import MockBackend
        return MockBackend()
    raise InvalidInputError(f"unknown backend {name!r} (choose 'local' or 'google')")


@dataclass
class SpeechHandle:
    """Handle for background speech."""

    cancel_event: threading.Event
    _player: Player

    def cancel(self) -> None:
        self.cancel_event.set()

    def wait(self, timeout: float | None = None) -> None:
        self._player.join(timeout)
        if self._player.error and not isinstance(self._player.error, SynthesisCancelled):
            raise self._player.error

    @property
    def done(self) -> bool:
        return self._player.done


class SpeechEngine:
    def __init__(self, backend: Backend, profile: VoiceProfile):
        self.backend = backend
        self.profile = profile
        self._current: SpeechHandle | None = None
        self._lock = threading.Lock()

    # ---- generation ------------------------------------------------------
    def iter_audio(
        self,
        text: str,
        params: SynthParams | None = None,
        cancel: threading.Event | None = None,
        sample_rate: int | None = None,
        progress: ProgressFn | None = None,
    ) -> Iterator[AudioChunk]:
        params = params or SynthParams()
        cancel = cancel or threading.Event()
        prof = self.profile
        chunks = iter_chunks(
            text,
            max_chars=self.backend.max_chunk_chars,
            paragraph_pause_ms=prof.paragraph_pause_ms,
            sentence_pause_ms=prof.sentence_pause_ms,
        )
        # Pull the first chunk eagerly so empty input fails before any backend work.
        first = next(chunks)

        def all_chunks() -> Iterator[Chunk]:
            yield first
            yield from chunks

        done = 0
        for ch in all_chunks():
            if cancel.is_set():
                raise SynthesisCancelled("cancelled")
            audio = self.backend.synthesize(ch.text, prof, params, cancel)
            audio = crossfade_edges(audio)
            if sample_rate:
                audio = resample(audio, sample_rate)
            done += 1
            if progress:
                progress(done, None, ch.text)
            yield audio
            if ch.pause_ms:
                yield silence(ch.pause_ms, audio.sample_rate)

    def synthesize_to_file(self, text, path, fmt="wav", params=None, cancel=None,
                           sample_rate=None, progress=None, bitrate_kbps=None) -> dict:
        """Synthesize to ``path`` (wav / mp3 / pcm). Atomic: the final file appears only after
        a complete, validated write; on any failure nothing is left behind."""
        sink = make_sink(path, fmt, sample_rate, bitrate_kbps)  # fails early on bad settings / missing encoder
        rate = None
        try:
            for c in self.iter_audio(text, params, cancel, sample_rate, progress):
                sink.write(c)
                rate = c.sample_rate
            sink.commit()
        except BaseException:
            sink.abort()
            raise
        info = {"path": str(path), "format": fmt, "bytes": Path(path).stat().st_size}
        if fmt == "mp3":
            info.update(sample_rate=sink.rate, bitrate_kbps=sink.bitrate,
                        duration_s=round(sink.info["duration_s"], 3), mp3_frames=sink.info["frames"])
        else:
            info.update(sample_rate=rate, duration_s=round(sink.samples / rate, 3) if rate else 0.0)
        return info

    # ---- playback --------------------------------------------------------
    def speak_async(self, text, params=None, progress=None) -> SpeechHandle:
        """Start speaking in the background; interrupts any previous speech first."""
        with self._lock:
            self.stop()
            cancel = threading.Event()
            gen = self.iter_audio(text, params, cancel, progress=progress)
            handle = SpeechHandle(cancel, Player(gen, cancel).start())
            self._current = handle
            return handle

    def speak(self, text, params=None, progress=None) -> None:
        """Blocking speak. Ctrl+C cancels cleanly."""
        h = self.speak_async(text, params, progress)
        try:
            while not h.done:
                h.wait(0.2)
            h.wait()
        except KeyboardInterrupt:
            h.cancel()
            h.wait(2)
            raise

    def stop(self) -> None:
        h, self._current = self._current, None
        if h:
            h.cancel()
            h.wait(2)

    def close(self) -> None:
        self.stop()
        self.backend.close()
