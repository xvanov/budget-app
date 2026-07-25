"""Amazon refunds must reverse the category they came from, not become income.

Regression tests for a real data defect. ``_book_refund`` selected the original
category with ``category_path LIKE 'Expense >%'`` — a HIERARCHICAL prefix that
stopped existing when the app moved to flat category names. Both of its lookups
therefore always missed and every matched Amazon refund fell through to the
``Other Income`` fallback. In the user's live DB that was 130 rows: returns showed
up as income instead of reducing what they had bought.

The second half of the defect is that ``match_all`` books a refund only at the
moment it first matches, so fixing the rule does not repair rows already linked.
``rebook_refunds`` exists for that and is tested here too.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services import amazon_service


ORDER = "111-TEST-REFUND-1"


async def _seed(db, *, item_category=None, child_category=None, refund_cents=2500):
    """One order, one charge, optionally a split child, and a matched refund credit."""
    cursor = await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, category_path, entity_type, business_context)
           VALUES ('plaid','amz.csv','card-0002','2026-03-11',-2500,
                   'Amazon','Amazon', ?, 'expense','personal')""",
        (item_category,),
    )
    charge_id = cursor.lastrowid

    child_id = None
    if child_category is not None:
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, category_path, entity_type, business_context,
                linked_transaction_id)
               VALUES ('plaid','amz.csv','card-0002','2026-03-11',-2500,
                       'Amazon: Widget','Amazon: Widget', ?, 'expense','personal', ?)""",
            (child_category, charge_id),
        )
        child_id = cursor.lastrowid

    cursor = await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, category_path, entity_type, business_context)
           VALUES ('plaid','amz.csv','card-0002','2026-03-25', ?,
                   'Amazon','Amazon','Ben','income','personal')""",
        (refund_cents,),
    )
    credit_id = cursor.lastrowid

    await db.execute(
        """INSERT INTO amazon_items
           (owner, order_id, asin, product_name, total_cents, ship_date, order_date,
            is_cancelled, used_gift_card, fingerprint, matched_transaction_id,
            split_transaction_id)
           VALUES ('ben', ?, 'A1', 'Widget', 2500, '2026-03-10', '2026-03-09',
                   0, 0, ?, ?, ?)""",
        (ORDER, f"fp-{ORDER}-{child_category}-{item_category}", charge_id, child_id),
    )
    await db.execute(
        """INSERT INTO amazon_refunds
           (owner, order_id, amount_cents, refund_date, reason, fingerprint,
            matched_transaction_id)
           VALUES ('ben', ?, ?, '2026-03-24', 'Customer return', ?, ?)""",
        (ORDER, refund_cents, f"rfp-{ORDER}-{refund_cents}", credit_id),
    )
    await db.commit()
    return {"charge": charge_id, "child": child_id, "credit": credit_id}


async def _cleanup(db, ids):
    for txn_id in [v for v in ids.values() if v]:
        await db.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
    for order in (ORDER, VOCAB_ORDER):
        await db.execute("DELETE FROM amazon_items WHERE order_id=?", (order,))
        await db.execute("DELETE FROM amazon_refunds WHERE order_id=?", (order,))
    await db.commit()


async def _category_of(db, txn_id):
    cursor = await db.execute(
        "SELECT category_path, entity_type, categorization_reason FROM transactions WHERE id=?",
        (txn_id,),
    )
    row = await cursor.fetchone()
    return dict(row)


