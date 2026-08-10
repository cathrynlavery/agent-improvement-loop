#!/usr/bin/env python3
"""Mine local agent sessions for self-improvement candidates.

The command is intentionally conservative: it scans transcripts, writes a
proposal queue and a compact review packet, and never applies changes to skills,
memory, runbooks, source code.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import socket
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = 1
DEFAULT_OUTPUT_ROOT = Path.home() / ".agent-improvement"
DEFAULT_CONFIG_PATH = DEFAULT_OUTPUT_ROOT / "config.json"
RESOLUTION_DECISIONS = {"fixed", "wontfix", "ignored"}

# When True (via --full), keep full, unredacted excerpts inline. Default masks
# secrets and shortens excerpts so the written output is safe to commit, sync,
# paste into a writeup, or hand to another agent.
FULL_DETAIL = False
FULL_EXCERPT_LIMIT = 4000
CORRECTION_SCAN_LIMIT = 360

# CLIs whose command name ends in this suffix get the `tool` route. Override
# with "tracked_cli_suffix" in the config file to track your own naming scheme.
TRACKED_CLI_SUFFIX = "-cli"


def _build_tracked_cli_res(suffix: str) -> Tuple[re.Pattern[str], re.Pattern[str]]:
    esc = re.escape(suffix)
    loose = re.compile(rf"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9._-]*{esc})(?=$|[\s;&|)])")
    # Anchored form, used to reject malformed names the tokenizer can pick up
    # from shell quoting or transcript scaffolding (e.g. "'media-cli",
    # "$c-cli", "===social-cli") before they ever become a proposal.
    anchored = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._-]*{esc}$")
    return loose, anchored


TRACKED_CLI_RE, VALID_TRACKED_CLI_RE = _build_tracked_cli_res(TRACKED_CLI_SUFFIX)
ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*")
SHELL_SEPARATORS = {";", "&&", "||", "|", "do", "then", "else"}
COMMAND_PREFIXES = {"command", "env", "noglob", "time"}
REMOTE_COMMAND_WRAPPERS = {"bash", "kssh", "kssh_once", "sh", "ssh", "zsh"}
# Corrections are split into strong cues (explicitly corrective phrases) and
# weak cues (words that also appear constantly in ordinary specs and prompts —
# "do not", "instead", "actually"). Weak cues only count in short, reactive
# messages; a long task brief containing "do NOT change anything" is an
# instruction, not a correction.
STRONG_CORRECTION_RE = re.compile(
    r"\b("
    r"no,? that'?s wrong|that'?s wrong|that is wrong|not what i asked|"
    r"you missed|never do|stop doing|you should have|why did you|"
    r"i meant|remember this"
    r")\b",
    re.IGNORECASE,
)
# "Actually" only counts when it opens a line ("Actually, use X"). Mid-sentence
# "actually" ("how do we actually get that set up?") is ordinary emphasis, not
# a correction.
WEAK_CORRECTION_RE = re.compile(
    r"(?:^\s*actually\b|\b(instead|don'?t do|do not|should have)\b)",
    re.IGNORECASE | re.MULTILINE,
)
STRONG_CORRECTION_MAX_CHARS = 2000
WEAK_CORRECTION_MAX_CHARS = 400
# Cap correction evidence carried per session so one noisy session cannot
# dominate a memory/context proposal.
MAX_CORRECTIONS_PER_SESSION = 3
# Pasted call/meeting transcripts (speaker-tagged or timestamped dialogue) are
# source material the user is relaying, not a correction of the agent. The
# transcript body routinely contains weak trigger words ("actually", "instead")
# that would otherwise count as correction cues.
TRANSCRIPT_SPEAKER_RE = re.compile(r"<b>\s*speaker\s*\d+", re.IGNORECASE)
TRANSCRIPT_TIMESTAMP_RE = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?\]")
FAILURE_RE = re.compile(
    r"("
    r"exit code:\s*[1-9]|non-zero|command not found|no such file|"
    r"permission denied|traceback|exception|panic:|api error|http\s+(4\d\d|5\d\d)|"
    r"\b(401|403|404|409|422|429|500|502|503)\b|"
    r"\b(error|failed|failure|invalid|unauthorized|forbidden)\b"
    r")",
    re.IGNORECASE,
)
BAD_EXIT_RE = re.compile(r"(?i)(process exited with code|exit code)[:\s]+[1-9]\d*\b")
GOOD_EXIT_RE = re.compile(r"(?i)(process exited with code|exit code)[:\s]+0\b")
COMPLETED_TOOL_RESULT_RE = re.compile(r"(?i)\bscript completed\b")
TRACKED_CLI_FRICTION_RE = re.compile(
    r"(?i)\b("
    r"FAIL|not configured|missing required|unknown option|usage:|"
    r"not found|invalid|unauthorized|forbidden|rate limit|silent null"
    r")\b"
)
# Hard error phrases that outrank the inspection-command exemption below:
# help/doctor output that contains these is real friction, not documentation.
TRACKED_CLI_STRONG_FRICTION_RE = re.compile(
    r"(?i)("
    r"\bFAIL\b|\bnot configured\b|\bmissing required\b|"
    r"\bunknown (option|flag)\b|\binvalid (option|flag|argument)\b|"
    r"\bunauthorized\b|\bforbidden\b|\brate limit\b|\bsilent null\b|"
    r"\baccepts at most\b|\bunexpected extra arg\b|error:|\bnot found\b"
    r")"
)
INSPECTION_COMMAND_RE = re.compile(
    r"(?i)(^|\s)(--help|-h|--version|version|doctor|inventory)(?=$|\s|[;&|])"
)
TOOLING_FRICTION_RE = re.compile(
    r"(?i)\b("
    r"unknown option|invalid option|usage:|command not found|no such file|"
    r"permission denied|missing required|required option|must specify|"
    r"not found|unsupported"
    r")\b"
)
# "Stuck"/hang signals: the CLI did not cleanly fail, it stalled, timed out, or
# was canceled. This is friction even without a non-zero exit.
HANG_RE = re.compile(
    r"(?i)("
    r"timed out|timeout|deadline exceeded|context deadline|operation canceled|"
    r"operation cancelled|still running|appears stuck|took too long|"
    r"killed|sigterm|sigkill"
    r")"
)
# A result must be exactly empty after removing a known runner envelope. Do not
# search for these strings inside larger output: a response that merely contains
# an empty object or "0 rows" is not itself empty.
SILENT_EMPTY_RE = re.compile(
    r"(?is)^(?:\[\]|\{\}|null|no results[.!]?|\(?\s*0 rows?\s*\)?|\(empty\))$"
)
SILENT_EMPTY_RUNNER_RE = re.compile(
    r"(?is)^script completed\s*\nwall time[^\n]*\noutput:\s*\n?"
)
SILENT_EMPTY_EXIT_RUNNER_RE = re.compile(
    r"(?is)^process exited with code\s+0\s*\n(?:final )?output:\s*\n?"
)
SILENT_EMPTY_ACK_RE = re.compile(
    r"(?i)\b("
    r"no (?:(?:existing|matching) )?(?:results?|matches?|records?|rows?|items?|data|entries|"
    r"pull requests?|prs?|logs?|threads?)|"
    r"(?:returned|found|got|shows?) (?:no|zero) (?:results?|matches?|records?|rows?|"
    r"items?|data|entries|pull requests?|prs?|logs?|threads?)|"
    r"empty (?:result|response|list|object|output)|nothing (?:matched|returned|found)"
    r")\b"
)
# Generic data-returning command shapes. Tracked CLIs and MCP calls qualify
# separately; these signatures cover common shell/API query paths without
# treating every command with no stdout as a failed fetch.
SILENT_EMPTY_FETCH_VERBS = {"fetch", "get", "list", "query", "read", "search", "show"}
SILENT_EMPTY_MUTATION_VERBS = {
    "create",
    "delete",
    "deploy",
    "edit",
    "insert",
    "mkdir",
    "post",
    "publish",
    "put",
    "remove",
    "rm",
    "send",
    "set",
    "touch",
    "update",
    "upload",
    "write",
}
SILENT_EMPTY_IGNORE_EXECUTABLES = {
    "[",
    "chmod",
    "cp",
    "echo",
    "grep",
    "mkdir",
    "mv",
    "printf",
    "rg",
    "rm",
    "tee",
    "test",
    "touch",
}
DETECT_SILENT_EMPTY = True
# A tracked CLI invoked at least this many times in a single session can be
# retry-before-success friction when corroborated by failure/hang evidence or
# same-subcommand flag variation.
RETRY_STUCK_THRESHOLD = 3
BACKLOG_IGNORE_EXECUTABLES = {
    "",
    "-v",
    "<redacted-long-token>",
    "awk",
    "cat",
    "cd",
    "chmod",
    "cp",
    "curl",
    "echo",
    "env",
    "export",
    "false",
    "find",
    "for",
    "grep",
    "head",
    "if",
    "jq",
    "ls",
    "mkdir",
    "mv",
    "printf",
    "pwd",
    "rg",
    "rm",
    "sed",
    "set",
    "sleep",
    "source",
    "ssh",
    "tail",
    "tee",
    "test",
    "touch",
    "true",
    "wc",
    "while",
    "which",
}

SECRET_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?i)(authorization:\s*)(bearer|basic)\s+[^\s,;]+"), r"\1<redacted-auth>"),
    (re.compile(r"(?i)(cookie:\s*)[^\n\r]+"), r"\1<redacted-cookie>"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|session[_-]?cookie)"
            r"([\"'\s:=]+)([^\"'\s,;]{8,})"
        ),
        r"\1\2<redacted-secret>",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "<redacted-openai-key>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    (
        re.compile(
            r"(?i)\b(aws_?secret_?access_?key|secret_?access_?key|client_?secret|"
            r"access_?token|refresh_?token)([\"'\s:=]+)([^\"'\s,;]{8,})"
        ),
        r"\1\2<redacted-secret>",
    ),
    (re.compile(r"\b[srp]k_(live|test)_[A-Za-z0-9]{16,}\b"), "<redacted-stripe-key>"),
    (re.compile(r"\bwhsec_[A-Za-z0-9]{16,}\b"), "<redacted-stripe-key>"),
    (re.compile(r"\bxox[abeprs]-[A-Za-z0-9-]{10,}\b"), "<redacted-slack-token>"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "<redacted-google-key>"),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"), "<redacted-npm-token>"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "<redacted-gitlab-token>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "<redacted-github-token>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "<redacted-github-token>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"), "<redacted-jwt>"),
    (re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\w)"), "<phone>"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "<redacted-private-key>",
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"), "<redacted-auth>"),
    (re.compile(r"\b[A-Za-z0-9_+/=-]{56,}\b"), "<redacted-long-token>"),
]

# A user message that opens with an XML-ish tag is injected scaffolding
# (system instructions, task seeds, notifications), not something the user
# typed. This generic rule future-proofs the explicit marker list below.
LEADING_TAG_RE = re.compile(r"^<[A-Za-z][A-Za-z0-9_-]*[\s>/]")
# Extra scaffold markers loaded from the config file ("extra_scaffold_markers").
EXTRA_SCAFFOLD_MARKERS: List[str] = []
# Shell command literals inside current Codex runtime code. Restricting to
# cmd/command properties avoids treating patch text, plan prose, and regex
# arguments as executed CLI commands.
CODE_SHELL_STRING_RE = re.compile(
    r"(?<![\w.-])(?:['\"])?(?:cmd|command)(?:['\"])?\s*:\s*"
    r"(['\"`])((?:\\.|(?!\1).)*)\1"
)
# A tool input that opens with a code keyword is a program, not a shell command.
CODE_COMMAND_RE = re.compile(
    r"^\s*(?://[^\n]*\n\s*)*(const|let|var|await|async|function|return|import|export|try|if)\b"
)
# When False (default), failures inside subagent transcripts do not feed the
# backlog route: exploratory subagents fail by design while probing.
INCLUDE_SUBAGENT_FAILURES = False


@dataclass
class Evidence:
    source: str
    path: str
    line: int
    kind: str
    excerpt: str
    session_id: str = ""
    tool_name: str = ""
    command: str = ""
    occurred_at: str = ""
    machine: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "path": self.path,
            "line": self.line,
            "session_id": self.session_id,
            "kind": self.kind,
            "tool_name": self.tool_name,
            "command": self.command,
            "occurred_at": self.occurred_at,
            "machine": self.machine,
            "excerpt": self.excerpt,
        }


@dataclass
class ToolCall:
    call_id: str
    name: str
    line: int
    command: str = ""
    skill: str = ""
    clis: List[str] = field(default_factory=list)
    occurred_at: str = ""


@dataclass
class SessionSummary:
    source: str
    path: Path
    session_id: str
    cwd: str = ""
    started_at: str = ""
    ended_at: str = ""
    machine: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tracked_cli_invocations: Dict[str, List[Evidence]] = field(default_factory=dict)
    skill_invocations: Dict[str, List[Evidence]] = field(default_factory=dict)
    failures: List[Evidence] = field(default_factory=list)
    silent_empty: List[Evidence] = field(default_factory=list)
    corrections: List[Evidence] = field(default_factory=list)

    def has_signal(self) -> bool:
        return bool(
            self.tracked_cli_invocations
            or self.skill_invocations
            or self.failures
            or self.silent_empty
            or self.corrections
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "path": str(self.path),
            "session_id": self.session_id,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "machine": self.machine,
            "tool_call_count": len(self.tool_calls),
            "tracked_cli_names": sorted(self.tracked_cli_invocations),
            "skill_names": sorted(self.skill_invocations),
            "failure_count": len(self.failures),
            "silent_empty_count": len(self.silent_empty),
            "correction_count": len(self.corrections),
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_time(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            value = value / 1000.0
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def file_mtime_utc(path: Path) -> dt.datetime:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)


PATH_TIMESTAMP_RE = re.compile(
    r"(?:rollout-)?(\d{4}-\d{2}-\d{2}T\d{2}[-:]\d{2}[-:]\d{2})(?:\.\d+)?Z?"
)


def isoformat_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def evidence_time(ev: Evidence) -> Optional[dt.datetime]:
    """Return the strongest available event time for an evidence item.

    Parsed per-record transcript timestamps are authoritative. Older or
    synthetic formats without one fall back to the transcript mtime, then a
    timestamp encoded in a Codex rollout filename.
    """
    parsed = parse_time(ev.occurred_at)
    if parsed:
        return parsed
    path = Path(ev.path).expanduser()
    try:
        if path.exists():
            return file_mtime_utc(path)
    except OSError:
        pass
    match = PATH_TIMESTAMP_RE.search(path.name)
    if match:
        date_part, time_part = match.group(1).split("T", 1)
        parsed = parse_time(f"{date_part}T{time_part.replace('-', ':')}Z")
        if parsed:
            return parsed
    return None


def latest_evidence_time(evidence_items: List[Evidence]) -> Optional[dt.datetime]:
    times = [value for value in (evidence_time(ev) for ev in evidence_items) if value]
    return max(times) if times else None


def shorten(text: str, limit: int = 360) -> str:
    if FULL_DETAIL:
        limit = max(limit, FULL_EXCERPT_LIMIT)
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def redact(text: str) -> str:
    out = text or ""
    if FULL_DETAIL:
        return out
    for pattern, replacement in SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_for_fleet(text: str) -> str:
    """Always redact a fleet-bound string, even when local --full mode is set."""
    out = text or ""
    for pattern, replacement in SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_structure_for_fleet(value: Any) -> Any:
    """Recursively sanitize every string crossing a machine boundary."""
    if isinstance(value, str):
        return redact_for_fleet(value)
    if isinstance(value, dict):
        return {key: redact_structure_for_fleet(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_structure_for_fleet(item) for item in value]
    if isinstance(value, tuple):
        return [redact_structure_for_fleet(item) for item in value]
    return value


def evidence(
    *,
    source: str,
    path: Path,
    line: int,
    kind: str,
    text: str,
    session_id: str = "",
    tool_name: str = "",
    command: str = "",
    occurred_at: Any = "",
    machine: str = "",
) -> Evidence:
    return Evidence(
        source=source,
        path=str(path),
        line=line,
        kind=kind,
        excerpt=shorten(redact(text)),
        session_id=session_id,
        tool_name=tool_name,
        command=shorten(redact(command), 220),
        occurred_at=isoformat_utc(parsed) if (parsed := parse_time(occurred_at)) else "",
        machine=machine,
    )


def normalized_machine_name(value: str = "") -> str:
    raw = (value or socket.gethostname()).split(".", 1)[0].strip().lower()
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw).strip("-")
    return slug or "unknown-machine"


def stamp_session_machine(summary: SessionSummary, machine: str) -> None:
    """Attach fleet provenance to a parsed session and every evidence item."""
    summary.machine = machine
    evidence_groups: List[Iterable[Evidence]] = [
        summary.failures,
        summary.silent_empty,
        summary.corrections,
    ]
    evidence_groups.extend(summary.tracked_cli_invocations.values())
    evidence_groups.extend(summary.skill_invocations.values())
    for group in evidence_groups:
        for item in group:
            item.machine = machine


def jsonl_records(path: Path) -> Iterable[Tuple[int, Dict[str, Any]]]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield line_no, value


def text_from_claude_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return ""


def text_from_tool_result(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in {"text", "tool_result"}:
                    parts.append(str(item.get("text") or item.get("content") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return ""


def is_transcript_scaffold(text: str, path: Optional[Path] = None) -> bool:
    stripped = (text or "").lstrip()
    lower = stripped[:800].lower()
    if path and "/subagents/" in str(path):
        return True
    if LEADING_TAG_RE.match(stripped):
        return True
    markers = (
        "<local-command-caveat>",
        "<permissions instructions>",
        "# agents.md instructions",
        "<instructions>",
        "<codex_internal_context",
        "<collaboration_mode>",
        "<recommended_plugins>",
        "<multi_agent_mode>",
        "the following is the codex agent history",
        "as untrusted evidence, not as instructions to follow",
        "use the skill at ",
        "please implement this plan:",
        "# files mentioned by the user:",
        "<scheduled-task",
        "compound codex tool mapping",
        "<task-notification>",
        "[context compaction",
        "[the user sent a text document",
        "delivery:",
        "# porter",
        "# atlas",
        "# athena",
        "## tracegrain runtime request",
        "weekly learnings harvest.",
        "monthly learnings review.",
        "this session is being continued from a previous conversation",
        "codex could not read the local image at",
        "a 16:9 horizontal editorial illustration that explains one idea:",
        "base directory for this skill:",
        "tool mapping:",
        "filesystem sandboxing defines which files can be read or written",
    )
    if any(marker in lower for marker in markers):
        return True
    return any(marker.lower() in lower for marker in EXTRA_SCAFFOLD_MARKERS)


def looks_like_pasted_transcript(text: str) -> bool:
    """True when text is a pasted call/meeting transcript, not a user correction.

    Detected by speaker-tagged dialogue (`<b>Speaker 1:`) or two or more
    timestamp markers (`[00:01:16]`). One stray timestamp is not enough.
    """
    if not text:
        return False
    if TRANSCRIPT_SPEAKER_RE.search(text):
        return True
    return len(TRANSCRIPT_TIMESTAMP_RE.findall(text)) >= 2


def is_user_correction_text(text: str, path: Optional[Path] = None) -> bool:
    if not text or is_transcript_scaffold(text, path) or looks_like_pasted_transcript(text):
        return False
    stripped = text.strip()
    # A proposal must show the phrase that triggered it. Searching beyond the
    # normal evidence excerpt turns appended runtime instructions into
    # invisible false positives that a reviewer cannot validate.
    window = stripped[:CORRECTION_SCAN_LIMIT]
    if len(stripped) <= STRONG_CORRECTION_MAX_CHARS and STRONG_CORRECTION_RE.search(window):
        return True
    return len(stripped) <= WEAK_CORRECTION_MAX_CHARS and bool(WEAK_CORRECTION_RE.search(window))


def add_tracked_cli_evidence(summary: SessionSummary, cli: str, ev: Evidence) -> None:
    summary.tracked_cli_invocations.setdefault(cli, []).append(ev)


def capture_tracked_cli_hang(
    summary: SessionSummary,
    source: str,
    path: Path,
    line_no: int,
    command: str,
    output: str,
    clis: Optional[List[str]] = None,
    occurred_at: Any = "",
) -> None:
    """Record a non-failing tracked CLI result that stalled or timed out.

    A clean timeout/cancel does not trip the failure regex, so without this the
    "stuck" case the loop is meant to catch would be invisible. Only called for
    output that is not already classified as a failure.
    """
    if not output or not HANG_RE.search(output):
        return
    # Runtime wrappers explicitly distinguish a completed call from one that
    # is still running. Words such as "timeout" inside completed help output
    # describe flags or docs; they are not evidence that the invocation hung.
    if COMPLETED_TOOL_RESULT_RE.search(output) or GOOD_EXIT_RE.search(output):
        return
    # Older transcripts do not carry wrapper status. Keep inspection commands
    # conservative: their output commonly documents timeouts and cancellation.
    # Strong friction language is the exception — a help/doctor probe that
    # comes back with a hard error phrase is real evidence, not documentation.
    if INSPECTION_COMMAND_RE.search(command) and not TRACKED_CLI_STRONG_FRICTION_RE.search(output):
        return
    for cli in (clis if clis is not None else tracked_cli_names(command)):
        add_tracked_cli_evidence(
            summary,
            cli,
            evidence(
                source=source,
                path=path,
                line=line_no,
                kind="tracked_cli_hang",
                text=output,
                session_id=summary.session_id,
                tool_name="Bash",
                command=command,
                occurred_at=occurred_at,
            ),
        )


def shell_tokens(command: str) -> List[str]:
    # A shell newline terminates a command. Preserve that boundary before
    # shlex consumes all whitespace, otherwise adjacent commands can fuse.
    command = (command or "").replace("\n", " ; ").replace("\t", " ")
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return (command or "").split()


def strip_heredoc_bodies(command: str) -> str:
    """Remove shell heredoc payloads so prompt/code text is not executable."""
    text = command or ""
    search_from = 0
    opener_re = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
    while True:
        opener = opener_re.search(text, search_from)
        if not opener:
            return text
        body_start = text.find("\n", opener.end())
        if body_start < 0:
            return text
        delimiter = re.escape(opener.group(2))
        terminator = re.search(
            rf"(?m)^[ \t]*{delimiter}[ \t]*(?=['\"]?[ \t]*(?:\n|$))",
            text[body_start + 1 :],
        )
        if not terminator:
            return text[: body_start + 1]
        term_start = body_start + 1 + terminator.start()
        text = text[: body_start + 1] + text[term_start:]
        search_from = body_start + 1


def tracked_cli_names(command: str) -> List[str]:
    return sorted(_tracked_cli_names(command or "", depth=0))


def tracked_cli_names_from_code(text: str) -> List[str]:
    """Extract tracked-CLI names from string literals inside code-shaped input.

    Current Codex sessions execute tools through a JavaScript runtime, so the
    recorded input is code with the real shell commands embedded as string
    literals. Each literal is tokenized like a shell command, so prose mentions
    in argument position still do not count as invocations.
    """
    names: set[str] = set()
    if TRACKED_CLI_SUFFIX not in (text or ""):
        return []
    for match in CODE_SHELL_STRING_RE.finditer(text):
        # JavaScript string literals preserve shell newlines/tabs as escape
        # sequences in the transcript. POSIX shlex treats the backslash as an
        # escape and would otherwise fuse `true\nedge-cli` into the
        # fabricated command name `malformed-edge-cli`.
        literal = match.group(2).replace(r"\n", "\n").replace(r"\t", "\t")
        if TRACKED_CLI_SUFFIX in literal:
            names.update(_tracked_cli_names(literal, depth=1))
    return sorted(names)


def tracked_cli_argv_shapes(command: str, cli: str) -> List[Tuple[str, Tuple[str, ...]]]:
    """Return (subcommand, flags) shapes for a tracked CLI invocation.

    Shape comparison deliberately ignores positional values. Retry evidence is
    meant to catch syntax guessing (the same subcommand tried with different
    flags), not repeated reads of different records.
    """
    candidates = [command]
    if CODE_COMMAND_RE.match(command or ""):
        candidates = [
            match.group(2).replace(r"\n", "\n").replace(r"\t", "\t")
            for match in CODE_SHELL_STRING_RE.finditer(command)
            if cli in match.group(2)
        ]

    shapes: List[Tuple[str, Tuple[str, ...]]] = []
    for candidate in candidates:
        tokens = shell_tokens(candidate)
        for index, token in enumerate(tokens):
            if Path(token.strip()).name.strip() != cli:
                continue
            argv: List[str] = []
            for arg in tokens[index + 1 :]:
                if arg in SHELL_SEPARATORS:
                    break
                argv.append(arg)
            subcommand_parts: List[str] = []
            for arg in argv:
                if arg.startswith("-"):
                    break
                subcommand_parts.append(arg)
            subcommand = " ".join(subcommand_parts)
            flags = tuple(
                sorted({arg.split("=", 1)[0] for arg in argv if arg.startswith("-")})
            )
            shapes.append((subcommand, flags))
    return shapes


def has_retry_shape_variation(invocations: List[Evidence], cli: str) -> bool:
    """True when one subcommand was tried with more than one flag shape."""
    by_subcommand: Dict[str, set[Tuple[str, ...]]] = {}
    for item in invocations:
        for subcommand, flags in tracked_cli_argv_shapes(item.command, cli):
            if subcommand:
                by_subcommand.setdefault(subcommand, set()).add(flags)
    return any(len(flag_shapes) > 1 for flag_shapes in by_subcommand.values())


def _tracked_cli_names(command: str, depth: int) -> set[str]:
    if depth > 2 or TRACKED_CLI_SUFFIX not in command:
        return set()

    command = strip_heredoc_bodies(command)
    names: set[str] = set()
    tokens = shell_tokens(command)
    command_expected = True
    current_executable = ""

    for index, token in enumerate(tokens):
        if token in SHELL_SEPARATORS:
            command_expected = True
            current_executable = ""
            continue

        basename = Path(token.strip()).name.strip()
        if command_expected:
            if ENV_ASSIGNMENT_RE.match(token):
                continue
            if basename in COMMAND_PREFIXES:
                command_expected = True
                current_executable = basename
                continue

            current_executable = basename
            command_expected = False
            if basename.endswith(TRACKED_CLI_SUFFIX):
                names.add(basename)
                continue
            if basename in REMOTE_COMMAND_WRAPPERS:
                for nested in tokens[index + 1 :]:
                    if TRACKED_CLI_SUFFIX in nested:
                        names.update(_tracked_cli_names(nested, depth + 1))
                continue

        if current_executable == "source":
            if basename in {"kssh", "kssh_once"}:
                for nested in tokens[index + 1 :]:
                    if TRACKED_CLI_SUFFIX in nested:
                        names.update(_tracked_cli_names(nested, depth + 1))
                continue
        if current_executable in REMOTE_COMMAND_WRAPPERS and TRACKED_CLI_SUFFIX in token:
            names.update(_tracked_cli_names(token, depth + 1))

    names.update(tracked_cli_names_from_for_loop(command))
    return names


def tracked_cli_names_from_for_loop(command: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(.+?)\s*;?\s*do\b(.+?)(?:\bdone\b|$)", command, re.DOTALL):
        variable, items, body = match.groups()
        if f"${variable}" not in body:
            continue
        names.update(TRACKED_CLI_RE.findall(items))
    return names


def is_failure_text(text: str, command: str = "", clis: Optional[List[str]] = None) -> bool:
    if not text:
        return False
    tracked_names = tracked_cli_names(command) if clis is None else clis
    if BAD_EXIT_RE.search(text):
        return True
    if GOOD_EXIT_RE.search(text):
        return False
    if tracked_names:
        # Tracked CLI failures need an explicit status signal. Text-only
        # friction is too ambiguous: help, doctor, inventory, truncated output,
        # and deliberate auth/404 probes all contain error-shaped language.
        return False
    return bool(FAILURE_RE.search(text))


def tool_result_is_failure(
    call: Optional[ToolCall], text: str, is_error: Optional[bool] = None
) -> bool:
    """Classify a result while requiring status for tracked CLI/MCP calls."""
    if isinstance(is_error, bool):
        return is_error
    if not text:
        return False
    if call and (call.clis or call.name.startswith("mcp__")):
        return bool(BAD_EXIT_RE.search(text))
    return is_failure_text(text, call.command if call else "", call.clis if call else None)


def codex_tool_output_text(value: Any) -> str:
    """Flatten current Codex output blocks while preserving plain strings."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {
                "input_text",
                "output_text",
                "text",
            }:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("output") or "")
    return "" if value is None else str(value)


