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
- Codex tool use from both the legacy `function_call` payloads and the current `custom_tool_call` payloads. Current Codex sessions execute tools through a code runtime, so the recorded input is a program with the real shell commands embedded as string literals; tracked-CLI names are extracted from those literals with the same command-position rules, so prose mentions still do not count.
- Claude `Bash` command use from tool-call input.
- Actual CLI use (matching the tracked-CLI pattern), not prose mentions inside arbitrary command arguments. Recognizes direct shell execution and quoted remote-shell commands.
- Tool failure/friction. Claude Code marks failed tool calls with an explicit `is_error` flag; that flag is trusted when present. Text heuristics (exit-status lines, error phrases) are the fallback for transcripts that lack it, so successful build logs that merely contain the word "error" are not treated as failures.
- Hang/timeout signals, classified only from tool output — never from the command text, so `timeout 120 foo-cli ...` is not itself a hang.
- Silent-empty results. A call qualifies only when it has no existing failure/hang signal, clearly intends to return data (tracked CLI, MCP read, JSON output shape, or a small known/configured fetch-verb set), returns exactly `[]`, `{}`, `null`, empty stdout, `0 rows`, `No results`, or `(empty)`, and a later agent step proves the result was swallowed rather than surfaced at the end of the session. Normal-success commands (`grep`/`rg` no-match, tests, writes, filesystem mutations, and quiet invocations) are excluded. Ambiguous multi-command Codex runtime calls are also excluded because an empty result cannot be attributed safely.
- User corrections, excluding transcript scaffolding. Any user message that opens with an XML-ish tag is scaffolding (injected instructions, task seeds, notifications), plus an explicit marker list and config-supplied extras. Correction cues are tiered: explicitly corrective phrases ("that's wrong", "not what I asked") count in normal-length messages; ambient words ("do not", "instead", "actually") only count in short reactive messages.

## Routing

- `tool`: real tracked-CLI use produced a failure/hang/retry/silent-empty signal, or an MCP server accumulated recurring failure/silent-empty evidence (grouped per server as `mcp:<server>`). Fix the CLI or server contract.
- `skill_improvement`: a skill was invoked and the same session later contains a correction. Prefer patching the existing skill.
- `memory_context`: corrections not tied to a skill, grouped per project (`cwd`) and capped per session, so one busy session cannot flood the packet. Promote only durable preferences or runbook facts.
- `backlog`: repeated general-tool failures across sessions, plus recurring attributable silent-empty results from general data-fetch commands. Subagent transcripts are excluded by default (exploratory subagents fail by design), and ambiguous code-runtime inputs are skipped because there is no single shell executable to blame. Decide durable vs transient before creating a task.
- `content_idea`: real workflows or moments worth considering for public content. Stage editorial proposals with audience, outline, last30days query, confidence, recommendation, and privacy notes; never draft or publish automatically. Detectors include high-signal slash commands, command-level workflow clusters, private-build signals, and aggregate usage stories such as top skills, most-used CLI tools, loop examples, and slash-command roundups when transcript data contains them.

Proposal IDs are deterministic from route, target, and evidence references, so daily scans avoid restaging the same item unless `--include-seen` is passed.

Every proposal also carries `latest_evidence_at`, the maximum normalized event timestamp across its evidence. The normalizers preserve the transcript record timestamp on each evidence item. Formats without per-record timestamps fall back to the transcript mtime, then a date encoded in a Codex rollout filename. This watermark makes it possible to distinguish stale evidence from a post-fix regression.

## Resolutions

Reviewed outcomes live in a separate `~/.agent-improvement/resolutions.json`, keyed by `route:target`. Keeping this registry outside `state.json` prevents a missing or corrupt scan state from erasing completed decisions. Each entry records `decision` (`fixed`, `wontfix`, or `ignored`), `resolved_at`, `pr`, `note`, and `by`.

Resolution filtering runs before seen-key filtering:

- `fixed` evidence at or before `resolved_at` is suppressed.
- `fixed` evidence after `resolved_at` is emitted and annotated as a regression after the recorded fix.
- `wontfix` and `ignored` targets remain suppressed independent of evidence time.
- `--include-resolved` bypasses resolution suppression for debugging; `--include-seen` alone cannot bypass it.

Suppression is observable. Run metadata records suppressed counts and targets, and review packets repeat the summary and put reopened regressions in their own section. A `decisions.json` handoff lets a fix workflow batch-import `proposal_id`, `target`, `decision`, `resolved_at`, `pr`, `note`, and `by`; open or deferred proposals are omitted.

## Trends

State keeps a bounded per-target history of which runs flagged each `route:target`. Proposals whose target already appeared in previous runs are annotated as recurring and sorted to the top of the packet. Recurrence counts runs, not evidence lines, so fresh evidence for an old problem reads as "still broken", not "new problem". Recording a resolution trims runs at or before `resolved_at`; resolved stale evidence does not add history, so a later regression begins a new recurrence era. Dry runs read the history but never write it.

## Self-checks

Each run records per-source parse statistics (files scanned, tool calls parsed). A source whose transcripts parse but yield zero tool calls triggers a loud warning on stderr, in the run metadata, and in the review packet — the signature of an upstream transcript format change that would otherwise blind the loop silently.

## Configuration

Detector knobs live in an optional JSON config (`~/.agent-improvement/config.json` or `--config`): the tracked-CLI suffix, extra scaffold markers, extra redaction patterns, extra backlog ignores, extra remote-command wrappers, whether subagent failures count, and silent-empty detection controls (`detect_silent_empty`, extra fetch verbs, and ignored executables). Defaults stay generic; personal workflow details belong in the config file, not the source.

## Safety model

- Redact emails, phone numbers, auth headers, cookies, API keys, tokens, cloud/SaaS key shapes (Stripe, Slack, Google, GitHub, GitLab, npm, AWS), private-key blocks, JWTs, and long opaque token-like strings before writing excerpts. A corpus test asserts secret shapes never survive redaction.
- Store evidence references and short excerpts, not whole transcripts.
- For `content_idea`, treat real sessions as private source material. High-risk content evidence suppresses command/excerpt text even under `--full`; public drafts should use abstractions or synthetic examples.
- Keep public detector logic generic. Put private detector catalogs, local logs, and real dogfood output in an ignored `private/` directory or a private fork.
- Treat transcript scaffolding as non-user text.
- Keep scan/stage automation separate from apply automation.
- Prefer patching existing skills/runbooks over creating narrow new skills.
- Classify every candidate as durable or one-off before applying.

## Reviewer contract

A review turn reads the latest packet and produces one decision per proposal (apply / defer / reject / route onward), shows the exact diff or command first, and applies only after explicit approval. After approved work is completed, it emits a structured `decisions.json`: completed fixes become `fixed`, intentional durable non-fixes become `wontfix`, and stale/false-positive/one-off signals become `ignored`. Deferred and open proposals are not resolved.
