# agent-improvement-loop

**Self-improvement for you and your AI agents.** A small, local, safe daily loop that mines your Claude Code and Codex sessions for reusable improvements, then stages them for your approval.

It never changes anything on its own. It reads your transcripts, finds the friction you keep hitting, redacts anything sensitive, and writes a review packet of staged proposals. You decide what to apply.

The idea: most people point AI at their work. The higher-leverage first loop points it at **your own setup**. Every fix you make to a skill, a command, a hook, or a tool pays off in every future session. This tool finds those fixes by reading what already happened, instead of asking you to remember.

## What it does

1. **Collect** local Claude Code (`~/.claude/projects/**/*.jsonl`) and Codex (`~/.codex/sessions/**/*.jsonl`) transcripts from one or more home directories.
2. **Normalize** each into a small, redacted event model (tool calls, shell commands, skill use, failures, corrections, slash commands).
3. **Detect** reusable improvement signals from _actual tool usage_, not prose mentions. If you typed "don't use that CLI," that sentence is not counted as the CLI failing.
4. **Detect** grounded content ideas from real workflows, slash commands, and private-build signals, then stage them for editorial review.
5. **Stage** proposals and a human-readable review packet under `~/.agent-improvement/`.

It does **not** edit skills, memory, runbooks, config, or source code. Scan and stage are automated. Apply stays manual.

## Install

Requires Python 3.10+. No dependencies, standard library only.

```sh
git clone https://github.com/cathrynlavery/agent-improvement-loop
cd agent-improvement-loop
./bin/daily-improvement-loop --since-days 1
```

Or install the CLI on your PATH:

```sh
pipx install git+https://github.com/cathrynlavery/agent-improvement-loop
# or: uv tool install git+https://github.com/cathrynlavery/agent-improvement-loop
agent-improvement-loop --since-days 1
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

| Flag                                     | Meaning                                                                                                                                                                            |
| ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--since-days N`                         | Scan sessions modified within N days                                                                                                                                               |
| `--all`                                  | Backfill every discovered session                                                                                                                                                  |
| `--max-sessions N`                       | Keep only the most recent N after filtering                                                                                                                                        |
| `--source {all,claude,codex}`            | Which transcripts to read (default `all`)                                                                                                                                          |
| `--route {all,improvement,content_idea}` | Stage operational improvements, content ideas, or both (default `improvement`)                                                                                                     |
| `--include-seen`                         | Re-emit proposals even if their key was seen before                                                                                                                                |
| `--full`                                 | Keep full, unredacted excerpts inline (local use only; do not share the output)                                                                                                    |
| `--dry-run`                              | Print JSON, write nothing                                                                                                                                                          |
| `--home PATH`                            | Home dir containing `.claude` / `.codex` (default `~`)                                                                                                                             |
| `--extra-home PATH`                      | Additional home dir containing `.claude` / `.codex`, useful for logs copied from another Mac                                                                                       |
| `--output-root PATH`                     | Where to write the queue (default `~/.agent-improvement`)                                                                                                                          |
| `--config PATH`                          | JSON config overriding detector defaults (default `~/.agent-improvement/config.json`; see "Configuration")                                                                         |
| `--printing-press-root PATH`             | Root of your printing-press CLI tree, so a `tool` proposal points at the matching CLI source and the amend/reprint workflow (default `~/printing-press`, or `PRINTING_PRESS_ROOT`) |

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

By default the excerpts are short and secret-shaped strings are masked, so the queue is safe to hand to a local review agent, and you still get the real command, the real error, and a `path:line` pointer to open the full transcript. Treat it as private by default anyway: absolute paths, project names, and CLI names are not redacted. Pass `--full` when you want longer, unredacted detail written inline instead. That output is local-only; do not share it.

Each run also records per-source parse statistics, and warns loudly (stderr, run metadata, and the packet itself) when a source's transcripts parse but yield zero tool calls — the signature of a transcript format change that would otherwise silently blind the loop.

Proposals whose target was already flagged in previous runs are marked as recurring ("also flagged in N previous run(s)") and sorted to the top of the packet — a target that keeps coming back is the strongest signal the loop produces.

For `content_idea`, personal/private sessions are allowed as local source material, but the public output is intentionally conservative: high-risk content evidence suppresses command and excerpt text even when `--full` is enabled. Use the idea as a starting point, then remove names, raw messages, customer/client details, family details, auth material, exact private metrics, and any other identifying specifics before drafting or publishing.

## Open-source and private forks

This repo should stay public-safe. Put private detector catalogs, copied logs, real run outputs, receipts, and dogfood artifacts in a private fork or ignored local files such as `private/` and `.agent-improvement/`.

See [`docs/OPEN_SOURCE.md`](docs/OPEN_SOURCE.md) for the public/private split, release checklist, and content privacy rules.

## The routes (where a fix belongs)

The hard part is not noticing. It is deciding what kind of lesson you found. Proposals are routed to one of:

