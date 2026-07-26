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


# Net worth counts LINKED accounts only: an unlinked account has no balance
# anywhere in the database, and the owner's instruction was to omit those rather
# than estimate one from imported transactions ("you shouldn't be making up
# things"). So the fixture has to link them, which is also what makes these tests
# exercise the real path — a balance the bank reported.
DEMO_PLAID_BALANCES = {
    "bank-0001": ("depository", 250000),    # $2,500.00 asset
    "card-0002": ("credit", 40000),         # $400.00 OWED -> -$400.00 here
    "home-0003": ("depository", 100000),    # the residence's own cash
}


@pytest.fixture(autouse=True)
def demo_accounts(test_client):
    resp = test_client.post("/api/accounts/meta", json={"accounts": DEMO_ACCOUNTS})
    assert resp.status_code == 200
    _link_demo_accounts()


def _link_demo_accounts():
    """Give each demo account a Plaid row with a reported balance."""
    import os
    import sqlite3

    con = sqlite3.connect(os.environ["DB_PATH"])
    try:
        con.execute(
            """INSERT OR IGNORE INTO plaid_items (item_id, access_token_encrypted,
                   institution_id, institution_name) VALUES ('demo-item','x','ins_demo','Demo')"""
        )
        item_id = con.execute(
            "SELECT id FROM plaid_items WHERE item_id='demo-item'").fetchone()[0]
        for aid, (ptype, cents) in DEMO_PLAID_BALANCES.items():
            con.execute(
                """INSERT INTO plaid_accounts (item_db_id, plaid_account_id, name, mask,
                       type, canonical_account_id, include_in_sync, current_balance_cents)
                   VALUES (?,?,?,?,?,?,1,?)
                   ON CONFLICT(plaid_account_id) DO UPDATE SET
                       current_balance_cents=excluded.current_balance_cents,
                       type=excluded.type, canonical_account_id=excluded.canonical_account_id""",
                (item_id, f"plaid-{aid}", DEMO_ACCOUNTS[aid]["display_name"],
                 aid[-4:], ptype, aid, cents),
            )
        con.commit()
    finally:
        con.close()


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
    # A `real_estate` account now emits TWO lines: the property itself, under
    # `<id>::property`, and the bank account's own cash under the plain id. They
    # used to be collapsed into one, which meant the property value REPLACED the
    # cash rather than being counted beside it — the cash disappeared from net
    # worth entirely.
    prop = [a for a in before["accounts"] if a["id"] == "home-0003::property"]
    assert [a["kind"] for a in prop] == ["real_estate"]
    assert [a["name"] for a in prop] == ["Residence"]
    cash = [a for a in before["accounts"] if a["id"] == "home-0003"]
    assert len(cash) == 1 and cash[0]["kind"] != "real_estate"

    resp = test_client.patch("/api/accounts/home-0003/meta", json={"kind": "checking"})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "checking"

    after = test_client.get("/api/net-worth").json()
    assert all(a["kind"] != "real_estate" for a in after["accounts"])
    # Reclassifying drops the property line, so the property value leaves assets.
    assert not any(a["id"].endswith("::property") for a in after["accounts"])
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


def test_unlinked_accounts_are_excluded_not_estimated(test_client):
    """An account with no bank-reported balance must not reach the total.

    The database holds the net of whatever transactions were imported, which is
    NOT a balance — on the owner's real data chase-3600 summed to -$315,523 while
    the bank reported +$14,356.70, because the import starts mid-life and no
    opening balance is stored anywhere. Estimating from flows therefore put a
    six-figure fiction in the headline. The owner's instruction was explicit:
    exclude accounts that are not connected, and do not invent figures for them.
    """
    before = test_client.get("/api/net-worth").json()
    linked_ids = {a["id"] for a in before["accounts"]}
    assert "bank-0001" in linked_ids

    # Declare an account that exists in account_meta but has no Plaid link.
    resp = test_client.post("/api/accounts/meta", json={"accounts": {
        "ghost-0009": {"display_name": "Closed Cash App", "kind": "checking", "sort_order": 9},
    }})
    assert resp.status_code == 200

    after = test_client.get("/api/net-worth").json()
    ids = {a["id"] for a in after["accounts"]}
    assert "ghost-0009" not in ids, "an unlinked account must not be counted"

    # It is listed, though — omitted from the arithmetic is not the same as hidden.
    assert any(u["id"] == "ghost-0009" for u in after["unlinked_accounts"])
    assert "Closed Cash App" in after["balance_confidence"]["excluded_unlinked_accounts"]

    # Nothing in the total is an estimate, and the total did not move.
    assert after["balance_confidence"]["lines_estimated_from_flows"] == 0
    assert all(a.get("balance_basis") in (None, "plaid") for a in after["accounts"])
    assert after["total_net_worth_cents"] == before["total_net_worth_cents"]
    assert after["assets"] - after["liabilities"] == after["total_net_worth_cents"]


def test_history_ends_at_the_headline(test_client):
    """The chart's last point must equal the number printed above it.

    They were computed two different ways: the headline moved to bank-reported
    balances while the history kept its own unscoped flow sums, and the two
    disagreed by $211,071.90 on real data — a chart contradicting its own KPI.
    """
    data = test_client.get("/api/net-worth").json()
    history = data["history"]
    if not history:
        pytest.skip("no transactions in this fixture, so there is no series to check")
    assert history[-1]["value"] == data["total_net_worth_cents"]
    assert history[-1]["assets"] == data["assets"]
    assert history[-1]["liabilities"] == data["liabilities"]
