"""TriggerStore / TriggerResult — the in-memory /process results store."""
from __future__ import annotations

from triggers import OK, PROCESSING, TriggerStore


def _store(max_size: int = 3) -> TriggerStore:
    return TriggerStore(max_size=max_size)


def test_create_and_get():
    store = _store()
    result = store.create(trigger_id="t1", repo="o/r", pr_number=5, instance="gh")
    assert result.status == PROCESSING
    assert store.get("t1") is result
    assert store.get("missing") is None


def test_create_defaults_source_to_manual():
    result = _store().create(trigger_id="t", repo="o/r", pr_number=1, instance="gh")
    assert result.source == "manual"
    assert result.as_dict()["source"] == "manual"


def test_create_records_webhook_source():
    result = _store().create(
        trigger_id="t", repo="o/r", pr_number=1, instance="gh", source="webhook"
    )
    assert result.source == "webhook"
    assert result.as_dict()["source"] == "webhook"


def test_recent_returns_newest_first():
    store = _store(max_size=10)
    for i in range(4):
        store.create(trigger_id=f"t{i}", repo="o/r", pr_number=i, instance="gh")
        # Force a strictly increasing started_at so ordering is deterministic.
        store.get(f"t{i}").started_at = float(i)
    ordered = [r.trigger_id for r in store.recent()]
    assert ordered == ["t3", "t2", "t1", "t0"]


def test_recent_honours_limit():
    store = _store(max_size=10)
    for i in range(5):
        store.create(trigger_id=f"t{i}", repo="o/r", pr_number=i, instance="gh")
    assert len(store.recent(limit=2)) == 2
    assert store.recent(limit=0) == []


def test_record_decision_counts():
    result = _store().create(trigger_id="t", repo="o/r", pr_number=1, instance="gh")
    for decision in ("fix", "fix", "dismiss", "skip", None):
        result.record_decision(decision)
    assert result.as_dict()["counts"] == {
        "comments": 5, "fixes": 2, "dismisses": 1, "skips": 1,
    }


def test_add_slack_message_skips_refs_without_a_ts():
    result = _store().create(trigger_id="t", repo="o/r", pr_number=1, instance="gh")
    result.add_slack_message(None)
    result.add_slack_message({})
    result.add_slack_message({"channel": "C1"})  # no ts -> ignored
    result.add_slack_message({"ts": "1.2", "channel": "C1"})
    assert result.as_dict()["slack_messages"] == [{"ts": "1.2", "channel": "C1"}]


def test_finish_sets_terminal_state():
    result = _store().create(trigger_id="t", repo="o/r", pr_number=1, instance="gh")
    result.finish(OK)
    snapshot = result.as_dict()
    assert snapshot["status"] == "ok"
    assert snapshot["finished_at"] is not None
    assert snapshot["error"] is None


def test_finish_error_records_reason():
    result = _store().create(trigger_id="t", repo="o/r", pr_number=1, instance="gh")
    result.finish("error", error="GitHubError")
    assert result.as_dict()["error"] == "GitHubError"


def test_store_evicts_oldest_over_capacity():
    store = _store(max_size=3)
    for i in range(5):
        store.create(trigger_id=f"t{i}", repo="o/r", pr_number=i, instance="gh")
    assert store.get("t0") is None
    assert store.get("t1") is None
    assert store.get("t2") is not None
    assert store.get("t4") is not None