- **`tool`** — a CLI failed _or got stuck_ on you in real use. Fix the tool, not the prompt. (By default it recognizes CLIs named `*-pp-cli`; see "Configuration" below.) Three kinds of friction are caught: hard **failures** (non-zero exit, error text), **hang/timeout** signals where the command stalled without a clean error, and **retry-before-success** — the same CLI invoked many times in one session because the agent was guessing the syntax. The proposal summary breaks down which kinds fired and how many retries it took. For a printing-press CLI (resolved against `--printing-press-root`), the suggested action also names the source directory and points you at `/printing-press-amend` or `/printing-press-reprint`. Repeated **MCP tool failures** route here too, grouped per server (`mcp:<server>`): recurring failures usually mean expired auth, a broken server config, or a tool contract the agent keeps guessing wrong.
- **`skill_improvement`** — a reusable skill/command was used and the session later contained a correction. Patch the existing skill before creating a new one. Corrections that happened before the skill was invoked are treated as task context, not evidence that the skill failed.
- **`memory_context`** — a durable correction not tied to a skill, grouped per project (`cwd`) so the review question is "what line in _this_ project's `CLAUDE.md` / `AGENTS.md` or runbook would have prevented this". Corrections are detected conservatively: explicitly corrective phrases count in normal-length messages, while ambient words like "do not" or "instead" only count in short reactive messages — a long task brief that says "do NOT change X" is an instruction, not a correction.
- **`backlog`** — repeated tool failures worth tracking but not urgent, staged with evidence instead of a vague note. Failures inside subagent transcripts are excluded by default (exploratory subagents fail by design while probing).
- **`content_idea`** — a real workflow or moment that may be useful public content. The MVP detects repeated/high-value slash command usage, command-level workflow clusters (task-ledger, executive-assistant, and revenue-watch loops), aggregate usage stories (`top skills`, command-line stack, most-used slash commands when present), and private-build signals such as message/search/CRM workflows. These proposals include content type, audience, rough outline, suggested `/last30days` query, confidence, recommendation, and privacy/redaction notes. They are editorial staging only: no drafting, posting, or publishing.

And the most important non-route: **nothing.** One-off failures (a VPN was off) are discarded, not encoded. A system that cannot throw a lesson away turns into a haunted house of old warnings.

## Run it on a schedule

A loop you have to remember to run is not a loop. Automate the scan, keep the approval manual.

With cron:

```sh
# 7am daily: mine yesterday's sessions and stage proposals for review
0 7 * * * cd ~/agent-improvement-loop && ./bin/daily-improvement-loop --since-days 1 >> ~/agent-review.log 2>&1
```

On macOS, launchd survives sleep/wake better than cron. Copy
[`examples/com.example.agent-improvement-loop.plist`](examples/com.example.agent-improvement-loop.plist)
into `~/Library/LaunchAgents/`, edit the paths, and `launchctl load` it. The
example runs twice a week with `--since-days 4`, which overlaps windows so a
missed run cannot drop sessions.

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

- Redact before review (emails, phone numbers, tokens, keys, provider-specific key shapes, private-key blocks, long opaque strings). A corpus test asserts secret shapes never survive redaction.
- Store evidence references and short excerpts, not whole transcripts.
- Detect real tool usage, not prose mentions.
- Separate durable lessons from one-off incidents.
- Patch existing skills before creating new ones.
- Require human approval before anything changes.

The valuable thing is not autonomy. It is controlled compounding: yesterday's annoyance becomes tomorrow's default, and no single bad session gets enshrined as a permanent rule.

## Configuration

Detector defaults can be overridden without editing code. Put a JSON file at
`~/.agent-improvement/config.json` (or pass `--config PATH`); see
[`examples/config.example.json`](examples/config.example.json). Recognized keys:

| Key                             | Meaning                                                                                  |
| ------------------------------- | ---------------------------------------------------------------------------------------- |
| `tracked_cli_suffix`            | Command-name suffix the `tool` route tracks (default `-pp-cli`)                          |
| `extra_scaffold_markers`        | Strings that mark injected/scaffolded user messages your own automation inserts          |
| `extra_redaction_patterns`      | `[regex, replacement]` pairs appended to the built-in secret masks                       |
| `extra_backlog_ignore`          | Executables the `backlog` route should never blame                                       |
| `extra_remote_command_wrappers` | Wrappers (like your ssh helper) whose quoted commands should be scanned for tracked CLIs |
| `include_subagent_failures`     | Count failures from subagent transcripts toward `backlog` (default `false`)              |

General tool failures are still captured by the `backlog` route regardless of the tracked suffix.

If your CLI sources live somewhere other than `~/printing-press`, pass `--printing-press-root PATH` (or set `PRINTING_PRESS_ROOT`). The loop maps `<name>-pp-cli` to the first of `<root>/library/<name>`, `<root>/manuscripts/<name>`, or `<root>/<name>` that exists on disk; if none exist, the proposal simply omits the source line.

## Roadmap

- Repeated-command-chain detection, not just executable names.
- Silent-null JSON detection (command exits 0 but returns empty data) and "manual parsing where a `--json` path exists."
- A `--review` mode that reads the latest packet and walks the apply / defer / reject decisions with you.
- Extract embedded shell commands from code-runtime tool calls for the `backlog` route (tracked CLIs inside code literals are already detected; general executables are not).

## Tests

```sh
python3 scripts/test_daily_improvement_loop.py
```

## License

MIT. See [LICENSE](LICENSE).

Built by [Little Might](https://littlemight.com).
