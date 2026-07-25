"""A split parent must never end up on a spending category.

A composite charge is "split" into one child row per real item
(``amazon_service``) or component (``mortgage_splitter``), and the original row is
NEUTRALISED — flipped to ``Transfer`` / ``entity_type='transfer'`` — so the money
is counted exactly once, through the children.

Nothing used to stop a later recategorization from moving that parent back onto a
spending category, and then BOTH halves count. This is not hypothetical: the live
DB held

    txn 6967  Amazon  -$26.74  moved to 'Ben'/expense  reason='Bulk edit'
    txn 6983  Amazon  -$46.01  moved to 'Ben'/expense  reason='User correction'

each with two children summing to exactly the parent's amount, i.e. each purchase
counted twice. Every fixture below mirrors that shape.

Two things are asserted throughout, because either one alone is a false pass:

1. **The parent cannot move.** PATCH refuses it, bulk-edit skips it, apply-similar
   never even offers it, chat-correct skips it.
2. **Everything else still works.** A split CHILD is freely recategorizable —
   that IS per-item categorization — and a refund-paired row is not a split
   parent even though ``linked_transaction_id`` points at it, because refund and
   transfer pairing write that column SYMMETRICALLY. Over-protecting there would
   freeze normal rows out of editing, which is its own bug.

``call_llm`` is stubbed at both boundaries; a real shell-out hangs pytest forever
(see AGENTS.md).
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.amazon_matcher import SPLIT_PARENT_CATEGORY
from app.services.flat_categories import EXCLUDED_CATEGORY


TARGET_CATEGORY = "Groceries"

# Rows/aux rows this module inserted. The test DB is shared for the whole session
# and these fixtures deliberately put real spending amounts on real categories —
# leaving them behind breaks the dashboard-total tests in other modules.
_TXNS = []
_AMAZON_ITEMS = []
_RULE_PATTERNS = []


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """Neither router may shell out during these tests."""
    def explode(*args, **kwargs):
        raise AssertionError("no LLM call is expected on this path")

    monkeypatch.setattr("app.routers.transactions.call_llm", explode)
    monkeypatch.setattr("app.routers.chat_correct.call_llm", explode)


@pytest.fixture(autouse=True)
def clean_up():
    yield
    con = sqlite3.connect(os.environ["DB_PATH"])
    try:
        if _TXNS:
            con.execute(
                f"DELETE FROM transactions WHERE id IN ({','.join('?' * len(_TXNS))})",
                _TXNS,
            )
        if _AMAZON_ITEMS:
            con.execute(
                f"DELETE FROM amazon_items WHERE id IN ({','.join('?' * len(_AMAZON_ITEMS))})",
                _AMAZON_ITEMS,
            )
        for pattern in _RULE_PATTERNS:
            con.execute("DELETE FROM rules WHERE pattern=?", (pattern,))
        con.commit()
    finally:
        con.close()
        _TXNS.clear()
        _AMAZON_ITEMS.clear()
        _RULE_PATTERNS.clear()


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

async def _insert(db, **kw):
    cursor = await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, category_path, entity_type, business_context,
            linked_transaction_id, categorization_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            kw.get("platform", "test-split"),
            "split.csv",
            kw.get("account_id", "card-0001"),
            kw.get("date", "2026-04-01"),
            kw["amount"],
            kw.get("merchant", ""),
            kw.get("description", ""),
            kw.get("category"),
            kw.get("entity_type", "expense"),
            "personal",
            kw.get("linked"),
            kw.get("reason"),
        ),
    )
    _TXNS.append(cursor.lastrowid)
    return cursor.lastrowid


async def seed_split(tag, *, parent_category=SPLIT_PARENT_CATEGORY,
                     parent_entity_type="transfer"):
    """One split charge (parent + two children) plus ordinary siblings.

    ``parent_category`` defaults to the correctly neutralised state. Passing a
    spending category reproduces the live 6967/6983 corruption, which is what the
    ``/similar`` and apply-similar exclusions have to cope with — a parent that is
    ALREADY wrong must not be offered for further bulk writes.

    The two children sum to exactly the parent's amount, as a real split does.
    """
    from app.database import get_db

    name = f"Splitzon{tag}"
    db = await get_db()
    try:
        parent = await _insert(
            db, merchant=name, description=f"{name} order 112-{tag}",
            amount=-2674, category=parent_category, entity_type=parent_entity_type,
            date="2026-04-01", reason="Split into 2 Amazon line items",
        )
        ids = {
            "parent": parent,
            # -26.74 = -19.99 + -6.75, exactly like the live pair.
            "child_a": await _insert(
                db, merchant=f"{name}: Widget", description="Widget",
                amount=-1999, category=None, entity_type="expense",
                date="2026-04-01", linked=parent,
                reason=f"Amazon item from split of #{parent}",
            ),
            "child_b": await _insert(
                db, merchant=f"{name}: Tax & Shipping", description="Tax & Shipping",
                amount=-675, category=None, entity_type="expense",
                date="2026-04-01", linked=parent,
                reason=f"Amazon item from split of #{parent}",
            ),
            # Ordinary rows from the same payee: the legitimate targets.
            "plain_a": await _insert(
                db, merchant=name, amount=-1200, category="Uncategorized",
                entity_type="uncategorized", date="2026-04-02",
            ),
            "plain_b": await _insert(
                db, merchant=name, amount=-1300, category="Uncategorized",
                entity_type="uncategorized", date="2026-04-03",
            ),
        }
        await db.commit()
        return ids
    finally:
        await db.close()


async def fetch(txn_id):
    from app.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM transactions WHERE id=?", (txn_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def assert_split_intact(ids, *, expect_parent_category=SPLIT_PARENT_CATEGORY):
    """The invariant the whole guard exists to protect.

    The parent is neutralised, so it contributes nothing to any total, and the
    children still sum to exactly the parent's amount — meaning the charge is
    represented once, not twice.
    """
    parent = await fetch(ids["parent"])
    a = await fetch(ids["child_a"])
    b = await fetch(ids["child_b"])

    assert parent["category_path"] == expect_parent_category
    assert parent["entity_type"] in ("transfer", "excluded")
    assert a["amount_cents"] + b["amount_cents"] == parent["amount_cents"]
    assert a["linked_transaction_id"] == b["linked_transaction_id"] == parent["id"]


# --------------------------------------------------------------------------- #
# the helper itself
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_split_child_count_counts_children_but_not_symmetric_links(test_client):
    """``linked_transaction_id`` is overloaded; only the asymmetric case is a split.

    Refund pairing (``anomaly_detector._link_refund_pairs``) and transfer pairing
    both write the column in BOTH directions, and paystub linking points a row at
    itself. Reading any of those as "has children" would make ordinary rows
    permanently unrecategorizable.
    """
    from app.database import get_db
    from app.routers.transactions import split_child_count, split_parent_ids

    ids = await seed_split("Helper")
    db = await get_db()
    try:
        # A symmetric refund pair.
        purchase = await _insert(db, merchant="Zymbol Helper", amount=-5000,
                                 category="Shopping", date="2026-04-10")
        refund = await _insert(db, merchant="Zymbol Helper", amount=5000,
                               category="Shopping", entity_type="income",
                               date="2026-04-12", linked=purchase)
        await db.execute("UPDATE transactions SET linked_transaction_id=? WHERE id=?",
                         (refund, purchase))
        # A paystub-style self-link.
        selfie = await _insert(db, merchant="Payroll Helper", amount=250000,
                               category="Salary", entity_type="income",
                               date="2026-04-15")
        await db.execute("UPDATE transactions SET linked_transaction_id=? WHERE id=?",
                         (selfie, selfie))
        await db.commit()

        assert await split_child_count(db, ids["parent"]) == 2
        assert await split_child_count(db, ids["child_a"]) == 0
        assert await split_child_count(db, ids["plain_a"]) == 0
        assert await split_child_count(db, purchase) == 0, "refund pair is not a split"
        assert await split_child_count(db, refund) == 0
        assert await split_child_count(db, selfie) == 0, "self-link is not a child"

        parents = await split_parent_ids(db)
        assert ids["parent"] in parents
        assert not parents & {purchase, refund, selfie,
                              ids["child_a"], ids["plain_a"]}
    finally:
        await db.close()


def test_only_neutralising_categories_are_allowed_for_a_parent():
    from app.routers.transactions import category_neutralises_split
    from app.services.flat_categories import TRANSFER_CATEGORIES

    for safe in (SPLIT_PARENT_CATEGORY, EXCLUDED_CATEGORY, *TRANSFER_CATEGORIES):
        assert category_neutralises_split(safe), safe
    assert category_neutralises_split("Transfer > Mortgage Principal")

    for unsafe in ("Groceries", "Ben", "Shopping", "Other", "Salary", "", None):
        assert not category_neutralises_split(unsafe), unsafe


# --------------------------------------------------------------------------- #
# PATCH /api/transactions/{id}
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_patch_split_parent_to_a_spending_category_is_refused(test_client):
    """The exact 6983 corruption, refused with an explanation the user can act on."""
    ids = await seed_split("Patch")

    resp = test_client.patch(f"/api/transactions/{ids['parent']}",
                             json={"category_path": TARGET_CATEGORY})
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "2 item rows" in detail
    assert "26.74" in detail          # the parent's amount, in the message
    assert TARGET_CATEGORY in detail
    assert "Excluded" in detail       # what to do instead

    # Nothing was written — not the category, not the reason.
    row = await fetch(ids["parent"])
    assert row["category_path"] == SPLIT_PARENT_CATEGORY
    assert row["entity_type"] == "transfer"
    assert row["categorization_reason"] == "Split into 2 Amazon line items"
    await assert_split_intact(ids)


@pytest.mark.asyncio
async def test_a_refused_patch_writes_nothing_at_all(test_client):
    """The refusal is decided before the first write, so the tags in the same
    request are not half-applied."""
    ids = await seed_split("Atomic")
    before = await fetch(ids["parent"])

    resp = test_client.patch(f"/api/transactions/{ids['parent']}", json={
        "category_path": TARGET_CATEGORY, "tags": ["reviewed"],
    })
    assert resp.status_code == 409, resp.text

    after = await fetch(ids["parent"])
    assert after["tags"] == before["tags"]
    assert after["category_path"] == before["category_path"]


@pytest.mark.asyncio
async def test_patch_split_parent_to_excluded_is_allowed(test_client):
    """Opting the whole charge out cannot double-count, so it is the escape hatch."""
    ids = await seed_split("Excl")

    resp = test_client.patch(f"/api/transactions/{ids['parent']}",
                             json={"category_path": EXCLUDED_CATEGORY})
    assert resp.status_code == 200, resp.text

    row = await fetch(ids["parent"])
    assert row["category_path"] == EXCLUDED_CATEGORY
    assert row["entity_type"] == "excluded"
    await assert_split_intact(ids, expect_parent_category=EXCLUDED_CATEGORY)


@pytest.mark.asyncio
async def test_patch_split_parent_back_to_transfer_is_allowed(test_client):
    """Repairing a corrupted parent must not be blocked by the guard itself."""
    ids = await seed_split("Repair", parent_category="Ben",
                           parent_entity_type="expense")

    resp = test_client.patch(f"/api/transactions/{ids['parent']}",
                             json={"category_path": SPLIT_PARENT_CATEGORY})
    assert resp.status_code == 200, resp.text
    assert (await fetch(ids["parent"]))["category_path"] == SPLIT_PARENT_CATEGORY
    await assert_split_intact(ids)


@pytest.mark.asyncio
async def test_patch_a_split_child_is_allowed(test_client):
    """Per-item categorization is the entire point of splitting; it must keep working."""
    ids = await seed_split("Child")

    resp = test_client.patch(f"/api/transactions/{ids['child_a']}",
                             json={"category_path": TARGET_CATEGORY})
    assert resp.status_code == 200, resp.text

    child = await fetch(ids["child_a"])
    assert child["category_path"] == TARGET_CATEGORY
    assert child["entity_type"] == "expense"
    assert child["linked_transaction_id"] == ids["parent"]
    # Categorizing an item did not disturb the parent.
    await assert_split_intact(ids)


@pytest.mark.asyncio
async def test_patch_an_ordinary_row_is_unaffected(test_client):
    ids = await seed_split("Plain")

    resp = test_client.patch(f"/api/transactions/{ids['plain_a']}",
                             json={"category_path": TARGET_CATEGORY})
    assert resp.status_code == 200, resp.text
    assert (await fetch(ids["plain_a"]))["category_path"] == TARGET_CATEGORY


# --------------------------------------------------------------------------- #
# POST /api/transactions/bulk-edit
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_bulk_edit_skips_the_parent_and_still_updates_the_others(test_client):
    """A 40-row selection must not fail because one row happens to be a split charge."""
    ids = await seed_split("Bulk")

    resp = test_client.post("/api/transactions/bulk-edit", json={
        "ids": [ids["plain_a"], ids["parent"], ids["plain_b"]],
        "patch": {"category_path": TARGET_CATEGORY},
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # The keys the frontend already reads keep their meaning.
    assert data["updated_count"] == 2
    assert set(data["updated_ids"]) == {ids["plain_a"], ids["plain_b"]}
    # ...and the skip is reported rather than silently dropping the count.
    assert data["skipped_ids"] == [ids["parent"]]
    assert data["skipped_count"] == 1
    assert "item rows" in data["skipped_reason"]

    for key in ("plain_a", "plain_b"):
        assert (await fetch(ids[key]))["category_path"] == TARGET_CATEGORY
    await assert_split_intact(ids)


@pytest.mark.asyncio
async def test_bulk_edit_reports_no_skips_when_there_are_none(test_client):
    ids = await seed_split("BulkClean")

    data = test_client.post("/api/transactions/bulk-edit", json={
        "ids": [ids["plain_a"], ids["plain_b"]],
        "patch": {"category_path": TARGET_CATEGORY},
    }).json()
    assert data["updated_count"] == 2
    assert data["skipped_ids"] == []
    assert data["skipped_reason"] is None


@pytest.mark.asyncio
async def test_bulk_edit_can_still_exclude_a_split_parent(test_client):
    ids = await seed_split("BulkExcl")

    data = test_client.post("/api/transactions/bulk-edit", json={
        "ids": [ids["parent"]],
        "patch": {"category_path": EXCLUDED_CATEGORY},
    }).json()
    assert data["updated_ids"] == [ids["parent"]]
    assert data["skipped_ids"] == []
    await assert_split_intact(ids, expect_parent_category=EXCLUDED_CATEGORY)


@pytest.mark.asyncio
async def test_bulk_edit_of_notes_only_does_not_touch_the_guard(test_client):
    """The guard is about categories; annotating a split parent is harmless."""
    ids = await seed_split("BulkNote")

    data = test_client.post("/api/transactions/bulk-edit", json={
        "ids": [ids["parent"]],
        "patch": {"notes": "combined Amazon charge"},
    }).json()
    assert data["updated_ids"] == [ids["parent"]]
    assert (await fetch(ids["parent"]))["notes"] == "combined Amazon charge"
    await assert_split_intact(ids)


# --------------------------------------------------------------------------- #
# GET /{id}/similar and POST /apply-similar
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_similar_never_lists_a_split_parent(test_client):
    """Including an already-corrupted parent (the live 6967 state) would re-corrupt it.

    The parent here sits on a spending category, so the pre-existing
    transfer/excluded filters do NOT cover it — only the split-parent exclusion
    does.
    """
    ids = await seed_split("Sim", parent_category="Ben", parent_entity_type="expense")

    data = test_client.get(f"/api/transactions/{ids['plain_a']}/similar",
                           params={"category_path": TARGET_CATEGORY}).json()
    got = {m["id"] for m in data["matches"]}
    assert ids["plain_b"] in got, "the ordinary sibling is still offered"
    assert ids["parent"] not in got, "a split parent must never be offered"


@pytest.mark.asyncio
async def test_apply_similar_all_matching_does_not_touch_a_split_parent(test_client):
    ids = await seed_split("Apply", parent_category="Ben",
                           parent_entity_type="expense")

    resp = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["plain_a"],
        "category_path": TARGET_CATEGORY,
        "ids": [],
        "all_matching": True,
    })
    assert resp.status_code == 200, resp.text
    assert ids["parent"] not in resp.json()["updated_ids"]

    parent = await fetch(ids["parent"])
    assert parent["category_path"] == "Ben", "still wrong, but not made worse"
    assert parent["categorization_reason"] == "Split into 2 Amazon line items"


@pytest.mark.asyncio
async def test_apply_similar_refuses_an_explicitly_ticked_split_parent(test_client):
    """A stale client that still has the parent in its list cannot write through."""
    ids = await seed_split("ApplyId", parent_category="Ben",
                           parent_entity_type="expense")

    resp = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["plain_a"],
        "category_path": TARGET_CATEGORY,
        "ids": [ids["parent"]],
    })
    assert resp.status_code == 400, resp.text
    assert (await fetch(ids["parent"]))["category_path"] == "Ben"


@pytest.mark.asyncio
async def test_similar_never_lists_an_amazon_matched_row(test_client):
    """An Amazon-matched row's category comes from the item export, not a merchant key.

    A bulk apply on "Amazon" silently replaced that better information; in the
    live DB one such apply overwrote 107 correctly-booked refunds.
    """
    from app.database import get_db

    ids = await seed_split("Amz")
    db = await get_db()
    try:
        matched = await _insert(
            db, merchant=f"Splitzon{'Amz'}", amount=-4601, category="Shopping",
            date="2026-04-05",
        )
        cursor = await db.execute(
            """INSERT INTO amazon_items
               (order_id, owner, product_name, total_cents, matched_transaction_id,
                fingerprint)
               VALUES ('112-AMZ-TEST', 'ben', 'Bluetooth Speaker', 4601, ?, ?)""",
            (matched, f"fp-split-guard-{matched}"),
        )
        _AMAZON_ITEMS.append(cursor.lastrowid)
        await db.commit()
    finally:
        await db.close()

    data = test_client.get(f"/api/transactions/{ids['plain_a']}/similar",
                           params={"category_path": TARGET_CATEGORY}).json()
    got = {m["id"] for m in data["matches"]}
    assert ids["plain_b"] in got, "ordinary siblings are still offered"
    assert matched not in got, "an Amazon-matched row must not be offered"

    # And apply-similar therefore cannot reach it either.
    test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["plain_a"], "category_path": TARGET_CATEGORY,
        "ids": [], "all_matching": True,
    })
    assert (await fetch(matched))["category_path"] == "Shopping"


@pytest.mark.asyncio
async def test_similar_never_lists_an_amazon_matched_refund(test_client):
    from app.database import get_db

    ids = await seed_split("AmzRef")
    db = await get_db()
    try:
        refunded = await _insert(
            db, merchant="SplitzonAmzRef", amount=1500, category="Shopping",
            entity_type="income", date="2026-04-06",
        )
        await db.execute(
            """INSERT INTO amazon_refunds
               (order_id, owner, refund_date, amount_cents, matched_transaction_id,
                fingerprint)
               VALUES ('112-AMZREF-TEST', 'ben', '2026-04-06', 1500, ?, ?)""",
            (refunded, f"fp-split-guard-refund-{refunded}"),
        )
        await db.commit()
    finally:
        await db.close()

    try:
        data = test_client.get(f"/api/transactions/{ids['plain_a']}/similar",
                               params={"category_path": TARGET_CATEGORY}).json()
        assert refunded not in {m["id"] for m in data["matches"]}
    finally:
        con = sqlite3.connect(os.environ["DB_PATH"])
        try:
            con.execute("DELETE FROM amazon_refunds WHERE matched_transaction_id=?",
                        (refunded,))
            con.commit()
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# POST /api/chat-correct
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_chat_correct_skips_the_split_parent_and_says_so(test_client, monkeypatch):
    ids = await seed_split("Chat")
    search = "splitzonchat"
    _RULE_PATTERNS.append(search)

    def mock_call_llm(prompt, **kwargs):
        return json.dumps({
            "summary": "Filed Splitzon under Groceries",
            "actions": [{
                "type": "recategorize",
                "params": {"search": search, "category_path": TARGET_CATEGORY},
            }],
        })

    monkeypatch.setattr("app.routers.chat_correct.call_llm", mock_call_llm)

    resp = test_client.post("/api/chat-correct",
                            json={"message": f"Put all {search} under {TARGET_CATEGORY}"})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert ids["parent"] not in data["affected_ids"]
    # The children and the ordinary rows all match the search string and are fine.
    for key in ("child_a", "child_b", "plain_a", "plain_b"):
        assert ids[key] in data["affected_ids"], key

    diff = data["context_diff"] or ""
    assert "1 rows skipped" in diff
    assert "split into item rows" in diff

    await assert_split_intact(ids)


# --------------------------------------------------------------------------- #
# the regression this whole module exists for
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_no_write_path_can_double_count_a_split_charge(test_client, monkeypatch):
    """Hit every entry point that damaged the live DB and assert the invariant holds.

    The corruption signature was: parent on a spending category AND children
    summing to the parent — the same money counted twice. After the guard, no
    endpoint can produce it.
    """
    ids = await seed_split("Regress")
    search = "splitzonregress"
    _RULE_PATTERNS.append(search)

    # 1. PATCH ("User correction" — what damaged 6983).
    assert test_client.patch(
        f"/api/transactions/{ids['parent']}",
        json={"category_path": TARGET_CATEGORY},
    ).status_code == 409

    # 2. bulk-edit ("Bulk edit" — what damaged 6967).
    bulk = test_client.post("/api/transactions/bulk-edit", json={
        "ids": [ids["parent"]], "patch": {"category_path": "Ben"},
    }).json()
    assert bulk["updated_ids"] == []

    # 3. apply-similar, both shapes.
    test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["plain_a"], "category_path": TARGET_CATEGORY,
        "ids": [], "all_matching": True,
    })
    assert test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["plain_a"], "category_path": TARGET_CATEGORY,
        "ids": [ids["parent"]],
    }).status_code == 400

    # 4. chat-correct.
    monkeypatch.setattr(
        "app.routers.chat_correct.call_llm",
        lambda prompt, **kw: json.dumps({
            "summary": "bulk", "actions": [{
                "type": "recategorize",
                "params": {"search": search, "category_path": TARGET_CATEGORY},
            }],
        }),
    )
    assert test_client.post(
        "/api/chat-correct", json={"message": f"move {search} to {TARGET_CATEGORY}"}
    ).status_code == 200

    # The invariant, after all four.
    await assert_split_intact(ids)
    parent = await fetch(ids["parent"])
    assert parent["categorization_reason"] == "Split into 2 Amazon line items"

    # And the charge is represented exactly once in what the dashboard counts:
    # the parent is a transfer (never summed), the children are.
    countable = [r for r in [parent, await fetch(ids["child_a"]),
                             await fetch(ids["child_b"])]
                 if r["entity_type"] not in ("transfer", "excluded")]
    assert sum(r["amount_cents"] for r in countable) == parent["amount_cents"]


# --------------------------------------------------------------------------- #
# Split CHILDREN: like propagates to like
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_bare_parent_charge_is_never_offered_the_item_children(test_client):
    """The near-disaster: one Amazon row sweeping every product row.

    Split children are named "Amazon: <product>", so their merchant key contains
    "amazon" and scores a subset "strong" match against a bare "Amazon" charge. On
    the real DB that offered 1,342 rows for a single inline edit, which would have
    flattened every product-level category the Amazon split produced.
    """
    from app.database import get_db
    db = await get_db()
    ids = []
    try:
        # An ordinary bare Amazon charge, plus a split parent with two product children.
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, category_path, entity_type, business_context)
               VALUES ('plaid','g.csv','card-0002','2026-06-01',-5000,
                       'Amazon','Amazon','Shopping','expense','personal')""")
        bare = cursor.lastrowid; ids.append(bare)
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, category_path, entity_type, business_context)
               VALUES ('plaid','g.csv','card-0002','2026-06-02',-3000,
                       'Amazon','Amazon','Transfer','transfer','personal')""")
        parent = cursor.lastrowid; ids.append(parent)
        for name, cat, amt in (("Amazon: Widget Alpha", "Health", -1000),
                               ("Amazon: Widget Beta", "Pets", -2000)):
            cursor = await db.execute(
                """INSERT INTO transactions
                   (source_platform, source_file, account_id, posted_date, amount_cents,
                    merchant, description, category_path, entity_type, business_context,
                    linked_transaction_id)
                   VALUES ('plaid','g.csv','card-0002','2026-06-02',?,?,?,?,'expense',
                           'personal',?)""",
                (amt, name, name, cat, parent))
            ids.append(cursor.lastrowid)
        await db.commit()

        resp = test_client.get(
            f"/api/transactions/{bare}/similar", params={"category_path": "Groceries"})
        assert resp.status_code == 200, resp.text
        offered = {m["id"] for m in resp.json()["matches"]}
        assert parent not in offered, "a split parent must never be offered"
        for child in ids[2:]:
            assert child not in offered, (
                "a product child must not be offered to a bare parent-charge source — "
                "that flattens the per-item categories")
    finally:
        for txn_id in reversed(ids):
            await db.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
        await db.commit()
        await db.close()


@pytest.mark.asyncio
async def test_one_product_child_still_propagates_to_the_same_product(test_client):
    """The useful case must keep working: like propagates to like.

    Filing one "Amazon: Fresh Step" child under Pets should still offer the other
    Fresh Step rows, which are children too.
    """
    from app.database import get_db
    db = await get_db()
    ids = []
    try:
        parents = []
        for _ in range(2):
            cursor = await db.execute(
                """INSERT INTO transactions
                   (source_platform, source_file, account_id, posted_date, amount_cents,
                    merchant, description, category_path, entity_type, business_context)
                   VALUES ('plaid','g.csv','card-0002','2026-06-05',-2000,
                           'Amazon','Amazon','Transfer','transfer','personal')""")
            parents.append(cursor.lastrowid); ids.append(cursor.lastrowid)
        children = []
        for parent in parents:
            cursor = await db.execute(
                """INSERT INTO transactions
                   (source_platform, source_file, account_id, posted_date, amount_cents,
                    merchant, description, category_path, entity_type, business_context,
                    linked_transaction_id)
                   VALUES ('plaid','g.csv','card-0002','2026-06-05',-2000,
                           'Amazon: Freshstep Clumping Cat Litter Unscented',
                           'Amazon: Freshstep Clumping Cat Litter Unscented',
                           'Shopping','expense','personal',?)""",
                (parent,))
            children.append(cursor.lastrowid); ids.append(cursor.lastrowid)
        await db.commit()

        resp = test_client.get(
            f"/api/transactions/{children[0]}/similar", params={"category_path": "Pets"})
        assert resp.status_code == 200, resp.text
        offered = {m["id"] for m in resp.json()["matches"]}
        assert children[1] in offered, (
            "a child source must still reach its sibling product rows")
        for parent in parents:
            assert parent not in offered, "split parents stay protected either way"
    finally:
        for txn_id in reversed(ids):
            await db.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
        await db.commit()
        await db.close()
