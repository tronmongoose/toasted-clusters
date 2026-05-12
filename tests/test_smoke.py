"""Offline smoke tests — no audio hardware, no model download."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def test_get_transcriber_local():
    from transcribe import LocalTranscriber, get_transcriber
    t = get_transcriber("local")
    assert isinstance(t, LocalTranscriber)


def test_cloud_requires_key():
    import pytest
    from transcribe import get_transcriber
    with pytest.raises(RuntimeError):
        get_transcriber("cloud")


def test_unknown_mode_raises():
    import pytest
    from transcribe import get_transcriber
    with pytest.raises(ValueError):
        get_transcriber("frontier")


def test_finance_guard_detects_money():
    from finance_guard import contains_financial_data
    assert contains_financial_data("We spent $1,234.56 last quarter.")
    assert contains_financial_data("balance: $500")
    assert contains_financial_data("account #1234")


def test_finance_guard_clean_text():
    from finance_guard import contains_financial_data
    assert not contains_financial_data("We talked about the weather and weekend plans.")
