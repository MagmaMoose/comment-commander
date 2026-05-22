"""_process_pr must serialize concurrent runs.

GitHub fans one review out into several webhook deliveries; before _PROCESS_LOCK
their _process_pr runs executed concurrently and raced on `git push` to the same
branch — the losers got "fetch first" and silently dropped their commit, reply
and thread-resolve.
"""
from __future__ import annotations

import threading
import time

import processor


def test_process_pr_serializes_concurrent_runs(monkeypatch):
    """Five threads enter _process_pr at once; _PROCESS_LOCK must ensure the
    real worker (_process_pr_locked) never runs in two threads at the same
    time — otherwise concurrent deliveries race on `git push`."""
    inside = 0
    peak = 0
    bookkeeping = threading.Lock()

    def fake_locked(**kwargs):
        # No barrier here: _PROCESS_LOCK admits one thread at a time, so all
        # five could never reach a Barrier(5) together — it would deadlock.
        # The sleep below gives ample overlap window if the lock were absent.
        nonlocal inside, peak
        with bookkeeping:
            inside += 1
            peak = max(peak, inside)
        time.sleep(0.03)  # hold the section long enough to overlap if unlocked
        with bookkeeping:
            inside -= 1

    monkeypatch.setattr(processor, "_process_pr_locked", fake_locked)

    threads = [threading.Thread(target=processor._process_pr) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 1, f"expected serialized execution, saw {peak} runs at once"


def test_process_pr_forwards_kwargs_to_locked(monkeypatch):
    """The wrapper must pass every argument straight through to the worker."""
    seen: dict = {}
    monkeypatch.setattr(processor, "_process_pr_locked", lambda **kw: seen.update(kw))
    processor._process_pr(instance="i", pr_number=7, delivery="d")
    assert seen == {"instance": "i", "pr_number": 7, "delivery": "d"}
