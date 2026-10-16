"""Exception hierarchy and CLI exit codes for the speech engine."""

from __future__ import annotations


class SpeechEngineError(Exception):
    """Base class for all speech-engine failures."""

    exit_code = 1


class InvalidInputError(SpeechEngineError):
    """Empty/unusable text or bad parameters."""

    exit_code = 2


class ConfigurationError(SpeechEngineError):
    """Missing credentials, model files or other setup."""

    exit_code = 3


class BackendUnavailableError(SpeechEngineError):
    """The selected backend cannot run (missing package, model, credentials, network)."""

    exit_code = 4


class SynthesisError(SpeechEngineError):
    """The backend ran but synthesis failed."""

    exit_code = 5


class RateLimitError(SynthesisError):
    """The provider rejected the request for quota / rate reasons."""

    exit_code = 6


class AudioOutputError(SpeechEngineError):
    """Playback device or output file problem."""

    exit_code = 7


class SynthesisCancelled(SpeechEngineError):
    """Raised inside a worker when the request was cancelled."""

    exit_code = 130


class EncoderUnavailableError(SpeechEngineError):
    """A required audio encoder (e.g. MP3 via 'lameenc') is not installed."""

    exit_code = 8
