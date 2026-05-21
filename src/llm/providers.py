"""Concrete LLM providers.

All four wire-formats are reachable with raw httpx, so we avoid pulling in
provider SDKs. New providers only need a `decide()` method that produces the
JSON object described in `llm.base.build_user_prompt`.
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
    SYSTEM_PROMPT,
    build_user_prompt,
    parse_decision,
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

    def decide(self, context: CommentContext) -> Decision:
        raise NotImplementedError


class _OpenAICompatibleProvider(BaseProvider):
    """Shared implementation for chat-completions style APIs."""

    default_base_url: str = ""

    def decide(self, context: CommentContext) -> Decision:
        base = self._cfg.base_url or self.default_base_url
        user = build_user_prompt(context)
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
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
        return parse_decision(content)


class DeepSeekProvider(_OpenAICompatibleProvider):
    default_base_url = "https://api.deepseek.com"


class OpenAIProvider(_OpenAICompatibleProvider):
    default_base_url = "https://api.openai.com/v1"


class OpenRouterProvider(_OpenAICompatibleProvider):
    default_base_url = "https://openrouter.ai/api/v1"


class AnthropicProvider(BaseProvider):
    default_base_url = "https://api.anthropic.com/v1"

    def decide(self, context: CommentContext) -> Decision:
        base = self._cfg.base_url or self.default_base_url
        user = build_user_prompt(context)
        body = {
            "model": self.model,
            "max_tokens": 4096,
            "temperature": 0.1,
            "system": SYSTEM_PROMPT,
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
        content_blocks = data.get("content") or []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    return parse_decision(text)
        raise LLMError(f"anthropic response had no text content: {data}")


_PROVIDERS: dict[str, type[BaseProvider]] = {
    "deepseek": DeepSeekProvider,
    "openai": OpenAIProvider,
    "openrouter": OpenRouterProvider,
    "anthropic": AnthropicProvider,
}
