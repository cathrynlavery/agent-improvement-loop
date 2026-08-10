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
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Optional


SCHEMA_VERSION = 1
ENTRY_ID_RE = re.compile(r"\b(?P<id>(?:ERR|LRN|FEAT)-\d{8}-[A-Z0-9]{4,})\b", re.I)
ENTRY_ID_FULL_RE = re.compile(r"(?:ERR|LRN|FEAT)-\d{8}-[A-Z0-9]{4,}", re.I)
SHA256_RE = re.compile(r"[a-f0-9]{64}")
ENTRY_HEADING_RE = re.compile(
    r"^## \[(?P<id>(?:ERR|LRN|FEAT)-\d{8}-[A-Z0-9]{4,})\][^\n]*$",
    re.I | re.M,
)
BOLD_STATUS_RE = re.compile(r"^\*\*Status\*\*:\s*(?P<value>.+?)\s*$", re.I | re.M)
YAML_STATUS_RE = re.compile(r"^status:\s*(?P<value>.+?)\s*$", re.I | re.M)
ACTIVE_GENERATION_RE = re.compile(r"^- Generation ID: `(?P<id>[a-f0-9]{32})`$", re.M)
GENERATION_ID_RE = re.compile(r"[a-f0-9]{32}")

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


def atomic_create_json(path: Path, value: Any) -> None:
    """Publish a complete JSON file without ever replacing an existing path."""
    payload = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.create-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            if tmp.exists():
                tmp.unlink()
            raise
        os.link(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def store_lock(root: Path, name: str) -> Iterable[None]:
    """Serialize store publications while leaving evidence untouched."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / f".{name}.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
        Path("rekeys"),
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


def strict_entry_id(value: str) -> str:
    candidate = value.strip().upper()
    if not ENTRY_ID_FULL_RE.fullmatch(candidate):
        raise ValueError(
            "entry ID must match TYPE-YYYYMMDD-SUFFIX with TYPE ERR, LRN, or FEAT"
        )
    return candidate


def strict_entry_type(value: str) -> str:
    candidate = value.strip().upper()
    if candidate not in {"ERR", "LRN", "FEAT"}:
        raise ValueError("entry type must be ERR, LRN, or FEAT")
    return candidate


def strict_generation_id(value: str) -> str:
    candidate = value.strip().lower()
    if not GENERATION_ID_RE.fullmatch(candidate):
        raise ValueError("catalog generation must be a 32-character lowercase hex ID")
    return candidate


def strict_entry_date(value: str) -> str:
    candidate = value.strip()
    try:
        dt.datetime.strptime(candidate, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("entry date must be a real YYYYMMDD date") from exc
    return candidate


def strict_decision_status(value: str) -> str:
    candidate = normalize_status(value)
    if candidate not in ACTIONABLE_STATUSES | CLOSED_STATUSES:
        raise ValueError("decision status is missing or unknown")
    return candidate


def normalized_source_path(value: str) -> str:
    """Return a safe store-relative immutable evidence path."""
    path = Path(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("source path must be a normalized relative path")
    if len(path.parts) != 3 or path.parts[0] != "entries" or path.suffix != ".md":
        raise ValueError("source path must match entries/<machine>/<entry>.md")
    return path.as_posix()


def verified_entry_path(root: Path, relative: str) -> Path:
    """Resolve one entry path only when every store-owned component is non-symlinked."""
    normalized = normalized_source_path(relative)
    root = root.expanduser()
    entries_root = root / "entries"
    candidate = root / normalized
    for component in (root, entries_root, candidate.parent, candidate):
        if component.is_symlink():
            raise ValueError(f"entry path contains a symlink: {component}")
    if not entries_root.is_dir():
        raise ValueError(f"entries directory does not exist: {entries_root}")
    if not candidate.is_file():
        raise ValueError(f"entry file does not exist: {normalized}")
    entries_real = entries_root.resolve(strict=True)
    candidate_real = candidate.resolve(strict=True)
    if not candidate_real.is_relative_to(entries_real):
        raise ValueError(f"entry path escapes the entries directory: {normalized}")
    return candidate


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
    # entry_id is the effective logical ID after a validated re-key overlay.
    entry_id: str
    source_id: str
    machine: str
    path: str
    sha256: str
    source_path: str
    source_sha256: str
    status: str
    status_source: str
    title: str
    rekey: Optional[dict[str, Any]] = None


def load_rekey_mappings(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Load structurally valid re-key overlays without applying them."""
    mappings: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    for path in sorted((root / "rekeys").glob("*.json")):
        display_path = str(path.relative_to(root))
        if path.is_symlink():
            invalid.append(
                {"path": display_path, "reason": "invalid rekey mapping: file is a symlink"}
            )
            continue
        try:
            data = read_json(path)
            if not isinstance(data, dict):
                raise ValueError("mapping must be a JSON object")
            source_path = normalized_source_path(str(data.get("source_path") or ""))
            source_id = strict_entry_id(str(data.get("source_id") or ""))
            source_sha256 = str(data.get("source_sha256") or "").strip().lower()
            if not SHA256_RE.fullmatch(source_sha256):
                raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
            new_id = strict_entry_id(str(data.get("new_id") or ""))
            if new_id == source_id:
                raise ValueError("new_id must differ from source_id")
            if new_id.split("-", 1)[0] != source_id.split("-", 1)[0]:
                raise ValueError("new_id must retain the source ERR/LRN/FEAT type")
            source_token = sha256_bytes(source_path.encode("utf-8"))[:12]
            expected_name = f"{new_id}--{source_token}.json"
            if path.name != expected_name:
                raise ValueError(f"mapping filename must be exactly {expected_name}")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            invalid.append({"path": display_path, "reason": f"invalid rekey mapping: {exc}"})
            continue
        mappings.append(
            {
                **data,
                "source_path": source_path,
                "source_id": source_id,
                "source_sha256": source_sha256,
                "new_id": new_id,
                "mapping_path": display_path,
            }
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for mapping in mappings:
        grouped.setdefault(mapping["source_path"], []).append(mapping)
    duplicate_paths = {source for source, rows in grouped.items() if len(rows) > 1}
    if duplicate_paths:
        retained: list[dict[str, Any]] = []
        for mapping in mappings:
            if mapping["source_path"] in duplicate_paths:
                invalid.append(
                    {
                        "path": mapping["mapping_path"],
                        "reason": (
                            "invalid rekey mapping: multiple mappings target "
                            f"{mapping['source_path']}"
                        ),
                    }
                )
            else:
                retained.append(mapping)
        mappings = retained
    return mappings, invalid


def write_rekey_mapping(
    root: Path,
    source_path: str,
    source_sha256: str,
    new_id: str,
    *,
    actor: str = "",
    note: str = "",
) -> tuple[Path, dict[str, Any], bool]:
    """Atomically record a path-and-hash-bound logical re-key overlay."""
    load_store_config(root)
    with store_lock(root, "rekeys"):
        return _write_rekey_mapping_locked(
            root,
            source_path,
            source_sha256,
            new_id,
            actor=actor,
            note=note,
        )


def _write_rekey_mapping_locked(
    root: Path,
    source_path: str,
    source_sha256: str,
    new_id: str,
    *,
    actor: str,
    note: str,
) -> tuple[Path, dict[str, Any], bool]:
    relative = normalized_source_path(source_path)
    expected_sha256 = source_sha256.strip().lower()
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("source sha256 must be a lowercase SHA-256 digest")
    new_id = strict_entry_id(new_id)
    evidence_path = verified_entry_path(root, relative)
    actual_sha256 = sha256_bytes(evidence_path.read_bytes())
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"stale source hash for {relative}: expected {expected_sha256}, "
            f"current {actual_sha256}"
        )
    source_id = normalized_entry_id(evidence_path.name) or normalized_entry_id(
        evidence_path.read_text(encoding="utf-8", errors="replace")
    )
    if not source_id:
        raise ValueError(f"rekey source has no entry ID: {relative}")
    if new_id == source_id:
        raise ValueError("new logical ID must differ from the source ID")
    if new_id.split("-", 1)[0] != source_id.split("-", 1)[0]:
        raise ValueError("new logical ID must retain the source ERR/LRN/FEAT type")

    existing, invalid = load_rekey_mappings(root)
    if invalid:
        raise ValueError(
            "cannot write a rekey while invalid mappings exist: "
            + "; ".join(f"{row['path']}: {row['reason']}" for row in invalid)
        )
    for mapping in existing:
        if mapping["source_path"] != relative:
            continue
        if (
            mapping["source_sha256"] == expected_sha256
            and mapping["new_id"] == new_id
            and mapping["source_id"] == source_id
        ):
            return root / mapping["mapping_path"], mapping, False
        raise ValueError(
            f"source already has a different rekey mapping: {mapping['mapping_path']}"
        )

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source_path": relative,
        "source_id": source_id,
        "source_sha256": expected_sha256,
        "new_id": new_id,
        "created_at": utc_now(),
    }
    if actor.strip():
        payload["by"] = actor.strip()
    if note.strip():
        payload["note"] = note.strip()
    source_token = sha256_bytes(relative.encode("utf-8"))[:12]
    mapping_path = root / "rekeys" / f"{new_id}--{source_token}.json"
    try:
        atomic_create_json(mapping_path, payload)
    except FileExistsError:
        try:
            raced = read_json(mapping_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"rekey destination appeared concurrently and is unreadable: {mapping_path}"
            ) from exc
        identity = ("source_path", "source_id", "source_sha256", "new_id")
        if isinstance(raced, dict) and all(raced.get(key) == payload[key] for key in identity):
            return mapping_path, raced, False
        raise ValueError(
            f"rekey destination appeared concurrently with different content: {mapping_path}"
        )
    return mapping_path, payload, True


