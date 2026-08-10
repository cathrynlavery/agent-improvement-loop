#!/bin/bash
# Collect machine-owned learning entries onto one leader and publish the
# generated catalog back to reachable peers. Raw transcripts are never copied.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

LOOP_ROOT="${AGENT_IMPROVEMENT_ROOT:-$HOME/.local/share/agent-improvement-loop}"
LEARNINGS_ROOT="${AGENT_LEARNINGS_ROOT:-$HOME/.agents/learnings}"
REMOTE_HOSTS="${AGENT_LEARNINGS_REMOTE_HOSTS:-}"
RUN_LOG="$HOME/Library/Logs/learnings-collect-runs.log"
DETAIL_LOG="$HOME/Library/Logs/learnings-collect.log"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$LEARNINGS_ROOT/entries" "$LEARNINGS_ROOT/fleet"
touch "$RUN_LOG" "$DETAIL_LOG"

local_machine="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("machine", "unknown"))' "$LEARNINGS_ROOT/config.json" 2>/dev/null || printf unknown)"
available="$local_machine"
remote_available=""
unavailable=""
for remote_host in $REMOTE_HOSTS; do
  mkdir -p "$LEARNINGS_ROOT/entries/$remote_host"
  if rsync -a --ignore-existing --timeout=20 \
    "$remote_host:.agents/learnings/entries/$remote_host/" \
    "$LEARNINGS_ROOT/entries/$remote_host/" >> "$DETAIL_LOG" 2>&1; then
    available="${available}${available:+,}$remote_host"
    remote_available="${remote_available}${remote_available:+ }$remote_host"
  else
    unavailable="${unavailable}${unavailable:+,}$remote_host"
  fi
done

catalog_output="$(
  "$LOOP_ROOT/bin/learnings-store" --root "$LEARNINGS_ROOT" catalog 2>&1
)"
catalog_exit=$?
printf '=== %s ===\n%s\n' "$NOW" "$catalog_output" >> "$DETAIL_LOG"

if [ "$catalog_exit" -eq 0 ]; then
  cp "$LEARNINGS_ROOT/catalog.json" \
    "$LEARNINGS_ROOT/conflicts.json" \
    "$LEARNINGS_ROOT/ACTIVE.md" \
    "$LEARNINGS_ROOT/fleet/" >> "$DETAIL_LOG" 2>&1
fi

for remote_host in $remote_available; do
  ssh -o BatchMode=yes -o ConnectTimeout=8 "$remote_host" \
    'mkdir -p "$HOME/.agents/learnings/fleet"' >> "$DETAIL_LOG" 2>&1 || continue
  rsync -a --timeout=20 \
    "$LEARNINGS_ROOT/catalog.json" \
    "$LEARNINGS_ROOT/conflicts.json" \
    "$LEARNINGS_ROOT/ACTIVE.md" \
    "$remote_host:.agents/learnings/fleet/" >> "$DETAIL_LOG" 2>&1 || true
done

printf 'RUN %s exit=%s available=%s unavailable=%s %s\n' \
  "$NOW" "$catalog_exit" "${available:--}" "${unavailable:--}" \
  "${catalog_output:-no-summary}" >> "$RUN_LOG"
exit "$catalog_exit"
