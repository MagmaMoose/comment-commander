"""Access-log middleware: /health stays out, every other path lands in logs."""
from __future__ import annotations

import logging
from typing import Any

from fastapi.testclient import TestClient

import main
from conftest import comment_payload, sign_body
from main import create_app


def _client(settings, provider, deliveries, monkeypatch):
    def fake_process_jobs(*_a, **_kw):
        return None
    monkeypatch.setattr(main, "process_jobs", fake_process_jobs)
    app = create_app(
        settings=settings,
        provider=provider,
        signing_key_path="/tmp/stub-signing-key",
        deliveries=deliveries,
    )
    return TestClient(app)


def test_health_does_not_appear_in_app_logs(settings, stub_provider, deliveries, caplog, monkeypatch):
    client = _client(settings, stub_provider, deliveries, monkeypatch)
    with caplog.at_level(logging.INFO, logger="comment-commander"):
        for _ in range(3):
            assert client.get("/health").status_code == 200
    # The middleware should NOT log /health from the comment-commander logger.
    # (httpx's own client log may show the request — that's a test artifact.)
    cc_records = [r for r in caplog.records if r.name == "comment-commander"]
    assert not any("/health" in r.message for r in cc_records), \
        f"Unexpected /health entries from comment-commander logger: {[r.message for r in cc_records]}"


def test_webhook_request_is_logged(settings, stub_provider, deliveries, caplog, monkeypatch, make_request):
    client = _client(settings, stub_provider, deliveries, monkeypatch)
    req = make_request(comment_payload(), settings.github_webhook_secret, "pull_request_review_comment")
    with caplog.at_level(logging.INFO, logger="comment-commander"):
        response = client.post("/webhook", content=req["raw"], headers=req["headers"])
    assert response.status_code == 202
    assert any("POST /webhook 202" in r.message for r in caplog.records), \
        f"No /webhook log line found: {[r.message for r in caplog.records]}"
