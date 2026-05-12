"""STT backends for Toasted Clusters.

LocalTranscriber wraps Lightning Whisper MLX with a lazy singleton so the model
(~3 GB for large-v3) is downloaded and loaded exactly once per process. The
async wrapper lets callers run transcription off the event loop.

CloudTranscriber is an opt-in Deepgram backend for users who don't have Apple
Silicon. It's only used when TRANSCRIBER_MODE=cloud and DEEPGRAM_API_KEY is set.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger("toasted_clusters.stt")

# Whisper MLX hallucinates these phrases on silence/noise. We strip them at the
# transcript level rather than fight the model.
_HALLUCINATIONS = [
    "thank you.",
    "thanks for watching.",
    "please subscribe.",
    "terima kasih",        # Indonesian "thank you"
    "ご視聴",                # Japanese "thank you for watching"
    "字幕",                  # Japanese "subtitles"
]


_whisper_singleton = None


def _get_whisper():
    """Lazy singleton — first call downloads the model (~3GB for large-v3)."""
    global _whisper_singleton
    if _whisper_singleton is not None:
        return _whisper_singleton
    from lightning_whisper_mlx import LightningWhisperMLX

    model = os.environ.get("WHISPER_MODEL", "large-v3")
    log.info("Loading Whisper MLX model %s (first call downloads ~3GB)…", model)
    _whisper_singleton = LightningWhisperMLX(model=model, batch_size=12)
    return _whisper_singleton


def _is_hallucination(text: str) -> bool:
    s = text.lower().strip()
    if len(s) < 3:
        return True
    # Non-ASCII majority = Japanese/Chinese/Korean hallucination noise
    ascii_ratio = sum(1 for c in s if c.isascii()) / max(len(s), 1)
    if ascii_ratio < 0.5:
        return True
    return any(h in s for h in _HALLUCINATIONS)


def transcribe_file(wav_path: Path | str) -> str:
    """Synchronous transcribe of a WAV file. Hallucination-filtered."""
    whisper = _get_whisper()
    result = whisper.transcribe(audio_path=str(wav_path))
    text = (result.get("text") if isinstance(result, dict) else str(result)) or ""
    text = text.strip()
    if _is_hallucination(text):
        return ""
    return text


class LocalTranscriber:
    """Whisper MLX wrapper for in-memory numpy buffers."""

    async def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        loop = asyncio.get_running_loop()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            _write_wav(wav_path, audio, sample_rate)
            return await loop.run_in_executor(None, transcribe_file, wav_path)
        finally:
            wav_path.unlink(missing_ok=True)


class CloudTranscriber:
    """Deepgram nova-3 fallback for non-Apple-Silicon hosts."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY required for TRANSCRIBER_MODE=cloud")
        self.api_key = api_key

    async def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        try:
            from deepgram import DeepgramClient, PrerecordedOptions
        except ImportError as e:
            raise RuntimeError("pip install 'toasted-clusters[cloud]' for cloud mode") from e

        client = DeepgramClient(self.api_key)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            _write_wav(wav_path, audio, sample_rate)
            with open(wav_path, "rb") as f:
                payload = {"buffer": f.read()}
            opts = PrerecordedOptions(model="nova-3", smart_format=True)
            resp = client.listen.prerecorded.v("1").transcribe_file(payload, opts)
            return resp["results"]["channels"][0]["alternatives"][0]["transcript"]
        finally:
            wav_path.unlink(missing_ok=True)


def get_transcriber(mode: str | None = None):
    mode = (mode or os.environ.get("TRANSCRIBER_MODE", "local")).lower()
    if mode == "local":
        return LocalTranscriber()
    if mode == "cloud":
        return CloudTranscriber(os.environ.get("DEEPGRAM_API_KEY", ""))
    raise ValueError(f"unknown TRANSCRIBER_MODE: {mode!r}")


def _write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    if audio.dtype != np.int16:
        clipped = np.clip(audio, -1.0, 1.0)
        audio = (clipped * 32767).astype(np.int16)
    if audio.ndim > 1:
        audio = audio.mean(axis=1).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(audio.tobytes())
