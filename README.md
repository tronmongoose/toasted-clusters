# Toasted Clusters

> Local, private meeting transcriber for macOS. Captures system audio,
> transcribes via Whisper MLX on Apple Silicon, writes plain-text markdown into
> your Obsidian vault with auto-wikilinks to people you already track.
>
> No cloud. No SaaS. No AI note-taker silently joining your meetings.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey)](#requirements)

---

## Why this exists

Cloud meeting note-takers (Granola, Otter, Fireflies, Read.ai, etc.) are
convenient — and they ship every word of your conversation to a third-party
service. For most calls that's a fine tradeoff. For personal, sensitive, or
strategic conversations, it isn't.

Toasted Clusters is the **made-from-scratch** alternative: the audio never
leaves your Mac. Transcription happens locally via Apple's MLX framework. The
output lands as plain markdown in your own Obsidian vault — searchable,
linkable, and yours forever.

It's the granola you bake yourself: same nutrients, fewer ingredients you
didn't pick.

## Who this is for

**Good fit if you:**
- Run an Apple Silicon Mac (M1, M2, M3, or M4 — Whisper MLX needs Metal)
- Already use Obsidian (or are willing to) as your second brain
- Want transcripts of your *personal* calls under your own control
- Are comfortable installing a kernel-level audio driver (BlackHole) and editing
  Audio MIDI Setup once
- Believe "local first" is a feature, not a slogan

**Not the right tool if you:**
- Want a turnkey product with a polished UI — this is a CLI
- Need speaker diarization out of the box — not yet implemented
  ([roadmap](#roadmap))
- Run Windows or Linux — Whisper MLX is Apple Silicon only today
- Need to capture calls protected by your employer's recording policy — read it
  first; don't use this to circumvent it
- Are in a two-party-consent jurisdiction and don't have consent — your laws,
  your liability

## How it works

```
  System audio
  ─────────────
       │
       ▼
  ┌─────────────────────────┐         ┌─────────────────┐
  │  BlackHole 2ch driver   │ ──────▶ │  Your speakers   │  ← you still hear it
  │  (kernel audio loopback)│         └─────────────────┘
  └─────────────────────────┘
       │
       ▼ (Multi-Output Device)
  ┌─────────────────────────┐
  │  sounddevice (Python)   │
  │  → 16 kHz mono WAV      │
  └─────────────────────────┘
       │
       ▼
  ┌─────────────────────────┐
  │  Whisper MLX (large-v3) │  ← 100% local. ~3 GB on first run.
  │  5-min chunking for long│
  │  audio (avoids loops)   │
  └─────────────────────────┘
       │
       ▼
  ┌─────────────────────────┐
  │  finance_guard          │  → tags transcripts that mention money
  │  wikilinks              │  → auto-links names from ~/vault/people/
  └─────────────────────────┘
       │
       ▼
  ~/vault/meetings/YYYY-MM-DD-meeting-HHMMSS.md
       (markdown + YAML frontmatter, ready for Obsidian)
```

## Requirements

- macOS with **Apple Silicon** (M1/M2/M3/M4)
- **Python 3.10+**
- **ffmpeg** (`brew install ffmpeg`) — used for audio normalization & chunking
- **BlackHole 2ch** virtual audio driver
- An Obsidian vault (the tool writes markdown files; Obsidian itself is
  optional, but the wikilink output is most useful if you read it in Obsidian)

## Setup

### 1. Install BlackHole

```bash
brew install blackhole-2ch
sudo killall coreaudiod   # avoids the reboot brew warns about
```

Verify the driver loaded:

```bash
python3 -c "import sounddevice as sd; print([d['name'] for d in sd.query_devices()])"
```

You should see `BlackHole 2ch` in the list.

### 2. Create a Multi-Output Device in Audio MIDI Setup

This sends system audio to both BlackHole (so we can record) and your speakers
(so you can still hear it).

1. Open `/Applications/Utilities/Audio MIDI Setup.app`
2. Click the **`+`** in the bottom-left → **Create Multi-Output Device**
3. Check the **Use** boxes for `BlackHole 2ch` **and** your speakers (whatever
   you actually listen through — built-in speakers, monitor speakers, etc.)
4. **Don't bundle outputs you don't use** — it disables volume keys and causes
   sync drift
5. Set the **Clock Source** dropdown to `BlackHole 2ch`
6. Enable **Drift Correction** on the speaker device (not on BlackHole)
7. Rename the Multi-Output device to something memorable (e.g. `Meeting Recording`)
8. Right-click the device → **"Use This Device For Sound Output"**

Day-to-day: switch system output to `Meeting Recording` before a meeting,
switch back to your normal output afterward (the Multi-Output Device has no
volume control, so it's awkward for normal use).

### 3. Clone, install, configure

```bash
git clone https://github.com/tronmongoose/toasted-clusters.git
cd toasted-clusters

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: at minimum, point OBSIDIAN_VAULT at your vault root.
```

### 4. First run

```bash
# Check the audio devices are wired up
python main.py devices

# Try the local Whisper model on an existing audio file
# (first run downloads ~3 GB of model weights)
python main.py transcribe path/to/sample.wav

# 30-second live capture from BlackHole; writes to ~/vault/meetings/
python main.py record --seconds 30
```

## Usage

### `devices`

Lists every audio device visible to the OS. Use this to confirm BlackHole and
your Multi-Output Device are configured correctly.

```bash
python main.py devices
```

### `transcribe FILE`

Transcribe any audio or video file (anything ffmpeg can read: WAV, MP3, MP4,
MOV, M4A, etc.). Prints the transcript to stdout. Pass `--write` to also write
the markdown file into your vault.

```bash
python main.py transcribe ~/Downloads/my-meeting.mov            # stdout only
python main.py transcribe ~/Downloads/my-meeting.mov --write    # + vault file
```

Audio longer than 15 minutes is automatically chunked into 5-minute pieces to
prevent Whisper's known "repetition loop" hallucination on long audio.

### `record`

Capture N seconds from BlackHole 2ch, transcribe, and write to the vault. Use
this for live meetings — start it just before the call.

```bash
python main.py record --seconds 1800                # 30-minute capture
python main.py record --seconds 60 --no-write       # 60-second test, stdout only
python main.py record --device "BlackHole 2ch"      # explicit device choice
```

## Output format

Each meeting becomes one markdown file at
`$OBSIDIAN_VAULT/$OBSIDIAN_MEETINGS_SUBDIR/YYYY-MM-DD-meeting-HHMMSS.md`:

```markdown
---
type: meeting-transcript
date: 2026-05-12
created: 2026-05-12T09:40:03-07:00
duration_sec: 2715
source: /Users/you/Desktop/intro-call.mov
tags:
  - meeting
  - transcript
  - finance-flagged
people:
  - "[[steve-fuchs]]"
---

# Meeting transcript — 2026-05-12 09:40

Hey, [[steve-fuchs|Steve]]. Hey, Eric. How are you doing? …
```

## Wikilinks

When writing a transcript, Toasted Clusters scans these subdirectories of your
vault for `.md` files and auto-links any matches it finds in the transcript:

- `~/vault/people/`     — `alex-jones.md` → `[[alex-jones|Alex Jones]]`
- `~/vault/projects/`   — `fast-forward.md` → `[[fast-forward|Fast Forward]]`
- `~/vault/companies/`  — same pattern

Matching is two-pass:

1. **Multi-word display names** (e.g. "Alex Jones"). Safe, no false positives.
2. **Bare first names** (e.g. "Steve") — only when the first name is unique
   across all registered people. If two registered people share a first name,
   neither gets auto-linked from the bare first name.

The second pass also refuses to link when the next word is capitalized (avoids
linking "Alex" inside "Alex Smith" if only "Alex Jones" is registered).

Matched entities also appear in the YAML frontmatter so Dataview queries pick
them up.

## Finance flag

A simple regex pass tags any transcript that mentions dollar amounts, account
numbers, balances, SSNs, or routing numbers. The tag is `finance-flagged` in
frontmatter. The transcript still lands in the vault — the tag is purely
informational, intended for downstream filtering (e.g. a Dataview query that
hides finance-flagged calls from a shared dashboard).

The detector is intentionally regex-only — no LLM, no PII inference. False
positives are acceptable; missing a hit is not.

## Hard rules / safety

- **Personal meetings only by default.** Don't capture work-product calls
  unless your employer's recording policy explicitly permits it.
- **Know your consent laws.** Many US states (and most EU countries) require
  all-party consent before recording. Get it.
- **Audio never leaves the Mac** in the default `local` mode. If you switch to
  `cloud` mode (Deepgram), the audio uploads to Deepgram's API — opt-in only.

## Configuration

All settings come from `.env` (or the process environment):

| Variable | Default | What it does |
|---|---|---|
| `OBSIDIAN_VAULT` | `~/vault` | Vault root |
| `OBSIDIAN_MEETINGS_SUBDIR` | `meetings` | Subdirectory under the vault |
| `WHISPER_MODEL` | `large-v3` | Whisper model. Smaller = faster, lower quality |
| `TRANSCRIBER_MODE` | `local` | `local` (Whisper MLX) or `cloud` (Deepgram) |
| `DEEPGRAM_API_KEY` | *(empty)* | Required only for `cloud` mode |

## Architecture & known limits

- **Whisper MLX hallucinates** on audio longer than ~15 minutes (repetition
  loops). Pipeline auto-chunks at 5 minutes to avoid this. The trade-off is
  occasional context loss between chunks — for normal conversation this is
  rarely noticeable.
- **No speaker diarization yet.** Output is one wall of text with no
  "Speaker A: / Speaker B:" turns. See [roadmap](#roadmap).
- **No summarization.** Raw transcript only. Pair with your own Obsidian
  workflows (Templater, Dataview, or a separate LLM step) to summarize.
- **Vault writes are raw filesystem.** No audit chain, no envelope signing.
  If you want signed/audited writes, see [Carryall upgrade](#carryall-upgrade-optional).
- **`mlx_models/` directory** in your working directory caches the model
  weights (~3 GB). It's gitignored by default. Move or delete only when you
  want to re-download.

## Testing

```bash
pip install pytest
python -m pytest tests/ -q
```

The unit suite is hardware-free and offline (no model load required).

## Carryall upgrade (optional)

If you need signed/scoped/audited vault writes — multiple agents writing to
shared storage, regulatory exposure, or just genuine multi-user concern —
[Carryall](https://github.com/tronmongoose/carryall) is an IAM-style envelope
library for agent storage. You can layer it on top of this pipeline by
replacing `_obsidian_write()` in `main.py` with a Carryall envelope call.

Out of scope for v0.1.0; documented as a forward path.

## Roadmap

- Speaker diarization via [pyannote-audio](https://github.com/pyannote/pyannote-audio)
- Local Ollama summarization pass (configurable model)
- Long-running record with start/stop hotkey instead of fixed `--seconds`
- Optional Carryall envelope-write backend
- Linux support via [faster-whisper](https://github.com/SYSTRAN/faster-whisper)

Contributions welcome. Open an issue first if it's larger than a typo or a
bug fix so we can align on shape.

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Credits

Built atop:

- [lightning-whisper-mlx](https://github.com/mustafaaljadery/lightning-whisper-mlx) — Apple Silicon Whisper
- [BlackHole](https://github.com/ExistentialAudio/BlackHole) — kernel-level virtual audio
- [sounddevice](https://python-sounddevice.readthedocs.io/) — PortAudio bindings
- [Obsidian](https://obsidian.md/) — the target vault format
- [ffmpeg](https://ffmpeg.org/) — audio normalization & chunking

The chunking strategy (5-min pieces above 15-min threshold) is borrowed from a
private long-running pipeline that learned it the hard way.
