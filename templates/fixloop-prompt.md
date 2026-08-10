# Daily learnings fix loop (headless leader)

You are running unattended with Read/Grep/Glob/Write/Edit only — no shell. Work fast and bounded.

Canonical store: `~/.agents/learnings/`.

- `config.json` must say `"role": "leader"`; otherwise stop without changes.
- `ACTIVE.md` is generated and read-only.
- Machine-owned evidence lives recursively under `entries/<machine>/<machine>--<ID>.md`; never edit it.
- Durable outcomes live in `decisions/<ID>.json` and override source status without racing peer files.
- Code/config work lives in `queue/FIX-QUEUE.md`.

## Procedure

1. Read `ACTIVE.md`, `conflicts.json`, and the referenced entry copies for actionable items.
2. Skip entries explicitly blocked on a human, upstream project, or open PR. Do not re-litigate them.
3. If an ID is listed in `conflicts.json`, queue one reconciliation line and do not choose a version unattended.
4. For each remaining actionable logical entry, take at most one safe action (cap: 10 entries):
   - **Promote**: add a concise, non-duplicate rule to the appropriate agent instructions or existing skill, then write `decisions/<ID>.json` with `schema_version`, `id`, `status: promoted`, `decided_at`, `by: fixloop`, `target`, and a one-line `note`.
   - **Resolve**: when the entry documents a completed fix or requires no action, write the same decision object with `status: resolved` and a factual note.
   - **Queue**: add or refresh one line in `queue/FIX-QUEUE.md`: `- [ ] <ID>: <one-line proposed fix> (queued <date>)`. Leave the logical entry actionable.
5. Cross-link related IDs in the queue or decision note. Do not mutate evidence files.

## Hard rules

- Never invent a fix without evidence in an entry copy.
- Never write secrets, tokens, credential values, or unnecessary PII.
- Never delete files and never edit `ACTIVE.md`, `catalog.json`, `conflicts.json`, `entries/`, or `legacy/`.
- Promotions must be rare and high-confidence; queue when uncertain.

## Output

End with exactly one summary line:
`FIXLOOP: promoted=<n> resolved=<n> queued=<n> conflicts=<n> skipped_blocked=<n>`

If nothing is actionable: `FIXLOOP: no actionable entries` and change no files.
