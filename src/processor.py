"""End-to-end webhook → clone → triage → commit → push loop."""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Settings
from github_client import (
    GitHubClient,
    GitHubError,
    RepositoryRef,
    ReviewComment,
    ReviewThread,
    parse_review_comment,
)
from llm import (
    CommentContext,
    Decision,
    FileChange,
    FileSnapshot,
    LLMError,
    LLMProvider,
)
from signing import configure_repo_signing

logger = logging.getLogger(__name__)


class ProcessorError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewJob:
    base_repo: RepositoryRef
    head_repo: RepositoryRef
    head_branch: str
    pr_number: int
    review_id: int | None
    comment: ReviewComment | None


def extract_jobs(payload: dict[str, Any], event: str, settings: Settings) -> list[ReviewJob]:
    """Convert a webhook payload to zero-or-more `ReviewJob`s."""
    repo = _parse_repo(payload.get("repository"))
    pull = payload.get("pull_request") or {}
    if not repo or not isinstance(pull, dict):
        return []
    if settings.allowed_repositories and repo.full_name not in settings.allowed_repositories:
        return []

    head = pull.get("head") or {}
    branch = head.get("ref") if isinstance(head, dict) else None
    pr_number = pull.get("number")
    if not isinstance(branch, str) or not isinstance(pr_number, int):
        return []
    head_repo = _parse_repo(head.get("repo")) or repo

    if event == "pull_request_review_comment":
        if payload.get("action") != "created":
            return []
        comment_data = payload.get("comment")
        if not isinstance(comment_data, dict):
            return []
        comment = parse_review_comment(comment_data)
        if (comment.user_login or "") not in settings.bot_logins:
            return []
        return [ReviewJob(
            base_repo=repo,
            head_repo=head_repo,
            head_branch=branch,
            pr_number=pr_number,
            review_id=None,
            comment=comment,
        )]

    if event == "pull_request_review":
        if payload.get("action") != "submitted":
            return []
        review = payload.get("review") or {}
        if not isinstance(review, dict):
            return []
        review_user = (review.get("user") or {}).get("login") if isinstance(review.get("user"), dict) else None
        review_id = review.get("id")
        if not isinstance(review_id, int) or (review_user or "") not in settings.bot_logins:
            return []
        return [ReviewJob(
            base_repo=repo,
            head_repo=head_repo,
            head_branch=branch,
            pr_number=pr_number,
            review_id=review_id,
            comment=None,
        )]

    return []


def process_jobs(
    jobs: list[ReviewJob],
    settings: Settings,
    *,
    delivery: str,
    provider: LLMProvider,
    signing_key_path: str | Path,
) -> None:
    if not jobs:
        return
    first = jobs[0]
    with GitHubClient(settings.github_pat) as gh:
        comments = _collect_comments(jobs, gh, settings)
        if not comments:
            logger.info("no_actionable_comments", extra={"delivery": delivery})
            return
        _process_pr(
            base_repo=first.base_repo,
            head_repo=first.head_repo,
            head_branch=first.head_branch,
            pr_number=first.pr_number,
            comments=comments,
            settings=settings,
            gh=gh,
            provider=provider,
            signing_key_path=signing_key_path,
            delivery=delivery,
        )


def _collect_comments(
    jobs: list[ReviewJob], gh: GitHubClient, settings: Settings
) -> list[ReviewComment]:
    out: list[ReviewComment] = []
    seen: set[int] = set()
    for job in jobs[: settings.max_comments_per_event]:
        if job.comment and job.comment.id not in seen:
            out.append(job.comment)
            seen.add(job.comment.id)
            continue
        if job.review_id is None:
            continue
        try:
            review_comments = gh.list_review_comments(job.base_repo, job.pr_number, job.review_id)
        except GitHubError as exc:
            logger.warning("list_review_comments failed: %s", exc)
            continue
        for comment in review_comments:
            if comment.id in seen:
                continue
            if (comment.user_login or "") in settings.bot_logins:
                out.append(comment)
                seen.add(comment.id)
    return out[: settings.max_comments_per_event]


