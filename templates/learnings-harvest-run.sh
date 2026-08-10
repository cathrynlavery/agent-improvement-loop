#!/bin/bash
# Weekly machine-local harvest with liveness outside the data store.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

LOOP_ROOT="${AGENT_IMPROVEMENT_ROOT:-$HOME/.local/share/agent-improvement-loop}"
LEARNINGS_ROOT="${AGENT_LEARNINGS_ROOT:-$HOME/.agents/learnings}"
PROMPT_FILE="${AGENT_LEARNINGS_HARVEST_PROMPT:-$LOOP_ROOT/templates/harvest-prompt.md}"
RUN_LOG="$HOME/Library/Logs/learnings-harvest-runs.log"
DETAIL_LOG="$HOME/Library/Logs/learnings-harvest.log"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
CODEX_BIN="${CODEX_BIN:-$HOME/.local/bin/codex}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$LEARNINGS_ROOT" "$HOME/Library/Logs"
touch "$RUN_LOG" "$DETAIL_LOG" "$LEARNINGS_ROOT/harvest-log.md"
if [ "${AGENT_LEARNINGS_LIVENESS_ONLY:-0}" = "1" ]; then
  "$LOOP_ROOT/bin/learnings-store" --root "$LEARNINGS_ROOT" catalog >> "$DETAIL_LOG" 2>&1
  catalog_exit=$?
  printf 'RUN %s exit=liveness-only catalog=%s\n' "$NOW" "$catalog_exit" >> "$RUN_LOG"
  exit "$catalog_exit"
fi
if [ ! -f "$PROMPT_FILE" ]; then
  printf 'RUN %s exit=missing-runtime\n' "$NOW" >> "$RUN_LOG"
  exit 0
fi

runner="claude"
output=""
runner_exit=127
if [ -x "$CLAUDE_BIN" ]; then
  output="$(
    cd "$HOME" && "$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" \
      --permission-mode acceptEdits \
      --allowedTools "Read" "Grep" "Glob" "Write" "Edit" \
      --max-turns 80 2>&1
  )"
  runner_exit=$?
fi

if [ "$runner_exit" -ne 0 ] && [ -x "$CODEX_BIN" ]; then
  claude_output="$output"
  runner="codex"
  output="$(
    cd "$LEARNINGS_ROOT" && "$CODEX_BIN" exec \
      --ignore-user-config \
      --ephemeral \
      --skip-git-repo-check \
      -C "$LEARNINGS_ROOT" \
      -s workspace-write \
      "$(cat "$PROMPT_FILE")" </dev/null 2>&1
  )"
  runner_exit=$?
  output="=== Claude unavailable ===
$claude_output
=== Codex fallback ===
$output"
fi
printf '=== %s learnings-harvest ===\n%s\n' "$NOW" "$output" >> "$DETAIL_LOG"
"$LOOP_ROOT/bin/learnings-store" --root "$LEARNINGS_ROOT" catalog >> "$DETAIL_LOG" 2>&1
catalog_exit=$?
printf 'RUN %s runner=%s exit=%s catalog=%s\n' \
  "$NOW" "$runner" "$runner_exit" "$catalog_exit" >> "$RUN_LOG"
exit 0
