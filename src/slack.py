"""Best-effort Slack notifications via chat.postMessage.

Designed to never break the bot's main path:
- Disabled (no-op) when SLACK_BOT_TOKEN or SLACK_CHANNEL_ID is unset.
- All failures (HTTP errors, Slack API `ok: false`, timeouts) are caught
  and logged at WARN — they do not propagate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import httpx

logger = logging.getLogger(__name__)


SLACK_API = "https://slack.com/api/chat.postMessage"

Decision = Literal["fix", "dismiss", "skip"]

_DECISION_PREFIX = {
    "fix": ":white_check_mark: *Fixed*",
    "dismiss": ":no_entry_sign: *Dismissed*",
    "skip": ":warning: *Skipped — manual review needed*",
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
    ) -> None:
        if not self.enabled:
            return
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
        )
        self._post(text)

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
    ) -> str:
        prefix = _DECISION_PREFIX.get(decision, f"*{decision}*")
        pr_url = f"https://github.com/{repo}/pull/{pr_number}"
        comment_url = f"{pr_url}#discussion_r{comment_id}"
        loc = comment_path + (f":{comment_line}" if comment_line else "")
        lines = [
            f"{prefix} on <{pr_url}|{repo}#{pr_number}>",
            f"• Comment: <{comment_url}|`{loc}`>",
        ]
        if decision == "fix" and commit_sha:
            short = commit_sha[:7]
            commit_url = f"https://github.com/{repo}/commit/{commit_sha}"
            subj = commit_subject or ""
            lines.append(f"• Commit: <{commit_url}|`{short}`>  `{subj}`")
        if reply:
            snippet = reply.strip().replace("\n", " ")
            if len(snippet) > 240:
                snippet = snippet[:237].rstrip() + "…"
            lines.append(f"> {snippet}")
        return "\n".join(lines)

    def _post(self, text: str) -> None:
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
            return
        if response.status_code != 200:
            logger.warning(
                "slack post failed status=%s body=%s",
                response.status_code, response.text[:200],
            )
            return
        try:
            body = response.json()
        except ValueError:
            logger.warning("slack returned non-JSON: %s", response.text[:200])
            return
        if not body.get("ok"):
            logger.warning("slack returned ok=false error=%s", body.get("error"))
