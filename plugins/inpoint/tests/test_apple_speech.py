"""Backend choice and the wrong-language heuristic. Run: uv run python -m pytest tests"""
import os

from inpoint import apple_speech as a


def test_wanted_follows_language_and_override(monkeypatch):
    monkeypatch.setattr(a, "unavailable_reason", lambda: None)
    monkeypatch.delenv("INPOINT_WHISPER_BACKEND", raising=False)
    assert a.wanted(None) is True          # no language: assume English
    assert a.wanted("en-IN") is True
    assert a.wanted("hi") is False         # Hindi -> Whisper
    monkeypatch.setenv("INPOINT_WHISPER_BACKEND", "mlx")
    assert a.wanted("en") is False
    monkeypatch.setenv("INPOINT_WHISPER_BACKEND", "apple")
    assert a.wanted("hi") is True          # forced, helper will exit 3 and we error


def test_wanted_false_when_unavailable(monkeypatch):
    monkeypatch.delenv("INPOINT_WHISPER_BACKEND", raising=False)
    monkeypatch.setattr(a, "unavailable_reason", lambda: "macOS only")
    assert a.wanted("en") is False


def test_looks_sparse():
    seg = lambda n: {"segments": [{"words": [{"word": "w"}] * n}]}
    assert a.looks_sparse(seg(5), duration=60) is True      # 5 wpm: wrong language
    assert a.looks_sparse(seg(120), duration=60) is False   # normal speech
    assert a.looks_sparse(seg(0), duration=8) is False      # short clips never trigger
