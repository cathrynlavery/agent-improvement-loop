---
name: learn-loop
description: "Interactive fix loop over a machine-local fleet learnings store. Surveys the published catalog and fix queue, plans a bounded batch for approval, executes and verifies fixes, then records immutable decisions without editing evidence. Use when the user says 'learn loop', 'work the fix queue', 'go through the learnings', or 'what should we be improving'."
---

# Learn Loop

Execute the improvement loop end to end: survey → plan → approve → fix → verify → decide. The scheduled headless pass may promote a safe rule, resolve an evidenced outcome, or queue work. This interactive skill performs real code and operational fixes.

The canonical store is `${AGENT_LEARNINGS_ROOT:-~/.agents/learnings}`. It is a machine-local data store, not necessarily a Git repository. Machine-owned files under `entries/` are immutable evidence. Durable outcomes belong in leader-owned `decisions/`; ID corrections belong in `rekeys/`.

## Phase 1 — Survey

1. Read `config.json`. Survey is allowed on any machine; mutations require `role: leader`.
2. Run `learnings status`. Treat a missing or mixed-generation view as a hard stop.
3. Read `ACTIVE.md`, `catalog.json`, `conflicts.json`, `queue/FIX-QUEUE.md`, and prior `reviews/FIX-PLAN-*-RESULTS.md` files. On a writer, use the generated files under `fleet/` and move execution to the leader.
4. Read every evidence copy referenced by the candidate logical entries. Never infer a fleet decision from one machine's copy.
5. Check loop health before trusting the backlog:
   - Recent harvest, collection, and fixloop RUN lines must exist under `~/Library/Logs`.
   - The leader must have an authenticated unattended runtime.
   - `catalog.json` must report zero invalid files, stale acknowledgements, and rekey errors before mutation.
6. Check current state for `in_progress` items with a PR or external dependency. A merged PR is not resolved until the shipped behavior is verified.

## Phase 2 — Plan and approve

Write `reviews/FIX-PLAN-<YYYYMMDD>.md` with the current catalog generation and at most three independently verifiable work items for an inline approval. Larger plans require explicit approval before execution.

Bucket each item as:

- **Promotion** — a durable rule in an instruction, skill, hook, wrapper, or test. Prefer mechanical enforcement over prose.
- **Code fix** — one repository, ownership boundary, test gate, and PR target.
- **Operational** — scheduler, configuration, quarantine, migration, or live-system repair.
- **Conflict reconciliation** — compatible copies to acknowledge or unrelated incidents to rekey.
- **Blocked on the user** — credentials, payment, merge approval, factual judgment, or destructive action.

Rank by recurrence across machines, severity, enforceability, and verification cost. Do not optimize for closing the largest number of entries.

## Phase 3 — Execute

Capture the current generation ID from `ACTIVE.md` immediately before every leader mutation. Each successful mutation publishes a new generation, so refresh it before the next mutation.

### Safe store commands

Generate a machine-derived, high-entropy ID for new evidence or a fresh rekey target:

```sh
learnings new-id --type ERR
```

On the leader, add `--machine <source-machine>` when generating a fresh rekey
target for a collected peer copy. Writers cannot generate IDs for another machine.

Record an ordinary outcome only when the logical entry is not conflicted:

```sh
learnings decide \
  --id ERR-YYYYMMDD-MACHINETOKEN \
  --status in_progress \
  --catalog-generation <generation-from-ACTIVE> \
  --by learn-loop \
  --note "Implemented and tested; awaiting merge" \
  --target <PR-or-file> \
  --evidence <test-or-live-receipt>
```

Split one unrelated source copy without renaming or editing its evidence:

```sh
learnings rekey \
  --source-path entries/<machine>/<machine>--<old-ID>.md \
  --source-sha256 <exact-source-sha256> \
  --new-id <fresh-ID> \
  --catalog-generation <generation-from-ACTIVE> \
  --by learn-loop \
  --note "Human-reviewed same-ID collision" \
  --confirm-split
```

Add `--alias-existing` only when the reviewed source is genuinely another copy of an existing logical incident.

Acknowledge compatible divergent copies only after reading every current copy:

```sh
learnings acknowledge-conflict \
  --id <logical-ID> \
  --status <status> \
  --catalog-generation <generation-from-ACTIVE> \
  --by learn-loop \
  --note "Reviewed all current copies; differences are compatible provenance" \
  --confirm-compatible
```

The command records the exact source path and SHA-256 set. A changed, added, or deleted copy automatically makes the acknowledgement stale and reopens the conflict. Use `--replace` only after reading the existing decision and intentionally superseding it.

### Fix execution

- Use isolated worktrees and parallel workers when the environment supports them; otherwise execute sequentially. Keep exactly one owner per deliverable path.
- Read governing repository instructions and current state before edits.
- Run every declared test gate from the orchestrator environment. Treat sandbox or missing-tool failures as unverified, not as product failures.
- Exercise the real surface when safe. Verify subject identity as well as command success.
- Keep source-repository commits and PRs separate from the learnings store. Never assume the store itself should be committed or pushed.

## Phase 4 — Close the loop

1. Never edit `entries/`, `legacy/`, `ACTIVE.md`, `catalog.json`, or `conflicts.json` directly.
2. Record every touched outcome with `learnings decide` or `learnings acknowledge-conflict`:
   - `resolved` or `promoted` only after verified completion.
   - `in_progress` for an open PR or implemented-but-unverified fix.
   - `blocked` or `deferred` with a factual reason when appropriate.
3. Remove or check off completed queue lines; retain blocked and in-progress work with current references.
4. Write `reviews/FIX-PLAN-<YYYYMMDD>-RESULTS.md`, one row per item: `done-verified`, `done-unverified`, `blocked`, or `skipped`, with evidence.
5. Rebuild and validate the leader catalog, run collection, then confirm `learnings status` succeeds on every reachable writer with the same generation.
6. Report shipped PRs, verified outcomes, blockers, and everything deliberately left open.

## Guardrails

- Never invent a fix or status from a title alone; read the evidence copies and current target state.
- Never resolve a divergent-copy conflict with `decide`; acknowledge compatible copies or rekey unrelated incidents.
- Live business surfaces require a read-before-write check and the narrowest safe mutation.
- Quarantined tools remain quarantined until their fixes merge and pass live verification.
- Public changes require a full-diff privacy scan with synthetic examples and generic machine/account names.
- Never put secrets, credentials, unnecessary PII, or private evidence in commands, logs, decisions, plans, or PRs.
