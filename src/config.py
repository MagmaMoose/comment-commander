"""Runtime configuration sourced entirely from environment variables.

Secret values (PATs, webhook secret, LLM key, SSH signing key, Slack token)
are projected into the pod by ExternalSecret + OnePasswordItem; the app
itself only reads env.

Multi-instance: github.com is always configured; pinkroccade.ghe.com (or
any other GHE) is enabled only when GHE_HOST + GHE_PAT are set.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from github_client import GitHubInstance

logger = logging.getLogger(__name__)


DEFAULT_BOT_LOGINS = (
    "Copilot,copilot[bot],github-copilot[bot],github-code-quality[bot]"
)
DEFAULT_MAX_FILE_BYTES = 180_000
DEFAULT_MAX_COMMENTS_PER_EVENT = 5


@dataclass(frozen=True)
class Settings:
    # GitHub.com (always configured)
    github_webhook_secret: str
    github_pat: str
    git_author_name: str
    git_author_email: str

    # GHE (optional). Empty values disable GHE entirely.
    ghe_host: str | None
    ghe_pat: str | None
    ghe_author_name: str | None
    ghe_author_email: str | None

    # SSH signing (shared across instances — same key uploaded to both)
    ssh_signing_private_key: str

    # LLM
    llm_provider: str
    llm_model: str
    llm_api_key: str
    llm_base_url: str | None

    # Behaviour
    bot_logins: frozenset[str]
    involved_users: frozenset[str]
    allowed_repositories: frozenset[str]
    max_file_bytes: int
    max_comments_per_event: int
    dry_run: bool
    merge_conflict_resolution: bool

    # Public surface
    public_webhook_url: str | None

    # Slack notifications (optional). When either is unset, Slack posting is a no-op.
    slack_bot_token: str | None
    slack_channel_id: str | None

    # Storage
    dedupe_db_path: str

    @property
    def instances(self) -> list[GitHubInstance]:
        out = [GitHubInstance.github_com(
            pat=self.github_pat,
            author_name=self.git_author_name,
            author_email=self.git_author_email,
        )]
        if self.ghe_host and self.ghe_pat and self.ghe_author_name and self.ghe_author_email:
            out.append(GitHubInstance.ghe(
                host=self.ghe_host,
                pat=self.ghe_pat,
                author_name=self.ghe_author_name,
                author_email=self.ghe_author_email,
            ))
        return out

    @classmethod
    def load(cls) -> Settings:
        provider = os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()
        return cls(
            github_webhook_secret=_require("GITHUB_WEBHOOK_SECRET"),
            github_pat=_require("GITHUB_PAT"),
            git_author_name=_require("GIT_AUTHOR_NAME"),
            git_author_email=_require("GIT_AUTHOR_EMAIL"),
            ghe_host=(os.environ.get("GHE_HOST") or None),
            ghe_pat=(os.environ.get("GHE_PAT") or None),
            ghe_author_name=(os.environ.get("GHE_AUTHOR_NAME") or None),
            ghe_author_email=(os.environ.get("GHE_AUTHOR_EMAIL") or None),
            ssh_signing_private_key=_require("SSH_SIGNING_PRIVATE_KEY"),
            llm_provider=provider,
            llm_model=os.environ.get("LLM_MODEL", "").strip() or _default_model_for(provider),
            llm_api_key=_require("LLM_API_KEY"),
            llm_base_url=(os.environ.get("LLM_BASE_URL") or None),
            bot_logins=_parse_login_set(os.environ.get("BOT_LOGINS") or DEFAULT_BOT_LOGINS),
            involved_users=_parse_login_set(os.environ.get("INVOLVED_USERS") or ""),
            allowed_repositories=_parse_set(os.environ.get("ALLOWED_REPOSITORIES") or ""),
            max_file_bytes=_positive_int(os.environ.get("MAX_FILE_BYTES"), DEFAULT_MAX_FILE_BYTES),
            max_comments_per_event=_positive_int(
                os.environ.get("MAX_COMMENTS_PER_EVENT"), DEFAULT_MAX_COMMENTS_PER_EVENT
            ),
            dry_run=_truthy(os.environ.get("DRY_RUN")),
            merge_conflict_resolution=_truthy_default(
                os.environ.get("MERGE_CONFLICT_RESOLUTION"), True
            ),
            public_webhook_url=(os.environ.get("PUBLIC_WEBHOOK_URL") or None),
            slack_bot_token=(os.environ.get("SLACK_BOT_TOKEN") or None),
            slack_channel_id=(os.environ.get("SLACK_CHANNEL_ID") or None),
            dedupe_db_path=os.environ.get("DEDUPE_DB_PATH", "/var/lib/comment-commander/deliveries.db"),
        )


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip() if name != "SSH_SIGNING_PRIVATE_KEY" else value


def _parse_set(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _parse_login_set(value: str) -> frozenset[str]:
    """GitHub usernames are case-insensitive — normalise to lower."""
    return frozenset(part.strip().lower() for part in value.split(",") if part.strip())


def _positive_int(value: str | None, fallback: int) -> int:
    if not value:
        return fallback
    try:
        parsed = int(value)
    except ValueError:
        return fallback
    return parsed if parsed > 0 else fallback


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _truthy_default(value: str | None, default: bool) -> bool:
    """Like `_truthy` but returns `default` when the env var is unset.

    Lets us ship features as default-on (e.g. MERGE_CONFLICT_RESOLUTION)
    while still allowing an explicit `false`/`0` kill-switch."""
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _default_model_for(provider: str) -> str:
    # DeepSeek default uses the reasoner model: triage of "is this comment a
    # real defect and what edit fixes it" is reasoning-heavy, and the chat
    # model skipped trivially-fixable comments too often in practice. Tests
    # don't hit a real API, so the change is observable only on deploy.
    return {
        "deepseek": "deepseek-reasoner",
        "anthropic": "claude-haiku-4-5",
        "openai": "gpt-4o-mini",
        "openrouter": "deepseek/deepseek-reasoner",
    }.get(provider, "deepseek-reasoner")