def apply_rekey_mappings(
    copies: list[EntryCopy], mappings: list[dict[str, Any]]
) -> tuple[list[EntryCopy], list[dict[str, str]], list[dict[str, Any]]]:
    """Apply only mappings whose immutable source path and hash still match."""
    by_path = {copy.source_path: copy for copy in copies}
    applied_by_path: dict[str, EntryCopy] = {}
    invalid: list[dict[str, str]] = []
    applied: list[dict[str, Any]] = []
    for mapping in mappings:
        source_path = mapping["source_path"]
        copy = by_path.get(source_path)
        if copy is None:
            invalid.append(
                {
                    "path": mapping["mapping_path"],
                    "reason": f"rekey source is missing: {source_path}",
                }
            )
            continue
        if copy.source_id != mapping["source_id"]:
            invalid.append(
                {
                    "path": mapping["mapping_path"],
                    "reason": (
                        f"rekey source ID changed for {source_path}: expected "
                        f"{mapping['source_id']}, current {copy.source_id}"
                    ),
                }
            )
            continue
        if copy.source_sha256 != mapping["source_sha256"]:
            invalid.append(
                {
                    "path": mapping["mapping_path"],
                    "reason": (
                        f"stale rekey hash for {source_path}: expected "
                        f"{mapping['source_sha256']}, current {copy.source_sha256}"
                    ),
                }
            )
            continue
        audit = {
            "mapping_path": mapping["mapping_path"],
            "source_id": copy.source_id,
            "source_path": copy.source_path,
            "source_sha256": copy.source_sha256,
            "new_id": mapping["new_id"],
        }
        applied_copy = replace(copy, entry_id=mapping["new_id"], rekey=audit)
        applied_by_path[source_path] = applied_copy
        applied.append(audit)
    return (
        [applied_by_path.get(copy.source_path, copy) for copy in copies],
        invalid,
        applied,
    )


