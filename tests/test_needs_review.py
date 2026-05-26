"""needs-review: suppress canned PR replies and route to Slack instead.

Background: comment-commander used to leak its 'I could not...' fallbacks
onto the PR (LLM errors, unreadable files, empty-reply skips). The fallback
isn't useful to PR readers and looks bad. This re-routes those outcomes:
no PR reply, Slack notify with the new `needs_review` decision label.
"""
from __future__ import annotations

import httpx

from slack import SlackNotifier, _DECISION_PREFIX


# --- Slack: needs_review decision label ------------------------------------


def test_slack_has_needs_review_label():
    """needs_review must be a first-class Slack decision label so the
    notify_decision call from processor.py doesn't fall back to the
    `*decision*` generic format."""
    assert "needs_review" in _DECISION_PREFIX
    # Distinct from `skip` (different emoji + text) so triage is visually
    # separable from intentional LLM skip-with-reasoning verdicts.
    assert _DECISION_PREFIX["needs_review"] != _DECISION_PREFIX["skip"]


def test_slack_formats_needs_review_message(monkeypatch):
    """A needs_review post should carry the same fields the existing
    decisions do — repo link, comment link, file:line — so the channel
    reader can jump straight to GitHub."""
    captured: list[dict] = []

    def fake_post(url, *, json, headers, timeout):
        captured.append(json)
        return httpx.Response(200, json={"ok": True, "ts": "1.0", "channel": "C1"})

    def fake_get(url, *, headers, timeout):
        return httpx.Response(200, json={"ok": True, "permalink": "https://github.com/MagmaMoose/comment-commander/pull/42#issuecomment-12345"})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    s = SlackNotifier(token="xoxb-token", channel="C1")
    s.notify_decision(
        decision="needs_review",
        repo="MagmaMoose/comment-commander", pr_number=42,
        comment_id=12345, comment_path="src/processor.py", comment_line=200,
        reply="llm_error",
        host="github.com",
    )
    msg = captured[-1]["text"]
    # Includes the new decision prefix and the deep link
    assert ":eyes:" in msg and "Needs review" in msg
    assert "MagmaMoose/comment-commander#42" in msg
    assert "src/processor.py:200" in msg
