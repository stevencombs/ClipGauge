#!/bin/bash
# Configure OLLAMA_MODELS for the portable Lexar tree. Does NOT install Ollama or pull models.
set -euo pipefail

# Project root = the folder holding scripts/ (portable; works when sourced from bash or zsh). AI_VIDEO_RENAMER_ROOT overrides.
_here="${BASH_SOURCE[0]:-${(%):-%x}}"
ROOT="${AI_VIDEO_RENAMER_ROOT:-$(cd "$(dirname "$_here")/.." && pwd)}"
MODELS_DIR="$ROOT/models"

echo "=== AI Video Renamer — Ollama env setup ==="

if [ ! -d "$ROOT/scripts" ]; then
  echo "ERROR: project folder not found at $ROOT (is the drive connected?)"
  exit 1
fi

if [ ! -d "$MODELS_DIR" ]; then
  echo "ERROR: models dir missing: $MODELS_DIR"
  exit 1
fi

export OLLAMA_MODELS="$MODELS_DIR"
echo "Exported OLLAMA_MODELS=$OLLAMA_MODELS"
echo "(ExFAT: do not create symlinks into this path.)"

if command -v ollama >/dev/null 2>&1; then
  echo "ollama CLI: $(command -v ollama) ($(ollama --version 2>/dev/null || echo version unknown))"
else
  echo "ollama CLI: NOT FOUND — see INSTALL.md (brew install ollama / official Mac installer)"
fi

if [ -x /opt/homebrew/bin/ffmpeg ]; then
  echo "ffmpeg: OK (/opt/homebrew/bin/ffmpeg)"
else
  echo "ffmpeg: MISSING — brew install ffmpeg"
fi

echo
echo "Next steps (large downloads — run when you are on a fast connection):"
echo "  1. Install Ollama if missing (INSTALL.md)"
echo "  2. Ensure this shell (or ollama serve LaunchAgent) has OLLAMA_MODELS set"
echo "  3. ollama pull qwen2.5vl:7b   # Air tier — multi-GB; ask before running"
echo "  4. python3 $ROOT/scripts/detect_ram.py"
echo "  5. Copy one test clip into $ROOT/inbox/ then: python3 $ROOT/scripts/run_pipeline.py"
echo
echo "To persist in zsh:"
echo "  echo 'export OLLAMA_MODELS=\"$MODELS_DIR\"' >> ~/.zprofile"
