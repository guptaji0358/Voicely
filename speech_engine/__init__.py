"""Original speech-generation package. Optional, independent of the OCR code.

    from speech_engine import SpeechEngine, make_backend, get_profile
    eng = SpeechEngine(make_backend("local"), get_profile("original"))
    eng.speak("Hello world")                       # blocking, RAM-only playback
    eng.synthesize_to_file(text, "out.wav")        # explicit file output
"""

from .engine import SpeechEngine, SpeechHandle, make_backend
from .exceptions import SpeechEngineError
from .voice_profiles import VoiceProfile, get_profile, load_profiles

__all__ = ["SpeechEngine", "SpeechHandle", "make_backend", "SpeechEngineError",
           "VoiceProfile", "get_profile", "load_profiles"]
__version__ = "0.1.0"
