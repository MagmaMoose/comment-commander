"""End-to-end webhook → clone → triage → commit → push loop."""
from __future__ import annotations

import json
import logging
import re
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
from slack import SlackNotifier

logger = logging.getLogger(__name__)


# Hidden marker appended to every bot reply. Lets the manual-trigger code
# path skip threads we've already touched without relying on user-name
# matching (which can't tell the bot's commits/comments apart from Caleb's
# own, since both use the same identity).
BOT_MARKER = "<!-- comment-commander -->"

# Accepts:
#   https://github.com/owner/repo/pull/123
#   https://github.com/owner/repo/pull/123/files
#   https://github.com/owner/repo/pull/123#discussion_r456
# Plus the lighter `owner/repo#123` shorthand.
_PR_URL_RE = re.compile(
    r"^(?:https?://github\.com/)?(?P<owner>[^/]+)/(?P<repo>[^/#]+)(?:/pull/|#)(?P<num>\d+)"
)


class ProcessorError(RuntimeError):
    pass


def parse_pr_url(value: str) -> tuple[RepositoryRef, int] | None:
    """Parse a PR URL or owner/repo#N shorthand. Returns None if invalid."""
    if not isinstance(value, str):
        return None
    match = _PR_URL_RE.match(value.strip())
    if not match:
        return None
    return (
        RepositoryRef(owner=match.group("owner"), repo=match.group("repo")),
        int(match.group("num")),
    )


def _format_reply_body(body: str) -> str:
    """Append the bot marker so manual reruns can skip our own replies."""
    body = body.strip()
    if not body or BOT_MARKER in body:
        return body
    return f"{body}\n\n{BOT_MARKER}"


@dataclass(frozen=True)
class ReviewJob:
    base_repo: RepositoryRef
    head_repo: RepositoryRef
    head_branch: str
    pr_number: int
    review_id: int | None
    comment: ReviewComment | None


@dataclass
class _PendingReply:
    """In-flight bot action queued until after the optional push.

    `decision` carries the LLM verdict (fix/dismiss/skip) for downstream
    notifications. `None` means we couldn't even reach a verdict (file
    unreadable / LLM error) — no Slack ping in that case.
    """
    comment: ReviewComment
    thread: ReviewThread | None
    body: str
    resolve: bool
    decision: Any = None  # Literal["fix", "dismiss", "skip"] | None
    commit_sha: str | None = None
    commit_subject: str | None = None
    reply_text: str = ""


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
        if (comment.user_login or "").lower() not in settings.bot_logins:
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
        if not isinstance(review_id, int) or (review_user or "").lower() not in settings.bot_logins:
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


def process_pr_manual(
    repo: RepositoryRef,
    pr_number: int,
    settings: Settings,
    *,
    trigger_id: str,
    provider: LLMProvider,
    signing_key_path: str | Path,
    slack: SlackNotifier | None = None,
) -> None:
    """Manual triggered processing — re-walks every unresolved review thread.

    Differences vs the webhook flow:
    - Author isn't restricted to BOT_LOGINS; any non-bot author counts
      (humans, Copilot, other review bots).
    - Thread-reply comments (the ones with in_reply_to_id set) are
      skipped so we only act on thread starters.
    - Comments carrying the `BOT_MARKER` are skipped so we don't recurse
      into our own past replies.
    - `_process_pr` then drops anything already resolved.
    """
    if settings.allowed_repositories and repo.full_name not in settings.allowed_repositories:
        logger.info(
            "manual trigger refused — repo not in allow-list trigger=%s repo=%s",
            trigger_id, repo.full_name,
        )
        return
    logger.info(
        "manual trigger received trigger=%s repo=%s pr=%s",
        trigger_id, repo.full_name, pr_number,
    )
    with GitHubClient(settings.github_pat) as gh:
        try:
            pr = gh.get_pull_request(repo, pr_number)
        except GitHubError as exc:
            logger.warning("get_pull_request failed trigger=%s: %s", trigger_id, exc)
            return
        head_ref = pr.get("head") or {}
        head_branch = head_ref.get("ref") if isinstance(head_ref, dict) else None
        head_repo = _parse_repo(head_ref.get("repo")) if isinstance(head_ref, dict) else None
        head_repo = head_repo or repo
        if not isinstance(head_branch, str) or not head_branch:
            logger.warning(
                "manual trigger: PR has no head ref trigger=%s repo=%s pr=%s",
                trigger_id, repo.full_name, pr_number,
            )
            return

        try:
            all_comments = gh.list_pr_review_comments(repo, pr_number)
        except GitHubError as exc:
            logger.warning("list_pr_review_comments failed trigger=%s: %s", trigger_id, exc)
            return

        actionable = [
            c for c in all_comments
            if c.in_reply_to_id is None and BOT_MARKER not in c.body
        ]
        logger.info(
            "manual trigger pre-filter total=%d actionable=%d (replies+bot-markers excluded) trigger=%s",
            len(all_comments), len(actionable), trigger_id,
        )
        if not actionable:
            return

        actionable = actionable[: settings.max_comments_per_event]
        _process_pr(
            base_repo=repo,
            head_repo=head_repo,
            head_branch=head_branch,
            pr_number=pr_number,
            comments=actionable,
            settings=settings,
            gh=gh,
            provider=provider,
            signing_key_path=signing_key_path,
            delivery=f"manual:{trigger_id}",
            slack=slack or SlackNotifier(token=None, channel=None),
        )


