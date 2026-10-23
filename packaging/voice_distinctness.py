"""Report how close each built-in voice is to its nearest neighbour (pitch + long-term spectrum).

    python packaging/voice_distinctness.py [name ...]

Distance = sqrt((pitch difference in semitones)^2 + (RMS dB difference of a 16-band spectrum)^2 / 4).
Anything under ~2.5 is likely to be heard as 'the same voice'.
"""
import math
import sys
import threading
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speech_engine.backends.base import SynthParams  # noqa: E402
from speech_engine.backends.local_backend import LocalBackend  # noqa: E402
from speech_engine.voice_profiles import load_profiles  # noqa: E402

TEXT = "Hello, this is a short sample of my voice. I hope you enjoy how I sound."


def features(pcm: bytes, sr: int):
    x = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
    n = int(sr * 0.04)
    f0s = []
    for s in range(0, len(x) - n, n // 2):
        f = x[s:s + n] - x[s:s + n].mean()
        if np.sqrt((f ** 2).mean()) < 0.02:
            continue
        ac = np.correlate(f, f, "full")[n - 1:]
        lo, hi = int(sr / 400), int(sr / 70)
        k = lo + int(np.argmax(ac[lo:hi]))
        if ac[k] > 0.5 * ac[0]:
            f0s.append(sr / k)
    sp = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    fr = np.fft.rfftfreq(len(x), 1 / sr)
    edges = np.geomspace(100, 8000, 17)
    band = np.array([sp[(fr >= a) & (fr < b)].sum() for a, b in zip(edges[:-1], edges[1:])])
    return 12 * math.log2(float(np.median(f0s)) / 100) if f0s else 0.0, 10 * np.log10(band / band.sum() + 1e-12)


def main():
    profiles = load_profiles()
    names = sys.argv[1:] or sorted(profiles)
    be, ev, feat = LocalBackend(), threading.Event(), {}
    for n in names:
        c = be.synthesize(TEXT, profiles[n], SynthParams(), ev)
        feat[n] = features(c.pcm, c.sample_rate)
    def dist(a, b):
        return math.hypot(feat[a][0] - feat[b][0], float(np.sqrt(((feat[a][1] - feat[b][1]) ** 2).mean())) / 2)
    rows = []
    for a in names:
        b = min((m for m in names if m != a), key=lambda m: dist(a, m))
        rows.append((dist(a, b), a, b))
    for d, a, b in sorted(rows):
        print(f"{a:15s} nearest {b:15s} {d:5.2f}")
    print("min:", round(min(r[0] for r in rows), 2), " median:", round(float(np.median([r[0] for r in rows])), 2))


if __name__ == "__main__":
    main()
