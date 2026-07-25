"""Item-level Amazon refund attribution: which product came back, and whose.

Amazon's refund exports (``Refund Details.csv``, ``Returns Status.csv``) carry an
order id, a date and an amount — no ASIN and no product name. So a refund cannot
say what it reverses unless the item is INFERRED from the amount inside its own
order. Without that inference a return is just a positive number: it cannot take
the right category, and it cannot be shown cancelling out the purchase it undid.

Three things are tested here, in the order they run:

  * ``attribute_refund`` — the pure inference, including the two cases where it
    deliberately refuses to answer (``ambiguous``, ``unattributed``). A wrong item
    means the wrong category AND the wrong person, so a guess that looks certain
    is worse than a gap.
  * the category precedence — the attributed item's split child beats its charge
    row, which beats the order's majority category.
  * the pairing — including a refund whose only counterpart is a NEUTRALISED split
    parent (``entity_type='transfer'``). ``services/refund_pairs`` skips those by
    design, which is why refunds on split Amazon orders were never paired at all.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services import amazon_service
from app.services.amazon_matcher import (
    NEAR_ITEM_TOLERANCE_CENTS,
    attribute_refund,
    group_refund_events,
)
from app.services.refund_pairs import find_pairs


# ------------------------------------------------------------------ pure rules

def item(total, *, item_id=1, name="Widget", cancelled=False):
    return {"id": item_id, "order_id": "111-1", "product_name": name,
            "total_cents": total, "is_cancelled": cancelled, "recipient": "Ben Whitfield"}


def refund(cents):
    return {"id": 9, "order_id": "111-1", "amount_cents": cents, "owner": "ben"}


def test_one_item_at_the_exact_amount_is_the_attributed_item():
    result = attribute_refund(refund(2499), [item(2499, item_id=1), item(6400, item_id=2)])
    assert result["strategy"] == "exact_item"
    assert result["item"]["id"] == 1


def test_tax_drift_within_tolerance_still_attributes():
    """Amazon refunds the tax it collected, not the tax the export quoted."""
    result = attribute_refund(refund(2499 - 150), [item(2499, item_id=1), item(9900, item_id=2)])
    assert result["strategy"] == "near_item"
    assert result["item"]["id"] == 1


def test_drift_beyond_tolerance_is_not_attributed():
    over = 2499 - (NEAR_ITEM_TOLERANCE_CENTS + 1)
    result = attribute_refund(refund(over), [item(2499, item_id=1), item(9900, item_id=2)])
    assert result == {"item": None, "strategy": "unattributed"}


def test_exact_match_wins_over_a_nearby_one():
    """A cent-perfect item is the answer even when a neighbour is within $2."""
    result = attribute_refund(refund(2499), [item(2499, item_id=1), item(2500, item_id=2)])
    assert result["strategy"] == "exact_item"
    assert result["item"]["id"] == 1


def test_refund_of_the_whole_order_names_no_single_item():
    result = attribute_refund(refund(3499), [item(2499, item_id=1), item(1000, item_id=2)])
    assert result == {"item": None, "strategy": "whole_order"}


def test_two_items_at_the_same_amount_is_ambiguous_and_does_not_guess():
    """The refusal that matters: guessing would name the wrong product AND person."""
    result = attribute_refund(
        refund(2499),
        [item(2499, item_id=1, name="Socks"), item(2499, item_id=2, name="Vitamins")],
    )
    assert result == {"item": None, "strategy": "ambiguous"}


def test_a_cancelled_twin_does_not_make_an_exact_match_ambiguous():
    """A cancelled item was never charged, so it can never have been refunded."""
    result = attribute_refund(
        refund(2499),
        [item(2499, item_id=1), item(2499, item_id=2, cancelled=True)],
    )
    assert result["strategy"] == "exact_item"
    assert result["item"]["id"] == 1


def test_an_order_of_only_cancelled_items_attributes_nothing():
    result = attribute_refund(refund(2499), [item(2499, item_id=1, cancelled=True)])
    assert result == {"item": None, "strategy": "unattributed"}


def test_partial_refund_matching_nothing_in_the_order_is_unattributed():
    """Refunded shipping and restocking fees have no single item behind them."""
    result = attribute_refund(refund(499), [item(2499, item_id=1), item(6400, item_id=2)])
    assert result == {"item": None, "strategy": "unattributed"}


def test_an_item_with_no_recorded_total_is_not_a_candidate():
    result = attribute_refund(refund(2499), [{"id": 1, "order_id": "111-1", "total_cents": None,
                                              "is_cancelled": False}])
    assert result == {"item": None, "strategy": "unattributed"}


# ------------------------------------------------------- one return, many rows

def record(refund_id, cents, *, source, date="2026-06-03", order="111-1", owner="ben"):
    return {"id": refund_id, "order_id": order, "owner": owner, "amount_cents": cents,
            "refund_date": date, "source_file": source}


def test_one_return_reported_in_three_files_is_one_event():
    """The live shape: Amazon describes the same $16.11 return three times.

    Different dates, different status words, no shared id — and in the live data
    all three resolve to the same card credit, which is the proof they are one
    return rather than three.
    """
    events = group_refund_events([
        record(121, 1611, source="Refund Details.csv", date="2026-06-03"),
        record(214, 1611, source="Returns Status.csv", date="2026-05-21"),
        record(218, 1611, source="Your Orders.Returns.1.csv", date="2026-06-02"),
    ])
    assert len(events) == 1
    assert {r["id"] for r in events[0]} == {121, 214, 218}


def test_the_same_amount_twice_in_one_file_stays_two_events():
    """Two rows in ONE file are two real returns, not a duplicate view of one."""
    events = group_refund_events([
        record(1, 1611, source="Refund Details.csv"),
        record(2, 1611, source="Refund Details.csv"),
    ])
    assert len(events) == 2


def test_events_are_split_by_owner_and_amount():
    events = group_refund_events([
        record(1, 1611, source="Refund Details.csv"),
        record(2, 1611, source="Refund Details.csv", owner="ana"),
        record(3, 900, source="Refund Details.csv"),
    ])
    assert len(events) == 3


def test_events_come_back_oldest_first():
    events = group_refund_events([
        record(1, 100, source="Refund Details.csv", date="2026-06-03"),
        record(2, 200, source="Refund Details.csv", date="2026-01-09"),
    ])
    assert [e[0]["id"] for e in events] == [2, 1]


# ------------------------------------------------------------------ DB seeding

class Seed:
    """One Amazon order's real shape: a card charge, items, split children, refunds."""

    def __init__(self, db, order):
        self.db = db
        self.order = order
        self.txn_ids = []
        self._n = 0

    async def txn(self, amount, *, category=None, entity=None, date="2026-03-11",
                  merchant="Amazon", parent=None, reason=None):
        entity = entity or ("income" if amount > 0 else "expense")
        cursor = await self.db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents, merchant,
                description, normalized_description, category_path, entity_type,
                business_context, linked_transaction_id, categorization_reason)
               VALUES ('plaid','amz.csv','card-0002',?,?,?,?,?,?,?,'personal',?,?)""",
            (date, amount, merchant, merchant, merchant.lower(), category, entity, parent, reason),
        )
        self.txn_ids.append(cursor.lastrowid)
        return cursor.lastrowid

    async def item(self, total, *, name="Widget", charge=None, child=None,
                   recipient="Ben Whitfield Jr", cancelled=0, quantity=1):
        self._n += 1
        cursor = await self.db.execute(
            """INSERT INTO amazon_items
               (owner, order_id, asin, product_name, quantity, total_cents, ship_date,
                order_date, is_cancelled, used_gift_card, recipient, fingerprint,
                matched_transaction_id, split_transaction_id)
               VALUES ('ben',?,?,?,?,?,'2026-03-10','2026-03-09',?,0,?,?,?,?)""",
            (self.order, f"ASIN{self._n}", name, quantity, total, cancelled, recipient,
             f"fp-{self.order}-{self._n}", charge, child),
        )
        return cursor.lastrowid

    async def refund(self, cents, *, credit=None, date="2026-03-25", reason="Customer return",
                     source="Refund Details.csv"):
        self._n += 1
        cursor = await self.db.execute(
            """INSERT INTO amazon_refunds
               (owner, order_id, amount_cents, refund_date, reason, fingerprint,
                matched_transaction_id, source_file)
               VALUES ('ben',?,?,?,?,?,?,?)""",
            (self.order, cents, date, reason, f"rfp-{self.order}-{self._n}", credit, source),
        )
        return cursor.lastrowid

    async def commit(self):
        await self.db.commit()

    async def row(self, txn_id):
        cursor = await self.db.execute(
            "SELECT category_path, entity_type, categorization_reason, refund_match_id, "
            "refund_role, refund_partial FROM transactions WHERE id=?",
            (txn_id,),
        )
        return dict(await cursor.fetchone())

    async def refund_row(self, refund_id):
        cursor = await self.db.execute(
            "SELECT matched_item_id, match_strategy FROM amazon_refunds WHERE id=?",
            (refund_id,),
        )
        return dict(await cursor.fetchone())

    async def cleanup(self):
        for txn_id in self.txn_ids:
            await self.db.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
        await self.db.execute("DELETE FROM amazon_items WHERE order_id=?", (self.order,))
        await self.db.execute("DELETE FROM amazon_refunds WHERE order_id=?", (self.order,))
        await self.db.commit()


async def _seeded(order):
    from app.database import get_db
    db = await get_db()
    return db, Seed(db, order)


# ------------------------------------------------------------ attribution → DB

@pytest.mark.asyncio
async def test_attribution_fills_the_new_columns(test_client):
    db, seed = await _seeded("111-ATTR-COLUMNS")
    try:
        charge = await seed.txn(-3499, category="Shopping")
        socks = await seed.item(2499, name="Wool Socks", charge=charge)
        await seed.item(1000, name="Cable", charge=charge)
        credit = await seed.txn(2499, date="2026-03-25")
        refund_id = await seed.refund(2499, credit=credit)
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        assert await seed.refund_row(refund_id) == {
            "matched_item_id": socks, "match_strategy": "exact_item"}
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_two_refunds_in_one_order_take_different_items(test_client):
    """An item claimed by an earlier refund leaves the candidate pool.

    Two $25 returns from an order holding a $25.00 and a $26.00 item: without
    withdrawing the claimed item the second refund matches the first item exactly
    again, and one product is reported as returned twice while the other is never
    reported at all.
    """
    db, seed = await _seeded("111-ATTR-TWO-ITEMS")
    try:
        charge = await seed.txn(-5100, category="Shopping")
        mug_a = await seed.item(2500, name="Mug A", charge=charge)
        mug_b = await seed.item(2600, name="Mug B", charge=charge)
        a = await seed.refund(2500, date="2026-03-20")
        b = await seed.refund(2500, date="2026-03-27")
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        assert await seed.refund_row(a) == {"matched_item_id": mug_a,
                                            "match_strategy": "exact_item"}
        # Mug A is gone, so the second refund resolves to the other item on the
        # tax-drift tolerance rather than claiming Mug A twice.
        assert await seed.refund_row(b) == {"matched_item_id": mug_b,
                                            "match_strategy": "near_item"}
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_duplicate_export_rows_of_one_return_agree(test_client):
    """Both rows name the same product, and the return is paired exactly once.

    Attributed per record instead of per event, the second row would find its item
    already claimed and report ``unattributed`` — the same return would be listed
    once as "Vitamins for Elena" and once as unexplained.
    """
    db, seed = await _seeded("111-ATTR-DUPES")
    try:
        charge = await seed.txn(-2500, category="Health")
        vitamins = await seed.item(2500, name="Vitamins", charge=charge)
        credit = await seed.txn(2500, date="2026-03-25")
        a = await seed.refund(2500, credit=credit, date="2026-03-24",
                              source="Refund Details.csv")
        b = await seed.refund(2500, credit=credit, date="2026-03-18",
                              source="Returns Status.csv")
        await seed.commit()

        result = await amazon_service.attribute_and_pair_refunds(db)
        for refund_id in (a, b):
            assert await seed.refund_row(refund_id) == {"matched_item_id": vitamins,
                                                        "match_strategy": "exact_item"}
        assert result["booking"]["duplicate_export_rows"] >= 1, result
        assert (await seed.row(credit))["refund_match_id"] == charge
    finally:
        await seed.cleanup()
        await db.close()


# --------------------------------------------------------- category precedence

@pytest.mark.asyncio
async def test_category_comes_from_the_attributed_items_split_child(test_client):
    """The most specific fact wins: the child row categorized from the product name.

    The order's majority category would answer ``Groceries`` here (alphabetical
    tie-break over two equally common categories). Only item-level attribution can
    say the $25 that came back was the Health item.
    """
    db, seed = await _seeded("111-CAT-CHILD")
    try:
        parent = await seed.txn(-5500, category="Transfer", entity="transfer")
        child_a = await seed.txn(-3000, category="Groceries", parent=parent)
        child_b = await seed.txn(-2500, category="Health", parent=parent)
        await seed.item(3000, name="Coffee", charge=parent, child=child_a)
        vitamins = await seed.item(2500, name="Vitamins", charge=parent, child=child_b)
        credit = await seed.txn(2500, date="2026-03-25")
        await seed.refund(2500, credit=credit)
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        assert await amazon_service.refund_category_for_item(db, vitamins) == "Health"
        assert await amazon_service.refund_category_for_order(db, seed.order) == "Groceries"

        await amazon_service.rebook_refunds(db)
        got = await seed.row(credit)
        assert got["category_path"] == "Health"
        assert got["entity_type"] == "income"  # sign-derived, never a 'refund' type
        assert "Vitamins" in got["categorization_reason"]
        assert "Ben Whitfield Jr" in got["categorization_reason"]
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_category_falls_back_to_the_items_charge_row(test_client):
    """An unsplit single-item charge is the same money, so its category counts."""
    db, seed = await _seeded("111-CAT-CHARGE")
    try:
        charge = await seed.txn(-2500, category="Home & Garden")
        item_id = await seed.item(2500, name="Trowel", charge=charge)
        credit = await seed.txn(2500, date="2026-03-25")
        await seed.refund(2500, credit=credit)
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        assert await amazon_service.refund_category_for_item(db, item_id) == "Home & Garden"
        await amazon_service.rebook_refunds(db)
        assert (await seed.row(credit))["category_path"] == "Home & Garden"
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_a_neutralised_charge_is_never_the_category_answer(test_client):
    """Post-split the parent says ``Transfer``, which describes no purchase."""
    db, seed = await _seeded("111-CAT-TRANSFER")
    try:
        parent = await seed.txn(-2500, category="Transfer", entity="transfer")
        item_id = await seed.item(2500, name="Trowel", charge=parent)
        await seed.commit()
        assert await amazon_service.refund_category_for_item(db, item_id) is None
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_an_unattributable_refund_falls_through_to_other_income(test_client):
    """Nothing known about the order means there is no bucket to reverse."""
    db, seed = await _seeded("111-CAT-NONE")
    try:
        charge = await seed.txn(-2500, category=None)
        await seed.item(2500, name="Mystery", charge=charge)
        credit = await seed.txn(477, date="2026-03-25")
        await seed.refund(477, credit=credit)
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        await amazon_service._book_refund(
            db, {"order_id": seed.order, "amount_cents": 477, "transaction_id": credit})
        await db.commit()
        got = await seed.row(credit)
        assert got["category_path"] == "Other Income"
        assert "original category unknown" in got["categorization_reason"]
    finally:
        await seed.cleanup()
        await db.close()


# -------------------------------------------------------------------- pairing

@pytest.mark.asyncio
async def test_refund_pairs_with_the_attributed_items_split_child(test_client):
    """The child row holds the item's money, so it is the true other half."""
    db, seed = await _seeded("111-PAIR-CHILD")
    try:
        parent = await seed.txn(-5500, category="Transfer", entity="transfer")
        child_a = await seed.txn(-3000, category="Groceries", parent=parent)
        child_b = await seed.txn(-2500, category="Health", parent=parent)
        await seed.item(3000, name="Coffee", charge=parent, child=child_a)
        await seed.item(2500, name="Vitamins", charge=parent, child=child_b)
        credit = await seed.txn(2500, date="2026-03-25")
        await seed.refund(2500, credit=credit)
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        summary = await amazon_service.pair_refunds(db)
        assert summary["paired"] >= 1, summary

        credit_row, child_row = await seed.row(credit), await seed.row(child_b)
        assert credit_row["refund_match_id"] == child_b
        assert credit_row["refund_role"] == "credit"
        assert child_row["refund_match_id"] == credit
        assert child_row["refund_role"] == "charge"
        # Exact amounts on both sides: a full reversal, not a partial one.
        assert credit_row["refund_partial"] == 0 and child_row["refund_partial"] == 0
        # The unrefunded sibling stays untouched.
        assert (await seed.row(child_a))["refund_match_id"] is None
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_refund_pairs_with_a_neutralised_split_parent(test_client):
    """The gap this closes: a split Amazon parent is ``entity_type='transfer'``.

    ``refund_pairs.find_pairs`` skips transfer rows — correctly, since it pairs on
    merchant and date and internal movement pairs spuriously. But an Amazon order
    id ties these two rows together as a fact from Amazon's own export, so the
    order-level pairing is entitled to ignore that exclusion. Asserted both ways:
    the generic matcher finds nothing, and this one still pairs.
    """
    db, seed = await _seeded("111-PAIR-PARENT")
    try:
        parent = await seed.txn(-5500, category="Transfer", entity="transfer")
        child_a = await seed.txn(-3000, category="Groceries", parent=parent)
        child_b = await seed.txn(-2500, category="Health", parent=parent)
        await seed.item(3000, name="Coffee", charge=parent, child=child_a)
        await seed.item(2500, name="Vitamins", charge=parent, child=child_b)
        # A partial refund matching no single item: only the order-level charge row
        # can be its counterpart, and that row is the neutralised parent.
        credit = await seed.txn(1500, date="2026-03-25")
        refund_id = await seed.refund(1500, credit=credit)
        await seed.commit()

        rows = [
            {"id": parent, "amount_cents": -5500, "posted_date": "2026-03-11",
             "merchant": "Amazon", "description": "Amazon", "category_path": "Transfer",
             "entity_type": "transfer"},
            {"id": credit, "amount_cents": 1500, "posted_date": "2026-03-25",
             "merchant": "Amazon", "description": "Amazon", "category_path": None,
             "entity_type": "income"},
        ]
        assert find_pairs(rows) == [], "the generic matcher must still skip transfer rows"

        await amazon_service.attribute_refunds(db)
        assert (await seed.refund_row(refund_id))["match_strategy"] == "unattributed"
        summary = await amazon_service.pair_refunds(db)
        assert summary["paired"] >= 1, summary

        credit_row, parent_row = await seed.row(credit), await seed.row(parent)
        assert credit_row["refund_match_id"] == parent
        assert parent_row["refund_match_id"] == credit
        assert parent_row["refund_role"] == "charge"
        # $15 back on a $55 charge is a partial reversal.
        assert credit_row["refund_partial"] == 1 and parent_row["refund_partial"] == 1
        # Pairing must not un-neutralise the parent — that would double-count it.
        assert parent_row["entity_type"] == "transfer"
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_two_refunds_from_one_order_pair_with_different_rows(test_client):
    """One-to-one. A charge is reversed once; a second claim would double-count it."""
    db, seed = await _seeded("111-PAIR-ONE-TO-ONE")
    try:
        parent = await seed.txn(-5100, category="Transfer", entity="transfer")
        child_a = await seed.txn(-2500, category="Shopping", parent=parent)
        child_b = await seed.txn(-2600, category="Shopping", parent=parent)
        await seed.item(2500, name="Mug A", charge=parent, child=child_a)
        await seed.item(2600, name="Mug B", charge=parent, child=child_b)
        first = await seed.txn(2500, date="2026-03-20")
        second = await seed.txn(2500, date="2026-03-27")
        await seed.refund(2500, credit=first, date="2026-03-20")
        await seed.refund(2500, credit=second, date="2026-03-27")
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        await amazon_service.pair_refunds(db)

        first_row, second_row = await seed.row(first), await seed.row(second)
        assert first_row["refund_match_id"] == child_a
        assert second_row["refund_match_id"] == child_b
        # $25 back against the $26 item is not a full reversal.
        assert first_row["refund_partial"] == 0
        assert second_row["refund_partial"] == 1
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_a_second_refund_cannot_claim_an_already_paired_row(test_client):
    """When the only counterpart is taken, the second refund stays unpaired.

    Leaving it unpaired is the point: pairing it anyway would show one $25 charge
    as reversed twice, and hide it from the list on both counts.
    """
    db, seed = await _seeded("111-PAIR-CONTESTED")
    try:
        charge = await seed.txn(-2500, category="Shopping")
        await seed.item(2500, name="Mug", charge=charge)
        first = await seed.txn(2500, date="2026-03-20")
        second = await seed.txn(2500, date="2026-03-27")
        await seed.refund(2500, credit=first, date="2026-03-20")
        await seed.refund(2500, credit=second, date="2026-03-27")
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        summary = await amazon_service.pair_refunds(db)
        assert summary["no_reversible_row"] >= 1, summary
        assert (await seed.row(first))["refund_match_id"] == charge
        assert (await seed.row(second))["refund_match_id"] is None
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_ambiguity_claims_no_item_at_all(test_client):
    """Two identical items and two identical refunds: both stay ambiguous.

    An ambiguous refund does not quietly consume one of the candidates to make the
    next one resolvable — that would manufacture certainty out of processing order.
    """
    db, seed = await _seeded("111-ATTR-BOTH-AMBIGUOUS")
    try:
        charge = await seed.txn(-2000, category="Shopping")
        await seed.item(1000, name="Mug A", charge=charge)
        await seed.item(1000, name="Mug B", charge=charge)
        a = await seed.refund(1000, date="2026-03-20")
        b = await seed.refund(1000, date="2026-03-27")
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        for refund_id in (a, b):
            assert await seed.refund_row(refund_id) == {"matched_item_id": None,
                                                        "match_strategy": "ambiguous"}
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_a_refund_bigger_than_every_candidate_row_is_left_unpaired(test_client):
    """A credit larger than the charge is not that charge's reversal."""
    db, seed = await _seeded("111-PAIR-TOO-BIG")
    try:
        charge = await seed.txn(-1000, category="Shopping")
        await seed.item(1000, name="Mug", charge=charge)
        credit = await seed.txn(9900, date="2026-03-25")
        await seed.refund(9900, credit=credit)
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        summary = await amazon_service.pair_refunds(db)
        assert summary["no_reversible_row"] >= 1, summary
        assert (await seed.row(credit))["refund_match_id"] is None
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_a_pair_the_generic_matcher_got_wrong_is_re_pointed(test_client):
    """Order-level identity outranks a merchant-and-date coincidence.

    ``refund_pairs`` can only see "an Amazon credit near an Amazon charge", so on a
    split order it pairs the credit with the wrong line. Amazon's own export says
    which item came back, so that wins — and the charge it releases must be left
    with no dangling half-pair.
    """
    db, seed = await _seeded("111-PAIR-REPOINT")
    try:
        parent = await seed.txn(-5500, category="Transfer", entity="transfer")
        wrong = await seed.txn(-3000, category="Groceries", parent=parent)
        right = await seed.txn(-2500, category="Health", parent=parent)
        await seed.item(3000, name="Coffee", charge=parent, child=wrong)
        await seed.item(2500, name="Vitamins", charge=parent, child=right)
        credit = await seed.txn(2500, date="2026-03-25")
        await seed.refund(2500, credit=credit)
        # The state the generic matcher leaves behind: paired to the $30 line.
        await db.execute("UPDATE transactions SET refund_match_id=?, refund_role='credit', "
                         "refund_partial=1 WHERE id=?", (wrong, credit))
        await db.execute("UPDATE transactions SET refund_match_id=?, refund_role='charge', "
                         "refund_partial=1 WHERE id=?", (credit, wrong))
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        summary = await amazon_service.pair_refunds(db)
        assert summary["repointed_from_another_charge"] >= 1, summary

        assert (await seed.row(credit))["refund_match_id"] == right
        assert (await seed.row(right))["refund_match_id"] == credit
        released = await seed.row(wrong)
        assert released["refund_match_id"] is None
        assert released["refund_role"] is None
        assert released["refund_partial"] == 0
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_a_one_way_pair_is_repaired_not_reported_as_correct(test_client):
    """Only one half marked is not a pair — the Refunded tab joins both rows."""
    db, seed = await _seeded("111-PAIR-ONE-WAY")
    try:
        charge = await seed.txn(-2500, category="Health")
        await seed.item(2500, name="Vitamins", charge=charge)
        credit = await seed.txn(2500, date="2026-03-25")
        await seed.refund(2500, credit=credit)
        await db.execute("UPDATE transactions SET refund_match_id=?, refund_role='credit' "
                         "WHERE id=?", (charge, credit))
        await seed.commit()

        await amazon_service.attribute_refunds(db)
        summary = await amazon_service.pair_refunds(db)
        assert summary["paired"] >= 1 and summary["already_paired"] == 0, summary
        assert (await seed.row(charge))["refund_match_id"] == credit
        assert (await seed.row(charge))["refund_role"] == "charge"
    finally:
        await seed.cleanup()
        await db.close()


