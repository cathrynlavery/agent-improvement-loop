# agent-improvement-loop

**Self-improvement for you and your AI agents.** A small, local, safe daily loop that mines your Claude Code and Codex sessions for reusable improvements, then stages them for your approval.

It never changes anything on its own. It reads your transcripts, finds the friction you keep hitting, redacts anything sensitive, and writes a review packet of staged proposals. You decide what to apply.

The idea: most people point AI at their work. The higher-leverage first loop points it at **your own setup**. Every fix you make to a skill, a command, a hook, or a tool pays off in every future session. This tool finds those fixes by reading what already happened, instead of asking you to remember.

## What it does

1. **Collect** local Claude Code (`~/.claude/projects/**/*.jsonl`) and Codex (`~/.codex/sessions/**/*.jsonl`) transcripts from one or more home directories.
2. **Normalize** each into a small, redacted event model (tool calls, shell commands, skill use, failures, corrections, slash commands).
3. **Detect** reusable improvement signals from *actual tool usage*, not prose mentions. If you typed "don't use that CLI," that sentence is not counted as the CLI failing.
4. **Detect** grounded content ideas from real workflows, slash commands, and private-build signals, then stage them for editorial review.
5. **Stage** proposals and a human-readable review packet under `~/.agent-improvement/`.

It does **not** edit skills, memory, runbooks, config, or source code. Scan and stage are automated. Apply stays manual.

## Install

Requires Python 3.10+. No dependencies, standard library only.

```sh
git clone <this-repo> agent-improvement-loop
cd agent-improvement-loop
./bin/daily-improvement-loop --since-days 1
```

## Usage

```sh
# Mine sessions from the last day and stage proposals
./bin/daily-improvement-loop --since-days 1

# Backfill everything (first run), capped
./bin/daily-improvement-loop --all --max-sessions 500

# Just Claude, or just Codex
./bin/daily-improvement-loop --source claude --since-days 7

# Preview as JSON without writing the queue
./bin/daily-improvement-loop --since-days 1 --dry-run

# Stage only content ideas grounded in real session evidence
./bin/daily-improvement-loop --route content_idea --since-days 7

# Include logs copied from other machines
./bin/daily-improvement-loop --home ~/.agent-logs/laptop --extra-home ~/.agent-logs/desktop --route content_idea
```

| Flag | Meaning |
|---|---|
| `--since-days N` | Scan sessions modified within N days |
| `--all` | Backfill every discovered session |
| `--max-sessions N` | Keep only the most recent N after filtering |
| `--source {all,claude,codex}` | Which transcripts to read (default `all`) |
| `--route {all,improvement,content_idea}` | Stage operational improvements, content ideas, or both (default `improvement`) |
| `--include-seen` | Re-emit proposals even if their key was seen before |
| `--full` | Keep full, unredacted excerpts inline (local use only; do not share the output) |
| `--dry-run` | Print JSON, write nothing |
| `--home PATH` | Home dir containing `.claude` / `.codex` (default `~`) |
| `--extra-home PATH` | Additional home dir containing `.claude` / `.codex`, useful for logs copied from another Mac |
| `--output-root PATH` | Where to write the queue (default `~/.agent-improvement`) |
| `--printing-press-root PATH` | Root of your printing-press CLI tree, so a `tool` proposal points at the matching CLI source and the amend/reprint workflow (default `~/printing-press`, or `PRINTING_PRESS_ROOT`) |

## Output

```text
~/.agent-improvement/
  state.json                     # last scan time + seen proposal keys
  session-index.jsonl            # one row per session with signals
  runs/<run-id>.json             # run metadata
  proposals/<run-id>/*.json      # one staged proposal per file
  review-packets/<run-id>.md     # the human-readable packet you review
```

Every proposal is marked `manual_approval_required`. Each one has a target, a route, the evidence line it came from, and a suggested action.

By default the excerpts are short and secret-shaped strings are masked, so the queue is safe to commit, sync, or paste into a writeup, and you still get the real command, the real error, and a `path:line` pointer to open the full transcript. Pass `--full` when you want longer, unredacted detail written inline instead. That output is local-only; do not share it.

For `content_idea`, personal/private sessions are allowed as local source material, but the public output is intentionally conservative: high-risk content evidence suppresses command and excerpt text even when `--full` is enabled. Use the idea as a starting point, then remove names, raw messages, customer/client details, family details, auth material, exact private metrics, and any other identifying specifics before drafting or publishing.

## Open-source and private forks

This repo should stay public-safe. Put private detector catalogs, copied logs, real run outputs, receipts, and dogfood artifacts in a private fork or ignored local files such as `private/` and `.agent-improvement/`.

See [`docs/OPEN_SOURCE.md`](docs/OPEN_SOURCE.md) for the public/private split, release checklist, and content privacy rules.