def silent_empty_payload(output: str) -> Optional[str]:
    """Return the normalized empty payload, or None for non-empty output."""
    payload = output or ""
    payload = SILENT_EMPTY_RUNNER_RE.sub("", payload, count=1)
    payload = SILENT_EMPTY_EXIT_RUNNER_RE.sub("", payload, count=1)
    payload = payload.strip()
    if not payload:
        return "(empty stdout)"
    return payload if SILENT_EMPTY_RE.fullmatch(payload) else None


def silent_empty_command(call: ToolCall) -> str:
    """Return one attributable shell command, rejecting runtime ambiguity."""
    command = call.command or ""
    if not CODE_COMMAND_RE.match(command):
        return command
    commands = [
        match.group(2).replace(r"\n", "\n").replace(r"\t", "\t")
        for match in CODE_SHELL_STRING_RE.finditer(command)
    ]
    return commands[0] if len(commands) == 1 else ""


def command_intends_data(call: ToolCall, command: str) -> bool:
    """Conservatively classify calls whose successful result should carry data."""
    if call.name.startswith("mcp__"):
        tool_words = set(call.name.rsplit("__", 1)[-1].lower().split("_"))
        return not bool(tool_words & SILENT_EMPTY_MUTATION_VERBS)
    if not command:
        return False
    tokens = shell_tokens(command)
    if not tokens:
        return False
    if any(token in {";", "&&", "||"} for token in tokens):
        return False
    executable = first_executable(command)
    if executable in SILENT_EMPTY_IGNORE_EXECUTABLES:
        return False
    if any(token in {"-q", "--quiet"} for token in tokens):
        return False
    command_words = {
        token.lower().split()[0]
        for token in tokens[1:4]
        if not token.startswith("-") and token.strip()
    }
    if command_words & SILENT_EMPTY_MUTATION_VERBS:
        return False
    for index, token in enumerate(tokens):
        if token in {"-X", "--request", "--method"} and index + 1 < len(tokens):
            if tokens[index + 1].lower() in SILENT_EMPTY_MUTATION_VERBS:
                return False
        if token.startswith(("--request=", "--method=")):
            if token.split("=", 1)[1].lower() in SILENT_EMPTY_MUTATION_VERBS:
                return False
    if re.search(r"(?:^|\s)(?:>|>>)\s*[^&]", command):
        return False
    if executable == "curl" and any(
        token in {"-d", "--data", "--data-raw", "--data-binary", "-o", "--output"}
        or token.startswith("--data=")
        or token.startswith("--output=")
        for token in tokens
    ):
        return False
    if call.clis:
        return True
    if "--json" in tokens:
        return True
    for index, token in enumerate(tokens):
        if token == "--format" and index + 1 < len(tokens) and tokens[index + 1].lower() == "json":
            return True
        if token.lower() == "--format=json":
            return True
    if executable == "curl":
        return True
    if executable == "gh" and "api" in tokens[1:3]:
        return True
    if executable == "bq" and "query" in tokens[1:]:
        return True
    if executable == "psql" and any(token in {"-c", "--command"} for token in tokens):
        return True
    return executable.lower() in SILENT_EMPTY_FETCH_VERBS or any(
        token.lower() in SILENT_EMPTY_FETCH_VERBS for token in tokens[1:3]
    )