# ------------------------------------------------------------ the one-step run

@pytest.mark.asyncio
async def test_attribute_and_pair_respects_a_hand_set_category(test_client):
    """A category the owner chose outranks anything inferred — until told otherwise."""
    db, seed = await _seeded("111-MANUAL")
    try:
        charge = await seed.txn(-2500, category="Health")
        await seed.item(2500, name="Vitamins", charge=charge)
        credit = await seed.txn(2500, date="2026-03-25", category="Gifts & Donations",
                                reason="User correction")
        await seed.refund(2500, credit=credit)
        await seed.commit()

        result = await amazon_service.attribute_and_pair_refunds(db)
        assert result["booking"]["skipped_manually_categorized"] >= 1, result
        assert (await seed.row(credit))["category_path"] == "Gifts & Donations"
        # Pairing is independent of the category, so it still happens.
        assert (await seed.row(credit))["refund_match_id"] == charge

        result = await amazon_service.attribute_and_pair_refunds(db, overwrite_manual=True)
        assert result["booking"]["rebooked"] >= 1, result
        assert (await seed.row(credit))["category_path"] == "Health"
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_running_it_twice_changes_nothing_the_second_time(test_client):
    """Re-runnable: the second pass reports the pair as already made, not remade."""
    db, seed = await _seeded("111-IDEMPOTENT")
    try:
        charge = await seed.txn(-2500, category="Health")
        await seed.item(2500, name="Vitamins", charge=charge)
        credit = await seed.txn(2500, date="2026-03-25")
        await seed.refund(2500, credit=credit)
        await seed.commit()

        first = await amazon_service.attribute_and_pair_refunds(db)
        before = await seed.row(credit)
        second = await amazon_service.attribute_and_pair_refunds(db)
        assert first["pairing"]["paired"] >= 1
        assert second["pairing"]["already_paired"] >= 1, second
        assert await seed.row(credit) == before
    finally:
        await seed.cleanup()
        await db.close()


