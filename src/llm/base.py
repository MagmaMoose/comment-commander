"""Provider-agnostic types, prompt builder, and response parser."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    content: str


@dataclass(frozen=True)
class FileChange:
    path: str
    content: str


@dataclass(frozen=True)
class CommentContext:
    repository: str
    pr_number: int
    comment_body: str
    comment_path: str
    comment_line: int | None
    comment_side: str | None
    diff_hunk: str | None
    files: list[FileSnapshot] = field(default_factory=list)


@dataclass(frozen=True)
class Decision:
    decision: Literal["fix", "dismiss", "skip"]
    reply: str
    commit_message: str | None = None
    files: list[FileChange] = field(default_factory=list)


@dataclass(frozen=True)
class MergeConflictContext:
    repository: str
    pr_number: int
    base_branch: str
    head_branch: str
    conflicted_files: list[FileSnapshot] = field(default_factory=list)


@dataclass(frozen=True)
class MergeResolution:
    decision: Literal["resolve", "abort"]
    reason: str
    commit_message: str | None = None
    files: list[FileChange] = field(default_factory=list)


class LLMProvider(Protocol):
    name: str
    model: str

    def decide(self, context: CommentContext) -> Decision: ...
    def resolve_merge(self, context: MergeConflictContext) -> MergeResolution: ...


SYSTEM_PROMPT = (
    "You triage GitHub Copilot pull request review comments. Return only "
    "valid JSON matching the requested shape — no prose, no chain-of-thought "
    "outside the JSON. "
    "Default to action. If the comment identifies a real defect AND the fix "
    "is contained to file(s) shown in this prompt, choose decision=fix and "
    "return full replacement contents for every file you change (multi-file "
    "edits are fine when one change requires another, e.g. a caller plus its "
    "test). Trivial mechanical edits — renaming or removing an unused "
    "parameter, routing a value through an existing helper (e.g. redacting "
    "with _redact_pat), adding a one-line validation, removing dead code, "
    "applying an obvious typo or null-check, dropping a redundant flag — are "
    "exactly what fix is for. "
    "Choose decision=dismiss when the comment is wrong, obsolete, stylistic "
    "noise, or the existing code is already correct. "
    "Choose decision=skip ONLY when answering correctly requires code that "
    "is NOT in the provided files. Caution or general risk-aversion is not a "
    "reason to skip — if you can see the relevant code, you can fix it. "
    "Never mention AI, automation, webhooks, or bots in replies or commit "
    "messages. Commit messages MUST follow Conventional Commits 1.0.0: "
    "`<type>(<scope>)?: <subject>`. Types: "
    "feat, fix, chore, refactor, docs, test, style, perf, build, ci. "
    "Subject is imperative, no trailing period, ≤72 chars. Include a scope "
    "when obvious from the changed file (directory name, kustomize app); "
    "otherwise omit. Examples: "
    "`fix(atlantis): mount GCP SA key so terragrunt can read GCS state`, "
    "`chore(deps): pin mikrotik-minder Helm chart to 0.1.0`, "
    "`feat: add comment-commander GitRepository`."
)


MERGE_SYSTEM_PROMPT = (
    "You resolve merge conflicts in a git repository. Return only valid JSON "
    "matching the requested shape — no prose, no chain-of-thought outside "
    "the JSON. "
    "You will be given files containing standard git conflict markers "
    "(`<<<<<<<`, `=======`, `>>>>>>>`) and the names of the base and head "
    "branches. Default to action: if every conflict has an obvious correct "
    "merge — keep behaviour, prefer the side carrying the head branch's "
    "intent, preserve both sides when they're complementary (e.g. two "
    "imports added independently, two list entries) — return "
    "decision=resolve with the full resolved contents for every conflicted "
    "file. Conflict markers MUST NOT appear in the returned content. "
    "Choose decision=abort ONLY when semantics genuinely diverge (a refactor "
    "collided with a feature, two incompatible behaviour changes touch the "
    "same lines) such that picking either side would silently break code. "
    "General caution is not a reason to abort. "
    "Never mention AI, automation, or bots in the commit message. "
    "Commit messages MUST follow Conventional Commits 1.0.0 — "
    "use `fix(merge): resolve conflicts with <base_branch>` or similar."
)


def build_user_prompt(context: CommentContext) -> str:
    payload = {
        "required_response_shape": {
            "decision": "fix | dismiss | skip",
            "reply": "short markdown body for the GitHub review thread",
            "commitMessage": "required for fix; Conventional Commits 1.0.0 — `<type>(<scope>)?: <subject>` (types: feat, fix, chore, refactor, docs, test, style, perf, build, ci)",
            "files": [
                {
                    "path": "repo-relative path (must match one of the provided files OR a sibling clearly required by the fix)",
                    "content": "full file contents after the change",
                }
            ],
        },
        "rules": [
            "Prefer decision=fix when the edit is contained to the file(s) shown — bias toward acting.",
            "Use decision=dismiss when the comment is wrong, obsolete, stylistic noise, or already addressed.",
            "Use decision=skip ONLY when fixing would require code that is not in the provided files. Caution or risk-aversion is not a valid reason to skip.",
            "For fix: include FULL replacement content for every file you change, not diffs.",
            "Do not wrap file content in markdown fences.",
            "Keep replies under 400 characters and never use the words AI, automation, webhook, or bot.",
        ],
        "repository": context.repository,
        "pull_request": context.pr_number,
        "comment": {
            "body": context.comment_body,
            "path": context.comment_path,
            "line": context.comment_line,
            "side": context.comment_side,
            "diff_hunk": context.diff_hunk,
        },
        "files": [{"path": f.path, "content": f.content} for f in context.files],
    }
    return json.dumps(payload, ensure_ascii=False)


_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)


def _extract_json_body(raw: str) -> str:
    """Strip a leading chain-of-thought block (reasoning models like
    DeepSeek-R1 / deepseek-reasoner can prepend `<think>…</think>` even
    when the system prompt forbids it) and any surrounding markdown
    fences, so the inner JSON object survives for `json.loads`."""
    text = _THINK_BLOCK_RE.sub("", raw).strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def parse_decision(raw: str) -> Decision:
    text = _extract_json_body(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Provider returned non-JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise LLMError("Provider returned a non-object JSON payload")

    decision = data.get("decision")
    if decision not in {"fix", "dismiss", "skip"}:
        raise LLMError(f"Invalid decision value: {decision!r}")

    files_raw = data.get("files") if isinstance(data.get("files"), list) else []
    files: list[FileChange] = []
    for entry in files_raw:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        content = entry.get("content")
        if isinstance(path, str) and isinstance(content, str):
            files.append(FileChange(path=path, content=content))

    return Decision(
        decision=decision,
        reply=data.get("reply") or "",
        commit_message=data.get("commitMessage") if isinstance(data.get("commitMessage"), str) else None,
        files=files,
    )


def build_merge_user_prompt(context: MergeConflictContext) -> str:
    payload = {
        "required_response_shape": {
            "decision": "resolve | abort",
            "reason": "short string explaining the call",
            "commitMessage": "required for resolve; Conventional Commits 1.0.0 — e.g. `fix(merge): resolve conflicts with <base_branch>`",
            "files": [
                {
                    "path": "repo-relative path (MUST match one of the provided conflicted files)",
                    "content": "full resolved file contents — no conflict markers",
                }
            ],
        },
        "rules": [
            "Prefer decision=resolve when every conflict has an obvious merge — bias toward acting.",
            "decision=resolve requires full resolved content for EVERY conflicted file listed below.",
            "decision=abort ONLY when picking a side would change semantics in a way that would silently break code. General caution is not a reason to abort.",
            "Strip all `<<<<<<<`, `=======`, `>>>>>>>` lines from resolved content — markered content is rejected.",
            "Do not introduce unrelated changes; preserve formatting and trailing newlines.",
            "Do not wrap content in markdown fences.",
        ],
        "repository": context.repository,
        "pull_request": context.pr_number,
        "base_branch": context.base_branch,
        "head_branch": context.head_branch,
        "conflicted_files": [
            {"path": f.path, "content": f.content} for f in context.conflicted_files
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_merge_resolution(raw: str) -> MergeResolution:
    text = _extract_json_body(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Provider returned non-JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise LLMError("Provider returned a non-object JSON payload")

    decision = data.get("decision")
    if decision not in {"resolve", "abort"}:
        raise LLMError(f"Invalid merge decision value: {decision!r}")

    files_raw = data.get("files") if isinstance(data.get("files"), list) else []
    files: list[FileChange] = []
    for entry in files_raw:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        content = entry.get("content")
        if isinstance(path, str) and isinstance(content, str):
            files.append(FileChange(path=path, content=content))

    return MergeResolution(
        decision=decision,
        reason=data.get("reason") if isinstance(data.get("reason"), str) else "",
        commit_message=data.get("commitMessage") if isinstance(data.get("commitMessage"), str) else None,
        files=files,
    )
