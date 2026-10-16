"""Voice profiles: configurable data, separate from any synthesis code.

IMPORTANT - what ``original`` is:
  A *configuration* of existing, licensed building blocks. It is NOT a newly
  trained acoustic model.
    * local backend  : a weighted blend of Kokoro-82M voice style vectors plus
                       speed/pause settings. Blending produces a timbre that
                       is not any single shipped voice, but it is still derived
                       from Kokoro's voices.
    * google backend : one prebuilt Gemini voice plus a natural-language
                       ``style`` instruction. The underlying voice is Google's.
  A genuinely custom-trained voice is a separate project; see
  ``backends/base.py`` (CustomModelBackend note) and the README.

Profiles can be overridden/extended by a JSON file (``--profiles-file`` or
``SPEECH_ENGINE_PROFILES``) with the same field names as ``VoiceProfile``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from .exceptions import ConfigurationError, InvalidInputError


@dataclass(frozen=True)
class VoiceProfile:
    name: str
    description: str = ""
    # --- design intent (documentation only; not an acoustic guarantee) ---
    character: str = ""
    # --- local (Kokoro) controls ---
    local_blend: dict[str, float] = field(default_factory=dict)  # voice -> weight
    local_lang: str = "en-us"
    speed: float = 1.0
    pitch_semitones: float = 0.0   # local only: raises pitch, tempo preserved (resampling; approximate)
    sentence_pause: float = 0.30   # seconds, applied by Kokoro inside a chunk
    clause_pause: float = 0.12
    # --- google (Gemini TTS) controls ---
    google_voice: str = "Sulafat"
    google_style: str = ""
    # --- inter-chunk pauses applied by our pipeline (ms) ---
    paragraph_pause_ms: int = 650
    sentence_pause_ms: int = 280

    def validate(self) -> None:
        if not self.name or not self.name.replace("-", "").replace("_", "").isalnum():
            raise InvalidInputError(f"invalid profile name {self.name!r}")
        if not 0.5 <= self.speed <= 2.0:
            raise InvalidInputError("speed must be between 0.5 and 2.0")
        if self.local_blend:
            if any(w <= 0 for w in self.local_blend.values()):
                raise InvalidInputError("blend weights must be positive")
        if not -4.0 <= self.pitch_semitones <= 8.0:
            raise InvalidInputError("pitch_semitones must be between -4 and 8")
        if self.paragraph_pause_ms < 0 or self.sentence_pause_ms < 0:
            raise InvalidInputError("pauses must be >= 0")

    def with_overrides(self, **kw) -> "VoiceProfile":
        kw = {k: v for k, v in kw.items() if v is not None}
        p = replace(self, **kw)
        p.validate()
        return p


ORIGINAL = VoiceProfile(
    name="original",
    description="Warm, refined, confident; conversational with controlled emphasis.",
    character=(
        "warm and articulate; moderate pace with natural pauses; varied sentence "
        "intonation; stable across long narration"
    ),
    # Blend of three Kokoro voices. Weights are a documented starting point that
    # should be auditioned and tuned by ear (see README, 'Tuning the voice').
    local_blend={"af_heart": 0.5, "bf_emma": 0.3, "af_nicole": 0.2},
    speed=0.97,
    google_voice="Sulafat",
    google_style=(
        "Speak as a warm, refined and confident narrator: articulate and "
        "conversational, at a moderate pace with natural pauses, varied pitch "
        "movement and gentle emphasis on key words. Keep the same calm, consistent "
        "delivery throughout."
    ),
)

# Contrast profiles used for A/B comparison; not part of the identity.
REFERENCE = VoiceProfile(
    name="reference",
    description="Single unblended stock voice, neutral delivery (for comparison).",
    local_blend={"af_heart": 1.0},
    google_voice="Kore",
    google_style="Read clearly in a neutral tone at a moderate pace.",
)
DEEP = VoiceProfile(
    name="deep",
    description="Calmer, slightly slower male-leaning blend (for comparison).",
    local_blend={"am_adam": 0.6, "bm_george": 0.4},
    speed=0.93,
    google_voice="Charon",
    google_style="Speak in a calm, grounded, unhurried documentary-narrator manner.",
)

BOY = VoiceProfile(
    name="boy",
    description="Young-sounding male voice (approximation; no child voices exist in Kokoro).",
    character="light, energetic, youthful male; slightly brisk",
    local_blend={"am_puck": 0.6, "am_liam": 0.4},
    pitch_semitones=3.0,
    speed=1.03,
    google_voice="Puck",
    google_style="Speak like an upbeat, curious young boy of about ten: bright, lively and "
                 "clear, with playful intonation. Keep it natural and wholesome.",
)

BUILTIN: dict[str, VoiceProfile] = {p.name: p for p in (ORIGINAL, REFERENCE, DEEP, BOY)}


def load_profiles(path: str | os.PathLike | None = None) -> dict[str, VoiceProfile]:
    """Built-in profiles, overlaid with any user JSON file."""
    profiles = dict(BUILTIN)
    path = path or os.environ.get("SPEECH_ENGINE_PROFILES")
    if not path:
        return profiles
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"cannot read profiles file {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("profiles file must be a JSON object: {name: {...}}")
    allowed = {f.name for f in fields(VoiceProfile)}
    for name, spec in data.items():
        if not isinstance(spec, dict):
            raise ConfigurationError(f"profile {name!r} must be an object")
        unknown = set(spec) - allowed - {"name"}
        if unknown:
            raise ConfigurationError(f"profile {name!r}: unknown fields {sorted(unknown)}")
        base = profiles.get(name, VoiceProfile(name=name))
        prof = replace(base, **{k: v for k, v in spec.items() if k != "name"})
        prof.validate()
        profiles[name] = prof
    return profiles


def get_profile(name: str, path: str | os.PathLike | None = None) -> VoiceProfile:
    profiles = load_profiles(path)
    try:
        return profiles[name]
    except KeyError:
        raise InvalidInputError(
            f"unknown voice profile {name!r}; available: {', '.join(sorted(profiles))}"
        ) from None