def silent_empty_evidence(
    summary: SessionSummary,
    source: str,
    path: Path,
    line_no: int,
    call: Optional[ToolCall],
    output: str,
    is_error: Optional[bool],
    occurred_at: Any,
) -> Optional[Evidence]:
    """Build high-confidence evidence for a swallowed, successful empty result."""
    if not DETECT_SILENT_EMPTY or not call or is_error:
        return None
    if len(call.clis) > 1:
        return None
    command = silent_empty_command(call)
    if not command_intends_data(call, command):
        return None
    # This detector is intentionally stricter than hard-failure classification:
    # any visible failure or hang phrase disqualifies the silent-empty signal.
    if BAD_EXIT_RE.search(output) or FAILURE_RE.search(output) or HANG_RE.search(output):
        return None
    payload = silent_empty_payload(output)
    if payload is None:
        return None
    return evidence(
        source=source,
        path=path,
        line=line_no,
        kind="silent_empty",
        text=payload,
        session_id=summary.session_id,
        tool_name=call.name,
        command=command,
        occurred_at=occurred_at,
    )


def record_silent_empty(summary: SessionSummary, ev: Evidence, call: ToolCall) -> None:
    summary.silent_empty.append(ev)
    for cli in call.clis:
        add_tracked_cli_evidence(summary, cli, ev)


def empty_result_was_swallowed(
    result_line: int,
    agent_step_lines: List[int],
    agent_messages: List[Tuple[int, str]],
) -> bool:
    """Require a later step, unless that step explicitly acknowledges emptiness."""
    later_steps = [line for line in agent_step_lines if line > result_line]
    if not later_steps:
        return False
    next_step = min(later_steps)
    return not any(
        line == next_step and SILENT_EMPTY_ACK_RE.search(text)
        for line, text in agent_messages
    )


def tracked_cli_failures_for_output(
    command: str,
    output: str,
    clis: Optional[List[str]] = None,
    confirmed_failure: bool = False,
) -> List[str]:
    clis = tracked_cli_names(command) if clis is None else clis
    if not clis or (not confirmed_failure and not is_failure_text(output, command, clis)):
        return []
    if len(clis) == 1:
        return list(clis)

    lines = output.splitlines()
    localized = set()
    for index, line in enumerate(lines):
        for cli in clis:
            if cli not in line:
                continue
            window = "\n".join(lines[max(0, index - 1) : index + 3])
            if BAD_EXIT_RE.search(window) or TRACKED_CLI_FRICTION_RE.search(window):
                localized.add(cli)
    return sorted(localized)


def parse_claude_session(path: Path) -> SessionSummary:
    summary = SessionSummary(source="claude", path=path, session_id=path.stem)
    calls: Dict[str, ToolCall] = {}
    silent_candidates: List[Tuple[int, ToolCall, str, Optional[bool], Any]] = []
    agent_step_lines: List[int] = []
    agent_messages: List[Tuple[int, str]] = []
    seen_user_texts: set[str] = set()
    agent_has_responded = False

    for line_no, rec in jsonl_records(path):
        session_id = str(rec.get("sessionId") or summary.session_id)
        summary.session_id = session_id or summary.session_id
        summary.cwd = str(rec.get("cwd") or summary.cwd or "")
        ts = rec.get("timestamp")
        if ts:
            summary.started_at = summary.started_at or str(ts)
            summary.ended_at = str(ts)

        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        role = msg.get("role") or rec.get("type")
        content = msg.get("content")

        if role == "assistant":
            agent_has_responded = True
            if content:
                agent_step_lines.append(line_no)
                assistant_text = text_from_claude_content(content)
                if assistant_text:
                    agent_messages.append((line_no, assistant_text))

        if role == "user":
            text = text_from_claude_content(content)
            if text:
                if (
                    agent_has_responded
                    and seen_user_texts
                    and text not in seen_user_texts
                    and is_user_correction_text(text, path)
                ):
                    summary.corrections.append(
                        evidence(
                            source="claude",
                            path=path,
                            line=line_no,
                            kind="user_correction",
                            text=text,
                            session_id=summary.session_id,
                            occurred_at=ts,
                        )
                    )
                if not is_transcript_scaffold(text, path):
                    seen_user_texts.add(text)

        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    name = str(item.get("name") or "")
                    inp = item.get("input") if isinstance(item.get("input"), dict) else {}
                    call_id = str(item.get("id") or f"{path}:{line_no}:{len(calls)}")
                    command = str(inp.get("command") or "")
                    skill = str(inp.get("skill") or "")
                    call = ToolCall(
                        call_id=call_id,
                        name=name,
                        line=line_no,
                        command=command,
                        skill=skill,
                        clis=tracked_cli_names(command) if name == "Bash" else [],
                        occurred_at=(isoformat_utc(parsed) if (parsed := parse_time(ts)) else ""),
                    )
                    calls[call_id] = call
                    summary.tool_calls.append(call)
                    if name == "Skill" and skill:
                        summary.skill_invocations.setdefault(skill, []).append(
                            evidence(
                                source="claude",
                                path=path,
                                line=line_no,
                                kind="skill_invocation",
                                text=f"Skill({skill})",
                                session_id=summary.session_id,
                                tool_name=name,
                                occurred_at=ts,
                            )
                        )
                    if name == "Bash" and command:
                        for cli in call.clis:
                            add_tracked_cli_evidence(
                                summary,
                                cli,
                                evidence(
                                    source="claude",
                                    path=path,
                                    line=line_no,
                                    kind="tracked_cli_invocation",
                                    text=command,
                                    session_id=summary.session_id,
                                    tool_name=name,
                                    command=command,
                                    occurred_at=ts,
                                ),
                            )
                elif item.get("type") == "tool_result":
                    call_id = str(item.get("tool_use_id") or "")
                    call = calls.get(call_id)
                    result_text = text_from_tool_result(item.get("content"))
                    # Claude Code marks failed tool calls explicitly; trust that
                    # flag when present. Older tracked CLI/MCP results require
                    # an exit-code signal instead of ambiguous output text.
                    is_err = item.get("is_error")
                    failed = tool_result_is_failure(
                        call,
                        result_text,
                        is_err if isinstance(is_err, bool) else None,
                    )
                    if call and result_text and failed:
                        ev = evidence(
                            source="claude",
                            path=path,
                            line=line_no,
                            kind="tool_failure",
                            text=result_text,
                            session_id=summary.session_id,
                            tool_name=call.name,
                            command=call.command,
                            occurred_at=ts,
                        )
                        summary.failures.append(ev)
                        if call.name == "Bash":
                            clis = tracked_cli_failures_for_output(
                                call.command,
                                result_text,
                                call.clis,
                                confirmed_failure=True,
                            )
                            for cli in clis or call.clis:
                                add_tracked_cli_evidence(summary, cli, ev)
                    elif call and result_text and call.name == "Bash":
                        capture_tracked_cli_hang(
                            summary,
                            "claude",
                            path,
                            line_no,
                            call.command,
                            result_text,
                            call.clis,
                            ts,
                        )

                    if call:
                        silent_candidates.append(
                            (
                                line_no,
                                call,
                                result_text,
                                is_err if isinstance(is_err, bool) else None,
                                ts,
                            )
                        )

    for line_no, call, output, is_err, ts in silent_candidates:
        if not empty_result_was_swallowed(line_no, agent_step_lines, agent_messages):
            continue
        ev = silent_empty_evidence(
            summary, "claude", path, line_no, call, output, is_err, ts
        )
        if ev:
            record_silent_empty(summary, ev, call)
    return summary


