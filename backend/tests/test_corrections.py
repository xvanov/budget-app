"""Tests for Story 05: User corrections re-categorize semantically AND update the living context."""

import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.rules_service import list_rules, apply_rules, add_rule
from app.services.rules_service import determine_entity_type


def _make_mock_llm():
    """Return a mock call_llm for the regular categorizer (categorizes by amount sign)."""
    def mock_call_llm(prompt, **kwargs):
        results = []
        for line in prompt.split('\n'):
            import re
            m = re.match(r'\s+ID=(\d+):', line)
            if m:
                txn_id = int(m.group(1))
                is_negative = '$-' in line
                results.append({
                    "id": txn_id,
                    "category_path": "Other" if is_negative else "Other Income",
                    "entity_type": "expense" if is_negative else "income",
                    "confidence_score": 0.85,
                    "categorization_reason": "Test categorization",
                    "business_context": "personal",
                })
        return json.dumps(results)
    return mock_call_llm


def _make_semantic_mock(related_ids, pattern="navarro"):
    """Return a mock call_llm for the semantic matching step in PATCH."""
    def mock_call_llm(prompt, **kwargs):
        return json.dumps({
            "related_ids": related_ids,
            "pattern": pattern,
            "rule_description": "TERESA NAVARRO Zelle/Cash App payments are rental income",
            "context_fact": "TERESA NAVARRO is a tenant who pays rent via Zelle and Cash App.",
        })
    return mock_call_llm


def _a_positive_txn_id(test_client):
    """A real transaction id with a POSITIVE amount.

    Two reasons not to hardcode id 1. It assumes this test is the first thing in
    the session to touch the transactions table — any earlier test that inserts
    and deletes rows consumes the low autoincrement ids and `/api/transactions/1`
    404s. And these tests assign "Rental Income", which only stays coherent on an
    inflow now that entity_type follows the amount's sign.
    """
    resp = test_client.get("/api/transactions?page_size=200&scope=all")
    assert resp.status_code == 200, resp.text
    for t in resp.json()["transactions"]:
        if (t["amount_cents"] or 0) > 0:
            return t["id"]
    raise AssertionError("no positive-amount transaction in the fixture data")


def _import_with_mock(test_client, monkeypatch):
    """Import samples with mocked categorizer LLM."""
    monkeypatch.setattr("app.services.categorizer.call_llm", _make_mock_llm())
    resp = test_client.post("/api/import/samples")
    assert resp.status_code == 200
    return resp


