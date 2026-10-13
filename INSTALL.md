# Install plan — Ollama + whisper.cpp + ffmpeg (macOS Apple Silicon)

Target tree: `<your drive>/AI-Video-Renamer` (any internal, external or network drive; see *Choosing a location* below)

## Choosing a location

ClipGauge's `AI-Video-Renamer/` folder can live on **any drive**: the Mac's internal disk, an external SSD (USB/Thunderbolt), or a network share (SMB/NAS). No particular brand is required. Pick a location with:

- **Space:** ~10 GB for the AI models, plus room for your clips.
- **Speed:** a fast local or external SSD is best; models load and frames extract much faster than over Wi-Fi. Network drives work but are slower and must stay mounted during a run.
- **Portability:** put it on an external drive if you want to move it between Macs without reinstalling models.

Throughout these docs, `<your drive>/AI-Video-Renamer` stands for wherever you put it, e.g. `~/AI-Video-Renamer` (internal), `/Volumes/<DriveName>/AI-Video-Renamer` (external or network). Choose or create it in **Setup › Choose or Create…**, or set `AI_VIDEO_RENAMER_ROOT`.

**exFAT drives:** don't create symlinks inside the project; exFAT can't store them.

### Recommended drive for Mac laptops

For a MacBook, a compact USB-C SSD keeps the project portable and fast: [recommended drive](https://amzn.to/4ihwdRF).
*Affiliate link: I may earn a commission at no cost to you.*


## Prerequisites check

```bash
# Homebrew
which brew || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# ffmpeg (expected at Homebrew path)
/opt/homebrew/bin/ffmpeg -version
/opt/homebrew/bin/ffprobe -version

# project drive available
df -h "<your drive>"
ls "<your drive>/AI-Video-Renamer"
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

## 2. Point OLLAMA_MODELS at the project's models/ folder (no symlinks)

Models must live in the portable tree:

```text
<your drive>/AI-Video-Renamer/models
```

**Shell session / profile** (add to `~/.zprofile` or `~/.zshrc`):

```bash
# AI Video Renamer — only when the project drive is available
if [ -d "<your drive>/AI-Video-Renamer/models" ]; then
  export OLLAMA_MODELS="<your drive>/AI-Video-Renamer/models"
fi
```

Or run the helper (does not pull models):

```bash
source "<your drive>/AI-Video-Renamer/scripts/setup-ollama-env.sh"
```

**launchd / Ollama.app:** If the GUI app ignores shell env, set the variable in a LaunchAgent that wraps `ollama serve`, or quit Ollama.app and start:

```bash
export OLLAMA_MODELS="<your drive>/AI-Video-Renamer/models"
ollama serve
```

Do **not** create symlinks inside the project tree (exFAT drives can't store them).

## 3. Pull Air-tier model (REQUIRES APPROVAL — multi-GB download)

Only after the project drive is available and `OLLAMA_MODELS` is set:

```bash
export OLLAMA_MODELS="<your drive>/AI-Video-Renamer/models"
ollama pull qwen2.5vl:7b
# optional fallback:
# ollama pull qwen2.5vl:3b
```

Pro machines later: `ollama pull gemma3:12b` (or stronger).

**Do not pull** until you've checked the download size, free disk space and time.

## 4. Verify ffmpeg

```bash
test -x /opt/homebrew/bin/ffmpeg && echo "ffmpeg OK"
/opt/homebrew/bin/ffmpeg -version | head -1
```

If missing: `brew install ffmpeg`

## 5. whisper.cpp speech-to-text (optional, REQUIRES APPROVAL — downloads)

Run **after** the `qwen2.5vl:7b` pull has finished (the script refuses while an `ollama pull` is running unless `--force`):

```bash
cd "<your drive>/AI-Video-Renamer"
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
cd "<your drive>/AI-Video-Renamer"
python3 scripts/detect_ram.py
python3 scripts/check_resolve.py
python3 scripts/selftest.py -v      # offline unit tests
export OLLAMA_MODELS="<your drive>/AI-Video-Renamer/models"
ollama list                          # must show qwen2.5vl:7b
python3 scripts/run_pipeline.py --video "<your drive>/DaVinci Resolve/E-Reader Review/x4 unboxing.MP4" --reuse-frames --skip-whisper
# or copy ONE small .MP4 into inbox/ and run: python3 scripts/run_pipeline.py
```

Expect: Resolve guard, frames in `processing/`, transcription (or a skip message), VLM description, proposed filename, sidecars in `logs/dry-run/<folder>/`, a line in `logs/dry-run/report.jsonl`, status.json updates with ETA. First describe call includes model load from the project drive (slower). No renames while `dry_run` is true.

## Checklist

- [ ] brew available
- [ ] ffmpeg at `/opt/homebrew/bin/ffmpeg`
- [ ] Ollama installed
- [ ] `OLLAMA_MODELS` → the project's `models/`
- [ ] `ollama pull qwen2.5vl:7b` (approved)
- [ ] `python3 scripts/selftest.py` passes
- [ ] `scripts/setup-whisper.sh` (approved; after the Ollama pull)
- [ ] `run_pipeline.py` dry-run OK → sidecars in `logs/dry-run/`
