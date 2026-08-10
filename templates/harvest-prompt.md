# Weekly machine-local learnings harvest

You are running unattended with Read/Grep/Glob/Write/Edit only — no shell.

Canonical store: `~/.agents/learnings/`. Read `config.json` to get this machine's normalized name. New evidence MUST be written only to:

`entries/<machine>/<machine>--<TYPE-YYYYMMDD-MACHINETOKENRANDOM>.md`

Use the three entry types:

- `ERR`: unexpected command, tool, API, automation, or workflow failure.
- `LRN`: correction, knowledge gap, insight, recurring pattern, or better approach.
- `FEAT`: missing capability or requested system improvement.

Scan only this machine's Claude Code and Codex transcripts from the last seven days. Extract recurring signals (2+ occurrences) or one severe signal. Ignore transient network failures and one-off trivia.

Before writing, search this machine's entries and `fleet/catalog.json` when present. If a related logical entry exists, do not edit its evidence file; create a new entry only for materially new evidence and add `See Also` metadata.

Every new ID suffix must begin with up to eight uppercase alphanumeric characters
derived from the configured machine name and end with at least ten random
uppercase hexadecimal characters. Search local entries and the fleet catalog for
the complete candidate before writing. Every new file must contain that unique
ID, `**Status**: pending`, priority, area, summary, details, suggested action,
source agent, evidence references, and redacted excerpts. Never append to
aggregate files. Never edit another machine's directory. Never write secrets or
unnecessary PII.

Write at most ten entries, then append one liveness summary line to the root file
`~/.agents/learnings/harvest-log.md`. Create that root file if needed. Never use or
edit a `harvest-log.md` under `legacy/`.