def parse_json_maybe(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def codex_message_text(payload: Dict[str, Any]) -> str:
    if payload.get("type") == "user_message":
        msg = payload.get("message")
        if isinstance(msg, str):
            return msg
        elems = payload.get("text_elements")
        if isinstance(elems, list):
            return "\n".join(str(x) for x in elems if x)
    if payload.get("type") == "message":
        content = payload.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"input_text", "text"}:
                    parts.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
    return ""


def parse_codex_session(path: Path) -> SessionSummary:
    summary = SessionSummary(source="codex", path=path, session_id=path.stem)
    calls: Dict[str, ToolCall] = {}
    silent_candidates: List[Tuple[int, ToolCall, str, Optional[bool], Any]] = []
    agent_step_lines: List[int] = []
    agent_messages: List[Tuple[int, str]] = []
    seen_user_texts: set[str] = set()
    agent_has_responded = False

    for line_no, rec in jsonl_records(path):
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        ts = rec.get("timestamp") or payload.get("timestamp")
        if ts:
            summary.started_at = summary.started_at or str(ts)
            summary.ended_at = str(ts)

        if rec.get("type") == "session_meta":
            summary.session_id = str(payload.get("id") or summary.session_id)
            summary.cwd = str(payload.get("cwd") or summary.cwd or "")
            continue
        if rec.get("type") == "turn_context":
            summary.cwd = str(payload.get("cwd") or summary.cwd or "")

        if payload.get("role") == "assistant" or payload.get("type") in {
            "agent_message",
            "assistant_message",
            "custom_tool_call",
            "function_call",
            "reasoning",
        }:
            agent_has_responded = True

        text = codex_message_text(payload)
        # Codex stores developer, user, and assistant messages in the same
        # response_item/message shape. Only user-role messages can carry user
        # corrections; accepting every role turns runtime instructions and
        # assistant prose into false correction evidence.
        # Older fixtures may omit role, so preserve that legacy user shape.
        is_user_text = payload.get("type") == "user_message" or (
            payload.get("type") == "message" and payload.get("role") in {None, "user"}
        )
        if text and is_user_text:
            if (
                agent_has_responded
                and seen_user_texts
                and text not in seen_user_texts
                and is_user_correction_text(text, path)
            ):
                summary.corrections.append(
                    evidence(
                        source="codex",
                        path=path,
                        line=line_no,
                        kind="user_correction",
                        text=text,
                        session_id=summary.session_id,
                        occurred_at=ts,
                    )
                )
            if not is_transcript_scaffold(text, path):
                seen_user_texts.add(text)

        payload_type = payload.get("type")
        if payload_type == "agent_message" or (
            payload_type == "message" and payload.get("role") == "assistant"
        ):
            agent_step_lines.append(line_no)
            agent_text = (
                str(payload.get("message") or "")
                if payload_type == "agent_message"
                else codex_message_text(payload)
            )
            if agent_text:
                agent_messages.append((line_no, agent_text))
        if payload_type in {"function_call", "custom_tool_call"}:
            agent_step_lines.append(line_no)
            name = str(payload.get("name") or "")
            call_id = str(payload.get("call_id") or f"{path}:{line_no}:{len(calls)}")
            if payload_type == "custom_tool_call":
                # Newer Codex sessions execute tools through a code runtime:
                # the input is a raw string (often JavaScript) with the real
                # shell commands embedded as string literals.
                command = str(payload.get("input") or "")
                if CODE_COMMAND_RE.match(command):
                    clis = tracked_cli_names_from_code(command)
                else:
                    clis = sorted(
                        set(tracked_cli_names(command)) | set(tracked_cli_names_from_code(command))
                    )
            else:
                args = parse_json_maybe(payload.get("arguments"))
                command = str(args.get("cmd") or args.get("command") or "")
                clis = tracked_cli_names(command)
            call = ToolCall(
                call_id=call_id,
                name=name,
                line=line_no,
                command=command,
                clis=clis,
                occurred_at=(isoformat_utc(parsed) if (parsed := parse_time(ts)) else ""),
            )
            calls[call_id] = call
            summary.tool_calls.append(call)
            if command:
                for cli in clis:
                    add_tracked_cli_evidence(
                        summary,
                        cli,
                        evidence(
                            source="codex",
                            path=path,
                            line=line_no,
                            kind="tracked_cli_invocation",
                            text=command,
                            session_id=summary.session_id,
                            tool_name=name,
                            command=command,
                            occurred_at=ts,
                        ),
                    )
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or "")
            call = calls.get(call_id)
            output = codex_tool_output_text(payload.get("output"))
            is_err = payload.get("is_error")
            status = is_err if isinstance(is_err, bool) else None
            if call and output and tool_result_is_failure(call, output, status):
                ev = evidence(
                    source="codex",
                    path=path,
                    line=line_no,
                    kind="tool_failure",
                    text=output,
                    session_id=summary.session_id,
                    tool_name=call.name,
                    command=call.command,
                    occurred_at=ts,
                )
                summary.failures.append(ev)
                for cli in tracked_cli_failures_for_output(
                    call.command, output, call.clis, confirmed_failure=True
                ):
                    add_tracked_cli_evidence(summary, cli, ev)
            elif call and output:
                capture_tracked_cli_hang(
                    summary, "codex", path, line_no, call.command, output, call.clis, ts
                )

            if call:
                silent_candidates.append((line_no, call, output, status, ts))

    for line_no, call, output, is_err, ts in silent_candidates:
        if not empty_result_was_swallowed(line_no, agent_step_lines, agent_messages):
            continue
        ev = silent_empty_evidence(summary, "codex", path, line_no, call, output, is_err, ts)
        if ev:
            record_silent_empty(summary, ev, call)
    return summary


def command_from_tool_payload(tool_name: str, payload: Any) -> str:
    data = parse_json_maybe(payload)
    if not data:
        return ""
    return str(data.get("command") or data.get("cmd") or "")


def parse_hermes_messages(
    *,
    source: str,
    path: Path,
    profile: str,
    session_id: str,
    rows: Iterable[Tuple[int, str, str, str, str, Any]],
    cwd: str = "",
) -> SessionSummary:
    """Normalize one Hermes SQLite session without treating prompts as corrections."""
    summary = SessionSummary(source=source, path=path, session_id=session_id, cwd=cwd)
    calls: Dict[str, ToolCall] = {}
    seen_user_texts: set[str] = set()
    agent_has_responded = False

    for line_no, role, content, tool_calls_raw, tool_call_id, timestamp in rows:
        parsed_time = parse_time(timestamp)
        if parsed_time:
            iso = isoformat_utc(parsed_time)
            summary.started_at = summary.started_at or iso
            summary.ended_at = iso
        content = content or ""

        if role in {"assistant", "tool"}:
            agent_has_responded = True
        if role == "user" and content:
            if (
                agent_has_responded
                and seen_user_texts
                and content not in seen_user_texts
                and is_user_correction_text(content, path)
            ):
                summary.corrections.append(
                    evidence(
                        source=source,
                        path=path,
                        line=line_no,
                        kind="user_correction",
                        text=content,
                        session_id=session_id,
                        occurred_at=timestamp,
                    )
                )
            if not is_transcript_scaffold(content, path):
                seen_user_texts.add(content)

        if tool_calls_raw:
            try:
                parsed_calls = json.loads(tool_calls_raw)
            except (json.JSONDecodeError, TypeError):
                parsed_calls = []
            if isinstance(parsed_calls, dict):
                parsed_calls = [parsed_calls]
            if isinstance(parsed_calls, list):
                for index, item in enumerate(parsed_calls):
                    if not isinstance(item, dict):
                        continue
                    fn_obj = item.get("function") if isinstance(item.get("function"), dict) else item
                    fn = fn_obj if isinstance(fn_obj, dict) else {}
                    name = str(fn.get("name") or item.get("name") or "")
                    call_id = str(
                        item.get("id")
                        or item.get("call_id")
                        or f"{session_id}:{line_no}:{index}"
                    )
                    arguments = fn.get("arguments") or item.get("arguments") or {}
                    command = command_from_tool_payload(name, arguments)
                    parsed_arguments = parse_json_maybe(arguments)
                    skill = (
                        str(parsed_arguments.get("skill") or parsed_arguments.get("name") or "")
                        if name in {"skill_view", "skill_manage", "Skill"}
                        else ""
                    )
                    clis = tracked_cli_names(command)
                    call = ToolCall(
                        call_id=call_id,
                        name=name,
                        line=line_no,
                        command=command,
                        skill=skill,
                        clis=clis,
                        occurred_at=(isoformat_utc(parsed_time) if parsed_time else ""),
                    )
                    calls[call_id] = call
                    summary.tool_calls.append(call)
                    if skill:
                        summary.skill_invocations.setdefault(skill, []).append(
                            evidence(
                                source=source,
                                path=path,
                                line=line_no,
                                kind="skill_invocation",
                                text=f"{name}({skill})",
                                session_id=session_id,
                                tool_name=name,
                                occurred_at=timestamp,
                            )
                        )
                    for cli in clis:
                        add_tracked_cli_evidence(
                            summary,
                            cli,
                            evidence(
                                source=source,
                                path=path,
                                line=line_no,
                                kind="tracked_cli_invocation",
                                text=command,
                                session_id=session_id,
                                tool_name=name,
                                command=command,
                                occurred_at=timestamp,
                            ),
                        )

        if role == "tool" and content:
            call = calls.get(str(tool_call_id or ""))
            if call and tool_result_is_failure(call, content, None):
                ev = evidence(
                    source=source,
                    path=path,
                    line=line_no,
                    kind="tool_failure",
                    text=content,
                    session_id=session_id,
                    tool_name=call.name,
                    command=call.command,
                    occurred_at=timestamp,
                )
                summary.failures.append(ev)
                for cli in tracked_cli_failures_for_output(
                    call.command,
                    content,
                    call.clis,
                    confirmed_failure=True,
                ):
                    add_tracked_cli_evidence(summary, cli, ev)
            elif call:
                capture_tracked_cli_hang(
                    summary,
                    source,
                    path,
                    line_no,
                    call.command,
                    content,
                    call.clis,
                    timestamp,
                )

    if profile:
        summary.cwd = summary.cwd or f"profile:{profile}"
    return summary


def hermes_profile_name(db_path: Path, home: Path) -> str:
    try:
        relative = db_path.relative_to(home / ".hermes" / "profiles")
        return relative.parts[0]
    except ValueError:
        return "default"


def discover_hermes_profile_dbs(home: Path) -> List[Path]:
    dbs: List[Path] = []
    default_db = home / ".hermes" / "state.db"
    if default_db.exists():
        dbs.append(default_db)
    profiles_root = home / ".hermes" / "profiles"
    if profiles_root.exists():
        for profile_dir in sorted(item for item in profiles_root.iterdir() if item.is_dir()):
            db_path = profile_dir / "state.db"
            if db_path.exists():
                dbs.append(db_path)
    return dbs


