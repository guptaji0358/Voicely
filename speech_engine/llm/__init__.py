"""Modular, optional LLM layer (see service.py). Importing this package never touches the network."""

from .base import LLMConfig, LLMError
from .service import LLMService

__all__ = ["LLMConfig", "LLMError", "LLMService"]
