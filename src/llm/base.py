"""Provider-agnostic types, prompt builder, and response parser."""
from __future__ import annotations

import json
import logging
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


class LLMProvider(Protocol):
    name: str
    model: str

    def decide(self, context: CommentContext) -> Decision: ...


SYSTEM_PROMPT = (
    "You triage GitHub Copilot pull request review comments. "
    "Return only valid JSON matching the requested shape. "
    "If the comment identifies a real, safely fixable defect, return full "
    "replacement contents for every file you change. You may change multiple "
    "files when an edit requires it (for example a caller plus its test). "
    "If the comment is wrong, obsolete, stylistic noise, or not safely fixable "
    "with the provided context, do not change code. Never mention AI, "
    "automation, webhooks, or bots in replies or commit messages. "
    "Commit messages MUST follow the Conventional Commits 1.0.0 spec: "
    "`<type>(<scope>)?: <subject>`. Use one of these types: "
    "feat, fix, chore, refactor, docs, test, style, perf, build, ci. "
    "Subject is imperative, no trailing period, ideally <=72 chars. "
    "If a scope is obvious from the changed file (e.g. the directory name "
    "or the kustomize app), include it; otherwise omit. Examples: "
    "`fix(atlantis): mount GCP SA key so terragrunt can read GCS state`, "
    "`chore(deps): pin mikrotik-minder Helm chart to 0.1.0`, "
    "`feat: add comment-commander GitRepository`."
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
            "Use decision=fix only if you are confident the comment is a real defect and you can fix it with the provided files.",
            "Use decision=dismiss when the comment is a false positive, stylistic noise, obsolete, or already handled.",
            "Use decision=skip when more repository context is required to fix safely.",
            "For fix: include FULL replacement content for every file you change, not diffs.",
            "Do not wrap file content in markdown fences.",
            "Keep replies under 400 characters.",
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


def parse_decision(raw: str) -> Decision:
    text = raw.strip()
    if text.startswith("```"):
        # Strip ```json ... ``` fencing if the model added it despite the prompt.
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
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