def parse_hermes_profile_db(
    db_path: Path,
    *,
    home: Optional[Path] = None,
    since: Optional[dt.datetime] = None,
    max_sessions: int = 0,
) -> List[SessionSummary]:
    home = home or Path.home()
    profile = hermes_profile_name(db_path, home)
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        since_timestamp = since.timestamp() if since else 0
        session_rows = connection.execute(
            "select id, coalesce(cwd,''), started_at, "
            "coalesce(ended_at, started_at), source from sessions "
            "where coalesce(ended_at, started_at) >= ? "
            "order by coalesce(ended_at, started_at)",
            (since_timestamp,),
        ).fetchall()
        if max_sessions:
            session_rows = session_rows[-max_sessions:]
        summaries: List[SessionSummary] = []
        for session_id, cwd, _started, _ended, _session_source in session_rows:
            rows = connection.execute(
                "select id, role, coalesce(content,''), coalesce(tool_calls,''), "
                "coalesce(tool_call_id,''), timestamp from messages "
                "where session_id=? and active=1 order by id",
                (session_id,),
            ).fetchall()
            summary = parse_hermes_messages(
                source="hermes_profile_log",
                path=db_path,
                profile=profile,
                session_id=str(session_id),
                rows=rows,
                cwd=str(cwd or f"profile:{profile}"),
            )
            if summary.has_signal():
                summaries.append(summary)
        return summaries
    finally:
        connection.close()



def discover_claude_sessions(home: Path) -> List[Path]:
    root = home / ".claude" / "projects"
    if not root.exists():
        return []
    return sorted(root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime)


def discover_codex_sessions(home: Path) -> List[Path]:
    root = home / ".codex" / "sessions"
    if not root.exists():
        return []
    return sorted(root.glob("**/*.jsonl"), key=lambda p: p.stat().st_mtime)


def discover_session_files(homes: List[Path], source: str) -> List[Tuple[str, Path]]:
    files: List[Tuple[str, Path]] = []
    seen: set[Path] = set()
    for home in homes:
        home = home.expanduser()
        if source in {"all", "claude"}:
            for path in discover_claude_sessions(home):
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    files.append(("claude", path))
        if source in {"all", "codex"}:
            for path in discover_codex_sessions(home):
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    files.append(("codex", path))
    return files


def session_in_window(path: Path, since: Optional[dt.datetime]) -> bool:
    if since is None:
        return True
    return file_mtime_utc(path) >= since


def proposal_key(route: str, target_kind: str, target_name: str, evidence_items: List[Evidence]) -> str:
    h = hashlib.sha256()
    h.update(route.encode())
    h.update(b"\0")
    h.update(target_kind.encode())
    h.update(b"\0")
    h.update(target_name.encode())
    for ev in evidence_items:
        h.update(b"\0")
        h.update(f"{ev.machine}:{ev.source}:{ev.path}:{ev.line}:{ev.kind}".encode())
    return h.hexdigest()[:20]


def make_proposal(
    *,
    route: str,
    title: str,
    summary: str,
    target_kind: str,
    target_name: str,
    evidence_items: List[Evidence],
    suggested_action: str,
    impact: List[str],
) -> Dict[str, Any]:
    key = proposal_key(route, target_kind, target_name, evidence_items)
    created_at = utc_now()
    latest = latest_evidence_time(evidence_items)
    machines = sorted({ev.machine for ev in evidence_items if ev.machine})
    return {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": f"imp-{key}",
        "proposal_key": key,
        "created_at": created_at,
        "latest_evidence_at": isoformat_utc(latest) if latest else created_at,
        "status": "proposed",
        "route": route,
        "title": title,
        "summary": summary,
        "impact": impact,
        "target": {"kind": target_kind, "name": target_name},
        "machines": machines,
        "evidence": [ev.as_dict() for ev in evidence_items[:12]],
        "suggested_action": suggested_action,
        "apply_policy": {
            "mode": "manual_approval_required",
            "notes": "This command stages proposals only. Review and approve before editing skills, memory, runbooks, source code.",
        },
    }




def generate_proposals(sessions: List[SessionSummary]) -> List[Dict[str, Any]]:
    proposals: List[Dict[str, Any]] = []

    tracked_invocations: Dict[str, List[Evidence]] = {}
    tracked_failures: Dict[str, List[Evidence]] = {}
    tracked_hangs: Dict[str, List[Evidence]] = {}
    tracked_silent_empty: Dict[str, List[Evidence]] = {}
    tracked_max_retries: Dict[str, int] = {}
    for session in sessions:
        include_session_silent_empty = (
            INCLUDE_SUBAGENT_FAILURES or "/subagents/" not in str(session.path)
        )
        for cli, items in session.tracked_cli_invocations.items():
            invocations = [item for item in items if item.kind == "tracked_cli_invocation"]
            tracked_invocations.setdefault(cli, []).extend(invocations)
            for item in items:
                # Classify strictly by the kind assigned at parse time. Regexing
                # the excerpt here would re-scan invocation excerpts, which are
                # command text: `timeout 120 foo-cli ...` is not a hang.
                if item.kind == "tool_failure":
                    tracked_failures.setdefault(cli, []).append(item)
                elif item.kind == "tracked_cli_hang":
                    tracked_hangs.setdefault(cli, []).append(item)
                elif item.kind == "silent_empty" and include_session_silent_empty:
                    tracked_silent_empty.setdefault(cli, []).append(item)

            # Retry-before-success is per CLI and session. Repetition alone is
            # normal use; qualify it only when the same session has failure/hang
            # evidence or one subcommand is tried with differing flag shapes.
            by_session: Dict[str, List[Evidence]] = {}
            signal_sessions = {
                item.session_id
                for item in items
                if item.kind in {"tool_failure", "tracked_cli_hang"}
                or (item.kind == "silent_empty" and include_session_silent_empty)
            }
            for item in invocations:
                by_session.setdefault(item.session_id, []).append(item)
            for session_id, session_invocations in by_session.items():
                count = len(session_invocations)
                if count < RETRY_STUCK_THRESHOLD:
                    continue
                if session_id in signal_sessions or has_retry_shape_variation(
                    session_invocations, cli
                ):
                    tracked_max_retries[cli] = max(
                        tracked_max_retries.get(cli, 0), count
                    )

    flagged_clis = sorted(
        cli
        for cli in (
            set(tracked_failures)
            | set(tracked_hangs)
            | set(tracked_silent_empty)
            | {
                cli
                for cli, count in tracked_max_retries.items()
                if count >= RETRY_STUCK_THRESHOLD
            }
        )
        if VALID_TRACKED_CLI_RE.match(cli)
    )
    for cli in flagged_clis:
        failures = tracked_failures.get(cli, [])
        hangs = tracked_hangs.get(cli, [])
        silent_empty = tracked_silent_empty.get(cli, [])
        invocations = tracked_invocations.get(cli, [])
        max_retries = tracked_max_retries.get(cli, 0)
        stuck = max_retries >= RETRY_STUCK_THRESHOLD
        evidence_items = (failures + hangs + silent_empty + invocations)[:12]
        session_count = len({ev.session_id for ev in evidence_items})

        friction_bits: List[str] = []
        if failures:
            friction_bits.append(f"{len(failures)} failure signal(s)")
        if hangs:
            friction_bits.append(f"{len(hangs)} hang/timeout signal(s)")
        if silent_empty:
            friction_bits.append(f"{len(silent_empty)} swallowed empty result(s)")
        if stuck:
            friction_bits.append(f"retried up to {max_retries}x in one session before it worked")
        summary = f"{cli}: " + ", ".join(friction_bits) + f" across {session_count} session(s)."

        action = (
            "Review the evidence. If it reflects a missing command, bad flag, bad JSON "
            "contract, silent-null result, fragile auth flow, or syntax the agent had to "
            "guess and retry, fix the tool itself instead of working around it in a prompt."
        )

        proposals.append(
            make_proposal(
                route="tool",
                title=f"Review {cli} friction from real CLI use",
                summary=summary,
                target_kind="tool",
                target_name=cli,
                evidence_items=evidence_items,
                suggested_action=action,
                impact=["shorter", "safer", "more_correct", "more_ergonomic"],
            )
        )

    skill_corrections: Dict[str, List[Evidence]] = {}
    skill_sessions: Dict[str, set[str]] = {}
    for session in sessions:
        if not session.corrections:
            continue
        for skill, skill_evidence in session.skill_invocations.items():
            first_skill_line = min(ev.line for ev in skill_evidence)
            relevant_corrections = [
                ev for ev in session.corrections if ev.line > first_skill_line
            ]
            if not relevant_corrections:
                continue
            skill_corrections.setdefault(skill, []).extend(skill_evidence + relevant_corrections)
            skill_sessions.setdefault(skill, set()).add(session.session_id)
    for skill, evidence_items in sorted(skill_corrections.items()):
        proposals.append(
            make_proposal(
                route="skill_improvement",
                title=f"Review {skill} skill after user correction",
                summary=(
                    f"The {skill} skill was invoked in {len(skill_sessions.get(skill, set()))} "
                    "session(s) that also contained user correction signal(s)."
                ),
                target_kind="skill",
                target_name=skill,
                evidence_items=evidence_items[:12],
                suggested_action=(
                    "Read the skill and the referenced transcript lines. If the "
                    "correction is durable, stage a patch to SKILL.md or a support "
                    "file. Prefer patching this existing skill over creating a new one."
                ),
                impact=["shorter", "more_correct", "more_ergonomic"],
            )
        )

    # Corrections not tied to a skill are grouped per project (cwd), so the
    # review question becomes "what line in THIS project's CLAUDE.md/AGENTS.md
    # would have prevented this", and one busy session cannot flood the packet.
    corrections_by_project: Dict[str, List[Evidence]] = {}
    for session in sessions:
        if session.corrections and not session.skill_invocations:
            corrections_by_project.setdefault(session.cwd or "unknown-project", []).extend(
                session.corrections[:MAX_CORRECTIONS_PER_SESSION]
            )
    for project, items in sorted(corrections_by_project.items()):
        label = Path(project).name if project != "unknown-project" else project
        proposals.append(
            make_proposal(
                route="memory_context",
                title=f"Review durable corrections for {label}",
                summary=(
                    f"{len(items)} correction signal(s) in sessions under {project} "
                    "were not tied to a specific invoked skill."
                ),
                target_kind="memory_or_runbook",
                target_name=project,
                evidence_items=items[:12],
                suggested_action=(
                    "Classify each correction as durable preference, project runbook "
                    "update, or one-off incident. Stage AGENTS.md/CLAUDE.md/memory edits "
                    "only for durable lessons; discard transient environment failures."
                ),
                impact=["safer", "more_correct", "more_ergonomic"],
            )
        )

    repeated_failures: Dict[str, List[Evidence]] = {}
    for session in sessions:
        if not INCLUDE_SUBAGENT_FAILURES and "/subagents/" in str(session.path):
            # Exploratory subagents fail by design while probing; their
            # failures are not evidence of a durable tooling gap.
            continue
        for fail in session.failures:
            executable = backlog_executable(fail.command)
            if executable and not executable.endswith(TRACKED_CLI_SUFFIX):
                repeated_failures.setdefault(executable, []).append(fail)
    silent_empty_by_executable: Dict[str, List[Evidence]] = {}
    for session in sessions:
        if not INCLUDE_SUBAGENT_FAILURES and "/subagents/" in str(session.path):
            continue
        for empty in session.silent_empty:
            if empty.tool_name.startswith("mcp__"):
                continue
            executable = backlog_executable(empty.command)
            if executable and not executable.endswith(TRACKED_CLI_SUFFIX):
                silent_empty_by_executable.setdefault(executable, []).append(empty)
    for executable in sorted(set(repeated_failures) | set(silent_empty_by_executable)):
        durable_failures = [
            item
            for item in repeated_failures.get(executable, [])
            if TOOLING_FRICTION_RE.search(item.excerpt)
        ]
        empties = silent_empty_by_executable.get(executable, [])
        failure_sessions = len({item.session_id for item in durable_failures})
        empty_sessions = len({item.session_id for item in empties})
        if not (
            (len(durable_failures) >= 3 and failure_sessions >= 2)
            or (len(empties) >= 3 and empty_sessions >= 2)
        ):
            continue
        all_items = durable_failures + empties
        session_count = len({item.session_id for item in all_items})
        signal_bits = []
        if durable_failures:
            signal_bits.append(f"{len(durable_failures)} command-interface failure(s)")
        if empties:
            signal_bits.append(f"{len(empties)} swallowed empty result(s)")
        proposals.append(
            make_proposal(
                route="backlog",
                title=f"Investigate repeated {executable} command friction",
                summary=(
                    f"{executable} had {', '.join(signal_bits)} across "
                    f"{session_count} session(s)."
                ),
                target_kind="tooling",
                target_name=executable,
                evidence_items=all_items[:12],
                suggested_action=(
                    "Decide whether this is a durable tooling/runbook gap or a transient "
                    "environment issue. For unexpected empty data, add a clear empty-result "
                    "error or contract check instead of consuming it."
                ),
                impact=["shorter", "safer", "more_correct"],
            )
        )

    # MCP tool failures group by server: recurring failures usually mean
    # expired auth, a broken server config, or a tool contract the agent keeps
    # guessing wrong - the same "fix the tool, not the prompt" lesson as CLIs.
    mcp_failures: Dict[str, List[Evidence]] = {}
    mcp_silent_empty: Dict[str, List[Evidence]] = {}
    for session in sessions:
        if not INCLUDE_SUBAGENT_FAILURES and "/subagents/" in str(session.path):
            continue
        for fail in session.failures:
            if not fail.tool_name.startswith("mcp__"):
                continue
            parts = fail.tool_name.split("__")
            server = parts[1] if len(parts) > 1 and parts[1] else fail.tool_name
            mcp_failures.setdefault(server, []).append(fail)
        for empty in session.silent_empty:
            if not empty.tool_name.startswith("mcp__"):
                continue
            parts = empty.tool_name.split("__")
            server = parts[1] if len(parts) > 1 and parts[1] else empty.tool_name
            mcp_silent_empty.setdefault(server, []).append(empty)
    for server in sorted(set(mcp_failures) | set(mcp_silent_empty)):
        failures = mcp_failures.get(server, [])
        empties = mcp_silent_empty.get(server, [])
        all_items = failures + empties
        session_count = len({item.session_id for item in all_items})
        if len(all_items) < 3 or session_count < 2:
            continue
        tools = sorted({item.tool_name for item in all_items})
        shown = ", ".join(tools[:4]) + ("..." if len(tools) > 4 else "")
        signal_bits = []
        if failures:
            signal_bits.append(f"{len(failures)} failed tool call(s)")
        if empties:
            signal_bits.append(f"{len(empties)} swallowed empty result(s)")
        proposals.append(
            make_proposal(
                route="tool",
                title=f"Review mcp:{server} friction from real use",
                summary=(
                    f"MCP server {server}: {', '.join(signal_bits)} across "
                    f"{session_count} session(s) ({shown})."
                ),
                target_kind="mcp_server",
                target_name=f"mcp:{server}",
                evidence_items=all_items[:12],
                suggested_action=(
                    "Review the failed or empty calls. Recurring MCP friction usually means "
                    "expired auth, a broken server config, a swallowed empty response, or a "
                    "tool schema the agent keeps guessing wrong. Fix the server setup or its "
                    "tool contract instead of prompting around it."
                ),
                impact=["shorter", "safer", "more_correct"],
            )
        )

    return dedupe_proposals(proposals)


