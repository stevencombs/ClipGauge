#!/bin/bash
# Install whisper.cpp (Homebrew formula `whisper.cpp`, formerly `whisper-cpp`, provides `whisper-cli`) and download the
# tier's ggml model into the portable Lexar tree: models/whisper/ggml-<model>.bin
#
#   Air (16 GB) -> base.en (~142 MB)      Pro (64 GB) -> large-v3-turbo (~1.6 GB)
#   (config.json whisper.model per tier; override with --model NAME or AI_VIDEO_RENAMER_WHISPER_MODEL)
#
# Resumable: downloads to <file>.part with `curl -C -` and retries; re-run after a drop.
# Downloads things — run only when approved and AFTER `ollama pull qwen2.5vl:7b` has finished.
#
# Usage:
#   scripts/setup-whisper.sh                 # brew install + tier model
#   scripts/setup-whisper.sh --model small.en
#   scripts/setup-whisper.sh --skip-brew     # model only
#   scripts/setup-whisper.sh --plan          # print what would happen; no network, no installs
#   scripts/setup-whisper.sh --force         # run even if an `ollama pull` is still in progress
set -euo pipefail

# Project root = the folder holding scripts/ (portable); AI_VIDEO_RENAMER_ROOT overrides.
ROOT="${AI_VIDEO_RENAMER_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
WHISPER_DIR="$ROOT/models/whisper"
BASE_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main"
MODEL=""
SKIP_BREW=0
PLAN=0
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL="${2:?--model needs a name}"; shift 2 ;;
    --skip-brew) SKIP_BREW=1; shift ;;
    --plan|--dry-run) PLAN=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "=== AI Video Renamer — whisper.cpp setup ==="
if [ ! -d "$ROOT" ]; then
  echo "ERROR: project folder not found at $ROOT" >&2
  exit 1
fi

# Base URL from config (if present)
CFG_URL=$(/usr/bin/env python3 -c "import json;print((json.load(open('$ROOT/config/config.json')).get('whisper') or {}).get('download_base_url') or '')" 2>/dev/null || true)
[ -n "$CFG_URL" ] && BASE_URL="$CFG_URL"

if [ -z "$MODEL" ]; then
  MODEL=$(/usr/bin/env python3 "$ROOT/scripts/transcribe.py" --print-model)
fi
FILE="ggml-${MODEL%.bin}.bin"
FILE="${FILE/ggml-ggml-/ggml-}"
DEST="$WHISPER_DIR/$FILE"
URL="$BASE_URL/$FILE"
BREW="$(command -v brew || echo /opt/homebrew/bin/brew)"
CLI="$(command -v whisper-cli || true)"
[ -z "$CLI" ] && [ -x /opt/homebrew/bin/whisper-cli ] && CLI=/opt/homebrew/bin/whisper-cli

echo "Tier model: $MODEL"
echo "Model file: $DEST"
echo "URL:        $URL"
echo "whisper-cli: ${CLI:-not installed}"

if pgrep -f "ollama pull" >/dev/null 2>&1; then
  echo "NOTE: an 'ollama pull' is still running (shares the slow link)."
  if [ "$PLAN" -eq 0 ] && [ "$FORCE" -eq 0 ]; then
    echo "Refusing to start downloads until it finishes (or pass --force)." >&2
    exit 3
  fi
fi

if [ "$PLAN" -eq 1 ]; then
  echo
  echo "PLAN (nothing executed):"
  if [ "$SKIP_BREW" -eq 0 ] && [ -z "$CLI" ]; then echo "  $BREW install whisper.cpp"; else echo "  (skip brew: whisper-cli present or --skip-brew)"; fi
  if [ -s "$DEST" ]; then echo "  (model already present: $DEST)"; else echo "  curl -L -C - --retry 10 -o '$DEST.part' '$URL' && mv '$DEST.part' '$DEST'"; fi
  exit 0
fi

# 1) whisper.cpp via Homebrew
if [ "$SKIP_BREW" -eq 0 ]; then
  if [ -n "$CLI" ]; then
    echo "whisper-cli already installed: $CLI"
  else
    echo "Installing whisper.cpp via Homebrew…"
    "$BREW" install whisper.cpp
    CLI="$(command -v whisper-cli || echo /opt/homebrew/bin/whisper-cli)"
  fi
fi

# 2) ggml model (resumable)
mkdir -p "$WHISPER_DIR"
remote_size() {
  curl -sIL --max-time 30 "$URL" | tr -d '\r' | awk 'tolower($1)=="content-length:"{n=$2} END{print n+0}'
}
if [ -s "$DEST" ]; then
  echo "Model already present: $DEST ($(du -h "$DEST" | cut -f1))"
else
  EXPECT=$(remote_size || echo 0)
  echo "Downloading $FILE (${EXPECT} bytes) → $DEST.part (resumable)…"
  ok=0
  for attempt in $(seq 1 30); do
    if curl -L --fail --retry 10 --retry-delay 5 --retry-all-errors --connect-timeout 20 \
         -C - -o "$DEST.part" "$URL"; then
      ok=1; break
    fi
    # 416 = already complete (range past EOF)
    if [ "$EXPECT" -gt 0 ] && [ "$(stat -f %z "$DEST.part" 2>/dev/null || echo 0)" -ge "$EXPECT" ]; then ok=1; break; fi
    echo "curl attempt $attempt failed; retrying in 10s (partial kept)…"
    sleep 10
  done
  [ "$ok" -eq 1 ] || { echo "ERROR: download failed; re-run to resume ($DEST.part)" >&2; exit 4; }
  GOT=$(stat -f %z "$DEST.part")
  if [ "$EXPECT" -gt 0 ] && [ "$GOT" -ne "$EXPECT" ]; then
    echo "ERROR: size mismatch (got $GOT, expected $EXPECT); re-run to resume" >&2
    exit 5
  fi
  mv "$DEST.part" "$DEST"
  echo "Model ready: $DEST ($(du -h "$DEST" | cut -f1))"
fi

# 3) smoke check (no audio needed)
if [ -n "${CLI:-}" ] && [ -x "$CLI" ]; then
  "$CLI" --help >/dev/null 2>&1 && echo "whisper-cli OK: $CLI"
else
  echo "WARNING: whisper-cli not found — run without --skip-brew" >&2
fi
echo
echo "Next: python3 $ROOT/scripts/transcribe.py '<clip>'   (or run_pipeline.py — transcription is now automatic)"
