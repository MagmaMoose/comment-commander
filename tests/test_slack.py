"""SlackNotifier: formatting + best-effort transport."""
from __future__ import annotations

import httpx
import pytest

from slack import SlackNotifier


def test_disabled_when_token_missing():
    n = SlackNotifier(token=None, channel="C123")
    assert n.enabled is False


def test_disabled_when_channel_missing():
    n = SlackNotifier(token="xoxb-x", channel=None)
    assert n.enabled is False


def test_disabled_notifier_skips_post(monkeypatch):
    calls: list = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: calls.append((a, kw)))
    SlackNotifier(token=None, channel=None).notify_decision(
        decision="fix", repo="o/r", pr_number=1, comment_id=42,
        comment_path="x.py", comment_line=10,
    )
    assert calls == []


def test_enabled_notifier_posts_to_slack(monkeypatch):
    captured: dict = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(httpx, "post", fake_post)
    SlackNotifier(token="xoxb-x", channel="C123").notify_decision(
        decision="fix",
        repo="CalebSargeant/infra", pr_number=242, comment_id=3282274306,
        comment_path="kubernetes/_clusters/firefly/foo.yaml", comment_line=12,
        commit_sha="7fa338d1234abcd5678", commit_subject="fix(mikrotik): pin chart",
        reply="Pinned chart version to 0.1.0 to prevent unintended upgrades.",
    )
    assert captured["url"] == "https://slack.com/api/chat.postMessage"
    assert captured["headers"]["Authorization"] == "Bearer xoxb-x"
    payload = captured["json"]
    assert payload["channel"] == "C123"
    assert payload["mrkdwn"] is True
    text = payload["text"]
    assert "*Fixed*" in text
    assert "CalebSargeant/infra#242" in text
    # Comment link
    assert "https://github.com/CalebSargeant/infra/pull/242#discussion_r3282274306" in text
    # Path:line annotation
    assert "kubernetes/_clusters/firefly/foo.yaml:12" in text
    # Commit link with 7-char short sha
    assert "https://github.com/CalebSargeant/infra/commit/7fa338d1234abcd5678" in text
    assert "`7fa338d`" in text
    # Conventional Commit subject preserved verbatim
    assert "fix(mikrotik): pin chart" in text
    # Reply snippet included
    assert "Pinned chart version" in text


def test_dismiss_uses_dismiss_prefix(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, json, **kw: (captured.setdefault("json", json),
                                httpx.Response(200, json={"ok": True}))[1],
    )
    SlackNotifier(token="t", channel="c").notify_decision(
        decision="dismiss", repo="o/r", pr_number=1, comment_id=2,
        comment_path="x.py", comment_line=None,
        reply="Already handled upstream.",
    )
    assert "*Dismissed*" in captured["json"]["text"]
    # No commit link for dismiss
    assert "Commit:" not in captured["json"]["text"]


def test_slack_failure_does_not_raise(monkeypatch):
    def boom(*a, **kw):
        raise httpx.ConnectError("network down")
    monkeypatch.setattr(httpx, "post", boom)
    # Must not propagate
    SlackNotifier(token="t", channel="c").notify_decision(
        decision="fix", repo="o/r", pr_number=1, comment_id=2,
        comment_path="x.py", comment_line=1,
    )


def test_slack_ok_false_does_not_raise(monkeypatch):
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **kw: httpx.Response(200, json={"ok": False, "error": "channel_not_found"}),
    )
    SlackNotifier(token="t", channel="c").notify_decision(
        decision="fix", repo="o/r", pr_number=1, comment_id=2,
        comment_path="x.py", comment_line=1,
    )


def test_long_reply_is_truncated(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, json, **kw: (captured.setdefault("json", json),
                                httpx.Response(200, json={"ok": True}))[1],
    )
    long_reply = "x" * 1000
    SlackNotifier(token="t", channel="c").notify_decision(
        decision="dismiss", repo="o/r", pr_number=1, comment_id=2,
        comment_path="x.py", comment_line=None, reply=long_reply,
    )
    text = captured["json"]["text"]
    assert "…" in text
    # Total Slack text shouldn't blow past Slack's chat.postMessage 40k char
    # limit — we cap the reply snippet to ~240 chars.
    snippet_line = [line for line in text.split("\n") if line.startswith("> ")][0]
    assert len(snippet_line) < 260
