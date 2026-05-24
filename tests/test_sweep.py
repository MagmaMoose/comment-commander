"""_collect_sweep_comments: catch unresolved bot comments the webhook missed.

Background: the webhook flow only sees comments delivered in its own event
payload. If Copilot's review has >MAX_COMMENTS_PER_EVENT inline comments,
or a webhook delivery is lost (tunnel outage, GHEnt downtime), the surplus
sits forever until /process is manually re-triggered. The sweep closes that
gap by listing all review comments on the PR and processing any Copilot-
authored ones the current run didn't already handle.
"""
from __future__ import annotations

from typing import Any

import pytest

from github_client import GitHubError, RepositoryRef, ReviewComment
from processor import BOT_MARKER, _collect_sweep_comments


REPO = RepositoryRef(owner="octo-org", repo="octo-repo")


def _comment(
    cid: int,
    *,
    user: str = "copilot[bot]",
    body: str = "fix this",
    reply_to: int | None = None,
) -> ReviewComment:
    return ReviewComment(
        id=cid,
        node_id=f"PRRC_{cid}",
        user_login=user,
        body=body,
        path="src/app.ts",
        diff_hunk="@@ -1 +1 @@",
        line=1,
        side="RIGHT",
        in_reply_to_id=reply_to,
    )


class _StubGitHub:
    """Minimal GitHubClient surface for sweep tests."""

    def __init__(self, comments: list[ReviewComment] | None = None, raise_exc: Exception | None = None):
        self._comments = comments or []
        self._raise = raise_exc
        self.calls: list[tuple[Any, int]] = []

    def list_pr_review_comments(self, repo: RepositoryRef, pr_number: int) -> list[ReviewComment]:
        self.calls.append((repo.full_name, pr_number))
        if self._raise is not None:
            raise self._raise
        return list(self._comments)


def test_returns_bot_authored_unhandled_comments(settings):
    """The happy path: 2 bot comments on the PR, 1 was already handled, the
    other is what the webhook would have missed — it must show up."""
    gh = _StubGitHub([_comment(1), _comment(2)])
    out = _collect_sweep_comments(gh, REPO, 12, already_handled_ids={1}, settings=settings)
    assert [c.id for c in out] == [2]
    assert gh.calls == [("octo-org/octo-repo", 12)]


def test_excludes_non_bot_authors(settings):
    """Human-authored comments stay out — the webhook flow's bot-only
    contract is preserved (broader coverage would change behaviour for
    every PR with human review comments)."""
    gh = _StubGitHub([
        _comment(1, user="copilot[bot]"),
        _comment(2, user="some-human"),
        _comment(3, user="Copilot"),  # case-insensitive match
    ])
    out = _collect_sweep_comments(gh, REPO, 12, already_handled_ids=set(), settings=settings)
    assert sorted(c.id for c in out) == [1, 3]


def test_excludes_reply_comments(settings):
    """Thread replies are skipped — only thread *heads* are actionable
    (replies live under an already-resolved-or-not parent that we walk
    independently)."""
    gh = _StubGitHub([
        _comment(1),
        _comment(2, reply_to=1),
    ])
    out = _collect_sweep_comments(gh, REPO, 12, already_handled_ids=set(), settings=settings)
    assert [c.id for c in out] == [1]


def test_excludes_bot_marker_replies(settings):
    """Our own past replies (which carry BOT_MARKER) must not be picked
    back up — that would recurse into our own work."""
    gh = _StubGitHub([
        _comment(1, body="real review note"),
        _comment(2, body=f"Fixed in abc1234. {BOT_MARKER}"),
    ])
    out = _collect_sweep_comments(gh, REPO, 12, already_handled_ids=set(), settings=settings)
    assert [c.id for c in out] == [1]


def test_already_handled_ids_are_skipped(settings):
    """The caller passes IDs from this run's `pending` list so the sweep
    doesn't re-emit anything we're already about to process."""
    gh = _StubGitHub([_comment(1), _comment(2), _comment(3)])
    out = _collect_sweep_comments(
        gh, REPO, 12, already_handled_ids={1, 3}, settings=settings,
    )
    assert [c.id for c in out] == [2]


def test_api_failure_returns_empty_list(settings, caplog):
    """list_pr_review_comments failing must not break the surrounding
    _process_pr_locked run — the webhook batch's own comments still get
    processed; sweep just contributes nothing this round."""
    import logging

    caplog.set_level(logging.WARNING, logger="processor")
    gh = _StubGitHub(raise_exc=GitHubError("upstream 503"))
    out = _collect_sweep_comments(gh, REPO, 12, already_handled_ids=set(), settings=settings)
    assert out == []
    # And the failure is surfaced so it shows up in run logs / Slack debug.
    assert any("sweep list_pr_review_comments failed" in r.message for r in caplog.records)


def test_respects_bot_logins_setting(settings):
    """The set of bot logins is settings-driven (BOT_LOGINS env var), not
    hard-coded — so a custom Copilot-like reviewer can be added/removed
    without code changes."""
    import dataclasses

    custom = dataclasses.replace(
        settings,
        bot_logins=frozenset({"my-custom-reviewer[bot]"}),
    )
    gh = _StubGitHub([
        _comment(1, user="my-custom-reviewer[bot]"),
        _comment(2, user="copilot[bot]"),  # NOT in the custom set
    ])
    out = _collect_sweep_comments(gh, REPO, 12, already_handled_ids=set(), settings=custom)
    assert [c.id for c in out] == [1]


def test_empty_pr_returns_nothing(settings):
    """No review comments on the PR at all — sweep is a clean no-op."""
    gh = _StubGitHub([])
    out = _collect_sweep_comments(gh, REPO, 12, already_handled_ids=set(), settings=settings)
    assert out == []
