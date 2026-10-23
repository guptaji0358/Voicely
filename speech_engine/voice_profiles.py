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
    # --- voice-library metadata (UI grouping/labels only; no effect on synthesis) ---
    gender: str = ""          # "female" | "male" | "" (unspecified)
    display_name: str = ""    # friendly label; falls back to ``name``
    tagline: str = ""         # short style tag shown next to the name, e.g. "Warm · Narration"
    # --- local (Kokoro) controls ---
    local_blend: dict[str, float] = field(default_factory=dict)  # voice -> weight
    local_lang: str = "en-us"
    speed: float = 1.0
    pitch_semitones: float = 0.0   # local only: raises pitch, tempo preserved (resampling; approximate)
    tilt_db: float = 0.0           # local only: spectral tilt, + brighter / - warmer (shelf EQ around 1.5 kHz)
    style_push: float = 0.0        # local only: exaggerate what makes the blend differ from the average voice of its sex
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
        if not -6.0 <= self.pitch_semitones <= 8.0:
            raise InvalidInputError("pitch_semitones must be between -6 and 8")
        if not -9.0 <= self.tilt_db <= 9.0:
            raise InvalidInputError("tilt_db must be between -9 and 9")
        if not -0.5 <= self.style_push <= 1.0:
            raise InvalidInputError("style_push must be between -0.5 and 1.0")
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

def _extra(name, description, blend, google_voice, google_style, *, lang="en-us",
           speed=1.0, pitch=0.0, tilt=0.0, push=0.0) -> VoiceProfile:
    return VoiceProfile(name=name, description=description, local_blend=blend, local_lang=lang,
                        speed=speed, pitch_semitones=pitch, tilt_db=tilt, style_push=push,
                        google_voice=google_voice, google_style=google_style)


# 10 girl + 10 boy voices. Like ``original`` these are blends of stock Kokoro voices
# (plus speed/pitch) on the local backend and prebuilt Gemini voices + a style
# instruction on the google backend; none is a newly trained voice.
GIRLS = (
    _extra("girl_bella", "Bright, friendly young woman.", {"af_bella": 1.0}, "Kore",
           "Speak in a bright, friendly, upbeat tone."),
    _extra("girl_sarah", "Soft, warm and gentle.", {"af_sarah": 0.7, "af_nicole": 0.3}, "Leda",
           "Speak softly and warmly, gentle and reassuring.", speed=0.97),
    _extra("girl_sky", "Light, airy and quick.", {"af_sky": 0.6, "af_aoede": 0.4}, "Aoede",
           "Speak in a light, airy, lively manner.", speed=1.05),
    _extra("girl_nova", "Clear, modern, confident.", {"af_nova": 0.7, "af_river": 0.3}, "Zephyr",
           "Speak clearly and confidently, like a modern presenter."),
    _extra("girl_jessica", "Expressive storyteller.", {"af_jessica": 1.0}, "Callirrhoe",
           "Speak expressively, like a storyteller, with varied intonation.", speed=1.02),
    _extra("girl_kore", "Calm, composed, professional.", {"af_kore": 0.6, "af_alloy": 0.4}, "Autonoe",
           "Speak calmly and professionally at an even pace.", speed=0.98),
    _extra("girl_alice", "British, crisp and polite.", {"bf_alice": 0.6, "bf_lily": 0.4}, "Despina",
           "Speak with a crisp, polite British manner.", lang="en-gb"),
    _extra("girl_isabella", "British, elegant and slow.", {"bf_isabella": 1.0}, "Erinome",
           "Speak elegantly and unhurriedly with a British accent.", lang="en-gb", speed=0.95),
    _extra("girl_emma", "British, warm narrator.", {"bf_emma": 1.0}, "Laomedeia",
           "Speak as a warm British narrator.", lang="en-gb", speed=0.97),
    _extra("girl_young", "Young-sounding girl (approximation; Kokoro has no child voices).",
           {"af_sky": 0.5, "af_bella": 0.5}, "Achernar",
           "Speak like a cheerful, curious young girl: bright, lively and clear.",
           speed=1.04, pitch=2.0),
)
BOYS = (
    _extra("boy_liam", "Friendly, natural young man.", {"am_liam": 1.0}, "Puck",
           "Speak in a friendly, natural, upbeat tone."),
    _extra("boy_eric", "Steady, clear and neutral.", {"am_eric": 1.0}, "Orus",
           "Speak clearly and steadily in a neutral tone."),
    _extra("boy_michael", "Warm, mature and reassuring.", {"am_michael": 0.7, "am_echo": 0.3},
           "Charon", "Speak warmly and reassuringly like an experienced presenter.", speed=0.97),
    _extra("boy_echo", "Smooth, relaxed.", {"am_echo": 1.0}, "Enceladus",
           "Speak in a smooth, relaxed, conversational manner."),
    _extra("boy_fenrir", "Deep, strong, dramatic.", {"am_fenrir": 0.6, "am_onyx": 0.4}, "Fenrir",
           "Speak in a deep, strong, dramatic voice.", speed=0.95),
    _extra("boy_adam", "Calm, grounded narrator.", {"am_adam": 0.5, "am_michael": 0.5}, "Iapetus",
           "Speak calmly and grounded, like a documentary narrator.", speed=0.94),
    _extra("boy_daniel", "British, polished.", {"bm_daniel": 1.0}, "Umbriel",
           "Speak in a polished British manner.", lang="en-gb"),
    _extra("boy_fable", "British, expressive storyteller.", {"bm_fable": 0.6, "bm_lewis": 0.4},
           "Algieba", "Speak as an expressive British storyteller.", lang="en-gb"),
    _extra("boy_george", "British, deep and authoritative.", {"bm_george": 1.0}, "Rasalgethi",
           "Speak in a deep, authoritative British voice.", lang="en-gb", speed=0.95),
    _extra("boy_teen", "Teenage-sounding boy (approximation; Kokoro has no child voices).",
           {"am_puck": 0.5, "am_eric": 0.5}, "Algenib",
           "Speak like an energetic teenage boy: lively, casual and clear.",
           speed=1.05, pitch=2.0),
)

