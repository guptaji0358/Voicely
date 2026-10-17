"""Render one permanent preview clip per built-in voice into speech_engine/voice_samples/<name>.wav.

The GUI plays these stored files for the speaker button instead of synthesizing at run-time.
Re-run only when a profile's blend/speed/pitch or SAMPLE_TEXT changes (existing files are kept unless --force):

    python packaging/make_voice_samples.py [--force] [--only girl_bella,boy_liam]

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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="overwrite existing samples")
    ap.add_argument("--only", help="comma-separated voice names")
    args = ap.parse_args()

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
        out = OUT_DIR / f"{n}.wav"
        if out.exists() and not args.force:
            print(f"skip   {n} (exists)")
            continue
        eng = SpeechEngine(backend, profiles[n])
        try:
            info = eng.synthesize_to_file(SAMPLE_TEXT, out, "wav", SynthParams())
            print(f"ok     {n}  {info.get('duration_s', 0):.1f}s  {out.stat().st_size // 1024} KB")
        except Exception as exc:  # keep going; report at the end
            failed += 1
            print(f"FAILED {n}: {exc}", file=sys.stderr)
        finally:
            eng.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
