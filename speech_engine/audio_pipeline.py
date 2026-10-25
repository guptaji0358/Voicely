"""PCM utilities, WAV decoding, file writing and cancellable RAM-only playback.

All audio inside the package is mono int16 LE PCM. Compressed bytes (MP3, etc.)
are never treated as PCM: ``decode_audio_bytes`` accepts only WAV or an explicit
raw-PCM declaration and raises otherwise.
"""

from __future__ import annotations

import errno
import io
import os
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np

from .backends.base import AudioChunk
from .exceptions import AudioOutputError, InvalidInputError, SynthesisCancelled, SynthesisError

_COMPRESSED_MAGIC = (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"OggS", b"fLaC")


def float_to_pcm16(samples: np.ndarray) -> bytes:
    a = np.asarray(samples, dtype=np.float32)
    if a.ndim > 1:
        a = a.mean(axis=1)
    a = np.nan_to_num(a, nan=0.0, posinf=1.0, neginf=-1.0)
    return (np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def decode_audio_bytes(data: bytes, mime: str | None = None, sample_rate: int | None = None) -> AudioChunk:
    """Decode WAV (RIFF) bytes, or raw L16 PCM when ``mime`` says audio/l16 and a rate is known."""
    if not data:
        raise SynthesisError("backend returned no audio data")
    if data[:4] == b"RIFF":
        try:
            with wave.open(io.BytesIO(data), "rb") as w:
                if w.getsampwidth() != 2:
                    raise SynthesisError(f"unsupported WAV sample width {w.getsampwidth()*8} bit")
                ch, rate = w.getnchannels(), w.getframerate()
                pcm = w.readframes(w.getnframes())
        except (wave.Error, EOFError) as exc:
            raise SynthesisError(f"invalid WAV data from backend: {exc}") from exc
        if ch > 1:
            arr = np.frombuffer(pcm, "<i2").reshape(-1, ch).mean(axis=1).astype("<i2")
            pcm = arr.tobytes()
        return AudioChunk(pcm, rate)
    if data.startswith(_COMPRESSED_MAGIC):
        raise SynthesisError("backend returned compressed audio; refusing to play it as PCM")
    if mime and mime.lower().startswith("audio/l16") and sample_rate:
        if len(data) % 2:
            data = data[:-1]
        return AudioChunk(data, sample_rate)
    raise SynthesisError(f"unrecognised audio payload (mime={mime!r}); not treating it as PCM")


def silence(ms: int, sample_rate: int) -> AudioChunk:
    return AudioChunk(b"\x00\x00" * int(sample_rate * ms / 1000), sample_rate)


def resample(chunk: AudioChunk, target_rate: int) -> AudioChunk:
    """Resample mono PCM16. Polyphase (scipy) when available, else linear interpolation."""
    if target_rate == chunk.sample_rate or not chunk.pcm:
        return AudioChunk(chunk.pcm, target_rate) if not chunk.pcm else chunk
    if target_rate < 8000 or target_rate > 96000:
        raise InvalidInputError("sample rate must be between 8000 and 96000")
    x = np.frombuffer(chunk.pcm, "<i2").astype(np.float32)
    try:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(target_rate, chunk.sample_rate)
        y = resample_poly(x, target_rate // g, chunk.sample_rate // g)
    except ImportError:
        n = max(1, int(round(len(x) * target_rate / chunk.sample_rate)))
        y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return AudioChunk(np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes(), target_rate)


_VOCODER_BELOW = 0.8   # WSOLA repeats each pitch segment several times when slowing this much, which sounds buzzy


def _phase_vocoder(x: np.ndarray, speed: float, rate: int) -> bytes:
    """Pitch-preserving slow-down with an STFT phase vocoder and identity phase locking (smooth, no repeated segments)."""
    n = int(rate * 0.064) // 8 * 8
    hop_s = n // 8                                   # synthesis hop (87.5 % overlap)
    hop_a = hop_s * speed                            # analysis hop
    win = np.hanning(n + 1)[:n].astype(np.float32)
    x = np.pad(x, (n, n))
    frames = int((len(x) - 2 * n) / hop_a) + 1
    starts = (np.arange(frames) * hop_a).astype(np.int64)
    spec = np.fft.rfft(np.stack([x[s:s + n] * win for s in starts]), axis=1)
    mag, ang = np.abs(spec), np.angle(spec)
    omega = 2 * np.pi * np.arange(n // 2 + 1) * hop_a / n   # expected phase advance per analysis hop
    out_phase = np.empty_like(ang)
    out_phase[0] = ang[0]
    for i in range(1, frames):
        dev = ang[i] - ang[i - 1] - omega
        dev -= 2 * np.pi * np.round(dev / (2 * np.pi))
        inst = (omega + dev) / hop_a                         # instantaneous frequency (rad / sample)
        # identity phase locking: bins around a spectral peak follow the peak's phase rotation
        m = mag[i]
        peak = np.zeros(len(m), bool)
        peak[2:-2] = (m[2:-2] >= m[1:-3]) & (m[2:-2] >= m[3:-1]) & (m[2:-2] >= m[:-4]) & (m[2:-2] >= m[4:])
        idx = np.flatnonzero(peak)
        if len(idx):
            owner = idx[np.clip(np.searchsorted((idx[:-1] + idx[1:]) / 2, np.arange(len(m))), 0, len(idx) - 1)]
        else:
            owner = np.arange(len(m))
        adv = out_phase[i - 1] + inst * hop_s
        locked = adv[owner] + (ang[i] - ang[i][owner])
        out_phase[i] = np.where(np.isin(np.arange(len(m)), idx), adv, locked) if len(idx) else adv
    frames_t = np.fft.irfft(mag * np.exp(1j * out_phase), n, axis=1).astype(np.float32) * win
    y = np.zeros(frames * hop_s + n, np.float32)
    norm = np.zeros_like(y)
    w2 = win * win
    for i in range(frames):
        o = i * hop_s
        y[o:o + n] += frames_t[i]
        norm[o:o + n] += w2
    y = y / np.maximum(norm, 1e-3)
    y = y[n:len(y) - n] if len(y) > 2 * n else y
    return np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes()


def time_stretch(chunk: AudioChunk, speed: float, frame_ms: int = 40) -> AudioChunk:
    """Change tempo without changing pitch (WSOLA). ``speed`` > 1 is faster. Used for backends with no native speed.

    75 % overlapped Hann frames with a normalised cross-correlation search keep voiced sounds smooth; with 50 % overlap
    and a plain dot product slowed speech sounds buzzy / robotic.
    """
    if not chunk.pcm or abs(speed - 1.0) < 0.01:
        return chunk
    if not 0.25 <= speed <= 4.0:
        raise InvalidInputError("speed must be between 0.25 and 4.0")
    x = np.frombuffer(chunk.pcm, "<i2").astype(np.float32)
    if speed < _VOCODER_BELOW:
        return AudioChunk(_phase_vocoder(x, speed, chunk.sample_rate), chunk.sample_rate)
    n = int(chunk.sample_rate * frame_ms / 1000) // 4 * 4
    hop_out = n // 4
    hop_in = hop_out * speed
    tol = n // 3
    if len(x) < n * 2:
        return chunk
    win = np.hanning(n + 1)[:n].astype(np.float32)          # periodic Hann: overlap-adds to a constant at 75 %
    frames = int((len(x) - n - tol) / hop_in) + 1
    out = np.zeros(frames * hop_out + n, np.float32)
    norm = np.zeros_like(out)
    out[:n] += x[:n] * win
    norm[:n] += win
    o, pos = 0, 0
    for i in range(1, frames):
        # what naturally follows the previously chosen segment is the best-matching target
        target = x[pos + hop_out: pos + hop_out + n]
        centre = int(i * hop_in)
        lo, hi = max(0, centre - tol), min(len(x) - n, centre + tol)
        if hi <= lo or len(target) < n:
            break
        cand = np.lib.stride_tricks.sliding_window_view(x[lo:hi + n], n)[::2]
        energy = np.einsum("ij,ij->i", cand, cand) + 1e-3 * n
        k = int(np.argmax((cand @ target) / np.sqrt(energy))) * 2
        pos = lo + k
        o = i * hop_out
        out[o:o + n] += x[pos: pos + n] * win
        norm[o:o + n] += win
    end = o + n
    y = out[:end] / np.maximum(norm[:end], 1e-3)
    return AudioChunk(np.clip(np.rint(y), -32768, 32767).astype("<i2").tobytes(), chunk.sample_rate)


def crossfade_edges(chunk: AudioChunk, ms: int = 6) -> AudioChunk:
    """Tiny fade in/out to prevent clicks at chunk joins."""
    x = np.frombuffer(chunk.pcm, "<i2").astype(np.float32)
    n = min(int(chunk.sample_rate * ms / 1000), len(x) // 2)
    if n > 1:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        x[:n] *= ramp
        x[-n:] *= ramp[::-1]
    return AudioChunk(x.astype("<i2").tobytes(), chunk.sample_rate)


class _AtomicSink:
    """Writes to a hidden temp file beside the target; renames only on a validated commit.

    A failure at any point (synthesis, encoding, disk full, Ctrl+C) calls ``abort()``,
    which deletes the temp file, so a partial export is never left at the final path.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.part")
        self._done = False
        self.samples = 0  # mono samples written (at the output rate)

    # hooks -------------------------------------------------------------
    def _write(self, chunk: AudioChunk) -> None: raise NotImplementedError
    def _finish(self) -> None: """flush encoders and close handles"""
    def _discard(self) -> None: """close handles without raising"""
    def _validate(self) -> None: """raise SynthesisError if the temp file is unusable"""

    # api ---------------------------------------------------------------
    def write(self, chunk: AudioChunk) -> None:
        try:
            self._write(chunk)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise AudioOutputError(f"insufficient disk space writing {self.path}") from exc
            raise AudioOutputError(f"cannot write {self.path}: {exc}") from exc
        except wave.Error as exc:
            raise AudioOutputError(f"cannot write {self.path}: {exc}") from exc

    def commit(self) -> None:
        if self._done:
            return
        try:
            self._finish()
            if self.samples == 0 or not self.tmp.is_file():
                raise SynthesisError("no audio was produced; nothing saved")
            self._validate()
            self._replace_with_retry()
        except OSError as exc:
            self.abort()
            msg = "insufficient disk space" if exc.errno == errno.ENOSPC else str(exc)
            if isinstance(exc, PermissionError):
                msg += " (the file is probably open in a player/editor or locked by antivirus/sync; close it and retry)"
            raise AudioOutputError(f"cannot finish {self.path}: {msg}") from exc
        except BaseException:
            self.abort()
            raise
        self._done = True

    def _replace_with_retry(self, attempts: int = 10, delay: float = 0.3) -> None:
        # Windows briefly denies the rename while antivirus/indexer/sync scans the new
        # file or a player holds the target; retry before giving up.
        for i in range(attempts):
            try:
                os.replace(self.tmp, self.path)
                return
            except PermissionError:
                if i == attempts - 1:
                    raise
                time.sleep(delay)

    def abort(self) -> None:
        self._done = True
        try:
            self._discard()
        except Exception:
            pass
        try:
            self.tmp.unlink(missing_ok=True)
        except OSError:
            pass

    close = commit  # backwards-compatible name


class WavFileSink(_AtomicSink):
    """Streams chunks to a WAV file so long documents never sit fully in RAM."""

    def __init__(self, path):
        super().__init__(path)
        self._w: wave.Wave_write | None = None

    @property
    def frames(self) -> int:
        return self.samples

    def _write(self, chunk):
        if self._w is None:
            self._w = wave.open(str(self.tmp), "wb")
            self._w.setnchannels(1)
            self._w.setsampwidth(2)
            self._w.setframerate(chunk.sample_rate)
        self._w.writeframes(chunk.pcm)
        self.samples += len(chunk.pcm) // 2

    def _finish(self):
        if self._w is not None:
            self._w.close(); self._w = None

    def _discard(self):
        if self._w is not None:
            self._w.close(); self._w = None


class RawPcmSink(_AtomicSink):
    def __init__(self, path):
        super().__init__(path)
        self._f = None

    def _write(self, chunk):
        if self._f is None:
            self._f = open(self.tmp, "wb")
        self._f.write(chunk.pcm)
        self.samples += len(chunk.pcm) // 2

    def _finish(self):
        if self._f is not None:
            self._f.close(); self._f = None

    _discard = _finish


def make_sink(path: str | Path, fmt: str, sample_rate: int | None = None, bitrate_kbps: int | None = None):
    if fmt == "wav":
        return WavFileSink(path)
    if fmt == "pcm":
        return RawPcmSink(path)
    if fmt == "mp3":
        from .encoders import Mp3FileSink
        return Mp3FileSink(path, sample_rate, bitrate_kbps)
    raise InvalidInputError(f"unsupported output format {fmt!r} (supported: wav, mp3, pcm)")


class Player:
    """Plays chunks from an iterator on a worker thread via sounddevice (RAM only).

    The iterator is consumed lazily, so synthesis and playback overlap while
    memory stays bounded by ``prefetch`` chunks. ``cancel()`` stops promptly.
    """

    def __init__(self, chunks: Iterator[AudioChunk], cancel: threading.Event, prefetch: int = 2):
        self._chunks = chunks
        self.cancel_event = cancel
        self._q: queue.Queue = queue.Queue(maxsize=prefetch)
        self.error: BaseException | None = None
        self._producer = threading.Thread(target=self._produce, name="speech-synth", daemon=True)
        self._consumer = threading.Thread(target=self._consume, name="speech-play", daemon=True)

    def start(self) -> "Player":
        self._producer.start()
        self._consumer.start()
        return self

    def _put(self, item) -> bool:
        while not self.cancel_event.is_set():
            try:
                self._q.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _produce(self) -> None:
        try:
            for c in self._chunks:
                if not self._put(c):
                    return
        except SynthesisCancelled:
            pass
        except BaseException as exc:  # noqa: BLE001 - surfaced via .error
            self.error = exc
        finally:
            close = getattr(self._chunks, "close", None)
            if close:
                close()
            self._put(None)

    def _consume(self) -> None:
        stream = None
        try:
            try:
                import sounddevice as sd
            except ImportError as exc:
                raise AudioOutputError("playback needs the 'sounddevice' package (pip install sounddevice)") from exc
            while not self.cancel_event.is_set():
                try:
                    item = self._q.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    break
                try:
                    if stream is None or stream.samplerate != item.sample_rate:
                        if stream is not None:
                            stream.stop(); stream.close()
                        stream = sd.RawOutputStream(samplerate=item.sample_rate, channels=1, dtype="int16")
                        stream.start()
                except Exception as exc:  # sounddevice raises PortAudioError etc.
                    raise AudioOutputError(f"cannot open audio output device: {exc}") from exc
                # write in small slices so cancel() takes effect within ~100 ms
                step = item.sample_rate // 10 * 2
                for i in range(0, len(item.pcm), step):
                    if self.cancel_event.is_set():
                        break
                    stream.write(item.pcm[i : i + step])
        except BaseException as exc:  # noqa: BLE001
            if self.error is None:
                self.error = exc
            self.cancel_event.set()
        finally:
            if stream is not None:
                try:
                    if self.cancel_event.is_set():
                        stream.abort()
                    else:
                        stream.stop()
                finally:
                    stream.close()
            # drain so the producer is never left blocked
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break

    def join(self, timeout: float | None = None) -> None:
        self._producer.join(timeout)
        self._consumer.join(timeout)

    @property
    def done(self) -> bool:
        return not self._producer.is_alive() and not self._consumer.is_alive()


def collect(chunks: Iterable[AudioChunk]) -> AudioChunk:
    """Concatenate (tests / small inputs only)."""
    parts, rate = [], None
    for c in chunks:
        rate = rate or c.sample_rate
        parts.append(c.pcm)
    if rate is None:
        raise SynthesisError("no audio produced")
    return AudioChunk(b"".join(parts), rate)
