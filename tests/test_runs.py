"""GET /runs + run tracking for webhook deliveries.

Manual /process runs were already tracked via TriggerStore; this covers the
addition of webhook-delivered runs to the same store and the /runs endpoint
the comment-commander-pro dashboard polls.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import main
from conftest import comment_payload
from main import create_app


@pytest.fixture
def captured_jobs(monkeypatch) -> list[dict[str, Any]]:
    """Stub process_jobs — record the `result` it was handed, run no real work.

    The webhook background task (_run_jobs) still runs for real: it creates
    the tracked TriggerResult and finishes it, which is what we assert on.
    """
    sink: list[dict[str, Any]] = []

    def fake_process_jobs(jobs, settings_arg, *, delivery, provider,
                          signing_key_path, slack=None, result=None, **_):
        sink.append({"delivery": delivery, "result": result})

    monkeypatch.setattr(main, "process_jobs", fake_process_jobs)
    return sink


@pytest.fixture
def captured_manual(monkeypatch) -> list[dict[str, Any]]:
    """Stub process_pr_manual so the manual run finishes without real work."""
    sink: list[dict[str, Any]] = []

    def fake_process_pr_manual(instance, repo, pr_number, settings_arg, *,
                               trigger_id, provider, signing_key_path,
                               slack=None, result=None, **_):
        sink.append({"trigger_id": trigger_id, "result": result})

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


# --- auth ------------------------------------------------------------------


def test_runs_rejects_missing_token(settings, stub_provider, deliveries):
    client = _client(settings, stub_provider, deliveries)
    response = client.get("/runs")
    assert response.status_code == 403
    assert response.json() == {"detail": "invalid_token"}


def test_runs_rejects_bad_token(settings, stub_provider, deliveries):
    client = _client(settings, stub_provider, deliveries)
    response = client.get("/runs", headers={"X-Trigger-Token": "wrong"})
    assert response.status_code == 403


def test_runs_empty_when_nothing_processed(settings, stub_provider, deliveries):
    client = _client(settings, stub_provider, deliveries)
    response = client.get(
        "/runs", headers={"X-Trigger-Token": settings.github_webhook_secret}
    )
    assert response.status_code == 200
    assert response.json() == {"runs": []}


# --- webhook runs are tracked ----------------------------------------------


def test_webhook_delivery_records_a_run(
    settings, stub_provider, deliveries, captured_jobs, make_request
):
    """A webhook delivery should show up in GET /runs tagged source=webhook."""
    client = _client(settings, stub_provider, deliveries)
    req = make_request(
        comment_payload(),
        settings.github_webhook_secret,
        "pull_request_review_comment",
        delivery="del-hook",
    )
    assert client.post("/webhook", content=req["raw"], headers=req["headers"]).status_code == 202

    # process_jobs was handed a real TriggerResult to record into.
    assert len(captured_jobs) == 1
    assert captured_jobs[0]["result"] is not None

    response = client.get(
        "/runs", headers={"X-Trigger-Token": settings.github_webhook_secret}
    )
    assert response.status_code == 200
    runs = response.json()["runs"]
    assert len(runs) == 1
    run = runs[0]
    assert run["trigger_id"] == "del-hook"
    assert run["source"] == "webhook"
    assert run["repo"] == "octo-org/octo-repo"
    assert run["pr"] == 12
    assert run["instance"] == "github"
    # The background task ran (stubbed) to completion, so the run is terminal.
    assert run["status"] == "ok"
    assert run["finished_at"] is not None


def test_review_event_delivery_records_a_run(
    settings, stub_provider, deliveries, captured_jobs
):
    """A pull_request_review delivery is tracked just like a comment one."""
    import json

    from conftest import sign_body

    client = _client(settings, stub_provider, deliveries)
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
            "html_url": "https://github.com/octo-org/octo-repo/pull/12",
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
            "x-github-delivery": "del-review",
            "x-hub-signature-256": sign_body(settings.github_webhook_secret, raw),
        },
    )
    assert response.status_code == 202

    runs = client.get(
        "/runs", headers={"X-Trigger-Token": settings.github_webhook_secret}
    ).json()["runs"]
    assert [r["trigger_id"] for r in runs] == ["del-review"]
    assert runs[0]["source"] == "webhook"


def test_ignored_webhook_does_not_record_a_run(
    settings, stub_provider, deliveries, captured_jobs
):
    """A webhook that extracts no jobs (non-bot author) records nothing."""
    client = _client(settings, stub_provider, deliveries)
    payload = comment_payload(
        comment={
            "id": 99,
            "user": {"login": "human-reviewer"},
            "body": "Please fix this.",
            "path": "src/app.ts",
        }
    )
    import json

    from conftest import sign_body

    raw = json.dumps(payload).encode("utf-8")
    response = client.post(
        "/webhook",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request_review_comment",
            "x-github-delivery": "del-ignored",
            "x-hub-signature-256": sign_body(settings.github_webhook_secret, raw),
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}

    runs = client.get(
        "/runs", headers={"X-Trigger-Token": settings.github_webhook_secret}
    ).json()["runs"]
    assert runs == []


# --- manual runs still report source=manual --------------------------------


def test_manual_process_run_reports_manual_source(
    settings, stub_provider, deliveries, captured_manual
):
    """A /process run is tracked with source=manual (the default)."""
    client = _client(settings, stub_provider, deliveries)
    trigger_id = client.post(
        "/process",
        json={"pr_url": "https://github.com/CalebSargeant/infra/pull/242"},
        headers={"X-Trigger-Token": settings.github_webhook_secret},
    ).json()["trigger_id"]

    runs = client.get(
        "/runs", headers={"X-Trigger-Token": settings.github_webhook_secret}
    ).json()["runs"]
    assert len(runs) == 1
    assert runs[0]["trigger_id"] == trigger_id
    assert runs[0]["source"] == "manual"
    assert runs[0]["repo"] == "CalebSargeant/infra"


def test_runs_lists_both_manual_and_webhook(
    settings, stub_provider, deliveries, captured_jobs, captured_manual, make_request
):
    """Manual and webhook runs are both surfaced by the same /runs endpoint."""
    client = _client(settings, stub_provider, deliveries)

    # a webhook delivery
    req = make_request(
        comment_payload(),
        settings.github_webhook_secret,
        "pull_request_review_comment",
        delivery="del-first",
    )
    client.post("/webhook", content=req["raw"], headers=req["headers"])

    # a manual /process run
    client.post(
        "/process",
        json={"pr_url": "https://github.com/CalebSargeant/infra/pull/242"},
        headers={"X-Trigger-Token": settings.github_webhook_secret},
    )

    runs = client.get(
        "/runs", headers={"X-Trigger-Token": settings.github_webhook_secret}
    ).json()["runs"]
    assert len(runs) == 2
    by_source = {r["source"]: r for r in runs}
    assert set(by_source) == {"manual", "webhook"}
    assert by_source["webhook"]["trigger_id"] == "del-first"
    # /runs is ordered newest-first by started_at (see test_triggers for the
    # ordering guarantee in isolation).
    started = [r["started_at"] for r in runs]
    assert started == sorted(started, reverse=True)
