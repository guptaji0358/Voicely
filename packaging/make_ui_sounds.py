"""Synthesise the small UI sound effects into gui/assets/sounds/*.wav (pure standard library, no downloads).

    python packaging/make_ui_sounds.py

The files are compiled into the app (gui/resources/resources.qrc). Re-run only when you want to change the sounds.
All sounds are short, quiet and use soft attacks/decays so they never startle.
"""

from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

RATE = 44100
OUT = Path(__file__).resolve().parents[1] / "gui" / "assets" / "sounds"


def env(n: int, attack: float, decay_pow: float) -> list[float]:
    """Attack ramp (seconds) then a power-law decay over the rest of the sound."""
    a = max(1, int(attack * RATE))
    out = []
    for i in range(n):
        up = min(1.0, i / a)
        down = (1.0 - i / n) ** decay_pow
        out.append(up * down)
    return out


def tone(freq: float, dur: float, amp: float = 0.4, attack: float = 0.004, decay: float = 2.5, harm: float = 0.25,
         glide: float = 0.0) -> list[float]:
    n = int(dur * RATE)
    e = env(n, attack, decay)
    s, phase = [], 0.0
    for i in range(n):
        f = freq * (1.0 + glide * i / n)
        phase += 2 * math.pi * f / RATE
        s.append(amp * e[i] * (math.sin(phase) + harm * math.sin(2 * phase) + harm * 0.4 * math.sin(3 * phase)))
    return s


def mix(*parts: tuple[float, list[float]]) -> list[float]:
    """parts = (start_seconds, samples)"""
    n = max(int(t * RATE) + len(x) for t, x in parts)
    out = [0.0] * n
    for t, x in parts:
        o = int(t * RATE)
        for i, v in enumerate(x):
            out[o + i] += v
    return out


def noise_burst(dur: float, amp: float, lp: float = 0.35) -> list[float]:
    rnd = random.Random(7)
    n = int(dur * RATE)
    e = env(n, 0.0005, 4.0)
    y, out = 0.0, []
    for i in range(n):
        y += lp * (rnd.uniform(-1, 1) - y)          # one-pole low-pass: a soft "tick" rather than a hiss
        out.append(amp * e[i] * y)
    return out


def save(name: str, samples: list[float]) -> None:
    peak = max(1e-9, max(abs(v) for v in samples))
    scale = min(1.0, 0.85 / peak)
    OUT.mkdir(parents=True, exist_ok=True)
    with wave.open(str(OUT / f"{name}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, v * scale)) * 32767)) for v in samples))
    print(f"{name}.wav  {len(samples) / RATE * 1000:.0f} ms")


def main() -> None:
    save("click", mix((0, tone(1500, 0.045, 0.5, 0.0015, 3.0, 0.2)), (0, noise_burst(0.02, 0.5))))
    save("toggle", mix((0, tone(780, 0.07, 0.45, 0.003, 2.2)), (0.045, tone(1170, 0.08, 0.35, 0.003, 2.2))))
    save("generate", mix((0, tone(520, 0.11, 0.4, 0.006, 1.8)), (0.07, tone(780, 0.14, 0.4, 0.006, 1.8)),
                         (0.14, tone(1040, 0.22, 0.35, 0.006, 2.4))))
    notes = [1046.5, 1318.5, 1568.0, 2093.0]                 # C6 E6 G6 C7: a bright, short arpeggio
    save("success", mix(*[(0.075 * i, tone(f, 0.45 - 0.05 * i, 0.32, 0.004, 3.0, 0.3)) for i, f in enumerate(notes)]))
    save("error", mix((0, tone(311, 0.16, 0.5, 0.004, 1.6, 0.35)), (0.13, tone(233, 0.26, 0.5, 0.004, 2.0, 0.35))))
    save("dragover", tone(420, 0.18, 0.35, 0.03, 1.2, 0.2, glide=1.2))
    save("drop", mix((0, tone(640, 0.14, 0.5, 0.002, 2.4, 0.3, glide=-0.55)), (0, noise_burst(0.03, 0.4)),
                     (0.05, tone(1280, 0.2, 0.22, 0.003, 3.0, 0.2))))


if __name__ == "__main__":
    main()
