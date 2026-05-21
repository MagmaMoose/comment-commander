"""Runtime configuration sourced entirely from environment variables.

Secret values (PAT, webhook secret, LLM key, SSH signing key) are projected
into the pod by an `ExternalSecret` CR that syncs from OCI Vault — the app
itself never authenticates to OCI.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)


DEFAULT_BOT_LOGINS = "Copilot,copilot[bot],github-copilot[bot]"
DEFAULT_MAX_FILE_BYTES = 180_000
DEFAULT_MAX_COMMENTS_PER_EVENT = 5


@dataclass(frozen=True)
class Settings:
    # GitHub
    github_webhook_secret: str
    github_pat: str

    # Git identity (commit author/committer)
    git_author_name: str
    git_author_email: str

    # SSH signing (matches gpg.format=ssh in ~/.gitconfig)
    ssh_signing_private_key: str

    # LLM
    llm_provider: str
    llm_model: str
    llm_api_key: str
    llm_base_url: str | None

    # Behaviour
    bot_logins: frozenset[str]
    allowed_repositories: frozenset[str]
    max_file_bytes: int
    max_comments_per_event: int
    dry_run: bool

    # Storage
    dedupe_db_path: str

    @classmethod
    def load(cls) -> Settings:
        provider = os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()
        return cls(
            github_webhook_secret=_require("GITHUB_WEBHOOK_SECRET"),
            github_pat=_require("GITHUB_PAT"),
            git_author_name=_require("GIT_AUTHOR_NAME"),
            git_author_email=_require("GIT_AUTHOR_EMAIL"),
            ssh_signing_private_key=_require("SSH_SIGNING_PRIVATE_KEY"),
            llm_provider=provider,
            llm_model=os.environ.get("LLM_MODEL", "").strip() or _default_model_for(provider),
            llm_api_key=_require("LLM_API_KEY"),
            llm_base_url=(os.environ.get("LLM_BASE_URL") or None),
            bot_logins=_parse_login_set(os.environ.get("BOT_LOGINS") or DEFAULT_BOT_LOGINS),
            allowed_repositories=_parse_set(os.environ.get("ALLOWED_REPOSITORIES") or ""),
            max_file_bytes=_positive_int(os.environ.get("MAX_FILE_BYTES"), DEFAULT_MAX_FILE_BYTES),
            max_comments_per_event=_positive_int(
                os.environ.get("MAX_COMMENTS_PER_EVENT"), DEFAULT_MAX_COMMENTS_PER_EVENT
            ),
            dry_run=_truthy(os.environ.get("DRY_RUN")),
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


def _default_model_for(provider: str) -> str:
    return {
        "deepseek": "deepseek-chat",
        "anthropic": "claude-haiku-4-5",
        "openai": "gpt-4o-mini",
        "openrouter": "deepseek/deepseek-chat",
    }.get(provider, "deepseek-chat")
