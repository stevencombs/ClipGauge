# Install plan — Ollama + whisper.cpp + ffmpeg (macOS Apple Silicon)

Target tree: `/Volumes/Lexar/AI-Video-Renamer`

## Prerequisites check

```bash
# Homebrew
which brew || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# ffmpeg (expected at Homebrew path)
/opt/homebrew/bin/ffmpeg -version
/opt/homebrew/bin/ffprobe -version

# Lexar mounted
df -h /Volumes/Lexar
ls /Volumes/Lexar/AI-Video-Renamer
```

## 1. Install Ollama (if missing)

**Option A — Homebrew (preferred if brew already works):**

```bash
brew install ollama
# or: brew install --cask ollama
```

**Option B — Official installer:** https://ollama.com/download/mac

Start the app once so the `ollama` CLI is on PATH, or:

```bash
brew services start ollama   # if installed via brew formula
# else open Ollama.app from Applications
```

Verify:

```bash
ollama --version
```

## 2. Point OLLAMA_MODELS at Lexar (ExFAT — no symlinks)

Models must live in the portable tree:

```text
/Volumes/Lexar/AI-Video-Renamer/models
```

**Shell session / profile** (add to `~/.zprofile` or `~/.zshrc`):

```bash
# AI Video Renamer — only when Lexar is mounted
if [ -d /Volumes/Lexar/AI-Video-Renamer/models ]; then
  export OLLAMA_MODELS="/Volumes/Lexar/AI-Video-Renamer/models"
fi
```

Or run the helper (does not pull models):

```bash
source /Volumes/Lexar/AI-Video-Renamer/scripts/setup-ollama-env.sh
```

**launchd / Ollama.app:** If the GUI app ignores shell env, set the variable in a LaunchAgent that wraps `ollama serve`, or quit Ollama.app and start:

```bash
export OLLAMA_MODELS="/Volumes/Lexar/AI-Video-Renamer/models"
ollama serve
```

Do **not** create symlinks under the Lexar tree (ExFAT).

## 3. Pull Air-tier model (REQUIRES APPROVAL — multi-GB download)

Only after Lexar is mounted and `OLLAMA_MODELS` is set:

```bash
export OLLAMA_MODELS="/Volumes/Lexar/AI-Video-Renamer/models"
ollama pull qwen2.5vl:7b
# optional fallback:
# ollama pull qwen2.5vl:3b
```

Pro machines later: `ollama pull gemma3:12b` (or stronger).

**Do not pull** until Steven explicitly approves the download (size / disk / time).

## 4. Verify ffmpeg

```bash
test -x /opt/homebrew/bin/ffmpeg && echo "ffmpeg OK"
/opt/homebrew/bin/ffmpeg -version | head -1
```

If missing: `brew install ffmpeg`

## 5. whisper.cpp speech-to-text (optional, REQUIRES APPROVAL — downloads)

Run **after** the `qwen2.5vl:7b` pull has finished (the script refuses while an `ollama pull` is running unless `--force`):

```bash
cd /Volumes/Lexar/AI-Video-Renamer
scripts/setup-whisper.sh --plan     # show what it would do (no network)
scripts/setup-whisper.sh            # brew install whisper-cpp + tier model → models/whisper/
# options: --model small.en | --skip-brew | --force
```

- Installs the Homebrew formula `whisper.cpp` (renamed from `whisper-cpp`; also pulls `ggml`, `libomp`, `llama.cpp` as deps) (provides `whisper-cli`, Metal-accelerated).
- Downloads `ggml-<model>.bin` from `huggingface.co/ggerganov/whisper.cpp` into `models/whisper/` via `curl -C -` to a `.part` file (resumable — just re-run after a Wi-Fi drop), size-checked, then renamed.
- Tier models: Air → `base.en` (~142 MB); Pro → `large-v3-turbo` (~1.6 GB). Change in `config.json` → `whisper.model`.
- Until this is done the pipeline simply skips transcription with a clear message.

Verify: `python3 scripts/transcribe.py "<clip>"`

## 6. Smoke test (dry-run only)

```bash
cd /Volumes/Lexar/AI-Video-Renamer
python3 scripts/detect_ram.py
python3 scripts/check_resolve.py
python3 scripts/selftest.py -v      # offline unit tests
export OLLAMA_MODELS="/Volumes/Lexar/AI-Video-Renamer/models"
ollama list                          # must show qwen2.5vl:7b
python3 scripts/run_pipeline.py --video "/Volumes/Lexar/DaVinci Resolve/E-Reader Review/x4 unboxing.MP4" --reuse-frames --skip-whisper
# or copy ONE small .MP4 into inbox/ and run: python3 scripts/run_pipeline.py
```

Expect: Resolve guard, frames in `processing/`, transcription (or a skip message), VLM description, proposed filename, sidecars in `logs/dry-run/<folder>/`, a line in `logs/dry-run/report.jsonl`, status.json updates with ETA. First describe call includes model load from the Lexar drive (slower). No renames while `dry_run` is true.

## Checklist

- [ ] brew available
- [ ] ffmpeg at `/opt/homebrew/bin/ffmpeg`
- [ ] Ollama installed
- [ ] `OLLAMA_MODELS` → Lexar `models/`
- [ ] `ollama pull qwen2.5vl:7b` (approved)
- [ ] `python3 scripts/selftest.py` passes
- [ ] `scripts/setup-whisper.sh` (approved; after the Ollama pull)
- [ ] `run_pipeline.py` dry-run OK → sidecars in `logs/dry-run/`