# ------------------------------------------------------------------- endpoints

@pytest.mark.asyncio
async def test_status_reports_attribution_by_strategy(test_client):
    db, seed = await _seeded("111-API-STATUS")
    try:
        charge = await seed.txn(-2500, category="Health")
        await seed.item(2500, name="Vitamins", charge=charge)
        credit = await seed.txn(2500, date="2026-03-25")
        await seed.refund(2500, credit=credit)
        await seed.commit()
        await amazon_service.attribute_and_pair_refunds(db)

        body = test_client.get("/api/amazon/status").json()
        assert body["refund_attribution"].get("exact_item", 0) >= 1, body
        assert body["refunds_attributed_to_item"] >= 1
        assert body["refunds_paired"] >= 1
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_refunds_endpoint_shows_who_paid_for_what(test_client):
    db, seed = await _seeded("111-API-REFUNDS")
    try:
        charge = await seed.txn(-2500, category="Health")
        await seed.item(2500, name="Vitamins 500ct", charge=charge, recipient="Nadia Kessler")
        credit = await seed.txn(2500, date="2026-03-25")
        await seed.refund(2500, credit=credit)
        await seed.commit()
        await amazon_service.attribute_and_pair_refunds(db)

        resp = test_client.get("/api/amazon/refunds?limit=200")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        row = next(r for r in body["refunds"] if r["order_id"] == seed.order)
        for key in ("refund_date", "amount_cents", "order_id", "owner", "strategy",
                    "product_name", "recipient", "category_path", "transaction_id",
                    "paired_transaction_id"):
            assert key in row, row
        assert row["strategy"] == "exact_item"
        assert row["product_name"] == "Vitamins 500ct"
        assert row["recipient"] == "Nadia Kessler"
        assert row["category_path"] == "Health"
        assert row["transaction_id"] == credit
        assert row["paired_transaction_id"] == charge
    finally:
        await seed.cleanup()
        await db.close()


