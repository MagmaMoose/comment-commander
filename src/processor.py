"""End-to-end webhook → clone → triage → commit → push loop."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Settings
from github_client import (
    GitHubClient,
    GitHubError,
    GitHubInstance,
    RepositoryRef,
    ReviewComment,
    ReviewThread,
    find_instance_for_payload,
    parse_review_comment,
)
from llm import (
    CommentContext,
    Decision,
    FileChange,
    FileSnapshot,
    LLMError,
    LLMProvider,
    MergeConflictContext,
    MergeResolution,
)
from signing import configure_repo_signing
from slack import SlackNotifier
from triggers import TriggerResult

logger = logging.getLogger(__name__)


# Hidden marker appended to every bot reply. Lets the manual-trigger code
# path skip threads we've already touched without relying on user-name
# matching (which can't tell the bot's commits/comments apart from Caleb's
# own, since both use the same identity).
BOT_MARKER = "<!-- comment-commander -->"

# Accepts any host — github.com and GitHub Enterprise (e.g. *.ghe.com) alike:
#   https://github.com/owner/repo/pull/123
#   https://github.com/owner/repo/pull/123/files
#   https://github.com/owner/repo/pull/123#discussion_r456
#   https://pinkroccade.ghe.com/owner/repo/pull/123
# Plus the lighter `owner/repo#123` shorthand. The host is matched but not
# captured here — main.py's _host_from_pr_url extracts it for instance
# routing, and find_instance_for_host rejects hosts we aren't configured for.
_PR_URL_RE = re.compile(
    r"^(?:https?://[^/]+/)?(?P<owner>[^/]+)/(?P<repo>[^/#]+)(?:/pull/|#)(?P<num>\d+)"
)

# Serializes the clone -> commit -> push -> resolve critical section. GitHub
# fans one review out into several webhook deliveries (a pull_request_review
# event plus one pull_request_review_comment per comment); processing them
# concurrently raced on `git push` to the same branch, and the losing
# deliveries silently dropped their commit, reply and thread-resolve.
_PROCESS_LOCK = threading.Lock()


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
    instance: GitHubInstance
    base_repo: RepositoryRef
    head_repo: RepositoryRef
    head_branch: str
    base_branch: str
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
    """Convert a webhook payload to zero-or-more `ReviewJob`s.

    Picks the matching GitHubInstance (github.com or GHE) from the
    payload's URLs. Returns [] if no instance matches — i.e. webhook
    came from a host the bot wasn't configured against.
    """
    instance = find_instance_for_payload(payload, settings.instances)
    if instance is None:
        return []
    repo = _parse_repo(payload.get("repository"))
    pull = payload.get("pull_request") or {}
    if not repo or not isinstance(pull, dict):
        return []
    if settings.allowed_repositories and repo.full_name not in settings.allowed_repositories:
        return []

    head = pull.get("head") or {}
    branch = head.get("ref") if isinstance(head, dict) else None
    base = pull.get("base") or {}
    base_branch = base.get("ref") if isinstance(base, dict) else None
    pr_number = pull.get("number")
    if not isinstance(branch, str) or not isinstance(pr_number, int):
        return []
    if not isinstance(base_branch, str) or not base_branch:
        base_branch = ""  # follow-up merge resolution will no-op without it
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
            instance=instance,
            base_repo=repo,
            head_repo=head_repo,
            head_branch=branch,
            base_branch=base_branch,
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
            instance=instance,
            base_repo=repo,
            head_repo=head_repo,
            head_branch=branch,
            base_branch=base_branch,
            pr_number=pr_number,
            review_id=review_id,
            comment=None,
        )]

    return []


def user_has_commits(
    gh: GitHubClient,
    repo: RepositoryRef,
    pr_number: int,
    involved_users: frozenset[str],
) -> bool:
    """True iff any commit on the PR is authored or committed by one of
    `involved_users` (case-insensitive logins). Used to decide whether
    the webhook flow should act on this PR — bypassed by /process.
    """
    if not involved_users:
        return True  # whitelist disabled = always process
    try:
        commits = gh.list_pr_commits(repo, pr_number)
    except GitHubError as exc:
        logger.warning("user_has_commits: list_pr_commits failed: %s", exc)
        return True  # fail open — better to over-process than miss the user's PR
    for commit in commits:
        for role in ("author", "committer"):
            actor = commit.get(role)
            if isinstance(actor, dict):
                login = (actor.get("login") or "").lower()
                if login in involved_users:
                    return True
    return False


def process_pr_manual(
    instance: GitHubInstance,
    repo: RepositoryRef,
    pr_number: int,
    settings: Settings,
    *,
    trigger_id: str,
    provider: LLMProvider,
    signing_key_path: str | Path,
    slack: SlackNotifier | None = None,
    result: TriggerResult | None = None,
) -> None:
    """Manual triggered processing — re-walks every unresolved review thread.

    Differences vs the webhook flow:
    - Author isn't restricted to BOT_LOGINS; any non-bot author counts
      (humans, Copilot, other review bots).
    - Thread-reply comments (in_reply_to_id set) are skipped.
    - Comments carrying BOT_MARKER are skipped so we don't recurse into
      our own past replies.
    - INVOLVED_USERS whitelist is NOT applied (manual = explicit intent).
    """
    if settings.allowed_repositories and repo.full_name not in settings.allowed_repositories:
        logger.info(
            "manual trigger refused — repo not in allow-list trigger=%s repo=%s",
            trigger_id, repo.full_name,
        )
        return
    logger.info(
        "manual trigger received trigger=%s instance=%s repo=%s pr=%s",
        trigger_id, instance.name, repo.full_name, pr_number,
    )
    with GitHubClient.for_instance(instance) as gh:
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
        base_ref_obj = pr.get("base") or {}
        base_branch = base_ref_obj.get("ref") if isinstance(base_ref_obj, dict) else None
        if not isinstance(base_branch, str):
            base_branch = ""

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

        # No max_comments cap here: _process_pr applies it AFTER the
        # resolved-thread filter. Capping the raw `actionable` list first
        # meant a re-walk of a PR whose oldest N comments are all resolved
        # could never reach an unresolved comment sitting past position N.
        _process_pr(
            instance=instance,
            base_repo=repo,
            head_repo=head_repo,
            head_branch=head_branch,
            base_branch=base_branch,
            pr_number=pr_number,
            comments=actionable,
            settings=settings,
            gh=gh,
            provider=provider,
            signing_key_path=signing_key_path,
            delivery=f"manual:{trigger_id}",
            slack=slack or SlackNotifier(token=None, channel=None),
            result=result,
        )


def process_jobs(
    jobs: list[ReviewJob],
    settings: Settings,
    *,
    delivery: str,
    provider: LLMProvider,
    signing_key_path: str | Path,
    slack: SlackNotifier | None = None,
    result: TriggerResult | None = None,
) -> None:
    if not jobs:
        return
    first = jobs[0]
    instance = first.instance
    logger.info(
        "processing webhook delivery=%s instance=%s repo=%s pr=%s jobs=%d",
        delivery, instance.name, first.base_repo.full_name, first.pr_number, len(jobs),
    )
    with GitHubClient.for_instance(instance) as gh:
        # INVOLVED_USERS whitelist (webhook flow only — /process bypasses).
        if not user_has_commits(gh, first.base_repo, first.pr_number, settings.involved_users):
            logger.info(
                "skipping PR — no commits by INVOLVED_USERS=%s delivery=%s repo=%s pr=%s",
                sorted(settings.involved_users), delivery,
                first.base_repo.full_name, first.pr_number,
            )
            return
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
            instance=instance,
            base_repo=first.base_repo,
            head_repo=first.head_repo,
            head_branch=first.head_branch,
            base_branch=first.base_branch,
            pr_number=first.pr_number,
            comments=comments,
            settings=settings,
            gh=gh,
            provider=provider,
            signing_key_path=signing_key_path,
            delivery=delivery,
            slack=slack or SlackNotifier(token=None, channel=None),
            result=result,
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


def _collect_sweep_comments(
    gh: GitHubClient,
    base_repo: RepositoryRef,
    pr_number: int,
    already_handled_ids: set[int],
    settings: Settings,
) -> list[ReviewComment]:
    """Catch unresolved bot comments the webhook payload missed.

    Webhook handlers only see the comments their own event delivered. If a
    Copilot review has >max_comments_per_event items, the surplus is silently
    dropped by `_collect_comments`. Webhook deliveries can also be lost
    (e.g. during a tunnel outage). Either way the user has to manually re-run
    /process to clean up — this sweep eliminates that pattern.

    Returns *candidate* comments only. Caller still runs each through
    find_review_thread, so already-resolved threads are skipped.
    """
    try:
        all_comments = gh.list_pr_review_comments(base_repo, pr_number)
    except GitHubError as exc:
        logger.warning(
            "sweep list_pr_review_comments failed pr=%s repo=%s err=%s",
            pr_number, base_repo.full_name, exc,
        )
        return []
    return [
        c for c in all_comments
        if c.id not in already_handled_ids
        and c.in_reply_to_id is None
        and BOT_MARKER not in c.body
        and (c.user_login or "").lower() in settings.bot_logins
    ]


def _process_pr(**kwargs: Any) -> None:
    """Serialized entrypoint for PR processing — see _PROCESS_LOCK.

    GitHub fans one review out into several webhook deliveries; without
    serialization their _process_pr_locked runs raced on `git push` to the
    same branch and the losing deliveries dropped their commit, reply and
    thread-resolve. A queued delivery re-reads thread state on entry, so
    anything already handled is filtered out and it simply no-ops."""
    with _PROCESS_LOCK:
        _process_pr_locked(**kwargs)


def _process_pr_locked(
    *,
    instance: GitHubInstance,
    base_repo: RepositoryRef,
    head_repo: RepositoryRef,
    head_branch: str,
    base_branch: str,
    pr_number: int,
    comments: list[ReviewComment],
    settings: Settings,
    gh: GitHubClient,
    provider: LLMProvider,
    signing_key_path: str | Path,
    delivery: str,
    slack: SlackNotifier,
    result: TriggerResult | None = None,
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
        # Short-circuit once we have enough pending items to avoid
        # unnecessary API calls for thread lookups.
        if len(pending) >= settings.max_comments_per_event:
            break

    # Sweep — webhook deliveries only carry the comments from their own
    # event payload, so any unresolved bot comment that didn't fit in this
    # delivery (cap drop in _collect_comments, lost webhook, bot login
    # rename) sits forever until someone re-triggers /process. Pull them
    # in here so a single trigger is exhaustive.
    handled_ids = {c.id for c, _ in pending}
    for comment in _collect_sweep_comments(
        gh, base_repo, pr_number, handled_ids, settings
    ):
        try:
            thread = gh.find_review_thread(base_repo, pr_number, comment)
        except GitHubError as exc:
            logger.warning("sweep find_review_thread failed: %s", exc)
            thread = None
        if thread and thread.is_resolved:
            continue
        pending.append((comment, thread))
        logger.info(
            "sweep picked up comment_id=%s author=%s delivery=%s pr=%s repo=%s",
            comment.id, comment.user_login, delivery, pr_number,
            base_repo.full_name,
        )

    # Cap the *work*, not the comments considered. Applying max_comments
    # before the resolved-thread filter (which the manual flow used to do)
    # meant a re-walk of a PR whose first N comments are all resolved would
    # never reach an unresolved one past position N — it just no-op'd.
    # Doubled budget so a single trigger can absorb the webhook batch *and*
    # one sweep's worth of catch-up before deferring the rest to the next
    # trigger; user explicitly opted into 2× as the blast radius.
    pending = pending[: settings.max_comments_per_event * 2]

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
        _clone(instance, head_repo, head_branch, repo_dir)
        logger.info(
            "cloned instance=%s repo=%s branch=%s dir=%s",
            instance.name, head_repo.full_name, head_branch, repo_dir,
        )
        configure_repo_signing(
            repo_dir,
            signing_key_path,
            instance.author_name,
            instance.author_email,
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

        # Follow-up merge resolution. Runs in the same trigger so it can't
        # loop — the resolve-commit doesn't fan a webhook back to us. Skipped
        # silently when the kill-switch is off, in dry-run, or when we don't
        # know the base branch (older payload shapes, manual w/ no base).
        if (
            committed_any
            and base_branch
            and settings.merge_conflict_resolution
            and not settings.dry_run
        ):
            try:
                _resolve_merge_conflicts(
                    repo_dir=repo_dir,
                    instance=instance,
                    base_repo=base_repo,
                    head_repo=head_repo,
                    head_branch=head_branch,
                    base_branch=base_branch,
                    pr_number=pr_number,
                    provider=provider,
                    settings=settings,
                    slack=slack,
                    result=result,
                )
            except Exception as exc:  # noqa: BLE001
                # Comment fixes are already pushed; never break the main flow.
                logger.warning(
                    "merge-conflict resolution raised pr=%s repo=%s err=%r",
                    pr_number, base_repo.full_name, exc,
                )

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
            if result is not None:
                result.record_decision(pending_reply.decision)
            logger.info(
                "thread handled comment_id=%s replied=%s resolved=%s",
                pending_reply.comment.id, replied, resolved,
            )
            if pending_reply.decision is not None:
                slack_ref = slack.notify_decision(
                    decision=pending_reply.decision,
                    repo=base_repo.full_name,
                    pr_number=pr_number,
                    comment_id=pending_reply.comment.id,
                    comment_path=pending_reply.comment.path,
                    comment_line=pending_reply.comment.line,
                    commit_sha=pending_reply.commit_sha,
                    commit_subject=pending_reply.commit_subject,
                    reply=pending_reply.reply_text or "",
                    host=instance.host,
                )
                if result is not None:
                    result.add_slack_message(slack_ref)


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


def _clone(
    instance: GitHubInstance, repo: RepositoryRef, branch: str, dest: Path
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "git", "clone", "--depth", "1", "--branch", branch,
        instance.clone_url(repo.owner, repo.repo), str(dest),
    ])


def _commit_signed(repo_dir: Path, decision: Decision, settings: Settings) -> str:
    return _commit_with_subject(repo_dir, _commit_subject(decision))


def _commit_with_subject(repo_dir: Path, subject: str) -> str:
    """Add all + commit. Signing is driven by `commit.gpgsign=true` set by
    `configure_repo_signing` — we do not pass `-S` here because that flag
    overrides the per-repo config, which breaks tests that need unsigned
    commits and gives no production benefit (configure_repo_signing has
    already set gpgsign=true on the repo)."""
    _run(["git", "-C", str(repo_dir), "add", "--all"])
    _run(["git", "-C", str(repo_dir), "commit", "-m", subject])
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
    # Works for both github.com and GHE — match the token marker and keep
    # the rest of the URL intact so logs still show which repo we clone.
    return re.sub(r"://x-access-token:[^@]+@", "://x-access-token:***@", arg)


_ERROR_SUMMARY_MAX = 500


def summarize_exception(exc: BaseException) -> str:
    """One-line summary suitable for `TriggerResult.error`.

    Before this existed, `/process` stored only `type(exc).__name__`, which
    surfaced opaque entries like "CalledProcessError" in the runs UI with no
    hint at which git step failed. For `CalledProcessError` we now attach
    the redacted command and the first non-empty line of stderr (or stdout
    when stderr is silent — git push sometimes uses stdout for its rejection
    message). For everything else, include the exception's `str()` so
    GitHubError / LLMError messages survive."""
    name = type(exc).__name__
    if isinstance(exc, subprocess.CalledProcessError):
        cmd_args = exc.cmd if isinstance(exc.cmd, (list, tuple)) else [str(exc.cmd or "")]
        cmd = " ".join(_redact_pat(str(a)) for a in cmd_args)
        text = (exc.stderr or exc.stdout or "")
        if isinstance(text, (bytes, bytearray)):
            try:
                text = text.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - belt-and-braces, decode never raises here
                text = ""
        detail = next(
            (line.strip() for line in str(text).splitlines() if line.strip()),
            "",
        )
        summary = f"{name} rc={exc.returncode} cmd={cmd!r}"
        if detail:
            summary += f" stderr={detail!r}"
    else:
        message = str(exc).strip()
        summary = f"{name}: {message}" if message else name
    if len(summary) > _ERROR_SUMMARY_MAX:
        summary = summary[: _ERROR_SUMMARY_MAX - 1] + "…"
    return summary


_CONVENTIONAL_COMMIT_TYPES = (
    "feat", "fix", "chore", "refactor", "docs", "test",
    "style", "perf", "build", "ci", "revert",
)
_CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(" + "|".join(_CONVENTIONAL_COMMIT_TYPES) + r")(\([^)]+\))?!?:\s+\S"
)


def _commit_subject(decision: Decision) -> str:
    return _normalise_commit_subject(
        decision.commit_message, fallback="fix: address PR review feedback"
    )


def _normalise_commit_subject(raw: str | None, *, fallback: str) -> str:
    """Normalise an LLM commit subject into a Conventional Commits header.

    The system prompt instructs the model to comply, but we don't trust
    that — when it forgets, prefix `fix:` (most findings are bug fixes)
    and lower-case the first letter so the subject reads naturally
    after the colon.
    """
    lines = (raw or "").splitlines()
    first = (lines[0] if lines else "").strip().rstrip(".")
    if not first:
        return fallback
    if _CONVENTIONAL_COMMIT_RE.match(first):
        return first
    head = first[0].lower() + first[1:] if first[0].isupper() else first
    return f"fix: {head}"


def _resolve_merge_conflicts(
    *,
    repo_dir: Path,
    instance: GitHubInstance,
    base_repo: RepositoryRef,
    head_repo: RepositoryRef,
    head_branch: str,
    base_branch: str,
    pr_number: int,
    provider: LLMProvider,
    settings: Settings,
    slack: SlackNotifier,
    result: TriggerResult | None = None,
) -> None:
    """Follow-up step run after our own push. Tries to merge base into the
    PR branch; on conflict, asks the LLM for full resolved file contents,
    commits a signed merge resolution, and pushes. If anything looks
    ambiguous, aborts the merge and leaves the PR conflicted — never pushes
    a half-resolved state."""
    log_kv = (
        f"pr={pr_number} repo={base_repo.full_name} "
        f"base={base_branch} head={head_branch}"
    )

    def _notify(outcome: str, **extra: Any) -> None:
        """Local helper so the four post-outcome sites stay terse and the
        `result.add_slack_message` wiring lives in one place."""
        ref = slack.notify_merge_resolution(
            outcome=outcome,
            repo=base_repo.full_name,
            pr_number=pr_number,
            base_branch=base_branch,
            head_branch=head_branch,
            host=instance.host,
            **extra,
        )
        if result is not None:
            result.add_slack_message(ref)

    # The triage path clones --depth 1, which doesn't have enough history
    # for git to compute a merge base against an unrelated branch. Unshallow
    # once; harmless if already complete. Git's "no-op" exits are non-zero
    # with messages like "is not a shallow repository" or "--unshallow on a
    # complete repository does not make sense" — both benign, so we ignore
    # the failure entirely. Real network errors will surface at the next
    # fetch step below.
    unshallow = subprocess.run(
        ["git", "-C", str(repo_dir), "fetch", "--unshallow", "origin"],
        capture_output=True, text=True,
    )
    if unshallow.returncode != 0:
        logger.debug(
            "merge-followup: unshallow no-op %s stderr=%s",
            log_kv, _redact_pat((unshallow.stderr or "").strip()),
        )

    fetch = subprocess.run(
        ["git", "-C", str(repo_dir), "fetch", "origin", base_branch],
        capture_output=True, text=True,
    )
    if fetch.returncode != 0:
        # Remote URL is configured with x-access-token:<PAT>@host, so any
        # auth/network failure stderr can echo the PAT — redact before logging.
        logger.warning(
            "merge-followup: fetch base failed %s stderr=%s",
            log_kv, _redact_pat((fetch.stderr or "").strip()),
        )
        return

    base_ref = f"origin/{base_branch}"
    merge = subprocess.run(
        ["git", "-C", str(repo_dir), "merge", "--no-commit", "--no-ff", base_ref],
        capture_output=True, text=True,
    )
    conflicts = _list_conflicted_files(repo_dir)

    if merge.returncode == 0 and not conflicts:
        # Either base already merged into head, or a fast-forward / clean
        # merge with staged changes. Finalise only if there's something to
        # commit; otherwise abort to leave the index clean.
        if _has_staged_changes(repo_dir):
            subject = f"chore(merge): merge {base_ref} into {head_branch}"
            sha = _commit_with_subject(repo_dir, subject)
            _push(repo_dir, head_branch)
            logger.info("merge-followup: clean merge sha=%s %s", sha[:7], log_kv)
            _notify(
                "resolve",
                conflicted_paths=[],
                commit_sha=sha,
                commit_subject=subject,
                reason="clean merge — no conflicts",
            )
        else:
            subprocess.run(
                ["git", "-C", str(repo_dir), "merge", "--abort"],
                capture_output=True, text=True,
            )
            logger.info("merge-followup: already up to date %s", log_kv)
        return

    if not conflicts:
        # Merge failed for some non-conflict reason (e.g. unrelated histories
        # if unshallow didn't reach the base merge-base). Abort so we don't
        # leave a half-merged state, and bail.
        logger.warning(
            "merge-followup: merge failed without conflicts %s stderr=%s",
            log_kv, _redact_pat((merge.stderr or "").strip()),
        )
        subprocess.run(
            ["git", "-C", str(repo_dir), "merge", "--abort"],
            capture_output=True, text=True,
        )
        return

    logger.info(
        "merge-followup: %d conflicted file(s) %s",
        len(conflicts), log_kv,
    )
    snapshots = _read_conflicted_snapshots(repo_dir, conflicts, settings)
    if not snapshots:
        logger.warning(
            "merge-followup: no readable conflicted files %s",
            log_kv,
        )
        _abort_merge(repo_dir)
        return

    ctx = MergeConflictContext(
        repository=base_repo.full_name,
        pr_number=pr_number,
        base_branch=base_branch,
        head_branch=head_branch,
        conflicted_files=snapshots,
    )
    try:
        resolution = provider.resolve_merge(ctx)
    except LLMError as exc:
        logger.warning("merge-followup: LLM error %s err=%s", log_kv, exc)
        _abort_merge(repo_dir)
        _notify("abort", conflicted_paths=conflicts, reason=f"LLM error: {exc}")
        return

    if resolution.decision == "abort":
        logger.info(
            "merge-followup: LLM aborted reason=%r %s",
            resolution.reason, log_kv,
        )
        _abort_merge(repo_dir)
        _notify(
            "abort",
            conflicted_paths=conflicts,
            reason=resolution.reason or "LLM declined to resolve",
        )
        return

    expected = set(conflicts)
    returned = {f.path for f in resolution.files}
    missing = expected - returned
    if missing:
        logger.warning(
            "merge-followup: LLM resolution missed paths=%s %s",
            sorted(missing), log_kv,
        )
        _abort_merge(repo_dir)
        _notify(
            "abort",
            conflicted_paths=conflicts,
            reason=f"LLM did not return content for: {sorted(missing)}",
        )
        return

    to_apply = [f for f in resolution.files if f.path in expected]
    # Reject any "resolved" file that still carries conflict markers — the
    # model can echo the markered input back when it gets confused, and we
    # must never push that to a real branch.
    marker_offenders = [f.path for f in to_apply if _contains_conflict_markers(f.content)]
    if marker_offenders:
        logger.warning(
            "merge-followup: LLM returned content with conflict markers paths=%s %s",
            marker_offenders, log_kv,
        )
        _abort_merge(repo_dir)
        _notify(
            "abort",
            conflicted_paths=conflicts,
            reason=f"LLM resolution still contained conflict markers in: {marker_offenders}",
        )
        return

    _write_merge_resolutions(repo_dir, to_apply)
    subject = _normalise_commit_subject(
        resolution.commit_message,
        fallback=f"fix(merge): resolve conflicts with {base_branch}",
    )
    sha = _commit_with_subject(repo_dir, subject)
    _push(repo_dir, head_branch)
    logger.info(
        "merge-followup: resolved sha=%s files=%d %s",
        sha[:7], len(conflicts), log_kv,
    )
    _notify(
        "resolve",
        conflicted_paths=conflicts,
        commit_sha=sha,
        commit_subject=subject,
        reason=resolution.reason or "resolved",
    )


def _list_conflicted_files(repo_dir: Path) -> list[str]:
    res = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        return []
    return [line for line in (res.stdout or "").splitlines() if line]


def _has_staged_changes(repo_dir: Path) -> bool:
    res = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--cached", "--name-only"],
        capture_output=True, text=True,
    )
    return bool((res.stdout or "").strip())


def _read_conflicted_snapshots(
    repo_dir: Path, paths: list[str], settings: Settings
) -> list[FileSnapshot]:
    """Read each conflicted file (with markers) for the LLM prompt.

    Skips anything outside the repo, unreadable, or above the size cap so
    the prompt stays bounded. Caller treats an empty list as "abort".
    """
    repo_root = repo_dir.resolve()
    out: list[FileSnapshot] = []
    for path in paths:
        target = (repo_dir / path).resolve()
        try:
            target.relative_to(repo_root)
        except ValueError:
            continue
        if not target.is_file():
            continue
        try:
            if target.stat().st_size > settings.max_file_bytes:
                continue
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        out.append(FileSnapshot(path=path, content=content))
    return out


_MARKER_RE = re.compile(r"^(?:<{7}|={7}|>{7})(?: |\t|$)", re.MULTILINE)


def _contains_conflict_markers(content: str) -> bool:
    """True if `content` has a line that begins with seven `<`, `=`, or `>`
    followed by a space, tab, or end-of-line — the shape of git's standard
    conflict markers. Not a substring scan, so legitimate prose like
    `<<<<<<<some` (no space) won't false-positive."""
    return bool(_MARKER_RE.search(content))


def _write_merge_resolutions(repo_dir: Path, files: list[FileChange]) -> None:
    """Write resolved file contents during a merge. Unlike `_apply_files`,
    we do NOT skip when on-disk content matches: during a merge the on-disk
    file holds conflict markers, so equality would mean the LLM echoed the
    markers back — caller has already gate'd this by checking returned
    paths cover every conflict."""
    repo_root = repo_dir.resolve()
    for change in files:
        target = (repo_dir / change.path).resolve()
        try:
            target.relative_to(repo_root)
        except ValueError:
            logger.warning("merge-followup: rejected_path_outside_repo: %s", change.path)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change.content, encoding="utf-8")


def _abort_merge(repo_dir: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo_dir), "merge", "--abort"],
        capture_output=True, text=True,
    )


def _parse_repo(value: Any) -> RepositoryRef | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    owner = value.get("owner")
    owner_login = owner.get("login") if isinstance(owner, dict) else None
    if not isinstance(name, str) or not isinstance(owner_login, str):
        return None
    return RepositoryRef(owner=owner_login, repo=name)
