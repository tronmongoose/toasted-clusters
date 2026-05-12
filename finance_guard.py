"""Detect financial data in transcripts.

Used to tag transcripts that mention money so downstream notification or
search workflows can route them differently (e.g. don't ship to a public
Telegram channel; keep them on private ntfy or only in the local vault).

This module is intentionally regex-only — no LLM, no PII inference. False
positives are acceptable; false negatives are not.
"""
from __future__ import annotations

import re

FINANCIAL_PATTERNS = [
    r"\$[\d,]+\.?\d*",              # $1,234.56
    r"balance[:\s]+\$",             # "balance: $..."
    r"account\s*#?\s*\d{4}",        # "account #1234"
    r"SSN|social security",         # SSN references
    r"routing\s*number",            # bank routing numbers
    r"\b\d{1,3}(?:,\d{3})+\s*(?:dollars?|usd)\b",  # "1,234 dollars"
]

_RE = [re.compile(p, re.IGNORECASE) for p in FINANCIAL_PATTERNS]


def contains_financial_data(text: str) -> bool:
    """True if `text` looks like it contains personal/financial figures."""
    return any(p.search(text) for p in _RE)
