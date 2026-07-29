# Closing the loop

The miner in this repo is the *capture* half of a self-improvement system: it reads what happened and stages proposals. This document describes the *closing* half — how staged learnings actually turn into fixes, and how the loop keeps itself honest. It comes from running the full system in production against a store of ~150 accumulated entries.

## The core finding

**Capture works. Consumption doesn't happen unless something forces it.** The store this was battle-tested on had entries re-deriving the same lesson four separate times, a rule violated again 17 days after being logged, and an error recurring after being marked "resolved". The only entries that never recurred were the ones promoted into *enforcement* — a hook or a wrapper — rather than prose in an instructions file. Hence the ordering when promoting a lesson:

> hook/wrapper (blocks the mistake) > skill (loads when relevant) > instructions-file line (prose, weakest)

## The four stages

| Stage | Cadence | Actor | What it does |
|---|---|---|---|
| **Capture** | continuous | every agent session | log errors/corrections to the store, one file per entry, filename = ID, mandatory `**Status**` field |
| **Harvest** | weekly | headless scheduled run | mine transcripts for learnings that in-session logging missed (this repo's miner) |
| **Fixloop** | daily | headless scheduled run, **no shell access** | triage new entries: promote safe rules, resolve knowledge notes, queue real work into `FIX-QUEUE.md` (`templates/fixloop-prompt.md`) |
| **Learn-loop** | on demand | interactive session with a human | execute the queue: plan → approve → fix → verify → close statuses (`skills/learn-loop/SKILL.md`) |

The split matters: the daily pass has no shell, so it can never push code or touch credentials unattended — the expensive, risky work always lands in the queue for a human-supervised session.

## Design rules that earned their place

1. **Log a line even on an empty run.** A scheduler that only logs when it finds something is indistinguishable from a dead scheduler. (Found: a "weekly" harvest that had run twice in 13 days, and a git backup dead for six weeks that nobody noticed.)
2. **Every entry carries a Status, and closing statuses is a required step, not a courtesy.** Unclosed entries are how a 150-entry backlog accumulates. When a fix ships, the entry gets a Resolution line with the commit/PR; when a rule is promoted, the entry says where.
3. **Dead-man checks are alert-only.** A freshness monitor that also "repairs" things becomes a second actor racing the first. Alert; let a human or the interactive loop decide.
4. **Stale-lock protocol.** A 0-byte `.git/index.lock` under a *running* git process is normal mid-operation state — removing it corrupts live work. Check for a live process first; only remove orphaned locks.
5. **One file per entry, filename = ID.** Aggregate append-files written by concurrent sessions on multiple machines produce sync conflicts and lost edits. Per-entry files also make `Status` greps see the whole store.
6. **Verification is a named observation, not a green exit.** Exit 0, a passing `doctor`, and an installed binary are not evidence. A row count equal to the page size is a failure signal. "Unverified" is always a legal outcome; a confident wrong number is not.
7. **The loop eats its own dog food.** The miner must filter the loop's own injected prompts and notifications out of correction detection, or the system flags itself as user friction.

## Files here

- `skills/learn-loop/SKILL.md` — the interactive execution skill (Claude Code skill format; adapt the trigger phrases to your setup).
- `templates/fixloop-prompt.md` — the daily headless triage prompt.
- `templates/learnings-fixloop-run.sh` — runner shim: invokes the headless pass, logs a RUN line unconditionally, makes a scoped commit with the stale-lock protocol.
- `examples/com.example.learnings-fixloop.plist` — macOS LaunchAgent for the daily schedule.
