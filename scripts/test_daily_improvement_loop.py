#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import daily_improvement_loop as loop


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


class DailyImprovementLoopTests(unittest.TestCase):
    def test_claude_pp_cli_detection_requires_bash_tool_use(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "sessionId": "s1",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "message": {
                            "role": "user",
                            "content": "We discussed stripe-pp-cli but did not run it.",
                        },
                    },
                    {
                        "type": "assistant",
                        "sessionId": "s1",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "call1",
                                    "name": "Bash",
                                    "input": {"command": "stripe-pp-cli invoices list --json"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s1",
                        "timestamp": "2026-06-15T00:00:02Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call1",
                                    "content": "Exit code: 1\nAPI error 401: token secret sk-test-1234567890abcd",
                                }
                            ],
                        },
                    },
                ],
            )
            summary = loop.parse_claude_session(path)
            self.assertEqual(sorted(summary.pp_cli_invocations), ["stripe-pp-cli"])
            self.assertEqual(len(summary.failures), 1)
            self.assertNotIn("sk-test", summary.failures[0].excerpt)
            self.assertIn("<redacted-", summary.failures[0].excerpt)

    def test_codex_exec_command_detection(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "codex.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "session_meta",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "payload": {"id": "c1", "cwd": "/tmp/work"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "call2",
                            "arguments": json.dumps({"cmd": "amazon-orders-pp-cli orders list --json"}),
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-15T00:00:02Z",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call2",
                            "output": "Exit code: 1\nError: forbidden for user cat@example.com",
                        },
                    },
                ],
            )
            summary = loop.parse_codex_session(path)
            self.assertEqual(sorted(summary.pp_cli_invocations), ["amazon-orders-pp-cli"])
            self.assertEqual(len(summary.failures), 1)
            self.assertIn("<email>", summary.failures[0].excerpt)

    def test_codex_successful_output_with_error_word_is_not_failure(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "codex.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "session_meta",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "payload": {"id": "c2", "cwd": "/tmp/work"},
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "call_id": "call3",
                            "arguments": json.dumps({"cmd": "xcodebuild build"}),
                        },
                    },
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-15T00:00:02Z",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call3",
                            "output": (
                                "Process exited with code 0\n"
                                "Output: build setting SWIFT_TREAT_WARNINGS_AS_ERRORS = NO"
                            ),
                        },
                    },
                ],
            )
            summary = loop.parse_codex_session(path)
            self.assertEqual(summary.failures, [])

    def test_bulk_pp_cli_failure_is_localized_to_failing_cli(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            command = "for c in stripe-pp-cli shopify-pp-cli; do echo $c; $c doctor; done"
            write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "sessionId": "s2",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "call4",
                                    "name": "Bash",
                                    "input": {"command": command},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s2",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call4",
                                    "content": (
                                        "stripe-pp-cli\n"
                                        "OK Auth: configured\n"
                                        "shopify-pp-cli\n"
                                        "FAIL Auth: not configured\n"
                                    ),
                                }
                            ],
                        },
                    },
                ],
            )
            summary = loop.parse_claude_session(path)
            proposals = loop.generate_proposals([summary])
            pp_targets = [p["target"]["name"] for p in proposals if p["route"] == "tool"]
            self.assertEqual(pp_targets, ["shopify-pp-cli"])

    def test_pp_cli_help_output_is_not_friction(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "sessionId": "s3",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "call5",
                                    "name": "Bash",
                                    "input": {"command": "wavespeed-pp-cli run --help"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s3",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call5",
                                    "content": "Usage: wavespeed-pp-cli run [model-or-alias] [flags]",
                                }
                            ],
                        },
                    },
                ],
            )
            summary = loop.parse_claude_session(path)
            proposals = loop.generate_proposals([summary])
            self.assertFalse([p for p in proposals if p["route"] == "tool"])

    def test_pp_cli_mentions_inside_command_arguments_are_not_invocations(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "sessionId": "s5",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "call6",
                                    "name": "Bash",
                                    "input": {
                                        "command": (
                                            "bd create --description "
                                            "'Document wavespeed-pp-cli workflow failure'"
                                        )
                                    },
                                }
                            ],
                        },
                    }
                ],
            )
            summary = loop.parse_claude_session(path)
            self.assertEqual(summary.pp_cli_invocations, {})

    def test_pp_cli_inside_remote_shell_command_counts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "sessionId": "s6",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "call7",
                                    "name": "Bash",
                                    "input": {
                                        "command": (
                                            "source bin/_ssh.sh kssh "
                                            "'wavespeed-pp-cli profile save bad --model-id x'"
                                        )
                                    },
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s6",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "call7",
                                    "content": "Error: unknown flag: --model-id",
                                }
                            ],
                        },
                    },
                ],
            )
            summary = loop.parse_claude_session(path)
            self.assertEqual(sorted(summary.pp_cli_invocations), ["wavespeed-pp-cli"])

    def test_transcript_scaffold_is_not_user_correction(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "sessionId": "s4",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "message": {
                            "role": "user",
                            "content": (
                                "<local-command-caveat>Caveat: DO NOT respond unless asked."
                                "</local-command-caveat>"
                            ),
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s4",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": "Base directory for this skill: /tmp/skill\nDo not edit directly.",
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s4",
                        "timestamp": "2026-06-15T00:00:02Z",
                        "message": {
                            "role": "user",
                            "content": (
                                "<task-notification><status>completed</status>"
                                "<summary>Agent came to rest</summary></task-notification>"
                            ),
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s4",
                        "timestamp": "2026-06-15T00:00:03Z",
                        "message": {
                            "role": "user",
                            "content": (
                                "Codex could not read the local image at `-`: "
                                "No such file or directory (os error 2). Do not retry."
                            ),
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s4",
                        "timestamp": "2026-06-15T00:00:04Z",
                        "message": {
                            "role": "user",
                            "content": (
                                "A 16:9 horizontal editorial illustration that explains ONE idea: "
                                "do not include text or labels."
                            ),
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s4",
                        "timestamp": "2026-06-15T00:00:05Z",
                        "message": {
                            "role": "user",
                            "content": (
                                "This session is being continued from a previous conversation "
                                "that ran out of context. Do not treat this as a correction."
                            ),
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s4",
                        "timestamp": "2026-06-15T00:00:06Z",
                        "message": {
                            "role": "user",
                            "content": (
                                "<codex_internal_context source=\"goal\">"
                                "Do not treat injected goal context as user correction."
                            ),
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s4",
                        "timestamp": "2026-06-15T00:00:07Z",
                        "message": {
                            "role": "user",
                            "content": (
                                "<collaboration_mode>Default</collaboration_mode>"
                                "Do not treat mode scaffolding as user correction."
                            ),
                        },
                    },
                ],
            )
            summary = loop.parse_claude_session(path)
            self.assertEqual(summary.corrections, [])

    def test_proposals_are_routed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "sessionId": "s1",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "skill1",
                                    "name": "Skill",
                                    "input": {"skill": "deploy-cli"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "s1",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": "Actually, don't parse human output; always use --json.",
                        },
                    },
                ],
            )
            summary = loop.parse_claude_session(path)
            proposals = loop.generate_proposals([summary])
            routes = {p["route"] for p in proposals}
            self.assertIn("skill_improvement", routes)

    def test_skill_improvement_requires_correction_after_skill_invocation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "sessionId": "s-before",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "message": {
                            "role": "user",
                            "content": "Do not use stale context. Here is the project brief.",
                        },
                    },
                    {
                        "type": "assistant",
                        "sessionId": "s-before",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "skill-before",
                                    "name": "Skill",
                                    "input": {"skill": "grill-me"},
                                }
                            ],
                        },
                    },
                ],
            )
            summary = loop.parse_claude_session(path)
            proposals = loop.generate_proposals([summary])
            self.assertFalse([p for p in proposals if p["route"] == "skill_improvement"])

    def test_full_mode_keeps_unredacted_excerpt(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "sessionId": "sf",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "callf",
                                    "name": "Bash",
                                    "input": {"command": "stripe-pp-cli invoices list --json"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "sf",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "callf",
                                    "content": "Exit code: 1\nAPI error 401: token secret sk-test-1234567890abcd",
                                }
                            ],
                        },
                    },
                ],
            )
            loop.FULL_DETAIL = True
            try:
                summary = loop.parse_claude_session(path)
            finally:
                loop.FULL_DETAIL = False
            self.assertTrue(summary.failures)
            self.assertIn("sk-test-1234567890abcd", summary.failures[0].excerpt)
            self.assertNotIn("<redacted-", summary.failures[0].excerpt)


    def test_printing_press_source_maps_pp_cli_to_library(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "library" / "wavespeed").mkdir(parents=True)
            self.assertEqual(
                loop.printing_press_source("wavespeed-pp-cli", root),
                root / "library" / "wavespeed",
            )
            # Not a pp-cli, no root, and unknown name all resolve to None.
            self.assertIsNone(loop.printing_press_source("ripgrep", root))
            self.assertIsNone(loop.printing_press_source("wavespeed-pp-cli", None))
            self.assertIsNone(loop.printing_press_source("nope-pp-cli", root))

    def test_pp_cli_timeout_is_flagged_as_hang(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            write_jsonl(
                path,
                [
                    {
                        "type": "assistant",
                        "sessionId": "h1",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "callh",
                                    "name": "Bash",
                                    "input": {"command": "wavespeed-pp-cli image create --prompt x"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "h1",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "callh",
                                    "content": "request timed out after 60s, no output returned",
                                }
                            ],
                        },
                    },
                ],
            )
            summary = loop.parse_claude_session(path)
            # A clean timeout is not a hard failure, so it must surface as a hang.
            self.assertFalse(summary.failures)
            proposals = loop.generate_proposals([summary])
            tool_props = [p for p in proposals if p["target"]["name"] == "wavespeed-pp-cli"]
            self.assertEqual(len(tool_props), 1)
            self.assertIn("hang/timeout", tool_props[0]["summary"])

    def test_pp_cli_retried_in_one_session_is_flagged_without_failure(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "claude.jsonl"
            rows = []
            for i in range(3):
                rows.append(
                    {
                        "type": "assistant",
                        "sessionId": "r1",
                        "timestamp": f"2026-06-15T00:00:0{i}Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": f"r{i}",
                                    "name": "Bash",
                                    "input": {"command": f"wavespeed-pp-cli profile save --name a{i}"},
                                }
                            ],
                        },
                    }
                )
            write_jsonl(path, rows)
            summary = loop.parse_claude_session(path)
            self.assertFalse(summary.failures)
            proposals = loop.generate_proposals([summary])
            tool_props = [p for p in proposals if p["target"]["name"] == "wavespeed-pp-cli"]
            self.assertEqual(len(tool_props), 1)
            self.assertIn("retried up to 3x", tool_props[0]["summary"])

    def test_printing_press_source_line_added_to_tool_proposal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "library" / "wavespeed").mkdir(parents=True)
            session = Path(td) / "claude.jsonl"
            write_jsonl(
                session,
                [
                    {
                        "type": "assistant",
                        "sessionId": "p1",
                        "timestamp": "2026-06-15T00:00:00Z",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "callp",
                                    "name": "Bash",
                                    "input": {"command": "wavespeed-pp-cli profile save --name a"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "sessionId": "p1",
                        "timestamp": "2026-06-15T00:00:01Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "callp",
                                    "content": "Error: unknown option --name",
                                }
                            ],
                        },
                    },
                ],
            )
            summary = loop.parse_claude_session(session)
            proposals = loop.generate_proposals([summary], root)
            tool_props = [p for p in proposals if p["target"]["name"] == "wavespeed-pp-cli"]
            self.assertEqual(len(tool_props), 1)
            action = tool_props[0]["suggested_action"]
            self.assertIn(str(root / "library" / "wavespeed"), action)
            self.assertIn("/printing-press-amend", action)


    def test_malformed_pp_cli_names_do_not_become_proposals(self):
        summary = loop.SessionSummary(source="claude", path=Path("x"), session_id="m1")
        for bad in ("'wavespeed-pp-cli", "$c-pp-cli", "===x-twitter-pp-cli"):
            summary.pp_cli_invocations[bad] = [
                loop.Evidence(
                    source="claude",
                    path="x",
                    line=1,
                    kind="pp_cli_hang",
                    excerpt="timed out",
                    session_id="m1",
                    command=f"echo {bad}",
                )
            ]
        proposals = loop.generate_proposals([summary])
        self.assertEqual([p for p in proposals if p["route"] == "tool"], [])

    def test_repeated_slash_command_stages_content_idea(self):
        sessions = []
        for i in range(2):
            path = Path(f"session-{i}.jsonl")
            summary = loop.SessionSummary(source="codex", path=path, session_id=f"slash-{i}")
            ev = loop.Evidence(
                source="codex",
                path=str(path),
                line=10,
                kind="slash_command",
                excerpt="/investigate",
                session_id=f"slash-{i}",
            )
            summary.slash_commands.setdefault("investigate", []).append(ev)
            sessions.append(summary)

        proposals = loop.generate_proposals(sessions, route="content_idea")
        content_props = [p for p in proposals if p["route"] == "content_idea"]

        self.assertEqual(len(content_props), 1)
        proposal = content_props[0]
        self.assertEqual(proposal["content_type"], "how_to")
        self.assertIn("/investigate", proposal["title"])
        self.assertEqual(proposal["recommendation"], "write_now")
        self.assertGreaterEqual(proposal["confidence"], 0.7)
        self.assertTrue(proposal["last30days"]["should_run"])
        self.assertIn("suggested_search_query", proposal["last30days"])
        self.assertIn("rough_outline", proposal)
        self.assertIn("privacy", proposal)
        self.assertEqual(proposal["privacy"]["risk_level"], "low")
        self.assertEqual(proposal["apply_policy"]["mode"], "manual_review_required")

    def test_content_idea_marks_private_contact_workflows_high_risk(self):
        summary = loop.SessionSummary(source="codex", path=Path("sms.jsonl"), session_id="sms1")
        ev = loop.Evidence(
            source="codex",
            path="sms.jsonl",
            line=12,
            kind="tool_failure",
            excerpt="Built iMessage search database for jane@example.com and Jane Doe at +1 (415) 555-2671",
            session_id="sms1",
            command="python scripts/import_imessage.py --contact 'Jane Doe' --phone '+1 (415) 555-2671' --sqlite messages.db",
        )
        summary.failures.append(ev)

        proposals = loop.generate_proposals([summary], route="content_idea")
        content_props = [p for p in proposals if p["route"] == "content_idea"]

        self.assertEqual(len(content_props), 1)
        proposal = content_props[0]
        self.assertEqual(proposal["privacy"]["risk_level"], "high")
        self.assertIn("raw message contents", proposal["privacy"]["must_anonymize"])
        self.assertIn("personal CRM", proposal["title"])
        self.assertEqual(proposal["recommendation"], "needs_context")
        serialized = json.dumps(proposal)
        self.assertNotIn("Jane Doe", serialized)
        self.assertNotIn("jane@example.com", serialized)
        self.assertNotIn("415", serialized)
        self.assertIn("<private workflow evidence redacted>", serialized)

    def test_route_filter_can_emit_only_content_ideas(self):
        summary = loop.SessionSummary(source="codex", path=Path("slash.jsonl"), session_id="slash-only")
        ev = loop.Evidence(
            source="codex",
            path="slash.jsonl",
            line=1,
            kind="slash_command",
            excerpt="/gstack",
            session_id="slash-only",
        )
        summary.slash_commands.setdefault("gstack", []).append(ev)
        proposals = loop.generate_proposals([summary], route="content_idea")
        self.assertTrue(proposals)
        self.assertEqual({p["route"] for p in proposals}, {"content_idea"})
    def test_default_route_preserves_improvement_only_behavior(self):
        summary = loop.SessionSummary(source="codex", path=Path("slash.jsonl"), session_id="default-route")
        ev = loop.Evidence(
            source="codex",
            path="slash.jsonl",
            line=1,
            kind="slash_command",
            excerpt="/gstack",
            session_id="default-route",
        )
        summary.slash_commands.setdefault("gstack", []).append(ev)
        proposals = loop.generate_proposals([summary])
        self.assertEqual([p for p in proposals if p["route"] == "content_idea"], [])

    def test_workflow_command_cluster_stages_content_idea(self):
        summary = loop.SessionSummary(source="claude", path=Path("task-ledger.jsonl"), session_id="ledger1")
        summary.tool_calls.extend(
            [
                loop.ToolCall(
                    call_id="a",
                    name="Bash",
                    line=10,
                    command="curl -s -X POST $TASK_LEDGER_API_URL/api/issues/WORK-123/checkout",
                ),
                loop.ToolCall(
                    call_id="b",
                    name="Bash",
                    line=11,
                    command="bash scripts/task-ledger-update.sh --issue-id WORK-123 --status done",
                ),
            ]
        )

        proposals = loop.generate_proposals([summary], route="content_idea")
        titles = [p["title"] for p in proposals]

        self.assertIn("How I turn agent work into a task ledger instead of chat chaos", titles)
        proposal = next(p for p in proposals if p["title"] == "How I turn agent work into a task ledger instead of chat chaos")
        self.assertEqual(proposal["trigger"]["kind"], "workflow_command_cluster")
        self.assertEqual(proposal["recommendation"], "write_now")
        self.assertIn("task ledger", proposal["last30days"]["suggested_search_query"])

    def test_private_workflow_command_cluster_suppresses_evidence(self):
        summary = loop.SessionSummary(source="claude", path=Path("inbox.jsonl"), session_id="ea1")
        summary.tool_calls.extend(
            [
                loop.ToolCall(
                    call_id="a",
                    name="Bash",
                    line=10,
                    command="gog gmail search 'from:jane@example.com newer_than:24h' --json",
                ),
                loop.ToolCall(
                    call_id="b",
                    name="Bash",
                    line=11,
                    command="gog calendar events primary --from now --to tomorrow --json",
                ),
            ]
        )

        proposals = loop.generate_proposals([summary], route="content_idea")
        proposal = next(p for p in proposals if "executive assistant" in p["title"])
        serialized = json.dumps(proposal)

        self.assertEqual(proposal["privacy"]["risk_level"], "high")
        self.assertEqual(proposal["recommendation"], "needs_context")
        self.assertNotIn("jane@example.com", serialized)
        self.assertIn("<private workflow evidence redacted>", serialized)

    def test_content_privacy_notice_and_full_detail_still_suppresses_high_risk_evidence(self):
        previous_full = loop.FULL_DETAIL
        loop.FULL_DETAIL = True
        try:
            ev = loop.Evidence(
                source="claude",
                path="private.jsonl",
                line=1,
                kind="workflow_signal",
                excerpt="gmail thread from jane@example.com about private customer details",
                session_id="private1",
                command="gog gmail thread jane@example.com --plain",
            )
            proposal = loop.make_content_proposal(
                title="Private inbox workflow",
                content_type="how_to",
                evidence_items=[ev],
                trigger_kind="private_workflow",
                real_workflow_or_moment="Inbox workflow with personal details",
                audience=["operators"],
                why_interesting=["Useful workflow"],
                suggested_search_query="AI inbox workflow privacy",
                rough_outline=["Collect", "Triage", "Anonymize"],
                confidence=0.8,
                recommendation="write_now",
            )
        finally:
            loop.FULL_DETAIL = previous_full

        serialized = json.dumps(proposal)
        self.assertEqual(proposal["recommendation"], "needs_context")
        self.assertIn("content_notice", proposal["privacy"])
        self.assertIn("<private workflow evidence redacted>", serialized)
        self.assertNotIn("jane@example.com", serialized)

    def test_top_skills_content_idea_from_skill_usage(self):
        summary = loop.SessionSummary(source="claude", path=Path("skills.jsonl"), session_id="skills1")
        for i, skill in enumerate(["task-ledger", "task-ledger", "x-research", "seo", "task-ledger", "x-research"], 1):
            summary.skill_invocations.setdefault(skill, []).append(
                loop.Evidence(
                    source="claude",
                    path="skills.jsonl",
                    line=i,
                    kind="skill_invocation",
                    excerpt=f"Skill({skill})",
                    session_id="skills1",
                    tool_name="Skill",
                )
            )

        proposals = loop.generate_proposals([summary], route="content_idea")
        proposal = next(p for p in proposals if "top 10 skills" in p["title"].lower())

        self.assertEqual(proposal["trigger"]["kind"], "aggregate_skill_usage")
        self.assertIn("task-ledger", proposal["summary"])
        self.assertIn("x-research", proposal["summary"])
        self.assertEqual(proposal["recommendation"], "write_now")

    def test_most_used_slash_commands_content_idea_from_usage(self):
        summary = loop.SessionSummary(source="codex", path=Path("slash.jsonl"), session_id="slash-agg")
        for i, command in enumerate(["gstack", "investigate", "gstack", "last30days"], 1):
            summary.slash_commands.setdefault(command, []).append(
                loop.Evidence(
                    source="codex",
                    path="slash.jsonl",
                    line=i,
                    kind="slash_command",
                    excerpt=f"/{command}",
                    session_id="slash-agg",
                )
            )

        proposals = loop.generate_proposals([summary], route="content_idea")
        proposal = next(p for p in proposals if "slash commands" in p["title"].lower())

        self.assertEqual(proposal["trigger"]["kind"], "aggregate_slash_command_usage")
        self.assertIn("/gstack", proposal["summary"])
        self.assertIn("/investigate", proposal["summary"])
        self.assertEqual(proposal["recommendation"], "write_now")

    def test_loop_examples_content_idea_from_multiple_workflow_clusters(self):
        task_ledger = loop.SessionSummary(source="claude", path=Path("task-ledger.jsonl"), session_id="ledger-loop")
        task_ledger.tool_calls.extend([
            loop.ToolCall(call_id="a", name="Bash", line=1, command="curl $TASK_LEDGER_API_URL/api/issues/WORK-123/checkout"),
            loop.ToolCall(call_id="b", name="Bash", line=2, command="task-ledger-update.sh --issue-id WORK-123 --status done"),
        ])
        revenue = loop.SessionSummary(source="claude", path=Path("rev.jsonl"), session_id="rev-loop")
        revenue.tool_calls.extend([
            loop.ToolCall(call_id="c", name="Bash", line=1, command="revenue-pp-cli flow-decay --days 90"),
            loop.ToolCall(call_id="d", name="Bash", line=2, command="revenue-pp-cli metric-aggregates --metric-id abc"),
            loop.ToolCall(call_id="e", name="Bash", line=3, command="revenue-pp-cli campaign-values-report --since 2026-01-01"),
        ])

        proposals = loop.generate_proposals([task_ledger, revenue], route="content_idea")
        proposal = next(p for p in proposals if "loop examples" in p["title"].lower())

        self.assertEqual(proposal["trigger"]["kind"], "aggregate_loop_examples")
        self.assertIn("task-ledger", proposal["summary"])
        self.assertIn("revenue-watch", proposal["summary"])
        self.assertEqual(proposal["recommendation"], "write_now")

    def test_most_used_cli_commands_content_idea_from_tool_calls(self):
        summary = loop.SessionSummary(source="claude", path=Path("commands.jsonl"), session_id="cmd1")
        summary.tool_calls.extend([
            loop.ToolCall(call_id="a", name="Bash", line=1, command="revenue-pp-cli flow-decay --days 90"),
            loop.ToolCall(call_id="b", name="Bash", line=2, command="revenue-pp-cli metric-aggregates --metric-id abc"),
            loop.ToolCall(call_id="c", name="Bash", line=3, command="gog gmail search 'in:inbox' --json"),
            loop.ToolCall(call_id="d", name="Bash", line=4, command="gh pr view 1 --json url"),
            loop.ToolCall(call_id="e", name="Bash", line=5, command="# comment only"),
            loop.ToolCall(call_id="f", name="Bash", line=6, command="<<'MD' heredoc content"),
        ])

        proposals = loop.generate_proposals([summary], route="content_idea")
        proposal = next(p for p in proposals if "command-line stack" in p["title"].lower())

        self.assertEqual(proposal["trigger"]["kind"], "aggregate_cli_usage")
        self.assertIn("revenue-pp-cli", proposal["summary"])
        self.assertIn("gog", proposal["summary"])
        self.assertIn("gh", proposal["summary"])
        self.assertNotIn("<<'MD'", proposal["summary"])
        self.assertNotIn("#", proposal["summary"])

    def test_absolute_paths_are_not_slash_commands(self):
        summary = loop.SessionSummary(source="codex", path=Path("paths.jsonl"), session_id="paths")
        ev = loop.Evidence(
            source="codex",
            path="paths.jsonl",
            line=1,
            kind="slash_command",
            excerpt="/Users",
            session_id="paths",
        )
        # Regression guard for parser-level behavior discovered in real data:
        # slash-command regex should not classify absolute macOS paths as commands.
        self.assertEqual(loop.SLASH_COMMAND_RE.findall("/Users/knox/project\n/gstack"), ["gstack"])
        summary.slash_commands.setdefault("Users", []).append(ev)
        proposals = loop.generate_proposals([summary], route="content_idea")
        self.assertEqual([p for p in proposals if "/Users" in p.get("title", "")], [])

    def test_discover_session_files_combines_multiple_homes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home_a = root / "knox"
            home_b = root / "mac-studio"
            codex_a = home_a / ".codex" / "sessions" / "a.jsonl"
            codex_b = home_b / ".codex" / "sessions" / "b.jsonl"
            write_jsonl(codex_a, [])
            write_jsonl(codex_b, [])

            files = loop.discover_session_files([home_a, home_b], "codex")

            self.assertEqual([source for source, _ in files], ["codex", "codex"])
            self.assertEqual({path.name for _, path in files}, {"a.jsonl", "b.jsonl"})


if __name__ == "__main__":
    unittest.main()
