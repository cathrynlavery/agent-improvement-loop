#!/usr/bin/env python3
"""Install the learnings leader schedule into a managed crontab block."""

from __future__ import annotations

import argparse
import shlex
import subprocess
from pathlib import Path


BEGIN = "# BEGIN agent-improvement-learnings"
END = "# END agent-improvement-learnings"


def build_block(
    home: Path, loop_root: Path, role: str, remote_hosts: str = ""
) -> str:
    commands = {
        "harvest": loop_root / "templates/learnings-harvest-run.sh",
        "collect": loop_root / "scripts/collect_learnings_fleet.sh",
        "fixloop": loop_root / "templates/learnings-fixloop-run.sh",
        "review": loop_root / "templates/learnings-review-run.sh",
    }
    prefix = f"HOME={shlex.quote(str(home))}"
    remote_prefix = (
        f"AGENT_LEARNINGS_REMOTE_HOSTS={shlex.quote(remote_hosts)} "
        if remote_hosts
        else ""
    )
    lines = [
        BEGIN,
        f'40 18 * * 0 {prefix} /bin/bash "{commands["harvest"]}"',
    ]
    if role == "leader":
        lines.extend(
            (
                f'10 7 * * * {prefix} {remote_prefix}/bin/bash "{commands["collect"]}"',
                f'30 7 * * * {prefix} /bin/bash "{commands["fixloop"]}"',
                f'7 9 1 * * {prefix} /bin/bash "{commands["review"]}"',
            )
        )
    lines.append(END)
    return "\n".join(lines)


def replace_managed_block(existing: str, block: str) -> str:
    lines = existing.splitlines()
    kept: list[str] = []
    inside = False
    saw_begin = False
    for line in lines:
        if line == BEGIN:
            if inside:
                raise ValueError(f"duplicate {BEGIN} marker")
            inside = True
            saw_begin = True
            continue
        if line == END:
            if not inside:
                raise ValueError(f"{END} marker without {BEGIN}")
            inside = False
            continue
        if not inside:
            kept.append(line)
    if inside:
        raise ValueError(f"{BEGIN} marker without {END}")
    if saw_begin:
        while kept and not kept[-1].strip():
            kept.pop()
    prefix = "\n".join(kept).rstrip()
    return (prefix + "\n\n" if prefix else "") + block.rstrip() + "\n"


def read_crontab() -> str:
    result = subprocess.run(
        ["crontab", "-l"], capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        return result.stdout
    if result.returncode == 1:
        return ""
    raise RuntimeError(result.stderr.strip() or "could not read crontab")


def install_crontab(value: str) -> None:
    subprocess.run(["crontab", "-"], input=value, text=True, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default="~")
    parser.add_argument(
        "--loop-root", default="~/.local/share/agent-improvement-loop"
    )
    parser.add_argument("--role", choices=("writer", "leader"), required=True)
    parser.add_argument("--remote-hosts", default="")
    args = parser.parse_args()
    home = Path(args.home).expanduser().resolve()
    loop_root = Path(args.loop_root).expanduser().resolve()
    block = build_block(home, loop_root, args.role, args.remote_hosts)
    updated = replace_managed_block(read_crontab(), block)
    install_crontab(updated)
    print(f"installed=crontab role={args.role} marker=agent-improvement-learnings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
