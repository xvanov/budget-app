"""Plaid sync against a stubbed client — no network, no LLM.

Covers the things that quietly corrupt data if wrong: the amount-sign
inversion, pending->posted upgrades, cursor persistence across pages, and error
handling that must not discard the cursor.
"""
import base64

import aiosqlite
import pytest
import pytest_asyncio

from app.services import plaid_sync
from app.services.plaid_sync import (
    STATUS_ERROR,
    STATUS_LOGIN_REQUIRED,
    canonical_account_for_mask,
    plaid_transaction_to_row,
)

TEST_KEY = base64.b64encode(b"budget-app-test-key-not-a-secret").decode()


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", TEST_KEY)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import app.database as database

    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.db"))
    await database.init_db()
    conn = await aiosqlite.connect(str(tmp_path / "test.db"))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    await conn.close()


class StubClient:
    """Serves canned transactions/sync pages and records the cursors requested."""

    def __init__(self, pages, fail_with=None):
        self.pages = pages
        self.fail_with = fail_with
        self.cursors_seen = []

    def transactions_sync(self, access_token, cursor=None, count=500):
        self.cursors_seen.append(cursor)
        if self.fail_with:
            raise self.fail_with
        return self.pages.pop(0)


def txn(transaction_id, amount, date="2026-07-01", account="plaid-acct-1", **extra):
    row = {
        "transaction_id": transaction_id,
        "account_id": account,
        "amount": amount,
        "date": date,
        "name": "AMAZON MKTPL*RT4XY",
        "merchant_name": "Amazon",
        "pending": False,
    }
    row.update(extra)
    return row


def page(added=(), modified=(), removed=(), next_cursor="cursor-1", has_more=False):
    return {
        "added": list(added),
        "modified": list(modified),
        "removed": list(removed),
        "next_cursor": next_cursor,
        "has_more": has_more,
    }


async def _seed_item(db, mask="0002", canonical="card-0002", include=1):
    item_db_id = await plaid_sync.store_item(
        db, item_id="item-abc", access_token="access-sandbox-token",
        institution_id="ins_56", institution_name="Chase",
    )
    await db.execute(
        """INSERT INTO plaid_accounts (item_db_id, plaid_account_id, name, mask,
               canonical_account_id, include_in_sync) VALUES (?,?,?,?,?,?)""",
        (item_db_id, "plaid-acct-1", "Amazon Prime Visa", mask, canonical, include),
    )
    await db.commit()
    return item_db_id


# ------------------------------------------------------------- pure mapping

def test_plaid_positive_amount_becomes_a_negative_expense():
    """Plaid reports an outflow as POSITIVE. This repo stores outflows NEGATIVE.

    Getting this backwards turns every purchase into income.
    """
    row = plaid_transaction_to_row(txn("t1", 46.01), "card-0002")
    assert row["amount_cents"] == -4601


def test_plaid_negative_amount_becomes_a_positive_credit():
    """A Plaid refund is negative, and must land positive here."""
    row = plaid_transaction_to_row(txn("t2", -64.33), "card-0002")
    assert row["amount_cents"] == 6433


def test_row_mapping_fields():
    row = plaid_transaction_to_row(
        txn("t3", 12.5, date="2026-07-04", authorized_date="2026-07-02",
            original_description="AMZN Mktp US*ABC", pending=True),
        "card-0002",
    )
    assert row["external_id"] == "t3"
    assert row["source_platform"] == "plaid"
    assert row["posted_date"] == "2026-07-04"
    assert row["transaction_date"] == "2026-07-02"
    assert row["pending"] == 1
    assert row["description"] == "AMZN Mktp US*ABC"
    assert row["normalized_description"] == "amzn mktp usabc"


def test_merchant_falls_back_through_counterparties_then_name():
    row = plaid_transaction_to_row(
        {"transaction_id": "t", "account_id": "a", "amount": 1, "date": "2026-01-01",
         "name": "RAW NAME", "counterparties": [{"name": "Counterparty Inc"}]},
        "card-0002",
    )
    assert row["merchant"] == "Counterparty Inc"
    row = plaid_transaction_to_row(
        {"transaction_id": "t", "account_id": "a", "amount": 1, "date": "2026-01-01",
         "name": "RAW NAME"},
        "card-0002",
    )
    assert row["merchant"] == "RAW NAME"