def _process_pr(
    *,
    base_repo: RepositoryRef,
    head_repo: RepositoryRef,
    head_branch: str,
    pr_number: int,
    comments: list[ReviewComment],
    settings: Settings,
    gh: GitHubClient,
    provider: LLMProvider,
    signing_key_path: str | Path,
    delivery: str,
) -> None:
    pending: list[tuple[ReviewComment, ReviewThread | None]] = []
    for comment in comments:
        try:
            thread = gh.find_review_thread(base_repo, pr_number, comment)
        except GitHubError as exc:
            logger.warning("find_review_thread failed: %s", exc)
            thread = None
        if thread and thread.is_resolved:
            logger.info(
                "thread_already_resolved",
                extra={"delivery": delivery, "comment_id": comment.id},
            )
            continue
        pending.append((comment, thread))

    if not pending:
        return

    with tempfile.TemporaryDirectory(prefix="comment-commander-") as tmp:
        repo_dir = Path(tmp) / "repo"
        _clone(head_repo, head_branch, repo_dir, settings)
        configure_repo_signing(
            repo_dir,
            signing_key_path,
            settings.git_author_name,
            settings.git_author_email,
        )

        replies: list[tuple[ReviewComment, ReviewThread | None, str, bool]] = []
        committed_any = False

        for comment, thread in pending:
            files = _gather_files_for_comment(repo_dir, comment, settings)
            if files is None:
                replies.append((
                    comment,
                    thread,
                    "I could not safely inspect the file referenced by this comment, so I am leaving this thread open for manual review.",
                    False,
                ))
                continue
            try:
                decision = provider.decide(_context_for(
                    base_repo=base_repo,
                    pr_number=pr_number,
                    comment=comment,
                    files=files,
                ))
            except LLMError as exc:
                logger.warning("llm_failure: %s", exc)
                replies.append((
                    comment,
                    thread,
                    "I could not produce a confident answer for this comment, so I am leaving this thread open for manual review.",
                    False,
                ))
                continue

            if decision.decision == "skip":
                replies.append((
                    comment,
                    thread,
                    decision.reply
                    or "I could not determine a safe change here without more repository context.",
                    False,
                ))
                continue

            if decision.decision == "dismiss":
                replies.append((
                    comment,
                    thread,
                    decision.reply or "This does not appear to require a code change.",
                    True,
                ))
                continue

            applied = _apply_files(repo_dir, decision.files)
            if not applied:
                replies.append((
                    comment,
                    thread,
                    decision.reply or "The current code already appears to address this.",
                    True,
                ))
                continue

            if settings.dry_run:
                logger.info(
                    "dry_run_fix",
                    extra={
                        "delivery": delivery,
                        "comment_id": comment.id,
                        "files": [f.path for f in applied],
                    },
                )
                _reset_repo(repo_dir)
                replies.append((comment, thread, decision.reply or "Dry run: change preview only.", False))
                continue

            sha = _commit_signed(repo_dir, decision, settings)
            committed_any = True
            replies.append((
                comment,
                thread,
                decision.reply or f"Addressed in {sha[:7]}.",
                True,
            ))

        if committed_any:
            _push(repo_dir, head_branch)

        for comment, thread, body, resolve in replies:
            try:
                if body.strip():
                    gh.reply_to_comment(base_repo, pr_number, comment.id, body[:2000])
                if resolve and thread and not thread.is_resolved:
                    gh.resolve_thread(thread.id)
            except GitHubError as exc:
                logger.warning("reply_or_resolve_failed: %s", exc)


def _context_for(
    *,
    base_repo: RepositoryRef,
    pr_number: int,
    comment: ReviewComment,
    files: list[FileSnapshot],
) -> CommentContext:
    return CommentContext(
        repository=base_repo.full_name,
        pr_number=pr_number,
        comment_body=comment.body,
        comment_path=comment.path,
        comment_line=comment.line,
        comment_side=comment.side,
        diff_hunk=comment.diff_hunk,
        files=files,
    )


def _gather_files_for_comment(
    repo_dir: Path, comment: ReviewComment, settings: Settings
) -> list[FileSnapshot] | None:
    target = (repo_dir / comment.path).resolve()
    try:
        target.relative_to(repo_dir.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        size = target.stat().st_size
    except OSError:
        return None
    if size > settings.max_file_bytes:
        return None
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return [FileSnapshot(path=comment.path, content=content)]


def _apply_files(repo_dir: Path, files: list[FileChange]) -> list[FileChange]:
    applied: list[FileChange] = []
    repo_root = repo_dir.resolve()
    for change in files:
        target = (repo_dir / change.path).resolve()
        try:
            target.relative_to(repo_root)
        except ValueError:
            logger.warning("rejected_path_outside_repo: %s", change.path)
            continue
        try:
            existing = target.read_text(encoding="utf-8") if target.exists() else None
        except (OSError, UnicodeDecodeError):
            existing = None
        if existing == change.content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change.content, encoding="utf-8")
        applied.append(change)
    return applied


def _clone(repo: RepositoryRef, branch: str, dest: Path, settings: Settings) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://x-access-token:{settings.github_pat}@github.com/{repo.owner}/{repo.repo}.git"
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", branch, url, str(dest)],
        check=True,
        capture_output=True,
    )


def _commit_signed(repo_dir: Path, decision: Decision, settings: Settings) -> str:
    subprocess.run(
        ["git", "-C", str(repo_dir), "add", "--all"],
        check=True,
        capture_output=True,
    )
    message = _commit_subject(decision)
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "-S", "-m", message],
        check=True,
        capture_output=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return sha


def _push(repo_dir: Path, branch: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo_dir), "push", "origin", f"HEAD:refs/heads/{branch}"],
        check=True,
        capture_output=True,
    )


def _reset_repo(repo_dir: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo_dir), "reset", "--hard", "HEAD"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "clean", "-fd"],
        check=True,
        capture_output=True,
    )


def _commit_subject(decision: Decision) -> str:
    subject = (decision.commit_message or "Address PR review feedback").splitlines()[0].strip()
    return subject or "Address PR review feedback"


def _parse_repo(value: Any) -> RepositoryRef | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    owner = value.get("owner")
    owner_login = owner.get("login") if isinstance(owner, dict) else None
    if not isinstance(name, str) or not isinstance(owner_login, str):
        return None
    return RepositoryRef(owner=owner_login, repo=name)