def first_executable(command: str) -> str:
    if not command:
        return ""
    command = command.strip()
    if not command:
        return ""
    for sep in ("&&", "||", ";", "|", "\n"):
        command = command.split(sep, 1)[0]
    parts = command.strip().split()
    if not parts:
        return ""
    shell_fragments = {
        "#",
        "case",
        "do",
        "done",
        "else",
        "esac",
        "fi",
        "for",
        "function",
        "if",
        "in",
        "then",
        "while",
        "}",
    }
    first = parts[0]
    if (
        first in shell_fragments
        or first.startswith(("#", "}", ")"))
        or first.endswith((")", "}", ";;"))
        or "$(" in first
    ):
        return ""
    if ENV_ASSIGNMENT_RE.match(first):
        return parts[1] if len(parts) > 1 else ""
    if parts[0] in {"env", "command", "time"} and len(parts) > 1:
        return parts[1]
    return Path(parts[0]).name


def backlog_executable(command: str) -> str:
    if not command:
        return ""
    if CODE_COMMAND_RE.match(command):
        # Code-shaped input (current Codex exec calls carry JavaScript): there
        # is no shell executable to blame, so it cannot feed the backlog route.
        return ""
    candidates = re.split(r"\s*(?:&&|\|\||;|\||\n)\s*", command)
    fallback = ""
    for candidate in candidates:
        executable = first_executable(candidate)
        if not executable:
            continue
        if not fallback:
            fallback = executable
        if executable.startswith("-") or executable.startswith("<"):
            continue
        if executable in BACKLOG_IGNORE_EXECUTABLES:
            continue
        return executable
    if fallback in BACKLOG_IGNORE_EXECUTABLES or fallback.startswith("-") or fallback.startswith("<"):
        return ""
    return fallback


