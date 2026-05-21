"""Multi-instance routing, INVOLVED_USERS whitelist, and /setup-webhook."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from conftest import comment_payload, sign_body
from github_client import GitHubInstance, find_instance_for_payload
from main import create_app
from processor import extract_jobs, user_has_commits


# --- instance detection ----------------------------------------------------


def test_detect_github_com_instance():
    payload = {"repository": {"html_url": "https://github.com/foo/bar"}}
    insts = [
        GitHubInstance.github_com(pat="p", author_name="x", author_email="y"),
        GitHubInstance.ghe(host="pinkroccade.ghe.com", pat="p", author_name="x", author_email="y"),
    ]
    inst = find_instance_for_payload(payload, insts)
    assert inst is not None and inst.host == "github.com"


def test_detect_ghe_instance():
    payload = {"repository": {"html_url": "https://pinkroccade.ghe.com/org/repo"}}
    insts = [
        GitHubInstance.github_com(pat="p", author_name="x", author_email="y"),
        GitHubInstance.ghe(host="pinkroccade.ghe.com", pat="p", author_name="x", author_email="y"),
    ]
    inst = find_instance_for_payload(payload, insts)
    assert inst is not None and inst.host == "pinkroccade.ghe.com"


def test_detect_falls_back_to_pull_request_url():
    payload = {
        "repository": {"html_url": "ssh://something-else"},
        "pull_request": {"html_url": "https://pinkroccade.ghe.com/o/r/pull/1"},
    }
    insts = [GitHubInstance.ghe(host="pinkroccade.ghe.com", pat="p", author_name="x", author_email="y")]
    assert find_instance_for_payload(payload, insts) is insts[0]


def test_detect_returns_none_for_unknown_host():
    payload = {"repository": {"html_url": "https://gitea.example.com/o/r"}}
    insts = [GitHubInstance.github_com(pat="p", author_name="x", author_email="y")]
    assert find_instance_for_payload(payload, insts) is None


def test_ghe_instance_urls():
    inst = GitHubInstance.ghe(
        host="pinkroccade.ghe.com",
        pat="ghp_xxx",
        author_name="sargea50",
        author_email="Caleb.sargeant@pinkroccade.nl",
    )
    assert inst.api_base == "https://pinkroccade.ghe.com/api/v3"
    assert inst.graphql_url == "https://pinkroccade.ghe.com/api/graphql"
    assert inst.clone_url("o", "r") == "https://x-access-token:ghp_xxx@pinkroccade.ghe.com/o/r.git"
    assert inst.html_url_prefix() == "https://pinkroccade.ghe.com/"


# --- extract_jobs with multi-instance --------------------------------------


def test_extract_jobs_picks_ghe_instance_when_payload_matches(settings):
    ghe_settings = replace(
        settings,
        ghe_host="pinkroccade.ghe.com",
        ghe_pat="ghe-pat",
        ghe_author_name="sargea50",
        ghe_author_email="Caleb.sargeant@pinkroccade.nl",
    )
    payload = comment_payload()
    payload["repository"]["html_url"] = "https://pinkroccade.ghe.com/org/repo"
    payload["pull_request"]["html_url"] = "https://pinkroccade.ghe.com/org/repo/pull/12"
    jobs = extract_jobs(payload, "pull_request_review_comment", ghe_settings)
    assert len(jobs) == 1
    assert jobs[0].instance.host == "pinkroccade.ghe.com"
    assert jobs[0].instance.author_name == "sargea50"


def test_extract_jobs_drops_event_from_unconfigured_host(settings):
    payload = comment_payload()
    payload["repository"]["html_url"] = "https://gitea.example.com/o/r"
    payload["pull_request"]["html_url"] = "https://gitea.example.com/o/r/pull/12"
    assert extract_jobs(payload, "pull_request_review_comment", settings) == []


# --- INVOLVED_USERS whitelist ---------------------------------------------


class _FakeGh:
    """Stand-in for GitHubClient.list_pr_commits."""

    def __init__(self, commits: list[dict]):
        self._commits = commits

    def list_pr_commits(self, repo, pr_number):
        return self._commits


def test_user_has_commits_empty_whitelist_passes_anything():
    gh = _FakeGh([])
    from github_client import RepositoryRef
    assert user_has_commits(gh, RepositoryRef("o", "r"), 1, frozenset()) is True


def test_user_has_commits_matches_author():
    gh = _FakeGh([
        {"author": {"login": "stranger"}, "committer": {"login": "stranger"}},
        {"author": {"login": "CalebSargeant"}, "committer": {"login": "stranger"}},
    ])
    from github_client import RepositoryRef
    assert user_has_commits(gh, RepositoryRef("o", "r"), 1, frozenset({"calebsargeant"})) is True


def test_user_has_commits_matches_committer():
    gh = _FakeGh([
        {"author": {"login": "stranger"}, "committer": {"login": "Sargea50"}},
    ])
    from github_client import RepositoryRef
    assert user_has_commits(gh, RepositoryRef("o", "r"), 1, frozenset({"sargea50"})) is True


def test_user_has_commits_rejects_when_no_match():
    gh = _FakeGh([
        {"author": {"login": "stranger"}, "committer": {"login": "another"}},
    ])
    from github_client import RepositoryRef
    assert user_has_commits(gh, RepositoryRef("o", "r"), 1, frozenset({"calebsargeant"})) is False


# --- /setup-webhook --------------------------------------------------------


def _client(settings, provider, deliveries):
    app = create_app(
        settings=settings,
        provider=provider,
        signing_key_path="/tmp/stub-signing-key",
        deliveries=deliveries,
    )
    return TestClient(app)


def test_setup_webhook_rejects_bad_token(settings, stub_provider, deliveries):
    client = _client(settings, stub_provider, deliveries)
    response = client.post(
        "/setup-webhook",
        json={"target": "https://github.com/foo/bar"},
    )
    assert response.status_code == 403


def test_setup_webhook_rejects_unknown_host(settings, stub_provider, deliveries):
    client = _client(settings, stub_provider, deliveries)
    response = client.post(
        "/setup-webhook",
        json={"target": "https://gitea.example.com/foo/bar"},
        headers={"X-Trigger-Token": settings.github_webhook_secret},
    )
    assert response.status_code == 400
    assert response.json()["detail"].startswith("unknown_host")


def test_setup_webhook_creates_repo_hook(settings, stub_provider, deliveries):
    import respx
    client = _client(settings, stub_provider, deliveries)
    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/repos/CalebSargeant/infra/hooks").mock(
            return_value=httpx.Response(200, json=[])
        )
        route = mock.post("/repos/CalebSargeant/infra/hooks").mock(
            return_value=httpx.Response(201, json={"id": 12345})
        )
        response = client.post(
            "/setup-webhook",
            json={"target": "https://github.com/CalebSargeant/infra"},
            headers={"X-Trigger-Token": settings.github_webhook_secret},
        )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["scope"] == "CalebSargeant/infra"
    assert body["hook_id"] == 12345
    assert route.called
    payload = route.calls.last.request
    import json as _json
    parsed = _json.loads(payload.content)
    assert parsed["events"] == ["pull_request_review", "pull_request_review_comment"]
    assert parsed["config"]["url"] == "https://comment-commander.example.com/webhook"
    assert parsed["config"]["secret"] == settings.github_webhook_secret


def test_setup_webhook_creates_org_hook(settings, stub_provider, deliveries):
    import respx
    client = _client(settings, stub_provider, deliveries)
    with respx.mock(base_url="https://api.github.com") as mock:
        mock.get("/orgs/magmamoose/hooks").mock(
            return_value=httpx.Response(200, json=[])
        )
        route = mock.post("/orgs/magmamoose/hooks").mock(
            return_value=httpx.Response(201, json={"id": 999})
        )
        response = client.post(
            "/setup-webhook",
            json={"target": "https://github.com/magmamoose"},
            headers={"X-Trigger-Token": settings.github_webhook_secret},
        )
    assert response.status_code == 201
    assert route.called


def test_setup_webhook_is_idempotent_on_existing_hook(settings, stub_provider, deliveries):
    """Second call must NOT 502 — it should return the pre-existing hook."""
    import respx
    client = _client(settings, stub_provider, deliveries)
    public = settings.public_webhook_url
    existing_hook = {
        "id": 628163836,
        "events": ["pull_request_review", "pull_request_review_comment"],
        "config": {"url": public, "content_type": "json"},
    }
    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as mock:
        list_route = mock.get("/orgs/magmamoose/hooks").mock(
            return_value=httpx.Response(200, json=[existing_hook])
        )
        create_route = mock.post("/orgs/magmamoose/hooks")
        response = client.post(
            "/setup-webhook",
            json={"target": "https://github.com/magmamoose"},
            headers={"X-Trigger-Token": settings.github_webhook_secret},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "already_exists"
    assert body["hook_id"] == 628163836
    assert list_route.called
    assert not create_route.called  # never tried to create a duplicate


def test_setup_webhook_propagates_403_saml_block(settings, stub_provider, deliveries):
    """GHE PATs need per-org SAML SSO authorisation. The 403 must reach the
    caller verbatim — 502 hides the actionable message."""
    import respx
    ghe_settings = replace(
        settings,
        ghe_host="pinkroccade.ghe.com",
        ghe_pat="ghe-pat",
        ghe_author_name="sargea50",
        ghe_author_email="Caleb.sargeant@pinkroccade.nl",
    )
    client = _client(ghe_settings, stub_provider, deliveries)
    saml_message = "Resource protected by organization SAML enforcement. You must grant your Personal Access token access to an organization within this enterprise."
    with respx.mock(base_url="https://pinkroccade.ghe.com/api/v3") as mock:
        mock.get("/repos/samenlevingszaken/foo/hooks").mock(
            return_value=httpx.Response(403, json={"message": saml_message})
        )
        # In case listing somehow succeeds, the create call must also 403.
        mock.post("/repos/samenlevingszaken/foo/hooks").mock(
            return_value=httpx.Response(403, json={"message": saml_message})
        )
        response = client.post(
            "/setup-webhook",
            json={"target": "https://pinkroccade.ghe.com/samenlevingszaken/foo"},
            headers={"X-Trigger-Token": settings.github_webhook_secret},
        )
    assert response.status_code == 403, response.text
    assert "SAML" in response.text


def test_setup_webhook_routes_to_ghe(settings, stub_provider, deliveries):
    """A GHE target must hit pinkroccade.ghe.com/api/v3, not api.github.com."""
    import respx
    ghe_settings = replace(
        settings,
        ghe_host="pinkroccade.ghe.com",
        ghe_pat="ghe-pat",
        ghe_author_name="sargea50",
        ghe_author_email="Caleb.sargeant@pinkroccade.nl",
    )
    client = _client(ghe_settings, stub_provider, deliveries)
    with respx.mock(base_url="https://pinkroccade.ghe.com/api/v3") as mock:
        mock.get("/repos/some-org/some-repo/hooks").mock(
            return_value=httpx.Response(200, json=[])
        )
        route = mock.post("/repos/some-org/some-repo/hooks").mock(
            return_value=httpx.Response(201, json={"id": 777})
        )
        response = client.post(
            "/setup-webhook",
            json={"target": "https://pinkroccade.ghe.com/some-org/some-repo"},
            headers={"X-Trigger-Token": settings.github_webhook_secret},
        )
    assert response.status_code == 201
    assert response.json()["instance"] == "ghe"
    assert route.called
