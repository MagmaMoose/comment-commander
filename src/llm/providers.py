"""Concrete LLM providers.

All four wire-formats are reachable with raw httpx, so we avoid pulling in
provider SDKs. New providers only need a `_chat()` method that turns a
(system, user) prompt pair into the raw model response text; `decide()` and
`resolve_merge()` are then shared.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from llm.base import (
    CommentContext,
    Decision,
    LLMError,
    MERGE_SYSTEM_PROMPT,
    MergeConflictContext,
    MergeResolution,
    SYSTEM_PROMPT,
    build_merge_user_prompt,
    build_user_prompt,
    parse_decision,
    parse_merge_resolution,
)

logger = logging.getLogger(__name__)


@dataclass
class _ProviderConfig:
    name: str
    api_key: str
    model: str
    base_url: str | None
    timeout: float = 60.0


def build_provider(
    provider: str, api_key: str, model: str, base_url: str | None = None
) -> "BaseProvider":
    key = provider.lower().strip()
    cfg = _ProviderConfig(name=key, api_key=api_key, model=model, base_url=base_url)
    try:
        return _PROVIDERS[key](cfg)
    except KeyError as exc:
        raise LLMError(f"Unknown LLM_PROVIDER: {provider!r}") from exc


class BaseProvider:
    def __init__(self, cfg: _ProviderConfig):
        self.name = cfg.name
        self.model = cfg.model
        self._cfg = cfg

    def _chat(self, system: str, user: str) -> str:
        raise NotImplementedError

    def decide(self, context: CommentContext) -> Decision:
        return parse_decision(self._chat(SYSTEM_PROMPT, build_user_prompt(context)))

    def resolve_merge(self, context: MergeConflictContext) -> MergeResolution:
        return parse_merge_resolution(
            self._chat(MERGE_SYSTEM_PROMPT, build_merge_user_prompt(context))
        )


class _OpenAICompatibleProvider(BaseProvider):
    """Shared implementation for chat-completions style APIs."""

    default_base_url: str = ""

    def _chat(self, system: str, user: str) -> str:
        base = self._cfg.base_url or self.default_base_url
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        response = httpx.post(
            f"{base.rstrip('/')}/chat/completions",
            json=body,
            headers={
                "authorization": f"Bearer {self._cfg.api_key}",
                "content-type": "application/json",
            },
            timeout=self._cfg.timeout,
        )
        if not response.is_success:
            raise LLMError(
                f"{self.name} chat/completions failed ({response.status_code}): "
                f"{response.text[:200]}"
            )
        data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"{self.name} response missing content: {data}") from exc
        if not isinstance(content, str):
            raise LLMError(f"{self.name} content not a string: {content!r}")
        return content


class DeepSeekProvider(_OpenAICompatibleProvider):
    default_base_url = "https://api.deepseek.com"


class OpenAIProvider(_OpenAICompatibleProvider):
    default_base_url = "https://api.openai.com/v1"


class OpenRouterProvider(_OpenAICompatibleProvider):
    default_base_url = "https://openrouter.ai/api/v1"


class AnthropicProvider(BaseProvider):
    default_base_url = "https://api.anthropic.com/v1"

    def _chat(self, system: str, user: str) -> str:
        base = self._cfg.base_url or self.default_base_url
        body = {
            "model": self.model,
            "max_tokens": 4096,
            "temperature": 0.1,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        response = httpx.post(
            f"{base.rstrip('/')}/messages",
            json=body,
            headers={
                "x-api-key": self._cfg.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=self._cfg.timeout,
        )
        if not response.is_success:
            raise LLMError(
                f"anthropic messages failed ({response.status_code}): {response.text[:200]}"
            )
        data = response.json()
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return text
        raise LLMError(f"anthropic response had no text content: {data}")


_PROVIDERS: dict[str, type[BaseProvider]] = {
    "deepseek": DeepSeekProvider,
    "openai": OpenAIProvider,
    "openrouter": OpenRouterProvider,
    "anthropic": AnthropicProvider,
}
