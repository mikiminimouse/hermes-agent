#!/usr/bin/env bash
# Isolated E2E runner for the google_meet bot (meetlab).
# Usage: run_meet_e2e.sh <meet-url> [duration] [headed]
#   duration: e.g. 120, 5m, 2h  (default 5m)
#   headed:   pass "headed" to show the window on DISPLAY=:10 (default headless)
set -u

URL="${1:?usage: run_meet_e2e.sh <meet-url> [duration] [headed]}"
DUR="${2:-5m}"
MODE_HEADED="${3:-}"

PY=/home/vitaly/.hermes/hermes-agent/venv/bin/python
WT=/home/vitaly/hermes-meetlab
OUT="/home/vitaly/.hermes/profiles/meetlab/workspace/meetings/e2e-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"

export HERMES_MEET_URL="$URL"
export HERMES_MEET_OUT_DIR="$OUT"
export HERMES_MEET_GUEST_NAME="Hermes Agent"
export HERMES_MEET_MODE="transcribe"
export HERMES_MEET_DURATION="$DUR"
# Real-Chrome persistent profile (authed session lives here).
export HERMES_MEET_CHROME_PATH="/home/vitaly/.local/bin/google-chrome-stable"
export HERMES_MEET_USER_DATA_DIR="/home/vitaly/.hermes/browser-profiles/meet-google"
export HERMES_MEET_NO_SANDBOX="1"
# Egress + auth gate: same DE region the session was minted in.
export HERMES_MEET_PROXY="http://127.0.0.1:18888"
export HERMES_MEET_REQUIRE_AUTH="1"
# Caption language: Meet transcribes only the configured language (no
# auto-detect), so set Russian for RU meetings.
export HERMES_MEET_CAPTION_LANG="${HERMES_MEET_CAPTION_LANG:-Russian}"
# Optional UI language for RU/EN testing (e.g. ru-RU). Default: profile native.
[ -n "${HERMES_MEET_LANG:-}" ] && export HERMES_MEET_LANG

if [ "$MODE_HEADED" = "headed" ]; then
  export HERMES_MEET_HEADED="1"
  export DISPLAY=":10"
  echo "mode: HEADED on DISPLAY=:10"
else
  echo "mode: headless"
fi

echo "url:      $URL"
echo "out:      $OUT"
echo "duration: $DUR"
echo "----- starting bot (Ctrl-C to stop early; transcript at \$OUT/transcript.txt) -----"
cd "$WT" && exec "$PY" -m plugins.google_meet.meet_bot