@pytest.mark.parametrize(
    "mask,expected",
    [
        ("0001", "home-0001"),
        ("0002", "card-0002"),
        ("0003", "bank-0003"),
        ("7777", "plaid-7777"),
        (None, "plaid-unknown"),
    ],
)
def test_canonical_account_mapping_reuses_existing_ids(mask, expected):
    # Masks are unique across these ids, so the mapping cannot be order-dependent.
    existing = ["home-0001", "card-0002", "bank-0003", "bank-0004"]
    assert canonical_account_for_mask(mask, existing) == expected


# ------------------------------------------------------------------ sync flow

@pytest.mark.asyncio
async def test_sync_inserts_rows_and_persists_cursor(db):
    item_db_id = await _seed_item(db)
    client = StubClient([page(added=[txn("t1", 46.01), txn("t2", 10.00)], next_cursor="cur-A")])

    result = await plaid_sync.sync_item(db, item_db_id, client=client)
    assert result["added"] == 2

    cursor = await db.execute("SELECT cursor, status, last_synced_at FROM plaid_items WHERE id=?", (item_db_id,))
    row = await cursor.fetchone()
    assert row["cursor"] == "cur-A"
    assert row["status"] == "ACTIVE"
    assert row["last_synced_at"]

    cursor = await db.execute(
        "SELECT external_id, account_id, amount_cents, source_platform FROM transactions ORDER BY external_id"
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    assert [r["external_id"] for r in rows] == ["t1", "t2"]
    assert all(r["account_id"] == "card-0002" for r in rows)
    assert all(r["source_platform"] == "plaid" for r in rows)
    assert rows[0]["amount_cents"] == -4601


@pytest.mark.asyncio
async def test_sync_follows_pagination_and_sends_the_advancing_cursor(db):
    item_db_id = await _seed_item(db)
    client = StubClient([
        page(added=[txn("t1", 1.00)], next_cursor="cur-1", has_more=True),
        page(added=[txn("t2", 2.00)], next_cursor="cur-2", has_more=False),
    ])
    result = await plaid_sync.sync_item(db, item_db_id, client=client)
    assert result["added"] == 2
    assert client.cursors_seen == [None, "cur-1"], "second page must request the first page's cursor"
    cursor = await db.execute("SELECT cursor FROM plaid_items WHERE id=?", (item_db_id,))
    assert (await cursor.fetchone())[0] == "cur-2"


@pytest.mark.asyncio
async def test_resync_with_same_transaction_id_updates_instead_of_duplicating(db):
    item_db_id = await _seed_item(db)
    await plaid_sync.sync_item(db, item_db_id, client=StubClient([page(added=[txn("t1", 46.01)])]))
    result = await plaid_sync.sync_item(
        db, item_db_id, client=StubClient([page(modified=[txn("t1", 50.00)])])
    )
    assert result["updated"] == 1
    assert result["added"] == 0
    cursor = await db.execute("SELECT COUNT(*), MIN(amount_cents) FROM transactions WHERE external_id='t1'")
    count, amount = await cursor.fetchone()
    assert count == 1, "one Plaid transaction must never become two rows"
    assert amount == -5000


@pytest.mark.asyncio
async def test_pending_transaction_is_upgraded_in_place_when_it_posts(db):
    """Plaid gives a posted charge a NEW id and points back via pending_transaction_id."""
    item_db_id = await _seed_item(db)
    await plaid_sync.sync_item(
        db, item_db_id, client=StubClient([page(added=[txn("pending-1", 46.01, pending=True)])])
    )
    result = await plaid_sync.sync_item(
        db, item_db_id,
        client=StubClient([page(added=[txn("posted-1", 46.01, pending=False,
                                          pending_transaction_id="pending-1")])]),
    )
    assert result["updated"] == 1
    cursor = await db.execute("SELECT external_id, pending FROM transactions")
    rows = [dict(r) for r in await cursor.fetchall()]
    assert len(rows) == 1, "pending + posted are one purchase, not two"
    assert rows[0]["external_id"] == "posted-1"
    assert rows[0]["pending"] == 0


@pytest.mark.asyncio
async def test_overlapping_syncs_do_not_violate_the_unique_index(db):
    """Regression: two syncs of the same item ran at once in production.

    A manual "Sync now" overlapped a scripted sync. Both passed the old
    SELECT-then-INSERT existence check and both inserted, so the loser died on
    `UNIQUE constraint failed: transactions.external_id` and the item was marked
    ERROR even though the rows were fine. Re-delivering the same transaction
    must be idempotent, not fatal.
    """
    item_db_id = await _seed_item(db)
    same_page = [txn("dup-1", 12.34), txn("dup-2", 56.78)]

    first = await plaid_sync.sync_item(db, item_db_id, client=StubClient([page(added=same_page)]))
    assert first["added"] == 2

    # Exactly what the second, overlapping sync would deliver.
    second = await plaid_sync.sync_item(db, item_db_id, client=StubClient([page(added=same_page)]))
    assert second["added"] == 0
    assert second["updated"] == 2

    cursor = await db.execute("SELECT COUNT(*) FROM transactions")
    assert (await cursor.fetchone())[0] == 2, "re-delivery must not duplicate rows"

    cursor = await db.execute("SELECT status, error_code FROM plaid_items WHERE id=?", (item_db_id,))
    row = await cursor.fetchone()
    assert row["status"] == "ACTIVE", "an overlapping sync must not leave the item in ERROR"
    assert row["error_code"] is None


@pytest.mark.asyncio
async def test_removed_transactions_are_deleted(db):
    item_db_id = await _seed_item(db)
    await plaid_sync.sync_item(db, item_db_id, client=StubClient([page(added=[txn("t1", 5.0)])]))
    result = await plaid_sync.sync_item(
        db, item_db_id, client=StubClient([page(removed=[{"transaction_id": "t1"}])])
    )
    assert result["removed"] == 1
    cursor = await db.execute("SELECT COUNT(*) FROM transactions")
    assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_transactions_for_excluded_accounts_are_skipped(db):
    item_db_id = await _seed_item(db, include=0)
    result = await plaid_sync.sync_item(db, item_db_id, client=StubClient([page(added=[txn("t1", 5.0)])]))
    assert result["added"] == 0
    assert result["skipped_accounts"] == 1
    cursor = await db.execute("SELECT COUNT(*) FROM transactions")
    assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_unknown_plaid_account_is_skipped_not_misfiled(db):
    item_db_id = await _seed_item(db)
    client = StubClient([page(added=[txn("t1", 5.0, account="some-other-account")])])
    result = await plaid_sync.sync_item(db, item_db_id, client=client)
    assert result["added"] == 0
    assert result["skipped_accounts"] == 1


# --------------------------------------------------------------- error handling

@pytest.mark.asyncio
async def test_login_required_sets_status_and_keeps_the_cursor(db):
    item_db_id = await _seed_item(db)
    await plaid_sync.sync_item(db, item_db_id, client=StubClient([page(added=[], next_cursor="cur-keep")]))

    class LoginRequired(Exception):
        body = '{"error_code":"ITEM_LOGIN_REQUIRED"}'

    with pytest.raises(LoginRequired):
        await plaid_sync.sync_item(db, item_db_id, client=StubClient([], fail_with=LoginRequired()))

    cursor = await db.execute("SELECT status, error_code, cursor FROM plaid_items WHERE id=?", (item_db_id,))
    row = await cursor.fetchone()
    assert row["status"] == STATUS_LOGIN_REQUIRED
    assert row["error_code"] == "ITEM_LOGIN_REQUIRED"
    assert row["cursor"] == "cur-keep", "discarding the cursor would force a full re-pull"


@pytest.mark.asyncio
async def test_generic_failure_sets_error_status(db):
    item_db_id = await _seed_item(db)
    with pytest.raises(RuntimeError):
        await plaid_sync.sync_item(db, item_db_id, client=StubClient([], fail_with=RuntimeError("boom")))
    cursor = await db.execute("SELECT status, error_code FROM plaid_items WHERE id=?", (item_db_id,))
    row = await cursor.fetchone()
    assert row["status"] == STATUS_ERROR
    assert row["error_code"] == "SYNC_ERROR"


@pytest.mark.asyncio
async def test_sync_all_reports_per_item_errors_without_aborting(db):
    await _seed_item(db)
    totals = await plaid_sync.sync_all(
        db, client=StubClient([], fail_with=RuntimeError("boom")), categorize=False
    )
    assert totals["items"] == 1
    assert len(totals["errors"]) == 1


# ------------------------------------------------------------ token protection

@pytest.mark.asyncio
async def test_access_token_is_stored_encrypted_not_plaintext(db):
    await _seed_item(db)
    cursor = await db.execute("SELECT access_token_encrypted FROM plaid_items")
    stored = (await cursor.fetchone())[0]
    assert stored.startswith("v1:")
    assert "access-sandbox-token" not in stored
    from app.services.crypto import decrypt_secret

    assert decrypt_secret(stored) == "access-sandbox-token"


@pytest.mark.asyncio
async def test_list_items_never_exposes_the_token(db):
    await _seed_item(db)
    items = await plaid_sync.list_items(db)
    blob = repr(items)
    assert "access_token" not in blob
    assert "v1:" not in blob
    assert items[0]["has_cursor"] is False
