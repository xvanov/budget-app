"""Refund pairing — a charge matched to the credit that reversed it.

The rules are deliberately conservative because a wrong pair HIDES two real
transactions from the transactions list. The partial-refund thresholds exist
because of a concrete false positive on live data: a $1.00 credit paired to a
$20.00 charge 29 days earlier (5%), which was actually an unrelated small refund
from the same merchant.
"""
import aiosqlite
import pytest
import pytest_asyncio

from app.services.refund_pairs import (
    PARTIAL_MIN_RATIO,
    PARTIAL_WINDOW_DAYS,
    REFUND_WINDOW_DAYS,
    detect_and_store,
    find_pairs,
    normalize_merchant,
)


# Most of this file tests `find_pairs` as a pure function. The Amazon-attribution
# tests at the bottom need a real database, because the attribution lives in the
# `amazon_refunds` / `amazon_items` tables rather than in the transaction rows.
@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "refunds.db"))
    import app.database as database

    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "refunds.db"))
    await database.init_db()
    conn = await aiosqlite.connect(str(tmp_path / "refunds.db"))
    conn.row_factory = aiosqlite.Row
    yield conn
    await conn.close()


async def _insert(db, posted_date, amount_cents, merchant, category="Shopping"):
    """One plaid-scope transaction. Returns its id."""
    cursor = await db.execute(
        """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, normalized_description, category_path, entity_type)
           VALUES ('plaid', 'plaid:test', 'chase-1236', ?, ?, ?, ?, ?, ?, ?)""",
        (posted_date, amount_cents, merchant, merchant, merchant.lower(), category,
         "income" if amount_cents > 0 else "expense"),
    )
    await db.commit()
    return cursor.lastrowid


def txn(tid, amount, date, merchant="Lowe's", entity=None, category="Shopping"):
    return {
        "id": tid, "amount_cents": amount, "posted_date": date, "merchant": merchant,
        "description": merchant, "category_path": category,
        "entity_type": entity or ("income" if amount > 0 else "expense"),
    }


def test_exact_amount_credit_pairs_as_a_full_refund():
    pairs = find_pairs([txn(1, -14764, "2026-03-01"), txn(2, 14764, "2026-03-10")])
    assert len(pairs) == 1
    assert pairs[0]["charge_id"] == 1 and pairs[0]["credit_id"] == 2
    assert pairs[0]["partial"] is False
    assert pairs[0]["days_to_refund"] == 9


def test_smaller_credit_pairs_as_partial():
    pairs = find_pairs([txn(1, -10000, "2026-03-01"), txn(2, 6000, "2026-03-05")])
    assert len(pairs) == 1
    assert pairs[0]["partial"] is True


def test_tiny_credit_is_not_treated_as_a_partial_refund():
    """The live false positive: $1.00 back on a $20.00 charge is not its refund."""
    pairs = find_pairs([txn(1, -2000, "2026-03-01"), txn(2, 100, "2026-03-30")])
    assert pairs == []


def test_partial_outside_the_short_window_is_rejected():
    beyond = f"2026-06-30"  # ~120 days after, well past PARTIAL_WINDOW_DAYS
    pairs = find_pairs([txn(1, -10000, "2026-03-01"), txn(2, 6000, beyond)])
    assert pairs == []


def test_exact_match_still_allowed_in_the_long_window():
    """An exact amount from the same merchant is near-proof, so it gets 180 days."""
    pairs = find_pairs([txn(1, -10000, "2026-01-01"), txn(2, 10000, "2026-05-01")])
    assert len(pairs) == 1
    assert pairs[0]["partial"] is False


def test_credit_before_the_charge_is_never_a_refund_of_it():
    pairs = find_pairs([txn(1, -5000, "2026-03-10"), txn(2, 5000, "2026-03-01")])
    assert pairs == []


def test_credit_larger_than_the_charge_is_rejected():
    pairs = find_pairs([txn(1, -5000, "2026-03-01"), txn(2, 9000, "2026-03-05")])
    assert pairs == []


def test_one_to_one_two_identical_refunds_claim_two_charges():
    rows = [txn(1, -5000, "2026-03-01"), txn(2, -5000, "2026-03-02"),
            txn(3, 5000, "2026-03-10"), txn(4, 5000, "2026-03-11")]
    pairs = find_pairs(rows)
    assert len(pairs) == 2
    assert {p["charge_id"] for p in pairs} == {1, 2}
    assert {p["credit_id"] for p in pairs} == {3, 4}


def test_a_single_charge_cannot_be_refunded_twice():
    rows = [txn(1, -5000, "2026-03-01"), txn(2, 5000, "2026-03-10"), txn(3, 5000, "2026-03-11")]
    pairs = find_pairs(rows)
    assert len(pairs) == 1


def test_closest_date_wins_among_equal_amounts():
    rows = [txn(1, -5000, "2026-01-01"), txn(2, -5000, "2026-03-08"), txn(3, 5000, "2026-03-10")]
    pairs = find_pairs(rows)
    assert pairs[0]["charge_id"] == 2


def test_different_merchants_never_pair():
    pairs = find_pairs([txn(1, -5000, "2026-03-01", merchant="Lowe's"),
                        txn(2, 5000, "2026-03-05", merchant="Ulta Beauty")])
    assert pairs == []