def load_decisions(
    root: Path,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    decisions: dict[str, dict[str, Any]] = {}
    invalid: list[dict[str, str]] = []
    duplicate_ids: set[str] = set()
    for path in sorted((root / "decisions").glob("*.json")):
        display_path = str(path.relative_to(root))
        if path.is_symlink():
            invalid.append({"path": display_path, "reason": "decision file is a symlink"})
            continue
        try:
            data = read_json(path)
            if not isinstance(data, dict):
                raise ValueError("decision must be a JSON object")
            entry_id = strict_entry_id(path.stem)
            if path.name != f"{entry_id}.json":
                raise ValueError(f"decision filename must be exactly {entry_id}.json")
            if "id" in data and str(data["id"]) != entry_id:
                raise ValueError(f"decision id must exactly match {entry_id}")
            status = normalize_status(str(data.get("status") or ""))
            if status == "unknown":
                raise ValueError("decision has missing or unknown status")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            invalid.append({"path": display_path, "reason": f"invalid decision: {exc}"})
            continue
        if "accepted_hashes" in data:
            invalid.append(
                {
                    "path": display_path,
                    "reason": (
                        "invalid decision: accepted_hashes is not copy-specific; "
                        "use accepted_copies with path and sha256"
                    ),
                }
            )
            continue
        if entry_id in decisions:
            duplicate_ids.add(entry_id)
            invalid.append(
                {
                    "path": display_path,
                    "reason": f"duplicate decision for {entry_id}",
                }
            )
            continue
        decisions[entry_id] = {
            **data,
            "id": entry_id,
            "status": status,
            "path": display_path,
        }
    for entry_id in duplicate_ids:
        first = decisions.pop(entry_id, None)
        if first:
            invalid.append(
                {
                    "path": str(first["path"]),
                    "reason": f"duplicate decision for {entry_id}",
                }
            )
    return decisions, invalid


def decision_copy_acknowledgement(
    decision: Optional[dict[str, Any]], current_copies: list[EntryCopy]
) -> tuple[bool, bool, list[dict[str, str]], Optional[str]]:
    """Bind compatible-copy acknowledgement to the exact path-and-hash set."""
    if not decision or "accepted_copies" not in decision:
        return False, False, [], None
    value = decision.get("accepted_copies")
    if not isinstance(value, list):
        return True, False, [], "accepted_copies must be an array"
    accepted: list[dict[str, str]] = []
    try:
        for item in value:
            if not isinstance(item, dict):
                raise ValueError("each accepted copy must be an object")
            path = normalized_source_path(str(item.get("path") or ""))
            digest = str(item.get("sha256") or "").strip().lower()
            if not SHA256_RE.fullmatch(digest):
                raise ValueError(f"accepted copy has invalid sha256: {path}")
            accepted.append({"path": path, "sha256": digest})
    except ValueError as exc:
        return True, False, accepted, str(exc)
    pairs = [(item["path"], item["sha256"]) for item in accepted]
    if len(pairs) != len(set(pairs)) or len({path for path, _ in pairs}) != len(pairs):
        return True, False, accepted, "accepted_copies contains duplicate paths or pairs"
    accepted = sorted(accepted, key=lambda item: (item["path"], item["sha256"]))
    current = sorted(
        (
            {"path": copy.source_path, "sha256": copy.source_sha256}
            for copy in current_copies
        ),
        key=lambda item: (item["path"], item["sha256"]),
    )
    return True, accepted == current, accepted, None


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
    if root.is_symlink():
        return [], [{"path": str(root), "reason": "store root is a symlink"}]
    if entries_root.is_symlink():
        return [], [{"path": str(entries_root), "reason": "entries directory is a symlink"}]
    if not entries_root.is_dir():
        return [], [{"path": str(entries_root), "reason": "entries directory is missing"}]
    paths: list[Path] = []
    for machine_dir in sorted(entries_root.iterdir()):
        if machine_dir.is_symlink():
            invalid.append(
                {"path": str(machine_dir), "reason": "entry machine directory is a symlink"}
            )
            continue
        if machine_dir.is_dir():
            paths.extend(sorted(machine_dir.glob("*.md")))
    for discovered_path in paths:
        relative_path = str(discovered_path.relative_to(root))
        try:
            path = verified_entry_path(root, relative_path)
            payload = path.read_bytes()
        except (OSError, ValueError) as exc:
            invalid.append({"path": relative_path, "reason": f"unsafe entry path: {exc}"})
            continue
        text = payload.decode("utf-8", errors="replace")
        entry_id = normalized_entry_id(path.name) or normalized_entry_id(text)
        try:
            machine = machine_slug(path.parent.name)
        except ValueError as exc:
            invalid.append({"path": relative_path, "reason": str(exc)})
            continue
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
        digest = sha256_bytes(payload)
        copies.append(
            EntryCopy(
                entry_id=entry_id,
                source_id=entry_id,
                machine=machine,
                path=relative_path,
                sha256=digest,
                source_path=relative_path,
                source_sha256=digest,
                status=status,
                status_source=source,
                title=title_from_text(text, entry_id),
            )
        )
    return copies, invalid


def build_catalog(root: Path) -> dict[str, Any]:
    root = root.expanduser()
    with store_lock(root, "catalog-build"):
        return _build_catalog_locked(root)


def _build_catalog_locked(root: Path) -> dict[str, Any]:
    config = load_store_config(root)
    copies, invalid = scan_entry_copies(root)
    rekey_mappings, rekey_load_invalid = load_rekey_mappings(root)
    copies, rekey_apply_invalid, applied_rekeys = apply_rekey_mappings(
        copies, rekey_mappings
    )
    rekey_invalid = rekey_load_invalid + rekey_apply_invalid
    invalid.extend(rekey_invalid)
    decisions, decision_invalid = load_decisions(root)
    invalid.extend(decision_invalid)
    grouped: dict[str, list[EntryCopy]] = {}
    for copy in copies:
        grouped.setdefault(copy.entry_id, []).append(copy)

    entries: list[dict[str, Any]] = []
    conflict_rows: list[dict[str, Any]] = []
    exact_duplicate_copies = 0
    acknowledged_conflicts = 0
    stale_acknowledgements = 0
    integrity_invalid: list[dict[str, str]] = []
    for entry_id, entry_copies in sorted(grouped.items()):
        hashes = sorted({copy.sha256 for copy in entry_copies})
        exact_duplicate_copies += len(entry_copies) - len(hashes)
        decision = decisions.get(entry_id)
        status = select_status(entry_copies)
        effective_status = str(decision.get("status")) if decision else status
        raw_conflict = len(hashes) > 1
        acknowledgement_requested, copy_set_matches, accepted_copies, ack_error = (
            decision_copy_acknowledgement(decision, entry_copies)
        )
        conflict_acknowledged = raw_conflict and copy_set_matches
        acknowledgement_stale = acknowledgement_requested and not copy_set_matches
        conflict = (raw_conflict and not conflict_acknowledged) or acknowledgement_stale
        if acknowledgement_stale:
            reason = f"stale accepted_copies for {entry_id}: current copy set changed"
            if ack_error:
                reason += f" ({ack_error})"
            integrity_invalid.append(
                {
                    "path": str(decision.get("path") if decision else "decisions"),
                    "reason": reason,
                }
            )
        acknowledged_conflicts += int(conflict_acknowledged)
        stale_acknowledgements += int(acknowledgement_stale)
        row = {
            "id": entry_id,
            "title": entry_copies[0].title,
            "status": status,
            "effective_status": effective_status,
            "actionable": conflict or effective_status in ACTIONABLE_STATUSES,
            "conflict": conflict,
            "raw_conflict": raw_conflict,
            "conflict_acknowledged": conflict_acknowledged,
            "acknowledgement_stale": acknowledgement_stale,
            "accepted_copies": accepted_copies,
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
                    "accepted_copies": accepted_copies,
                    "acknowledgement_stale": acknowledgement_stale,
                    "copies": [copy.__dict__ for copy in entry_copies],
                }
            )

    for entry_id, decision in sorted(decisions.items()):
        if entry_id in grouped or "accepted_copies" not in decision:
            continue
        _, _, accepted_copies, ack_error = decision_copy_acknowledgement(decision, [])
        reason = f"stale accepted_copies for {entry_id}: all source copies are missing"
        if ack_error:
            reason += f" ({ack_error})"
        integrity_invalid.append({"path": str(decision["path"]), "reason": reason})
        stale_acknowledgements += 1
        row = {
            "id": entry_id,
            "title": entry_id,
            "status": "unknown",
            "effective_status": decision["status"],
            "actionable": True,
            "conflict": True,
            "raw_conflict": False,
            "conflict_acknowledged": False,
            "acknowledgement_stale": True,
            "accepted_copies": accepted_copies,
            "hashes": [],
            "machines": [],
            "copies": [],
            "decision": decision,
        }
        entries.append(row)
        conflict_rows.append(
            {
                "id": entry_id,
                "hashes": [],
                "accepted_copies": accepted_copies,
                "acknowledgement_stale": True,
                "copies": [],
            }
        )

    entries.sort(key=lambda entry: entry["id"])
    invalid.extend(integrity_invalid)

    summary = {
        "entry_files": len(copies),
        "logical_entries": len(entries),
        "exact_duplicate_copies": exact_duplicate_copies,
        "conflicting_ids": len(conflict_rows),
        "invalid_files": len(invalid),
        "actionable_entries": sum(1 for entry in entries if entry["actionable"]),
        "rekeys_applied": len(applied_rekeys),
        "rekey_errors": len(rekey_invalid),
        "decision_errors": len(decision_invalid),
        "integrity_errors": len(integrity_invalid),
        "acknowledged_conflicts": acknowledged_conflicts,
        "stale_acknowledgements": stale_acknowledgements,
    }
    generation_id = uuid.uuid4().hex
    generated_at = utc_now()
    catalog = {
        "schema_version": SCHEMA_VERSION,
        "generation_id": generation_id,
        "generated_at": generated_at,
        "store_machine": config["machine"],
        "store_role": config["role"],
        "summary": summary,
        "invalid": invalid,
        "rekeys": {"applied": applied_rekeys, "invalid": rekey_invalid},
        "entries": entries,
    }
    atomic_write_json(root / "catalog.json", catalog)
    atomic_write_json(
        root / "conflicts.json",
        {
            "schema_version": SCHEMA_VERSION,
            "generation_id": generation_id,
            "generated_at": generated_at,
            "conflicts": conflict_rows,
        },
    )

    active_lines = [
        "# Active learnings",
        "",
        "Generated from machine-owned entry files. Do not edit this file directly.",
        "",
        f"- Generation ID: `{generation_id}`",
        f"- Logical entries: {summary['logical_entries']}",
        f"- Actionable: {summary['actionable_entries']}",
        f"- Conflicting IDs: {summary['conflicting_ids']}",
        f"- Invalid files: {summary['invalid_files']}",
        "",
        "## Conflicts requiring review",
        "",
    ]
    conflict_entries = [entry for entry in entries if entry["conflict"]]
    if not conflict_entries:
        active_lines.append("- None.")
    for entry in conflict_entries:
        sources = ", ".join(entry["machines"]) or "missing source copies"
        reason = "STALE ACKNOWLEDGEMENT" if entry["acknowledgement_stale"] else "DIVERGENT COPIES"
        active_lines.append(
            f"- [{entry['id']}] {entry['title']} — {entry['effective_status']} "
            f"({sources}) {reason}"
        )
        for copy in entry["copies"]:
            active_lines.append(f"  - `{copy['path']}` `{copy['sha256']}`")
    active_lines.extend(["", "## Other actionable", ""])
    other_actionable = [
        entry for entry in entries if entry["actionable"] and not entry["conflict"]
    ]
    if not other_actionable:
        active_lines.append("- None.")
    for entry in entries:
        if not entry["actionable"] or entry["conflict"]:
            continue
        sources = ", ".join(entry["machines"])
        active_lines.append(
            f"- [{entry['id']}] {entry['title']} — {entry['effective_status']} "
            f"({sources})"
        )
        for copy in entry["copies"]:
            active_lines.append(f"  - `{copy['path']}`")
    atomic_write_bytes(root / "ACTIVE.md", ("\n".join(active_lines).rstrip() + "\n").encode())
    return catalog


