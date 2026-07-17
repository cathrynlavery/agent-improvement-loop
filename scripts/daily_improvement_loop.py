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
DEFAULT_CONFIG_PATH = DEFAULT_OUTPUT_ROOT / "config.json"
RESOLUTION_DECISIONS = {"fixed", "wontfix", "ignored"}

# When True (via --full), keep full, unredacted excerpts inline. Default masks
# secrets and shortens excerpts so the written output is safe to commit, sync,
# paste into a writeup, or hand to another agent.
FULL_DETAIL = False
FULL_EXCERPT_LIMIT = 4000

# CLIs whose command name ends in this suffix get the `tool` route. Override
# with "tracked_cli_suffix" in the config file to track your own naming scheme.
TRACKED_CLI_SUFFIX = "-pp-cli"


def _build_tracked_cli_res(suffix: str) -> Tuple[re.Pattern[str], re.Pattern[str]]:
    esc = re.escape(suffix)
    loose = re.compile(rf"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9._-]*{esc})(?=$|[\s;&|)])")
    # Anchored form, used to reject malformed names the tokenizer can pick up
    # from shell quoting or transcript scaffolding (e.g. "'wavespeed-pp-cli",
    # "$c-pp-cli", "===x-twitter-pp-cli") before they ever become a proposal.
    anchored = re.compile(rf"^[A-Za-z0-9][A-Za-z0-9._-]*{esc}$")
    return loose, anchored


