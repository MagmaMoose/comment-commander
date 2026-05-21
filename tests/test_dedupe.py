"""Delivery dedupe."""
from __future__ import annotations

from dedupe import DeliveryStore


def test_first_claim_succeeds(tmp_path):
    store = DeliveryStore(tmp_path / "d.db")
    assert store.claim("abc") is True


def test_second_claim_for_same_id_fails(tmp_path):
    store = DeliveryStore(tmp_path / "d.db")
    store.claim("abc")
    assert store.claim("abc") is False


def test_different_ids_are_independent(tmp_path):
    store = DeliveryStore(tmp_path / "d.db")
    assert store.claim("a") is True
    assert store.claim("b") is True


def test_persists_across_instances(tmp_path):
    path = tmp_path / "d.db"
    DeliveryStore(path).claim("abc")
    assert DeliveryStore(path).claim("abc") is False