def generated_view_errors(base: Path, *, require_all: bool) -> list[str]:
    """Check that catalog, conflicts, and ACTIVE belong to one publication."""
    paths = {
        "catalog": base / "catalog.json",
        "conflicts": base / "conflicts.json",
        "active": base / "ACTIVE.md",
    }
    present = {name: path.is_file() for name, path in paths.items()}
    if not any(present.values()) and not require_all:
        return []
    errors = [f"missing generated view: {paths[name]}" for name, exists in present.items() if not exists]
    if errors:
        return errors
    try:
        catalog = read_json(paths["catalog"])
        conflicts = read_json(paths["conflicts"])
        active_text = paths["active"].read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as exc:
        return [f"unreadable generated view under {base}: {exc}"]
    active_match = ACTIVE_GENERATION_RE.search(active_text)
    generation_ids = {
        str(catalog.get("generation_id") or ""),
        str(conflicts.get("generation_id") or ""),
        active_match.group("id") if active_match else "",
    }
    if "" in generation_ids:
        return [f"generated view is missing a generation ID under {base}"]
    if len(generation_ids) != 1:
        return [
            f"generated view generation mismatch under {base}: "
            + ", ".join(sorted(generation_ids))
        ]
    return []


def validate_store(root: Path, strict: bool) -> tuple[int, dict[str, Any]]:
    root = root.expanduser()
    preexisting_errors = generated_view_errors(root, require_all=False)
    if preexisting_errors:
        summary: dict[str, Any] = {"view_integrity_errors": len(preexisting_errors)}
        catalog_path = root / "catalog.json"
        if catalog_path.is_file():
            try:
                existing = read_json(catalog_path)
                if isinstance(existing.get("summary"), dict):
                    summary = {**existing["summary"], **summary}
            except (OSError, json.JSONDecodeError):
                pass
        return 1, summary
    catalog = build_catalog(root)
    summary = catalog["summary"]
    post_errors = generated_view_errors(root, require_all=True)
    summary["view_integrity_errors"] = len(post_errors)
    failed = (
        bool(post_errors)
        or summary["invalid_files"] > 0
        or (strict and summary["conflicting_ids"] > 0)
    )
    return (1 if failed else 0), summary