async def _tenant_rows(db):
    """Ids of one tenant's rent inflows, seeded on first use.

    "Apply this to every matching row" only means something when the payer shows
    up more than once and on more than one rail, spelled differently each time.
    Seeding that here instead of fishing for a name in ``samples/`` is what keeps
    these tests working on a machine that has no import data at all. Idempotent,
    because every test in this module shares one database.
    """
    cursor = await db.execute(
        "SELECT id FROM transactions WHERE LOWER(merchant) LIKE '%navarro%' ORDER BY id"
    )
    existing = [r[0] for r in await cursor.fetchall()]
    if existing:
        return existing

    ids = []
    for platform, account, merchant, description in (
        ("chase", "bank-0001", "TERESA NAVARRO", "Zelle payment from TERESA NAVARRO"),
        ("chase", "bank-0001", "Zelle payment to Teresa Navarro", "Zelle credit"),
        ("cash_app", "cash-app", "Teresa Navarro", "Cash App payment received"),
    ):
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, category_path, entity_type, business_context)
               VALUES (?, 'tenant.csv', ?, '2026-02-01', 120000, ?, ?,
                       'Other Income', 'income', 'personal')""",
            (platform, account, merchant, description),
        )
        ids.append(cursor.lastrowid)
    await db.commit()
    return ids


@pytest.mark.asyncio
async def test_patch_returns_correct_structure(test_client, monkeypatch):
    """PATCH /api/transactions/{id} returns {re_categorized_count, affected_ids, context_diff, before, after}."""
    _import_with_mock(test_client, monkeypatch)

    monkeypatch.setattr("app.routers.transactions.call_llm", _make_semantic_mock([]))
    txn_id = _a_positive_txn_id(test_client)

    resp = test_client.patch(
        f"/api/transactions/{txn_id}",
        json={"category_path": "Rental Income", "apply_to_matching": False}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "re_categorized_count" in data
    assert "affected_ids" in data
    assert "context_diff" in data
    assert "before" in data
    assert "after" in data
    assert data["re_categorized_count"] == 1
    assert data["affected_ids"] == [txn_id]
    assert data["before"]["id"] == txn_id
    assert data["after"]["category_path"] == "Rental Income"


@pytest.mark.asyncio
async def test_patch_single_updates_category(test_client, monkeypatch):
    """PATCH without apply_to_matching updates only that transaction."""
    _import_with_mock(test_client, monkeypatch)

    txn_id = _a_positive_txn_id(test_client)
    resp = test_client.patch(
        f"/api/transactions/{txn_id}",
        json={"category_path": "Rental Income", "apply_to_matching": False}
    )
    assert resp.status_code == 200, resp.text

    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute("SELECT category_path FROM transactions WHERE id=?", (txn_id,))
        row = await cursor.fetchone()
        assert row["category_path"] == "Rental Income"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_patch_apply_to_matching_updates_semantic_navarro(test_client, monkeypatch):
    """PATCH with apply_to_matching=true updates all NAVARRO rows across channels."""
    _import_with_mock(test_client, monkeypatch)

    from app.database import get_db
    db = await get_db()
    try:
        navarro_ids = await _tenant_rows(db)
    finally:
        await db.close()

    assert len(navarro_ids) > 0, "Expected at least one NAVARRO row in the dataset"

    monkeypatch.setattr("app.routers.transactions.call_llm", _make_semantic_mock(navarro_ids))

    target_id = navarro_ids[0]
    resp = test_client.patch(
        f"/api/transactions/{target_id}",
        json={"category_path": "Rental Income", "apply_to_matching": True}
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["re_categorized_count"] == len(navarro_ids)
    assert set(data["affected_ids"]) == set(navarro_ids)
    assert "context_diff" in data

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, category_path, entity_type FROM transactions WHERE LOWER(merchant) LIKE '%navarro%'"
        )
        updated = await cursor.fetchall()
        for row in updated:
            assert row["category_path"] == "Rental Income", \
                f"Row {row['id']} has wrong category: {row['category_path']}"
            assert row["entity_type"] == "income", \
                f"Row {row['id']} has wrong entity_type: {row['entity_type']}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_rules_list_after_correction(test_client, monkeypatch):
    """After correction with apply_to_matching, GET /api/rules includes the new rule."""
    _import_with_mock(test_client, monkeypatch)

    from app.database import get_db
    db = await get_db()
    try:
        navarro_ids = await _tenant_rows(db)
    finally:
        await db.close()

    monkeypatch.setattr("app.routers.transactions.call_llm", _make_semantic_mock(navarro_ids))

    target_id = navarro_ids[0]
    test_client.patch(
        f"/api/transactions/{target_id}",
        json={"category_path": "Rental Income", "apply_to_matching": True}
    )

    resp = test_client.get("/api/rules")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["rules"]) >= 1
    rule = data["rules"][0]
    assert rule["pattern"] == "navarro"
    assert rule["category_path"] == "Rental Income"

    rule_id = rule["id"]
    resp2 = test_client.get(f"/api/rules/{rule_id}")
    assert resp2.status_code == 200
    rule_detail = resp2.json()
    assert rule_detail["pattern"] == "navarro"


@pytest.mark.asyncio
async def test_rules_persist_and_applied_on_import(test_client, monkeypatch):
    """Rule survives restart (DB commit) and is applied on next import BEFORE LLM."""
    _import_with_mock(test_client, monkeypatch)

    from app.database import get_db
    db = await get_db()
    try:
        navarro_ids = await _tenant_rows(db)
    finally:
        await db.close()

    monkeypatch.setattr("app.routers.transactions.call_llm", _make_semantic_mock(navarro_ids))

    target_id = navarro_ids[0]
    test_client.patch(
        f"/api/transactions/{target_id}",
        json={"category_path": "Rental Income", "apply_to_matching": True}
    )

    resp = test_client.get("/api/rules")
    rules_before = resp.json()["rules"]
    assert len(rules_before) >= 1

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_path IS NULL OR category_path = 'Uncategorized'"
        )
        row = await cursor.fetchone()
        uncat_before = row[0] if row else 0
    finally:
        await db.close()

    monkeypatch.setattr("app.services.categorizer.call_llm", _make_mock_llm())

    resp = test_client.post("/api/categorize")
    assert resp.status_code == 200

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_path IS NULL OR category_path = 'Uncategorized'"
        )
        row = await cursor.fetchone()
        uncat_after = row[0] if row else 0
        assert uncat_after == 0, "All transactions should be categorized"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_context_updated_after_correction(test_client, monkeypatch):
    """After correction with apply_to_matching, GET /api/context shows the fact."""
    _import_with_mock(test_client, monkeypatch)

    from app.database import get_db
    db = await get_db()
    try:
        navarro_ids = await _tenant_rows(db)
    finally:
        await db.close()

    monkeypatch.setattr("app.routers.transactions.call_llm", _make_semantic_mock(navarro_ids))

    context_before = test_client.get("/api/context").json()

    target_id = navarro_ids[0]
    test_client.patch(
        f"/api/transactions/{target_id}",
        json={"category_path": "Rental Income", "apply_to_matching": True}
    )

    context_after = test_client.get("/api/context").json()
    assert "NAVARRO" in context_after["content"].upper() or "navarro" in context_after["content"].lower(), \
        "Appended fact should mention NAVARRO"


@pytest.mark.asyncio
async def test_apply_rules_service(test_client, monkeypatch):
    """Service-level test: apply_rules categorizes uncategorized transactions matching the rule."""
    from app.database import get_db
    db = await get_db()

    cursor = await db.execute("SELECT COUNT(*) FROM transactions")
    row = await cursor.fetchone()
    if row[0] == 0:
        monkeypatch.setattr("app.services.categorizer.call_llm", _make_mock_llm())
        test_client.post("/api/import/samples")

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM transactions WHERE LOWER(merchant) LIKE '%netflix%'"
        )
        netflix_ids = [dict(r)["id"] for r in await cursor.fetchall()]
        assert len(netflix_ids) > 0, "Expected Netflix transactions in dataset"

        for nid in netflix_ids:
            await db.execute(
                "UPDATE transactions SET category_path=NULL, entity_type='uncategorized' WHERE id=?",
                (nid,)
            )
        await db.commit()

        # apply_rules runs EVERY rule, and only the first one to match an
        # uncategorized row wins. Earlier tests leave netflix rules behind (a
        # chat-correct recategorize creates one), so drop those first — otherwise
        # this test silently asserts on whichever rule happened to be inserted
        # first, which is how it came to depend on a hierarchical path.
        await db.execute("DELETE FROM rules WHERE LOWER(pattern) LIKE '%netflix%'")
        await db.commit()

        rule_id = await add_rule(db, "netflix", "Subscriptions",
                                 "Netflix subscriptions")
        assert rule_id > 0

        categorized = await apply_rules(db)

        cursor = await db.execute(
            "SELECT category_path FROM transactions WHERE LOWER(merchant) LIKE '%netflix%' AND category_path = 'Subscriptions'"
        )
        rows = await cursor.fetchall()
        assert len(rows) > 0, "Netflix transactions should have been categorized by rule"
        assert len(rows) == len(netflix_ids), "All Netflix transactions should match"
        assert categorized == len(netflix_ids), "apply_rules should return correct count"
    finally:
        await db.close()


def test_determine_entity_type_util():
    assert determine_entity_type("Income > Rentals > Navarro") == "income"
    assert determine_entity_type("Expense > Personal > Groceries") == "expense"
    assert determine_entity_type("Transfer > Account-to-account") == "transfer"
    assert determine_entity_type("") == "expense"
