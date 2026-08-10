---
name: learn-loop
description: "Close captured agent-improvement work: triage fleet learnings, select a bounded evidence-backed batch, implement and live-verify fixes, and record leader decisions. Use for 'learn loop', 'fix the learnings', 'process the backlog', or when asked what recurring agent/tool friction to improve."
---

# Learn Loop

Turn captured friction into verified system improvements:

`survey → select → approve → fix → verify → decide`

The canonical store is `${AGENT_LEARNINGS_ROOT:-~/.agents/learnings}`. Machine-owned files under `entries/` are immutable evidence. Effective outcomes live in leader-owned `decisions/`; ID corrections live in `rekeys/`.

## Modes

- **Survey** — recommend what to fix without changing code or store outcomes.
- **Fix** — execute an approved bounded batch through verification and decision.
- **Reconcile** — resolve divergent-copy conflicts. Read [STORE-PROTOCOL.md](STORE-PROTOCOL.md) before any store mutation.
- **Resume** — continue an existing plan or PR from current state; do not repeat completed work.

## 1. Pin the snapshot

1. Read `config.json` and run `learnings status`.
2. Use `ACTIVE.md`, `catalog.json`, and `conflicts.json` on the leader; on a writer, use `fleet/` for survey and move mutations to the leader.
3. Stop store mutation when generated views disagree or the catalog reports invalid files, stale acknowledgements, rekey errors, decision errors, or integrity errors.
4. Read the fix queue and prior `FIX-PLAN-*-RESULTS.md` files. Re-check live PRs and external dependencies instead of trusting their recorded status.
5. When the request concerns loop health, check recent harvest, collection, and triage run lines plus the leader's unattended runtime.

**Snapshot complete when:** one healthy catalog generation is pinned and every previously touched candidate is classified as finished, still active, or superseded.

## 2. Pass the evidence gate

For each candidate, read every referenced evidence copy and inspect the current target. A title or aggregate row is a pointer, not proof.

Assign exactly one disposition:

- **Promote** — encode a durable rule in an existing instruction, skill, test, hook, or wrapper.
- **Code fix** — change one repository with a named test and shipping target.
- **Operational fix** — repair a scheduler, configuration, migration, or live integration.
- **Reconcile** — acknowledge compatible copies or rekey unrelated incidents.
- **Close without change** — duplicate, stale, already fixed, one-off, or intentionally declined.
- **Blocked** — requires credentials, payment, destructive authority, merge approval, or unresolved human judgment.

Rank candidates by recurrence across machines, severity, enforceability, and verification cost. Select at most three unless the user approves a larger batch.

In Survey mode, present the ranked recommendation and stop here without writing a plan or outcome. In Fix or Reconcile mode, write `reviews/FIX-PLAN-<date>.md`; add a short sequence or topic suffix when that day's path already exists.

The plan records:

- pinned catalog generation;
- current and desired behavior per item;
- independently checkable acceptance criteria;
- explicit out-of-scope boundaries;
- target repository or live surface.

An explicit request to execute named work is approval for that scope. Otherwise present the plan and wait.

**Evidence gate passes when:** every selected item has current proof, a single disposition, acceptance criteria, scope boundaries, and approval.

## 3. Execute the smallest effective change

Follow each target repository's instructions and preserve unrelated work. Prefer the narrowest durable enforcement seam: test or wrapper, then skill or instruction when mechanical enforcement is not available.

Keep source-repository changes separate from the learnings store. Store evidence records what happened; source commits and PRs change the system.

When independent lanes are safe, use the concurrency available in the environment with one owner per deliverable path. Store mutations remain serial because every changed mutation publishes a new generation.

**Execution complete when:** the requested behavior is implemented, scoped, and ready for its declared verification—not merely when files changed.

## 4. Pass the ship gate

Verification must name the observation:

- run focused tests while iterating and the full relevant suite at the end;
- exercise the real command, app, schedule, or integration when safe;
- verify the correct account, machine, environment, and deployed version;
- scan the full diff before a public PR for private names, paths, identifiers, credentials, and real evidence;
- when runtime or store behavior changed, publish one generation and confirm `learnings status` on every reachable machine.

An open PR is `in_progress`. A merged change without live proof is still unverified. Record that honestly instead of closing it.

**Ship gate passes when:** every acceptance criterion has a test, receipt, URL, catalog summary, or explicit `unverified` result.

## 5. Pass the decision gate

Read [STORE-PROTOCOL.md](STORE-PROTOCOL.md) before recording outcomes or reconciling conflicts.

For every selected item:

1. Record the effective status and evidence through the leader CLI.
2. Refresh the generation after each changed mutation.
3. Remove completed queue lines; retain active or blocked work with its current reference.
4. Write `reviews/FIX-PLAN-<date>-RESULTS.md` with one row per item: `done-verified`, `done-unverified`, `blocked`, or `skipped`.
5. Rebuild, validate, collect, and confirm one fleet generation when the store changed.

**Decision gate passes when:** every touched item has one durable outcome, queue state matches reality, and generated views are healthy.

## 6. Report the handoff

Lead with the outcome. Report:

- shipped commits and PRs;
- verification evidence;
- items deliberately left open and why;
- user action required, if any.

The final report must stand alone; commentary and private evidence are not part of the handoff.

## Guardrails

- Evidence remains immutable; decisions and rekeys carry interpretation.
- Compatible-copy acknowledgement binds to the exact current path-and-hash set.
- Public artifacts use synthetic examples and generic machine, account, project, and tool names.
- Live writes begin from current state and use the narrowest safe mutation.
- Credentials, secrets, raw transcripts, unnecessary PII, and private evidence stay out of commands, logs, decisions, plans, commits, and PRs.