def catalog_for_mutation(root: Path, expected_generation: str) -> dict[str, Any]:
    """Load one healthy leader catalog only when its generation is still current."""
    config = load_store_config(root)
    if config["role"] != "leader":
        raise ValueError("fleet decisions and rekeys may be written only on the leader")
    view_errors = generated_view_errors(root, require_all=True)
    if view_errors:
        raise ValueError("; ".join(view_errors))
    catalog = read_json(root / "catalog.json")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("entries"), list):
        raise ValueError("catalog.json is not a valid catalog")
    expected = strict_generation_id(expected_generation)
    actual = str(catalog.get("generation_id") or "")
    if actual != expected:
        raise ValueError(
            f"stale catalog generation: expected {expected}, current {actual or 'missing'}"
        )
    summary = catalog.get("summary")
    if not isinstance(summary, dict) or int(summary.get("invalid_files", 0)) != 0:
        raise ValueError("catalog has invalid files; repair integrity errors before mutation")
    return catalog


def catalog_entry(catalog: dict[str, Any], entry_id: str) -> dict[str, Any]:
    for row in catalog["entries"]:
        if isinstance(row, dict) and row.get("id") == entry_id:
            return row
    raise ValueError(f"logical entry is not present in the current catalog: {entry_id}")


def machine_id_token(machine: str) -> str:
    token = re.sub(r"[^A-Z0-9]", "", machine.upper())[:8]
    return token or "MACHINE"


