# Design

A conservative first implementation of a daily session-mining loop.

## Four phases

1. **Collect** local Claude Code and Codex transcripts.
2. **Normalize** them into a small, redacted event model.
3. **Detect** reusable improvement signals.
4. **Stage** proposals for an interactive review/apply turn.

Only the first three are safe to automate. The apply phase stays approval-gated unless each target has its own write gate.

## Normalized session

Every transcript reduces to tool calls, CLI invocations, skill invocations, failures, corrections, and slash commands, with evidence stored as references plus short redacted excerpts, never full transcript dumps.

## Detectors

- `Skill` tool use, parsed structurally from tool calls.
- Codex `function_call` command use from JSON arguments.
- Claude `Bash` command use from tool-call input.
- Actual CLI use (matching the tracked-CLI pattern), not prose mentions inside arbitrary command arguments. Recognizes direct shell execution and quoted remote-shell commands.
- Tool failure/friction, with exit-status awareness so successful build logs that merely contain the word "error" are not treated as failures.
- User corrections, excluding transcript scaffolding (permission blocks, injected context, local-command caveats, task notifications, continuation summaries, image attachment caveats, skill payloads, subagent seed prompts).

## Routing

- `tool`: real CLI use produced a failure/friction signal. Fix the CLI contract.
- `skill_improvement`: a skill was invoked and the same session later contains a correction. Prefer patching the existing skill.
- `memory_context`: corrections not tied to a skill. Promote only durable preferences or runbook facts.
- `backlog`: repeated tool failures. Decide durable vs transient before creating a task.
- `content_idea`: real workflows or moments worth considering for public content. Stage editorial proposals with audience, outline, last30days query, confidence, recommendation, and privacy notes; never draft or publish automatically. Detectors include high-signal slash commands, command-level workflow clusters, private-build signals, and aggregate usage stories such as top skills, most-used CLI tools, loop examples, and slash-command roundups when transcript data contains them.

Proposal IDs are deterministic from route, target, and evidence references, so daily scans avoid restaging the same item unless `--include-seen` is passed.

## Safety model

- Redact emails, phone numbers, auth headers, cookies, API keys, tokens, and long opaque token-like strings before writing excerpts.
- Store evidence references and short excerpts, not whole transcripts.
- For `content_idea`, treat real sessions as private source material. High-risk content evidence suppresses command/excerpt text even under `--full`; public drafts should use abstractions or synthetic examples.
- Keep public detector logic generic. Put private detector catalogs, local logs, and real dogfood output in an ignored `private/` directory or a private fork.
- Treat transcript scaffolding as non-user text.
- Keep scan/stage automation separate from apply automation.
- Prefer patching existing skills/runbooks over creating narrow new skills.
- Classify every candidate as durable or one-off before applying.

## Reviewer contract

A review turn reads the latest packet and produces one decision per proposal (apply / defer / reject / route onward), shows the exact diff or command first, and applies only after explicit approval.