def dedupe_proposals(proposals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for proposal in proposals:
        key = proposal["proposal_key"]
        if key in seen:
            continue
        seen.add(key)
        out.append(proposal)
    return out


def load_config(path: Path) -> Dict[str, Any]:
    """Read the optional JSON config file; missing or malformed means defaults."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def apply_config(cfg: Dict[str, Any]) -> None:
    """Apply config overrides to the module-level detector knobs.

    Recognized keys: tracked_cli_suffix, extra_scaffold_markers,
    extra_redaction_patterns ([[regex, replacement], ...]),
    extra_backlog_ignore, extra_remote_command_wrappers,
    include_subagent_failures, detect_silent_empty, silent_empty_fetch_verbs,
    silent_empty_ignore.
    """
    global TRACKED_CLI_SUFFIX, TRACKED_CLI_RE, VALID_TRACKED_CLI_RE
    global INCLUDE_SUBAGENT_FAILURES, DETECT_SILENT_EMPTY
    suffix = cfg.get("tracked_cli_suffix")
    if isinstance(suffix, str) and suffix:
        TRACKED_CLI_SUFFIX = suffix
        TRACKED_CLI_RE, VALID_TRACKED_CLI_RE = _build_tracked_cli_res(suffix)
    markers = cfg.get("extra_scaffold_markers")
    if isinstance(markers, list):
        EXTRA_SCAFFOLD_MARKERS.extend(str(m) for m in markers if m)
    patterns = cfg.get("extra_redaction_patterns")
    if isinstance(patterns, list):
        for entry in patterns:
            if not (isinstance(entry, list) and len(entry) == 2):
                continue
            try:
                SECRET_PATTERNS.append((re.compile(str(entry[0])), str(entry[1])))
            except re.error as exc:
                print(f"warning: bad redaction pattern {entry[0]!r}: {exc}", file=sys.stderr)
    ignore = cfg.get("extra_backlog_ignore")
    if isinstance(ignore, list):
        BACKLOG_IGNORE_EXECUTABLES.update(str(x) for x in ignore)
    wrappers = cfg.get("extra_remote_command_wrappers")
    if isinstance(wrappers, list):
        REMOTE_COMMAND_WRAPPERS.update(str(x) for x in wrappers)
    if isinstance(cfg.get("include_subagent_failures"), bool):
        INCLUDE_SUBAGENT_FAILURES = cfg["include_subagent_failures"]
    if isinstance(cfg.get("detect_silent_empty"), bool):
        DETECT_SILENT_EMPTY = cfg["detect_silent_empty"]
    fetch_verbs = cfg.get("silent_empty_fetch_verbs")
    if isinstance(fetch_verbs, list):
        SILENT_EMPTY_FETCH_VERBS.update(str(x).lower() for x in fetch_verbs if x)
    silent_ignore = cfg.get("silent_empty_ignore")
    if isinstance(silent_ignore, list):
        SILENT_EMPTY_IGNORE_EXECUTABLES.update(str(x) for x in silent_ignore if x)


def parser_health_warnings(stats: Dict[str, Dict[str, int]]) -> List[str]:
    """Flag a source whose transcripts parse but yield no tool calls.

    This is the failure mode that actually bit: a transcript schema change
    (Codex moving to custom_tool_call) left the loop running green while
    reading zero commands. Surface it instead of silently staging nothing.
    """
    warnings = []
    for source, counts in sorted(stats.items()):
        if counts.get("files", 0) >= 5 and counts.get("tool_calls", 0) == 0:
            warnings.append(
                f"{source}: {counts['files']} transcript(s) scanned but 0 tool calls "
                "parsed - the parser may be stale for this source's current format."
            )
    return warnings


def load_state(root: Path) -> Dict[str, Any]:
    path = root / "state.json"
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "seen_proposal_keys": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "seen_proposal_keys": []}
    if not isinstance(state, dict):
        return {"schema_version": SCHEMA_VERSION, "seen_proposal_keys": []}
    state.setdefault("seen_proposal_keys", [])
    return state


def validate_resolution_target(target: Any) -> str:
    value = str(target or "").strip()
    route, separator, name = value.partition(":")
    if not separator or not route or not name:
        raise ValueError("resolution target must be route:target")
    return value


def normalize_resolution(entry: Dict[str, Any]) -> Dict[str, str]:
    decision = str(entry.get("decision") or "").strip().lower()
    if decision not in RESOLUTION_DECISIONS:
        allowed = ", ".join(sorted(RESOLUTION_DECISIONS))
        raise ValueError(f"resolution decision must be one of: {allowed}")
    resolved = parse_time(entry.get("resolved_at"))
    if not resolved:
        raise ValueError("resolution resolved_at must be an ISO8601 timestamp")
    return {
        "decision": decision,
        "resolved_at": isoformat_utc(resolved),
        "pr": str(entry.get("pr") or "").strip(),
        "note": str(entry.get("note") or "").strip(),
        "by": str(entry.get("by") or "").strip(),
    }


def load_resolutions(root: Path) -> Dict[str, Dict[str, str]]:
    path = root / "resolutions.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object keyed by route:target")
    resolutions: Dict[str, Dict[str, str]] = {}
    for target, entry in raw.items():
        target = validate_resolution_target(target)
        if not isinstance(entry, dict):
            raise ValueError(f"resolution for {target} must be a JSON object")
        resolutions[target] = normalize_resolution(entry)
    return resolutions


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_run_id(value: Any) -> Optional[dt.datetime]:
    try:
        parsed = dt.datetime.strptime(str(value), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=dt.timezone.utc)


def trim_target_history(
    state: Dict[str, Any], target: str, resolved_at: Any
) -> None:
    """Remove recurrence runs at or before a target's resolution watermark."""
    watermark = parse_time(resolved_at)
    if not watermark:
        return
    history = state.get("target_run_history")
    if not isinstance(history, dict) or target not in history:
        return
    history[target] = [
        run_id
        for run_id in (history.get(target) or [])
        if (run_time := parse_run_id(run_id)) is not None and run_time > watermark
    ]
    if not history[target]:
        history.pop(target, None)


def update_resolutions(
    root: Path, updates: Dict[str, Dict[str, Any]]
) -> Dict[str, Dict[str, str]]:
    resolutions = load_resolutions(root)
    normalized: Dict[str, Dict[str, str]] = {}
    for target, entry in updates.items():
        target = validate_resolution_target(target)
        normalized[target] = normalize_resolution(entry)
    resolutions.update(normalized)
    write_json(root / "resolutions.json", resolutions)

    # A corrupt operational state must never erase or block the separate
    # resolutions registry. Trim recurrence history only when state is valid.
    state_path = root / "state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            state = None
        if isinstance(state, dict):
            for target, entry in normalized.items():
                trim_target_history(state, target, entry["resolved_at"])
            write_json(state_path, state)
    return resolutions


def remove_resolution(root: Path, target: str) -> Dict[str, Dict[str, str]]:
    target = validate_resolution_target(target)
    resolutions = load_resolutions(root)
    if target not in resolutions:
        raise ValueError(f"no resolution recorded for {target}")
    del resolutions[target]
    write_json(root / "resolutions.json", resolutions)
    return resolutions


def load_decision_import(path: Path, default_by: str = "") -> Dict[str, Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        raise ValueError("decisions import must be an object with a decisions array")
    updates: Dict[str, Dict[str, Any]] = {}
    for index, row in enumerate(payload["decisions"], 1):
        if not isinstance(row, dict):
            raise ValueError(f"decisions[{index}] must be a JSON object")
        target = validate_resolution_target(row.get("target") or row.get("route_target"))
        if target in updates:
            raise ValueError(f"duplicate decisions entry for {target}")
        updates[target] = {
            "decision": row.get("decision"),
            "resolved_at": row.get("resolved_at") or utc_now(),
            "pr": row.get("pr") or "",
            "note": row.get("note") or "",
            "by": row.get("by") or default_by,
        }
    return updates


def append_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def append_jsonl_dedup(
    path: Path, rows: List[Dict[str, Any]], key_fields: Tuple[str, ...] = ("path", "ended_at")
) -> int:
    """Append only rows whose key is not already present.

    Overlapping scan windows re-index the same sessions run after run; without
    this the session index grows with duplicate rows forever. A session that
    gained new activity has a new ``ended_at`` and is appended again.
    """
    existing = set()
    if path.exists():
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    existing.add(tuple(str(rec.get(k, "")) for k in key_fields))
    fresh = [
        row for row in rows if tuple(str(row.get(k, "")) for k in key_fields) not in existing
    ]
    append_jsonl(path, fresh)
    return len(fresh)


def write_review_packet(
    root: Path,
    run_id: str,
    sessions: List[SessionSummary],
    proposals: List[Dict[str, Any]],
    parser_warnings: Optional[List[str]] = None,
    resolution_suppressed: Optional[List[Dict[str, Any]]] = None,
    regressions: Optional[List[Dict[str, Any]]] = None,
    fleet_sources: Optional[List[Dict[str, Any]]] = None,
) -> Path:
    path = root / "review-packets" / f"{run_id}.md"
    resolution_suppressed = resolution_suppressed or []
    regressions = regressions or []
    fleet_sources = fleet_sources or []
    session_count = (
        sum(int(item.get("sessions_with_signals", 0)) for item in fleet_sources)
        if fleet_sources
        else len(sessions)
    )
    machines = sorted(
        {str(item.get("machine")) for item in fleet_sources if item.get("machine")}
        or {session.machine for session in sessions if session.machine}
    )
    suppressed_targets = sorted({item["target"] for item in resolution_suppressed})
    target_text = ", ".join(f"`{target}`" for target in suppressed_targets) or "none"
    lines = [
        f"# Daily Improvement Review Packet ({run_id})",
        "",
        (
            "This packet contains FULL, unredacted excerpts (run with --full). Do not commit or share it as-is."
            if FULL_DETAIL
            else "This packet is safe to hand to an agent for review. It contains redacted excerpts and evidence references, not full transcript dumps."
        ),
        "",
        "## Rules",
        "",
        "- Do not apply changes without approval.",
        "- Distinguish durable lessons from one-off incidents.",
        "- Prefer patching existing umbrella skills over creating narrow skills.",
        "- Route tool and CLI changes through your own CLI fix workflow when real CLI use is the evidence.",
        "- Do not save transient environment failures as permanent rules.",
        "",
        "## Summary",
        "",
        f"- Machines: {', '.join(machines) or 'unknown'}",
        f"- Sessions with signals: {session_count}",
        f"- Proposals staged this run: {len(proposals)}",
        (
            f"- {len(resolution_suppressed)} proposals suppressed as already-resolved "
            f"(targets: {target_text})"
        ),
        f"- Regressions re-opened after a fix: {len(regressions)}",
        "",
    ]
    if parser_warnings:
        lines[-1:-1] = [f"- PARSER WARNING: {w}" for w in parser_warnings]
    lines.extend(["## Regressions re-opened after a fix", ""])
    if not regressions:
        lines.append("None.")
    for proposal in regressions:
        regression = proposal["regression"]
        lines.append(
            f"- `{proposal['proposal_id']}` `{regression['target']}`: evidence at "
            f"{regression['latest_evidence_at']} is newer than fix "
            f"{regression['pr'] or '(no PR recorded)'} at {regression['fixed_at']}."
        )
    lines.extend(["", "## Proposals", ""])
    if not proposals:
        lines.append("No proposals met the deterministic threshold.")
    for proposal in proposals:
        lines.extend(
            [
                f"### {proposal['proposal_id']} - {proposal['title']}",
                "",
                f"- Route: `{proposal['route']}`",
                f"- Target: `{proposal['target']['kind']}:{proposal['target']['name']}`",
                f"- Summary: {proposal['summary']}",
                f"- Suggested action: {proposal['suggested_action']}",
                "- Evidence:",
            ]
        )
        for ev in proposal["evidence"][:8]:
            loc = f"{ev['path']}:{ev['line']}"
            cmd = f" command=`{ev['command']}`" if ev.get("command") else ""
            machine = f" [{ev['machine']}]" if ev.get("machine") else ""
            lines.append(f"  - `{ev['kind']}`{machine} {loc}{cmd} - {ev['excerpt']}")
        lines.append("")

    if fleet_sources:
        lines.extend(["## Fleet Sources", ""])
        for item in fleet_sources:
            lines.append(
                f"- `{item.get('machine', 'unknown')}` run=`{item.get('run_id', '')}` "
                f"sessions={item.get('sessions_with_signals', 0)} "
                f"proposals={item.get('proposal_count', 0)}"
            )
        lines.append("")

    lines.extend(["## Session Index", ""])
    for session in sessions[:200]:
        data = session.as_dict()
        lines.append(
            f"- `{data['machine'] or 'unknown'}` `{data['source']}` `{data['session_id']}` "
            f"tools={data['tool_call_count']} tracked={','.join(data['tracked_cli_names']) or '-'} "
            f"skills={','.join(data['skill_names']) or '-'} failures={data['failure_count']} "
            f"corrections={data['correction_count']}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def write_fleet_bundle(
    root: Path,
    run_id: str,
    machine: str,
    result: Dict[str, Any],
    proposals: List[Dict[str, Any]],
) -> Optional[Path]:
    """Write a redacted, self-contained handoff without copying transcripts."""
    if FULL_DETAIL:
        print("warning: --full disables fleet bundle output", file=sys.stderr)
        return None
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "bundle_kind": "agent_improvement_redacted_proposals",
        "redacted": True,
        "machine": machine,
        "run_id": run_id,
        "started_at": result.get("started_at"),
        "files_scanned": result.get("files_scanned", 0),
        "sessions_with_signals": result.get("sessions_with_signals", 0),
        "proposal_count": len(proposals),
        "parser_warnings": redact_structure_for_fleet(result.get("parser_warnings", [])),
        "proposals": redact_structure_for_fleet(proposals),
    }
    path = root / "fleet-outbox" / machine / f"{run_id}.json"
    write_json(path, bundle)
    return path


def collect_fleet_bundles(args: argparse.Namespace) -> int:
    """Merge the latest redacted bundle per Mac into one orchestrator packet."""
    root = Path(args.output_root).expanduser()
    inbox = Path(args.fleet_inbox).expanduser()
    latest_by_machine: Dict[str, Dict[str, Any]] = {}
    for path in sorted(inbox.glob("*/*.json")):
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warning: skipped invalid fleet bundle {path}: {exc}", file=sys.stderr)
            continue
        if not isinstance(bundle, dict) or bundle.get("redacted") is not True:
            print(f"warning: skipped non-redacted fleet bundle {path}", file=sys.stderr)
            continue
        # Treat peer bundles as untrusted input. Re-sanitize every string even
        # when the producer marked the bundle redacted so an older scanner or
        # a missed evidence field cannot leak credentials into the fleet packet.
        bundle = redact_structure_for_fleet(bundle)
        machine = normalized_machine_name(str(bundle.get("machine") or path.parent.name))
        run_id = str(bundle.get("run_id") or "")
        if not run_id:
            continue
        current = latest_by_machine.get(machine)
        if current is None or run_id > str(current.get("run_id") or ""):
            latest_by_machine[machine] = bundle

    if not latest_by_machine:
        print(f"error: no valid redacted fleet bundles under {inbox}", file=sys.stderr)
        return 2

    fleet_sources: List[Dict[str, Any]] = []
    proposals: List[Dict[str, Any]] = []
    parser_warnings: List[str] = []
    for machine, bundle in sorted(latest_by_machine.items()):
        bundle_proposals = bundle.get("proposals")
        if not isinstance(bundle_proposals, list):
            bundle_proposals = []
        fleet_sources.append(
            {
                "machine": machine,
                "run_id": str(bundle.get("run_id") or ""),
                "sessions_with_signals": int(bundle.get("sessions_with_signals") or 0),
                "proposal_count": len(bundle_proposals),
            }
        )
        parser_warnings.extend(
            f"{machine}: {warning}" for warning in bundle.get("parser_warnings", [])
        )
        for original in bundle_proposals:
            if not isinstance(original, dict):
                continue
            proposal = copy.deepcopy(original)
            origin_id = str(proposal.get("proposal_id") or "")
            proposal["origin_proposal_id"] = origin_id
            proposal["proposal_id"] = f"{machine}-{origin_id}"
            proposal["proposal_key"] = hashlib.sha256(
                f"{machine}:{proposal.get('proposal_key', origin_id)}".encode()
            ).hexdigest()[:20]
            proposal["machines"] = [machine]
            for item in proposal.get("evidence", []):
                if isinstance(item, dict):
                    item["machine"] = machine
            proposals.append(proposal)

    fleet_run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-fleet")
    proposal_dir = root / "proposals" / fleet_run_id
    for proposal in proposals:
        write_json(proposal_dir / f"{proposal['proposal_id']}.json", proposal)
    packet_path = write_review_packet(
        root,
        fleet_run_id,
        [],
        proposals,
        parser_warnings,
        fleet_sources=fleet_sources,
    )
    run_result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": fleet_run_id,
        "started_at": utc_now(),
        "source": "fleet_redacted_bundles",
        "machines": [item["machine"] for item in fleet_sources],
        "sessions_with_signals": sum(
            item["sessions_with_signals"] for item in fleet_sources
        ),
        "proposal_count": len(proposals),
        "fleet_sources": fleet_sources,
        "review_packet": str(packet_path),
    }
    write_json(root / "runs" / f"{fleet_run_id}.json", run_result)
    print(
        f"fleet_machines={','.join(run_result['machines'])} "
        f"sessions_with_signals={run_result['sessions_with_signals']} "
        f"proposals={len(proposals)}"
    )
    print(f"review_packet={packet_path}")
    return 0


TARGET_HISTORY_KEEP = 10


def proposal_target_key(proposal: Dict[str, Any]) -> str:
    return f"{proposal['route']}:{proposal['target']['name']}"


def annotate_recurrence(
    proposals: List[Dict[str, Any]],
    state: Dict[str, Any],
    resolutions: Optional[Dict[str, Dict[str, str]]] = None,
) -> None:
    """Mark proposals whose target was already flagged in previous runs.

    A target that keeps coming back is the strongest reviewable signal this
    loop produces, so recurring proposals are annotated and sorted to the top
    of the packet. Recurrence is counted per route:target across runs, not per
    evidence line, so fresh evidence for an old problem still reads as "still
    broken", not "new problem".
    """
    history = state.get("target_run_history") or {}
    resolutions = resolutions or {}
    for proposal in proposals:
        key = proposal_target_key(proposal)
        runs = history.get(key) or []
        resolution = resolutions.get(key)
        watermark = parse_time(resolution.get("resolved_at")) if resolution else None
        if watermark:
            runs = [
                run_id
                for run_id in runs
                if (run_time := parse_run_id(run_id)) is not None and run_time > watermark
            ]
        prior = len(runs)
        if prior:
            proposal["recurrence"] = prior + 1
            proposal["summary"] += f" Recurring: also flagged in {prior} previous run(s)."
    proposals.sort(
        key=lambda p: (-(p.get("recurrence") or 1), -len(p.get("evidence") or []))
    )


def update_target_history(
    state: Dict[str, Any], proposals: List[Dict[str, Any]], run_id: str
) -> None:
    """Record which run detected each route:target, keeping a bounded window."""
    history = state.setdefault("target_run_history", {})
    for proposal in proposals:
        key = proposal_target_key(proposal)
        runs = history.setdefault(key, [])
        if run_id not in runs:
            runs.append(run_id)
        del runs[:-TARGET_HISTORY_KEEP]


@dataclass
class ProposalFilterResult:
    proposals: List[Dict[str, Any]]
    suppressed: List[Dict[str, Any]]
    regressions: List[Dict[str, Any]]
    resolved_nonregressions: List[Dict[str, Any]]