def known_entry_ids(root: Path) -> set[str]:
    copies, _ = scan_entry_copies(root)
    mappings, _ = load_rekey_mappings(root)
    decisions, _ = load_decisions(root)
    result = {copy.entry_id for copy in copies} | {copy.source_id for copy in copies}
    result.update(mapping["new_id"] for mapping in mappings)
    result.update(mapping["source_id"] for mapping in mappings)
    result.update(decisions)
    for catalog_path in (root / "catalog.json", root / "fleet/catalog.json"):
        if not catalog_path.is_file():
            continue
        try:
            catalog = read_json(catalog_path)
        except (OSError, json.JSONDecodeError):
            continue
        for row in catalog.get("entries", []) if isinstance(catalog, dict) else []:
            if isinstance(row, dict) and normalized_entry_id(str(row.get("id") or "")):
                result.add(strict_entry_id(str(row["id"])))
    return result


def generate_entry_id(
    root: Path,
    entry_type: str,
    entry_date: Optional[str] = None,
    source_machine: Optional[str] = None,
) -> str:
    """Generate a machine-derived, high-entropy logical ID not present locally."""
    config = load_store_config(root)
    prefix = strict_entry_type(entry_type)
    date = strict_entry_date(
        entry_date or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    )
    selected_machine = (
        machine_slug(source_machine) if source_machine else config["machine"]
    )
    if selected_machine != config["machine"]:
        if config["role"] != "leader":
            raise ValueError("only the leader may generate an ID for another machine")
        machine_dir = root / "entries" / selected_machine
        if machine_dir.is_symlink() or not machine_dir.is_dir():
            raise ValueError(
                f"source machine is not present in the leader store: {selected_machine}"
            )
    machine_token = machine_id_token(selected_machine)
    with store_lock(root, "ids"):
        existing = known_entry_ids(root)
        for _ in range(100):
            candidate = f"{prefix}-{date}-{machine_token}{secrets.token_hex(5).upper()}"
            if candidate not in existing:
                return candidate
    raise ValueError("could not generate a unique entry ID after 100 attempts")


def decision_payload(
    entry_id: str,
    status: str,
    *,
    actor: str,
    note: str,
    target: str = "",
    evidence: Optional[list[str]] = None,
    accepted_copies: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    actor = actor.strip()
    note = note.strip()
    if not actor:
        raise ValueError("decision recorder (--by) is required")
    if not note:
        raise ValueError("decision note is required")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": strict_entry_id(entry_id),
        "status": strict_decision_status(status),
        "decided_at": utc_now(),
        "by": actor,
        "note": note,
    }
    if target.strip():
        payload["target"] = target.strip()
    cleaned_evidence = [item.strip() for item in (evidence or []) if item.strip()]
    if cleaned_evidence:
        payload["evidence"] = cleaned_evidence
    if accepted_copies is not None:
        payload["accepted_copies"] = sorted(
            accepted_copies, key=lambda item: (item["path"], item["sha256"])
        )
    return payload


def decision_identity(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key not in {"decided_at", "path"}
    }


def write_decision_locked(
    root: Path, payload: dict[str, Any], *, replace_existing: bool
) -> tuple[Path, bool]:
    path = root / "decisions" / f"{payload['id']}.json"
    if path.is_symlink():
        raise ValueError(f"decision destination is a symlink: {path}")
    if path.exists():
        existing = read_json(path)
        if not isinstance(existing, dict):
            raise ValueError(f"existing decision is not a JSON object: {path}")
        if decision_identity(existing) == decision_identity(payload):
            return path, False
        if not replace_existing:
            raise ValueError(
                f"decision already exists for {payload['id']}; pass --replace after review"
            )
        atomic_write_json(path, payload)
        return path, True
    try:
        atomic_create_json(path, payload)
    except FileExistsError as exc:
        raise ValueError(f"decision appeared concurrently: {path}; retry from a fresh catalog") from exc
    return path, True


