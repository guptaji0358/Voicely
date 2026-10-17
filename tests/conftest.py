"""Shared fixtures. Every test gets its own empty data directory, so dictionary / feedback / LLM settings of the
developer's real profile are never read or modified. MOCKED components are labelled as such in each test module."""

import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("SPEECH_ENGINE_DATA_DIR", str(d))
    monkeypatch.setenv("SPEECH_ENGINE_ALLOW_MOCK", "1")
    return d


@pytest.fixture
def cancel():
    return threading.Event()


@pytest.fixture
def loose():
    """Acoustic thresholds relaxed for the MOCK backend's sine tones (they are not speech)."""
    from speech_engine.verification import Thresholds
    return Thresholds(min_rate=0, max_rate=1e9)
