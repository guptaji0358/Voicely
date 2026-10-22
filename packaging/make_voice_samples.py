"""Render one permanent preview clip per built-in voice into speech_engine/voice_samples/<name>.mp3.
The app only ever plays these stored files for previews - it never calls a cloud API for them.

The GUI plays these stored files for the speaker button instead of synthesizing at run-time.
Re-run only when a profile's blend/speed/pitch or SAMPLE_TEXT changes (existing files are kept unless --force):

    python packaging/make_voice_samples.py [--force] [--only girl_bella,boy_liam]
    python packaging/make_voice_samples.py --convert-wav     # turn existing <name>.wav samples into .mp3 (no synthesis)

Samples are made with the local (Kokoro) backend at the profile's own settings.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from speech_engine.backends.base import SynthParams  # noqa: E402
from speech_engine.engine import SpeechEngine, make_backend  # noqa: E402
from speech_engine.voice_profiles import load_profiles  # noqa: E402

SAMPLE_TEXT = "Hello, this is a short sample of my voice. I hope you enjoy how I sound."
OUT_DIR = ROOT / "speech_engine" / "voice_samples"


def convert_wavs() -> int:
    import wave

    import lameenc

    for w in sorted(OUT_DIR.glob("*.wav")):
        out = w.with_suffix(".mp3")
        with wave.open(str(w), "rb") as f:
            rate, ch, pcm = f.getframerate(), f.getnchannels(), f.readframes(f.getnframes())
        enc = lameenc.Encoder()
        enc.set_bit_rate(96)
        enc.set_in_sample_rate(rate)
        enc.set_out_sample_rate(rate)
        enc.set_channels(ch)
        enc.set_quality(2)
        out.write_bytes(enc.encode(pcm) + enc.flush())
        w.unlink()
        print(f"mp3    {w.stem}  {out.stat().st_size // 1024} KB")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="overwrite existing samples")
    ap.add_argument("--only", help="comma-separated voice names")
    ap.add_argument("--convert-wav", action="store_true", help="convert existing .wav samples to .mp3, then delete the .wav")
    args = ap.parse_args()
    if args.convert_wav:
        return convert_wavs()

    backend = make_backend("local")
    st = backend.status()
    if not st.available:
        print("local backend unavailable: " + "; ".join(st.problems), file=sys.stderr)
        return 2
    OUT_DIR.mkdir(exist_ok=True)
    profiles = load_profiles()
    names = args.only.split(",") if args.only else sorted(profiles)
    failed = 0
    for n in names:
        out = OUT_DIR / f"{n}.mp3"
        if out.exists() and not args.force:
            print(f"skip   {n} (exists)")
            continue
        eng = SpeechEngine(backend, profiles[n])
        try:
            info = eng.synthesize_to_file(SAMPLE_TEXT, out, "mp3", SynthParams())
            print(f"ok     {n}  {info.get('duration_s', 0):.1f}s  {out.stat().st_size // 1024} KB")
        except Exception as exc:  # keep going; report at the end
            failed += 1
            print(f"FAILED {n}: {exc}", file=sys.stderr)
        finally:
            eng.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