def filter_new_proposals(
    proposals: List[Dict[str, Any]],
    state: Dict[str, Any],
    include_seen: bool,
    resolutions: Optional[Dict[str, Dict[str, str]]] = None,
    include_resolved: bool = False,
) -> ProposalFilterResult:
    """Apply durable resolutions first, then ordinary seen-key deduplication."""
    resolutions = resolutions or {}
    seen = set(state.get("seen_proposal_keys") or [])
    emitted: List[Dict[str, Any]] = []
    suppressed: List[Dict[str, Any]] = []
    regressions: List[Dict[str, Any]] = []
    resolved_nonregressions: List[Dict[str, Any]] = []

    for proposal in proposals:
        target = proposal_target_key(proposal)
        resolution = resolutions.get(target)
        already_resolved = False
        is_regression = False
        if resolution:
            decision = resolution["decision"]
            if decision == "fixed":
                latest = parse_time(proposal.get("latest_evidence_at"))
                watermark = parse_time(resolution.get("resolved_at"))
                is_regression = bool(latest and watermark and latest > watermark)
                already_resolved = not is_regression
            else:
                already_resolved = True

            proposal["resolution"] = dict(resolution)
            if is_regression:
                pr = resolution.get("pr") or "recorded fix"
                marker = f"Regression after {pr} (fixed {resolution['resolved_at']})"
                proposal["summary"] = f"{proposal['summary']} {marker}."
                proposal["regression"] = {
                    "target": target,
                    "pr": resolution.get("pr", ""),
                    "fixed_at": resolution["resolved_at"],
                    "latest_evidence_at": proposal["latest_evidence_at"],
                }
                regressions.append(proposal)
            elif already_resolved:
                resolved_nonregressions.append(proposal)
                if not include_resolved:
                    suppressed.append(
                        {
                            "proposal_id": proposal["proposal_id"],
                            "target": target,
                            "decision": decision,
                            "resolved_at": resolution["resolved_at"],
                            "pr": resolution.get("pr", ""),
                        }
                    )
                    continue
                proposal["included_resolved"] = True

        if not include_seen and proposal.get("proposal_key") in seen:
            continue
        emitted.append(proposal)

    return ProposalFilterResult(emitted, suppressed, regressions, resolved_nonregressions)


def proposals_for_fleet_snapshot(
    detected: List[Dict[str, Any]],
    state: Dict[str, Any],
    resolutions: Optional[Dict[str, Dict[str, str]]] = None,
    include_resolved: bool = False,
) -> List[Dict[str, Any]]:
    """Return all currently active proposals for a latest-wins fleet bundle.

    Local packets remain delta-oriented, but collectors replace each machine's
    prior bundle with its newest one. The bundle therefore must be a snapshot
    of active unresolved proposals rather than only newly-seen proposal keys.
    """
    candidates = copy.deepcopy(detected)
    return filter_new_proposals(
        candidates,
        state,
        include_seen=True,
        resolutions=resolutions,
        include_resolved=include_resolved,
    ).proposals


def compute_since(args: argparse.Namespace, state: Dict[str, Any]) -> Optional[dt.datetime]:
    if args.all:
        return None
    if args.since_days is not None:
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=float(args.since_days))
    last = parse_time(state.get("last_scan_started_at"))
    if last:
        return last
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)


def scan(args: argparse.Namespace) -> int:
    global FULL_DETAIL
    FULL_DETAIL = bool(getattr(args, "full", False))
    home = Path(args.home).expanduser()
    homes = [home] + [Path(item).expanduser() for item in getattr(args, "extra_home", [])]
    machine = normalized_machine_name(getattr(args, "machine", ""))
    root = Path(args.output_root).expanduser()
    config_path = Path(getattr(args, "config", "") or DEFAULT_CONFIG_PATH).expanduser()
    apply_config(load_config(config_path))
    state = load_state(root)
    try:
        resolutions = load_resolutions(root)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    since = compute_since(args, state)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    files: List[Tuple[str, Path]] = discover_session_files(homes, args.source)
    files = [(source, p) for source, p in files if session_in_window(p, since)]
    files.sort(key=lambda item: item[1].stat().st_mtime)
    if args.max_sessions:
        files = files[-args.max_sessions :]

    sessions: List[SessionSummary] = []
    parse_stats: Dict[str, Dict[str, int]] = {}
    for source, path in files:
        try:
            parsed = parse_claude_session(path) if source == "claude" else parse_codex_session(path)
        except Exception as exc:
            print(f"warning: failed to parse {path}: {exc}", file=sys.stderr)
            continue
        stats = parse_stats.setdefault(source, {"files": 0, "tool_calls": 0})
        stats["files"] += 1
        stats["tool_calls"] += len(parsed.tool_calls)
        if parsed.has_signal():
            stamp_session_machine(parsed, machine)
            sessions.append(parsed)

    hermes_db_count = 0
    if args.source in {"all", "hermes_profile_log"}:
        seen_dbs: set[Path] = set()
        for scan_home in homes:
            for db_path in discover_hermes_profile_dbs(scan_home):
                resolved = db_path.resolve()
                if resolved in seen_dbs:
                    continue
                seen_dbs.add(resolved)
                hermes_db_count += 1
                try:
                    parsed_sessions = parse_hermes_profile_db(
                        db_path,
                        home=scan_home,
                        since=since,
                        max_sessions=(
                            args.max_sessions if args.source == "hermes_profile_log" else 0
                        ),
                    )
                except Exception as exc:
                    print(f"warning: failed to parse Hermes profile DB {db_path}: {exc}", file=sys.stderr)
                    continue
                stats = parse_stats.setdefault(
                    "hermes_profile_log", {"files": 0, "tool_calls": 0}
                )
                stats["files"] += 1
                stats["tool_calls"] += sum(len(item.tool_calls) for item in parsed_sessions)
                for item in parsed_sessions:
                    stamp_session_machine(item, machine)
                sessions.extend(parsed_sessions)

    if args.max_sessions:
        sessions = sorted(
            sessions, key=lambda item: item.ended_at or item.started_at
        )[-args.max_sessions :]
    parser_warnings = parser_health_warnings(parse_stats)
    for warning in parser_warnings:
        print(f"warning: {warning}", file=sys.stderr)

    detected = generate_proposals(sessions)
    annotate_recurrence(detected, state, resolutions)
    fleet_proposals = proposals_for_fleet_snapshot(
        detected,
        state,
        resolutions,
        args.include_resolved,
    )
    filtered = filter_new_proposals(
        detected,
        state,
        args.include_seen,
        resolutions,
        args.include_resolved,
    )
    proposals = filtered.proposals
    suppressed_targets = sorted({item["target"] for item in filtered.suppressed})
    regression_rows = [
        {"proposal_id": proposal["proposal_id"], **proposal["regression"]}
        for proposal in filtered.regressions
    ]

    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": utc_now(),
        "source": args.source,
        "machine": machine,
        "since": since.isoformat() if since else None,
        "files_scanned": len(files) + hermes_db_count,
        "sessions_with_signals": len(sessions),
        "proposal_count": len(proposals),
        "fleet_proposal_count": len(fleet_proposals),
        "resolved_suppressed_count": len(filtered.suppressed),
        "resolved_suppressed_targets": suppressed_targets,
        "regression_count": len(filtered.regressions),
        "regressions": regression_rows,
        "parse_stats": parse_stats,
        "parser_warnings": parser_warnings,
        "output_root": str(root),
    }

    if args.dry_run:
        print(json.dumps({**result, "proposals": proposals}, ensure_ascii=False, indent=2))
        return 0

    session_rows = [s.as_dict() for s in sessions]
    append_jsonl_dedup(root / "session-index.jsonl", session_rows)

    proposal_dir = root / "proposals" / run_id
    for proposal in proposals:
        write_json(proposal_dir / f"{proposal['proposal_id']}.json", proposal)

    packet_path = write_review_packet(
        root,
        run_id,
        sessions,
        proposals,
        parser_warnings,
        filtered.suppressed,
        filtered.regressions,
    )
    state["schema_version"] = SCHEMA_VERSION
    resolved_ids = {id(proposal) for proposal in filtered.resolved_nonregressions}
    update_target_history(
        state,
        [proposal for proposal in detected if id(proposal) not in resolved_ids],
        run_id,
    )
    state["last_scan_started_at"] = result["started_at"]
    state["last_run_id"] = run_id
    seen = set(state.get("seen_proposal_keys") or [])
    seen.update(p["proposal_key"] for p in proposals)
    state["seen_proposal_keys"] = sorted(seen)
    write_json(root / "state.json", state)
    write_json(root / "runs" / f"{run_id}.json", {**result, "review_packet": str(packet_path)})
    bundle_path = write_fleet_bundle(root, run_id, machine, result, fleet_proposals)

    print(
        f"machine={machine} scanned={result['files_scanned']} "
        f"sessions_with_signals={len(sessions)} proposals={len(proposals)} "
        f"fleet_proposals={len(fleet_proposals)}"
    )
    print(
        f"resolved_suppressed={len(filtered.suppressed)} "
        f"targets={','.join(suppressed_targets) or '-'} regressions={len(filtered.regressions)}"
    )
    print(f"review_packet={packet_path}")
    if bundle_path:
        print(f"fleet_bundle={bundle_path}")
    if proposals:
        print(f"proposal_dir={proposal_dir}")
    return 0


def manage_resolutions(args: argparse.Namespace) -> int:
    root = Path(args.output_root).expanduser()
    actor = str(args.by or os.environ.get("USER") or "unknown")
    try:
        if args.collect_fleet:
            return collect_fleet_bundles(args)
        if args.list_resolutions:
            print(json.dumps(load_resolutions(root), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.unresolve:
            remove_resolution(root, args.unresolve)
            print(f"unresolved={validate_resolution_target(args.unresolve)}")
            return 0
        if args.resolve_from:
            updates = load_decision_import(Path(args.resolve_from).expanduser(), actor)
            update_resolutions(root, updates)
            print(f"resolutions_imported={len(updates)} targets={','.join(sorted(updates))}")
            return 0
        if args.resolve:
            if not args.decision:
                raise ValueError("--resolve requires --decision")
            resolved_at = args.resolved_at or utc_now()
            target = validate_resolution_target(args.resolve)
            update_resolutions(
                root,
                {
                    target: {
                        "decision": args.decision,
                        "resolved_at": resolved_at,
                        "pr": args.pr or "",
                        "note": args.note or "",
                        "by": actor,
                    }
                },
            )
            print(f"resolved={target} decision={args.decision}")
            return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return scan(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan local agent sessions and stage self-improvement proposals."
    )
    parser.add_argument("--home", default=str(Path.home()), help="Home directory containing .claude/.codex")
    parser.add_argument(
        "--extra-home",
        action="append",
        default=[],
        help="Additional home directory containing .claude/.codex logs, e.g. logs copied from another machine over ssh",
    )
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Proposal queue root")
    parser.add_argument(
        "--source",
        choices=["all", "claude", "codex", "hermes_profile_log"],
        default="all",
    )
    parser.add_argument(
        "--machine",
        default="",
        help="Stable fleet machine name embedded in sessions, proposals, and redacted bundles",
    )
    parser.add_argument("--all", action="store_true", help="Backfill all discovered sessions")
    parser.add_argument("--since-days", type=float, default=None, help="Scan sessions modified within N days")
    parser.add_argument("--max-sessions", type=int, default=0, help="Limit to most recent N sessions after filtering")
    parser.add_argument("--include-seen", action="store_true", help="Emit proposals even if their keys were seen before")
    parser.add_argument(
        "--include-resolved",
        action="store_true",
        help="Debug bypass: emit already-resolved proposals (seen-key filtering still applies)",
    )
    parser.add_argument("--full", action="store_true", help="Keep full, unredacted excerpts inline (local use only; do not share the output)")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=(
            "JSON config overriding detector defaults (tracked_cli_suffix, "
            "extra_scaffold_markers, extra_redaction_patterns, extra_backlog_ignore, "
            "extra_remote_command_wrappers, include_subagent_failures, "
            "detect_silent_empty, silent_empty_fetch_verbs, silent_empty_ignore)"
        ),
    )
    parser.add_argument(
        "--fleet-inbox",
        default=str(DEFAULT_OUTPUT_ROOT / "fleet-inbox"),
        help="Root containing <machine>/<run-id>.json redacted fleet bundles",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print JSON and do not write queue files")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--resolve", metavar="ROUTE:TARGET", help="Record a target resolution")
    actions.add_argument("--resolve-from", metavar="PATH", help="Import structured decisions JSON")
    actions.add_argument("--list-resolutions", action="store_true", help="Print the resolutions registry")
    actions.add_argument("--unresolve", metavar="ROUTE:TARGET", help="Remove a target resolution")
    actions.add_argument(
        "--collect-fleet",
        action="store_true",
        help="Merge the latest redacted bundle per machine into one fleet review packet",
    )
    parser.add_argument("--decision", choices=sorted(RESOLUTION_DECISIONS), help="Resolution decision")
    parser.add_argument("--resolved-at", help="Resolution watermark (ISO8601 UTC; default now)")
    parser.add_argument("--pr", help="Fix PR number or URL")
    parser.add_argument("--note", help="Human-readable resolution note")
    parser.add_argument("--by", help="Person or agent recording the resolution (default current user)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return manage_resolutions(args)


if __name__ == "__main__":
    raise SystemExit(main())
