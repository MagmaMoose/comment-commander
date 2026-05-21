"""Webhook layer: signature verification, dedupe, payload routing."""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

import main
from conftest import comment_payload, sign_body
from main import create_app


@pytest.fixture
def captured(monkeypatch) -> list[dict[str, Any]]:
    sink: list[dict[str, Any]] = []

    def fake_process_jobs(jobs, settings_arg, *, delivery, provider, signing_key_path):
        sink.append({
            "jobs": jobs,
            "delivery": delivery,
            "provider": provider,
            "signing_key_path": signing_key_path,
        })

    monkeypatch.setattr(main, "process_jobs", fake_process_jobs)
    return sink


def _client(settings, provider, deliveries):
    app = create_app(
        settings=settings,
        provider=provider,
        signing_key_path="/tmp/stub-signing-key",
        deliveries=deliveries,
    )
    return TestClient(app)


def test_rejects_invalid_signature(settings, stub_provider, deliveries, captured):
    client = _client(settings, stub_provider, deliveries)
    response = client.post(
        "/webhook",
        content=b"{}",
        headers={
            "x-github-event": "pull_request_review_comment",
            "x-hub-signature-256": "sha256=bad",
        },
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "invalid_signature"}


def test_ignores_non_copilot_comment(settings, stub_provider, deliveries, captured, make_request):
    client = _client(settings, stub_provider, deliveries)
    payload = comment_payload(
        comment={
            "id": 99,
            "user": {"login": "human-reviewer"},
            "body": "Please fix this.",
            "path": "src/app.ts",
        }
    )
    req = make_request(payload, settings.github_webhook_secret, "pull_request_review_comment")
    response = client.post("/webhook", content=req["raw"], headers=req["headers"])
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert captured == []


def test_processes_copilot_comment_and_dispatches_job(settings, stub_provider, deliveries, captured, make_request):
    client = _client(settings, stub_provider, deliveries)
    req = make_request(
        comment_payload(),
        settings.github_webhook_secret,
        "pull_request_review_comment",
    )
    response = client.post("/webhook", content=req["raw"], headers=req["headers"])
    assert response.status_code == 202
    assert response.json() == {"status": "processing", "jobs": 1}
    assert len(captured) == 1
    assert captured[0]["delivery"] == "del-1"
    assert captured[0]["signing_key_path"] == "/tmp/stub-signing-key"


def test_duplicate_delivery_is_ignored(settings, stub_provider, deliveries, captured, make_request):
    client = _client(settings, stub_provider, deliveries)
    req = make_request(
        comment_payload(),
        settings.github_webhook_secret,
        "pull_request_review_comment",
        delivery="del-dup",
    )
    first = client.post("/webhook", content=req["raw"], headers=req["headers"])
    second = client.post("/webhook", content=req["raw"], headers=req["headers"])
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert len(captured) == 1  # background task ran only once


def test_dispatches_review_event(settings, stub_provider, deliveries, captured):
    client = _client(settings, stub_provider, deliveries)
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
                "ref": "feature/copilot-fix",
                "sha": "head-sha",
                "repo": {
                    "full_name": "octo-org/octo-repo",
                    "name": "octo-repo",
                    "owner": {"login": "octo-org"},
                },
            },
        },
        "review": {"id": 500, "user": {"login": "copilot[bot]"}},
    }
    raw = json.dumps(payload).encode("utf-8")
    response = client.post(
        "/webhook",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request_review",
            "x-github-delivery": "del-rv",
            "x-hub-signature-256": sign_body(settings.github_webhook_secret, raw),
        },
    )
    assert response.status_code == 202
    assert len(captured) == 1
    assert captured[0]["jobs"][0].review_id == 500
