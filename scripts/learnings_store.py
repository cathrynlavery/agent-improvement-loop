#!/usr/bin/env python3
"""Machine-scoped learnings store migration and catalog tooling.

The live store is intentionally local to each Mac.  Entry filenames include
the source machine, while a leader can collect every machine directory and
materialize a logical catalog without allowing one replica to overwrite
another.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


SCHEMA_VERSION = 1
ENTRY_ID_RE = re.compile(r"\b(?P<id>(?:ERR|LRN|FEAT)-\d{8}-[A-Z0-9]{4,})\b", re.I)
ENTRY_HEADING_RE = re.compile(
    r"^## \[(?P<id>(?:ERR|LRN|FEAT)-\d{8}-[A-Z0-9]{4,})\][^\n]*$",
    re.I | re.M,
)
BOLD_STATUS_RE = re.compile(r"^\*\*Status\*\*:\s*(?P<value>.+?)\s*$", re.I | re.M)
YAML_STATUS_RE = re.compile(r"^status:\s*(?P<value>.+?)\s*$", re.I | re.M)

ACTIONABLE_STATUSES = {
    "active",
    "in_progress",
    "investigating",
    "open",
    "partial",
    "pending",
    "untriaged",
    "unresolved",
}
CLOSED_STATUSES = {
    "applied",
    "blocked",
    "deferred",
    "documented",
    "ignored",
    "promoted",
    "promoted_to_skill",
    "resolved",
    "wont_fix",
    "worked_around",
}
STATUS_PRIORITY = (
    "untriaged",
    "unresolved",
    "pending",
    "in_progress",
    "investigating",
    "open",
    "partial",
    "active",
    "resolved",
    "promoted_to_skill",
    "promoted",
    "applied",
    "documented",
    "worked_around",
    "blocked",
    "deferred",
    "wont_fix",
    "ignored",
    "unknown",
)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def machine_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if not slug:
        raise ValueError("machine name must contain at least one letter or number")
    return slug


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_write_bytes(path: Path, value: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_bytes(value)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_bytes(path, payload.encode("utf-8"))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_store_config(root: Path) -> dict[str, Any]:
    path = root / "config.json"
    if not path.is_file():
        raise ValueError(f"store is not initialized: missing {path}")
    config = read_json(path)
    if not isinstance(config, dict):
        raise ValueError(f"{path} must contain a JSON object")
    config["machine"] = machine_slug(str(config.get("machine") or ""))
    if config.get("role") not in {"writer", "leader"}:
        raise ValueError(f"{path} role must be writer or leader")
    return config


def initialize_store(root: Path, machine: str, role: str = "writer") -> dict[str, Any]:
    root = root.expanduser()
    machine = machine_slug(machine)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    for relative in (
        Path("entries") / machine,
        Path("legacy") / machine,
        Path("decisions"),
        Path("queue"),
        Path("reviews"),
        Path("fleet"),
    ):
        (root / relative).mkdir(parents=True, exist_ok=True, mode=0o700)

    config_path = root / "config.json"
    if config_path.exists():
        config = read_json(config_path)
        configured_machine = machine_slug(str(config.get("machine") or ""))
        if configured_machine != machine:
            raise ValueError(
                f"{config_path} belongs to {configured_machine}, not requested {machine}"
            )
        config["role"] = role
    else:
        config = {
            "schema_version": SCHEMA_VERSION,
            "machine": machine,
            "role": role,
            "created_at": utc_now(),
        }
    config["updated_at"] = utc_now()
    atomic_write_json(config_path, config)
    return config


def collision_safe_path(path: Path, payload: bytes) -> tuple[Path, str]:
    """Return a non-overwriting target and whether it is new/same/variant."""
    if not path.exists():
        return path, "new"
    existing = path.read_bytes()
    if existing == payload:
        return path, "same"
    digest = sha256_bytes(payload)[:12]
    variant = path.with_name(f"{path.stem}--{digest}{path.suffix}")
    if variant.exists() and variant.read_bytes() == payload:
        return variant, "same"
    return variant, "variant"


def copy_preserving(source: Path, target: Path) -> str:
    payload = source.read_bytes()
    resolved, result = collision_safe_path(target, payload)
    if result != "same":
        atomic_write_bytes(resolved, payload)
    return result


def normalized_entry_id(value: str) -> Optional[str]:
    match = ENTRY_ID_RE.search(value)
    return match.group("id").upper() if match else None


def split_aggregate_entries(text: str) -> Iterable[tuple[str, str]]:
    matches = list(ENTRY_HEADING_RE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start() : end].strip()
        section = re.sub(r"\n---\s*$", "", section).rstrip() + "\n"
        yield match.group("id").upper(), section


def normalize_migrated_text(text: str) -> tuple[str, bool]:
    """Give legacy evidence a triage state without changing the source snapshot."""
    status, _ = parse_status(text)
    if status != "unknown":
        return text, False
    heading = ENTRY_HEADING_RE.search(text)
    if heading:
        insertion = heading.end()
        normalized = text[:insertion] + "\n\n**Status**: untriaged" + text[insertion:]
    else:
        lines = text.splitlines(keepends=True)
        insertion_index = 1 if lines and lines[0].startswith("#") else 0
        lines.insert(insertion_index, "\n**Status**: untriaged\n")
        normalized = "".join(lines)
    return normalized, True


def write_entry(root: Path, machine: str, entry_id: str, text: str) -> str:
    payload = text.rstrip().encode("utf-8") + b"\n"
    target = root / "entries" / machine / f"{machine}--{entry_id}.md"
    resolved, result = collision_safe_path(target, payload)
    if result != "same":
        atomic_write_bytes(resolved, payload)
    return result


def migrate_store(root: Path, machine: str, source: Path, role: str) -> dict[str, Any]:
    machine = machine_slug(machine)
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"source store does not exist: {source}")
    initialize_store(root, machine, role)

    counts = {
        "source_files": 0,
        "legacy_new": 0,
        "legacy_same": 0,
        "legacy_variants": 0,
        "entries_new": 0,
        "entries_same": 0,
        "entry_variants": 0,
        "aggregate_entries": 0,
        "statuses_normalized": 0,
    }
    aggregate_names = {"ERRORS.md", "LEARNINGS.md", "FEATURE_REQUESTS.md"}

    def record_entry_result(result: str) -> None:
        key = "entry_variants" if result == "variant" else f"entries_{result}"
        counts[key] += 1

    for path in sorted(source.rglob("*")):
        if path.is_symlink() or not path.is_file() or ".git" in path.parts:
            continue
        relative = path.relative_to(source)
        counts["source_files"] += 1
        result = copy_preserving(path, root / "legacy" / machine / relative)
        counts[f"legacy_{'variants' if result == 'variant' else result}"] += 1

        if path.parent == source and path.suffix.lower() == ".md":
            text = path.read_text(encoding="utf-8", errors="replace")
            file_id = normalized_entry_id(path.name)
            if file_id:
                normalized, changed = normalize_migrated_text(text)
                counts["statuses_normalized"] += int(changed)
                result = write_entry(root, machine, file_id, normalized)
                record_entry_result(result)
            if path.name in aggregate_names:
                for entry_id, section in split_aggregate_entries(text):
                    normalized, changed = normalize_migrated_text(section)
                    counts["statuses_normalized"] += int(changed)
                    result = write_entry(root, machine, entry_id, normalized)
                    record_entry_result(result)
                    counts["aggregate_entries"] += 1

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "machine": machine,
        "source": str(source),
        "migrated_at": utc_now(),
        "counts": counts,
    }
    atomic_write_json(root / "legacy" / machine / "migration.json", manifest)
    return manifest


def normalize_existing_migration(root: Path, machine: str) -> int:
    machine = machine_slug(machine)
    changed = 0
    for path in sorted((root / "entries" / machine).glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        normalized, did_change = normalize_migrated_text(text)
        if did_change:
            atomic_write_bytes(path, normalized.rstrip().encode("utf-8") + b"\n")
            changed += 1
    return changed


def normalize_status(value: str) -> str:
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    cleaned = re.sub(r"[^a-z_]", "", cleaned)
    for status in STATUS_PRIORITY:
        if cleaned == status or cleaned.startswith(status + "_"):
            return status
    if cleaned.startswith("wontfix"):
        return "wont_fix"
    if cleaned.startswith("worked_around"):
        return "worked_around"
    if cleaned.startswith("resolved"):
        return "resolved"
    if cleaned.startswith("promoted"):
        return "promoted"
    if cleaned.startswith("pending"):
        return "pending"
    if cleaned.startswith("unresolved"):
        return "unresolved"
    if cleaned.startswith("partial"):
        return "partial"
    if cleaned.startswith("untriaged"):
        return "untriaged"
    if cleaned.startswith("blocked"):
        return "blocked"
    if cleaned.startswith("deferred"):
        return "deferred"
    if cleaned.startswith("promoted_to_skill"):
        return "promoted_to_skill"
    return "unknown"


def parse_status(text: str) -> tuple[str, str]:
    match = BOLD_STATUS_RE.search(text)
    if match:
        return normalize_status(match.group("value")), "markdown"
    match = YAML_STATUS_RE.search(text)
    if match:
        return normalize_status(match.group("value")), "yaml"
    return "unknown", "missing"


def title_from_text(text: str, entry_id: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            title = re.sub(r"^#+\s*", "", line).strip()
            title = re.sub(rf"^\[{re.escape(entry_id)}\]\s*", "", title, flags=re.I)
            return title or entry_id
    return entry_id


@dataclass(frozen=True)
class EntryCopy:
    entry_id: str
    machine: str
    path: str
    sha256: str
    status: str
    status_source: str
    title: str


def load_decisions(root: Path) -> dict[str, dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    for path in sorted((root / "decisions").glob("*.json")):
        try:
            data = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        entry_id = normalized_entry_id(path.stem)
        if not entry_id or not isinstance(data, dict):
            continue
        status = normalize_status(str(data.get("status") or ""))
        if status != "unknown":
            decisions[entry_id] = {**data, "status": status, "path": str(path)}
    return decisions


def select_status(copies: list[EntryCopy]) -> str:
    statuses = {copy.status for copy in copies}
    for status in STATUS_PRIORITY:
        if status in statuses:
            return status
    return "unknown"


def scan_entry_copies(root: Path) -> tuple[list[EntryCopy], list[dict[str, str]]]:
    copies: list[EntryCopy] = []
    invalid: list[dict[str, str]] = []
    entries_root = root / "entries"
    for path in sorted(entries_root.glob("*/*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        entry_id = normalized_entry_id(path.name) or normalized_entry_id(text)
        machine = machine_slug(path.parent.name)
        if not entry_id:
            invalid.append({"path": str(path), "reason": "missing entry ID"})
            continue
        expected_prefix = f"{machine}--{entry_id}"
        if not path.name.startswith(expected_prefix):
            invalid.append(
                {"path": str(path), "reason": f"filename must start with {expected_prefix}"}
            )
        status, source = parse_status(text)
        if status == "unknown":
            invalid.append({"path": str(path), "reason": "missing or unknown status"})
        copies.append(
            EntryCopy(
                entry_id=entry_id,
                machine=machine,
                path=str(path.relative_to(root)),
                sha256=sha256_bytes(path.read_bytes()),
                status=status,
                status_source=source,
                title=title_from_text(text, entry_id),
            )
        )
    return copies, invalid


def build_catalog(root: Path) -> dict[str, Any]:
    root = root.expanduser()
    config = load_store_config(root)
    copies, invalid = scan_entry_copies(root)
    decisions = load_decisions(root)
    grouped: dict[str, list[EntryCopy]] = {}
    for copy in copies:
        grouped.setdefault(copy.entry_id, []).append(copy)

    entries: list[dict[str, Any]] = []
    conflict_rows: list[dict[str, Any]] = []
    exact_duplicate_copies = 0
    for entry_id, entry_copies in sorted(grouped.items()):
        hashes = sorted({copy.sha256 for copy in entry_copies})
        exact_duplicate_copies += len(entry_copies) - len(hashes)
        decision = decisions.get(entry_id)
        status = select_status(entry_copies)
        effective_status = str(decision.get("status")) if decision else status
        conflict = len(hashes) > 1
        row = {
            "id": entry_id,
            "title": entry_copies[0].title,
            "status": status,
            "effective_status": effective_status,
            "actionable": effective_status in ACTIONABLE_STATUSES,
            "conflict": conflict,
            "hashes": hashes,
            "machines": sorted({copy.machine for copy in entry_copies}),
            "copies": [copy.__dict__ for copy in entry_copies],
            "decision": decision,
        }
        entries.append(row)
        if conflict:
            conflict_rows.append(
                {
                    "id": entry_id,
                    "hashes": hashes,
                    "copies": [copy.__dict__ for copy in entry_copies],
                }
            )

    summary = {
        "entry_files": len(copies),
        "logical_entries": len(entries),
        "exact_duplicate_copies": exact_duplicate_copies,
        "conflicting_ids": len(conflict_rows),
        "invalid_files": len(invalid),
        "actionable_entries": sum(1 for entry in entries if entry["actionable"]),
    }
    catalog = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "store_machine": config["machine"],
        "store_role": config["role"],
        "summary": summary,
        "invalid": invalid,
        "entries": entries,
    }
    atomic_write_json(root / "catalog.json", catalog)
    atomic_write_json(
        root / "conflicts.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generated_at": catalog["generated_at"],
            "conflicts": conflict_rows,
        },
    )

    active_lines = [
        "# Active learnings",
        "",
        "Generated from machine-owned entry files. Do not edit this file directly.",
        "",
        f"- Logical entries: {summary['logical_entries']}",
        f"- Actionable: {summary['actionable_entries']}",
        f"- Conflicting IDs: {summary['conflicting_ids']}",
        f"- Invalid files: {summary['invalid_files']}",
        "",
        "## Actionable",
        "",
    ]
    for entry in entries:
        if not entry["actionable"]:
            continue
        marker = " CONFLICT" if entry["conflict"] else ""
        sources = ", ".join(entry["machines"])
        active_lines.append(
            f"- [{entry['id']}] {entry['title']} — {entry['effective_status']} "
            f"({sources}){marker}"
        )
        for copy in entry["copies"]:
            active_lines.append(f"  - `{copy['path']}`")
    atomic_write_bytes(root / "ACTIVE.md", ("\n".join(active_lines).rstrip() + "\n").encode())
    return catalog


def validate_store(root: Path, strict: bool) -> tuple[int, dict[str, Any]]:
    catalog = build_catalog(root)
    summary = catalog["summary"]
    failed = summary["invalid_files"] > 0 or (strict and summary["conflicting_ids"] > 0)
    return (1 if failed else 0), summary


def active_view_path(root: Path) -> Path:
    """Prefer the leader-published fleet view on writers."""
    config = load_store_config(root)
    fleet_view = root / "fleet" / "ACTIVE.md"
    local_view = root / "ACTIVE.md"
    if config["role"] == "writer" and fleet_view.is_file():
        return fleet_view
    return local_view


def default_root() -> Path:
    return Path(os.environ.get("AGENT_LEARNINGS_ROOT", "~/.agents/learnings")).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(default_root()), help="learnings store root")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="initialize a machine-local store")
    init_parser.add_argument("--machine", required=True)
    init_parser.add_argument("--role", choices=("writer", "leader"), default="writer")

    migrate_parser = subparsers.add_parser("migrate", help="copy and namespace a legacy store")
    migrate_parser.add_argument("--machine", required=True)
    migrate_parser.add_argument("--source", required=True)
    migrate_parser.add_argument("--role", choices=("writer", "leader"), default="writer")

    normalize_parser = subparsers.add_parser(
        "normalize", help="add untriaged status to already-migrated legacy copies"
    )
    normalize_parser.add_argument("--machine", required=True)

    subparsers.add_parser("catalog", help="materialize catalog, conflicts, and ACTIVE.md")
    subparsers.add_parser("status", help="show the actionable fleet learning list")
    validate_parser = subparsers.add_parser("validate", help="validate and summarize the store")
    validate_parser.add_argument("--strict", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).expanduser()
    try:
        if args.command == "init":
            result = initialize_store(root, args.machine, args.role)
            print(
                f"initialized={root} machine={result['machine']} role={result['role']}"
            )
            return 0
        if args.command == "migrate":
            result = migrate_store(root, args.machine, Path(args.source), args.role)
            build_catalog(root)
            print(
                f"migrated={result['machine']} source_files={result['counts']['source_files']} "
                f"entries_new={result['counts']['entries_new']} "
                f"entry_variants={result['counts']['entry_variants']}"
            )
            return 0
        if args.command == "catalog":
            result = build_catalog(root)
            print(" ".join(f"{key}={value}" for key, value in result["summary"].items()))
            return 0
        if args.command == "status":
            path = active_view_path(root)
            if not path.is_file():
                raise ValueError(
                    f"active view is not available yet: {path}; run collection first"
                )
            sys.stdout.write(path.read_text(encoding="utf-8"))
            return 0
        if args.command == "normalize":
            changed = normalize_existing_migration(root, args.machine)
            build_catalog(root)
            print(f"normalized={changed} machine={machine_slug(args.machine)}")
            return 0
        if args.command == "validate":
            exit_code, summary = validate_store(root, args.strict)
            print(" ".join(f"{key}={value}" for key, value in summary.items()))
            return exit_code
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
