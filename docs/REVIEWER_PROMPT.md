# Agent Improvement Packet Reviewer

Use this prompt when handing a staged review packet to an agent.

```text
You are reviewing staged proposals from agent-improvement-loop.

Read the latest packet:

latest=$(ls -t ~/.agent-improvement/review-packets/*.md | head -1)
sed -n '1,260p' "$latest"

Your job is to triage proposals and prepare improvements. Do not apply changes
until I explicitly approve the specific diff or command.

For each proposal, return exactly one decision:

- `apply` — durable, useful, and enough evidence exists. Show the exact diff or
  command you would run, then wait for approval.
- `defer` — probably useful, but needs more evidence, a product decision, or a
  dedicated implementation pass.
- `reject` — one-off incident, false positive, stale context, or not worth
  encoding.
- `route onward` — belongs in another system: another repo, your issue
  tracker, or your CLI fix/rebuild workflow.

Review rules:

- Tool proposals from real CLI failures should usually become CLI fixes, not
  prompt rules.
- Skill proposals should patch existing SKILL.md files before creating new
  skills.
- Memory/runbook proposals should only preserve durable preferences or facts.
- Ignore transcript scaffolding, copied instructions, context summaries, and
  transient environment failures.
- If the packet contains a PARSER WARNING line, treat it as a proposal against
  this tool itself: the transcript format may have changed upstream.
- Never print raw secrets or auth files.
- Keep scan/stage automated and apply manual.

Final review format:

## Decisions

- apply: <proposal id> — <target> — <why>
- defer: <proposal id> — <target> — <why>
- reject: <proposal id> — <target> — <why>
- route onward: <proposal id> — <target> — <where/why>

## Proposed Changes

For each apply, show the exact patch or command. Stop before applying.

After I approve and the fixes are implemented and verified, write a structured
`decisions.json` handoff. Include only completed or deliberately closed targets;
never include `defer`, still-open work, or work that has merely been proposed.

Use this schema:

{
  "schema_version": 1,
  "decisions": [
    {
      "proposal_id": "imp-example",
      "target": "tool:example-pp-cli",
      "decision": "fixed",
      "resolved_at": "<ISO8601 UTC; omit to use import time>",
      "pr": "<PR number or URL, or empty>",
      "note": "<what was verified or why it was closed>",
      "by": "<reviewer or fix workflow>"
    }
  ]
}

Map implemented and verified work to `fixed`, intentional durable non-fixes to
`wontfix`, and false-positive, stale, or one-off signals to `ignored`. The
`target` must be the packet's exact `route:target` key. Import the handoff with:

./bin/daily-improvement-loop --resolve-from decisions.json
```
