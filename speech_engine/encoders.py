"""MP3 export: streaming LAME encoding (``lameenc``), format/bitrate rules, and validation.

Encoder choice: ``lameenc`` (Python bindings that bundle LAME, an in-process streaming
encoder). Chosen over FFmpeg because it needs no external binary on PATH, takes PCM from
RAM and emits MP3 frames incrementally (bounded memory, no intermediate WAV). FFmpeg is
not used or required.

Rules enforced here:
  * output is always genuine MPEG audio Layer III, produced by encoding PCM - never a
    renamed WAV, never compressed bytes treated as PCM;
  * bitrate must be valid for the chosen sample rate (MPEG-1: 32-320k at 32/44.1/48 kHz;
    MPEG-2: 8-160k at 16/22.05/24 kHz; MPEG-2.5: 8-64k at 8/11.025/12 kHz);
  * default sample rate is 44.1 kHz (speech is resampled up from the 24 kHz synthesis rate),
    which is universally playable and allows the 192k default.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .audio_pipeline import _AtomicSink, resample
from .backends.base import AudioChunk
from .exceptions import EncoderUnavailableError, InvalidInputError, SynthesisError

DEFAULT_MP3_RATE = 44100
DEFAULT_MP3_BITRATE = 192

_MPEG1_RATES = (32000, 44100, 48000)
_MPEG2_RATES = (16000, 22050, 24000)
_MPEG25_RATES = (8000, 11025, 12000)
_MPEG1_BR = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_MPEG2_BR = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)
_MPEG25_BR = tuple(b for b in _MPEG2_BR if b <= 64)

INSTALL_HELP = (
    "MP3 export needs the 'lameenc' package.\n"
    "  Install:  pip install lameenc\n"
    "  (it ships prebuilt wheels with LAME included; no FFmpeg needed)\n"
    "  Check:    python -m speech_engine.cli test"
)


def allowed_bitrates(sample_rate: int) -> tuple[int, ...]:
    if sample_rate in _MPEG1_RATES:
        return _MPEG1_BR
    if sample_rate in _MPEG2_RATES:
        return _MPEG2_BR
    if sample_rate in _MPEG25_RATES:
        return _MPEG25_BR
    raise InvalidInputError(
        "MP3 supports sample rates 8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000 "
        f"(got {sample_rate})"
    )


def parse_bitrate(value: str | int | None) -> int | None:
    """'192k' / '192' / 192 -> 192; None stays None."""
    if value is None:
        return None
    s = str(value).strip().lower().removesuffix("kbps").removesuffix("k")
    if not s.isdigit():
        raise InvalidInputError(f"invalid bitrate {value!r}; use e.g. 128k, 192k, 320k")
    return int(s)


def resolve_mp3_settings(sample_rate: int | None, bitrate_kbps: int | None) -> tuple[int, int]:
    rate = sample_rate or DEFAULT_MP3_RATE
    allowed = allowed_bitrates(rate)
    if bitrate_kbps is None:
        bitrate_kbps = max(b for b in allowed if b <= DEFAULT_MP3_BITRATE)
    elif bitrate_kbps not in allowed:
        raise InvalidInputError(
            f"{bitrate_kbps}k is not a valid MP3 bitrate at {rate} Hz; choose one of: "
            + ", ".join(f"{b}k" for b in allowed)
        )
    return rate, bitrate_kbps


def encoder_status() -> dict:
    try:
        import lameenc  # noqa: F401
        return {"mp3": True, "encoder": "lameenc (LAME)", "problem": None}
    except ImportError:
        return {"mp3": False, "encoder": None, "problem": INSTALL_HELP}


class Mp3FileSink(_AtomicSink):
    """Streams PCM chunks through LAME to a temp file; commit validates then renames."""

    def __init__(self, path, sample_rate: int | None = None, bitrate_kbps: int | None = None):
        super().__init__(path)
        self.rate, self.bitrate = resolve_mp3_settings(sample_rate, bitrate_kbps)
        try:
            import lameenc
        except ImportError as exc:
            raise EncoderUnavailableError(INSTALL_HELP) from exc
        self._enc = lameenc.Encoder()
        self._enc.set_bit_rate(self.bitrate)
        self._enc.set_in_sample_rate(self.rate)
        self._enc.set_out_sample_rate(self.rate)
        self._enc.set_channels(1)
        self._enc.set_quality(2)
        self._f = None
        self.info: dict = {}

    def _write(self, chunk: AudioChunk) -> None:
        if chunk.sample_rate != self.rate:
            chunk = resample(chunk, self.rate)
        if not chunk.pcm:
            return
        try:
            data = self._enc.encode(chunk.pcm)  # int16 LE mono bytes
        except Exception as exc:
            raise SynthesisError(f"MP3 encoding failed: {exc}") from exc
        if self._f is None:
            self._f = open(self.tmp, "wb")
        self._f.write(data)
        self.samples += len(chunk.pcm) // 2

    def _finish(self) -> None:
        if self.samples == 0:
            return
        try:
            tail = self._enc.flush()
        except Exception as exc:
            raise SynthesisError(f"MP3 encoding failed while finishing: {exc}") from exc
        self._f.write(tail)
        self._f.close()
        self._f = None

    def _discard(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None

    def _validate(self) -> None:
        self.info = validate_mp3(self.tmp)
        if self.info["sample_rate"] != self.rate:
            raise SynthesisError("encoded MP3 has an unexpected sample rate")
        expected = self.samples / self.rate
        if abs(self.info["duration_s"] - expected) > 0.15:
            raise SynthesisError(
                f"encoded MP3 duration {self.info['duration_s']:.2f}s differs from expected {expected:.2f}s"
            )
        self.info["bitrate_kbps"] = self.bitrate


# ---------------------------------------------------------------- validation
def validate_mp3(path: str | Path) -> dict:
    """Walk every MPEG Layer III frame header (independent of the encoder).

    Raises SynthesisError unless the whole file is a contiguous run of valid frames
    (optionally with ID3v2 at the start and ID3v1 at the end).
    """
    data = Path(path).read_bytes()
    pos = 0
    if data[:3] == b"ID3" and len(data) >= 10:
        size = (data[6] << 21) | (data[7] << 14) | (data[8] << 7) | data[9]
        pos = 10 + size
    end = len(data) - 128 if data[-128:-125] == b"TAG" else len(data)
    frames, samples, rate, first_br = 0, 0, None, None
    while pos + 4 <= end:
        h = int.from_bytes(data[pos:pos + 4], "big")
        if (h >> 21) & 0x7FF != 0x7FF:
            break
        ver, layer = (h >> 19) & 3, (h >> 17) & 3
        br_i, sr_i, pad = (h >> 12) & 0xF, (h >> 10) & 3, (h >> 9) & 1
        if ver == 1 or layer != 1 or br_i in (0, 15) or sr_i == 3:
            break
        mpeg1 = ver == 3
        br = (_MPEG1_BR if mpeg1 else _MPEG2_BR)[br_i - 1]
        sr = ((44100, 48000, 32000) if mpeg1 else (22050, 24000, 16000) if ver == 2 else (11025, 12000, 8000))[sr_i]
        length = (144 * br * 1000 // sr if mpeg1 else 72 * br * 1000 // sr) + pad
        if length < 4 or pos + length > len(data):
            break
        rate = rate or sr
        first_br = first_br or br
        if sr != rate:
            raise SynthesisError("MP3 sample rate changes mid-stream")
        frames += 1
        samples += 1152 if mpeg1 else 576
        pos += length
    if frames == 0:
        raise SynthesisError("output is not valid MP3 (no MPEG audio frames found)")
    if pos < end - 4 and any(data[pos:end]):
        raise SynthesisError(f"MP3 is corrupt or truncated near byte {pos}")
    return {"frames": frames, "sample_rate": rate, "bitrate_kbps": first_br,
            "duration_s": samples / rate, "bytes": len(data)}


# ---------------------------------------------------------------- format rules
_EXT_FORMAT = {".wav": "wav", ".mp3": "mp3", ".pcm": "pcm", ".raw": "pcm"}


def resolve_format(output: str | Path, fmt: str | None) -> str:
    """Explicit --format wins but must agree with a known extension; otherwise infer."""
    ext = Path(output).suffix.lower()
    inferred = _EXT_FORMAT.get(ext)
    if fmt is None:
        if inferred is None:
            raise InvalidInputError(
                f"cannot infer the format from {str(output)!r}; use .wav/.mp3/.pcm or pass --format"
            )
        return inferred
    if fmt not in ("wav", "mp3", "pcm"):
        raise InvalidInputError(f"unsupported format {fmt!r} (supported: wav, mp3, pcm)")
    if inferred is not None and inferred != fmt:
        raise InvalidInputError(
            f"--format {fmt} does not match the file extension {ext!r}; rename the output or fix --format"
        )
    return fmt
