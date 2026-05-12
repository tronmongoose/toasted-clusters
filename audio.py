"""Audio normalization + chunked transcription for long-form recordings.

Whisper MLX hallucinates (repetition loops) on audio longer than ~15 minutes
because it loses context. Chunking into 5-minute pieces avoids this — each
chunk gets a fresh context so runaway loops don't propagate.

ffmpeg + ffprobe required (usually `brew install ffmpeg`).
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from transcribe import transcribe_file


def validate_audio(wav_path: Path) -> Path:
    """Ensure audio is 16kHz mono WAV; convert via ffmpeg if not.

    Returns the path to the validated WAV. May be a sibling file if conversion
    was required (e.g. `foo.mov` → `foo_16k.wav`).
    """
    if not wav_path.exists():
        raise FileNotFoundError(f"Audio file not found: {wav_path}")

    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(wav_path)],
            capture_output=True, text=True, timeout=10,
        )
        info = json.loads(probe.stdout)
        streams = info.get("streams", [])
        if streams:
            s = streams[0]
            if (s.get("codec_name") == "pcm_s16le"
                    and int(s.get("sample_rate", 0)) == 16000
                    and int(s.get("channels", 0)) == 1):
                return wav_path
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        pass

    converted = wav_path.parent / f"{wav_path.stem}_16k.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_path),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(converted)],
        capture_output=True, timeout=600, check=True,
    )
    return converted


def probe_duration(wav_path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(wav_path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def transcribe_audio_chunked(
    wav_path: Path,
    *,
    chunk_secs: int = 300,
    chunk_threshold_secs: int = 900,
    progress: bool = True,
) -> str:
    """Transcribe a WAV. Chunks audio > threshold (default 15 min) into 5-min pieces."""
    duration = probe_duration(wav_path)

    if duration <= chunk_threshold_secs:
        if progress:
            print(f"  Single-pass transcribe ({duration:.0f}s)")
        return transcribe_file(wav_path)

    if progress:
        print(f"  Chunking {duration:.0f}s audio into {chunk_secs}s pieces…")
    with tempfile.TemporaryDirectory(prefix="tc_chunks_") as tmp:
        tmp_dir = Path(tmp)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(wav_path),
             "-f", "segment", "-segment_time", str(chunk_secs),
             "-c", "copy", str(tmp_dir / "chunk_%03d.wav")],
            capture_output=True, check=True,
        )
        chunks = sorted(tmp_dir.glob("chunk_*.wav"))
        if progress:
            print(f"  {len(chunks)} chunks to transcribe")

        parts: list[str] = []
        for i, c in enumerate(chunks):
            t0 = time.time()
            text = transcribe_file(c)
            if progress:
                words = len(text.split()) if text else 0
                print(f"    [{i+1}/{len(chunks)}] {words} words in {time.time()-t0:.1f}s")
            if text:
                parts.append(text)
        return "\n\n".join(parts)
