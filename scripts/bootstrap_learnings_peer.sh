#!/bin/bash
# One-shot, retry-safe bootstrap for a peer that may be offline when the fleet is installed.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

REMOTE_HOST="${1:?usage: bootstrap_learnings_peer.sh <host> <machine> [writer] [harvest-minute]}"
MACHINE="${2:?usage: bootstrap_learnings_peer.sh <host> <machine> [writer] [harvest-minute]}"
ROLE="${3:-writer}"
HARVEST_MINUTE="${4:-30}"
LOOP_ROOT="${AGENT_IMPROVEMENT_ROOT:-$HOME/.local/share/agent-improvement-loop}"
LEARNINGS_ROOT="${AGENT_LEARNINGS_ROOT:-$HOME/.agents/learnings}"
LEGACY_SOURCE="${AGENT_LEARNINGS_LEGACY_SOURCE:-}"
MARKER="$LEARNINGS_ROOT/fleet/bootstrap-$MACHINE.complete"
RUN_LOG="$HOME/Library/Logs/learnings-bootstrap-$MACHINE.log"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$LEARNINGS_ROOT/fleet" "$HOME/Library/Logs"
touch "$RUN_LOG"
if [ -f "$MARKER" ]; then
  exit 0
fi
case "$REMOTE_HOST" in
  -*|*[!A-Za-z0-9._-]*)
    printf 'RUN %s exit=invalid-host host=%s\n' "$NOW" "$REMOTE_HOST" >> "$RUN_LOG"
    exit 2
    ;;
esac
case "$MACHINE" in
  ''|*[!a-z0-9-]*)
    printf 'RUN %s exit=invalid-machine machine=%s\n' "$NOW" "$MACHINE" >> "$RUN_LOG"
    exit 2
    ;;
esac
if [ "$ROLE" != "writer" ]; then
  printf 'RUN %s exit=invalid-role role=%s\n' "$NOW" "$ROLE" >> "$RUN_LOG"
  exit 2
fi
case "$HARVEST_MINUTE" in
  ''|*[!0-9]*)
    printf 'RUN %s exit=invalid-minute minute=%s\n' "$NOW" "$HARVEST_MINUTE" >> "$RUN_LOG"
    exit 2
    ;;
esac
if [ "$HARVEST_MINUTE" -gt 59 ]; then
  printf 'RUN %s exit=invalid-minute minute=%s\n' "$NOW" "$HARVEST_MINUTE" >> "$RUN_LOG"
  exit 2
fi

if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "$REMOTE_HOST" true >> "$RUN_LOG" 2>&1; then
  printf 'RUN %s exit=peer-unavailable host=%s\n' "$NOW" "$REMOTE_HOST" >> "$RUN_LOG"
  exit 0
fi

ssh "$REMOTE_HOST" 'mkdir -p "$HOME/.local/share/agent-improvement-loop" "$HOME/.local/bin" "$HOME/.agents/skills/self-improving-agent"' >> "$RUN_LOG" 2>&1 || exit 1
rsync -a --exclude='.git/' --exclude='__pycache__/' --exclude='.DS_Store' \
  "$LOOP_ROOT/" "$REMOTE_HOST:.local/share/agent-improvement-loop/" >> "$RUN_LOG" 2>&1 || exit 1
rsync -a "$LOOP_ROOT/bin/learnings" "$REMOTE_HOST:.local/bin/learnings" >> "$RUN_LOG" 2>&1 || exit 1
if [ -d "$HOME/.agents/skills/self-improving-agent" ]; then
  rsync -a "$HOME/.agents/skills/self-improving-agent/" \
    "$REMOTE_HOST:.agents/skills/self-improving-agent/" >> "$RUN_LOG" 2>&1 || exit 1
fi
if [ -f "$HOME/.agents/.gitignore" ]; then
  rsync -a "$HOME/.agents/.gitignore" "$REMOTE_HOST:.agents/.gitignore" >> "$RUN_LOG" 2>&1 || exit 1
fi

ssh "$REMOTE_HOST" /bin/bash -s -- \
  "$MACHINE" "$ROLE" "$HARVEST_MINUTE" "$LEGACY_SOURCE" \
  >> "$RUN_LOG" 2>&1 <<'REMOTE_BOOTSTRAP'
set -u
machine="$1"
role="$2"
harvest_minute="$3"
legacy_source="$4"
loop_root="$HOME/.local/share/agent-improvement-loop"
store="$HOME/.agents/learnings"
case "$legacy_source" in
  '') legacy='' ;;
  /*) legacy="$legacy_source" ;;
  *) legacy="$HOME/$legacy_source" ;;
esac

if [ ! -f "$store/config.json" ] && [ -n "$legacy" ] && [ -d "$legacy" ]; then
  "$loop_root/bin/learnings-store" --root "$store" migrate \
    --machine "$machine" --source "$legacy" --role "$role"
else
  "$loop_root/bin/learnings-store" --root "$store" init \
    --machine "$machine" --role "$role"
fi
"$loop_root/bin/learnings-store" --root "$store" normalize --machine "$machine"
python3 "$loop_root/scripts/update_learnings_instruction.py" "$HOME/.codex/AGENTS.md"

uid="$(id -u)"
if launchctl print "gui/$uid" >/dev/null 2>&1; then
  launch_label="io.agent-improvement-loop.learnings-harvest"
  python3 "$loop_root/scripts/install_learnings_launchd.py" \
    --home "$HOME" --role "$role" --harvest-minute "$harvest_minute"
  launchctl bootout "gui/$uid/$launch_label" 2>/dev/null || true
  launchctl enable "gui/$uid/$launch_label"
  launchctl bootstrap "gui/$uid" \
    "$HOME/Library/LaunchAgents/$launch_label.plist"
else
  python3 "$loop_root/scripts/install_learnings_cron.py" \
    --home "$HOME" --role "$role"
fi

AGENT_LEARNINGS_LIVENESS_ONLY=1 \
  "$loop_root/templates/learnings-harvest-run.sh"
REMOTE_BOOTSTRAP
bootstrap_exit=$?
if [ "$bootstrap_exit" -ne 0 ]; then
  printf 'RUN %s exit=bootstrap-failed code=%s host=%s\n' \
    "$NOW" "$bootstrap_exit" "$REMOTE_HOST" >> "$RUN_LOG"
  exit "$bootstrap_exit"
fi

printf 'host=%s machine=%s role=%s completed_at=%s\n' \
  "$REMOTE_HOST" "$MACHINE" "$ROLE" "$NOW" > "$MARKER"
printf 'RUN %s exit=0 host=%s machine=%s role=%s\n' \
  "$NOW" "$REMOTE_HOST" "$MACHINE" "$ROLE" >> "$RUN_LOG"
