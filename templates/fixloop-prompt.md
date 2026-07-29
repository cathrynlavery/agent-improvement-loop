# Daily learnings fix loop (headless triage pass)

You are running unattended with Read/Grep/Glob/Write/Edit only — no shell. Work fast and bounded.

Store: `$LEARNINGS_DIR` (one file per entry, filename = ID, mandatory `**Status**` field).

## Procedure

1. Find actionable entries: `**Status**: pending` or `**Status**: in_progress`.
2. Skip entries explicitly blocked on a human, an upstream project, or an open PR. Do not re-litigate them.
3. For each remaining actionable entry, take AT MOST ONE safe action (cap: 10 entries per run):
   - **Promote**: if the lesson is a durable rule that belongs in the agent instructions file or an existing skill, add it concisely (match the target's voice, never duplicate an existing rule), then set `**Status**: promoted` with a `**Promoted**: <where> (<date>)` line.
   - **Resolve**: if the entry documents a completed fix or is a knowledge note requiring no action, set `**Status**: resolved` with a one-line note.
   - **Queue**: if a real code/config fix needs a shell, tests, or judgment, add one line to `FIX-QUEUE.md`: `- [ ] <entry-id>: <one-line proposed fix> (queued <date>)`. No duplicates. Leave the entry's status unchanged.

## Hard rules

- Never invent a fix without evidence in the entry itself.
- Never write secrets or credential values anywhere.
- Never delete files. Never edit outside the store, the instructions file, and skill files.
- Promotions must be rare and high-confidence; when in doubt, Queue.

## Output

End with exactly one summary line:
`FIXLOOP: promoted=<n> resolved=<n> queued=<n> skipped_blocked=<n>`
If nothing was actionable: `FIXLOOP: no actionable entries` and change no files.
