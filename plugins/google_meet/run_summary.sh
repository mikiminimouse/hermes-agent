#!/usr/bin/env bash
# HERMES_MEET_SUMMARY_CMD target: the bot fires this on graceful meeting end
# with the meeting directory as $1. Thin wrapper that pins the interpreter and
# puts the nvm node (which carries the `codex` CLI) on PATH, then runs the
# post-call summarizer. Logs to <meeting-dir>/summary.log.
#
# Override knobs (env):
#   HERMES_MEET_SUMMARY_PYTHON  - python to run meet_summarize.py
#   HERMES_MEET_NODE_BIN        - dir holding the `codex` binary to add to PATH
set -euo pipefail

MEETING_DIR="${1:?usage: run_summary.sh <meeting-dir>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PY="${HERMES_MEET_SUMMARY_PYTHON:-/home/vitaly/.hermes/hermes-agent/venv/bin/python}"

# Ensure codex is reachable (meet_summarize also has an nvm fallback).
if ! command -v codex >/dev/null 2>&1; then
  NODE_BIN="${HERMES_MEET_NODE_BIN:-}"
  if [ -z "$NODE_BIN" ]; then
    NODE_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -r | head -1 || true)"
  fi
  [ -n "$NODE_BIN" ] && export PATH="$NODE_BIN:$PATH"
fi

exec "$PY" "$HERE/meet_summarize.py" "$MEETING_DIR" \
  >>"$MEETING_DIR/summary.log" 2>&1
