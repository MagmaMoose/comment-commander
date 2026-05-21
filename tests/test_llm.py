"""LLM provider parsing + wire-format tests."""
from __future__ import annotations

import json

import httpx
import pytest

from llm.base import (
    CommentContext,
    LLMError,
    parse_decision,
    build_user_prompt,
)
from llm.providers import build_provider


def test_parse_decision_accepts_fix():
    raw = json.dumps({
        "decision": "fix",
        "reply": "Done.",
        "commitMessage": "Fix typo",
        "files": [{"path": "a.py", "content": "print(1)\n"}],
    })
    decision = parse_decision(raw)
    assert decision.decision == "fix"
    assert decision.commit_message == "Fix typo"
    assert decision.files[0].path == "a.py"


def test_parse_decision_strips_markdown_fence():
    raw = "```json\n" + json.dumps({"decision": "dismiss", "reply": "no"}) + "\n```"
    decision = parse_decision(raw)
    assert decision.decision == "dismiss"


def test_parse_decision_rejects_bad_decision():
    raw = json.dumps({"decision": "yolo", "reply": "x"})
    with pytest.raises(LLMError):
        parse_decision(raw)


def test_parse_decision_rejects_non_json():
    with pytest.raises(LLMError):
        parse_decision("not json at all")


def test_build_user_prompt_includes_comment_and_files():
    context = CommentContext(
        repository="o/r",
        pr_number=7,
        comment_body="check this",
        comment_path="a.py",
        comment_line=10,
        comment_side="RIGHT",
        diff_hunk="@@",
        files=[],
    )
    raw = build_user_prompt(context)
    payload = json.loads(raw)
    assert payload["repository"] == "o/r"
    assert payload["pull_request"] == 7
    assert payload["comment"]["path"] == "a.py"


def test_deepseek_provider_posts_chat_completions(monkeypatch):
    captured: dict = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"decision":"dismiss","reply":"ok"}'}}
                ]
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = build_provider("deepseek", "k", "deepseek-chat")
    decision = provider.decide(
        CommentContext(
            repository="o/r",
            pr_number=1,
            comment_body="x",
            comment_path="a.py",
            comment_line=None,
            comment_side=None,
            diff_hunk=None,
            files=[],
        )
    )
    assert decision.decision == "dismiss"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer k"
    assert captured["json"]["model"] == "deepseek-chat"


def test_anthropic_provider_posts_messages(monkeypatch):
    captured: dict = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": '{"decision":"skip","reply":"need more"}'}
                ]
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = build_provider("anthropic", "anth-key", "claude-haiku-4-5")
    decision = provider.decide(
        CommentContext(
            repository="o/r",
            pr_number=1,
            comment_body="x",
            comment_path="a.py",
            comment_line=None,
            comment_side=None,
            diff_hunk=None,
            files=[],
        )
    )
    assert decision.decision == "skip"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "anth-key"


def test_unknown_provider_raises():
    with pytest.raises(LLMError):
        build_provider("madeupcorp", "k", "m")