@pytest.mark.asyncio
async def test_attribute_refunds_endpoint_reports_all_three_steps(test_client):
    resp = test_client.post("/api/amazon/attribute-refunds")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"attribution", "booking", "pairing"}, body
    assert "by_strategy" in body["attribution"]
    for key in ("paired", "already_paired", "no_reversible_row"):
        assert key in body["pairing"], body


# --------------------------------------------------------------------------- #
# Pairing fallback: a refund whose order has no matched charge
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_refund_with_no_order_charge_pairs_by_exact_amount(test_client):
    """42 matched refunds belonged to orders whose charge was never matched.

    Their charge predates the imported statements, or a gift-card balance made the
    card charge a different amount, so the order route had nothing to offer and
    $1,265.74 of returned money sat in the ledger as income for purchases it could
    not point at. An exact amount on the same merchant, earlier in time, is enough
    to cancel the two out.
    """
    from app.database import get_db
    from app.services import amazon_service
    db = await get_db()
    ids = []
    order = "111-FALLBACK-PAIR-1"
    try:
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, transaction_date,
                amount_cents, merchant, description, category_path, entity_type,
                business_context)
               VALUES ('plaid','f.csv','card-0001','2033-02-10','2033-02-09',-6433,
                       'Amazon','Amazon','Shopping','expense','personal')""")
        charge_id = cursor.lastrowid; ids.append(charge_id)
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, transaction_date,
                amount_cents, merchant, description, category_path, entity_type,
                business_context)
               VALUES ('plaid','f.csv','card-0001','2033-03-15','2033-03-14',6433,
                       'Amazon','Amazon','Other Income','income','personal')""")
        credit_id = cursor.lastrowid; ids.append(credit_id)
        # A refund record whose order has NO matched charge row at all.
        await db.execute(
            """INSERT INTO amazon_refunds
               (owner, order_id, amount_cents, refund_date, reason, fingerprint,
                matched_transaction_id)
               VALUES ('kalin', ?, 6433, '2033-03-14', 'Customer return', ?, ?)""",
            (order, f"fp-fallback-{order}", credit_id))
        await db.commit()

        await amazon_service.pair_refunds(db)

        cursor = await db.execute(
            "SELECT refund_match_id, refund_role FROM transactions WHERE id=?", (credit_id,))
        credit = dict(await cursor.fetchone())
        assert credit["refund_match_id"] == charge_id, (
            f"refund should have paired with the same-amount charge, got {credit}")
        assert credit["refund_role"] == "credit"
    finally:
        for txn_id in ids:
            await db.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
        await db.execute("DELETE FROM amazon_refunds WHERE order_id=?", (order,))
        await db.commit()
        await db.close()


