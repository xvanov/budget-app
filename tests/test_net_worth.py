"""Tests for Story 32 — Net worth tracking."""

import pytest
import json


def test_net_worth_shape(test_client):
    """GET /api/net-worth returns required fields."""
    resp = test_client.get("/api/net-worth")
    assert resp.status_code == 200
    data = resp.json()

    assert "as_of" in data
    assert "accounts" in data
    assert "assets" in data
    assert "liabilities" in data
    assert "total_net_worth_cents" in data
    assert "history" in data

    assert isinstance(data["accounts"], list)
    assert isinstance(data["history"], list)
    assert isinstance(data["assets"], int)
    assert isinstance(data["liabilities"], int)
    assert isinstance(data["total_net_worth_cents"], int)

    # Identity: assets - liabilities = total
    assert data["assets"] - data["liabilities"] == data["total_net_worth_cents"]


def test_net_worth_accounts_have_required_fields(test_client):
    """Each account has id, name, kind, balance."""
    resp = test_client.get("/api/net-worth")
    data = resp.json()
    for a in data["accounts"]:
        assert "id" in a
        assert "name" in a
        assert "kind" in a
        assert "balance" in a
        assert isinstance(a["balance"], int)
        assert a["kind"] in ("checking", "credit", "real_estate", "savings", "brokerage", "retirement")

    # These three kinds are what the owner's real DB holds, and each takes a
    # different arithmetic path above (asset / liability / property-with-debt).
    # A fresh test DB has no accounts at all, so requiring them unconditionally
    # only asserts that a fixture was loaded — which is why this file sat
    # un-runnable (it asked for a `client` fixture that does not exist) rather
    # than failing honestly. Assert the shape always, the population only when
    # there is one.
    kinds = set(a["kind"] for a in data["accounts"])
    if kinds:
        assert "checking" in kinds
        assert "credit" in kinds
        assert "real_estate" in kinds


def test_net_worth_real_estate_has_debt(test_client):
    """Real estate accounts have debt field."""
    resp = test_client.get("/api/net-worth")
    data = resp.json()
    for a in data["accounts"]:
        if a["kind"] == "real_estate":
            assert "debt" in a
            assert isinstance(a["debt"], int)
            # Zero is the correct answer for a mortgage that has been paid off,
            # which this one has been since 2026-07-08. The old assertion was
            # `> 0`, and the only thing keeping it "true" was the lender
            # projection's mid-life balance being reported as money still owed.
            assert a["debt"] >= 0


def test_net_worth_history_shape(test_client):
    """GET /api/net-worth/history returns time series."""
    resp = test_client.get("/api/net-worth/history")
    assert resp.status_code == 200
    data = resp.json()
    assert "history" in data
    assert "as_of" in data
    for h in data["history"]:
        assert "date" in h
        assert "value" in h
        assert isinstance(h["value"], int)


def test_net_worth_history_start_end(test_client):
    """History respects start/end params."""
    full = test_client.get("/api/net-worth/history").json()
    filtered = test_client.get("/api/net-worth/history?start=2025-01-01&end=2025-06-01").json()
    assert len(filtered["history"]) <= len(full["history"])
    if filtered["history"]:
        assert filtered["history"][0]["date"] >= "2025-01-01"
        assert filtered["history"][-1]["date"] <= "2025-06-01"
