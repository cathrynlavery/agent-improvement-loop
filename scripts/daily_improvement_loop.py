#!/usr/bin/env python3
"""Mine local Claude/Codex sessions for self-improvement candidates.

The command is intentionally conservative: it scans transcripts, writes a
proposal queue and a compact review packet, and never applies changes to skills,
memory, runbooks, source code.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = 1
CONTENT_PRIVACY_NOTICE = (
    "This tool reads local agent transcripts. For content_idea output, treat sessions as "
    "private source material: mine personal context locally, but remove names, messages, "
    "customer/client details, family details, secrets, and exact sensitive data before publishing."
)
DEFAULT_OUTPUT_ROOT = Path.home() / ".agent-improvement"

# When True (via --full), keep full, unredacted excerpts inline. Default masks
# secrets and shortens excerpts so the written output is safe to commit, sync,
# paste into a writeup, or hand to another agent.
FULL_DETAIL = False
FULL_EXCERPT_LIMIT = 4000

PP_CLI_RE = re.compile(
    r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9._-]*-pp-cli)(?=$|[\s;&|)])"
)
# Anchored form, used to reject malformed names the tokenizer can pick up from
# shell quoting or transcript scaffolding (e.g. "'wavespeed-pp-cli", "$c-pp-cli",
# "===x-twitter-pp-cli") before they ever become a proposal.
VALID_PP_CLI_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*-pp-cli$")
ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*")
SHELL_SEPARATORS = {";", "&&", "||", "|", "do", "then", "else"}
COMMAND_PREFIXES = {"command", "env", "noglob", "time"}
REMOTE_COMMAND_WRAPPERS = {"bash", "kssh", "kssh_once", "sh", "ssh", "zsh"}
SLASH_COMMAND_RE = re.compile(r"(?m)^\s*/([a-z][A-Za-z0-9:_-]*)\b")
CORRECTION_RE = re.compile(
    r"\b("
    r"actually|no,? that'?s wrong|that's wrong|that is wrong|not what i asked|"
    r"you missed|don't do|do not|never do|stop doing|instead|should have|"
    r"why did you|i meant|remember this"
    r")\b",
    re.IGNORECASE,
)
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
PP_FRICTION_RE = re.compile(
    r"(?i)\b("
    r"FAIL|not configured|missing required|unknown option|usage:|"
    r"not found|invalid|unauthorized|forbidden|rate limit|silent null"
    r")\b"
)
PP_STRONG_FRICTION_RE = re.compile(
    r"(?i)("
    r"\bFAIL\b|\bnot configured\b|\bmissing required\b|"
    r"\bunknown (option|flag)\b|\binvalid (option|flag|argument)\b|"
    r"\bunauthorized\b|\bforbidden\b|\brate limit\b|\bsilent null\b|"
    r"\baccepts at most\b|\bunexpected extra arg\b|error:|\bnot found\b"
    r")"
)
HELP_COMMAND_RE = re.compile(r"(^|\s)(--help|-h)($|\s)")
TOOLING_FRICTION_RE = re.compile(
    r"(?i)\b("
    r"unknown option|invalid option|usage:|command not found|no such file|"
    r"permission denied|missing required|required option|must specify|"
    r"not found|unsupported"
    r")\b"
)
# "Stuck"/hang signals: the CLI did not cleanly fail, it stalled, timed out, or
# was canceled. This is friction even without a non-zero exit, and is a common
# printing-press CLI smell the failure regex alone would miss.
HANG_RE = re.compile(
    r"(?i)("
    r"timed out|timeout|deadline exceeded|context deadline|operation canceled|"
    r"operation cancelled|still running|appears stuck|took too long|"
    r"killed|sigterm|sigkill"
    r")"
)
# A *-pp-cli invoked at least this many times in a single session is treated as
# retry-before-success friction (the agent guessing syntax), even with no error.
RETRY_STUCK_THRESHOLD = 3
CONTENT_CLI_IGNORE = {
    "",
    "#",
    "bash",
    "cat",
    "cd",
    "curl",
    "echo",
    "env",
    "export",
    "find",
    "grep",
    "jq",
    "kill",
    "ls",
    "ps",
    "sh",
    "sleep",
    "source",
    "which",
    "zsh",
}
CONTENT_SLASH_COMMANDS = {
    "investigate": {
        "title": "How I use /investigate to debug agents before fixing code",
        "content_type": "how_to",
        "query": "coding agents root cause debugging slash commands Claude Code Codex",
        "audience": ["agent builders", "engineering leads", "Claude Code and Codex users"],
    },
    "gstack": {
        "title": "How I use /gstack for browser QA with agents",
        "content_type": "tutorial",
        "query": "agent browser QA visual testing gstack coding agents",
        "audience": ["AI app builders", "frontend engineers", "agent operators"],
    },
    "last30days": {
        "title": "How I check what people actually care about before writing or building",
        "content_type": "case_study",
        "query": "social listening last 30 days Reddit X YouTube research agents",
        "audience": ["founders", "content operators", "agent builders"],
    },
}
PRIVATE_CONTENT_RE = re.compile(
    r"(?i)\b(imessage|sms|text messages?|message threads?|contacts?|phone|family|"
    r"client|customer|health|medical|daycare|school|calendar|gmail|email|inbox)\b"
)
BUILD_CONTENT_RE = re.compile(
    r"(?i)\b(sqlite|database|search|crm|import_|export_|index|transcript|agent|"
    r"workflow|skill|slash command|task ledger|hermes|codex|claude code)\b"
)
CONTENT_WORKFLOW_PATTERNS = [
    {
        "name": "task_ledger",
        "regex": re.compile(r"(?i)(TASK_LEDGER_API_URL|ISSUE_TRACKER_API_URL|/api/issues/|task-ledger-update\.sh|/checkout\b)"),
        "min_signals": 2,
        "title": "How I turn agent work into a task ledger instead of chat chaos",
        "content_type": "case_study",
        "query": "AI agents task ledger issue tracker agent operations workflows",
        "audience": ["founders", "agent operators", "engineering managers"],
        "why": [
            "It shows the operational layer that makes many agents manageable.",
            "It is a concrete antidote to agents disappearing into chat logs.",
        ],
        "outline": [
            "The problem: agent work disappears when it only lives in chat",
            "The task-ledger pattern: checkout, heartbeat, update, close",
            "How to make agent work auditable without micromanaging it",
            "What belongs in the ledger vs. what stays in private transcripts",
            "How readers can copy the loop with their own tracker",
        ],
        "recommendation": "write_now",
        "confidence": 0.78,
    },
    {
        "name": "executive_assistant_sweep",
        "regex": re.compile(r"(?i)(gog gmail|gmail search|gmail thread|calendar events|calendar list|inbox sweep)"),
        "min_signals": 2,
        "title": "How I use agents as an executive assistant without handing them my whole life",
        "content_type": "how_to",
        "query": "AI executive assistant inbox calendar triage agents privacy workflow",
        "audience": ["founders", "operators", "busy parents building with AI"],
        "why": [
            "It is a real workflow with high reader pull: inbox and calendar triage without generic assistant fluff.",
            "It has a useful privacy angle because the public version must teach boundaries, not expose details.",
        ],
        "outline": [
            "The problem: the inbox and calendar are context, not just notifications",
            "The sweep loop: collect, classify, escalate, draft, close",
            "The privacy boundary: what the agent can inspect vs. what it can share",
            "How to make the output decision-ready instead of noisy",
            "A copyable version using synthetic examples",
        ],
        "recommendation": "needs_context",
        "confidence": 0.74,
    },
    {
        "name": "revenue_watch",
        "regex": re.compile(r"(?i)(revenue-pp-cli|metric-aggregates|flow-decay|campaign-values-report|flow-values-report|revenue watch)"),
        "min_signals": 3,
        "title": "How to make agents run a daily revenue watch instead of checking dashboards",
        "content_type": "tutorial",
        "query": "AI agents daily revenue monitoring dashboard automation ecommerce",
        "audience": ["ecommerce founders", "growth operators", "agent builders"],
        "why": [
            "It turns dashboard checking into an agent-run operating cadence.",
            "It is useful even when the exact business metrics stay private.",
        ],
        "outline": [
            "The problem: dashboards are passive and easy to ignore",
            "The watch loop: pull metrics, compare decay, flag anomalies, write the brief",
            "What the agent should calculate vs. what a human decides",
            "How to anonymize the data for a public walkthrough",
            "A lightweight implementation path",
        ],
        "recommendation": "needs_context",
        "confidence": 0.76,
    },
]
# Where printing-press CLI source trees live, so a tool proposal can point at the
# actual CLI to fix (and the amend/reprint workflow), not just name it.
PRINTING_PRESS_ROOT_DEFAULT = "~/printing-press"
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
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"), "<redacted-github-token>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "<redacted-github-token>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b"), "<redacted-jwt>"),
    (re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}(?!\w)"), "<phone>"),
    (re.compile(r"\b[A-Za-z0-9_+/=-]{56,}\b"), "<redacted-long-token>"),
]


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

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "path": self.path,
            "line": self.line,
            "session_id": self.session_id,
            "kind": self.kind,
            "tool_name": self.tool_name,
            "command": self.command,
            "excerpt": self.excerpt,
        }


@dataclass
class ToolCall:
    call_id: str
    name: str
    line: int
    command: str = ""
    skill: str = ""


@dataclass
class SessionSummary:
    source: str
    path: Path
    session_id: str
    cwd: str = ""
    started_at: str = ""
    ended_at: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    pp_cli_invocations: Dict[str, List[Evidence]] = field(default_factory=dict)
    skill_invocations: Dict[str, List[Evidence]] = field(default_factory=dict)
    slash_commands: Dict[str, List[Evidence]] = field(default_factory=dict)
    failures: List[Evidence] = field(default_factory=list)
    corrections: List[Evidence] = field(default_factory=list)

    def has_signal(self) -> bool:
        return bool(
            self.pp_cli_invocations
            or self.skill_invocations
            or self.failures
            or self.corrections
            or self.slash_commands
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "path": str(self.path),
            "session_id": self.session_id,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "tool_call_count": len(self.tool_calls),
            "pp_cli_names": sorted(self.pp_cli_invocations),
            "skill_names": sorted(self.skill_invocations),
            "slash_commands": sorted(self.slash_commands),
            "failure_count": len(self.failures),
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
    )


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
    return any(
        marker in lower
        for marker in (
            "<local-command-caveat>",
            "<permissions instructions>",
            "# agents.md instructions",
            "<instructions>",
            "<codex_internal_context",
            "<collaboration_mode>",
            "compound codex tool mapping",
            "<task-notification>",
            "this session is being continued from a previous conversation",
            "codex could not read the local image at",
            "a 16:9 horizontal editorial illustration that explains one idea:",
            "base directory for this skill:",
            "tool mapping:",
            "filesystem sandboxing defines which files can be read or written",
        )
    )


def is_user_correction_text(text: str, path: Optional[Path] = None) -> bool:
    return bool(text and not is_transcript_scaffold(text, path) and CORRECTION_RE.search(text))


def add_pp_cli_evidence(summary: SessionSummary, cli: str, ev: Evidence) -> None:
    summary.pp_cli_invocations.setdefault(cli, []).append(ev)


def capture_pp_cli_hang(
    summary: SessionSummary, source: str, path: Path, line_no: int, command: str, output: str
) -> None:
    """Record a non-failing pp-cli result that stalled or timed out.

    A clean timeout/cancel does not trip the failure regex, so without this the
    "stuck" case the loop is meant to catch would be invisible. Only called for
    output that is not already classified as a failure.
    """
    if not output or not HANG_RE.search(output):
        return
    for cli in pp_cli_names(command):
        add_pp_cli_evidence(
            summary,
            cli,
            evidence(
                source=source,
                path=path,
                line=line_no,
                kind="pp_cli_hang",
                text=output,
                session_id=summary.session_id,
                tool_name="Bash",
                command=command,
            ),
        )


def shell_tokens(command: str) -> List[str]:
    lexer = shlex.shlex(command or "", posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:
        return (command or "").split()


def pp_cli_names(command: str) -> List[str]:
    return sorted(_pp_cli_names(command or "", depth=0))


def _pp_cli_names(command: str, depth: int) -> set[str]:
    if depth > 2 or "-pp-cli" not in command:
        return set()

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
            if basename.endswith("-pp-cli"):
                names.add(basename)
                continue
            if basename in REMOTE_COMMAND_WRAPPERS:
                for nested in tokens[index + 1 :]:
                    if "-pp-cli" in nested:
                        names.update(_pp_cli_names(nested, depth + 1))
                continue

        if current_executable == "source":
            if basename in {"kssh", "kssh_once"}:
                for nested in tokens[index + 1 :]:
                    if "-pp-cli" in nested:
                        names.update(_pp_cli_names(nested, depth + 1))
                continue
        if current_executable in REMOTE_COMMAND_WRAPPERS and "-pp-cli" in token:
            names.update(_pp_cli_names(token, depth + 1))

    names.update(pp_cli_names_from_for_loop(command))
    return names


def pp_cli_names_from_for_loop(command: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(.+?)\s*;?\s*do\b(.+?)(?:\bdone\b|$)", command, re.DOTALL):
        variable, items, body = match.groups()
        if f"${variable}" not in body:
            continue
        names.update(PP_CLI_RE.findall(items))
    return names


def is_failure_text(text: str, command: str = "") -> bool:
    if not text:
        return False
    pp_names = pp_cli_names(command)
    if BAD_EXIT_RE.search(text):
        return True
    if GOOD_EXIT_RE.search(text):
        if pp_names and PP_STRONG_FRICTION_RE.search(text):
            return True
        return False
    if pp_names:
        if HELP_COMMAND_RE.search(command) and not PP_STRONG_FRICTION_RE.search(text):
            return False
        return bool(PP_STRONG_FRICTION_RE.search(text))
    return bool(FAILURE_RE.search(text))


def pp_cli_failures_for_output(command: str, output: str) -> List[str]:
    clis = pp_cli_names(command)
    if not clis or not is_failure_text(output, command):
        return []
    if len(clis) == 1:
        return clis

    lines = output.splitlines()
    localized = set()
    for index, line in enumerate(lines):
        for cli in clis:
            if cli not in line:
                continue
            window = "\n".join(lines[max(0, index - 1) : index + 3])
            if is_failure_text(window, command) or PP_FRICTION_RE.search(window):
                localized.add(cli)
    return sorted(localized)


def parse_claude_session(path: Path) -> SessionSummary:
    summary = SessionSummary(source="claude", path=path, session_id=path.stem)
    calls: Dict[str, ToolCall] = {}

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

        if role == "user":
            text = text_from_claude_content(content)
            if text:
                for command in SLASH_COMMAND_RE.findall(text):
                    ev = evidence(
                        source="claude",
                        path=path,
                        line=line_no,
                        kind="slash_command",
                        text=f"/{command}",
                        session_id=summary.session_id,
                    )
                    summary.slash_commands.setdefault(command, []).append(ev)
                if is_user_correction_text(text, path):
                    summary.corrections.append(
                        evidence(
                            source="claude",
                            path=path,
                            line=line_no,
                            kind="user_correction",
                            text=text,
                            session_id=summary.session_id,
                        )
                    )

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
                    call = ToolCall(call_id=call_id, name=name, line=line_no, command=command, skill=skill)
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
                            )
                        )
                    if name == "Bash" and command:
                        for cli in pp_cli_names(command):
                            add_pp_cli_evidence(
                                summary,
                                cli,
                                evidence(
                                    source="claude",
                                    path=path,
                                    line=line_no,
                                    kind="pp_cli_invocation",
                                    text=command,
                                    session_id=summary.session_id,
                                    tool_name=name,
                                    command=command,
                                ),
                            )
                elif item.get("type") == "tool_result":
                    call_id = str(item.get("tool_use_id") or "")
                    call = calls.get(call_id)
                    result_text = text_from_tool_result(item.get("content"))
                    if call and result_text and is_failure_text(result_text, call.command):
                        ev = evidence(
                            source="claude",
                            path=path,
                            line=line_no,
                            kind="tool_failure",
                            text=result_text,
                            session_id=summary.session_id,
                            tool_name=call.name,
                            command=call.command,
                        )
                        summary.failures.append(ev)
                        if call.name == "Bash":
                            for cli in pp_cli_failures_for_output(call.command, result_text):
                                add_pp_cli_evidence(summary, cli, ev)
                    elif call and result_text and call.name == "Bash":
                        capture_pp_cli_hang(
                            summary, "claude", path, line_no, call.command, result_text
                        )

        tool_result = rec.get("toolUseResult")
        if isinstance(tool_result, dict):
            text = json.dumps(tool_result, sort_keys=True)
            source_uuid = str(rec.get("sourceToolAssistantUUID") or "")
            if text and is_failure_text(text):
                call = next((c for c in calls.values() if c.call_id == source_uuid), None)
                command = call.command if call else ""
                tool_name = call.name if call else ""
                summary.failures.append(
                    evidence(
                        source="claude",
                        path=path,
                        line=line_no,
                        kind="tool_failure",
                        text=text,
                        session_id=summary.session_id,
                        tool_name=tool_name,
                        command=command,
                    )
                )

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

        text = codex_message_text(payload)
        if text and payload.get("type") in {"user_message", "message"}:
            for command in SLASH_COMMAND_RE.findall(text):
                ev = evidence(
                    source="codex",
                    path=path,
                    line=line_no,
                    kind="slash_command",
                    text=f"/{command}",
                    session_id=summary.session_id,
                )
                summary.slash_commands.setdefault(command, []).append(ev)
            if is_user_correction_text(text, path):
                summary.corrections.append(
                    evidence(
                        source="codex",
                        path=path,
                        line=line_no,
                        kind="user_correction",
                        text=text,
                        session_id=summary.session_id,
                    )
                )

        if payload.get("type") == "function_call":
            name = str(payload.get("name") or "")
            call_id = str(payload.get("call_id") or f"{path}:{line_no}:{len(calls)}")
            args = parse_json_maybe(payload.get("arguments"))
            command = str(args.get("cmd") or args.get("command") or "")
            call = ToolCall(call_id=call_id, name=name, line=line_no, command=command)
            calls[call_id] = call
            summary.tool_calls.append(call)
            if command:
                for cli in pp_cli_names(command):
                    add_pp_cli_evidence(
                        summary,
                        cli,
                        evidence(
                            source="codex",
                            path=path,
                            line=line_no,
                            kind="pp_cli_invocation",
                            text=command,
                            session_id=summary.session_id,
                            tool_name=name,
                            command=command,
                        ),
                    )
        elif payload.get("type") == "function_call_output":
            call_id = str(payload.get("call_id") or "")
            call = calls.get(call_id)
            output = str(payload.get("output") or "")
            if call and output and is_failure_text(output, call.command):
                ev = evidence(
                    source="codex",
                    path=path,
                    line=line_no,
                    kind="tool_failure",
                    text=output,
                    session_id=summary.session_id,
                    tool_name=call.name,
                    command=call.command,
                )
                summary.failures.append(ev)
                for cli in pp_cli_failures_for_output(call.command, output):
                    add_pp_cli_evidence(summary, cli, ev)
            elif call and output:
                capture_pp_cli_hang(summary, "codex", path, line_no, call.command, output)

    return summary


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
        h.update(f"{ev.source}:{ev.path}:{ev.line}:{ev.kind}".encode())
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
    return {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": f"imp-{key}",
        "proposal_key": key,
        "created_at": utc_now(),
        "status": "proposed",
        "route": route,
        "title": title,
        "summary": summary,
        "impact": impact,
        "target": {"kind": target_kind, "name": target_name},
        "evidence": [ev.as_dict() for ev in evidence_items[:12]],
        "suggested_action": suggested_action,
        "apply_policy": {
            "mode": "manual_approval_required",
            "notes": "This command stages proposals only. Review and approve before editing skills, memory, runbooks, source code.",
        },
    }


def content_proposal_key(title: str, evidence_items: List[Evidence]) -> str:
    h = hashlib.sha256()
    h.update(b"content_idea\0")
    h.update(title.encode())
    for ev in evidence_items:
        h.update(b"\0")
        h.update(f"{ev.source}:{ev.path}:{ev.line}:{ev.kind}".encode())
    return h.hexdigest()[:20]


def privacy_for_content(evidence_items: List[Evidence]) -> Dict[str, Any]:
    joined = "\n".join(
        " ".join([ev.excerpt or "", ev.command or ""]) for ev in evidence_items
    )
    is_private = bool(PRIVATE_CONTENT_RE.search(joined))
    must_anonymize = [
        "names",
        "emails",
        "phone numbers",
        "auth tokens",
        "client/customer details",
    ]
    blocked = ["raw secrets", "private client data", "family or health details"]
    if is_private:
        must_anonymize.extend(["raw message contents", "contact names", "thread excerpts"])
        blocked.extend(["raw messages", "contact lists"])
    return {
        "risk_level": "high" if is_private else "low",
        "must_anonymize": must_anonymize,
        "safe_public_abstraction": (
            "Teach the reusable workflow with synthetic examples; keep private names, "
            "messages, clients, family details, auth material, and exact sensitive data out."
        ),
        "blocked_details": blocked,
    }


def content_evidence_dicts(evidence_items: List[Evidence], privacy: Dict[str, Any]) -> List[Dict[str, Any]]:
    if privacy.get("risk_level") == "high":
        return [
            {
                "source": ev.source,
                "path": ev.path,
                "line": ev.line,
                "session_id": ev.session_id,
                "kind": ev.kind,
                "tool_name": ev.tool_name,
                "command": "<private workflow evidence redacted>",
                "excerpt": "<private workflow evidence redacted>",
            }
            for ev in evidence_items[:12]
        ]
    return [ev.as_dict() for ev in evidence_items[:12]]


def make_content_proposal(
    *,
    title: str,
    content_type: str,
    evidence_items: List[Evidence],
    trigger_kind: str,
    real_workflow_or_moment: str,
    audience: List[str],
    why_interesting: List[str],
    suggested_search_query: str,
    rough_outline: List[str],
    confidence: float,
    recommendation: str,
) -> Dict[str, Any]:
    key = content_proposal_key(title, evidence_items)
    privacy = privacy_for_content(evidence_items)
    if privacy["risk_level"] == "high" and recommendation == "write_now":
        recommendation = "needs_context"
        confidence = min(confidence, 0.72)
    return {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": f"content-{key}",
        "proposal_key": key,
        "created_at": utc_now(),
        "status": "proposed",
        "route": "content_idea",
        "title": title,
        "content_type": content_type,
        "recommendation": recommendation,
        "confidence": round(confidence, 2),
        "summary": real_workflow_or_moment,
        "target": {"kind": "content_angle", "name": title},
        "trigger": {
            "kind": trigger_kind,
            "real_workflow_or_moment": real_workflow_or_moment,
        },
        "audience": audience,
        "why_interesting": why_interesting,
        "reader_usefulness": [
            "Grounded in actual sessions rather than generic brainstorming.",
            "Can be turned into a tutorial, case study, or behind-the-scenes post after review.",
        ],
        "timeliness": {
            "why_now": "Agent workflows, local AI memory, and coding-agent operations are active public conversations.",
            "last30days_recommended": True,
        },
        "last30days": {
            "should_run": True,
            "suggested_search_query": suggested_search_query,
            "purpose": "Validate public language, objections, adjacent examples, and current demand before drafting.",
        },
        "rough_outline": rough_outline,
        "privacy": {**privacy, "content_notice": CONTENT_PRIVACY_NOTICE},
        "suggested_action": (
            "Review the angle, privacy notes, and evidence references. If approved, "
            "optionally run the suggested last30days query, then brief a writer. Do not auto-draft."
        ),
        "evidence": content_evidence_dicts(evidence_items, privacy),
        "review": {
            "needed_from_cat": [
                "Approve, reject, or reframe the angle.",
                "Confirm what details must stay private or be anonymized.",
                "Decide whether to run last30days before briefing a writer.",
            ],
            "next_action": "Stage for editorial review; do not draft or publish automatically.",
        },
        "apply_policy": {
            "mode": "manual_review_required",
            "notes": "This route stages content ideas only. Review before research, drafting, posting, or publishing.",
        },
    }


def workflow_evidence_from_tool_call(session: SessionSummary, call: ToolCall) -> Evidence:
    return Evidence(
        source=session.source,
        path=str(session.path),
        line=call.line,
        kind="workflow_signal",
        excerpt=call.command,
        session_id=session.session_id,
        tool_name=call.name,
        command=call.command,
    )


def generate_workflow_content_proposals(sessions: List[SessionSummary]) -> List[Dict[str, Any]]:
    proposals: List[Dict[str, Any]] = []
    for pattern in CONTENT_WORKFLOW_PATTERNS:
        evidence_items: List[Evidence] = []
        regex = pattern["regex"]
        for session in sessions:
            for call in session.tool_calls:
                if call.command and regex.search(call.command):
                    evidence_items.append(workflow_evidence_from_tool_call(session, call))
        min_signals = int(pattern.get("min_signals", 2))
        if len(evidence_items) < min_signals:
            continue
        session_count = len({ev.session_id for ev in evidence_items})
        recommendation = str(pattern.get("recommendation", "save_for_later"))
        proposals.append(
            make_content_proposal(
                title=str(pattern["title"]),
                content_type=str(pattern.get("content_type", "case_study")),
                evidence_items=evidence_items,
                trigger_kind="workflow_command_cluster",
                real_workflow_or_moment=(
                    f"Matched {len(evidence_items)} command-level workflow signal(s) "
                    f"across {session_count} session(s), suggesting a repeatable operating loop."
                ),
                audience=list(pattern.get("audience", ["agent builders", "operators"])),
                why_interesting=list(pattern.get("why", ["Grounded in repeated real-session commands."])),
                suggested_search_query=str(pattern.get("query", "AI agent workflow operations")),
                rough_outline=list(pattern.get("outline", ["The workflow", "The implementation", "What readers can copy"])),
                confidence=float(pattern.get("confidence", 0.7)),
                recommendation=recommendation,
            )
        )
    return proposals


def is_content_cli_name(executable: str) -> bool:
    if not executable or executable in CONTENT_CLI_IGNORE or executable.startswith("-"):
        return False
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9._-]*$", executable))


def content_count_summary(items: List[Tuple[str, int]], prefix: str = "") -> str:
    return ", ".join(f"{prefix}{name} ({count})" for name, count in items)


def top_evidence(items_by_name: Dict[str, List[Evidence]], ranked: List[Tuple[str, int]], limit: int = 12) -> List[Evidence]:
    evidence_items: List[Evidence] = []
    for name, _count in ranked:
        evidence_items.extend(items_by_name.get(name, [])[:2])
        if len(evidence_items) >= limit:
            break
    return evidence_items[:limit]


def generate_aggregate_content_proposals(
    sessions: List[SessionSummary], workflow_proposals: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    proposals: List[Dict[str, Any]] = []

    skill_items: Dict[str, List[Evidence]] = {}
    slash_items: Dict[str, List[Evidence]] = {}
    cli_items: Dict[str, List[Evidence]] = {}
    for session in sessions:
        for name, items in session.skill_invocations.items():
            skill_items.setdefault(name, []).extend(items)
        for name, items in session.slash_commands.items():
            if name and name[0].islower():
                slash_items.setdefault(name, []).extend(items)
        for call in session.tool_calls:
            executable = first_executable(call.command or "")
            if not is_content_cli_name(executable):
                continue
            cli_items.setdefault(executable, []).append(workflow_evidence_from_tool_call(session, call))

    top_skills = Counter({name: len(items) for name, items in skill_items.items()}).most_common(10)
    if len(top_skills) >= 3:
        proposals.append(
            make_content_proposal(
                title="My top 10 skills for running agents like an operating system",
                content_type="listicle",
                evidence_items=top_evidence(skill_items, top_skills),
                trigger_kind="aggregate_skill_usage",
                real_workflow_or_moment=(
                    "Most-used skills across scanned sessions: " + content_count_summary(top_skills[:10])
                ),
                audience=["agent builders", "founders", "AI operators"],
                why_interesting=[
                    "It turns actual usage data into a practical stack, not a generic tools list.",
                    "Readers can copy the categories: research, task ledger, QA, publishing, and review.",
                ],
                suggested_search_query="best Claude Code skills agent workflows tools operators",
                rough_outline=[
                    "The scoring rule: skills I actually use repeatedly",
                    "Top 10 skills and what each one does in the loop",
                    "Which skills are for quality, which are for leverage, which are for safety",
                    "What I would install first if starting from zero",
                    "What the list says about where agent work is going",
                ],
                confidence=0.8,
                recommendation="write_now",
            )
        )

    top_slashes = Counter({name: len(items) for name, items in slash_items.items()}).most_common(10)
    if len(top_slashes) >= 3:
        proposals.append(
            make_content_proposal(
                title="My most-used slash commands for agent work",
                content_type="listicle",
                evidence_items=top_evidence(slash_items, top_slashes),
                trigger_kind="aggregate_slash_command_usage",
                real_workflow_or_moment=(
                    "Most-used slash commands across scanned sessions: "
                    + content_count_summary(top_slashes[:10], prefix="/")
                ),
                audience=["Claude Code users", "Codex users", "agent operators"],
                why_interesting=[
                    "Slash commands are visible, copyable control surfaces for repeatable agent work.",
                    "A frequency-ranked list is more credible than a generic command catalog.",
                ],
                suggested_search_query="Claude Code slash commands agent workflow examples",
                rough_outline=[
                    "Why slash commands beat repeating prompts",
                    "The commands I use most and what each one gates",
                    "When a command should exist vs. a one-off prompt",
                    "How to design commands around review, QA, and safety",
                    "A starter command set readers can copy",
                ],
                confidence=0.78,
                recommendation="write_now",
            )
        )

    top_clis = Counter({name: len(items) for name, items in cli_items.items()}).most_common(10)
    if len(top_clis) >= 3:
        proposals.append(
            make_content_proposal(
                title="The command-line stack I actually use to run agent workflows",
                content_type="listicle",
                evidence_items=top_evidence(cli_items, top_clis),
                trigger_kind="aggregate_cli_usage",
                real_workflow_or_moment=(
                    "Most-used command-line tools across scanned sessions: "
                    + content_count_summary(top_clis[:10])
                ),
                audience=["agent builders", "technical founders", "operators"],
                why_interesting=[
                    "It shows the boring plumbing behind useful agent work: CLIs, APIs, and local scripts.",
                    "A usage-ranked stack is more credible than a wishlist of trendy tools.",
                ],
                suggested_search_query="AI agent workflows command line tools CLI automation stack",
                rough_outline=[
                    "Why my agents spend so much time in the command line",
                    "The top tools by real usage and what each one unlocks",
                    "Which tools are public-safe and which need synthetic examples",
                    "How I decide when to make a CLI vs. a skill vs. a slash command",
                    "A starter stack for readers who want agent workflows that leave chat",
                ],
                confidence=0.78,
                recommendation="write_now",
            )
        )

    if len(workflow_proposals) >= 2:
        labels = []
        evidence_items: List[Evidence] = []
        for proposal in workflow_proposals:
            title = proposal.get("title", "")
            if "task ledger" in title:
                labels.append("task-ledger")
            elif "executive assistant" in title:
                labels.append("executive-assistant")
            elif "revenue watch" in title:
                labels.append("revenue-watch")
            else:
                labels.append(str(title))
            for ev in proposal.get("evidence", [])[:3]:
                evidence_items.append(
                    Evidence(
                        source=str(ev.get("source", "")),
                        path=str(ev.get("path", "")),
                        line=int(ev.get("line", 0) or 0),
                        kind=str(ev.get("kind", "workflow_signal")),
                        excerpt=str(ev.get("excerpt", "")),
                        session_id=str(ev.get("session_id", "")),
                        tool_name=str(ev.get("tool_name", "")),
                        command=str(ev.get("command", "")),
                    )
                )
        labels = list(dict.fromkeys(labels))
        proposals.append(
            make_content_proposal(
                title="Loop examples: the agent workflows I keep reusing",
                content_type="roundup",
                evidence_items=evidence_items[:12],
                trigger_kind="aggregate_loop_examples",
                real_workflow_or_moment=(
                    "Reusable loops surfaced from real sessions: " + ", ".join(labels[:8])
                ),
                audience=["agent builders", "operators", "technical founders"],
                why_interesting=[
                    "The strongest story is not one workflow; it is the pattern across workflows.",
                    "Loop examples make the abstract advice concrete and show what agents can own repeatedly.",
                ],
                suggested_search_query="AI agent workflow loops examples recurring automation operators",
                rough_outline=[
                    "What makes something a loop instead of a prompt",
                    "Loop 1: task ledger / agent work tracking",
                    "Loop 2: executive assistant triage",
                    "Loop 3: revenue watch / dashboard replacement",
                    "How to choose which loop to build next",
                ],
                confidence=0.82,
                recommendation="write_now",
            )
        )

    return proposals


def generate_content_proposals(sessions: List[SessionSummary]) -> List[Dict[str, Any]]:
    proposals: List[Dict[str, Any]] = []

    slash_by_name: Dict[str, List[Evidence]] = {}
    for session in sessions:
        for name, items in session.slash_commands.items():
            slash_by_name.setdefault(name, []).extend(items)
    for name, evidence_items in sorted(slash_by_name.items()):
        sessions_seen = {ev.session_id for ev in evidence_items}
        profile = CONTENT_SLASH_COMMANDS.get(name)
        if not profile and len(sessions_seen) < 2:
            continue
        command_label = f"/{name}"
        title = str(profile.get("title")) if profile else f"How I use {command_label} in real agent workflows"
        proposals.append(
            make_content_proposal(
                title=title,
                content_type=profile.get("content_type", "blog_post") if profile else "blog_post",
                evidence_items=evidence_items,
                trigger_kind="slash_command_usage",
                real_workflow_or_moment=(
                    f"{command_label} appeared in {len(evidence_items)} transcript signal(s) "
                    f"across {len(sessions_seen)} session(s), suggesting a repeatable agent workflow."
                ),
                audience=profile.get("audience", ["agent builders", "operators"]) if profile else ["agent builders", "operators"],
                why_interesting=[
                    "Slash commands are a concrete control surface for agents, not vague prompting advice.",
                    "Repeated use means this is likely part of a real operating system.",
                ],
                suggested_search_query=profile.get("query", f"{command_label} agent workflows slash commands") if profile else f"{command_label} agent workflows slash commands",
                rough_outline=[
                    f"The problem {command_label} solves",
                    "The workflow before the command existed",
                    "How I use it in real sessions",
                    "What readers can copy without copying private details",
                    "Where the loop still needs human judgment",
                ],
                confidence=0.82 if len(sessions_seen) >= 2 else 0.72,
                recommendation="write_now" if len(sessions_seen) >= 2 or profile else "save_for_later",
            )
        )

    private_build_evidence: List[Evidence] = []
    for session in sessions:
        for ev in session.failures:
            text = f"{ev.excerpt} {ev.command}"
            if PRIVATE_CONTENT_RE.search(text) and BUILD_CONTENT_RE.search(text):
                private_build_evidence.append(ev)
    if private_build_evidence:
        proposals.append(
            make_content_proposal(
                title="How I built a personal CRM from my text messages",
                content_type="case_study",
                evidence_items=private_build_evidence,
                trigger_kind="built_from_scratch_private_workflow",
                real_workflow_or_moment=(
                    "Session evidence points to a private communication/search workflow: "
                    "turning messages or contact context into a searchable operational system."
                ),
                audience=["founders", "operators", "agent builders", "local-first AI users"],
                why_interesting=[
                    "It has a clear before/after: messy private threads become searchable context.",
                    "It shows agents doing useful glue work around real life, not demo prompts.",
                    "It is publishable only as an anonymized architecture/workflow story.",
                ],
                suggested_search_query="personal CRM text messages AI memory iMessage search local-first agents",
                rough_outline=[
                    "The problem: useful commitments and context live in text threads",
                    "Why normal contact apps and inbox search are not enough",
                    "The local indexing/search architecture",
                    "Where agents help extract follow-up context",
                    "Privacy boundaries and synthetic examples",
                    "What to build next",
                ],
                confidence=0.72,
                recommendation="needs_context",
            )
        )

    workflow_proposals = generate_workflow_content_proposals(sessions)
    aggregate_proposals = generate_aggregate_content_proposals(sessions, workflow_proposals)
    return dedupe_proposals(proposals + workflow_proposals + aggregate_proposals)


def printing_press_source(cli: str, pp_root: Optional[Path]) -> Optional[Path]:
    """Resolve a ``*-pp-cli`` name to its source directory in the printing-press tree.

    Returns the first existing directory among ``<root>/library/<name>``,
    ``<root>/manuscripts/<name>``, and ``<root>/<name>``, or ``None`` when no root
    is configured, the name is not a pp-cli, or nothing matches on disk. Resolving
    on disk keeps the default safe: on a machine with no printing-press tree the
    proposal simply omits the source line instead of inventing a path.
    """
    if not pp_root or not cli.endswith("-pp-cli"):
        return None
    name = cli[: -len("-pp-cli")]
    for candidate in (pp_root / "library" / name, pp_root / "manuscripts" / name, pp_root / name):
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def generate_proposals(
    sessions: List[SessionSummary], pp_root: Optional[Path] = None, route: str = "improvement"
) -> List[Dict[str, Any]]:
    proposals: List[Dict[str, Any]] = []

    include_improvement = route in {"all", "improvement"}
    include_content = route in {"all", "content_idea"}

    if not include_improvement and include_content:
        return generate_content_proposals(sessions)

    pp_by_cli: Dict[str, List[Evidence]] = {}
    pp_failures: Dict[str, List[Evidence]] = {}
    pp_hangs: Dict[str, List[Evidence]] = {}
    pp_max_retries: Dict[str, int] = {}
    for session in sessions:
        for cli, items in session.pp_cli_invocations.items():
            pp_by_cli.setdefault(cli, []).extend(items)
            # Retries are counted per session: N invocations of the same CLI in
            # one session is the agent guessing syntax, not normal repeat use
            # spread across days.
            pp_max_retries[cli] = max(pp_max_retries.get(cli, 0), len(items))
            for item in items:
                if item.kind == "tool_failure" or is_failure_text(item.excerpt, item.command):
                    pp_failures.setdefault(cli, []).append(item)
                elif item.kind == "pp_cli_hang" or HANG_RE.search(item.excerpt or ""):
                    pp_hangs.setdefault(cli, []).append(item)

    flagged_clis = sorted(
        cli
        for cli in (
            set(pp_failures)
            | set(pp_hangs)
            | {cli for cli, count in pp_max_retries.items() if count >= RETRY_STUCK_THRESHOLD}
        )
        if VALID_PP_CLI_RE.match(cli)
    )
    for cli in flagged_clis:
        failures = pp_failures.get(cli, [])
        hangs = pp_hangs.get(cli, [])
        invocations = pp_by_cli.get(cli, [])
        max_retries = pp_max_retries.get(cli, 0)
        stuck = max_retries >= RETRY_STUCK_THRESHOLD
        evidence_items = (failures + hangs + invocations)[:12]
        session_count = len({ev.session_id for ev in evidence_items})

        friction_bits: List[str] = []
        if failures:
            friction_bits.append(f"{len(failures)} failure signal(s)")
        if hangs:
            friction_bits.append(f"{len(hangs)} hang/timeout signal(s)")
        if stuck:
            friction_bits.append(f"retried up to {max_retries}x in one session before it worked")
        summary = f"{cli}: " + ", ".join(friction_bits) + f" across {session_count} session(s)."

        action = (
            "Review the evidence. If it reflects a missing command, bad flag, bad JSON "
            "contract, silent-null result, fragile auth flow, or syntax the agent had to "
            "guess and retry, fix the tool itself instead of working around it in a prompt."
        )
        source = printing_press_source(cli, pp_root)
        if source is not None:
            action += (
                f" This is a printing-press CLI; its source is at {source}. Open its "
                "spec.yaml/README, then run /printing-press-amend to patch it (or "
                "/printing-press-reprint to rebuild it from scratch)."
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

    correction_only: List[Evidence] = []
    for session in sessions:
        if session.corrections and not session.skill_invocations:
            correction_only.extend(session.corrections)
    if correction_only:
        proposals.append(
            make_proposal(
                route="memory_context",
                title="Review durable user/project preference corrections",
                summary=(
                    f"{len(correction_only)} correction signal(s) were not tied to a "
                    "specific invoked skill."
                ),
                target_kind="memory_or_runbook",
                target_name="unrouted-corrections",
                evidence_items=correction_only[:12],
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
        for fail in session.failures:
            executable = backlog_executable(fail.command)
            if executable and not executable.endswith("-pp-cli"):
                repeated_failures.setdefault(executable, []).append(fail)
    for executable, failures in sorted(repeated_failures.items()):
        durable_failures = [f for f in failures if TOOLING_FRICTION_RE.search(f.excerpt)]
        session_count = len({f.session_id for f in durable_failures})
        if len(durable_failures) < 3 or session_count < 2:
            continue
        proposals.append(
            make_proposal(
                route="backlog",
                title=f"Investigate repeated {executable} command failures",
                summary=(
                    f"{executable} had {len(durable_failures)} command-interface friction "
                    f"signal(s) across "
                    f"{session_count} session(s)."
                ),
                target_kind="tooling",
                target_name=executable,
                evidence_items=durable_failures[:12],
                suggested_action=(
                    "Decide whether this is a durable tooling/runbook gap or a transient "
                    "environment issue. If durable, stage a backlog note or runbook patch."
                ),
                impact=["shorter", "safer"],
            )
        )

    if include_content:
        proposals.extend(generate_content_proposals(sessions))

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
    if parts[0] in {"env", "command", "time"} and len(parts) > 1:
        return parts[1]
    if "=" in parts[0] and len(parts) > 1:
        return parts[1]
    return Path(parts[0]).name


def backlog_executable(command: str) -> str:
    if not command:
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


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_review_packet(
    root: Path,
    run_id: str,
    sessions: List[SessionSummary],
    proposals: List[Dict[str, Any]],
) -> Path:
    path = root / "review-packets" / f"{run_id}.md"
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
        f"- Sessions with signals: {len(sessions)}",
        f"- Proposals staged this run: {len(proposals)}",
        "",
        "## Proposals",
        "",
    ]
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
            lines.append(f"  - `{ev['kind']}` {loc}{cmd} - {ev['excerpt']}")
        lines.append("")

    if any(proposal.get("route") == "content_idea" for proposal in proposals):
        lines.extend(
            [
                "## Content privacy notice",
                "",
                CONTENT_PRIVACY_NOTICE,
                "",
            ]
        )

    lines.extend(["## Session Index", ""])
    for session in sessions[:200]:
        data = session.as_dict()
        lines.append(
            f"- `{data['source']}` `{data['session_id']}` "
            f"tools={data['tool_call_count']} pp={','.join(data['pp_cli_names']) or '-'} "
            f"skills={','.join(data['skill_names']) or '-'} failures={data['failure_count']} "
            f"corrections={data['correction_count']}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def filter_new_proposals(proposals: List[Dict[str, Any]], state: Dict[str, Any], include_seen: bool) -> List[Dict[str, Any]]:
    if include_seen:
        return proposals
    seen = set(state.get("seen_proposal_keys") or [])
    return [p for p in proposals if p.get("proposal_key") not in seen]


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
    root = Path(args.output_root).expanduser()
    state = load_state(root)
    since = compute_since(args, state)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    files: List[Tuple[str, Path]] = discover_session_files(homes, args.source)
    files = [(source, p) for source, p in files if session_in_window(p, since)]
    files.sort(key=lambda item: item[1].stat().st_mtime)
    if args.max_sessions:
        files = files[-args.max_sessions :]

    sessions: List[SessionSummary] = []
    for source, path in files:
        try:
            parsed = parse_claude_session(path) if source == "claude" else parse_codex_session(path)
        except Exception as exc:
            print(f"warning: failed to parse {path}: {exc}", file=sys.stderr)
            continue
        if parsed.has_signal():
            sessions.append(parsed)

    pp_root_arg = getattr(args, "printing_press_root", None)
    pp_root = Path(pp_root_arg).expanduser() if pp_root_arg else None
    proposals = generate_proposals(sessions, pp_root, route=args.route)
    proposals = filter_new_proposals(proposals, state, args.include_seen)

    result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "started_at": utc_now(),
        "source": args.source,
        "since": since.isoformat() if since else None,
        "files_scanned": len(files),
        "sessions_with_signals": len(sessions),
        "proposal_count": len(proposals),
        "output_root": str(root),
    }

    if args.dry_run:
        print(json.dumps({**result, "proposals": proposals}, ensure_ascii=False, indent=2))
        return 0

    session_rows = [s.as_dict() for s in sessions]
    append_jsonl(root / "session-index.jsonl", session_rows)

    proposal_dir = root / "proposals" / run_id
    for proposal in proposals:
        write_json(proposal_dir / f"{proposal['proposal_id']}.json", proposal)

    packet_path = write_review_packet(root, run_id, sessions, proposals)
    state["schema_version"] = SCHEMA_VERSION
    state["last_scan_started_at"] = result["started_at"]
    state["last_run_id"] = run_id
    seen = set(state.get("seen_proposal_keys") or [])
    seen.update(p["proposal_key"] for p in proposals)
    state["seen_proposal_keys"] = sorted(seen)
    write_json(root / "state.json", state)
    write_json(root / "runs" / f"{run_id}.json", {**result, "review_packet": str(packet_path)})

    print(f"scanned={len(files)} sessions_with_signals={len(sessions)} proposals={len(proposals)}")
    print(f"review_packet={packet_path}")
    if proposals:
        print(f"proposal_dir={proposal_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan Claude/Codex sessions and stage self-improvement proposals."
    )
    parser.add_argument("--home", default=str(Path.home()), help="Home directory containing .claude/.codex")
    parser.add_argument(
        "--extra-home",
        action="append",
        default=[],
        help="Additional home directory containing .claude/.codex logs, e.g. logs copied from another machine over ssh",
    )
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Proposal queue root")
    parser.add_argument("--source", choices=["all", "claude", "codex"], default="all")
    parser.add_argument(
        "--route",
        choices=["all", "improvement", "content_idea"],
        default="improvement",
        help="Proposal route family to stage: operational improvements, content ideas, or both",
    )
    parser.add_argument("--all", action="store_true", help="Backfill all discovered sessions")
    parser.add_argument("--since-days", type=float, default=None, help="Scan sessions modified within N days")
    parser.add_argument("--max-sessions", type=int, default=0, help="Limit to most recent N sessions after filtering")
    parser.add_argument("--include-seen", action="store_true", help="Emit proposals even if their keys were seen before")
    parser.add_argument("--full", action="store_true", help="Keep full, unredacted excerpts inline (local use only; do not share the output)")
    parser.add_argument(
        "--printing-press-root",
        default=os.environ.get("PRINTING_PRESS_ROOT", PRINTING_PRESS_ROOT_DEFAULT),
        help="Root of the printing-press CLI tree; tool proposals point at the matching CLI source (default ~/printing-press)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print JSON and do not write queue files")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return scan(args)


if __name__ == "__main__":
    raise SystemExit(main())
