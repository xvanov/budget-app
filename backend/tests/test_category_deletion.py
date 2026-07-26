"""A deleted category stays deleted.

The bug these tests lock down, in the owner's words: "I delete categories and
then I go away from the view and come back and they're there again… Travel — I've
deleted it twice. Housing — deleted twice."

Two mechanisms put them back:

1. ``seed_flat_categories`` re-INSERTed every name in ``DEFAULT_CATEGORIES`` on
   every boot (and inside ``migrate_to_flat``, reachable from
   ``POST /api/categories/flatten``). It was documented as "idempotent; never
   deletes", and it was — idempotent with respect to the shipped defaults, which
   is precisely what undid the user's deletions.
2. Anything that writes a category NAME onto a transaction: the categorizer's
   prompt offered a hard-coded default list, an import rule outlives the category
   it points at, and ``ensure_flat_category`` created whatever the chat model
   asked for. The expenses/income breakdown is built from transaction data, so a
   deleted name reappearing there is indistinguishable from the row coming back.

Deleting must not be a permanent ban either — an explicit re-create has to work.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import get_db, init_db, seed_categories
from app.services.flat_categories import (
    CategoryDeleted,
    DEFAULT_CATEGORIES,
    deleted_category_names,
    seed_flat_categories,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _paths(test_client):
    resp = test_client.get("/api/categories")
    assert resp.status_code == 200
    return [c["path"] for c in resp.json()["categories"]]


def _id_of(test_client, path):
    for c in test_client.get("/api/categories").json()["categories"]:
        if c["path"] == path:
            return c["id"]
    return None


async def _boot_category_seeds():
    """Exactly what app startup does to the categories table."""
    await init_db()
    await seed_categories()
    db = await get_db()
    try:
        return await seed_flat_categories(db)
    finally:
        await db.close()


@pytest.fixture
def restore_categories(test_client):
    """Put the shared session DB back the way we found it.

    The backend test suite shares one SQLite file across every module, so a
    tombstone or a missing default left behind here would silently change later
    tests. Cleanup uses plain sqlite3 so it works from a sync fixture.
    """
    before = set(_paths(test_client))
    yield
    after = set(_paths(test_client))
    for path in sorted(after - before):
        cat_id = _id_of(test_client, path)
        if cat_id is not None:
            test_client.delete(f"/api/categories/{cat_id}?force=true")
    conn = sqlite3.connect(os.environ["DB_PATH"])
    try:
        conn.execute("DELETE FROM deleted_categories")
        conn.commit()
    finally:
        conn.close()
    for path in sorted(before - set(_paths(test_client))):
        if " > " in path:
            continue  # legacy hierarchical seed rows are not re-created by name
        test_client.post("/api/categories", json={"name": path})


async def _insert_txn(db, *, amount_cents, category_path, entity_type, merchant="ACME STORE"):
    cursor = await db.execute(
        """INSERT INTO transactions
             (source_platform, source_file, account_id, posted_date, amount_cents,
              merchant, description, normalized_description, category_path, entity_type)
           VALUES ('test', 'test.csv', 'test-acct', '2026-03-01', ?, ?, ?, ?, ?, ?)""",
        (amount_cents, merchant, merchant, merchant.lower(), category_path, entity_type),
    )
    await db.commit()
    return cursor.lastrowid


# ── (a) the seed must not resurrect a deletion ───────────────────────────────

@pytest.mark.asyncio
async def test_deleted_default_is_not_reseeded(test_client, restore_categories):
    """Delete a shipped default, re-run the seed, and it must stay gone."""
    assert "Travel" in DEFAULT_CATEGORIES
    assert "Travel" in _paths(test_client)

    cat_id = _id_of(test_client, "Travel")
    resp = test_client.delete(f"/api/categories/{cat_id}?force=true")
    assert resp.status_code == 200, resp.text
    assert resp.json()["tombstoned"] is True
    assert "Travel" not in _paths(test_client)

    db = await get_db()
    try:
        result = await seed_flat_categories(db)
    finally:
        await db.close()

    assert result["skipped_deleted"] >= 1
    assert "Travel" not in _paths(test_client), (
        "seed_flat_categories re-created a category the user deleted"
    )


@pytest.mark.asyncio
async def test_deleted_default_survives_a_boot(test_client, restore_categories):
    """The full startup category sequence (init_db + both seeds) keeps it gone."""
    cat_id = _id_of(test_client, "Housing")
    assert test_client.delete(f"/api/categories/{cat_id}?force=true").status_code == 200

    await _boot_category_seeds()
    assert "Housing" not in _paths(test_client)

    # Twice, because the user deleted it twice and it came back twice.
    await _boot_category_seeds()
    assert "Housing" not in _paths(test_client)


def test_flatten_endpoint_does_not_resurrect(test_client, restore_categories):
    """POST /api/categories/flatten runs migrate_to_flat -> seed. Still gone."""
    cat_id = _id_of(test_client, "Education")
    assert test_client.delete(f"/api/categories/{cat_id}?force=true").status_code == 200

    resp = test_client.post("/api/categories/flatten")
    assert resp.status_code == 200, resp.text
    assert "Education" not in _paths(test_client)


# ── (b) an explicit re-create still works ────────────────────────────────────

@pytest.mark.asyncio
async def test_deleted_category_can_be_explicitly_recreated(test_client, restore_categories):
    """Deletion is a decision, not a permanent ban."""
    cat_id = _id_of(test_client, "Shopping")
    assert test_client.delete(f"/api/categories/{cat_id}?force=true").status_code == 200
    assert "Shopping" not in _paths(test_client)

    resp = test_client.post("/api/categories", json={"name": "Shopping"})
    assert resp.status_code == 201, resp.text
    assert "Shopping" in _paths(test_client)

    # The tombstone is cleared, so the seed leaves the row alone rather than
    # inserting a duplicate the UI could not render (two rows, one React key).
    db = await get_db()
    try:
        await seed_flat_categories(db)
        assert "shopping" not in await deleted_category_names(db)
        cursor = await db.execute("SELECT COUNT(*) FROM categories WHERE path='Shopping'")
        assert (await cursor.fetchone())[0] == 1
    finally:
        await db.close()


# ── (c) categorization must not resurrect a deleted name ─────────────────────

@pytest.mark.asyncio
async def test_categorizer_prompt_offers_only_live_categories(test_client, restore_categories):
    """The prompt is built from the DB, and a deleted name is not on it."""
    from app.services.categorizer import _live_categories, build_categorization_prompt

    cat_id = _id_of(test_client, "Travel")
    assert test_client.delete(f"/api/categories/{cat_id}?force=true").status_code == 200

    db = await get_db()
    try:
        available = await _live_categories(db)
    finally:
        await db.close()

    assert "Travel" not in available
    assert "Groceries" in available
    prompt = build_categorization_prompt(
        [{"id": 1, "posted_date": "2026-03-01", "merchant": "AIRLINE",
          "amount_cents": -50000, "description": "FLIGHT", "account_id": "a"}],
        available,
    )
    assert "\nTravel\n" not in prompt


@pytest.mark.asyncio
async def test_llm_proposing_deleted_category_falls_back(test_client, monkeypatch,
                                                         restore_categories):
    """A model answer of "Travel" after Travel was deleted must not stick, and
    must not create the row."""
    from app.services.categorizer import _categorize_batch

    cat_id = _id_of(test_client, "Travel")
    assert test_client.delete(f"/api/categories/{cat_id}?force=true").status_code == 200

    db = await get_db()
    try:
        txn_id = await _insert_txn(db, amount_cents=-50000, category_path=None,
                                   entity_type="uncategorized", merchant="AIRLINE CO")

        def fake_llm(prompt, **kwargs):
            # The prompt must not have offered Travel; the model proposes it anyway.
            assert "\nTravel\n" not in prompt
            return json.dumps([{
                "id": txn_id, "category_path": "Travel", "confidence_score": 0.9,
                "categorization_reason": "Looks like a flight",
            }])

        monkeypatch.setattr("app.services.categorizer.call_llm", fake_llm)
        await _categorize_batch(db, [{
            "id": txn_id, "posted_date": "2026-03-01", "merchant": "AIRLINE CO",
            "amount_cents": -50000, "description": "FLIGHT", "account_id": "test-acct",
        }])
        await db.commit()

        cursor = await db.execute(
            "SELECT category_path, entity_type, categorization_reason "
            "FROM transactions WHERE id=?", (txn_id,),
        )
        row = await cursor.fetchone()
        assert row["category_path"] == "Other"
        assert row["entity_type"] == "expense"
        assert "does not exist" in row["categorization_reason"]

        cursor = await db.execute("SELECT COUNT(*) FROM categories WHERE path='Travel'")
        assert (await cursor.fetchone())[0] == 0

        await db.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
        await db.commit()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rule_into_deleted_category_is_suspended(test_client, restore_categories):
    """A rule outlives its category. It must not keep filing rows under the
    deleted name — and the rule row itself must survive, so re-creating the
    category brings it back."""
    from app.services.rules_service import add_rule, apply_rules

    db = await get_db()
    try:
        rule_id = await add_rule(db, "zanzibar tours", "Travel", "test rule")
    finally:
        await db.close()

    cat_id = _id_of(test_client, "Travel")
    assert test_client.delete(f"/api/categories/{cat_id}?force=true").status_code == 200

    db = await get_db()
    try:
        txn_id = await _insert_txn(db, amount_cents=-12345, category_path="Uncategorized",
                                   entity_type="expense", merchant="ZANZIBAR TOURS LTD")
        await apply_rules(db)
        cursor = await db.execute("SELECT category_path FROM transactions WHERE id=?", (txn_id,))
        assert (await cursor.fetchone())["category_path"] == "Uncategorized"
        cursor = await db.execute("SELECT COUNT(*) FROM rules WHERE id=?", (rule_id,))
        assert (await cursor.fetchone())[0] == 1, "the rule row must not be destroyed"
    finally:
        await db.close()

    # Bring Travel back explicitly: the rule resumes.
    assert test_client.post("/api/categories", json={"name": "Travel"}).status_code == 201
    db = await get_db()
    try:
        await apply_rules(db)
        cursor = await db.execute("SELECT category_path FROM transactions WHERE id=?", (txn_id,))
        assert (await cursor.fetchone())["category_path"] == "Travel"
        await db.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
        await db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        await db.commit()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ensure_flat_category_refuses_deleted_name(test_client, restore_categories):
    """The chat path's "create it if it's new" helper must not revive a deletion
    implicitly, but must when the user explicitly asks."""
    from app.routers.categories import ensure_flat_category

    cat_id = _id_of(test_client, "Personal Care")
    assert test_client.delete(f"/api/categories/{cat_id}?force=true").status_code == 200

    db = await get_db()
    try:
        with pytest.raises(CategoryDeleted):
            await ensure_flat_category(db, "Personal Care")
        cursor = await db.execute("SELECT COUNT(*) FROM categories WHERE path='Personal Care'")
        assert (await cursor.fetchone())[0] == 0

        # Case-insensitive: the same decision.
        with pytest.raises(CategoryDeleted):
            await ensure_flat_category(db, "personal care")

        new_id = await ensure_flat_category(db, "Personal Care", allow_revive=True)
        assert new_id
        assert "personal care" not in await deleted_category_names(db)
    finally:
        await db.close()


# ── (d) deleting a category in use ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_force_delete_in_use_uncategorizes_and_persists(test_client,
                                                              restore_categories):
    """force=true: transactions become Uncategorized with entity_type recomputed
    from the amount's SIGN, and the category row is gone for good."""
    db = await get_db()
    try:
        spend_id = await _insert_txn(db, amount_cents=-8800, category_path="Travel",
                                     entity_type="expense", merchant="HOTEL ONE")
        refund_id = await _insert_txn(db, amount_cents=4400, category_path="Travel",
                                      entity_type="expense", merchant="HOTEL ONE")
    finally:
        await db.close()

    cat_id = _id_of(test_client, "Travel")
    resp = test_client.delete(f"/api/categories/{cat_id}?force=true")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transactions_moved"] == 2
    assert body["moved_to"] == "Uncategorized"

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, category_path, entity_type FROM transactions WHERE id IN (?, ?)",
            (spend_id, refund_id),
        )
        rows = {r["id"]: dict(r) for r in await cursor.fetchall()}
        assert rows[spend_id]["category_path"] == "Uncategorized"
        assert rows[spend_id]["entity_type"] == "expense"
        # Positive amount -> income. The sign decides, not the old category.
        assert rows[refund_id]["category_path"] == "Uncategorized"
        assert rows[refund_id]["entity_type"] == "income"

        await seed_flat_categories(db)
        cursor = await db.execute("SELECT COUNT(*) FROM categories WHERE path='Travel'")
        assert (await cursor.fetchone())[0] == 0

        await db.execute("DELETE FROM transactions WHERE id IN (?, ?)", (spend_id, refund_id))
        await db.commit()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reassign_delete_moves_transactions_and_persists(test_client,
                                                               restore_categories):
    """reassign_to: transactions move to the chosen category, deletion persists."""
    db = await get_db()
    try:
        spend_id = await _insert_txn(db, amount_cents=-2500, category_path="Entertainment",
                                     entity_type="expense", merchant="CINEMA PLUS")
        refund_id = await _insert_txn(db, amount_cents=1500, category_path="Entertainment",
                                      entity_type="expense", merchant="CINEMA PLUS")
    finally:
        await db.close()

    cat_id = _id_of(test_client, "Entertainment")
    resp = test_client.delete(f"/api/categories/{cat_id}?reassign_to=Other")
    assert resp.status_code == 200, resp.text
    # `>= 2`, not `== 2`: the DB is shared across the session and another test file
    # (test_budgets) seeds its own Entertainment rows, so an exact count made this
    # pass alone and fail when the suite ran in file order. What this test is about
    # is that THESE two rows move and keep the right entity_type — asserted below.
    assert resp.json()["transactions_moved"] >= 2
    assert "Entertainment" not in _paths(test_client)

    db = await get_db()
    try:
        await seed_flat_categories(db)
        cursor = await db.execute(
            "SELECT id, category_path, entity_type FROM transactions WHERE id IN (?, ?)",
            (spend_id, refund_id),
        )
        rows = {r["id"]: dict(r) for r in await cursor.fetchall()}
        assert rows[spend_id]["category_path"] == "Other"
        assert rows[spend_id]["entity_type"] == "expense"
        assert rows[refund_id]["category_path"] == "Other"
        assert rows[refund_id]["entity_type"] == "income"

        cursor = await db.execute("SELECT COUNT(*) FROM categories WHERE path='Entertainment'")
        assert (await cursor.fetchone())[0] == 0

        await db.execute("DELETE FROM transactions WHERE id IN (?, ?)", (spend_id, refund_id))
        await db.commit()
    finally:
        await db.close()
