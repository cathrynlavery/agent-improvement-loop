#!/usr/bin/env python3
"""Install the machine-local learnings instruction without replacing other rules."""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


INSTRUCTION = (
    "- Self-improvement: when a command fails unexpectedly, the user corrects you, "
    "or you discover a better approach, read `~/.agents/learnings/config.json` and "
    "create a machine-prefixed `LRN`, `ERR`, or `FEAT` file under "
    "`~/.agents/learnings/entries/<machine>/` in self-improving-agent format. "
    "Use `learnings new-id --type <TYPE>` when available so the logical ID includes "
    "a machine token and high-entropy suffix. Search local entries and "
    "`fleet/catalog.json` first. Never append to aggregates; never log secrets."
)
INSTRUCTION_RE = re.compile(r"^- Self-improvement:.*$", re.M)


def update_text(text: str) -> str:
    if INSTRUCTION_RE.search(text):
        return INSTRUCTION_RE.sub(INSTRUCTION, text, count=1)
    return text.rstrip() + "\n\n## Self-improvement loop\n\n" + INSTRUCTION + "\n"


def update_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    updated = update_text(text)
    if updated == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(updated, encoding="utf-8")
    if path.exists():
        os.chmod(tmp, path.stat().st_mode & 0o777)
    os.replace(tmp, path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", default="~/.codex/AGENTS.md")
    args = parser.parse_args()
    path = Path(args.path).expanduser()
    changed = update_file(path)
    print(f"instruction={'updated' if changed else 'current'} path={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
