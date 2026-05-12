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
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from audio import probe_duration, transcribe_audio_chunked, validate_audio
from finance_guard import contains_financial_data
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


def _capture_streaming(
    system_device: str,
    mic_device: str | int | None,
    seconds: int,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> tuple[Path, Path | None]:
    """Stream system audio (and optionally mic) to temp WAV files as they arrive.

    Each sd.InputStream callback writes its block straight to its own
    soundfile.SoundFile (16 kHz mono PCM_16). SIGINT/SIGTERM finalize the
    files cleanly via a threading.Event, so a partial recording survives
    Ctrl-C, kill, or crash. Returns (sys_wav_path, mic_wav_path) — mic path
    is None when mic capture is disabled or fails to open.
    """
    import sounddevice as sd
    import soundfile as sf

    sys_tmp = tempfile.NamedTemporaryFile(prefix="toasted-clusters-sys-", suffix=".wav", delete=False)
    sys_tmp.close()
    sys_path = Path(sys_tmp.name)

    mic_path: Path | None = None
    if mic_device is not None:
        mic_tmp = tempfile.NamedTemporaryFile(prefix="toasted-clusters-mic-", suffix=".wav", delete=False)
        mic_tmp.close()
        mic_path = Path(mic_tmp.name)

    print(f"Capturing up to {seconds}s — system='{system_device}'"
          + (f" + mic={mic_device!r}" if mic_device is not None else " (no mic)")
          + f" at {sample_rate} Hz…", file=sys.stderr)
    print(f"  → streaming to {sys_path}"
          + (f" + {mic_path}" if mic_path else "")
          + " (survives stop/crash)", file=sys.stderr)

    stop = threading.Event()

    def _on_stop(signum, _frame):
        print(f"  ↳ signal {signum} received; finalizing capture…", file=sys.stderr)
        stop.set()

    prev_int = signal.signal(signal.SIGINT, _on_stop)
    prev_term = signal.signal(signal.SIGTERM, _on_stop)

    sys_wav = sf.SoundFile(str(sys_path), mode="w", samplerate=sample_rate,
                           channels=1, subtype="PCM_16")
    mic_wav: "sf.SoundFile | None" = None
    if mic_path is not None:
        mic_wav = sf.SoundFile(str(mic_path), mode="w", samplerate=sample_rate,
                               channels=1, subtype="PCM_16")

    def sys_cb(indata, _frames, _time, status):
        if status:
            print(f"  sys audio status: {status}", file=sys.stderr)
        mono = indata.mean(axis=1, keepdims=True) if indata.shape[1] > 1 else indata
        sys_wav.write(mono.copy())

    def mic_cb(indata, _frames, _time, status):
        if status:
            print(f"  mic audio status: {status}", file=sys.stderr)
        mono = indata.mean(axis=1, keepdims=True) if indata.shape[1] > 1 else indata
        mic_wav.write(mono.copy())  # type: ignore[union-attr]

    try:
        sys_stream = sd.InputStream(
            samplerate=sample_rate, channels=2, dtype="float32",
            device=system_device, callback=sys_cb,
        )
        streams = [sys_stream]

        if mic_path is not None:
            try:
                mic_stream = sd.InputStream(
                    samplerate=sample_rate, channels=1, dtype="float32",
                    device=mic_device, callback=mic_cb,
                )
                streams.append(mic_stream)
            except sd.PortAudioError as e:
                print(f"  warning: mic capture failed ({e}); recording system audio only",
                      file=sys.stderr)
                if mic_wav is not None:
                    mic_wav.close()
                    mic_wav = None
                try:
                    mic_path.unlink()
                except OSError:
                    pass
                mic_path = None

        try:
            for s in streams:
                s.start()
            stop.wait(timeout=seconds)
        finally:
            for s in streams:
                try:
                    s.stop()
                    s.close()
                except sd.PortAudioError:
                    pass
    finally:
        sys_wav.close()
        if mic_wav is not None:
            mic_wav.close()
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)

    return sys_path, mic_path


def _mix_streams(sys_wav: Path, mic_wav: Path, sample_rate: int = DEFAULT_SAMPLE_RATE) -> Path:
    """Mix two mono WAVs via ffmpeg amix at 0.7x each (matches prior in-Python balance)."""
    mixed_tmp = tempfile.NamedTemporaryFile(prefix="toasted-clusters-mixed-", suffix=".wav", delete=False)
    mixed_tmp.close()
    mixed_path = Path(mixed_tmp.name)
    subprocess.run(
        ["ffmpeg", "-y",
         "-i", str(sys_wav), "-i", str(mic_wav),
         "-filter_complex", "[0:a][1:a]amix=inputs=2:weights=0.7 0.7",
         "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le",
         str(mixed_path)],
        capture_output=True, check=True, timeout=600,
    )
    return mixed_path


def cmd_record(args: argparse.Namespace) -> int:
    mic_device = None if args.no_mic else _resolve_mic(args.mic)
    if mic_device is None and not args.no_mic:
        print("  warning: no usable mic device found; recording system audio only",
              file=sys.stderr)

    sys_wav, mic_wav = _capture_streaming(args.device, mic_device, args.seconds)

    if mic_wav is None:
        final_wav = sys_wav
    else:
        final_wav = _mix_streams(sys_wav, mic_wav)

    duration = probe_duration(final_wav)
    print(f"  captured {duration:.1f}s; transcribing…", file=sys.stderr)

    text = transcribe_audio_chunked(final_wav)

    # Cleanup temp WAVs only after transcription succeeds — on a crash earlier,
    # the source files in /var/folders/ are recoverable via
    # `python main.py transcribe <path>`.
    cleanup_paths: list[Path] = []
    if final_wav != sys_wav:
        cleanup_paths.append(final_wav)
    cleanup_paths.append(sys_wav)
    if mic_wav is not None:
        cleanup_paths.append(mic_wav)
    for p in cleanup_paths:
        try:
            p.unlink()
        except OSError:
            pass

    if not text.strip():
        print("(empty transcript — nothing to write)", file=sys.stderr)
        return 1

    print(text)

    if args.no_write:
        print("(--no-write set; skipping vault write)", file=sys.stderr)
        return 0

    path = _obsidian_write(text, duration_sec=int(duration), source=f"audio:{args.device}")
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
