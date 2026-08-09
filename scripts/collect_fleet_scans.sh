#!/bin/bash
# Pull only redacted proposal bundles to Knox and build one fleet packet.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

LOOP_ROOT="${AGENT_IMPROVEMENT_ROOT:-$HOME/.local/share/agent-improvement-loop}"
OUTPUT_ROOT="${AGENT_IMPROVEMENT_OUTPUT_ROOT:-$HOME/.agent-improvement}"
INBOX="$OUTPUT_ROOT/fleet-inbox"
RUN_LOG="$HOME/Library/Logs/agent-improvement-fleet-collect-runs.log"
DETAIL_LOG="$HOME/Library/Logs/agent-improvement-fleet-collect.log"
REMOTE_HOME="${AGENT_IMPROVEMENT_REMOTE_HOME:-/Users/cathrynlavery}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$INBOX"
touch "$RUN_LOG" "$DETAIL_LOG"

if [ -d "$OUTPUT_ROOT/fleet-outbox/knox" ]; then
  mkdir -p "$INBOX/knox"
  rsync -a "$OUTPUT_ROOT/fleet-outbox/knox/" "$INBOX/knox/" >> "$DETAIL_LOG" 2>&1 || true
fi

available="knox"
unavailable=""
for remote_host in ${AGENT_IMPROVEMENT_REMOTE_HOSTS:-silverfox studio mbp}; do
  mkdir -p "$INBOX/$remote_host"
  if rsync -a --timeout=20 \
    "$remote_host:$REMOTE_HOME/.agent-improvement/fleet-outbox/$remote_host/" \
    "$INBOX/$remote_host/" >> "$DETAIL_LOG" 2>&1; then
    available="$available,$remote_host"
  else
    unavailable="${unavailable}${unavailable:+,}$remote_host"
  fi
done

collect_output="$(
  "$LOOP_ROOT/bin/daily-improvement-loop" \
    --output-root "$OUTPUT_ROOT" \
    --fleet-inbox "$INBOX" \
    --collect-fleet 2>&1
)"
collect_exit=$?
printf '=== %s ===\n%s\n' "$NOW" "$collect_output" >> "$DETAIL_LOG"
summary="$(printf '%s\n' "$collect_output" | grep -E '^(fleet_machines=|review_packet=)' | tr '\n' ' ')"
printf 'RUN %s exit=%s available=%s unavailable=%s %s\n' \
  "$NOW" "$collect_exit" "$available" "${unavailable:--}" "${summary:-no-summary}" >> "$RUN_LOG"
exit "$collect_exit"

