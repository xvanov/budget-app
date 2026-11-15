"""Taking over bank connections from BonusPilot, and keeping manual labels intact.

Plaid is stubbed. The label tests matter most: a transaction the owner
categorized by hand must keep that category through a re-sync and through a
pending transaction being replaced by its posted version.
"""
import base64

import aiosqlite
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from app.services import plaid_client, plaid_sync

TEST_KEY = base64.b64encode(b"budget-app-test-key-not-a-secret").decode()
TOKEN = "test-integration-token"
ACCESS = "access-sandbox-SECRET-VALUE-123"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", TEST_KEY)
    monkeypatch.setenv("INTEGRATION_TOKEN", TOKEN)


@pytest_asyncio.fixture
async def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "import.db")
    import app.database as database

    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    return path


@pytest_asyncio.fixture
async def db(db_path):
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    yield conn
    await conn.close()


@pytest.fixture
def api(db_path):
    from app.main import app

    with TestClient(app) as client:
        yield client


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def stub_bank(monkeypatch, pages):
    """Serve the given transactions/sync pages and one account for any item."""
    monkeypatch.setattr(
        plaid_client,
        "fetch_item_accounts",
        lambda token: {
            "institution_id": "ins_9",
            "accounts": [{
                "account_id": "acct-1", "name": "Checking", "official_name": None,
                "mask": "7777", "type": "depository", "subtype": "checking",
                "balances": {"current": 900.0, "available": 880.0},
            }],
        },
    )
    queue = list(pages)
    monkeypatch.setattr(plaid_client, "transactions_sync", lambda token, cursor=None, count=500: queue.pop(0))


def page(added=(), modified=(), removed=()):
    return {
        "added": list(added), "modified": list(modified),
        "removed": [{"transaction_id": t} for t in removed],
        "next_cursor": "c", "has_more": False,
    }


