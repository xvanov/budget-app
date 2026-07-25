"""Settling up with one vendor, when the ordinary refund detector cannot reach it.

The real case: a $20/month subscription billed from 2025-01 to 2026-04 was refunded
in a single batch on 2026-05-07 — fifteen $20 credits at once, the oldest charge 460
days earlier. ``find_pairs`` caps refunds at 180 days because a wrong pair there
hides two real transactions, so twelve months of already-refunded spending stayed on
the books as real, and the batch of credits showed up as $308 of "income" in May.

``reconcile_exact`` answers the narrower question — "settle up with THIS vendor" —
where the vendor identity plus an exact equal-and-opposite amount does the work the
date window was doing.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.refund_pairs import reconcile_exact

MARK = "Reconcilevendor"
_INSERTED = []


@pytest.fixture(autouse=True)
def cleanup():
    yield
    if not _INSERTED:
        return
    con = sqlite3.connect(os.environ["DB_PATH"])
    try:
        con.execute(
            f"DELETE FROM transactions WHERE id IN ({','.join('?' * len(_INSERTED))})",
            _INSERTED,
        )
        con.execute("DELETE FROM refund_reconciliations WHERE pattern LIKE ?", (f"%{MARK.lower()}%",))
        con.commit()
    finally:
        con.close()
        _INSERTED.clear()


def _row(txn_id, amount, date, *, entity="expense", merchant=MARK):
    return {"id": txn_id, "amount_cents": amount, "posted_date": date,
            "merchant": merchant, "description": merchant, "entity_type": entity,
            "category_path": "Other"}


# --------------------------------------------------------------------------- #
# reconcile_exact — pure rules
# --------------------------------------------------------------------------- #

def test_a_year_of_charges_refunded_in_one_batch_all_pair():
    """The actual defect: 16 monthly charges, refunded together 460 days later."""
    charges = [_row(i, -2000, f"2025-{m:02d}-08") for i, m in enumerate(range(1, 13), start=1)]
    charges += [_row(20 + i, -2000, f"2026-{m:02d}-08") for i, m in enumerate(range(1, 5))]
    credits = [_row(100 + i, 2000, "2026-05-07", entity="income") for i in range(16)]

    pairs = reconcile_exact(charges + credits)
    assert len(pairs) == 16, f"expected every charge settled, got {len(pairs)}"
    assert {p["charge_id"] for p in pairs} == {c["id"] for c in charges}
    assert all(not p["partial"] for p in pairs)
    # FIFO: the oldest charge is settled by the first credit.
    assert pairs[0]["charge_id"] == 1


def test_only_exact_amounts_pair():
    """No partial matching at all — nothing here is ever a guess."""
    rows = [_row(1, -2000, "2025-01-08"), _row(2, 1500, "2025-06-01", entity="income")]
    assert reconcile_exact(rows) == []


def test_a_credit_cannot_predate_its_charge():
    rows = [_row(1, -2000, "2025-06-08"), _row(2, 2000, "2025-01-01", entity="income")]
    assert reconcile_exact(rows) == []


def test_one_to_one_no_double_claiming():
    """Two charges, one credit -> exactly one pair, and the older charge wins."""
    rows = [_row(1, -2000, "2025-01-08"), _row(2, -2000, "2025-02-08"),
            _row(3, 2000, "2026-05-07", entity="income")]
    pairs = reconcile_exact(rows)
    assert len(pairs) == 1
    assert pairs[0]["charge_id"] == 1


def test_leftovers_are_left_alone_not_force_matched():
    """22 x $20 and 2 x $7 charged, 21 x $20 and 1 x $7 back -> $27 still spent.

    Those are the live numbers. The remainder is real money and must survive.
    """
    charges = [_row(i, -2000, "2025-01-08") for i in range(1, 23)]
    charges += [_row(50, -700, "2025-01-08"), _row(51, -700, "2026-07-15")]
    credits = [_row(100 + i, 2000, "2026-05-07", entity="income") for i in range(21)]
    credits += [_row(200, 700, "2026-05-07", entity="income")]

    pairs = reconcile_exact(charges + credits)
    assert len(pairs) == 22
    paired = {p["charge_id"] for p in pairs} | {p["credit_id"] for p in pairs}
    leftover = [r for r in charges + credits if r["id"] not in paired]
    assert sum(r["amount_cents"] for r in leftover) == -2700, leftover


def test_transfers_and_excluded_rows_are_never_reconciled():
    rows = [_row(1, -2000, "2025-01-08", entity="transfer"),
            _row(2, 2000, "2026-05-07", entity="income"),
            _row(3, -2000, "2025-01-08", entity="excluded")]
    assert reconcile_exact(rows) == []


# --------------------------------------------------------------------------- #
# The endpoint, and durability across a re-detect
# --------------------------------------------------------------------------- #

async def _seed(db):
    """Three monthly charges, all refunded in one batch far outside the window."""
    ids = {"charges": [], "credits": []}
    for month in (1, 2, 3):
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, category_path, entity_type, business_context)
               VALUES ('plaid','v.csv','card-0001',?,-2000,?,?,'Other','expense','personal')""",
            (f"2025-{month:02d}-08", MARK, MARK),
        )
        ids["charges"].append(cursor.lastrowid)
        _INSERTED.append(cursor.lastrowid)
    for _ in range(3):
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, category_path, entity_type, business_context)
               VALUES ('plaid','v.csv','card-0001','2026-05-07',2000,?,?,
                       'Other Income','income','personal')""",
            (MARK, MARK),
        )
        ids["credits"].append(cursor.lastrowid)
        _INSERTED.append(cursor.lastrowid)
    await db.commit()
    return ids


@pytest.mark.asyncio
async def test_endpoint_dry_run_writes_nothing(test_client):
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db)
    finally:
        await db.close()

    resp = test_client.post(
        f"/api/refunds/reconcile-merchant?pattern={MARK}&dry_run=true")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pairs"] == 3, body
    assert body["dry_run"] is True

    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM transactions WHERE id IN "
            f"({','.join('?' * len(ids['charges']))}) AND refund_match_id IS NOT NULL",
            ids["charges"],
        )
        assert (await cursor.fetchone())[0] == 0, "dry run must not write"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_endpoint_pairs_and_reports_the_remainder(test_client):
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db)
        # One extra charge with no matching credit: genuinely still spent.
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, category_path, entity_type, business_context)
               VALUES ('plaid','v.csv','card-0001','2026-07-15',-2000,?,?,
                       'Other','expense','personal')""",
            (MARK, MARK),
        )
        _INSERTED.append(cursor.lastrowid)
        await db.commit()
    finally:
        await db.close()

    resp = test_client.post(f"/api/refunds/reconcile-merchant?pattern={MARK}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["pairs"] == 3, body
    assert body["leftover_net_cents"] == -2000, body
    assert body["reconciled_value_cents"] == 6000, body

    db = await get_db()
    try:
        for charge_id in ids["charges"]:
            cursor = await db.execute(
                "SELECT refund_match_id, refund_role, refund_partial FROM transactions WHERE id=?",
                (charge_id,))
            row = await cursor.fetchone()
            assert row["refund_match_id"] in ids["credits"], charge_id
            assert row["refund_role"] == "charge"
            assert row["refund_partial"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_reconciliation_survives_a_later_refund_detect(test_client):
    """The durability trap.

    ``detect_and_store`` CLEARS every pair in its scope before re-marking. Without
    the recorded intent, running refund detection again would silently undo the
    reconciliation and put all that refunded spending back on the books.
    """
    from app.database import get_db
    from app.services import refund_pairs

    db = await get_db()
    try:
        ids = await _seed(db)
    finally:
        await db.close()

    resp = test_client.post(f"/api/refunds/reconcile-merchant?pattern={MARK}")
    assert resp.json()["pairs"] == 3

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM refund_reconciliations WHERE LOWER(pattern)=?",
            (MARK.lower(),))
        assert (await cursor.fetchone())[0] == 1, "the intent must be recorded"

        await refund_pairs.detect_and_store(db, scope="plaid")

        cursor = await db.execute(
            f"SELECT COUNT(*) FROM transactions WHERE id IN "
            f"({','.join('?' * len(ids['charges']))}) AND refund_match_id IS NOT NULL",
            ids["charges"],
        )
        still = (await cursor.fetchone())[0]
        assert still == 3, f"re-detect wiped the reconciliation: only {still}/3 survived"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_empty_pattern_is_rejected(test_client):
    resp = test_client.post("/api/refunds/reconcile-merchant?pattern=%20%20")
    assert resp.status_code == 400
