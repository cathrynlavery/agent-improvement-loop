# Learnings store protocol

Read this reference when the learn loop will mutate the fleet store or reconcile a conflict.

## Data ownership

- `entries/<machine>/` and `legacy/<machine>/` are immutable evidence owned by that machine.
- `decisions/` records effective status, target, rationale, and verification.
- `rekeys/` maps one exact source path and SHA-256 to a corrected logical ID.
- `catalog.json`, `conflicts.json`, and `ACTIVE.md` are generated views and are never edited directly.
- Only the configured leader writes decisions or rekeys.

Every mutation requires the current generation from `ACTIVE.md`. A changed mutation rebuilds the catalog and emits a new generation; refresh it before the next mutation.

## Generate an ID

Use the store generator for new evidence or a fresh rekey target:

```sh
learnings new-id --type ERR
```

Valid types are `ERR`, `LRN`, and `FEAT`. On the leader, add `--machine <source-machine>` only when generating a fresh target for a collected peer copy.

## Record an ordinary outcome

Use this only for a non-conflicted logical entry:

```sh
learnings decide \
  --id ERR-YYYYMMDD-MACHINETOKEN \
  --status in_progress \
  --catalog-generation <current-generation> \
  --by learn-loop \
  --note "Implemented and tested; awaiting merge" \
  --target <PR-or-file> \
  --evidence <test-or-live-receipt>
```

Use `resolved` or `promoted` only after the ship gate passes. Use `in_progress` for open PRs and implemented-but-unverified work. Use `blocked` or `deferred` with a factual reason. `--replace` intentionally supersedes an existing decision after it has been read.

## Split unrelated incidents

When copies share an ID but describe different incidents, preserve the evidence and rekey one copy:

```sh
learnings rekey \
  --source-path entries/<machine>/<machine>--<old-ID>.md \
  --source-sha256 <exact-source-sha256> \
  --new-id <fresh-ID> \
  --catalog-generation <current-generation> \
  --by learn-loop \
  --note "Reviewed same-ID collision" \
  --confirm-split
```

Add `--alias-existing` only when the source is genuinely another copy of an existing logical incident. Otherwise the new ID must be fresh.

## Acknowledge compatible copies

When every current copy describes the same incident and their differences are compatible:

```sh
learnings acknowledge-conflict \
  --id <logical-ID> \
  --status <effective-status> \
  --catalog-generation <current-generation> \
  --by learn-loop \
  --note "Reviewed every current copy; provenance differences are compatible" \
  --confirm-compatible
```

The acknowledgement records the exact source path and SHA-256 set. Any changed, added, or deleted copy makes it stale and reopens the conflict.

## Mutation completion criteria

A store mutation is complete only when:

1. the command reports the intended decision, acknowledgement, or rekey;
2. `learnings validate` reports zero invalid files, stale acknowledgements, rekey errors, decision errors, and integrity errors;
3. the selected logical entry has the intended effective status and conflict state;
4. a full reconciliation batch also passes `learnings validate --strict`;
5. collection publishes one generation to every reachable writer;
6. `learnings status` succeeds against that generation on each writer.
