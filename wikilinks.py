"""Obsidian wikilink extraction.

Scans `$OBSIDIAN_VAULT/{people,projects,companies}/*.md`, builds a name
registry keyed by filename slug, and rewrites transcript text to wrap matches
in `[[slug|Display Name]]` wikilinks.

Two passes:
  1. Multi-word display names (e.g. "Alex Jones") — always safe to link.
  2. Bare first names (e.g. "Steve") — only when the first name is unique
     across the registry. If two registered people share a first name, neither
     gets auto-linked from the bare first name.

Files starting with `_` (templates) or named README/index are skipped.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _vault_root() -> Path:
    raw = os.environ.get("OBSIDIAN_VAULT", "~/vault")
    return Path(os.path.expanduser(raw))


_SKIP_STEMS = {"readme", "index", "_template"}


@dataclass
class Entity:
    slug: str           # filename without .md, e.g. "alex-jones"
    display: str        # human name reconstructed from slug
    kind: str           # "people" | "projects" | "companies"


@dataclass
class LinkResult:
    text: str
    matched: dict[str, list[str]] = field(default_factory=dict)


def load_registry(vault_root: Path | None = None) -> list[Entity]:
    root = vault_root or _vault_root()
    out: list[Entity] = []
    for kind in ("people", "projects", "companies"):
        d = root / kind
        if not d.is_dir():
            continue
        for p in d.glob("*.md"):
            stem = p.stem
            if stem.lower() in _SKIP_STEMS or stem.startswith("_"):
                continue
            out.append(Entity(slug=stem, display=_slug_to_display(stem), kind=kind))
        for sub in d.iterdir():
            if sub.is_dir() and not sub.name.startswith("."):
                out.append(Entity(slug=sub.name, display=_slug_to_display(sub.name), kind=kind))
    out.sort(key=lambda e: len(e.display), reverse=True)
    return out


def _slug_to_display(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part)


def inject(text: str, registry: list[Entity] | None = None) -> LinkResult:
    """Wrap mentions of registered entities in [[slug|Display]] wikilinks."""
    if registry is None:
        registry = load_registry()
    if not registry:
        return LinkResult(text=text)

    first_name_counts: dict[str, int] = {}
    for ent in registry:
        if ent.kind != "people" or " " not in ent.display:
            continue
        first = ent.display.split(" ", 1)[0]
        first_name_counts[first.lower()] = first_name_counts.get(first.lower(), 0) + 1
    unique_first_names = {
        ent.display.split(" ", 1)[0]: ent
        for ent in registry
        if ent.kind == "people"
        and " " in ent.display
        and first_name_counts.get(ent.display.split(" ", 1)[0].lower(), 0) == 1
    }

    matched: dict[str, set[str]] = {}
    linked_spans: list[tuple[int, int]] = []

    # Pass 1: multi-word display names.
    for ent in registry:
        if " " not in ent.display:
            continue
        pattern = re.compile(rf"\b{re.escape(ent.display)}\b", re.IGNORECASE)
        new_chunks: list[str] = []
        cursor = 0
        hits = 0
        for m in pattern.finditer(text):
            if any(not (m.end() <= s or m.start() >= e) for s, e in linked_spans):
                continue
            new_chunks.append(text[cursor:m.start()])
            new_chunks.append(f"[[{ent.slug}|{ent.display}]]")
            cursor = m.end()
            hits += 1
        if hits:
            new_chunks.append(text[cursor:])
            text = "".join(new_chunks)
            matched.setdefault(ent.kind, set()).add(ent.slug)
            linked_spans = [(m.start(), m.end()) for m in re.finditer(r"\[\[[^\]]+\]\]", text)]

    # Pass 2: bare first names for people whose first name is unique.
    # Don't link if the next token is capitalized — that's likely "First
    # Lastname" where Lastname is someone else.
    looks_like_continued_name = re.compile(r"\s+[A-Z][a-z]")
    for first_name, ent in unique_first_names.items():
        pattern = re.compile(rf"\b{re.escape(first_name)}\b", re.IGNORECASE)
        new_chunks = []
        cursor = 0
        hits = 0
        for m in pattern.finditer(text):
            if any(not (m.end() <= s or m.start() >= e) for s, e in linked_spans):
                continue
            if looks_like_continued_name.match(text, m.end()):
                continue
            new_chunks.append(text[cursor:m.start()])
            new_chunks.append(f"[[{ent.slug}|{first_name}]]")
            cursor = m.end()
            hits += 1
        if hits:
            new_chunks.append(text[cursor:])
            text = "".join(new_chunks)
            matched.setdefault(ent.kind, set()).add(ent.slug)
            linked_spans = [(m.start(), m.end()) for m in re.finditer(r"\[\[[^\]]+\]\]", text)]

    return LinkResult(
        text=text,
        matched={k: sorted(v) for k, v in matched.items()},
    )
