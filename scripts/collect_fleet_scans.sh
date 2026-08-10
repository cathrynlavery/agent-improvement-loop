#!/bin/bash
# Pull redacted proposal bundles to one configured leader and build a fleet packet.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

LOOP_ROOT="${AGENT_IMPROVEMENT_ROOT:-$HOME/.local/share/agent-improvement-loop}"
OUTPUT_ROOT="${AGENT_IMPROVEMENT_OUTPUT_ROOT:-$HOME/.agent-improvement}"
INBOX="$OUTPUT_ROOT/fleet-inbox"
RUN_LOG="$HOME/Library/Logs/agent-improvement-fleet-collect-runs.log"
DETAIL_LOG="$HOME/Library/Logs/agent-improvement-fleet-collect.log"
LOCAL_MACHINE="${AGENT_IMPROVEMENT_MACHINE:?set AGENT_IMPROVEMENT_MACHINE to the leader machine name}"
REMOTE_OUTPUT_ROOT="${AGENT_IMPROVEMENT_REMOTE_OUTPUT_ROOT:-.agent-improvement}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$INBOX"
touch "$RUN_LOG" "$DETAIL_LOG"
case "$LOCAL_MACHINE" in
  ''|-*|*[!A-Za-z0-9._-]*)
    printf 'RUN %s exit=invalid-local-machine\n' "$NOW" >> "$RUN_LOG"
    exit 2
    ;;
esac

if [ -d "$OUTPUT_ROOT/fleet-outbox/$LOCAL_MACHINE" ]; then
  mkdir -p "$INBOX/$LOCAL_MACHINE"
  rsync -a "$OUTPUT_ROOT/fleet-outbox/$LOCAL_MACHINE/" \
    "$INBOX/$LOCAL_MACHINE/" >> "$DETAIL_LOG" 2>&1 || true
fi

available="$LOCAL_MACHINE"
unavailable=""
for remote_host in ${AGENT_IMPROVEMENT_REMOTE_HOSTS:-}; do
  case "$remote_host" in
    ''|-*|*[!A-Za-z0-9._-]*)
      unavailable="${unavailable}${unavailable:+,}${remote_host:-invalid-host}"
      continue
      ;;
  esac
  mkdir -p "$INBOX/$remote_host"
  if rsync -a --timeout=20 \
    "$remote_host:$REMOTE_OUTPUT_ROOT/fleet-outbox/$remote_host/" \
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
