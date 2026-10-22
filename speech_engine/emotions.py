"""Emotion presets ("feelings") applied on top of a voice profile.

Kokoro (local backend) has no emotion input, so a feeling is approximated with prosody: a pace multiplier, a pitch
shift (tempo preserved) and scaled pauses. The Google backend receives a natural-language style instruction instead,
which is a much stronger control. ``intensity`` (0..1) scales how far the preset moves away from neutral.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .exceptions import InvalidInputError
from .voice_profiles import VoiceProfile


@dataclass(frozen=True)
class Emotion:
    label: str
    speed: float = 1.0        # multiplier on the profile's pace
    pitch: float = 0.0        # semitones added to the profile's pitch
    pauses: float = 1.0       # multiplier on every pause
    style: str = ""           # google style instruction


EMOTIONS: dict[str, Emotion] = {
    "neutral": Emotion("Neutral"),
    "happy": Emotion("Happy", 1.06, 1.0, 0.9, "Speak happily, with a warm smile in the voice and a bright, upbeat tone."),
    "excited": Emotion("Excited", 1.15, 1.8, 0.75, "Speak with high energy and excitement, fast, bright and enthusiastic."),
    "sad": Emotion("Sad", 0.85, -1.0, 1.35, "Speak sadly and softly, slowly, with a heavy, downcast tone."),
    "angry": Emotion("Angry", 1.08, -0.5, 0.8, "Speak angrily: tense, forceful and clipped, with sharp emphasis."),
    "calm": Emotion("Calm", 0.9, -0.5, 1.25, "Speak calmly and soothingly, slow, gentle and unhurried."),
    "serious": Emotion("Serious", 0.95, -1.0, 1.1, "Speak in a serious, measured, authoritative tone."),
    "fearful": Emotion("Fearful", 1.12, 1.2, 0.9, "Speak nervously and fearfully, with a tense, slightly shaky urgency."),
    "tender": Emotion("Tender", 0.9, 0.3, 1.2, "Speak tenderly and lovingly, soft, intimate and warm."),
}

DEFAULT = "neutral"


def emotion_list() -> list[dict]:
    return [{"name": n, "label": e.label} for n, e in EMOTIONS.items()]


def apply_emotion(profile: VoiceProfile, name: str | None, intensity: float | None = None) -> VoiceProfile:
    """Return ``profile`` shifted towards the named emotion. ``None``/neutral/zero intensity returns it unchanged."""
    name = (name or DEFAULT).lower()
    if name not in EMOTIONS:
        raise InvalidInputError(f"unknown emotion {name!r}; available: {', '.join(EMOTIONS)}")
    k = 1.0 if intensity is None else intensity
    if isinstance(k, bool) or not isinstance(k, (int, float)) or not 0.0 <= k <= 1.0:
        raise InvalidInputError("emotion intensity must be a number between 0 and 1")
    if name == DEFAULT or k == 0:
        return profile
    e = EMOTIONS[name]
    style = profile.google_style
    if e.style:
        style = (style + " " if style else "") + e.style + ("" if k >= 0.67 else " Keep it subtle.")
    p = replace(
        profile,
        speed=min(2.0, max(0.5, profile.speed * (1 + (e.speed - 1) * k))),
        pitch_semitones=min(8.0, max(-4.0, profile.pitch_semitones + e.pitch * k)),
        sentence_pause=profile.sentence_pause * (1 + (e.pauses - 1) * k),
        clause_pause=profile.clause_pause * (1 + (e.pauses - 1) * k),
        paragraph_pause_ms=round(profile.paragraph_pause_ms * (1 + (e.pauses - 1) * k)),
        sentence_pause_ms=round(profile.sentence_pause_ms * (1 + (e.pauses - 1) * k)),
        google_style=style,
    )
    p.validate()
    return p