def test_merchant_spelling_variants_still_pair():
    pairs = find_pairs([txn(1, -2000, "2026-03-01", merchant="SUBSCRIBECO.COM"),
                        txn(2, 2000, "2026-03-05", merchant="Subscribecos")])
    assert len(pairs) == 1


@pytest.mark.parametrize("entity", ["transfer", "excluded", "asset_conversion"])
def test_transfers_and_excluded_rows_never_pair(entity):
    """Internal movement and opted-out rows are not purchases to refund."""
    pairs = find_pairs([txn(1, -5000, "2026-03-01", entity=entity),
                        txn(2, 5000, "2026-03-05", entity=entity)])
    assert pairs == []


def test_normalize_merchant_collapses_variants():
    assert normalize_merchant("SUBSCRIBECO.COM") == normalize_merchant("Subscribecos")
    assert normalize_merchant("Amazon.com") == normalize_merchant("AMAZON")
    assert normalize_merchant("Lowe's") != normalize_merchant("Ulta")


def test_thresholds_are_documented_values():
    assert REFUND_WINDOW_DAYS == 180
    assert PARTIAL_WINDOW_DAYS == 60
    assert PARTIAL_MIN_RATIO == 0.25


# ── Amazon's own attribution beats the heuristic ─────────────────────────────

@pytest.mark.asyncio
async def test_amazon_attributed_refund_is_paired(db):
    """A refund Amazon has tied to an item must pair with that item's purchase.

    `find_pairs` cannot see this. The purchase row was renamed
    "Amazon: <product>" by the item matcher while the credit is a bare "Amazon",
    so `normalize_merchant` puts them in different buckets and they never meet.
    On the owner's data that left 38 transactions / $3,146.52 of already-refunded
    Amazon spending on the books, e.g. a $163.38 item bought 2026-04-30 and
    returned 2026-05-15: April overstated by $163.38 and May showed the same
    amount as income.
    """
    charge_id = await _insert(db, "2026-04-30", -16338, "Amazon: Goldshire Hydrangea Tree")
    credit_id = await _insert(db, "2026-05-15", 16338, "Amazon")

    await db.execute(
        """INSERT INTO amazon_items (id, order_id, owner, asin, product_name, total_cents,
               order_date, matched_transaction_id, split_transaction_id)
           VALUES (9001, 'order-1', 'sophia', 'B0G6DGH8NV', 'Goldshire Hydrangea Tree',
                   16338, '2026-04-28', ?, ?)""",
        (charge_id, charge_id),
    )
    await db.execute(
        """INSERT INTO amazon_refunds (order_id, owner, refund_date, amount_cents,
               matched_transaction_id, matched_item_id, fingerprint)
           VALUES ('order-1', 'sophia', '2026-05-15', 16338, ?, 9001, 'fp-1')""",
        (credit_id,),
    )
    await db.commit()

    result = await detect_and_store(db)
    assert result["amazon_attributed_pairs"] == 1

    cursor = await db.execute(
        "SELECT id, refund_role, refund_match_id, refund_partial FROM transactions "
        "WHERE id IN (?, ?) ORDER BY amount_cents", (charge_id, credit_id))
    rows = {r["id"]: dict(r) for r in await cursor.fetchall()}
    assert rows[charge_id]["refund_role"] == "charge"
    assert rows[charge_id]["refund_match_id"] == credit_id
    assert rows[credit_id]["refund_role"] == "credit"
    assert rows[credit_id]["refund_match_id"] == charge_id
    assert rows[charge_id]["refund_partial"] == 0, "equal amounts are a FULL refund"


@pytest.mark.asyncio
async def test_amazon_attribution_never_overwrites_an_existing_pair(db):
    """It fills gaps only. A pair the heuristic already made must survive.

    Otherwise re-running detection could move a credit from the charge it really
    reversed onto a different one, which is how a correct pair becomes a wrong one.
    """
    charge_id = await _insert(db, "2026-04-30", -5000, "Amazon: Widget")
    credit_id = await _insert(db, "2026-05-02", 5000, "Amazon: Widget")
    other_charge = await _insert(db, "2026-03-01", -5000, "Amazon: Other Thing")

    await db.execute(
        """INSERT INTO amazon_items (id, order_id, owner, product_name, total_cents,
               order_date, matched_transaction_id, split_transaction_id)
           VALUES (9002, 'order-2', 'kalin', 'Other Thing', 5000, '2026-02-27', ?, ?)""",
        (other_charge, other_charge),
    )
    await db.execute(
        """INSERT INTO amazon_refunds (order_id, owner, refund_date, amount_cents,
               matched_transaction_id, matched_item_id, fingerprint)
           VALUES ('order-2', 'kalin', '2026-05-02', 5000, ?, 9002, 'fp-2')""",
        (credit_id,),
    )
    await db.commit()

    await detect_and_store(db)

    cursor = await db.execute(
        "SELECT refund_match_id FROM transactions WHERE id=?", (credit_id,))
    matched = (await cursor.fetchone())["refund_match_id"]
    assert matched == charge_id, (
        "the same-merchant, 2-day-apart pair is the better answer; Amazon "
        f"attribution must not steal the credit onto {other_charge}")
