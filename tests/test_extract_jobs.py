"""Webhook payload → ReviewJob mapping."""
from __future__ import annotations

from dataclasses import replace

from config import DEFAULT_BOT_LOGINS, _parse_login_set
from conftest import comment_payload
from processor import extract_jobs


def test_ignores_uncreated_comment(settings):
    payload = comment_payload(action="edited")
    assert extract_jobs(payload, "pull_request_review_comment", settings) == []


def test_extracts_copilot_comment(settings):
    payload = comment_payload()
    jobs = extract_jobs(payload, "pull_request_review_comment", settings)
    assert len(jobs) == 1
    assert jobs[0].pr_number == 12
    assert jobs[0].comment is not None
    assert jobs[0].comment.id == 99


def test_extracts_copilot_code_review_identity(settings):
    """The newer Copilot Code Review surface uses login `Copilot` (no [bot] suffix, capitalised)."""
    payload = comment_payload(
        comment={
            "id": 101,
            "node_id": "PRRC_101",
            "user": {"login": "Copilot"},
            "body": "Consider null checks here.",
            "path": "src/app.ts",
        }
    )
    jobs = extract_jobs(payload, "pull_request_review_comment", settings)
    assert len(jobs) == 1
    assert jobs[0].comment.user_login == "Copilot"


def test_extracts_github_code_quality_comment(settings):
    """github-code-quality[bot] posts inline review comments the same shape as
    Copilot. It's in DEFAULT_BOT_LOGINS, so settings parsed from env should
    route it through the same triage flow."""
    s = replace(settings, bot_logins=_parse_login_set(DEFAULT_BOT_LOGINS))
    payload = comment_payload(
        comment={
            "id": 202,
            "node_id": "PRRC_202",
            "user": {"login": "github-code-quality[bot]"},
            "body": "## Unused global variable\n\nThe global variable 'revision' is not used.",
            "path": "backend/alembic/versions/0013.py",
        }
    )
    jobs = extract_jobs(payload, "pull_request_review_comment", s)
    assert len(jobs) == 1
    assert jobs[0].comment.user_login == "github-code-quality[bot]"


def test_ignores_disallowed_repository(settings):
    blocked = dict(settings.__dict__)
    blocked["allowed_repositories"] = frozenset({"some-other/repo"})
    from dataclasses import replace
    s = replace(settings, allowed_repositories=frozenset({"some-other/repo"}))
    payload = comment_payload()
    assert extract_jobs(payload, "pull_request_review_comment", s) == []


def test_extracts_review_event(settings):
    payload = {
        "action": "submitted",
        "repository": {
            "full_name": "octo-org/octo-repo",
            "name": "octo-repo",
            "owner": {"login": "octo-org"},
            "html_url": "https://github.com/octo-org/octo-repo",
        },
        "pull_request": {
            "number": 12,
            "head": {
                "ref": "feature/x",
                "sha": "head-sha",
                "repo": {
                    "full_name": "octo-org/octo-repo",
                    "name": "octo-repo",
                    "owner": {"login": "octo-org"},
                },
            },
        },
        "review": {"id": 7, "user": {"login": "copilot[bot]"}},
    }
    jobs = extract_jobs(payload, "pull_request_review", settings)
    assert len(jobs) == 1
    assert jobs[0].review_id == 7
    assert jobs[0].comment is None


def test_ignores_non_bot_review(settings):
    payload = {
        "action": "submitted",
        "repository": {
            "full_name": "octo-org/octo-repo",
            "name": "octo-repo",
            "owner": {"login": "octo-org"},
            "html_url": "https://github.com/octo-org/octo-repo",
        },
        "pull_request": {
            "number": 12,
            "head": {
                "ref": "feature/x",
                "sha": "head-sha",
                "repo": {
                    "full_name": "octo-org/octo-repo",
                    "name": "octo-repo",
                    "owner": {"login": "octo-org"},
                },
            },
        },
        "review": {"id": 7, "user": {"login": "human"}},
    }
    assert extract_jobs(payload, "pull_request_review", settings) == []