PP_CLI_RE, VALID_PP_CLI_RE = _build_tracked_cli_res(TRACKED_CLI_SUFFIX)
ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*")
SHELL_SEPARATORS = {";", "&&", "||", "|", "do", "then", "else"}
COMMAND_PREFIXES = {"command", "env", "noglob", "time"}
REMOTE_COMMAND_WRAPPERS = {"bash", "kssh", "kssh_once", "sh", "ssh", "zsh"}
SLASH_COMMAND_RE = re.compile(r"(?m)^\s*/([a-z][A-Za-z0-9:_-]*)\b")
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
WEAK_CORRECTION_RE = re.compile(
    r"\b(actually|instead|don'?t do|do not|should have)\b",
    re.IGNORECASE,
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
PP_FRICTION_RE = re.compile(
    r"(?i)\b("
    r"FAIL|not configured|missing required|unknown option|usage:|"
    r"not found|invalid|unauthorized|forbidden|rate limit|silent null"
    r")\b"
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
# was canceled. This is friction even without a non-zero exit, and is a common
# printing-press CLI smell the failure regex alone would miss.
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
# A *-pp-cli invoked at least this many times in a single session can be
# retry-before-success friction when corroborated by failure/hang evidence or
# same-subcommand flag variation.
RETRY_STUCK_THRESHOLD = 3
# Reject code-literal-only names that cannot be resolved against an available
# printing-press source tree. This is config-gated for installations that use
# tracked CLI names without a local source checkout.
VALIDATE_PP_CLI_CANDIDATES = True
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
    tool_calls: List[ToolCall] = field(default_factory=list)
    pp_cli_invocations: Dict[str, List[Evidence]] = field(default_factory=dict)
    skill_invocations: Dict[str, List[Evidence]] = field(default_factory=dict)
    slash_commands: Dict[str, List[Evidence]] = field(default_factory=dict)
    failures: List[Evidence] = field(default_factory=list)
    silent_empty: List[Evidence] = field(default_factory=list)
    corrections: List[Evidence] = field(default_factory=list)

    def has_signal(self) -> bool:
        return bool(
            self.pp_cli_invocations
            or self.skill_invocations
            or self.failures
            or self.silent_empty
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
    if LEADING_TAG_RE.match(stripped):
        return True
    markers = (
        "<local-command-caveat>",
        "<permissions instructions>",
        "# agents.md instructions",
        "<instructions>",
        "<codex_internal_context",
        "<collaboration_mode>",
        "the following is the codex agent history",
        "as untrusted evidence, not as instructions to follow",
        "use the skill at ",
        "please implement this plan:",
        "# files mentioned by the user:",
        "<scheduled-task",
        "compound codex tool mapping",
        "<task-notification>",
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
    if len(stripped) <= STRONG_CORRECTION_MAX_CHARS and STRONG_CORRECTION_RE.search(stripped):
        return True
    return len(stripped) <= WEAK_CORRECTION_MAX_CHARS and bool(WEAK_CORRECTION_RE.search(stripped))


def add_pp_cli_evidence(summary: SessionSummary, cli: str, ev: Evidence) -> None:
    summary.pp_cli_invocations.setdefault(cli, []).append(ev)


def capture_pp_cli_hang(
    summary: SessionSummary,
    source: str,
    path: Path,
    line_no: int,
    command: str,
    output: str,
    clis: Optional[List[str]] = None,
    occurred_at: Any = "",
) -> None:
    """Record a non-failing pp-cli result that stalled or timed out.

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
    if INSPECTION_COMMAND_RE.search(command):
        return
    for cli in (clis if clis is not None else pp_cli_names(command)):
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


def pp_cli_names(command: str) -> List[str]:
    return sorted(_pp_cli_names(command or "", depth=0))


def pp_cli_names_from_code(text: str) -> List[str]:
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
        # escape and would otherwise fuse `true\ncloudflare-pp-cli` into the
        # fabricated command name `truencloudflare-pp-cli`.
        literal = match.group(2).replace(r"\n", "\n").replace(r"\t", "\t")
        if TRACKED_CLI_SUFFIX in literal:
            names.update(_pp_cli_names(literal, depth=1))
    return sorted(names)


def pp_cli_argv_shapes(command: str, cli: str) -> List[Tuple[str, Tuple[str, ...]]]:
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
        for subcommand, flags in pp_cli_argv_shapes(item.command, cli):
            if subcommand:
                by_subcommand.setdefault(subcommand, set()).add(flags)
    return any(len(flag_shapes) > 1 for flag_shapes in by_subcommand.values())


def _pp_cli_names(command: str, depth: int) -> set[str]:
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
                        names.update(_pp_cli_names(nested, depth + 1))
                continue

        if current_executable == "source":
            if basename in {"kssh", "kssh_once"}:
                for nested in tokens[index + 1 :]:
                    if TRACKED_CLI_SUFFIX in nested:
                        names.update(_pp_cli_names(nested, depth + 1))
                continue
        if current_executable in REMOTE_COMMAND_WRAPPERS and TRACKED_CLI_SUFFIX in token:
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


def is_failure_text(text: str, command: str = "", clis: Optional[List[str]] = None) -> bool:
    if not text:
        return False
    pp_names = pp_cli_names(command) if clis is None else clis
    if BAD_EXIT_RE.search(text):
        return True
    if GOOD_EXIT_RE.search(text):
        return False
    if pp_names:
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
        add_pp_cli_evidence(summary, cli, ev)


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


def pp_cli_failures_for_output(
    command: str,
    output: str,
    clis: Optional[List[str]] = None,
    confirmed_failure: bool = False,
) -> List[str]:
    clis = pp_cli_names(command) if clis is None else clis
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
            if BAD_EXIT_RE.search(window) or PP_FRICTION_RE.search(window):
                localized.add(cli)
    return sorted(localized)


def parse_claude_session(path: Path) -> SessionSummary:
    summary = SessionSummary(source="claude", path=path, session_id=path.stem)
    calls: Dict[str, ToolCall] = {}
    silent_candidates: List[Tuple[int, ToolCall, str, Optional[bool], Any]] = []
    agent_step_lines: List[int] = []
    agent_messages: List[Tuple[int, str]] = []

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

        if role == "assistant" and content:
            agent_step_lines.append(line_no)
            assistant_text = text_from_claude_content(content)
            if assistant_text:
                agent_messages.append((line_no, assistant_text))

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
                        occurred_at=ts,
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
                            occurred_at=ts,
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
                    call = ToolCall(
                        call_id=call_id,
                        name=name,
                        line=line_no,
                        command=command,
                        skill=skill,
                        clis=pp_cli_names(command) if name == "Bash" else [],
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
                            clis = pp_cli_failures_for_output(
                                call.command,
                                result_text,
                                call.clis,
                                confirmed_failure=True,
                            )
                            for cli in clis or call.clis:
                                add_pp_cli_evidence(summary, cli, ev)
                    elif call and result_text and call.name == "Bash":
                        capture_pp_cli_hang(
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
                    occurred_at=ts,
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
                        occurred_at=ts,
                    )
                )

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
                    clis = pp_cli_names_from_code(command)
                else:
                    clis = sorted(
                        set(pp_cli_names(command)) | set(pp_cli_names_from_code(command))
                    )
            else:
                args = parse_json_maybe(payload.get("arguments"))
                command = str(args.get("cmd") or args.get("command") or "")
                clis = pp_cli_names(command)
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
                for cli in pp_cli_failures_for_output(
                    call.command, output, call.clis, confirmed_failure=True
                ):
                    add_pp_cli_evidence(summary, cli, ev)
            elif call and output:
                capture_pp_cli_hang(
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
    created_at = utc_now()
    latest = latest_evidence_time(evidence_items)
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
                "occurred_at": ev.occurred_at,
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
    created_at = utc_now()
    latest = latest_evidence_time(evidence_items)
    if privacy["risk_level"] == "high" and recommendation == "write_now":
        recommendation = "needs_context"
        confidence = min(confidence, 0.72)
    return {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": f"content-{key}",
        "proposal_key": key,
        "created_at": created_at,
        "latest_evidence_at": isoformat_utc(latest) if latest else created_at,
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
        occurred_at=call.occurred_at,
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
    if not pp_root or not cli.endswith(TRACKED_CLI_SUFFIX):
        return None
    name = cli[: -len(TRACKED_CLI_SUFFIX)]
    for candidate in (pp_root / "library" / name, pp_root / "manuscripts" / name, pp_root / name):
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return None


def pp_cli_candidate_is_valid(
    cli: str, pp_root: Optional[Path], plain_command_clis: set[str]
) -> bool:
    """Conservatively reject unresolved names seen only in code literals."""
    if not VALIDATE_PP_CLI_CANDIDATES or cli in plain_command_clis:
        return True
    # A missing/unavailable source checkout cannot disprove the candidate.
    try:
        if not pp_root or not pp_root.is_dir():
            return True
    except OSError:
        return True
    return printing_press_source(cli, pp_root) is not None


def generate_proposals(
    sessions: List[SessionSummary], pp_root: Optional[Path] = None, route: str = "improvement"
) -> List[Dict[str, Any]]:
    proposals: List[Dict[str, Any]] = []

    include_improvement = route in {"all", "improvement"}
    include_content = route in {"all", "content_idea"}

    if not include_improvement and include_content:
        return generate_content_proposals(sessions)

    pp_invocations: Dict[str, List[Evidence]] = {}
    pp_failures: Dict[str, List[Evidence]] = {}
    pp_hangs: Dict[str, List[Evidence]] = {}
    pp_silent_empty: Dict[str, List[Evidence]] = {}
    pp_max_retries: Dict[str, int] = {}
    plain_command_clis: set[str] = set()
    for session in sessions:
        include_session_silent_empty = (
            INCLUDE_SUBAGENT_FAILURES or "/subagents/" not in str(session.path)
        )
        for call in session.tool_calls:
            if call.command and not CODE_COMMAND_RE.match(call.command):
                plain_command_clis.update(pp_cli_names(call.command))
        for cli, items in session.pp_cli_invocations.items():
            invocations = [item for item in items if item.kind == "pp_cli_invocation"]
            pp_invocations.setdefault(cli, []).extend(invocations)
            for item in items:
                # Classify strictly by the kind assigned at parse time. Regexing
                # the excerpt here would re-scan invocation excerpts, which are
                # command text: `timeout 120 foo-pp-cli ...` is not a hang.
                if item.kind == "tool_failure":
                    pp_failures.setdefault(cli, []).append(item)
                elif item.kind == "pp_cli_hang":
                    pp_hangs.setdefault(cli, []).append(item)
                elif item.kind == "silent_empty" and include_session_silent_empty:
                    pp_silent_empty.setdefault(cli, []).append(item)

            # Retry-before-success is per CLI and session. Repetition alone is
            # normal use; qualify it only when the same session has failure/hang
            # evidence or one subcommand is tried with differing flag shapes.
            by_session: Dict[str, List[Evidence]] = {}
            signal_sessions = {
                item.session_id
                for item in items
                if item.kind in {"tool_failure", "pp_cli_hang"}
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
                    pp_max_retries[cli] = max(pp_max_retries.get(cli, 0), count)

    flagged_clis = sorted(
        cli
        for cli in (
            set(pp_failures)
            | set(pp_hangs)
            | set(pp_silent_empty)
            | {cli for cli, count in pp_max_retries.items() if count >= RETRY_STUCK_THRESHOLD}
        )
        if VALID_PP_CLI_RE.match(cli)
        and pp_cli_candidate_is_valid(cli, pp_root, plain_command_clis)
    )
    for cli in flagged_clis:
        failures = pp_failures.get(cli, [])
        hangs = pp_hangs.get(cli, [])
        silent_empty = pp_silent_empty.get(cli, [])
        invocations = pp_invocations.get(cli, [])
        max_retries = pp_max_retries.get(cli, 0)
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
    include_subagent_failures, validate_pp_cli_candidates,
    detect_silent_empty, silent_empty_fetch_verbs, silent_empty_ignore.
    """
    global TRACKED_CLI_SUFFIX, PP_CLI_RE, VALID_PP_CLI_RE
    global INCLUDE_SUBAGENT_FAILURES, VALIDATE_PP_CLI_CANDIDATES, DETECT_SILENT_EMPTY
    suffix = cfg.get("tracked_cli_suffix")
    if isinstance(suffix, str) and suffix:
        TRACKED_CLI_SUFFIX = suffix
        PP_CLI_RE, VALID_PP_CLI_RE = _build_tracked_cli_res(suffix)
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
    if isinstance(cfg.get("validate_pp_cli_candidates"), bool):
        VALIDATE_PP_CLI_CANDIDATES = cfg["validate_pp_cli_candidates"]
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
) -> Path:
    path = root / "review-packets" / f"{run_id}.md"
    resolution_suppressed = resolution_suppressed or []
    regressions = regressions or []
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
        f"- Sessions with signals: {len(sessions)}",
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
            sessions.append(parsed)
    parser_warnings = parser_health_warnings(parse_stats)
    for warning in parser_warnings:
        print(f"warning: {warning}", file=sys.stderr)

    pp_root_arg = getattr(args, "printing_press_root", None)
    pp_root = Path(pp_root_arg).expanduser() if pp_root_arg else None
    detected = generate_proposals(sessions, pp_root, route=args.route)
    annotate_recurrence(detected, state, resolutions)
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
        "since": since.isoformat() if since else None,
        "files_scanned": len(files),
        "sessions_with_signals": len(sessions),
        "proposal_count": len(proposals),
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

    print(f"scanned={len(files)} sessions_with_signals={len(sessions)} proposals={len(proposals)}")
    print(
        f"resolved_suppressed={len(filtered.suppressed)} "
        f"targets={','.join(suppressed_targets) or '-'} regressions={len(filtered.regressions)}"
    )
    print(f"review_packet={packet_path}")
    if proposals:
        print(f"proposal_dir={proposal_dir}")
    return 0


def manage_resolutions(args: argparse.Namespace) -> int:
    root = Path(args.output_root).expanduser()
    actor = str(args.by or os.environ.get("USER") or "unknown")
    try:
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
    parser.add_argument(
        "--include-resolved",
        action="store_true",
        help="Debug bypass: emit already-resolved proposals (seen-key filtering still applies)",
    )
    parser.add_argument("--full", action="store_true", help="Keep full, unredacted excerpts inline (local use only; do not share the output)")
    parser.add_argument(
        "--printing-press-root",
        default=os.environ.get("PRINTING_PRESS_ROOT", PRINTING_PRESS_ROOT_DEFAULT),
        help="Root of the printing-press CLI tree; tool proposals point at the matching CLI source (default ~/printing-press)",
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=(
            "JSON config overriding detector defaults (tracked_cli_suffix, "
            "extra_scaffold_markers, extra_redaction_patterns, extra_backlog_ignore, "
            "extra_remote_command_wrappers, include_subagent_failures, "
            "validate_pp_cli_candidates, detect_silent_empty, "
            "silent_empty_fetch_verbs, silent_empty_ignore)"
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print JSON and do not write queue files")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--resolve", metavar="ROUTE:TARGET", help="Record a target resolution")
    actions.add_argument("--resolve-from", metavar="PATH", help="Import structured decisions JSON")
    actions.add_argument("--list-resolutions", action="store_true", help="Print the resolutions registry")
    actions.add_argument("--unresolve", metavar="ROUTE:TARGET", help="Remove a target resolution")
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