## The routes (where a fix belongs)

The hard part is not noticing. It is deciding what kind of lesson you found. Proposals are routed to one of:

- **`tool`** — a CLI failed *or got stuck* on you in real use. Fix the tool, not the prompt. (By default it recognizes CLIs named `*-pp-cli`; see "Tracking your own CLIs" below.) Three kinds of friction are caught: hard **failures** (non-zero exit, error text), **hang/timeout** signals where the command stalled without a clean error, and **retry-before-success** — the same CLI invoked many times in one session because the agent was guessing the syntax. The proposal summary breaks down which kinds fired and how many retries it took. For a printing-press CLI (resolved against `--printing-press-root`), the suggested action also names the source directory and points you at `/printing-press-amend` or `/printing-press-reprint`.
- **`skill_improvement`** — a reusable skill/command was used and the session later contained a correction. Patch the existing skill before creating a new one. Corrections that happened before the skill was invoked are treated as task context, not evidence that the skill failed.
- **`memory_context`** — a durable correction not tied to a skill. A candidate line for your `CLAUDE.md` / `AGENTS.md` or a runbook.
- **`backlog`** — repeated tool failures worth tracking but not urgent, staged with evidence instead of a vague note.
- **`content_idea`** — a real workflow or moment that may be useful public content. The MVP detects repeated/high-value slash command usage, command-level workflow clusters (task-ledger, executive-assistant, and revenue-watch loops), aggregate usage stories (`top skills`, command-line stack, most-used slash commands when present), and private-build signals such as message/search/CRM workflows. These proposals include content type, audience, rough outline, suggested `/last30days` query, confidence, recommendation, and privacy/redaction notes. They are editorial staging only: no drafting, posting, or publishing.

And the most important non-route: **nothing.** One-off failures (a VPN was off) are discarded, not encoded. A system that cannot throw a lesson away turns into a haunted house of old warnings.

## Run it every day

A loop you have to remember to run is not a loop. Automate the scan, keep the approval manual.

```sh
# 7am daily: mine yesterday's sessions and stage proposals for review
0 7 * * * cd ~/agent-improvement-loop && ./bin/daily-improvement-loop --since-days 1 >> ~/agent-review.log 2>&1
```

Then, when you have a minute, hand the latest packet to an agent to triage:

```sh
latest=$(ls -t ~/.agent-improvement/review-packets/*.md | head -1)
# open "$latest" or paste it into Claude/Codex and ask:
# "Read this packet. For each proposal, tell me apply / defer / reject,
#  show the exact diff or command, and wait for my approval."
```

For a fuller reusable review prompt, see
[`docs/REVIEWER_PROMPT.md`](docs/REVIEWER_PROMPT.md).

Schedule the scan. Never schedule the changes.

## The safety model is the point

- Redact before review (emails, tokens, keys, long opaque strings).
- Store evidence references and short excerpts, not whole transcripts.
- Detect real tool usage, not prose mentions.
- Separate durable lessons from one-off incidents.
- Patch existing skills before creating new ones.
- Require human approval before anything changes.

The valuable thing is not autonomy. It is controlled compounding: yesterday's annoyance becomes tomorrow's default, and no single bad session gets enshrined as a permanent rule.

## Tracking your own CLIs

The `tool` route recognizes CLIs whose command name ends in `-pp-cli` by default (the convention this tool was built around). To track your own tools, edit `PP_CLI_RE` near the top of `scripts/daily_improvement_loop.py` to match your CLI naming. General tool failures are still captured by the `backlog` route regardless.

If your CLI sources live somewhere other than `~/printing-press`, pass `--printing-press-root PATH` (or set `PRINTING_PRESS_ROOT`). The loop maps `<name>-pp-cli` to the first of `<root>/library/<name>`, `<root>/manuscripts/<name>`, or `<root>/<name>` that exists on disk; if none exist, the proposal simply omits the source line.

## Roadmap

- **Per-project context review.** Group sessions by project (their `cwd`) and ask, for each project: could a line in that project's `CLAUDE.md` / `AGENTS.md`, or a project hook, have made these sessions go better? Each session already records its `cwd`, and corrections already route to `memory_context`; this would make the *project* the unit of review. The point is not only tuning the agent. It surfaces what you could change in how you set a project up, for yourself as much as for the agent.
- Repeated-command-chain detection, not just executable names.
- Silent-null JSON detection (command exits 0 but returns empty data) and "manual parsing where a `--json` path exists."
- A `--review` mode that reads the latest packet and walks the apply / defer / reject decisions with you.

## Tests

```sh
python3 scripts/test_daily_improvement_loop.py
```

## License

MIT. See [LICENSE](LICENSE).

Built by [Little Might](https://littlemight.com).
