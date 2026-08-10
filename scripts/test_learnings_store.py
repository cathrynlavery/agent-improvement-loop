import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import learnings_store as store
import install_learnings_cron as cron
import install_learnings_launchd as launchd
import update_learnings_instruction as instruction


def entry(entry_id: str, status: str = "pending", summary: str = "Example") -> str:
    return f"""## [{entry_id}] best_practice

**Logged**: 2026-08-10T00:00:00Z
**Priority**: high
**Status**: {status}
**Area**: infra

### Summary

{summary}
"""


class LearningsStoreTests(unittest.TestCase):
    def test_cron_managed_block_is_idempotent_and_preserves_other_jobs(self):
        home = Path("/Users/example")
        loop_root = home / ".local/share/agent-improvement-loop"
        block = cron.build_block(
            home, loop_root, "leader", "desktop laptop notebook mini"
        )
        existing = "0 8 * * * /usr/local/bin/other\n"
        first = cron.replace_managed_block(existing, block)
        second = cron.replace_managed_block(first, block)
        self.assertEqual(first, second)
        self.assertIn("/usr/local/bin/other", first)
        self.assertIn("collect_learnings_fleet.sh", first)
        self.assertIn("AGENT_LEARNINGS_REMOTE_HOSTS='desktop laptop notebook mini'", first)
        self.assertEqual(first.count(cron.BEGIN), 1)

        writer = cron.build_block(home, loop_root, "writer")
        self.assertIn("learnings-harvest-run.sh", writer)
        self.assertNotIn("collect_learnings_fleet.sh", writer)

    def test_launchd_writer_and_leader_roles(self):
        home = Path("/Users/example")
        loop_root = home / ".local/share/agent-improvement-loop"
        writer = launchd.build_plists(
            home=home,
            loop_root=loop_root,
            role="writer",
            harvest_minute=20,
            remote_hosts="",
        )
        leader = launchd.build_plists(
            home=home,
            loop_root=loop_root,
            role="leader",
            harvest_minute=40,
            remote_hosts="desktop laptop notebook mini",
        )
        prefix = "io.agent-improvement-loop"
        self.assertEqual(set(writer), {f"{prefix}.learnings-harvest"})
        self.assertEqual(
            set(leader),
            {
                f"{prefix}.learnings-harvest",
                f"{prefix}.learnings-collect",
                f"{prefix}.learnings-fixloop",
                f"{prefix}.learnings-review",
            },
        )
        collect_env = leader[f"{prefix}.learnings-collect"]["EnvironmentVariables"]
        self.assertEqual(collect_env["AGENT_LEARNINGS_REMOTE_HOSTS"], "desktop laptop notebook mini")
        self.assertEqual(
            writer[f"{prefix}.learnings-harvest"]["StartCalendarInterval"]["Minute"],
            20,
        )

    def test_instruction_update_replaces_legacy_path_and_is_idempotent(self):
        legacy = (
            "# Rules\n\n- Self-improvement: append to "
            "~/old-agent-learnings/.\n"
        )
        updated = instruction.update_text(legacy)
        self.assertIn("~/.agents/learnings/config.json", updated)
        self.assertNotIn("Library/CloudStorage", updated)
        self.assertEqual(instruction.update_text(updated), updated)

    def test_instruction_update_adds_section_when_missing(self):
        updated = instruction.update_text("# Rules\n")
        self.assertIn("## Self-improvement loop", updated)
        self.assertIn(instruction.INSTRUCTION, updated)

    def test_active_view_prefers_published_fleet_view_on_writer(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store.initialize_store(root, "laptop", "writer")
            (root / "ACTIVE.md").write_text("local", encoding="utf-8")
            (root / "fleet/ACTIVE.md").write_text("fleet", encoding="utf-8")
            self.assertEqual(store.active_view_path(root), root / "fleet/ACTIVE.md")

            store.initialize_store(root, "laptop", "leader")
            self.assertEqual(store.active_view_path(root), root / "ACTIVE.md")

    def test_machine_slug_normalizes_computer_names(self):
        self.assertEqual(store.machine_slug("Team-Mac-Desktop"), "team-mac-desktop")
        self.assertEqual(store.machine_slug("Family’s Mac mini"), "familys-mac-mini")

    def test_migration_prefixes_entry_with_machine_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "old"
            root = Path(td) / "new"
            source.mkdir()
            (source / "ERR-20260810-A7K2.md").write_text(
                entry("ERR-20260810-A7K2"), encoding="utf-8"
            )

            first = store.migrate_store(root, "laptop", source, "writer")
            second = store.migrate_store(root, "laptop", source, "writer")

            target = root / "entries/laptop/laptop--ERR-20260810-A7K2.md"
            self.assertTrue(target.exists())
            self.assertEqual(first["counts"]["entries_new"], 1)
            self.assertEqual(second["counts"]["entries_same"], 1)
            self.assertEqual(list((root / "entries/laptop").glob("*.md")), [target])

    def test_aggregate_entries_are_split_and_legacy_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "old"
            root = Path(td) / "new"
            source.mkdir()
            aggregate = "# Learnings\n\n" + entry("LRN-20260810-B8M3")
            aggregate += "\n---\n\n" + entry("LRN-20260810-C9N4", "resolved")
            (source / "LEARNINGS.md").write_text(aggregate, encoding="utf-8")

            result = store.migrate_store(root, "desktop", source, "writer")

            self.assertEqual(result["counts"]["aggregate_entries"], 2)
            self.assertTrue((root / "legacy/desktop/LEARNINGS.md").exists())
            self.assertTrue(
                (root / "entries/desktop/desktop--LRN-20260810-B8M3.md").exists()
            )
            self.assertTrue(
                (root / "entries/desktop/desktop--LRN-20260810-C9N4.md").exists()
            )

    def test_same_id_same_content_is_logically_deduplicated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            text = entry("ERR-20260810-D2P5")
            store.initialize_store(root, "server", "leader")
            for machine in ("desktop", "laptop"):
                store.write_entry(root, machine, "ERR-20260810-D2P5", text)

            catalog = store.build_catalog(root)

            self.assertEqual(catalog["summary"]["entry_files"], 2)
            self.assertEqual(catalog["summary"]["logical_entries"], 1)
            self.assertEqual(catalog["summary"]["exact_duplicate_copies"], 1)
            self.assertEqual(catalog["summary"]["conflicting_ids"], 0)

    def test_same_id_different_content_is_preserved_and_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store.initialize_store(root, "server", "leader")
            for machine, summary in (("desktop", "One"), ("laptop", "Two")):
                store.write_entry(
                    root,
                    machine,
                    "LRN-20260810-E3Q6",
                    entry("LRN-20260810-E3Q6", summary=summary),
                )

            catalog = store.build_catalog(root)
            conflicts = json.loads((root / "conflicts.json").read_text(encoding="utf-8"))

            self.assertEqual(catalog["summary"]["logical_entries"], 1)
            self.assertEqual(catalog["summary"]["conflicting_ids"], 1)
            self.assertEqual(len(conflicts["conflicts"][0]["copies"]), 2)
            self.assertTrue(
                (root / "entries/desktop/desktop--LRN-20260810-E3Q6.md").exists()
            )
            self.assertTrue(
                (root / "entries/laptop/laptop--LRN-20260810-E3Q6.md").exists()
            )

    def test_rekey_cli_changes_only_logical_identity_and_retains_source_audit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_id = "ERR-20260810-R4K7"
            new_id = "ERR-20260810-N8W3"
            store.initialize_store(root, "server", "leader")
            self.assertTrue((root / "rekeys").is_dir())
            store.write_entry(root, "desktop", source_id, entry(source_id, summary="One"))
            store.write_entry(root, "laptop", source_id, entry(source_id, summary="Two"))
            source_path = root / f"entries/laptop/laptop--{source_id}.md"
            source_bytes = source_path.read_bytes()
            source_sha256 = store.sha256_bytes(source_bytes)
            generation = store.build_catalog(root)["generation_id"]

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = store.main(
                    [
                        "--root",
                        str(root),
                        "rekey",
                        "--source-path",
                        str(source_path.relative_to(root)),
                        "--source-sha256",
                        source_sha256,
                        "--new-id",
                        new_id,
                        "--catalog-generation",
                        generation,
                        "--by",
                        "test",
                        "--note",
                        "Reviewed split",
                        "--confirm-split",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("rekey=created", stdout.getvalue())
            self.assertEqual(source_path.read_bytes(), source_bytes)
            self.assertFalse(
                (root / f"entries/laptop/laptop--{new_id}.md").exists(),
                "re-key overlays must not rename or duplicate evidence",
            )
            self.assertEqual(len(list((root / "rekeys").glob("*.json"))), 1)

            catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["summary"]["logical_entries"], 2)
            self.assertEqual(catalog["summary"]["rekeys_applied"], 1)
            mapped = next(row for row in catalog["entries"] if row["id"] == new_id)
            copy = mapped["copies"][0]
            self.assertEqual(copy["entry_id"], new_id)
            self.assertEqual(copy["source_id"], source_id)
            self.assertEqual(copy["source_path"], str(source_path.relative_to(root)))
            self.assertEqual(copy["source_sha256"], source_sha256)
            self.assertEqual(copy["path"], copy["source_path"])
            self.assertEqual(copy["sha256"], copy["source_sha256"])
            self.assertEqual(copy["rekey"]["new_id"], new_id)

    def test_stale_rekey_hash_fails_at_write_and_is_flagged_after_source_changes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_id = "ERR-20260810-S5L8"
            new_id = "ERR-20260810-P9X4"
            store.initialize_store(root, "server", "leader")
            store.write_entry(root, "laptop", source_id, entry(source_id, summary="Original"))
            source_path = root / f"entries/laptop/laptop--{source_id}.md"
            relative = str(source_path.relative_to(root))
            correct_hash = store.sha256_bytes(source_path.read_bytes())
            generation = store.build_catalog(root)["generation_id"]

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = store.main(
                    [
                        "--root",
                        str(root),
                        "rekey",
                        "--source-path",
                        relative,
                        "--source-sha256",
                        "0" * 64,
                        "--new-id",
                        new_id,
                        "--catalog-generation",
                        generation,
                        "--by",
                        "test",
                        "--note",
                        "Reviewed split",
                        "--confirm-split",
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertIn("stale source hash", stderr.getvalue())
            self.assertEqual(list((root / "rekeys").glob("*.json")), [])

            store.write_rekey_mapping(root, relative, correct_hash, new_id)
            source_path.write_text(
                entry(source_id, summary="Changed after mapping"), encoding="utf-8"
            )
            catalog = store.build_catalog(root)

            self.assertNotIn(new_id, {row["id"] for row in catalog["entries"]})
            self.assertEqual(catalog["summary"]["rekeys_applied"], 0)
            self.assertEqual(catalog["summary"]["rekey_errors"], 1)
            self.assertTrue(
                any("stale rekey hash" in row["reason"] for row in catalog["invalid"])
            )
            original = next(row for row in catalog["entries"] if row["id"] == source_id)
            self.assertIsNone(original["copies"][0]["rekey"])

    def test_new_id_uses_machine_token_and_high_entropy_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store.initialize_store(root, "Team-Mac-Desktop", "writer")

            with mock.patch("learnings_store.secrets.token_hex", return_value="a1b2c3d4e5"):
                entry_id = store.generate_entry_id(root, "err", "20260810")

            self.assertEqual(entry_id, "ERR-20260810-TEAMMACDA1B2C3D4E5")

    def test_new_id_retries_when_generated_candidate_already_exists(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store.initialize_store(root, "desktop", "writer")
            first_id = "ERR-20260810-DESKTOP1111111111"
            store.write_entry(root, "desktop", first_id, entry(first_id))

            with mock.patch(
                "learnings_store.secrets.token_hex",
                side_effect=["1111111111", "2222222222"],
            ):
                entry_id = store.generate_entry_id(root, "ERR", "20260810")

            self.assertEqual(entry_id, "ERR-20260810-DESKTOP2222222222")

    def test_only_leader_can_generate_an_id_for_a_collected_source_machine(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store.initialize_store(root, "desktop", "writer")
            (root / "entries/laptop").mkdir()

            with self.assertRaisesRegex(ValueError, "only the leader"):
                store.generate_entry_id(root, "ERR", "20260810", "laptop")

            store.initialize_store(root, "desktop", "leader")
            with mock.patch("learnings_store.secrets.token_hex", return_value="abcdef1234"):
                entry_id = store.generate_entry_id(
                    root, "ERR", "20260810", "laptop"
                )

            self.assertEqual(entry_id, "ERR-20260810-LAPTOPABCDEF1234")

    def test_decide_requires_current_generation_and_cannot_hide_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry_id = "ERR-20260810-DESKTOP1A2B3C4D5E"
            store.initialize_store(root, "desktop", "leader")
            store.write_entry(root, "desktop", entry_id, entry(entry_id))
            catalog = store.build_catalog(root)

            with self.assertRaisesRegex(ValueError, "stale catalog generation"):
                store.decide_and_build(
                    root,
                    entry_id,
                    "resolved",
                    "0" * 32,
                    actor="test",
                    note="Verified",
                )
            self.assertFalse((root / f"decisions/{entry_id}.json").exists())

            path, payload, changed, refreshed = store.decide_and_build(
                root,
                entry_id,
                "resolved",
                catalog["generation_id"],
                actor="test",
                note="Verified",
                evidence=["test:test_decide"],
            )

            self.assertTrue(changed)
            self.assertEqual(path.name, f"{entry_id}.json")
            self.assertEqual(payload["status"], "resolved")
            self.assertNotEqual(refreshed["generation_id"], catalog["generation_id"])
            row = next(item for item in refreshed["entries"] if item["id"] == entry_id)
            self.assertFalse(row["actionable"])

            _, _, repeated, repeat_catalog = store.decide_and_build(
                root,
                entry_id,
                "resolved",
                refreshed["generation_id"],
                actor="test",
                note="Verified",
                evidence=["test:test_decide"],
            )
            self.assertFalse(repeated)
            self.assertEqual(repeat_catalog["generation_id"], refreshed["generation_id"])

            with self.assertRaisesRegex(ValueError, "--replace"):
                store.decide_and_build(
                    root,
                    entry_id,
                    "in_progress",
                    refreshed["generation_id"],
                    actor="test",
                    note="Different outcome",
                )

            _, replacement, replaced, after_replace = store.decide_and_build(
                root,
                entry_id,
                "in_progress",
                refreshed["generation_id"],
                actor="test",
                note="Different outcome",
                replace_existing=True,
            )
            self.assertTrue(replaced)
            self.assertEqual(replacement["status"], "in_progress")
            replaced_row = next(
                item for item in after_replace["entries"] if item["id"] == entry_id
            )
            self.assertTrue(replaced_row["actionable"])

            conflict_id = "LRN-20260810-DESKTOP2B3C4D5E6F"
            store.write_entry(root, "desktop", conflict_id, entry(conflict_id, summary="One"))
            store.write_entry(root, "laptop", conflict_id, entry(conflict_id, summary="Two"))
            conflict_catalog = store.build_catalog(root)
            with self.assertRaisesRegex(ValueError, "acknowledge-conflict"):
                store.decide_and_build(
                    root,
                    conflict_id,
                    "resolved",
                    conflict_catalog["generation_id"],
                    actor="test",
                    note="Must not hide conflict",
                )

    def test_acknowledge_conflict_binds_decision_to_current_copy_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry_id = "LRN-20260810-DESKTOP3C4D5E6F7A"
            store.initialize_store(root, "desktop", "leader")
            store.write_entry(root, "desktop", entry_id, entry(entry_id, summary="One"))
            store.write_entry(root, "laptop", entry_id, entry(entry_id, summary="Two"))
            catalog = store.build_catalog(root)

            with self.assertRaisesRegex(ValueError, "--confirm-compatible"):
                store.acknowledge_conflict_and_build(
                    root,
                    entry_id,
                    "in_progress",
                    catalog["generation_id"],
                    actor="test",
                    note="Compatible evidence",
                )

            path, payload, changed, refreshed = store.acknowledge_conflict_and_build(
                root,
                entry_id,
                "in_progress",
                catalog["generation_id"],
                actor="test",
                note="Compatible evidence",
                confirm_compatible=True,
            )

            self.assertTrue(changed)
            self.assertTrue(path.exists())
            self.assertEqual(len(payload["accepted_copies"]), 2)
            row = next(item for item in refreshed["entries"] if item["id"] == entry_id)
            self.assertTrue(row["raw_conflict"])
            self.assertTrue(row["conflict_acknowledged"])
            self.assertFalse(row["conflict"])
            self.assertTrue(row["actionable"])

            _, _, repeated, repeat_catalog = store.acknowledge_conflict_and_build(
                root,
                entry_id,
                "in_progress",
                refreshed["generation_id"],
                actor="test",
                note="Compatible evidence",
                confirm_compatible=True,
            )
            self.assertFalse(repeated)
            self.assertEqual(repeat_catalog["generation_id"], refreshed["generation_id"])

    def test_rekey_requires_explicit_alias_for_an_existing_logical_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_id = "ERR-20260810-DESKTOP4444444444"
            target_id = "ERR-20260810-LAPTOP5555555555"
            store.initialize_store(root, "desktop", "leader")
            store.write_entry(root, "desktop", source_id, entry(source_id, summary="One"))
            store.write_entry(root, "laptop", source_id, entry(source_id, summary="Two"))
            store.write_entry(root, "server", target_id, entry(target_id, summary="Alias"))
            catalog = store.build_catalog(root)
            source_path = root / f"entries/laptop/laptop--{source_id}.md"
            relative = str(source_path.relative_to(root))
            digest = store.sha256_bytes(source_path.read_bytes())

            with self.assertRaisesRegex(ValueError, "--alias-existing"):
                store.rekey_and_build(
                    root,
                    relative,
                    digest,
                    target_id,
                    catalog["generation_id"],
                    actor="test",
                    note="Reviewed alias",
                    confirm_split=True,
                    alias_existing=False,
                )

            _, _, created, refreshed = store.rekey_and_build(
                root,
                relative,
                digest,
                target_id,
                catalog["generation_id"],
                actor="test",
                note="Reviewed alias",
                confirm_split=True,
                alias_existing=True,
            )

            self.assertTrue(created)
            target = next(row for row in refreshed["entries"] if row["id"] == target_id)
            self.assertEqual(len(target["copies"]), 2)

    def test_decision_acknowledges_only_the_exact_current_conflict_hash_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry_id = "LRN-20260810-T6M9"
            store.initialize_store(root, "server", "leader")
            store.write_entry(root, "desktop", entry_id, entry(entry_id, summary="One"))
            store.write_entry(root, "laptop", entry_id, entry(entry_id, summary="Two"))
            first = store.build_catalog(root)
            accepted_copies = [
                {"path": copy["source_path"], "sha256": copy["source_sha256"]}
                for copy in first["entries"][0]["copies"]
            ]
            store.atomic_write_json(
                root / f"decisions/{entry_id}.json",
                {
                    "status": "resolved",
                    "accepted_copies": accepted_copies,
                    "decided_at": "2026-08-10T01:00:00Z",
                },
            )

            catalog = store.build_catalog(root)
            row = catalog["entries"][0]
            conflicts = json.loads((root / "conflicts.json").read_text(encoding="utf-8"))

            self.assertTrue(row["raw_conflict"])
            self.assertTrue(row["conflict_acknowledged"])
            self.assertFalse(row["conflict"])
            self.assertFalse(row["acknowledgement_stale"])
            self.assertEqual(row["accepted_copies"], sorted(accepted_copies, key=lambda copy: copy["path"]))
            self.assertEqual(catalog["summary"]["conflicting_ids"], 0)
            self.assertEqual(catalog["summary"]["acknowledged_conflicts"], 1)
            self.assertEqual(conflicts["conflicts"], [])

    def test_new_hash_reopens_an_acknowledged_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry_id = "FEAT-20260810-U7N2"
            store.initialize_store(root, "server", "leader")
            store.write_entry(root, "desktop", entry_id, entry(entry_id, summary="One"))
            store.write_entry(root, "laptop", entry_id, entry(entry_id, summary="Two"))
            first = store.build_catalog(root)
            accepted_copies = [
                {"path": copy["source_path"], "sha256": copy["source_sha256"]}
                for copy in first["entries"][0]["copies"]
            ]
            store.atomic_write_json(
                root / f"decisions/{entry_id}.json",
                {"status": "resolved", "accepted_copies": accepted_copies},
            )
            acknowledged = store.build_catalog(root)
            self.assertEqual(acknowledged["summary"]["conflicting_ids"], 0)

            store.write_entry(root, "mini", entry_id, entry(entry_id, summary="Three"))
            reopened = store.build_catalog(root)
            row = reopened["entries"][0]
            conflicts = json.loads((root / "conflicts.json").read_text(encoding="utf-8"))

            self.assertTrue(row["raw_conflict"])
            self.assertFalse(row["conflict_acknowledged"])
            self.assertTrue(row["acknowledgement_stale"])
            self.assertTrue(row["conflict"])
            self.assertEqual(row["accepted_copies"], sorted(accepted_copies, key=lambda copy: copy["path"]))
            self.assertEqual(len(row["hashes"]), 3)
            self.assertEqual(reopened["summary"]["conflicting_ids"], 1)
            self.assertEqual(reopened["summary"]["stale_acknowledgements"], 1)
            self.assertEqual(conflicts["conflicts"][0]["id"], entry_id)

    def test_deleted_copy_reopens_an_acknowledged_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry_id = "ERR-20260810-V8P3"
            store.initialize_store(root, "desktop", "leader")
            store.write_entry(root, "desktop", entry_id, entry(entry_id, summary="One"))
            store.write_entry(root, "laptop", entry_id, entry(entry_id, summary="Two"))
            first = store.build_catalog(root)
            accepted_copies = [
                {"path": copy["source_path"], "sha256": copy["source_sha256"]}
                for copy in first["entries"][0]["copies"]
            ]
            store.atomic_write_json(
                root / f"decisions/{entry_id}.json",
                {"status": "resolved", "accepted_copies": accepted_copies},
            )
            store.build_catalog(root)

            (root / f"entries/laptop/laptop--{entry_id}.md").unlink()
            reopened = store.build_catalog(root)
            row = reopened["entries"][0]

            self.assertTrue(row["conflict"])
            self.assertTrue(row["acknowledgement_stale"])
            self.assertTrue(row["actionable"])
            self.assertEqual(reopened["summary"]["integrity_errors"], 1)
            self.assertEqual(reopened["summary"]["conflicting_ids"], 1)

    def test_legacy_hash_only_acknowledgement_is_invalid_and_cannot_hide_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry_id = "LRN-20260810-W9Q4"
            store.initialize_store(root, "desktop", "leader")
            store.write_entry(root, "desktop", entry_id, entry(entry_id, summary="One"))
            store.write_entry(root, "laptop", entry_id, entry(entry_id, summary="Two"))
            first = store.build_catalog(root)
            store.atomic_write_json(
                root / f"decisions/{entry_id}.json",
                {
                    "status": "resolved",
                    "accepted_hashes": first["entries"][0]["hashes"],
                },
            )

            catalog = store.build_catalog(root)
            row = catalog["entries"][0]

            self.assertTrue(row["conflict"])
            self.assertTrue(row["actionable"])
            self.assertEqual(catalog["summary"]["decision_errors"], 1)
            self.assertEqual(row["effective_status"], "pending")

    def test_symlinked_machine_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"
            outside = Path(td) / "outside"
            outside.mkdir()
            entry_id = "FEAT-20260810-X2R5"
            (outside / f"laptop--{entry_id}.md").write_text(
                entry(entry_id), encoding="utf-8"
            )
            store.initialize_store(root, "desktop", "leader")
            (root / "entries/laptop").symlink_to(outside, target_is_directory=True)

            catalog = store.build_catalog(root)

            self.assertEqual(catalog["summary"]["logical_entries"], 0)
            self.assertTrue(
                any(
                    row["reason"] == "entry machine directory is a symlink"
                    for row in catalog["invalid"]
                )
            )

    def test_status_rejects_partially_published_fleet_generation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store.initialize_store(root, "laptop", "writer")
            store.atomic_write_json(root / "fleet/catalog.json", {"generation_id": "a" * 32})
            store.atomic_write_json(root / "fleet/conflicts.json", {"generation_id": "b" * 32})
            (root / "fleet/ACTIVE.md").write_text(
                f"# Active learnings\n\n- Generation ID: `{'a' * 32}`\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                exit_code = store.main(["--root", str(root), "status"])

            self.assertEqual(exit_code, 2)
            self.assertIn("generation mismatch", stderr.getvalue())

    def test_yaml_status_is_accepted_and_missing_status_is_reported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store.initialize_store(root, "server", "leader")
            store.write_entry(
                root,
                "server",
                "FEAT-20260810-F4R7",
                "---\nstatus: unresolved\n---\n# Feature\n",
            )
            store.write_entry(
                root,
                "server",
                "ERR-20260810-G5S8",
                "# Missing status\n",
            )

            catalog = store.build_catalog(root)

            first = next(row for row in catalog["entries"] if row["id"].endswith("F4R7"))
            self.assertEqual(first["effective_status"], "unresolved")
            self.assertTrue(first["actionable"])
            self.assertEqual(catalog["summary"]["invalid_files"], 1)

    def test_migration_marks_missing_status_untriaged_but_preserves_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "old"
            root = Path(td) / "new"
            source.mkdir()
            original = "# ERR-20260810-J7V2\n\nNo status in this old entry.\n"
            (source / "ERR-20260810-J7V2.md").write_text(original, encoding="utf-8")

            result = store.migrate_store(root, "laptop", source, "writer")
            catalog = store.build_catalog(root)

            migrated = root / "entries/laptop/laptop--ERR-20260810-J7V2.md"
            legacy = root / "legacy/laptop/ERR-20260810-J7V2.md"
            self.assertIn("**Status**: untriaged", migrated.read_text(encoding="utf-8"))
            self.assertEqual(legacy.read_text(encoding="utf-8"), original)
            self.assertEqual(result["counts"]["statuses_normalized"], 1)
            self.assertEqual(catalog["summary"]["invalid_files"], 0)
            self.assertEqual(catalog["summary"]["actionable_entries"], 1)

    def test_decision_overrides_source_status_without_mutating_peer_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store.initialize_store(root, "server", "leader")
            store.write_entry(
                root,
                "server",
                "ERR-20260810-H6T9",
                entry("ERR-20260810-H6T9"),
            )
            store.atomic_write_json(
                root / "decisions/ERR-20260810-H6T9.json",
                {"status": "promoted", "decided_at": "2026-08-10T01:00:00Z"},
            )

            catalog = store.build_catalog(root)

            self.assertEqual(catalog["entries"][0]["status"], "pending")
            self.assertEqual(catalog["entries"][0]["effective_status"], "promoted")
            self.assertFalse(catalog["entries"][0]["actionable"])


if __name__ == "__main__":
    unittest.main()
