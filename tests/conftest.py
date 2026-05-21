"""Shared fixtures."""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from config import Settings  # noqa: E402
from dedupe import DeliveryStore  # noqa: E402
from llm.base import CommentContext, Decision, FileChange  # noqa: E402


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        github_webhook_secret="webhook-secret",
        github_pat="github-pat",
        git_author_name="CalebSargeant",
        git_author_email="4991715+CalebSargeant@users.noreply.github.com",
        ssh_signing_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nstub\n-----END OPENSSH PRIVATE KEY-----\n",
        llm_provider="deepseek",
        llm_model="deepseek-chat",
        llm_api_key="llm-api-key",
        llm_base_url=None,
        bot_logins=frozenset({"copilot[bot]", "copilot"}),
        allowed_repositories=frozenset(),
        max_file_bytes=180_000,
        max_comments_per_event=5,
        dry_run=False,
        slack_bot_token=None,
        slack_channel_id=None,
        dedupe_db_path=str(tmp_path / "deliveries.db"),
    )


@pytest.fixture
def deliveries(settings: Settings) -> DeliveryStore:
    return DeliveryStore(settings.dedupe_db_path)


class StubProvider:
    """Returns a pre-configured decision regardless of input."""

    def __init__(self, decision: Decision):
        self.name = "stub"
        self.model = "stub"
        self.decision = decision
        self.calls: list[CommentContext] = []

    def decide(self, context: CommentContext) -> Decision:
        self.calls.append(context)
        return self.decision


@pytest.fixture
def stub_provider() -> StubProvider:
    return StubProvider(
        Decision(
            decision="dismiss",
            reply="Not actionable.",
            commit_message=None,
            files=[],
        )
    )


def comment_payload(**overrides: Any) -> dict[str, Any]:
    base = {
        "action": "created",
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
        "comment": {
            "id": 99,
            "node_id": "PRRC_99",
            "user": {"login": "copilot[bot]"},
            "body": "This should handle missing names.",
            "path": "src/app.ts",
            "diff_hunk": "@@ -1 +1 @@",
            "line": 1,
            "side": "RIGHT",
        },
    }
    base.update(overrides)
    return base


def sign_body(secret: str, body: bytes) -> str:
    import hashlib
    import hmac

    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


@pytest.fixture
def make_request():
    def _make(body: dict[str, Any], secret: str, event: str, delivery: str = "del-1") -> dict[str, Any]:
        raw = json.dumps(body).encode("utf-8")
        return {
            "raw": raw,
            "headers": {
                "content-type": "application/json",
                "x-github-event": event,
                "x-github-delivery": delivery,
                "x-hub-signature-256": sign_body(secret, raw),
            },
        }
    return _make