@pytest.mark.asyncio
async def test_the_fallback_never_pairs_a_charge_that_came_later(test_client):
    """A refund cannot reverse a purchase that had not happened yet."""
    from app.database import get_db
    from app.services import amazon_service
    db = await get_db()
    ids = []
    order = "111-FALLBACK-PAIR-2"
    try:
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, transaction_date,
                amount_cents, merchant, description, category_path, entity_type,
                business_context)
               VALUES ('plaid','f.csv','card-0001','2033-06-10','2033-06-09',-7777,
                       'Amazon','Amazon','Shopping','expense','personal')""")
        ids.append(cursor.lastrowid)
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, transaction_date,
                amount_cents, merchant, description, category_path, entity_type,
                business_context)
               VALUES ('plaid','f.csv','card-0001','2033-04-15','2033-04-14',7777,
                       'Amazon','Amazon','Other Income','income','personal')""")
        credit_id = cursor.lastrowid; ids.append(credit_id)
        await db.execute(
            """INSERT INTO amazon_refunds
               (owner, order_id, amount_cents, refund_date, reason, fingerprint,
                matched_transaction_id)
               VALUES ('kalin', ?, 7777, '2033-04-14', 'Customer return', ?, ?)""",
            (order, f"fp-fallback-{order}", credit_id))
        await db.commit()

        await amazon_service.pair_refunds(db)

        cursor = await db.execute(
            "SELECT refund_match_id FROM transactions WHERE id=?", (credit_id,))
        assert dict(await cursor.fetchone())["refund_match_id"] is None
    finally:
        for txn_id in ids:
            await db.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
        await db.execute("DELETE FROM amazon_refunds WHERE order_id=?", (order,))
        await db.commit()
        await db.close()
