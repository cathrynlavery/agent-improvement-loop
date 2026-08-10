import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

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
            home, loop_root, "leader", "silverfox studio mbp ronan"
        )
        existing = "0 8 * * * /usr/local/bin/other\n"
        first = cron.replace_managed_block(existing, block)
        second = cron.replace_managed_block(first, block)
        self.assertEqual(first, second)
        self.assertIn("/usr/local/bin/other", first)
        self.assertIn("collect_learnings_fleet.sh", first)
        self.assertIn("AGENT_LEARNINGS_REMOTE_HOSTS='silverfox studio mbp ronan'", first)
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
            remote_hosts="silverfox studio mbp ronan",
        )
        self.assertEqual(set(writer), {"ai.openclaw.learnings-harvest"})
        self.assertEqual(
            set(leader),
            {
                "ai.openclaw.learnings-harvest",
                "ai.openclaw.learnings-collect",
                "ai.openclaw.learnings-fixloop",
                "ai.openclaw.learnings-review",
            },
        )
        collect_env = leader["ai.openclaw.learnings-collect"]["EnvironmentVariables"]
        self.assertEqual(collect_env["AGENT_LEARNINGS_REMOTE_HOSTS"], "silverfox studio mbp ronan")
        self.assertEqual(
            writer["ai.openclaw.learnings-harvest"]["StartCalendarInterval"]["Minute"],
            20,
        )

    def test_instruction_update_replaces_legacy_path_and_is_idempotent(self):
        legacy = (
            "# Rules\n\n- Self-improvement: append to "
            "~/Library/CloudStorage/Dropbox/OpenClaw/.learnings/.\n"
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
            store.initialize_store(root, "studio", "writer")
            (root / "ACTIVE.md").write_text("local", encoding="utf-8")
            (root / "fleet/ACTIVE.md").write_text("fleet", encoding="utf-8")
            self.assertEqual(store.active_view_path(root), root / "fleet/ACTIVE.md")

            store.initialize_store(root, "studio", "leader")
            self.assertEqual(store.active_view_path(root), root / "ACTIVE.md")

    def test_machine_slug_normalizes_computer_names(self):
        self.assertEqual(store.machine_slug("Cat-Mac-Studio"), "cat-mac-studio")
        self.assertEqual(store.machine_slug("RonanRX’s Mac mini"), "ronanrxs-mac-mini")

    def test_migration_prefixes_entry_with_machine_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "old"
            root = Path(td) / "new"
            source.mkdir()
            (source / "ERR-20260810-A7K2.md").write_text(
                entry("ERR-20260810-A7K2"), encoding="utf-8"
            )

            first = store.migrate_store(root, "studio", source, "writer")
            second = store.migrate_store(root, "studio", source, "writer")

            target = root / "entries/studio/studio--ERR-20260810-A7K2.md"
            self.assertTrue(target.exists())
            self.assertEqual(first["counts"]["entries_new"], 1)
            self.assertEqual(second["counts"]["entries_same"], 1)
            self.assertEqual(list((root / "entries/studio").glob("*.md")), [target])

    def test_aggregate_entries_are_split_and_legacy_is_preserved(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "old"
            root = Path(td) / "new"
            source.mkdir()
            aggregate = "# Learnings\n\n" + entry("LRN-20260810-B8M3")
            aggregate += "\n---\n\n" + entry("LRN-20260810-C9N4", "resolved")
            (source / "LEARNINGS.md").write_text(aggregate, encoding="utf-8")

            result = store.migrate_store(root, "silverfox", source, "writer")

            self.assertEqual(result["counts"]["aggregate_entries"], 2)
            self.assertTrue((root / "legacy/silverfox/LEARNINGS.md").exists())
            self.assertTrue(
                (root / "entries/silverfox/silverfox--LRN-20260810-B8M3.md").exists()
            )
            self.assertTrue(
                (root / "entries/silverfox/silverfox--LRN-20260810-C9N4.md").exists()
            )

    def test_same_id_same_content_is_logically_deduplicated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            text = entry("ERR-20260810-D2P5")
            store.initialize_store(root, "knox", "leader")
            for machine in ("silverfox", "studio"):
                store.write_entry(root, machine, "ERR-20260810-D2P5", text)

            catalog = store.build_catalog(root)

            self.assertEqual(catalog["summary"]["entry_files"], 2)
            self.assertEqual(catalog["summary"]["logical_entries"], 1)
            self.assertEqual(catalog["summary"]["exact_duplicate_copies"], 1)
            self.assertEqual(catalog["summary"]["conflicting_ids"], 0)

    def test_same_id_different_content_is_preserved_and_flagged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store.initialize_store(root, "knox", "leader")
            for machine, summary in (("silverfox", "One"), ("studio", "Two")):
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
                (root / "entries/silverfox/silverfox--LRN-20260810-E3Q6.md").exists()
            )
            self.assertTrue(
                (root / "entries/studio/studio--LRN-20260810-E3Q6.md").exists()
            )

    def test_rekey_cli_changes_only_logical_identity_and_retains_source_audit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_id = "ERR-20260810-R4K7"
            new_id = "ERR-20260810-N8W3"
            store.initialize_store(root, "knox", "leader")
            self.assertTrue((root / "rekeys").is_dir())
            store.write_entry(root, "silverfox", source_id, entry(source_id, summary="One"))
            store.write_entry(root, "studio", source_id, entry(source_id, summary="Two"))
            source_path = root / f"entries/studio/studio--{source_id}.md"
            source_bytes = source_path.read_bytes()
            source_sha256 = store.sha256_bytes(source_bytes)

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
                        "--by",
                        "test",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("rekey=created", stdout.getvalue())
            self.assertEqual(source_path.read_bytes(), source_bytes)
            self.assertFalse(
                (root / f"entries/studio/studio--{new_id}.md").exists(),
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
            store.initialize_store(root, "knox", "leader")
            store.write_entry(root, "studio", source_id, entry(source_id, summary="Original"))
            source_path = root / f"entries/studio/studio--{source_id}.md"
            relative = str(source_path.relative_to(root))
            correct_hash = store.sha256_bytes(source_path.read_bytes())

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

    def test_decision_acknowledges_only_the_exact_current_conflict_hash_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            entry_id = "LRN-20260810-T6M9"
            store.initialize_store(root, "knox", "leader")
            store.write_entry(root, "silverfox", entry_id, entry(entry_id, summary="One"))
            store.write_entry(root, "studio", entry_id, entry(entry_id, summary="Two"))
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
            store.initialize_store(root, "knox", "leader")
            store.write_entry(root, "silverfox", entry_id, entry(entry_id, summary="One"))
            store.write_entry(root, "studio", entry_id, entry(entry_id, summary="Two"))
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

            store.write_entry(root, "ronan", entry_id, entry(entry_id, summary="Three"))
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
            store.initialize_store(root, "silverfox", "leader")
            store.write_entry(root, "silverfox", entry_id, entry(entry_id, summary="One"))
            store.write_entry(root, "studio", entry_id, entry(entry_id, summary="Two"))
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

            (root / f"entries/studio/studio--{entry_id}.md").unlink()
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
            store.initialize_store(root, "silverfox", "leader")
            store.write_entry(root, "silverfox", entry_id, entry(entry_id, summary="One"))
            store.write_entry(root, "studio", entry_id, entry(entry_id, summary="Two"))
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
            (outside / f"studio--{entry_id}.md").write_text(
                entry(entry_id), encoding="utf-8"
            )
            store.initialize_store(root, "silverfox", "leader")
            (root / "entries/studio").symlink_to(outside, target_is_directory=True)

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
            store.initialize_store(root, "studio", "writer")
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
            store.initialize_store(root, "knox", "leader")
            store.write_entry(
                root,
                "knox",
                "FEAT-20260810-F4R7",
                "---\nstatus: unresolved\n---\n# Feature\n",
            )
            store.write_entry(
                root,
                "knox",
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

            result = store.migrate_store(root, "studio", source, "writer")
            catalog = store.build_catalog(root)

            migrated = root / "entries/studio/studio--ERR-20260810-J7V2.md"
            legacy = root / "legacy/studio/ERR-20260810-J7V2.md"
            self.assertIn("**Status**: untriaged", migrated.read_text(encoding="utf-8"))
            self.assertEqual(legacy.read_text(encoding="utf-8"), original)
            self.assertEqual(result["counts"]["statuses_normalized"], 1)
            self.assertEqual(catalog["summary"]["invalid_files"], 0)
            self.assertEqual(catalog["summary"]["actionable_entries"], 1)

    def test_decision_overrides_source_status_without_mutating_peer_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store.initialize_store(root, "knox", "leader")
            store.write_entry(
                root,
                "knox",
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
