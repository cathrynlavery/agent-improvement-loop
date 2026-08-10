#!/bin/bash
# Local, proposal-only fleet scan. Raw transcripts never leave this Mac.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

LOOP_ROOT="${AGENT_IMPROVEMENT_ROOT:-$HOME/.local/share/agent-improvement-loop}"
OUTPUT_ROOT="${AGENT_IMPROVEMENT_OUTPUT_ROOT:-$HOME/.agent-improvement}"
RUN_LOG="$HOME/Library/Logs/agent-improvement-scan-runs.log"
DETAIL_LOG="$HOME/Library/Logs/agent-improvement-scan.log"
MACHINE="${AGENT_IMPROVEMENT_MACHINE:-$(scutil --get LocalHostName 2>/dev/null || hostname -s)}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

touch "$RUN_LOG" "$DETAIL_LOG"
if [ ! -x "$LOOP_ROOT/bin/daily-improvement-loop" ]; then
  printf 'RUN %s machine=%s exit=missing-scanner\n' "$NOW" "$MACHINE" >> "$RUN_LOG"
  exit 0
fi

scan_output="$(
  "$LOOP_ROOT/bin/daily-improvement-loop" \
    --home "$HOME" \
    --output-root "$OUTPUT_ROOT" \
    --machine "$MACHINE" \
    --source "${AGENT_IMPROVEMENT_SOURCE:-all}" \
    --since-days "${AGENT_IMPROVEMENT_SINCE_DAYS:-1.25}" \
    --max-sessions "${AGENT_IMPROVEMENT_MAX_SESSIONS:-2000}" 2>&1
)"
scan_exit=$?
printf '=== %s machine=%s ===\n%s\n' "$NOW" "$MACHINE" "$scan_output" >> "$DETAIL_LOG"
summary="$(printf '%s\n' "$scan_output" | grep -E '^(machine=|review_packet=|fleet_bundle=)' | tr '\n' ' ')"
printf 'RUN %s machine=%s exit=%s %s\n' "$NOW" "$MACHINE" "$scan_exit" "${summary:-no-summary}" >> "$RUN_LOG"
exit "$scan_exit"
