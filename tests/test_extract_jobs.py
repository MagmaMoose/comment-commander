"""Webhook payload → ReviewJob mapping."""
from __future__ import annotations

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
