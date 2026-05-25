"""Best-effort Slack notifications via chat.postMessage.

Designed to never break the bot's main path:
- Disabled (no-op) when SLACK_BOT_TOKEN or SLACK_CHANNEL_ID is unset.
- All failures (HTTP errors, Slack API `ok: false`, timeouts) are caught
  and logged at WARN — they do not propagate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)


SLACK_API = "https://slack.com/api/chat.postMessage"
SLACK_PERMALINK_API = "https://slack.com/api/chat.getPermalink"

Decision = Literal["fix", "dismiss", "skip"]

_DECISION_PREFIX = {
    "fix": ":white_check_mark: *Fixed*",
    "dismiss": ":no_entry_sign: *Dismissed*",
    "skip": ":warning: *Skipped — manual review needed*",
}

MergeOutcome = Literal["resolve", "abort"]

_MERGE_PREFIX = {
    "resolve": ":twisted_rightwards_arrows: *Merge conflict resolved*",
    "abort": ":warning: *Merge conflict NOT auto-resolved — manual review needed*",
}


@dataclass
class SlackNotifier:
    token: str | None
    channel: str | None
    timeout: float = 5.0

    @property
    def enabled(self) -> bool:
        return bool(self.token) and bool(self.channel)

    def notify_decision(
        self,
        *,
        decision: Decision,
        repo: str,
        pr_number: int,
        comment_id: int,
        comment_path: str,
        comment_line: int | None,
        commit_sha: str | None = None,
        commit_subject: str | None = None,
        reply: str = "",
        host: str = "github.com",
    ) -> dict[str, Any] | None:
        """Post the decision to Slack.

        Returns the message ref ({"ts", "channel", "permalink"}) on success,
        or None when disabled or the post failed. `permalink` is best-effort
        and may be None even on a successful post. Callers use the ref only to
        record the message id — never to gate the bot's main path."""
        if not self.enabled:
            return None
        text = self._format_message(
            decision=decision,
            repo=repo,
            pr_number=pr_number,
            comment_id=comment_id,
            comment_path=comment_path,
            comment_line=comment_line,
            commit_sha=commit_sha,
            commit_subject=commit_subject,
            reply=reply,
            host=host,
        )
        return self._post(text)

    def notify_merge_resolution(
        self,
        *,
        outcome: MergeOutcome,
        repo: str,
        pr_number: int,
        base_branch: str,
        head_branch: str,
        conflicted_paths: list[str],
        commit_sha: str | None = None,
        commit_subject: str | None = None,
        reason: str = "",
        host: str = "github.com",
    ) -> dict[str, Any] | None:
        """Post the merge-resolution outcome to Slack. Same fail-closed
        semantics as notify_decision — never propagates failure."""
        if not self.enabled:
            return None
        prefix = _MERGE_PREFIX.get(outcome, f"*{outcome}*")
        pr_url = f"https://{host}/{repo}/pull/{pr_number}"
        lines = [
            f"{prefix} on <{pr_url}|{repo}#{pr_number}>",
            f"• Branches: `{head_branch}` ← `{base_branch}`",
        ]
        if conflicted_paths:
            preview = ", ".join(f"`{p}`" for p in conflicted_paths[:5])
            if len(conflicted_paths) > 5:
                preview += f" (+{len(conflicted_paths) - 5} more)"
            lines.append(f"• Conflicts: {preview}")
        if outcome == "resolve" and commit_sha:
            short = commit_sha[:7]
            commit_url = f"https://{host}/{repo}/commit/{commit_sha}"
            subj = commit_subject or ""
            lines.append(f"• Commit: <{commit_url}|`{short}`>  `{subj}`")
        if reason:
            snippet = reason.strip().replace("\n", " ")
            if len(snippet) > 240:
                snippet = snippet[:237].rstrip() + "…"
            lines.append(f"> {snippet}")
        return self._post("\n".join(lines))

    # --- internals -----------------------------------------------------------

    def _format_message(
        self,
        *,
        decision: Decision,
        repo: str,
        pr_number: int,
        comment_id: int,
        comment_path: str,
        comment_line: int | None,
        commit_sha: str | None,
        commit_subject: str | None,
        reply: str,
        host: str,
    ) -> str:
        prefix = _DECISION_PREFIX.get(decision, f"*{decision}*")
        pr_url = f"https://{host}/{repo}/pull/{pr_number}"
        comment_url = f"{pr_url}#discussion_r{comment_id}"
        loc = comment_path + (f":{comment_line}" if comment_line else "")
        lines = [
            f"{prefix} on <{pr_url}|{repo}#{pr_number}>",
            f"• Comment: <{comment_url}|`{loc}`>",
        ]
        if decision == "fix" and commit_sha:
            short = commit_sha[:7]
            commit_url = f"https://{host}/{repo}/commit/{commit_sha}"
            subj = commit_subject or ""
            lines.append(f"• Commit: <{commit_url}|`{short}`>  `{subj}`")
        if reply:
            snippet = reply.strip().replace("\n", " ")
            if len(snippet) > 240:
                snippet = snippet[:237].rstrip() + "…"
            lines.append(f"> {snippet}")
        return "\n".join(lines)

    def _post(self, text: str) -> dict[str, Any] | None:
        """Post `text`; return {"ts", "channel", "permalink"} on success, else None."""
        try:
            response = httpx.post(
                SLACK_API,
                json={
                    "channel": self.channel,
                    "text": text,
                    "mrkdwn": True,
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("slack post failed (transport): %s", exc)
            return None
        if response.status_code != 200:
            logger.warning(
                "slack post failed status=%s body=%s",
                response.status_code, response.text[:200],
            )
            return None
        try:
            body = response.json()
        except ValueError:
            logger.warning("slack returned non-JSON: %s", response.text[:200])
            return None
        if not body.get("ok"):
            logger.warning("slack returned ok=false error=%s", body.get("error"))
            return None
        # chat.postMessage echoes the channel id + message ts (the "Slack
        # message id"). comment-commander-pro records these per trigger.
        channel = body.get("channel")
        ts = body.get("ts")
        return {
            "ts": ts,
            "channel": channel,
            "permalink": self._permalink(channel, ts),
        }

    def _permalink(self, channel: Any, ts: Any) -> str | None:
        """Best-effort chat.getPermalink lookup; None on any failure.

        A permalink is a nice-to-have for comment-commander-pro — it must
        never break the post, so every failure mode (transport error,
        non-200, non-JSON, `ok: false`) falls back to None."""
        if not channel or not ts:
            return None
        try:
            response = httpx.get(
                SLACK_PERMALINK_API,
                params={"channel": channel, "message_ts": ts},
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            logger.warning("slack getPermalink failed (transport): %s", exc)
            return None
        if response.status_code != 200:
            logger.warning(
                "slack getPermalink failed status=%s", response.status_code,
            )
            return None
        try:
            body = response.json()
        except ValueError:
            logger.warning("slack getPermalink returned non-JSON")
            return None
        if not body.get("ok"):
            logger.warning(
                "slack getPermalink returned ok=false error=%s", body.get("error"),
            )
            return None
        return body.get("permalink")
