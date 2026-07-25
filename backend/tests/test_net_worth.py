"""Tests for Story 32 — Net worth tracking."""

import pytest
import json


# Net worth is computed over `account_meta`, which starts empty on a fresh
# database, so these shape tests have to declare the accounts they expect.
# Account identity is data now: this is the same POST a user makes to name and
# classify their accounts, with invented ids.
DEMO_ACCOUNTS = {
    "bank-0001": {"display_name": "Everyday Checking", "kind": "checking", "sort_order": 1},
    "card-0002": {"display_name": "Rewards Card", "kind": "credit", "sort_order": 2},
    "home-0003": {"display_name": "Residence", "kind": "real_estate", "sort_order": 3},
}


@pytest.fixture(autouse=True)
def demo_accounts(test_client):
    resp = test_client.post("/api/accounts/meta", json={"accounts": DEMO_ACCOUNTS})
    assert resp.status_code == 200


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

    # Should have checking, credit, and real_estate accounts
    kinds = set(a["kind"] for a in data["accounts"])
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
            # debt could be 0 if amortization table is empty (test DB)


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


def test_kind_from_account_meta_drives_arithmetic(test_client):
    """`kind` is what makes a balance an asset, a liability or a property line.

    It used to be a constant in the router, so this arithmetic could not be
    corrected without a code change. Reclassifying the property must move the
    estimated property value off the asset side.
    """
    before = test_client.get("/api/net-worth").json()
    assert [a["kind"] for a in before["accounts"] if a["id"] == "home-0003"] == ["real_estate"]
    assert [a["name"] for a in before["accounts"] if a["id"] == "home-0003"] == ["Residence"]

    resp = test_client.patch("/api/accounts/home-0003/meta", json={"kind": "checking"})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "checking"

    after = test_client.get("/api/net-worth").json()
    assert all(a["kind"] != "real_estate" for a in after["accounts"])
    assert after["assets"] < before["assets"]
    assert after["assets"] - after["liabilities"] == after["total_net_worth_cents"]


def test_display_name_is_editable(test_client):
    """The owner renames an account through the API, not by editing source."""
    resp = test_client.patch("/api/accounts/bank-0001/meta",
                             json={"display_name": "Renamed Checking"})
    assert resp.status_code == 200

    nw = test_client.get("/api/net-worth").json()
    assert any(a["name"] == "Renamed Checking" for a in nw["accounts"])


def test_rejects_unknown_kind(test_client):
    resp = test_client.patch("/api/accounts/bank-0001/meta", json={"kind": "crypto"})
    assert resp.status_code == 400
