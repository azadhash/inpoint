#!/usr/bin/env bash
# One-time setup. Safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!!\033[0m %s\n' "$*"; }

say "Checking prerequisites"
if ! command -v uv >/dev/null 2>&1; then
  warn "uv is not installed. Run:  brew install uv"
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  warn "ffmpeg is not installed. Run:  brew install ffmpeg"
  exit 1
fi

say "Creating the virtualenv (Python 3.12)"
uv venv --python 3.12 --allow-existing

say "Installing dependencies (first run downloads ~300 MB)"
uv sync

if [ ! -f .env ]; then
  say "Writing .env from .env.example"
  cp .env.example .env
fi

if [ "${1:-}" = "--link-skill" ]; then
  say "Linking the jira-assets skill into ~/.claude/skills"
  mkdir -p "$HOME/.claude/skills"
  ln -sfn "$PWD/.claude/skills/jira-assets" "$HOME/.claude/skills/jira-assets"
  echo "    linked: $HOME/.claude/skills/jira-assets -> $PWD/.claude/skills/jira-assets"
fi

say "Running doctor"
uv run asst doctor || true

cat <<'EOF'

Ready. Try:
  ./asst doctor
  ./asst serve --open                     # drag-and-drop transcribe + frames UI
  ./asst jira open ABC-123                # start a ticket harvest

The Whisper model (~1.5 GB) downloads on your first transcription.
EOF