# Second batch (10 girls + 10 boys). Uses the Kokoro voices the first batch left out - Hindi, Spanish, French,
# Italian and Portuguese speakers read English with that accent - plus a few new blends of the English voices.
GIRLS_MORE = (
    _extra("girl_heart", "Warm, natural and friendly - the cleanest stock voice.", {"af_heart": 1.0},
           "Sulafat", "Speak warmly and naturally, friendly and clear."),
    _extra("girl_luna", "Soft, intimate and calm.", {"af_nicole": 0.6, "af_sky": 0.4}, "Vindemiatrix",
           "Speak softly and intimately, calm and soothing.", speed=0.92),
    _extra("girl_ruby", "Upbeat, energetic presenter.", {"af_bella": 0.5, "af_jessica": 0.5}, "Zephyr",
           "Speak like an upbeat, energetic presenter.", speed=1.08),
    _extra("girl_grace", "Graceful, refined British blend.", {"bf_emma": 0.5, "af_heart": 0.5}, "Gacrux",
           "Speak gracefully and with refinement.", lang="en-gb", speed=0.97),
    _extra("girl_maya", "Hindi-accented English, warm.", {"hf_alpha": 1.0}, "Pulcherrima",
           "Speak warmly with a light Indian accent."),
    _extra("girl_anya", "Hindi-accented English, soft.", {"hf_beta": 1.0}, "Kore",
           "Speak softly and gently with a light Indian accent.", speed=0.97),
    _extra("girl_sofia", "Spanish-accented English.", {"ef_dora": 1.0}, "Leda",
           "Speak warmly with a light Spanish accent."),
    _extra("girl_chloe", "French-accented English, elegant.", {"ff_siwis": 1.0}, "Aoede",
           "Speak elegantly with a light French accent."),
    _extra("girl_giulia", "Italian-accented English, expressive.", {"if_sara": 1.0}, "Despina",
           "Speak expressively with a light Italian accent."),
    _extra("girl_luisa", "Brazilian-accented English.", {"pf_dora": 1.0}, "Achernar",
           "Speak brightly with a light Brazilian accent."),
)
BOYS_MORE = (
    _extra("boy_santa", "Jolly, deep and warm.", {"am_santa": 1.0}, "Sadaltager",
           "Speak in a jolly, deep, warm voice.", speed=0.95),
    _extra("boy_onyx", "Deep, resonant and smooth.", {"am_onyx": 1.0}, "Alnilam",
           "Speak in a deep, resonant, smooth voice.", speed=0.95),
    _extra("boy_lewis", "British, calm and thoughtful.", {"bm_lewis": 1.0}, "Schedar",
           "Speak calmly and thoughtfully in a British manner.", lang="en-gb"),
    _extra("boy_alex", "Spanish-accented English.", {"em_alex": 1.0}, "Achird",
           "Speak in a friendly tone with a light Spanish accent."),
    _extra("boy_nico", "Italian-accented English.", {"im_nicola": 1.0}, "Zubenelgenubi",
           "Speak expressively with a light Italian accent."),
    _extra("boy_omega", "Hindi-accented English, steady.", {"hm_omega": 1.0}, "Orus",
           "Speak steadily with a light Indian accent."),
    _extra("boy_psi", "Hindi-accented English, smooth.", {"hm_psi": 1.0}, "Sadachbia",
           "Speak smoothly with a light Indian accent."),
    _extra("boy_pedro", "Brazilian-accented English.", {"pm_alex": 1.0}, "Puck",
           "Speak in a friendly tone with a light Brazilian accent."),
    _extra("boy_host", "Energetic radio host.", {"am_puck": 0.5, "am_liam": 0.5}, "Fenrir",
           "Speak like an energetic radio host.", speed=1.08),
    _extra("boy_sage", "Wise, deep elder.", {"am_onyx": 0.5, "bm_george": 0.5}, "Charon",
           "Speak slowly like a wise, deep-voiced elder.", speed=0.9),
)

