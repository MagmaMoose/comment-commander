"""Manual `/process` endpoint + PR-URL parsing + bot-marker behaviour."""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import main
from main import create_app
from processor import BOT_MARKER, _format_reply_body, parse_pr_url


# --- parse_pr_url ----------------------------------------------------------


@pytest.mark.parametrize("url,owner,repo,num", [
    ("https://github.com/CalebSargeant/infra/pull/242", "CalebSargeant", "infra", 242),
    ("https://github.com/foo/bar/pull/1/files", "foo", "bar", 1),
    ("https://github.com/foo/bar/pull/77#discussion_r999", "foo", "bar", 77),
    ("CalebSargeant/infra#242", "CalebSargeant", "infra", 242),
    ("magmamoose/comment-commander#3", "magmamoose", "comment-commander", 3),
])
def test_parse_pr_url_happy_paths(url, owner, repo, num):
    parsed = parse_pr_url(url)
    assert parsed is not None
    ref, n = parsed
    assert ref.owner == owner
    assert ref.repo == repo
    assert n == num


@pytest.mark.parametrize("garbage", [
    "",
    "not a url",
    "https://gitlab.com/foo/bar/-/merge_requests/1",
    "https://github.com/foo/bar",
    "https://github.com/foo/bar/issues/1",
    None,
    123,
])
def test_parse_pr_url_invalid(garbage):
    assert parse_pr_url(garbage) is None


# --- bot reply marker ------------------------------------------------------


def test_format_reply_body_adds_marker():
    out = _format_reply_body("Fixed in 7fa338d.")
    assert BOT_MARKER in out
    assert out.startswith("Fixed in 7fa338d.")


def test_format_reply_body_is_idempotent():
    once = _format_reply_body("hello")
    twice = _format_reply_body(once)
    assert once == twice
    assert twice.count(BOT_MARKER) == 1


def test_format_reply_body_empty():
    assert _format_reply_body("") == ""
    assert _format_reply_body("   \n  ") == ""


# --- /process endpoint -----------------------------------------------------


@pytest.fixture
def captured_manual(monkeypatch) -> list[dict[str, Any]]:
    sink: list[dict[str, Any]] = []

    def fake_process_pr_manual(repo, pr_number, settings_arg, *, trigger_id, provider, signing_key_path, slack=None, **_):
        sink.append({
            "repo": repo.full_name,
            "pr_number": pr_number,
            "trigger_id": trigger_id,
        })

    monkeypatch.setattr(main, "process_pr_manual", fake_process_pr_manual)
    return sink


def _client(settings, provider, deliveries):
    app = create_app(
        settings=settings,
        provider=provider,
        signing_key_path="/tmp/stub-signing-key",
        deliveries=deliveries,
    )
    return TestClient(app)


def test_process_rejects_missing_token(settings, stub_provider, deliveries, captured_manual):
    client = _client(settings, stub_provider, deliveries)
    response = client.post(
        "/process",
        json={"pr_url": "https://github.com/foo/bar/pull/1"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "invalid_token"}
    assert captured_manual == []


def test_process_rejects_bad_token(settings, stub_provider, deliveries, captured_manual):
    client = _client(settings, stub_provider, deliveries)
    response = client.post(
        "/process",
        json={"pr_url": "https://github.com/foo/bar/pull/1"},
        headers={"X-Trigger-Token": "wrong"},
    )
    assert response.status_code == 403
    assert captured_manual == []


def test_process_rejects_bad_url(settings, stub_provider, deliveries, captured_manual):
    client = _client(settings, stub_provider, deliveries)
    response = client.post(
        "/process",
        json={"pr_url": "not a url"},
        headers={"X-Trigger-Token": settings.github_webhook_secret},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid_pr_url"}
    assert captured_manual == []


def test_process_accepts_valid_request(settings, stub_provider, deliveries, captured_manual):
    client = _client(settings, stub_provider, deliveries)
    response = client.post(
        "/process",
        json={"pr_url": "https://github.com/CalebSargeant/infra/pull/242"},
        headers={"X-Trigger-Token": settings.github_webhook_secret},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "processing"
    assert body["repo"] == "CalebSargeant/infra"
    assert body["pr"] == 242
    assert "trigger_id" in body
    assert len(captured_manual) == 1
    assert captured_manual[0]["repo"] == "CalebSargeant/infra"
    assert captured_manual[0]["pr_number"] == 242
    assert captured_manual[0]["trigger_id"] == body["trigger_id"]


def test_process_shorthand_url(settings, stub_provider, deliveries, captured_manual):
    client = _client(settings, stub_provider, deliveries)
    response = client.post(
        "/process",
        json={"pr_url": "CalebSargeant/infra#242"},
        headers={"X-Trigger-Token": settings.github_webhook_secret},
    )
    assert response.status_code == 202
    assert captured_manual[0]["repo"] == "CalebSargeant/infra"
