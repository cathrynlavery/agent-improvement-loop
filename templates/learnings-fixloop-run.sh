#!/bin/bash
# Daily learnings fix loop: headless triage pass, then a scoped commit of the
# store. Alert-only failure posture; always exits 0. Adjust the four paths.
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

LEARNINGS_DIR="$HOME/.learnings"                 # your store
REPO_ROOT="$LEARNINGS_DIR"                       # repo containing the store (may be a parent dir)
PROMPT_FILE="$(dirname "$0")/fixloop-prompt.md"  # the triage prompt
FIXLOG="$LEARNINGS_DIR/fixloop-log.md"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

[ -f "$FIXLOG" ] || printf '# Fixloop log — one line per daily run\n\n' > "$FIXLOG"

if [ ! -x "$CLAUDE_BIN" ]; then
  printf 'RUN %s exit=missing-claude\n' "$NOW" >> "$FIXLOG"
  exit 0
fi

OUT="$("$CLAUDE_BIN" -p "$(cat "$PROMPT_FILE")" \
  --permission-mode acceptEdits \
  --allowedTools "Read" "Grep" "Glob" "Write" "Edit" \
  --max-turns 80 2>&1)"
runner_exit=$?

SUMMARY="$(printf '%s' "$OUT" | grep -o 'FIXLOOP: .*' | tail -1)"
# Log a line even on empty or failed runs — silent death must be visible.
printf 'RUN %s exit=%s %s\n' "$NOW" "$runner_exit" "${SUMMARY:-no-summary-line}" >> "$FIXLOG"

# Scoped commit. Skip if a live git process holds the index; remove only
# genuinely orphaned locks.
cd "$REPO_ROOT" || exit 0
if [ -f .git/index.lock ]; then
  if pgrep -f "git (add|commit)" >/dev/null 2>&1; then
    printf 'RUN %s commit=skipped-live-lock\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$FIXLOG"
    exit 0
  fi
  rm -f .git/index.lock
fi
git add "$LEARNINGS_DIR" 2>/dev/null
if ! git diff --cached --quiet 2>/dev/null; then
  git commit -q -m "learnings: daily fixloop $(date +%Y-%m-%d)" 2>/dev/null
  git push -q 2>/dev/null
fi
exit 0