@pytest.mark.asyncio
async def test_refund_category_comes_from_the_matched_charge(test_client):
    """A flat category on the charge is what the refund should reverse."""
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db, item_category="Health")
        assert await amazon_service.refund_category_for_order(db, ORDER) == "Health"
    finally:
        await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_refund_category_falls_back_to_the_split_child(test_client):
    """Once a charge is split the parent is neutralised, so the child holds the truth."""
    from app.database import get_db
    db = await get_db()
    try:
        # Parent on 'Transfer' is exactly the post-split shape and must be ignored.
        ids = await _seed(db, item_category="Transfer", child_category="Groceries")
        assert await amazon_service.refund_category_for_order(db, ORDER) == "Groceries"
    finally:
        await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_non_spending_categories_are_never_the_answer(test_client):
    """Transfer/Excluded/Uncategorized say nothing about what was bought."""
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db, item_category="Excluded", child_category="Uncategorized")
        assert await amazon_service.refund_category_for_order(db, ORDER) is None
    finally:
        await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_book_refund_reverses_the_category_and_keeps_the_sign(test_client):
    """The refund keeps its positive amount but sits on the spending category.

    That is what makes the flat model net correctly: the dashboard sums each
    category, so +$25 on Health reduces Health by $25 rather than adding income.
    """
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db, item_category="Health")
        await amazon_service._book_refund(db, {
            "order_id": ORDER, "amount_cents": 2500, "transaction_id": ids["credit"],
        })
        await db.commit()
        got = await _category_of(db, ids["credit"])
        assert got["category_path"] == "Health"
        # Sign-derived, never the dead 'refund' type the flat migration removed.
        assert got["entity_type"] == "income"
        assert "reverses Health" in (got["categorization_reason"] or "")
    finally:
        await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_book_refund_never_emits_a_hierarchical_category(test_client):
    """The bug was a hierarchical assumption; assert the output is flat."""
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db, item_category="Health")
        await amazon_service._book_refund(db, {
            "order_id": ORDER, "amount_cents": 2500, "transaction_id": ids["credit"],
        })
        await db.commit()
        got = await _category_of(db, ids["credit"])
        assert " > " not in (got["category_path"] or "")
    finally:
        await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_unattributable_refund_stays_honest_income(test_client):
    """No categorized source row means there is no bucket to reverse."""
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db, item_category=None)
        await amazon_service._book_refund(db, {
            "order_id": ORDER, "amount_cents": 2500, "transaction_id": ids["credit"],
        })
        await db.commit()
        got = await _category_of(db, ids["credit"])
        assert got["category_path"] == "Other Income"
        assert "original category unknown" in (got["categorization_reason"] or "")
    finally:
        await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_rebook_repairs_a_refund_matched_under_the_old_rule(test_client):
    """The repair path: already-linked refunds are re-derived, not left broken.

    Re-running the matcher cannot fix these — they are already linked, so
    ``match_all`` never books them again.
    """
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db, item_category="Health")
        # Simulate the damaged state: booked as income by the old fallback.
        await db.execute(
            "UPDATE transactions SET category_path='Other Income', entity_type='income', "
            "categorization_reason=? WHERE id=?",
            (f"Amazon refund for order {ORDER} — original category unknown", ids["credit"]),
        )
        await db.commit()

        summary = await amazon_service.rebook_refunds(db)
        assert summary["rebooked"] >= 1, summary
        got = await _category_of(db, ids["credit"])
        assert got["category_path"] == "Health"
    finally:
        await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_rebook_leaves_a_hand_categorized_row_alone(test_client):
    """A category the owner chose deliberately outranks anything inferred."""
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db, item_category="Health")
        await db.execute(
            "UPDATE transactions SET category_path='Gifts & Donations', "
            "categorization_reason='User correction' WHERE id=?",
            (ids["credit"],),
        )
        await db.commit()

        summary = await amazon_service.rebook_refunds(db)
        assert summary["skipped_manually_categorized"] >= 1, summary
        got = await _category_of(db, ids["credit"])
        assert got["category_path"] == "Gifts & Donations"

        # …unless explicitly told to.
        summary = await amazon_service.rebook_refunds(db, overwrite_manual=True)
        assert summary["rebooked"] >= 1, summary
        got = await _category_of(db, ids["credit"])
        assert got["category_path"] == "Health"
    finally:
        await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_rebook_endpoint_reports_a_summary(test_client):
    resp = test_client.post("/api/amazon/rebook-refunds")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key in ("matched_refunds", "rebooked", "already_correct",
                "skipped_manually_categorized", "no_source_category"):
        assert key in body, body


