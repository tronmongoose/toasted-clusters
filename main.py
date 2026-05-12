#!/usr/bin/env python3
"""Toasted Clusters — local meeting transcriber CLI.

Subcommands:
    devices                       List audio devices
    transcribe FILE [--write]     Transcribe an audio/video file; optionally write to vault
    record [--seconds N --device NAME --no-write]
                                  Capture from BlackHole, transcribe, write to vault

Writes to $OBSIDIAN_VAULT/$OBSIDIAN_MEETINGS_SUBDIR/ (defaults to ~/vault/meetings/).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

from audio import transcribe_audio_chunked, validate_audio
from finance_guard import contains_financial_data
from transcribe import get_transcriber
from wikilinks import inject as inject_wikilinks

load_dotenv()

DEFAULT_DEVICE = "BlackHole 2ch"
DEFAULT_SAMPLE_RATE = 16000


def _vault_meetings_dir() -> Path:
    root = Path(os.path.expanduser(os.environ.get("OBSIDIAN_VAULT", "~/vault")))
    sub = os.environ.get("OBSIDIAN_MEETINGS_SUBDIR", "meetings")
    return root / sub


# ── devices ──────────────────────────────────────────────────────────


def cmd_devices(_args: argparse.Namespace) -> int:
    import sounddevice as sd
    devices = sd.query_devices()
    print(f"{'idx':>3}  {'name':40s}  in/out")
    for i, d in enumerate(devices):
        print(f"{i:>3}  {d['name']:40s}  {d['max_input_channels']}/{d['max_output_channels']}")

    names = [d["name"] for d in devices]
    bh = any("BlackHole" in n for n in names)
    print()
    print(f"BlackHole 2ch present: {bh}")
    if not bh:
        print()
        print("BlackHole 2ch is NOT visible. One-time setup:")
        print("  1. brew install blackhole-2ch")
        print("  2. sudo killall coreaudiod   (avoids the reboot brew warns about)")
        print("  3. Open /Applications/Utilities/Audio MIDI Setup.app")
        print("  4. + → Create Multi-Output Device. Check BlackHole 2ch + your speakers.")
        print("  5. Rename the new device to something memorable (e.g. 'Meeting Recording').")
        print("  6. Right-click → 'Use This Device For Sound Output'.")
        return 1
    return 0


# ── transcribe a file ────────────────────────────────────────────────


def cmd_transcribe(args: argparse.Namespace) -> int:
    src = Path(args.file)
    wav = validate_audio(src)
    text = transcribe_audio_chunked(wav)
    print(text)
    if args.write:
        from audio import probe_duration
        path = _obsidian_write(text, duration_sec=int(probe_duration(wav)), source=str(src))
        print(f"wrote: {path}", file=sys.stderr)
    return 0


# ── record from BlackHole ────────────────────────────────────────────


def _capture(device: str, seconds: int) -> tuple[np.ndarray, int]:
    import sounddevice as sd
    sr = DEFAULT_SAMPLE_RATE
    print(f"Capturing {seconds}s from '{device}' at {sr} Hz…", file=sys.stderr)
    buf = sd.rec(int(seconds * sr), samplerate=sr, channels=1, dtype="float32", device=device)
    sd.wait()
    return buf.reshape(-1), sr


def cmd_record(args: argparse.Namespace) -> int:
    audio_buf, sr = _capture(args.device, args.seconds)
    transcriber = get_transcriber()
    text = asyncio.run(transcriber.transcribe(audio_buf, sr))

    if not text.strip():
        print("(empty transcript — nothing to write)", file=sys.stderr)
        return 1

    print(text)

    if args.no_write:
        print("(--no-write set; skipping vault write)", file=sys.stderr)
        return 0

    path = _obsidian_write(text, duration_sec=args.seconds, source=f"audio:{args.device}")
    print(f"wrote: {path}", file=sys.stderr)
    return 0


# ── Obsidian write ───────────────────────────────────────────────────


def _obsidian_write(transcript: str, *, duration_sec: int, source: str) -> Path:
    out_dir = _vault_meetings_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now().astimezone()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")
    path = out_dir / f"{date_str}-meeting-{time_str}.md"

    linked = inject_wikilinks(transcript)
    flagged = contains_financial_data(transcript)

    body = "\n".join([
        _build_frontmatter(
            date_iso=date_str,
            timestamp=now.isoformat(timespec="seconds"),
            duration_sec=duration_sec,
            source=source,
            flagged_finance=flagged,
            matched=linked.matched,
        ),
        "",
        f"# Meeting transcript — {date_str} {now.strftime('%H:%M')}",
        "",
        linked.text.strip(),
        "",
    ])
    path.write_text(body, encoding="utf-8")
    return path


def _build_frontmatter(
    *,
    date_iso: str,
    timestamp: str,
    duration_sec: int,
    source: str,
    flagged_finance: bool,
    matched: dict[str, list[str]],
) -> str:
    tags = ["meeting", "transcript"]
    if flagged_finance:
        tags.append("finance-flagged")

    lines = [
        "---",
        "type: meeting-transcript",
        f"date: {date_iso}",
        f"created: {timestamp}",
        f"duration_sec: {duration_sec}",
        f"source: {_yaml_quote(source)}",
        "tags:",
    ]
    lines.extend(f"  - {t}" for t in tags)

    for kind in ("people", "projects", "companies"):
        slugs = matched.get(kind, [])
        if not slugs:
            continue
        lines.append(f"{kind}:")
        lines.extend(f'  - "[[{s}]]"' for s in slugs)

    lines.append("---")
    return "\n".join(lines)


_SAFE_YAML = re.compile(r"^[A-Za-z0-9_./:-]+$")


def _yaml_quote(s: str) -> str:
    return s if _SAFE_YAML.match(s) else '"' + s.replace('"', '\\"') + '"'


# ── entry point ──────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="toasted-clusters")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list audio devices").set_defaults(fn=cmd_devices)

    t = sub.add_parser("transcribe", help="transcribe an audio/video file")
    t.add_argument("file")
    t.add_argument("--write", action="store_true", help="also write to the vault")
    t.set_defaults(fn=cmd_transcribe)

    r = sub.add_parser("record", help="capture from BlackHole, transcribe, write to vault")
    r.add_argument("--seconds", type=int, default=30)
    r.add_argument("--device", default=DEFAULT_DEVICE)
    r.add_argument("--no-write", action="store_true", help="print transcript only")
    r.set_defaults(fn=cmd_record)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
