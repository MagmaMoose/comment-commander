"""In-memory results store for manual `/process` triggers.

`/process` is fire-and-forget: it returns 202 + a `trigger_id` and runs the
real work in a background task, logging to stdout. This module remembers the
outcome of each trigger so the comment-commander-pro dashboard can poll
`GET /process/{trigger_id}` and surface the final status plus the Slack
messages the run posted.

Deliberately in-memory and bounded — results are lost on restart (the bot
runs a single replica with a Recreate rollout). A poller that gets a 404 for
an unknown trigger simply stops polling and leaves the run as it is.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

# Status values a trigger can report. `processing` is in-flight; the other
# two are terminal.
PROCESSING = "processing"
OK = "ok"
ERROR = "error"


@dataclass
class TriggerResult:
    """Mutable record of one `/process` run, updated as the run progresses.

    The background task owns the writes; the `/process/{trigger_id}` handler
    reads concurrently — so every read and write is guarded by `_lock`.
    """

    trigger_id: str
    repo: str
    pr_number: int
    instance: str
    status: str = PROCESSING
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    comments: int = 0
    fixes: int = 0
    dismisses: int = 0
    skips: int = 0
    slack_messages: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def record_decision(self, decision: str | None) -> None:
        """Tally one handled review comment by its LLM verdict.

        `decision` is "fix" | "dismiss" | "skip", or None when the bot
        couldn't reach a verdict (unreadable file / LLM error)."""
        with self._lock:
            self.comments += 1
            if decision == "fix":
                self.fixes += 1
            elif decision == "dismiss":
                self.dismisses += 1
            elif decision == "skip":
                self.skips += 1

    def add_slack_message(self, ref: dict[str, Any] | None) -> None:
        """Append a Slack message ref ({"ts", "channel"}). No-op without a ts."""
        if not ref or not ref.get("ts"):
            return
        with self._lock:
            self.slack_messages.append(ref)

    def finish(self, status: str, *, error: str | None = None) -> None:
        with self._lock:
            self.status = status
            self.error = error
            self.finished_at = time.time()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "trigger_id": self.trigger_id,
                "repo": self.repo,
                "pr": self.pr_number,
                "instance": self.instance,
                "status": self.status,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "counts": {
                    "comments": self.comments,
                    "fixes": self.fixes,
                    "dismisses": self.dismisses,
                    "skips": self.skips,
                },
                "slack_messages": list(self.slack_messages),
                "error": self.error,
            }


class TriggerStore:
    """Thread-safe, bounded store of `TriggerResult` keyed by `trigger_id`.

    Oldest entries are evicted once `max_size` is exceeded. The background
    task mutates its own `TriggerResult` in place, so a reader always sees
    live progress for triggers still in the store.
    """

    def __init__(self, max_size: int = 500) -> None:
        self._max_size = max(max_size, 1)
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, TriggerResult]" = OrderedDict()

    def create(
        self, *, trigger_id: str, repo: str, pr_number: int, instance: str
    ) -> TriggerResult:
        result = TriggerResult(
            trigger_id=trigger_id,
            repo=repo,
            pr_number=pr_number,
            instance=instance,
        )
        with self._lock:
            self._items[trigger_id] = result
            self._items.move_to_end(trigger_id)
            while len(self._items) > self._max_size:
                self._items.popitem(last=False)
        return result

    def get(self, trigger_id: str) -> TriggerResult | None:
        with self._lock:
            return self._items.get(trigger_id)
