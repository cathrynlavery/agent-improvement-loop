---
name: learn-loop
description: "Interactive fix loop over a learnings store. Surveys pending entries and the fix queue, health-checks the loop itself, plans fixes for approval, executes them (subagents for local work, a coding agent for repo changes, the orchestrator gates and commits), then closes every touched entry's status. Use when the user says 'learn loop', 'work the fix queue', 'go through the learnings', or 'what should we be improving'."
---

# Learn Loop

Execute the improvement loop end to end: survey → plan → approve → fix → verify → close statuses. If you run a scheduled headless triage pass (see `templates/fixloop-prompt.md`), that pass only queues work — THIS command is where queued work actually gets done.

Store convention: one file per entry, filename = entry ID (`ERR-YYYYMMDD-XXXX.md`), mandatory `**Status**: pending|in_progress|resolved|promoted` field. Set `LEARNINGS_DIR` to your store (e.g. `~/.learnings`).

## Phase 1 — Survey (always)

1. Status counts: `rg -o '\*\*Status\*\*: *(\w+)' -r '$1' --no-filename $LEARNINGS_DIR/*.md | sort | uniq -c`
2. Read `FIX-QUEUE.md` (work proposed by the headless pass) and every `pending`/`in_progress` entry it references. Read prior `FIX-PLAN-*-RESULTS.md` files — don't redo finished work.
3. Loop meta-health — check the loop's own plumbing before trusting its data:
   - Is the store's repo backup fresh? `git log -1` older than a day means a committer or scheduler is wedged.
   - `.git/index.lock` present? Check for a LIVE git process first (`ps aux | grep '[g]it '`). Live → wait; a 0-byte lock under a running `git add` is normal. No process + old mtime → remove.
   - Do the scheduled passes' logs have recent RUN lines? A gap means a scheduler died silently — a run must log a line even when it finds nothing.
4. Entries `in_progress` with a PR link: check each PR; merged ones get flipped to `resolved` now.

## Phase 2 — Plan and approve

Write a dated plan (`FIX-PLAN-<date>.md`) bucketing work as:
- **Promote** — a rule that belongs in your agent instructions, a skill, or a hook. Prefer enforcement over prose: hook/wrapper > skill > instructions-file line. (Empirically: prose rules get re-violated; only enforced rules stop recurring.)
- **Code fix** — per repo, each with a test gate and a PR target.
- **Operational** — schedulers, config, quarantines of tools producing wrong data.
- **Blocked on the user** — credentials, merges, judgment calls. Surface, never attempt.
Present the plan for approval before executing (small runs of ≤3 items may confirm inline).

## Phase 3 — Execute

- **Subagents** for parallel local work. Briefs must carry: target file byte sizes (+ read windows for large files), read-before-write, exactly one owner per deliverable path, and "no git commands — the orchestrator commits".
- **Coding agent** (e.g. `codex exec`) for repo code, one isolated worktree per lane, with an explicit prohibition on git/network actions. Capture the VCS head before and after every run; a moved head is a hard stop. Sandbox failures that LOOK like code failures usually aren't — module-cache write denials and localhost-listener binds are environmental; run the real test gate outside the sandbox.
- **The orchestrator** runs every test gate itself before committing; scoped adds only in large mixed repos.
- **Verification**: exercise the real surface. A row count equal to the page size is a failure signal, not a result. "Unverified" is a legal outcome; a confident wrong claim is not. Reviewers may demand live proof — build the branch binary and demonstrate against a disposable resource, redacting identifiers.

## Phase 4 — Close the loop (the step that historically gets skipped)

1. Every touched entry: Status → `resolved`/`promoted`/`in_progress` with a Resolution line (date, commit/PR, one sentence). Fixed-but-unmerged stays `in_progress` with the PR link.
2. Remove done lines from `FIX-QUEUE.md`.
3. Write `FIX-PLAN-<date>-RESULTS.md`: one row per item — done-verified / done-unverified / blocked / skipped, with evidence.
4. Commit (scoped) and push the store.
5. Report: what shipped (links), what's blocked on the user, what stayed open and why.

## Guardrails

- Live business surfaces: read current state first; if reality contradicts the entry, stop and report. Prefer the narrowest mutation (per-item over per-container).
- Tools quarantined for producing wrong data stay quarantined until their fixes merge — never re-enable as a side effect.
- Public/OSS pushes get a privacy scan of the full diff first (emails, IDs, tokens, client names).
- Never put secret values on command lines or in transcripts.