# Per-voice timbre so no two sound alike even where they share a Kokoro base voice:
# name -> (pitch semitones, tilt dB, style push, speed). Pitch moves the voice "size", tilt moves bright/warm,
# push exaggerates the blend's individuality. Tuned with packaging/voice_distinctness.py.
_TIMBRE = {
    "girl_heart": (-3.5, -5.5, 0.35, 1.0),
    "girl_kore": (-3.1, 3.5, 0.35, 0.98),
    "girl_luna": (-2.6, -2.0, 0.35, 0.92),
    "girl_sarah": (-2.2, 6.0, 0.35, 0.97),
    "girl_isabella": (-1.7, -4.0, 0.35, 0.94),
    "girl_grace": (-1.3, 1.5, 0.35, 1.0),
    "girl_emma": (-0.8, -6.5, 0.35, 1.0),
    "girl_giulia": (-0.4, 4.5, 0.35, 1.0),
    "girl_maya": (0.1, -1.0, 0.35, 1.0),
    "girl_nova": (0.5, 7.0, 0.35, 1.0),
    "girl_anya": (1.0, -5.5, 0.35, 1.0),
    "girl_sofia": (1.4, 3.5, 0.35, 1.0),
    "girl_jessica": (1.9, -2.0, 0.35, 1.0),
    "girl_alice": (2.3, 6.0, 0.35, 1.0),
    "girl_chloe": (2.8, -4.0, 0.35, 1.0),
    "girl_bella": (3.2, 1.5, 0.35, 1.0),
    "girl_luisa": (3.7, -6.5, 0.35, 1.02),
    "girl_sky": (4.1, 4.5, 0.35, 1.06),
    "girl_ruby": (4.6, -1.0, 0.35, 1.09),
    "girl_young": (5.0, 7.0, 0.35, 1.05),
    "boy_onyx": (-5.0, -5.5, 0.35, 0.95),
    "boy_sage": (-4.5, 3.5, 0.35, 0.9),
    "boy_santa": (-4.1, -2.0, 0.35, 0.95),
    "boy_fenrir": (-3.6, 6.0, 0.35, 0.95),
    "boy_george": (-3.1, -4.0, 0.35, 0.95),
    "boy_adam": (-2.6, 1.5, 0.35, 0.94),
    "boy_michael": (-2.2, -6.5, 0.35, 1.0),
    "boy_nico": (-1.7, 4.5, 0.35, 1.0),
    "boy_lewis": (-1.2, -1.0, 0.35, 1.0),
    "boy_omega": (-0.7, 7.0, 0.35, 1.0),
    "boy_daniel": (-0.3, -5.5, 0.35, 1.0),
    "boy_echo": (0.2, 3.5, 0.35, 1.0),
    "boy_liam": (0.7, -2.0, 0.35, 1.0),
    "boy_pedro": (1.2, 6.0, 0.35, 1.0),
    "boy_fable": (1.6, -4.0, 0.35, 1.0),
    "boy_eric": (2.1, 1.5, 0.35, 1.0),
    "boy_alex": (2.6, -6.5, 0.35, 1.0),
    "boy_psi": (3.1, 4.5, 0.35, 1.0),
    "boy_host": (3.5, -1.0, 0.35, 1.08),
    "boy_teen": (4.0, 7.0, 0.35, 1.06),
    "boy": (3.0, 0.0, 0.0, 1.03),
    "deep": (-2.0, -7.0, 0.2, 0.93),
    "original": (0.0, -1.0, 0.0, 0.97),
}


def _timbre(p: VoiceProfile) -> VoiceProfile:
    t = _TIMBRE.get(p.name)
    if not t:
        return p
    pitch, tilt, push, speed = t
    return replace(p, pitch_semitones=pitch, tilt_db=tilt, style_push=push, speed=speed)


# name -> (gender, display name, tagline). The girl_*/boy_* names derive their label automatically.
_META = {
    "original": ("female", "Original Voice", "Warm, refined narrator"),
    "reference": ("female", "Reference", "Neutral, for comparison"),
    "deep": ("male", "Deep Narrator", "Calm documentary narrator"),
    "boy": ("male", "Young Boy", "Youthful and energetic"),
    "girl_young": ("female", "Young Girl", "Youthful, bright (approximation)"),
    "boy_teen": ("male", "Teen Boy", "Teenage, energetic (approximation)"),
}


def _with_meta(p: VoiceProfile) -> VoiceProfile:
    if p.name in _META:
        g, disp, tag = _META[p.name]
    else:
        prefix, _, rest = p.name.partition("_")
        g = {"girl": "female", "boy": "male"}.get(prefix, "")
        disp, tag = rest.capitalize(), p.description.rstrip(".")
    return replace(p, gender=g, display_name=disp, tagline=tag)


BUILTIN: dict[str, VoiceProfile] = {
    p.name: p for p in map(lambda q: _timbre(_with_meta(q)), (ORIGINAL, REFERENCE, DEEP, BOY, *GIRLS, *BOYS, *GIRLS_MORE, *BOYS_MORE))}


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