def process_jobs(
    jobs: list[ReviewJob],
    settings: Settings,
    *,
    delivery: str,
    provider: LLMProvider,
    signing_key_path: str | Path,
    slack: SlackNotifier | None = None,
) -> None:
    if not jobs:
        return
    first = jobs[0]
    logger.info(
        "processing webhook delivery=%s repo=%s pr=%s jobs=%d",
        delivery, first.base_repo.full_name, first.pr_number, len(jobs),
    )
    with GitHubClient(settings.github_pat) as gh:
        comments = _collect_comments(jobs, gh, settings)
        if not comments:
            logger.info(
                "no actionable comments delivery=%s repo=%s pr=%s",
                delivery, first.base_repo.full_name, first.pr_number,
            )
            return
        logger.info(
            "collected %d Copilot comment(s) delivery=%s repo=%s pr=%s",
            len(comments), delivery, first.base_repo.full_name, first.pr_number,
        )
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
            slack=slack or SlackNotifier(token=None, channel=None),
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
            if (comment.user_login or "").lower() in settings.bot_logins:
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
    slack: SlackNotifier,
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
                "skipping resolved thread comment_id=%s pr=%s repo=%s",
                comment.id, pr_number, base_repo.full_name,
            )
            continue
        pending.append((comment, thread))

    if not pending:
        logger.info(
            "all threads resolved already pr=%s repo=%s",
            pr_number, base_repo.full_name,
        )
        return

    logger.info(
        "pending comments after thread filter pending=%d pr=%s repo=%s",
        len(pending), pr_number, base_repo.full_name,
    )

    with tempfile.TemporaryDirectory(prefix="comment-commander-") as tmp:
        repo_dir = Path(tmp) / "repo"
        _clone(head_repo, head_branch, repo_dir, settings)
        logger.info(
            "cloned repo=%s branch=%s dir=%s",
            head_repo.full_name, head_branch, repo_dir,
        )
        configure_repo_signing(
            repo_dir,
            signing_key_path,
            settings.git_author_name,
            settings.git_author_email,
        )

        replies: list[_PendingReply] = []
        committed_any = False

        for comment, thread in pending:
            logger.info(
                "processing comment id=%s path=%s line=%s",
                comment.id, comment.path, comment.line,
            )
            files = _gather_files_for_comment(repo_dir, comment, settings)
            if files is None:
                logger.warning(
                    "could not read commented file comment_id=%s path=%s",
                    comment.id, comment.path,
                )
                replies.append(_PendingReply(
                    comment=comment, thread=thread,
                    body="I could not safely inspect the file referenced by this comment, so I am leaving this thread open for manual review.",
                    resolve=False, decision=None,
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
                logger.warning("LLM call failed comment_id=%s: %s", comment.id, exc)
                replies.append(_PendingReply(
                    comment=comment, thread=thread,
                    body="I could not produce a confident answer for this comment, so I am leaving this thread open for manual review.",
                    resolve=False, decision=None,
                ))
                continue

            logger.info(
                "llm decision comment_id=%s decision=%s files=%d",
                comment.id, decision.decision, len(decision.files),
            )

            if decision.decision == "skip":
                replies.append(_PendingReply(
                    comment=comment, thread=thread,
                    body=decision.reply
                    or "I could not determine a safe change here without more repository context.",
                    resolve=False, decision="skip", reply_text=decision.reply,
                ))
                continue

            if decision.decision == "dismiss":
                replies.append(_PendingReply(
                    comment=comment, thread=thread,
                    body=decision.reply or "This does not appear to require a code change.",
                    resolve=True, decision="dismiss", reply_text=decision.reply,
                ))
                continue

            applied = _apply_files(repo_dir, decision.files)
            logger.info(
                "applied file changes comment_id=%s changed=%d paths=%s",
                comment.id, len(applied), [c.path for c in applied],
            )
            if not applied:
                replies.append(_PendingReply(
                    comment=comment, thread=thread,
                    body=decision.reply or "The current code already appears to address this.",
                    resolve=True, decision="dismiss", reply_text=decision.reply,
                ))
                continue

            if settings.dry_run:
                logger.info(
                    "DRY_RUN — skipping commit comment_id=%s files=%s",
                    comment.id, [f.path for f in applied],
                )
                _reset_repo(repo_dir)
                replies.append(_PendingReply(
                    comment=comment, thread=thread,
                    body=decision.reply or "Dry run: change preview only.",
                    resolve=False, decision="skip", reply_text=decision.reply,
                ))
                continue

            sha = _commit_signed(repo_dir, decision, settings)
            subject = _commit_subject(decision)
            logger.info(
                "signed commit sha=%s subject=%r comment_id=%s",
                sha[:7], subject, comment.id,
            )
            committed_any = True
            replies.append(_PendingReply(
                comment=comment, thread=thread,
                body=decision.reply or f"Addressed in {sha[:7]}.",
                resolve=True, decision="fix",
                commit_sha=sha, commit_subject=subject,
                reply_text=decision.reply,
            ))

        if committed_any:
            _push(repo_dir, head_branch)
            logger.info("pushed branch=%s repo=%s", head_branch, head_repo.full_name)

        for pending_reply in replies:
            replied = False
            resolved = False
            try:
                marked = _format_reply_body(pending_reply.body)
                if marked.strip():
                    gh.reply_to_comment(
                        base_repo, pr_number, pending_reply.comment.id,
                        marked[:2000],
                    )
                    replied = True
                if pending_reply.resolve and pending_reply.thread and not pending_reply.thread.is_resolved:
                    gh.resolve_thread(pending_reply.thread.id)
                    resolved = True
            except GitHubError as exc:
                logger.warning(
                    "reply/resolve failed comment_id=%s: %s",
                    pending_reply.comment.id, exc,
                )
                continue
            logger.info(
                "thread handled comment_id=%s replied=%s resolved=%s",
                pending_reply.comment.id, replied, resolved,
            )
            if pending_reply.decision is not None:
                slack.notify_decision(
                    decision=pending_reply.decision,
                    repo=base_repo.full_name,
                    pr_number=pr_number,
                    comment_id=pending_reply.comment.id,
                    comment_path=pending_reply.comment.path,
                    comment_line=pending_reply.comment.line,
                    commit_sha=pending_reply.commit_sha,
                    commit_subject=pending_reply.commit_subject,
                    reply=pending_reply.reply_text or "",
                )


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
    _run(["git", "clone", "--depth", "1", "--branch", branch, url, str(dest)])


def _commit_signed(repo_dir: Path, decision: Decision, settings: Settings) -> str:
    _run(["git", "-C", str(repo_dir), "add", "--all"])
    message = _commit_subject(decision)
    _run(["git", "-C", str(repo_dir), "commit", "-S", "-m", message])
    return _run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"]).stdout.strip()


def _push(repo_dir: Path, branch: str) -> None:
    _run(["git", "-C", str(repo_dir), "push", "origin", f"HEAD:refs/heads/{branch}"])


def _reset_repo(repo_dir: Path) -> None:
    _run(["git", "-C", str(repo_dir), "reset", "--hard", "HEAD"])
    _run(["git", "-C", str(repo_dir), "clean", "-fd"])


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """subprocess.run wrapper that captures + logs stderr on non-zero exits.

    Without this, git/ssh-keygen failures bubble up as opaque
    `CalledProcessError: ... returned non-zero exit status 128` with no
    way to see what actually went wrong.
    """
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        # `git clone` with a URL containing the PAT must not leak it.
        sanitised = [_redact_pat(a) for a in cmd]
        logger.error(
            "subprocess_failed cmd=%r rc=%s stderr=%s stdout=%s",
            sanitised,
            exc.returncode,
            (exc.stderr or "").strip(),
            (exc.stdout or "").strip(),
        )
        raise


def _redact_pat(arg: str) -> str:
    if "x-access-token:" in arg:
        return "https://x-access-token:***@github.com/.../...git"
    return arg


_CONVENTIONAL_COMMIT_TYPES = (
    "feat", "fix", "chore", "refactor", "docs", "test",
    "style", "perf", "build", "ci", "revert",
)
_CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(" + "|".join(_CONVENTIONAL_COMMIT_TYPES) + r")(\([^)]+\))?!?:\s+\S"
)


def _commit_subject(decision: Decision) -> str:
    """Normalise the LLM's commit subject into a Conventional Commits header.

    The system prompt instructs the model to comply, but we don't trust
    that — when it forgets, prefix `fix:` (most Copilot findings are
    bug fixes) and lower-case the first letter so the subject reads
    naturally after the colon.
    """
    lines = (decision.commit_message or "").splitlines()
    raw = (lines[0] if lines else "").strip().rstrip(".")
    if not raw:
        return "fix: address PR review feedback"
    if _CONVENTIONAL_COMMIT_RE.match(raw):
        return raw
    head = raw[0].lower() + raw[1:] if raw[0].isupper() else raw
    return f"fix: {head}"


def _parse_repo(value: Any) -> RepositoryRef | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    owner = value.get("owner")
    owner_login = owner.get("login") if isinstance(owner, dict) else None
    if not isinstance(name, str) or not isinstance(owner_login, str):
        return None
    return RepositoryRef(owner=owner_login, repo=name)
