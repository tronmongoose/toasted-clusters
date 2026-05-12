"""Unit tests for wikilink injection. No filesystem dependency on the vault."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from wikilinks import Entity, inject


REG = [
    Entity(slug="alex-jones", display="Alex Jones", kind="people"),
    Entity(slug="steve-fuchs", display="Steve Fuchs", kind="people"),
    Entity(slug="rick-huckle", display="Rick Huckle", kind="people"),
    Entity(slug="solo-project", display="Solo Project", kind="projects"),  # single-word would skip; this is two-word
    Entity(slug="fast-forward", display="Fast Forward", kind="projects"),
]


def test_multiword_match():
    out = inject("I spoke with Alex Jones about pricing.", REG)
    assert "[[alex-jones|Alex Jones]]" in out.text
    assert out.matched.get("people") == ["alex-jones"]


def test_case_insensitive():
    out = inject("alex jones called.", REG)
    assert "[[alex-jones|Alex Jones]]" in out.text


def test_multiple_distinct_people():
    out = inject("Alex Jones and Steve Fuchs met.", REG)
    assert "[[alex-jones|Alex Jones]]" in out.text
    assert "[[steve-fuchs|Steve Fuchs]]" in out.text
    assert sorted(out.matched["people"]) == ["alex-jones", "steve-fuchs"]


def test_single_word_skipped():
    """Single-word display names are too ambiguous to auto-link."""
    reg = [Entity(slug="solo", display="Solo", kind="projects")]
    out = inject("This is a Solo effort.", reg)
    assert "[[solo" not in out.text
    assert out.matched == {}


def test_project_match():
    out = inject("Migrating to Fast Forward soon.", REG)
    assert "[[fast-forward|Fast Forward]]" in out.text


def test_no_match_returns_unchanged():
    out = inject("Just random text without any names.", REG)
    assert out.text == "Just random text without any names."
    assert out.matched == {}


def test_word_boundary():
    """'Alex Joneson' should NOT match 'Alex Jones'."""
    out = inject("I met Alex Joneson at the conf.", REG)
    assert "[[alex-jones" not in out.text


def test_repeated_mention_links_all():
    out = inject("Alex Jones called. Alex Jones followed up.", REG)
    assert out.text.count("[[alex-jones|Alex Jones]]") == 2


def test_unique_first_name_links():
    """Bare 'Alex' should link to alex-jones because no other Alex in registry."""
    out = inject("Hey Alex, how are you?", REG)
    assert "[[alex-jones|Alex]]" in out.text


def test_ambiguous_first_name_does_not_link():
    """If two registered people share a first name, bare first name links nothing."""
    reg = [
        Entity(slug="alex-jones", display="Alex Jones", kind="people"),
        Entity(slug="alex-smith", display="Alex Smith", kind="people"),
    ]
    out = inject("Alex stopped by.", reg)
    assert "[[alex" not in out.text.lower()


def test_full_name_wins_over_bare_first_name():
    out = inject("Steve Fuchs joined the call.", REG)
    assert "[[steve-fuchs|Steve Fuchs]]" in out.text
    assert "[[steve-fuchs|Steve]] Fuchs" not in out.text
