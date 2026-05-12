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


# ── record from BlackHole (+ optional mic) ───────────────────────────


def _resolve_mic(explicit: str | None) -> str | int | None:
    """Pick a mic device. Explicit arg > MIC_DEVICE env > sounddevice default > first
    non-BlackHole input device. Returns None if nothing usable is found."""
    import sounddevice as sd
    if explicit:
        return explicit
    env = os.environ.get("MIC_DEVICE", "").strip()
    if env:
        return env
    try:
        default_in = sd.default.device[0]  # may be None or -1
        if default_in is not None and default_in >= 0:
            info = sd.query_devices(default_in)
            if info["max_input_channels"] > 0 and "BlackHole" not in info["name"]:
                return default_in
    except (sd.PortAudioError, IndexError, TypeError):
        pass
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and "BlackHole" not in d["name"]:
            return i
    return None


def _capture_dual(
    system_device: str,
    mic_device: str | int | None,
    seconds: int,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> tuple[np.ndarray, int]:
    """Capture system audio and (optionally) the mic in parallel; mix to mono.

    Two independent InputStreams write into preallocated float32 buffers via
    callbacks, then we sum the buffers with 0.7x gain on each side so the
    mix doesn't clip when both sides are loud. If `mic_device` is None we
    skip mic capture and just return the system-audio buffer.
    """
    import sounddevice as sd

    frames_total = int(seconds * sample_rate)
    sys_buf = np.zeros(frames_total, dtype=np.float32)
    mic_buf = np.zeros(frames_total, dtype=np.float32) if mic_device is not None else None
    sys_idx = 0
    mic_idx = 0

    def _mono(block: np.ndarray) -> np.ndarray:
        return block.mean(axis=1) if block.ndim > 1 and block.shape[1] > 1 else block.reshape(-1)

    def sys_cb(indata, _frames, _time, _status):
        nonlocal sys_idx
        chunk = _mono(indata)
        end = min(sys_idx + len(chunk), frames_total)
        sys_buf[sys_idx:end] = chunk[: end - sys_idx]
        sys_idx = end

    def mic_cb(indata, _frames, _time, _status):
        nonlocal mic_idx
        chunk = _mono(indata)
        end = min(mic_idx + len(chunk), frames_total)
        mic_buf[mic_idx:end] = chunk[: end - mic_idx]
        mic_idx = end

    print(f"Capturing {seconds}s — system='{system_device}'"
          + (f" + mic={mic_device!r}" if mic_device is not None else " (no mic)")
          + f" at {sample_rate} Hz…", file=sys.stderr)

    sys_stream = sd.InputStream(
        device=system_device, channels=2, samplerate=sample_rate,
        dtype="float32", callback=sys_cb,
    )
    streams = [sys_stream]
    if mic_device is not None:
        try:
            mic_stream = sd.InputStream(
                device=mic_device, channels=1, samplerate=sample_rate,
                dtype="float32", callback=mic_cb,
            )
            streams.append(mic_stream)
        except sd.PortAudioError as e:
            print(f"  warning: mic capture failed ({e}); recording system audio only",
                  file=sys.stderr)
            mic_device = None
            mic_buf = None

    for s in streams:
        s.start()
    try:
        sd.sleep(int(seconds * 1000))
    finally:
        for s in streams:
            s.stop()
            s.close()

    if mic_buf is None:
        return sys_buf, sample_rate
    mixed = np.clip(sys_buf * 0.7 + mic_buf * 0.7, -1.0, 1.0).astype(np.float32)
    return mixed, sample_rate


def cmd_record(args: argparse.Namespace) -> int:
    mic_device = None if args.no_mic else _resolve_mic(args.mic)
    if mic_device is None and not args.no_mic:
        print("  warning: no usable mic device found; recording system audio only",
              file=sys.stderr)
    audio_buf, sr = _capture_dual(args.device, mic_device, args.seconds)
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

    r = sub.add_parser("record", help="capture system audio + mic, transcribe, write to vault")
    r.add_argument("--seconds", type=int, default=30)
    r.add_argument("--device", default=DEFAULT_DEVICE, help="system audio loopback (default: BlackHole 2ch)")
    r.add_argument("--mic", default=None, help="mic device (name or index). default: system default input")
    r.add_argument("--no-mic", action="store_true", help="record system audio only; ignore the mic")
    r.add_argument("--no-write", action="store_true", help="print transcript only; skip vault write")
    r.set_defaults(fn=cmd_record)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