def decide_and_build(
    root: Path,
    entry_id: str,
    status: str,
    expected_generation: str,
    *,
    actor: str,
    note: str,
    target: str = "",
    evidence: Optional[list[str]] = None,
    replace_existing: bool = False,
) -> tuple[Path, dict[str, Any], bool, dict[str, Any]]:
    """Write an ordinary decision without allowing it to hide a conflict."""
    entry_id = strict_entry_id(entry_id)
    with store_lock(root, "catalog-build"):
        catalog = catalog_for_mutation(root, expected_generation)
        row = catalog_entry(catalog, entry_id)
        if row.get("conflict"):
            raise ValueError(
                f"{entry_id} is conflicted; rekey it or use acknowledge-conflict"
            )
        accepted_copies = None
        current_decision = row.get("decision")
        if row.get("raw_conflict") and isinstance(current_decision, dict):
            current_accepted = current_decision.get("accepted_copies")
            if isinstance(current_accepted, list):
                accepted_copies = current_accepted
        payload = decision_payload(
            entry_id,
            status,
            actor=actor,
            note=note,
            target=target,
            evidence=evidence,
            accepted_copies=accepted_copies,
        )
        with store_lock(root, "decisions"):
            path, changed = write_decision_locked(
                root, payload, replace_existing=replace_existing
            )
        refreshed = _build_catalog_locked(root) if changed else catalog
    return path, payload, changed, refreshed


def acknowledge_conflict_and_build(
    root: Path,
    entry_id: str,
    status: str,
    expected_generation: str,
    *,
    actor: str,
    note: str,
    target: str = "",
    evidence: Optional[list[str]] = None,
    replace_existing: bool = False,
    confirm_compatible: bool = False,
) -> tuple[Path, dict[str, Any], bool, dict[str, Any]]:
    """Acknowledge one human-reviewed compatible conflict at an exact copy set."""
    if not confirm_compatible:
        raise ValueError("--confirm-compatible is required after reviewing every copy")
    entry_id = strict_entry_id(entry_id)
    with store_lock(root, "catalog-build"):
        catalog = catalog_for_mutation(root, expected_generation)
        row = catalog_entry(catalog, entry_id)
        if not row.get("raw_conflict"):
            raise ValueError(f"{entry_id} is not a current divergent-copy conflict")
        if not row.get("conflict") and not row.get("conflict_acknowledged"):
            raise ValueError(f"{entry_id} is not a current divergent-copy conflict")
        copies = row.get("copies")
        if not isinstance(copies, list) or len(copies) < 2:
            raise ValueError(f"{entry_id} does not have multiple current source copies")
        accepted_copies = [
            {"path": str(copy["source_path"]), "sha256": str(copy["source_sha256"])}
            for copy in copies
        ]
        payload = decision_payload(
            entry_id,
            status,
            actor=actor,
            note=note,
            target=target,
            evidence=evidence,
            accepted_copies=accepted_copies,
        )
        with store_lock(root, "decisions"):
            path, changed = write_decision_locked(
                root, payload, replace_existing=replace_existing
            )
        refreshed = _build_catalog_locked(root) if changed else catalog
        refreshed_row = catalog_entry(refreshed, entry_id)
        if refreshed_row.get("conflict"):
            raise ValueError(f"{entry_id} remained conflicted after acknowledgement")
    return path, payload, changed, refreshed