def txn(tid, amount=12.0, pending=False, pending_transaction_id=None):
    return {
        "transaction_id": tid, "account_id": "acct-1", "amount": amount,
        "iso_currency_code": "USD", "date": "2026-07-01", "authorized_date": None,
        "pending": pending, "pending_transaction_id": pending_transaction_id,
        "name": "CORNER MARKET", "original_description": "CORNER MARKET",
        "merchant_name": "Corner Market", "counterparties": [],
        "personal_finance_category": {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES"},
        "payment_channel": "in store",
    }


# ── import endpoint ───────────────────────────────────────────────────────────

def test_import_requires_the_token(api):
    response = api.post("/api/integration/v1/items/import", json={"item_id": "i", "access_token": ACCESS})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_import_stores_the_token_encrypted_and_syncs_once(api, db, monkeypatch):
    stub_bank(monkeypatch, [page(added=[txn("t-1")])])
    response = api.post(
        "/api/integration/v1/items/import",
        headers=auth(),
        json={"item_id": "item-bp", "access_token": ACCESS, "institution_name": "Chase"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["account_count"] == 1
    assert body["sync"]["added"] == 1
    # The token itself never comes back, in the reply or in the stored row.
    assert ACCESS not in response.text
    row = await (await db.execute("SELECT access_token_encrypted FROM plaid_items WHERE item_id='item-bp'")).fetchone()
    assert row is not None
    assert ACCESS not in row[0]


@pytest.mark.asyncio
async def test_import_is_repeatable_for_the_same_item(api, db, monkeypatch):
    stub_bank(monkeypatch, [page(added=[txn("t-1")]), page(added=[])])
    headers = auth()
    payload = {"item_id": "item-bp", "access_token": ACCESS}
    assert api.post("/api/integration/v1/items/import", headers=headers, json=payload).status_code == 200
    assert api.post("/api/integration/v1/items/import", headers=headers, json=payload).status_code == 200
    count = (await (await db.execute("SELECT COUNT(*) FROM plaid_items WHERE item_id='item-bp'")).fetchone())[0]
    assert count == 1


# ── manual labels survive ─────────────────────────────────────────────────────

async def _label(db, external_id, category_path):
    await db.execute(
        "UPDATE transactions SET category_path=?, entity_type='personal', categorization_reason='manual' "
        "WHERE external_id=?",
        (category_path, external_id),
    )
    await db.commit()


async def _category(db, external_id):
    row = await (await db.execute(
        "SELECT category_path, entity_type FROM transactions WHERE external_id=?", (external_id,)
    )).fetchone()
    return tuple(row) if row else None


@pytest.mark.asyncio
async def test_a_manual_label_survives_a_full_resync(db, db_path, monkeypatch):
    item_db_id = await plaid_sync.store_item(db, "item-x", "tok", None, "Bank")
    await plaid_sync.store_accounts(db, item_db_id, [{
        "account_id": "acct-1", "name": "Checking", "mask": "7777", "type": "depository",
        "subtype": "checking", "balances": {"current": 1.0, "available": 1.0},
    }])
    await db.commit()

    # First sync brings the transaction in.
    stub_bank(monkeypatch, [page(added=[txn("t-1")])])
    await plaid_sync.sync_item(db, item_db_id)
    await _label(db, "t-1", "Expense > Personal > Groceries")

    # A full re-pull (cursor cleared) sends the same transaction again, changed.
    stub_bank(monkeypatch, [page(modified=[txn("t-1", amount=12.5)])])
    await db.execute("UPDATE plaid_items SET cursor=NULL WHERE id=?", (item_db_id,))
    await db.commit()
    await plaid_sync.sync_item(db, item_db_id)

    assert await _category(db, "t-1") == ("Expense > Personal > Groceries", "personal")
    amount = (await (await db.execute("SELECT amount_cents FROM transactions WHERE external_id='t-1'")).fetchone())[0]
    assert amount == -1250  # the data did update; only the label was kept


@pytest.mark.asyncio
async def test_a_manual_label_moves_to_the_posted_row_when_a_pending_one_posts(db, db_path, monkeypatch):
    item_db_id = await plaid_sync.store_item(db, "item-y", "tok", None, "Bank")
    await plaid_sync.store_accounts(db, item_db_id, [{
        "account_id": "acct-1", "name": "Checking", "mask": "7777", "type": "depository",
        "subtype": "checking", "balances": {"current": 1.0, "available": 1.0},
    }])
    await db.commit()

    stub_bank(monkeypatch, [page(added=[txn("hold-1", pending=True)])])
    await plaid_sync.sync_item(db, item_db_id)
    await _label(db, "hold-1", "Expense > Personal > Groceries")

    stub_bank(monkeypatch, [page(added=[txn("posted-1", pending_transaction_id="hold-1")])])
    await plaid_sync.sync_item(db, item_db_id)

    assert await _category(db, "posted-1") == ("Expense > Personal > Groceries", "personal")
    assert await _category(db, "hold-1") is None


# ── campaign events from BonusPilot ───────────────────────────────────────────

def _event(**overrides):
    base = {"external_key": "bp:acct-9:opened", "kind": "account_opened",
            "title": "Opened Chase Sapphire Checking", "occurred_on": "2026-09-02",
            "amount_cents": None, "account_ref": "acct-9", "source": "bonuspilot"}
    base.update(overrides)
    return base


def test_campaign_event_needs_the_token(api):
    assert api.post("/api/integration/v1/campaign-events", json=_event()).status_code == 401


def test_campaign_event_is_stored_once_and_updated_in_place(api, db_path):
    headers = auth()
    assert api.post("/api/integration/v1/campaign-events", headers=headers, json=_event()).status_code == 200
    assert api.post("/api/integration/v1/campaign-events", headers=headers,
                    json=_event(title="Opened Chase Sapphire Checking (renamed)")).status_code == 200
    events = api.get("/api/integration/v1/campaign-events", headers=headers).json()["events"]
    assert len(events) == 1
    assert events[0]["title"].endswith("(renamed)")


def test_campaign_event_rejects_a_bad_date(api):
    response = api.post("/api/integration/v1/campaign-events", headers=auth(),
                        json=_event(occurred_on="2026-13-40"))
    assert response.status_code == 422
