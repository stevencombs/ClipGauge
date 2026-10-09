#!/bin/zsh
# AI Video Renamer / ClipGauge — guided setup for a Mac (Apple silicon or Intel, macOS 14+).
#
#   scripts/setup-mac.sh            # check, then offer each missing piece one at a time (asks before every install)
#   scripts/setup-mac.sh --check    # only report what's installed; changes nothing
#   scripts/setup-mac.sh --root DIR # project folder (default: the folder that holds this scripts/ directory)
#
# Nothing is installed without a "y" from you. Homebrew's own installer asks for your password itself.
# Large downloads (vision model ~6 GB, whisper model 0.1–1.6 GB) are separate questions, so you can skip them on
# a slow connection and re-run this script later. Everything runs locally; no clip ever leaves this Mac.
set -u
CHECK_ONLY=0
ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --root) ROOT="${2:?--root needs a folder}"; shift 2 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -z "$ROOT" ] && ROOT="${AI_VIDEO_RENAMER_ROOT:-${0:A:h:h}}"
ROOT="${ROOT:A}"

bold=$'\e[1m'; dim=$'\e[2m'; green=$'\e[32m'; red=$'\e[31m'; yellow=$'\e[33m'; off=$'\e[0m'
ok()   { print -r -- "  ${green}✓${off} $1"; }
miss() { print -r -- "  ${red}✗${off} $1"; }
warn() { print -r -- "  ${yellow}!${off} $1"; }
hdr()  { print -r -- ""; print -r -- "${bold}$1${off}"; }
ask()  { [ "$CHECK_ONLY" -eq 1 ] && return 1; local a; read "a?$1 [y/N] "; [[ "$a" == [yY]* ]]; }

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
BREW="$(command -v brew || true)"
PY="$(command -v python3 || true)"

print -r -- "${bold}AI Video Renamer — setup${off}  ${dim}(project: $ROOT)${off}"
[ "$CHECK_ONLY" -eq 1 ] && print -r -- "${dim}Check only — nothing will be changed.${off}"

if [ ! -f "$ROOT/scripts/run_pipeline.py" ]; then
  miss "No project at $ROOT (scripts/run_pipeline.py missing). Create one from ClipGauge › Setup first."
  exit 1
fi
mkdir -p "$ROOT"/{inbox,logs,processing,notes,exports,models/whisper} 2>/dev/null || true

# 1) Homebrew --------------------------------------------------------------------------------------------
hdr "1. Homebrew (package manager)"
if [ -n "$BREW" ]; then
  ok "Homebrew: $BREW"
else
  miss "Homebrew is not installed."
  print -r -- "    It installs ffmpeg, Ollama, whisper.cpp and Python. Official installer: https://brew.sh"
  if ask "    Run the official Homebrew installer now? (it will ask for your Mac password)"; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)"
    [ -x /usr/local/bin/brew ] && eval "$(/usr/local/bin/brew shellenv)"
    BREW="$(command -v brew || true)"
  fi
fi

# 2) Tools ------------------------------------------------------------------------------------------------
hdr "2. Tools (ffmpeg, Ollama, whisper.cpp, Python 3)"
typeset -a missing
have() { command -v "$1" >/dev/null 2>&1; }
if have ffmpeg && have ffprobe; then ok "ffmpeg: $(command -v ffmpeg)"; else miss "ffmpeg / ffprobe"; missing+=(ffmpeg); fi
if have ollama || [ -d /Applications/Ollama.app ]; then ok "Ollama: $(command -v ollama || echo /Applications/Ollama.app)"; else miss "Ollama"; missing+=(ollama); fi
if have whisper-cli; then ok "whisper.cpp: $(command -v whisper-cli)"; else miss "whisper.cpp (whisper-cli)"; missing+=(whisper-cpp); fi
pyok=0
if [ -n "$PY" ] && "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  ok "Python: $("$PY" -c 'import sys;print(sys.version.split()[0])') ($PY)"; pyok=1
else
  miss "Python 3.10 or newer"; missing+=(python)
