#!/usr/bin/env python3
"""Install machine-local learnings LaunchAgent definitions."""

from __future__ import annotations

import argparse
import os
import plistlib
from pathlib import Path
from typing import Any


PATH_VALUE = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LABEL_PREFIX = "io.agent-improvement-loop"


def launch_agent(
    *,
    label: str,
    script: Path,
    schedule: dict[str, int],
    home: Path,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    log = home / "Library/Logs" / f"{label}.launchd.log"
    env = {"HOME": str(home), "PATH": PATH_VALUE}
    env.update(environment or {})
    return {
        "Label": label,
        "ProgramArguments": ["/bin/bash", str(script)],
        "StartCalendarInterval": schedule,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": env,
    }


def build_plists(
    *,
    home: Path,
    loop_root: Path,
    role: str,
    harvest_minute: int,
    remote_hosts: str,
) -> dict[str, dict[str, Any]]:
    templates = loop_root / "templates"
    result = {
        f"{LABEL_PREFIX}.learnings-harvest": launch_agent(
            label=f"{LABEL_PREFIX}.learnings-harvest",
            script=templates / "learnings-harvest-run.sh",
            schedule={"Weekday": 0, "Hour": 18, "Minute": harvest_minute},
            home=home,
        )
    }
    if role == "leader":
        result.update(
            {
                f"{LABEL_PREFIX}.learnings-collect": launch_agent(
                    label=f"{LABEL_PREFIX}.learnings-collect",
                    script=loop_root / "scripts/collect_learnings_fleet.sh",
                    schedule={"Hour": 7, "Minute": 10},
                    home=home,
                    environment={"AGENT_LEARNINGS_REMOTE_HOSTS": remote_hosts},
                ),
                f"{LABEL_PREFIX}.learnings-fixloop": launch_agent(
                    label=f"{LABEL_PREFIX}.learnings-fixloop",
                    script=templates / "learnings-fixloop-run.sh",
                    schedule={"Hour": 7, "Minute": 30},
                    home=home,
                ),
                f"{LABEL_PREFIX}.learnings-review": launch_agent(
                    label=f"{LABEL_PREFIX}.learnings-review",
                    script=templates / "learnings-review-run.sh",
                    schedule={"Day": 1, "Hour": 9, "Minute": 7},
                    home=home,
                ),
            }
        )
    return result


def atomic_write_plist(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with tmp.open("wb") as handle:
        plistlib.dump(value, handle, fmt=plistlib.FMT_XML, sort_keys=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default="~")
    parser.add_argument("--loop-root", default="~/.local/share/agent-improvement-loop")
    parser.add_argument("--role", choices=("writer", "leader"), required=True)
    parser.add_argument("--harvest-minute", type=int, required=True)
    parser.add_argument("--remote-hosts", default="")
    args = parser.parse_args()
    if not 0 <= args.harvest_minute <= 59:
        parser.error("--harvest-minute must be between 0 and 59")
    home = Path(args.home).expanduser().resolve()
    loop_root = Path(args.loop_root).expanduser().resolve()
    plists = build_plists(
        home=home,
        loop_root=loop_root,
        role=args.role,
        harvest_minute=args.harvest_minute,
        remote_hosts=args.remote_hosts,
    )
    destination = home / "Library/LaunchAgents"
    for label, value in plists.items():
        path = destination / f"{label}.plist"
        atomic_write_plist(path, value)
        print(f"installed={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