@pytest.mark.asyncio
async def test_rebook_overrides_an_apply_similar_propagation(test_client):
    """A merchant-key sweep is not a decision about this row; attribution beats it.

    ``apply-similar`` propagates a category from one row to every row with the same
    merchant key. For Amazon that means "this says Amazon, so take what the other
    Amazon rows got" — it knows nothing about the product. Item-level attribution
    knows the actual item and who it was for, so it must win. On the live DB 27
    refunds had been pinned to `Ben` by one such sweep.
    """
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db, item_category="Health")
        await db.execute(
            "UPDATE transactions SET category_path='Ben', "
            "categorization_reason='Applied from user correction on #6980 (similar merchant)' "
            "WHERE id=?",
            (ids["credit"],),
        )
        await db.commit()

        summary = await amazon_service.rebook_refunds(db)
        assert summary["rebooked"] >= 1, summary
        got = await _category_of(db, ids["credit"])
        assert got["category_path"] == "Health"
    finally:
        await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_rebook_still_respects_a_single_row_user_correction(test_client):
    """The distinction that makes the override safe: a real per-row choice stands."""
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db, item_category="Health")
        await db.execute(
            "UPDATE transactions SET category_path='Gifts & Donations', "
            "categorization_reason='User correction' WHERE id=?",
            (ids["credit"],),
        )
        await db.commit()

        summary = await amazon_service.rebook_refunds(db)
        assert summary["skipped_manually_categorized"] >= 1, summary
        got = await _category_of(db, ids["credit"])
        assert got["category_path"] == "Gifts & Donations"
    finally:
        await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_a_hand_typed_lookalike_reason_is_not_treated_as_machine_output(test_client):
    """The match is anchored, so a note that merely starts similarly is respected."""
    from app.database import get_db
    db = await get_db()
    try:
        ids = await _seed(db, item_category="Health")
        await db.execute(
            "UPDATE transactions SET category_path='Gifts & Donations', "
            "categorization_reason=? WHERE id=?",
            ("Applied from user correction on #6980 (similar merchant) — but I checked, "
             "this one really is a gift", ids["credit"]),
        )
        await db.commit()

        summary = await amazon_service.rebook_refunds(db)
        assert summary["skipped_manually_categorized"] >= 1, summary
        got = await _category_of(db, ids["credit"])
        assert got["category_path"] == "Gifts & Donations"
    finally:
        await _cleanup(db, ids)
        await db.close()


# --------------------------------------------------------------------------- #
# Who the order was for: the export's recipient outranks an inherited person
# --------------------------------------------------------------------------- #

VOCAB_ORDER = "111-TEST-PERSON-VOCAB"


async def _ensure_person_categories(db):
    """The person buckets, plus the recipient vocabulary that identifies them.

    Two separate preconditions, both real:

    * The categories must EXIST to be filed into. ``person_categories`` correctly
      finds nothing until the owner has created them — the default seed ships no
      person categories.
    * A category is recognised as naming a person because its name appears in the
      export's ``recipient`` field. So the vocabulary needs both household names
      present, exactly as the real export has them (1,265 rows for one person, 157
      for the other). With only one name seeded, the other category reads as an
      ordinary spending category and is left alone — which is correct behaviour,
      just not the scenario under test.
    """
    for name in ("Ben", "Ana"):
        cursor = await db.execute("SELECT id FROM categories WHERE path=?", (name,))
        if not await cursor.fetchone():
            await db.execute(
                "INSERT INTO categories (name, parent_id, path) VALUES (?, NULL, ?)",
                (name, name),
            )
    for asin, recipient in (("V1", "Ben Whitfield Jr"), ("V2", "Ana Delgado")):
        await db.execute(
            """INSERT OR IGNORE INTO amazon_items
               (owner, order_id, asin, product_name, total_cents, ship_date, order_date,
                is_cancelled, used_gift_card, recipient, fingerprint)
               VALUES ('ben', ?, ?, 'Vocab', 1, '2020-01-01','2020-01-01',0,0,?,?)""",
            (VOCAB_ORDER, asin, recipient, f"fp-vocab-{asin}"),
        )
    await db.commit()


async def _seed_person(db, *, recipient, purchase_category, refund_cents=2471):
    """An order for a named recipient, its charge, and a matched refund credit."""
    await _ensure_person_categories(db)
    cursor = await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, category_path, entity_type, business_context)
           VALUES ('plaid','amz.csv','card-0002','2026-03-11',?,
                   'Amazon','Amazon',?,'expense','personal')""",
        (-refund_cents, purchase_category),
    )
    charge_id = cursor.lastrowid
    cursor = await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, category_path, entity_type, business_context)
           VALUES ('plaid','amz.csv','card-0002','2026-03-25',?,
                   'Amazon','Amazon',?,'income','personal')""",
        (refund_cents, purchase_category),
    )
    credit_id = cursor.lastrowid
    cursor = await db.execute(
        """INSERT INTO amazon_items
           (owner, order_id, asin, product_name, total_cents, ship_date, order_date,
            is_cancelled, used_gift_card, recipient, fingerprint, matched_transaction_id)
           VALUES ('ana', ?, 'A9', 'Thing', ?, '2026-03-10','2026-03-09',0,0, ?, ?, ?)""",
        (ORDER, refund_cents, recipient, f"fp-person-{recipient}-{refund_cents}", charge_id),
    )
    item_id = cursor.lastrowid
    cursor = await db.execute(
        """INSERT INTO amazon_refunds
           (owner, order_id, amount_cents, refund_date, reason, fingerprint,
            matched_transaction_id, matched_item_id, match_strategy)
           VALUES ('ana', ?, ?, '2026-03-24','Customer return', ?, ?, ?, 'exact_item')""",
        (ORDER, refund_cents, f"rfp-person-{recipient}-{refund_cents}", credit_id, item_id),
    )
    await db.commit()
    return {"charge": charge_id, "credit": credit_id, "item": item_id}