fi
if (( ${#missing} )); then
  if [ -z "$BREW" ]; then
    warn "Install Homebrew first (step 1), then re-run this script."
  elif ask "    Install with Homebrew: ${missing[*]} ?"; then
    "$BREW" install "${missing[@]}"
    PY="$(command -v python3 || true)"
  fi
fi

# 3) Ollama server with models stored in the project ------------------------------------------------------
hdr "3. Ollama server (models live in $ROOT/models)"
MODELS="$ROOT/models"
if curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  ok "Ollama is running on 127.0.0.1:11434 ($(curl -s -m 3 http://127.0.0.1:11434/api/version))"
  warn "Make sure it was started with OLLAMA_MODELS=$MODELS, or models download to ~/.ollama instead."
else
  miss "Ollama is not running."
  LABEL="com.clipgauge.ollama"
  AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
  if [ -f "$HOME/Library/LaunchAgents/com.clipguage.ollama.plist" ]; then  # made by ClipGuage <= v0.5: reuse, no duplicate
    LABEL="com.clipguage.ollama"; AGENT="$HOME/Library/LaunchAgents/$LABEL.plist"
  fi
  OLLAMA_BIN="$(command -v ollama || true)"
  if [ -n "$OLLAMA_BIN" ] && ask "    Create a login item that runs 'ollama serve' with OLLAMA_MODELS=$MODELS ($AGENT)?"; then
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$OLLAMA_BIN</string><string>serve</string></array>
  <key>EnvironmentVariables</key><dict><key>OLLAMA_MODELS</key><string>$MODELS</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/clipgauge-ollama.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/clipgauge-ollama.log</string>
</dict></plist>
PLIST
    launchctl bootstrap "gui/$(id -u)" "$AGENT" 2>/dev/null || launchctl load "$AGENT"
    sleep 3
    curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null && ok "Ollama started." || warn "Ollama didn't answer yet — see ~/Library/Logs/clipgauge-ollama.log"
  fi
fi

# 4) Models -----------------------------------------------------------------------------------------------
hdr "4. AI models (large downloads)"
TIER_JSON="$("${PY:-python3}" "$ROOT/scripts/detect_ram.py" --json 2>/dev/null || echo '{}')"
jget() { print -r -- "$TIER_JSON" | "${PY:-python3}" -c "import json,sys;print(json.load(sys.stdin).get('$1') or '')" 2>/dev/null; }
TIER="$(jget tier)"; VLM="$(jget prefer_model)"; WMODEL="$(jget whisper_model)"; RAM="$(jget ram_gb_binary)"
[ -n "$TIER" ] && ok "This Mac: ${RAM} GB RAM → ${TIER} tier (vision: $VLM, speech: whisper $WMODEL)"
if [ -n "$VLM" ]; then
  name="${VLM%%:*}"; tag="${VLM#*:}"
  # Ollama ≤ 0.39: manifests/registry.ollama.ai/…; Ollama 0.40+: manifests-v2/ollama.com/… (a symlink to a blob)
  if [ -f "$MODELS/manifests-v2/ollama.com/library/$name/$tag" ] || [ -f "$MODELS/manifests-v2/registry.ollama.ai/library/$name/$tag" ] \
     || [ -f "$MODELS/manifests/registry.ollama.ai/library/$name/$tag" ]; then
    ok "Vision model $VLM is in $MODELS"
  else
    miss "Vision model $VLM not in $MODELS"
    if curl -s -m 3 http://127.0.0.1:11434/api/version >/dev/null 2>&1 && ask "    Download $VLM now (several GB, through the running Ollama server)?"; then
      ollama pull "$VLM"
    fi
  fi
fi
if [ -n "$WMODEL" ]; then
  if [ -s "$MODELS/whisper/ggml-$WMODEL.bin" ]; then
    ok "Whisper model ggml-$WMODEL.bin is in $MODELS/whisper"
  else
    miss "Whisper model ggml-$WMODEL.bin not in $MODELS/whisper"
    if ask "    Download it now (resumable)?"; then
      AI_VIDEO_RENAMER_ROOT="$ROOT" bash "$ROOT/scripts/setup-whisper.sh" --skip-brew --force
    fi
  fi
fi

# 5) Done -------------------------------------------------------------------------------------------------
hdr "5. Next"
print -r -- "  • Back in ClipGauge, open Setup and click Re-check."
print -r -- "  • New projects start in dry-run mode (nothing is renamed). Turn it off in config/config.json (\"dry_run\": false)"
print -r -- "    once the proposed names look right."
print -r -- "  • Guide: $ROOT/SETUP.md"
[ "$CHECK_ONLY" -eq 0 ] && { print -r -- ""; read "?Press Return to close…"; }
exit 0
