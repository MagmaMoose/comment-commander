"""LLM provider parsing + wire-format tests."""
from __future__ import annotations

import json

import httpx
import pytest

from llm.base import (
    CommentContext,
    FileSnapshot,
    LLMError,
    MergeConflictContext,
    build_merge_user_prompt,
    build_user_prompt,
    parse_decision,
    parse_merge_resolution,
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


def test_parse_decision_strips_thinking_block():
    """deepseek-reasoner / R1-style models can emit `<think>...</think>` in
    the response content despite the system-prompt instruction. The parser
    has to recover the JSON tail."""
    raw = (
        "<think>OK let's see — this comment looks like a real defect, but I "
        "should weigh dismissing vs fixing carefully...</think>\n"
        + json.dumps({"decision": "fix", "reply": "done", "commitMessage": "fix: redact PAT", "files": []})
    )
    decision = parse_decision(raw)
    assert decision.decision == "fix"
    assert decision.commit_message == "fix: redact PAT"


def test_parse_merge_resolution_strips_thinking_block():
    raw = (
        "<think>The conflict is on hello.txt; the feat-side change preserves "
        "intent.</think>"
        + json.dumps({
            "decision": "resolve",
            "reason": "kept feat side",
            "commitMessage": "fix(merge): resolve hello.txt",
            "files": [{"path": "hello.txt", "content": "feat\n"}],
        })
    )
    resolution = parse_merge_resolution(raw)
    assert resolution.decision == "resolve"
    assert resolution.files[0].content == "feat\n"


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


def test_parse_merge_resolution_resolve():
    raw = json.dumps({
        "decision": "resolve",
        "reason": "kept feat side; semantics preserved",
        "commitMessage": "fix(merge): resolve hello.txt with feat side",
        "files": [{"path": "hello.txt", "content": "line1\nline2 from feat\n"}],
    })
    resolution = parse_merge_resolution(raw)
    assert resolution.decision == "resolve"
    assert resolution.commit_message.startswith("fix(merge):")
    assert resolution.files[0].path == "hello.txt"
    assert "<<<<<<<" not in resolution.files[0].content


def test_parse_merge_resolution_abort():
    raw = json.dumps({
        "decision": "abort",
        "reason": "ambiguous semantics",
        "files": [],
    })
    resolution = parse_merge_resolution(raw)
    assert resolution.decision == "abort"
    assert resolution.files == []


def test_parse_merge_resolution_rejects_invalid_decision():
    with pytest.raises(LLMError):
        parse_merge_resolution(json.dumps({"decision": "skip"}))


def test_build_merge_user_prompt_includes_branches_and_files():
    ctx = MergeConflictContext(
        repository="o/r",
        pr_number=42,
        base_branch="main",
        head_branch="feat/x",
        conflicted_files=[FileSnapshot(path="a.py", content="<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>> main\n")],
    )
    raw = build_merge_user_prompt(ctx)
    payload = json.loads(raw)
    assert payload["base_branch"] == "main"
    assert payload["head_branch"] == "feat/x"
    assert payload["conflicted_files"][0]["path"] == "a.py"
    assert "<<<<<<<" in payload["conflicted_files"][0]["content"]


def test_resolve_merge_uses_chat(monkeypatch):
    """resolve_merge should hit the same chat endpoint with the merge prompt."""
    captured: dict = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"decision":"abort","reason":"ambiguous"}'}}
                ]
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = build_provider("deepseek", "k", "deepseek-chat")
    resolution = provider.resolve_merge(
        MergeConflictContext(
            repository="o/r",
            pr_number=1,
            base_branch="main",
            head_branch="feat",
            conflicted_files=[FileSnapshot(path="a.py", content="<<<<<<<\n")],
        )
    )
    assert resolution.decision == "abort"
    # System message should be the merge prompt, not the triage prompt.
    sys_msg = captured["json"]["messages"][0]
    assert sys_msg["role"] == "system"
    assert "merge conflict" in sys_msg["content"].lower()
