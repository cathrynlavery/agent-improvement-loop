# Closing the loop

The miner in this repo is the *capture* half of a self-improvement system: it reads what happened and stages proposals. This document describes the *closing* half — how staged learnings actually turn into fixes, and how the loop keeps itself honest. It comes from running the full system in production against a store of ~150 accumulated entries.

## The core finding

**Capture works. Consumption doesn't happen unless something forces it.** The store this was battle-tested on had entries re-deriving the same lesson four separate times, a rule violated again 17 days after being logged, and an error recurring after being marked "resolved". The only entries that never recurred were the ones promoted into *enforcement* — a hook or a wrapper — rather than prose in an instructions file. Hence the ordering when promoting a lesson:

> hook/wrapper (blocks the mistake) > skill (loads when relevant) > instructions-file line (prose, weakest)

## The four stages

| Stage | Cadence | Actor | What it does |
|---|---|---|---|
| **Capture** | continuous | every agent session | log errors/corrections locally, one machine-prefixed file per entry, mandatory `**Status**` field |
| **Harvest** | weekly | headless scheduled run | mine transcripts for learnings that in-session logging missed (this repo's miner) |
| **Fixloop** | daily | headless scheduled run, **no shell access** | triage new entries: promote safe rules, resolve knowledge notes, queue real work into `FIX-QUEUE.md` (`templates/fixloop-prompt.md`) |
| **Learn-loop** | on demand | interactive session with a human | execute the queue: plan → approve → fix → verify → record decisions (`skills/learn-loop/SKILL.md`) |

The split matters: the daily pass has no shell, so it can never push code or touch credentials unattended — the expensive, risky work always lands in the queue for a human-supervised session.

## Design rules that earned their place

1. **Log a line even on an empty run.** A scheduler that only logs when it finds something is indistinguishable from a dead scheduler. (Found: a "weekly" harvest that had run twice in 13 days, and a git backup dead for six weeks that nobody noticed.)
2. **Every entry carries a captured Status, and recording the fleet outcome is required, not a courtesy.** Unclosed entries are how a 150-entry backlog accumulates. Evidence stays immutable; the leader records the effective status, commit/PR, and verification in `decisions/<ID>.json`.
3. **Dead-man checks are alert-only.** A freshness monitor that also "repairs" things becomes a second actor racing the first. Alert; let a human or the interactive loop decide.
4. **Stale-lock protocol applies only to a source repository being fixed.** The machine-local store is not assumed to be a Git repository. In a source repo, a 0-byte `.git/index.lock` under a running Git process is normal; check for a live process before treating a lock as orphaned.
5. **One file per entry, filename = machine + ID.** Aggregate append-files written by concurrent sessions on multiple machines produce sync conflicts and lost edits. Each machine owns `entries/<machine>/<machine>--<ID>.md`; a leader catalogs copies by logical ID. Same-content copies deduplicate, while divergent copies remain intact and are flagged in `conflicts.json`.
6. **Verification is a named observation, not a green exit.** Exit 0, a passing `doctor`, and an installed binary are not evidence. A row count equal to the page size is a failure signal. "Unverified" is always a legal outcome; a confident wrong number is not.
7. **Liveness signals live outside protected paths.** On macOS, launchd's `/bin/bash` may be unable to write TCC-protected or cloud-synced directories even while the agent process it spawns can — so a run can succeed while its dead-man log line fails silently, the exact blindness the line exists to prevent. Write the RUN log to a plainly writable location (`~/Library/Logs`); a copy inside the store is a mirror, never the signal.
8. **The loop eats its own dog food.** The miner must filter the loop's own injected prompts and notifications out of correction detection, or the system flags itself as user friction.

## Files here

- `skills/learn-loop/SKILL.md` — the interactive execution skill (Claude Code skill format; adapt the trigger phrases to your setup).
- `templates/fixloop-prompt.md` — the daily headless triage prompt.
- `templates/learnings-fixloop-run.sh` — leader-only runner: catalogs evidence, invokes the headless pass, records decisions, and logs a RUN line unconditionally.
- `templates/learnings-harvest-run.sh` — machine-local capture runner with external liveness logging.
- `scripts/collect_learnings_fleet.sh` — leader collector for machine-owned entries and catalog publication.
- `examples/com.example.learnings-fixloop.plist` — macOS LaunchAgent for the daily schedule.

## Machine-local fleet store

Do not use a cloud-synced directory as the live multi-writer database. Each Mac
keeps its canonical evidence under `~/.agents/learnings/`:

```text
~/.agents/learnings/
  config.json
  entries/<machine>/<machine>--<ID>.md
  legacy/<machine>/...
  decisions/<ID>.json
  rekeys/<new-ID>--<source-token>.json
  queue/FIX-QUEUE.md
  catalog.json
  conflicts.json
  ACTIVE.md
```

`bin/learnings-store migrate` copies a legacy store without deleting it, keeps a
machine-namespaced snapshot, and extracts individual entries from legacy aggregate
files. `scripts/collect_learnings_fleet.sh` runs on one leader, copies only the
machine-owned entry directories, rebuilds the catalog, and publishes the generated
catalog back to reachable peers. Evidence remains immutable; triage outcomes are
separate decisions, so collection cannot overwrite them.

Re-key overlays bind an immutable copy's relative path and SHA-256 to a new logical
ID; they never rename or rewrite the evidence file. Compatible divergent copies
may be acknowledged only with their exact current set of source paths and SHA-256
values in a decision. Any changed, added, or deleted copy makes that
acknowledgement stale and returns the ID to `conflicts.json`.

Use `learnings new-id` for machine-derived high-entropy IDs, `learnings decide`
for ordinary outcomes, and `learnings acknowledge-conflict` only after reviewing
every current copy. Leader mutations require the catalog generation printed in
`ACTIVE.md`; stale plans fail instead of writing against a changed fleet snapshot.

Use `scripts/install_learnings_launchd.py` on Macs with a logged-in GUI session.
For an always-headless leader with no `gui/<uid>` launchd domain, use
`scripts/install_learnings_cron.py`; it owns only the marked learnings block and
preserves unrelated crontab entries.

The leader must have an authenticated headless agent runtime. A machine without
one remains a writer; do not schedule a collector/fixloop there just because it is
always online.

If a peer is asleep during initial rollout, `scripts/bootstrap_learnings_peer.sh`
is a retry-safe writer bootstrap: it deploys the runtime, migrates the untouched
legacy store, updates the agent instruction, installs capture, runs a liveness
check, and writes a completion marker only after all steps succeed.