def rekey_and_build(
    root: Path,
    source_path: str,
    source_sha256: str,
    new_id: str,
    expected_generation: str,
    *,
    actor: str,
    note: str,
    confirm_split: bool,
    alias_existing: bool,
) -> tuple[Path, dict[str, Any], bool, dict[str, Any]]:
    """Apply a reviewed rekey against one exact catalog generation."""
    if not confirm_split:
        raise ValueError("--confirm-split is required after reviewing the source copy")
    relative = normalized_source_path(source_path)
    new_id = strict_entry_id(new_id)
    with store_lock(root, "catalog-build"):
        catalog = catalog_for_mutation(root, expected_generation)
        source_row = None
        source_copy = None
        for row in catalog["entries"]:
            for copy in row.get("copies", []):
                if copy.get("source_path") == relative:
                    source_row = row
                    source_copy = copy
                    break
            if source_copy:
                break
        if source_copy is None or source_row is None:
            raise ValueError(f"rekey source is not present in the current catalog: {relative}")
        if str(source_copy.get("source_sha256")) != source_sha256.strip().lower():
            raise ValueError(f"stale source hash for {relative}")
        mappings, invalid = load_rekey_mappings(root)
        if invalid:
            raise ValueError("invalid rekey mappings must be repaired before mutation")
        same_mapping = any(
            mapping["source_path"] == relative
            and mapping["source_sha256"] == source_sha256.strip().lower()
            and mapping["new_id"] == new_id
            for mapping in mappings
        )
        existing_ids = {
            str(row.get("id")) for row in catalog["entries"] if isinstance(row, dict)
        }
        if not same_mapping and not source_row.get("conflict"):
            raise ValueError("rekey source is not part of a current conflict")
        if not same_mapping and new_id in existing_ids and not alias_existing:
            raise ValueError(
                f"{new_id} already exists; pass --alias-existing only for a reviewed alias"
            )
        if alias_existing and new_id not in existing_ids:
            raise ValueError("--alias-existing requires a logical ID already in the catalog")
        with store_lock(root, "rekeys"):
            path, mapping, created = _write_rekey_mapping_locked(
                root,
                relative,
                source_sha256,
                new_id,
                actor=actor,
                note=note,
            )
        refreshed = _build_catalog_locked(root) if created else catalog
    return path, mapping, created, refreshed


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

    new_id_parser = subparsers.add_parser(
        "new-id", help="generate a machine-derived collision-resistant entry ID"
    )
    new_id_parser.add_argument("--type", required=True, dest="entry_type")
    new_id_parser.add_argument("--date", default="", help="optional UTC date as YYYYMMDD")
    new_id_parser.add_argument(
        "--machine", default="", help="leader-only source machine override for a rekey"
    )

    rekey_parser = subparsers.add_parser(
        "rekey", help="generation-guarded mapping of one evidence copy to a logical ID"
    )
    rekey_parser.add_argument(
        "--source-path", required=True, help="store-relative entries/<machine>/<file>.md"
    )
    rekey_parser.add_argument(
        "--source-sha256", required=True, help="current SHA-256 of the immutable copy"
    )
    rekey_parser.add_argument("--new-id", required=True)
    rekey_parser.add_argument("--catalog-generation", required=True)
    rekey_parser.add_argument("--by", required=True)
    rekey_parser.add_argument("--note", required=True)
    rekey_parser.add_argument("--confirm-split", action="store_true")
    rekey_parser.add_argument("--alias-existing", action="store_true")

    decide_parser = subparsers.add_parser(
        "decide", help="write an atomic generation-guarded outcome for one logical entry"
    )
    decide_parser.add_argument("--id", required=True, dest="entry_id")
    decide_parser.add_argument("--status", required=True)
    decide_parser.add_argument("--catalog-generation", required=True)
    decide_parser.add_argument("--by", required=True)
    decide_parser.add_argument("--note", required=True)
    decide_parser.add_argument("--target", default="")
    decide_parser.add_argument("--evidence", action="append", default=[])
    decide_parser.add_argument("--replace", action="store_true")

    acknowledge_parser = subparsers.add_parser(
        "acknowledge-conflict",
        help="record that all current divergent copies were reviewed as compatible",
    )
    acknowledge_parser.add_argument("--id", required=True, dest="entry_id")
    acknowledge_parser.add_argument("--status", required=True)
    acknowledge_parser.add_argument("--catalog-generation", required=True)
    acknowledge_parser.add_argument("--by", required=True)
    acknowledge_parser.add_argument("--note", required=True)
    acknowledge_parser.add_argument("--target", default="")
    acknowledge_parser.add_argument("--evidence", action="append", default=[])
    acknowledge_parser.add_argument("--replace", action="store_true")
    acknowledge_parser.add_argument("--confirm-compatible", action="store_true")

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
        if args.command == "new-id":
            entry_id = generate_entry_id(
                root,
                args.entry_type,
                args.date or None,
                args.machine or None,
            )
            config = load_store_config(root)
            selected_machine = machine_slug(args.machine) if args.machine else config["machine"]
            print(f"id={entry_id} machine={selected_machine}")
            return 0
        if args.command == "rekey":
            path, mapping, created, catalog = rekey_and_build(
                root,
                args.source_path,
                args.source_sha256,
                args.new_id,
                args.catalog_generation,
                actor=args.by,
                note=args.note,
                confirm_split=args.confirm_split,
                alias_existing=args.alias_existing,
            )
            print(
                f"rekey={'created' if created else 'existing'} "
                f"source={mapping['source_path']} new_id={mapping['new_id']} "
                f"mapping={path} generation={catalog['generation_id']}"
            )
            return 0
        if args.command == "decide":
            path, payload, changed, catalog = decide_and_build(
                root,
                args.entry_id,
                args.status,
                args.catalog_generation,
                actor=args.by,
                note=args.note,
                target=args.target,
                evidence=args.evidence,
                replace_existing=args.replace,
            )
            print(
                f"decision={'written' if changed else 'existing'} id={payload['id']} "
                f"status={payload['status']} path={path} generation={catalog['generation_id']}"
            )
            return 0
        if args.command == "acknowledge-conflict":
            path, payload, changed, catalog = acknowledge_conflict_and_build(
                root,
                args.entry_id,
                args.status,
                args.catalog_generation,
                actor=args.by,
                note=args.note,
                target=args.target,
                evidence=args.evidence,
                replace_existing=args.replace,
                confirm_compatible=args.confirm_compatible,
            )
            print(
                f"acknowledgement={'written' if changed else 'existing'} "
                f"id={payload['id']} status={payload['status']} path={path} "
                f"generation={catalog['generation_id']}"
            )
            return 0
        if args.command == "status":
            path = active_view_path(root)
            if not path.is_file():
                raise ValueError(
                    f"active view is not available yet: {path}; run collection first"
                )
            view_errors = generated_view_errors(path.parent, require_all=True)
            if view_errors:
                raise ValueError("; ".join(view_errors))
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
