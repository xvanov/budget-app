"""BonusPilot integration API: auth, change feed paging, and what a consumer sees.

Plaid is stubbed (no network, no LLM). Transactions go in through the real
sync_item path, so the change log is written by the code that ships.
"""
import base64

import aiosqlite
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.services import plaid_sync

TEST_KEY = base64.b64encode(b"budget-app-test-key-not-a-secret").decode()
TOKEN = "test-integration-token"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setenv("INTEGRATION_TOKEN", TOKEN)


@pytest_asyncio.fixture
async def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "integration.db")
    import app.database as database

    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    return path


@pytest_asyncio.fixture
async def db(db_path):
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    await conn.close()


@pytest.fixture
def api(db_path):
    from app.main import app

    with TestClient(app) as client:
        yield client


class StubPlaid:
    def __init__(self):
        self.pages = []

    def queue(self, added=(), modified=(), removed=()):
        self.pages.append({
            "added": list(added), "modified": list(modified),
            "removed": [{"transaction_id": t} for t in removed],
            "next_cursor": f"plaid-cursor-{len(self.pages) + 1}", "has_more": False,
        })

    def transactions_sync(self, access_token, cursor=None, count=500):
        return self.pages.pop(0)


def plaid_txn(transaction_id, amount, account="acct-1", pfc=None, pending=False,
              pending_transaction_id=None, name="Some merchant"):
    """A Plaid transaction dict as the live client returns it (positive = money out)."""
    return {
        "transaction_id": transaction_id,
        "account_id": account,
        "amount": amount,
        "iso_currency_code": "USD",
        "date": "2026-07-01",
        "authorized_date": "2026-06-30",
        "pending": pending,
        "pending_transaction_id": pending_transaction_id,
        "name": name,
        "original_description": name,
        "merchant_name": None,
        "counterparties": [],
        "personal_finance_category": pfc or {"primary": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_OTHER"},
        "payment_channel": "online",
    }


async def link_item(db, item_id, accounts):
    """Store an item and its accounts exactly as the link flow does."""
    item_db_id = await plaid_sync.store_item(db, item_id, f"access-{item_id}", "ins_1", "Test Bank")
    await plaid_sync.store_accounts(db, item_db_id, accounts)
    return item_db_id


def account(account_id, mask):
    return {"account_id": account_id, "name": "Checking", "official_name": None, "mask": mask,
            "type": "depository", "subtype": "checking", "balances": {"current": 1500.25, "available": 1400.0}}


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# ── auth ──────────────────────────────────────────────────────────────────────

def test_rejects_requests_without_a_token(api):
    assert api.get("/api/integration/v1/items").status_code == 401


def test_rejects_a_wrong_token(api):
    response = api.get("/api/integration/v1/items", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


def test_rejects_a_token_without_the_bearer_scheme(api):
    response = api.get("/api/integration/v1/items", headers={"Authorization": TOKEN})
    assert response.status_code == 401


def test_fails_closed_when_no_token_is_configured(api, monkeypatch):
    monkeypatch.delenv("INTEGRATION_TOKEN", raising=False)
    response = api.get("/api/integration/v1/items", headers=auth())
    assert response.status_code == 503


# ── accounts and items ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lists_linked_items_and_their_accounts_in_cents(db, api):
    await link_item(db, "item-A", [account("acct-1", "1234")])
    await db.commit()

    items = api.get("/api/integration/v1/items", headers=auth()).json()["items"]
    assert [i["item_id"] for i in items] == ["item-A"]
    assert items[0]["institution_name"] == "Test Bank"

    accounts = api.get("/api/integration/v1/items/item-A/accounts", headers=auth()).json()["accounts"]
    assert accounts[0]["plaid_account_id"] == "acct-1"
    assert accounts[0]["mask"] == "1234"
    assert accounts[0]["current_balance_cents"] == 150025
    assert accounts[0]["available_balance_cents"] == 140000
    assert "access_token" not in str(accounts)


def test_unknown_item_is_404(api):
    assert api.get("/api/integration/v1/items/nope/transactions", headers=auth()).status_code == 404


# ── transaction change feed ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_feed_serves_plaid_shaped_transactions_with_plaid_sign_and_categories(db, api):
    await link_item(db, "item-A", [account("acct-1", "1234")])
    stub = StubPlaid()
    stub.queue(added=[
        # A paycheck: Plaid reports money IN as negative.
        plaid_txn("pay-1", -2000.50, pfc={"primary": "INCOME", "detailed": "INCOME_WAGES"}, name="ACME PAYROLL"),
        # A card purchase: money OUT is positive.
        plaid_txn("buy-1", 12.34, name="COFFEE"),
    ])
    await plaid_sync.sync_item(db, 1, client=stub)
    await db.commit()

    body = api.get("/api/integration/v1/items/item-A/transactions", headers=auth()).json()
    upserts = {c["transaction"]["transaction_id"]: c["transaction"] for c in body["changes"] if c["op"] == "upsert"}

    pay = upserts["pay-1"]
    assert pay["amount"] == -2000.5
    assert pay["account_id"] == "acct-1"
    assert pay["personal_finance_category"] == {"primary": "INCOME", "detailed": "INCOME_WAGES"}
    assert upserts["buy-1"]["amount"] == 12.34
    assert body["has_more"] is False


@pytest.mark.asyncio
async def test_pending_then_posted_reports_the_pending_removal_and_the_posted_row(db, api):
    await link_item(db, "item-A", [account("acct-1", "1234")])
    stub = StubPlaid()
    stub.queue(added=[plaid_txn("hold-1", 40.00, pending=True, name="GAS")])
    await plaid_sync.sync_item(db, 1, client=stub)
    stub.queue(added=[plaid_txn("posted-1", 40.00, pending_transaction_id="hold-1", name="GAS")])
    await plaid_sync.sync_item(db, 1, client=stub)
    await db.commit()

    changes = api.get("/api/integration/v1/items/item-A/transactions", headers=auth()).json()["changes"]
    ops = [(c["op"], c.get("transaction_id") or c["transaction"]["transaction_id"]) for c in changes]
    assert ("removed", "hold-1") in ops
    posted = [c["transaction"] for c in changes if c["op"] == "upsert" and c["transaction"]["transaction_id"] == "posted-1"]
    assert posted and posted[-1]["pending"] is False
    # The pending row is not reported as a live transaction any more.
    live_ids = {c["transaction"]["transaction_id"] for c in changes if c["op"] == "upsert"}
    assert "hold-1" not in live_ids


@pytest.mark.asyncio
async def test_removed_transactions_are_reported_as_removed(db, api):
    await link_item(db, "item-A", [account("acct-1", "1234")])
    stub = StubPlaid()
    stub.queue(added=[plaid_txn("dup-1", 5.00)])
    await plaid_sync.sync_item(db, 1, client=stub)
    stub.queue(removed=["dup-1"])
    await plaid_sync.sync_item(db, 1, client=stub)
    await db.commit()

    changes = api.get("/api/integration/v1/items/item-A/transactions", headers=auth()).json()["changes"]
    # The row is gone now, so the earlier "added" entry is served as a removal
    # too. Both say the same thing, and removal is idempotent for the client.
    assert [c["op"] for c in changes] == ["removed", "removed"]
    assert {c["transaction_id"] for c in changes} == {"dup-1"}


@pytest.mark.asyncio
async def test_paging_with_cursor_returns_every_change_exactly_once(db, api):
    await link_item(db, "item-A", [account("acct-1", "1234")])
    stub = StubPlaid()
    stub.queue(added=[plaid_txn(f"t-{i}", float(i + 1)) for i in range(7)])
    await plaid_sync.sync_item(db, 1, client=stub)
    await db.commit()

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        body = api.get("/api/integration/v1/items/item-A/transactions", headers=auth(), params=params).json()
        seen += [c["transaction"]["transaction_id"] for c in body["changes"]]
        cursor = body["next_cursor"]
        pages += 1
        if not body["has_more"]:
            break

    assert seen == [f"t-{i}" for i in range(7)]
    assert pages == 3

    # Caught up: asking again from the last cursor returns nothing new.
    again = api.get("/api/integration/v1/items/item-A/transactions", headers=auth(), params={"cursor": cursor}).json()
    assert again["changes"] == []
    assert again["next_cursor"] == cursor


@pytest.mark.asyncio
async def test_a_later_sync_only_shows_what_is_new(db, api):
    await link_item(db, "item-A", [account("acct-1", "1234")])
    stub = StubPlaid()
    stub.queue(added=[plaid_txn("old-1", 1.0)])
    await plaid_sync.sync_item(db, 1, client=stub)
    await db.commit()
    first = api.get("/api/integration/v1/items/item-A/transactions", headers=auth()).json()

    stub.queue(added=[plaid_txn("new-1", 2.0)])
    await plaid_sync.sync_item(db, 1, client=stub)
    await db.commit()
    second = api.get("/api/integration/v1/items/item-A/transactions", headers=auth(),
                     params={"cursor": first["next_cursor"]}).json()
    assert [c["transaction"]["transaction_id"] for c in second["changes"]] == ["new-1"]


@pytest.mark.asyncio
async def test_one_item_never_sees_another_items_changes(db, api):
    await link_item(db, "item-A", [account("acct-1", "1234")])
    await link_item(db, "item-B", [account("acct-9", "9999")])
    stub = StubPlaid()
    stub.queue(added=[plaid_txn("a-1", 1.0, account="acct-1")])
    await plaid_sync.sync_item(db, 1, client=stub)
    stub.queue(added=[plaid_txn("b-1", 1.0, account="acct-9")])
    await plaid_sync.sync_item(db, 2, client=stub)
    await db.commit()

    a = api.get("/api/integration/v1/items/item-A/transactions", headers=auth()).json()["changes"]
    b = api.get("/api/integration/v1/items/item-B/transactions", headers=auth()).json()["changes"]
    assert [c["transaction"]["transaction_id"] for c in a] == ["a-1"]
    assert [c["transaction"]["transaction_id"] for c in b] == ["b-1"]


def test_rejects_a_non_integer_cursor(api):
    response = api.get("/api/integration/v1/items/item-A/transactions", headers=auth(), params={"cursor": "abc"})
    assert response.status_code == 400
