#!/bin/bash
# Monthly proposal-only review on the fleet leader.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

LOOP_ROOT="${AGENT_IMPROVEMENT_ROOT:-$HOME/.local/share/agent-improvement-loop}"
LEARNINGS_ROOT="${AGENT_LEARNINGS_ROOT:-$HOME/.agents/learnings}"
PROMPT_FILE="${AGENT_LEARNINGS_REVIEW_PROMPT:-$LOOP_ROOT/templates/review-prompt.md}"
RUN_LOG="$HOME/Library/Logs/learnings-review-runs.log"
DETAIL_LOG="$HOME/Library/Logs/learnings-review.log"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$LEARNINGS_ROOT/reviews" "$HOME/Library/Logs"
touch "$RUN_LOG" "$DETAIL_LOG"
role="$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("role", ""))' "$LEARNINGS_ROOT/config.json" 2>/dev/null || true)"
if [ "$role" != "leader" ]; then
  printf 'RUN %s exit=skipped-not-leader role=%s\n' "$NOW" "${role:--}" >> "$RUN_LOG"
  exit 0
fi
if [ ! -x "$CLAUDE_BIN" ] || [ ! -f "$PROMPT_FILE" ]; then
  printf 'RUN %s exit=missing-runtime\n' "$NOW" >> "$RUN_LOG"
  exit 0
fi

output="$(
  cd "$HOME" && "$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" \
    --permission-mode acceptEdits \
    --allowedTools "Read" "Grep" "Glob" "Write" "Edit" \
    --max-turns 80 2>&1
)"
runner_exit=$?
printf '=== %s learnings-review ===\n%s\n' "$NOW" "$output" >> "$DETAIL_LOG"
printf 'RUN %s exit=%s\n' "$NOW" "$runner_exit" >> "$RUN_LOG"
exit 0