@pytest.mark.asyncio
async def test_recipient_corrects_a_refund_filed_under_the_wrong_person(test_client):
    """The live defect: 4 refunds sat on 'Ben' while the export said Ana's order.

    Steps 1-3 inherit the purchase row's category, and that category may itself
    have come from a merchant-key sweep that knew nothing about who ordered what.
    The recipient is a recorded fact, so it wins.
    """
    from app.database import get_db
    db = await get_db()
    ids = None
    try:
        ids = await _seed_person(db, recipient="Ana Delgado", purchase_category="Ben")
        refund = dict((await (await db.execute(
            "SELECT * FROM amazon_refunds WHERE order_id=?", (ORDER,))).fetchone()))
        assert await amazon_service.refund_category_for_refund(db, refund) == "Ana"
    finally:
        if ids:
            await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_a_real_spending_category_is_not_overridden_by_the_recipient(test_client):
    """"Housing" says what was bought, which beats who it was for."""
    from app.database import get_db
    db = await get_db()
    ids = None
    try:
        ids = await _seed_person(db, recipient="Ana Delgado", purchase_category="Housing")
        refund = dict((await (await db.execute(
            "SELECT * FROM amazon_refunds WHERE order_id=?", (ORDER,))).fetchone()))
        assert await amazon_service.refund_category_for_refund(db, refund) == "Housing"
    finally:
        if ids:
            await _cleanup(db, ids)
        await db.close()


@pytest.mark.asyncio
async def test_a_name_variant_still_resolves_to_the_person(test_client):
    """The export writes the same person several ways."""
    from app.database import get_db
    db = await get_db()
    for recipient, expected in (("Ana Delgado HOUSE", "Ana"),
                                ("Ana Delgado-Ruiz", "Ana"),
                                ("Ben Whitfield Jr", "Ben"),
                                ("Ben Whitfield", "Ben")):
        db2 = await get_db()
        ids = None
        try:
            ids = await _seed_person(db2, recipient=recipient, purchase_category="Ben"
                                     if expected == "Ana" else "Ana")
            refund = dict((await (await db2.execute(
                "SELECT * FROM amazon_refunds WHERE order_id=?", (ORDER,))).fetchone()))
            got = await amazon_service.refund_category_for_refund(db2, refund)
            assert got == expected, f"{recipient!r} -> {got!r}, expected {expected!r}"
        finally:
            if ids:
                await _cleanup(db2, ids)
            await db2.close()
    await db.close()


@pytest.mark.asyncio
async def test_a_pickup_location_is_not_mistaken_for_a_person(test_client):
    """"Amazon Locker - Rania" is infrastructure; it says nothing about who ordered."""
    from app.database import get_db
    db = await get_db()
    people = await amazon_service.person_categories(db)
    try:
        for junk in ("Amazon Locker - Velia Speedway",
                     "Amazon Hub Counter+ (Stonestown - Ana)",
                     "Not Available", "", None):
            assert amazon_service.person_for_recipient(junk, people) is None, junk
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_mixed_recipient_order_yields_no_single_person(test_client):
    """Two people on one order means there is no one answer; do not invent one."""
    from app.database import get_db
    db = await get_db()
    ids = None
    try:
        ids = await _seed_person(db, recipient="Ana Delgado", purchase_category="Ben")
        # A second item on the same order, for someone else.
        await db.execute(
            """INSERT INTO amazon_items
               (owner, order_id, asin, product_name, total_cents, ship_date, order_date,
                is_cancelled, used_gift_card, recipient, fingerprint)
               VALUES ('ana', ?, 'A8', 'Other Thing', 999, '2026-03-10','2026-03-09',
                       0,0,'Ben Whitfield Jr', ?)""",
            (ORDER, "fp-person-mixed"),
        )
        await db.commit()
        refund = dict((await (await db.execute(
            "SELECT * FROM amazon_refunds WHERE order_id=?", (ORDER,))).fetchone()))
        # The refund's OWN item still names Ana, so item-level evidence wins.
        assert await amazon_service.refund_person(db, refund) == "Ana"

        # With no attributed item, the order is genuinely mixed -> no answer.
        refund_no_item = {**refund, "matched_item_id": None}
        assert await amazon_service.refund_person(db, refund_no_item) is None
    finally:
        if ids:
            await _cleanup(db, ids)
        await db.close()
