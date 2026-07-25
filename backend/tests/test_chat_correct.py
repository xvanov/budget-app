"""Tests for Story 42: Chat-style natural-language corrections that update the living context."""

import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.rules_service import list_rules


def _make_mock_llm():
    """Mock categorizer LLM for import (categorizes by amount sign)."""
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
                    "category_path": "Expense > Personal > Other" if is_negative else "Income > Other Income",
                    "entity_type": "expense" if is_negative else "income",
                    "confidence_score": 0.85,
                    "categorization_reason": "Test categorization",
                    "business_context": "personal",
                })
        return json.dumps(results)
    return mock_call_llm


def _make_chat_mock(actions, summary="Chat correction applied"):
    """Return a mock call_llm for the chat-correct endpoint."""
    def mock_call_llm(prompt, **kwargs):
        return json.dumps({
            "summary": summary,
            "actions": actions,
        })
    return mock_call_llm


def _seed_tenant_rows(test_client):
    """One tenant's rent inflows, spelled the way three different rails write it.

    The chat-correct actions below search for this payer by name, so the rows have
    to exist regardless of what happens to be sitting in ``samples/`` — on a fresh
    clone that directory is empty. Idempotent: every test in this module shares one
    database. The check filters on ``merchant`` alone, matching what the assertions
    below query; ``search`` also spans ``description``, so it can report a hit on a
    row these tests would never find.
    """
    found = test_client.get("/api/transactions?merchant=NAVARRO&page_size=1&scope=all")
    assert found.status_code == 200, found.text
    if found.json()["total"]:
        return
    for platform, account, merchant in (
        ("chase", "bank-0001", "TERESA NAVARRO"),
        ("chase", "bank-0001", "Zelle payment to Teresa Navarro"),
        ("cash_app", "cash-app", "Teresa Navarro"),
    ):
        resp = test_client.post("/api/transactions", json={
            "amount_cents": 120000,
            "posted_date": "2026-02-01",
            "merchant": merchant,
            "category_path": "Other Income",
            "source_platform": platform,
            "account_id": account,
            "source_file": "tenant.csv",
        })
        assert resp.status_code == 201, resp.text


def _import_with_mock(test_client, monkeypatch):
    """Import samples with mocked categorizer LLM, plus the tenant fixture rows."""
    monkeypatch.setattr("app.services.categorizer.call_llm", _make_mock_llm())
    resp = test_client.post("/api/import/samples")
    assert resp.status_code == 200
    _seed_tenant_rows(test_client)
    return resp


@pytest.mark.asyncio
async def test_chat_correct_returns_correct_structure(test_client, monkeypatch):
    """POST /api/chat-correct returns {affected_count, affected_ids, summary, context_diff}."""
    _import_with_mock(test_client, monkeypatch)

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {"type": "tag", "params": {"search": "NAVARRO", "tags": ["person:navarro"]}},
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": "tag all NAVARRO transactions with person:navarro"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "affected_count" in data
    assert "affected_ids" in data
    assert "summary" in data
    assert data["summary"] == "Chat correction applied"
    assert data["affected_count"] > 0


@pytest.mark.asyncio
async def test_chat_correct_tags_navarro(test_client, monkeypatch):
    """Chat-correct tags all NAVARRO transactions with person:navarro."""
    _import_with_mock(test_client, monkeypatch)

    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, tags FROM transactions WHERE LOWER(merchant) LIKE '%navarro%'"
        )
        navarro_rows = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()

    assert len(navarro_rows) > 0, "Expected at least one NAVARRO row in dataset"
    navarro_ids = [r["id"] for r in navarro_rows]

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {"type": "tag", "params": {"search": "NAVARRO", "tags": ["person:navarro"]}},
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": "tag all NAVARRO transactions with person:navarro"}
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["affected_count"] == len(navarro_ids)
    assert set(data["affected_ids"]) == set(navarro_ids)

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, tags FROM transactions WHERE LOWER(merchant) LIKE '%navarro%'"
        )
        rows = await cursor.fetchall()
        for r in rows:
            tags = json.loads(r["tags"]) if r["tags"] and r["tags"] != "[]" else []
            assert "person:navarro" in tags, f"Row {r['id']} missing tag"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_chat_correct_recategorizes(test_client, monkeypatch):
    """Chat-correct recategorizes matching transactions."""
    _import_with_mock(test_client, monkeypatch)

    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM transactions WHERE LOWER(merchant) LIKE '%netflix%'"
        )
        netflix_rows = [dict(r)["id"] for r in await cursor.fetchall()]
    finally:
        await db.close()

    assert len(netflix_rows) > 0, "Expected Netflix transactions in dataset"

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {
            "type": "recategorize",
            "params": {
                "search": "Netflix",
                "category_path": "Expense > Personal > Subscriptions"
            }
        },
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": "recategorize Netflix as Subscriptions"}
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["affected_count"] == len(netflix_rows)

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, category_path FROM transactions WHERE LOWER(merchant) LIKE '%netflix%'"
        )
        rows = await cursor.fetchall()
        for r in rows:
            # Flat model: a legacy path from the LLM is coerced on the way in.
            assert r["category_path"] == "Subscriptions", \
                f"Row {r['id']} has wrong category: {r['category_path']}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_chat_correct_context_diff_has_diff(test_client, monkeypatch):
    """Chat-correct with tag action returns context_diff."""
    _import_with_mock(test_client, monkeypatch)

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {"type": "tag", "params": {"search": "NAVARRO", "tags": ["person:navarro"]}},
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": "tag NAVARRO with person:navarro"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["affected_count"] > 0
    assert data["context_diff"] is not None
    assert "Tagged" in data["context_diff"]


@pytest.mark.asyncio
async def test_chat_correct_empty_message_returns_400(test_client, monkeypatch):
    """POST /api/chat-correct with empty message returns 400."""
    resp = test_client.post(
        "/api/chat-correct",
        json={"message": ""}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_chat_correct_creates_rule(test_client, monkeypatch):
    """Chat-correct with recategorize action creates a rule."""
    _import_with_mock(test_client, monkeypatch)

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {
            "type": "recategorize",
            "params": {
                "search": "netflix",
                "category_path": "Expense > Personal > Subscriptions"
            }
        },
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": "recategorize Netflix as Subscriptions"}
    )
    assert resp.status_code == 200

    from app.database import get_db
    db = await get_db()
    try:
        rules = await list_rules(db)
    finally:
        await db.close()

    netflix_rules = [r for r in rules if "netflix" in r["pattern"].lower()]
    assert len(netflix_rules) >= 1
    # Flat model: the rule stores the coerced flat name, never the legacy path.
    assert netflix_rules[0]["category_path"] == "Subscriptions"


@pytest.mark.asyncio
async def test_chat_correct_updates_context(test_client, monkeypatch):
    """Chat-correct with update_context action appends to budget_context.md."""
    _import_with_mock(test_client, monkeypatch)

    context_before = test_client.get("/api/context").json()

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {"type": "update_context", "params": {"fact": "Netflix is a subscription service used by the household."}},
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": "Netflix is a household subscription"}
    )
    assert resp.status_code == 200

    context_after = test_client.get("/api/context").json()
    assert "Netflix" in context_after["content"]
    assert "subscription" in context_after["content"].lower()


@pytest.mark.asyncio
async def test_chat_correct_add_rule_action(test_client, monkeypatch):
    """Chat-correct with add_rule action creates a rule without affecting transactions."""
    _import_with_mock(test_client, monkeypatch)

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {
            "type": "add_rule",
            "params": {
                "pattern": "spectrum",
                "category_path": "Expense > Rental > Utilities",
                "description": "Spectrum bills are rental utilities"
            }
        },
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": "Spectrum is a rental utility"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["affected_count"] == 0
    assert data["context_diff"] is not None
    assert "rule" in data["context_diff"].lower()

    from app.database import get_db
    db = await get_db()
    try:
        rules = await list_rules(db)
    finally:
        await db.close()

    spectrum_rules = [r for r in rules if "spectrum" in r["pattern"].lower()]
    assert len(spectrum_rules) >= 1
    # Flat model: "Expense > Rental > Utilities" is coerced to "Utilities".
    assert spectrum_rules[0]["category_path"] == "Utilities"


# ───────── Workstream A/B: persistence + history ─────────

@pytest.mark.asyncio
async def test_recategorize_persists_in_db_after_request_returns(test_client, monkeypatch):
    """After POST /api/chat-correct returns, the affected rows actually carry
    the new category_path in the DB on a *fresh* connection. Proves db.commit()
    runs, so a separate UI re-fetch will see the change."""
    _import_with_mock(test_client, monkeypatch)

    # The LLM is stubbed to answer a legacy path; chat-correct coerces it to the
    # flat name, so that flat name is what must land in the DB.
    llm_cat = "Expense > Personal > Other"
    new_cat = "Other"

    from app.database import get_db
    db = await get_db()
    try:
        # Force a row to a known starting state we control, then verify the
        # chat-correct flips it to new_cat on the same id.
        cursor = await db.execute(
            "SELECT id FROM transactions WHERE LOWER(merchant) LIKE '%netflix%' "
            "ORDER BY id LIMIT 1"
        )
        target_id = (await cursor.fetchone())["id"]
        await db.execute(
            "UPDATE transactions SET category_path='Entertainment' WHERE id=?",
            (target_id,),
        )
        await db.commit()
    finally:
        await db.close()

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {"type": "recategorize", "params": {"search": "netflix", "category_path": llm_cat}},
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": "recategorize Netflix to Personal Other"}
    )
    assert resp.status_code == 200
    assert resp.json()["affected_count"] >= 1

    # Re-read from a fresh DB connection — proves db.commit() persisted.
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT category_path FROM transactions WHERE id=?",
            (target_id,)
        )
        after = dict(await cursor.fetchone())
    finally:
        await db.close()

    assert after["category_path"] == new_cat, (
        f"Row {target_id} not updated in DB: still {after['category_path']!r} "
        f"after chat-correct (expected {new_cat!r})."
    )


@pytest.mark.asyncio
async def test_chat_correct_inserts_history_row(test_client, monkeypatch):
    """POST /api/chat-correct writes a row into chat_corrections with the
    exact prompt, response summary, and affected_count from this call."""
    _import_with_mock(test_client, monkeypatch)

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {"type": "tag", "params": {"search": "NAVARRO", "tags": ["person:history-test"]}},
    ], summary="History-test summary"))

    prompt = "tag NAVARRO with person:history-test (history test row)"
    resp = test_client.post("/api/chat-correct", json={"message": prompt})
    assert resp.status_code == 200
    affected_from_response = resp.json()["affected_count"]
    assert affected_from_response > 0

    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT prompt, response, affected_count, source FROM chat_corrections "
            "WHERE prompt=? ORDER BY id DESC LIMIT 1",
            (prompt,)
        )
        row = await cursor.fetchone()
    finally:
        await db.close()

    assert row is not None, "no chat_corrections row inserted for this prompt"
    assert row["prompt"] == prompt
    assert row["response"] == "History-test summary"
    assert row["affected_count"] == affected_from_response
    assert row["source"] == "chat-page"  # default when client doesn't pass `source`


@pytest.mark.asyncio
async def test_chat_corrections_get_returns_newest_first(test_client, monkeypatch):
    """GET /api/chat-corrections returns rows newest-first and the most
    recent prompt matches the last POST."""
    _import_with_mock(test_client, monkeypatch)

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {"type": "tag", "params": {"search": "NAVARRO", "tags": ["a"]}},
    ]))
    r1 = test_client.post("/api/chat-correct", json={"message": "FIRST prompt"})
    assert r1.status_code == 200

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {"type": "tag", "params": {"search": "NAVARRO", "tags": ["b"]}},
    ]))
    r2 = test_client.post("/api/chat-correct", json={"message": "SECOND prompt"})
    assert r2.status_code == 200

    list_resp = test_client.get("/api/chat-corrections?limit=10")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) >= 2
    # Newest-first: SECOND must appear before FIRST
    prompts_in_order = [it["prompt"] for it in items]
    idx_first = prompts_in_order.index("FIRST prompt")
    idx_second = prompts_in_order.index("SECOND prompt")
    assert idx_second < idx_first, (
        f"history not newest-first: got {prompts_in_order}"
    )
    # And the first item has parsed affected_ids list
    assert isinstance(items[0]["affected_ids"], list)
    assert items[0]["affected_count"] == len(items[0]["affected_ids"])


@pytest.mark.asyncio
async def test_chat_correct_creates_missing_category(test_client, monkeypatch):
    """Workstream A, flat model: chat-correct must auto-create a brand-new
    category referenced in a create_category action — as ONE flat row. A
    hierarchical answer from the LLM is coerced to its leaf name, and no parent
    rows are invented (that is what put ``Expense`` and ``Expense > Groceries``
    into the live DB)."""
    _import_with_mock(test_client, monkeypatch)

    # A unique throwaway name that no seed / prior test creates.
    llm_path = "Expense > AutoTest_Workstream_A > New_Subleaf"
    flat_name = "New_Subleaf"

    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM categories WHERE path=? OR path=?", (llm_path, flat_name)
        )
        assert await cursor.fetchone() is None, "test precondition: category exists"
    finally:
        await db.close()

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {"type": "create_category", "params": {"category_path": llm_path}},
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": f"please create {flat_name}"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["affected_count"] == 0  # no transactions touched
    assert data["context_diff"] is not None
    assert f"Ensured category exists: {flat_name}" in data["context_diff"]

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, parent_id, path, name FROM categories WHERE name=? OR path=?",
            (flat_name, flat_name),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        assert len(rows) == 1, f"expected one flat row, got {rows}"
        assert rows[0]["parent_id"] is None
        assert rows[0]["path"] == flat_name == rows[0]["name"]

        # No hierarchy invented on the way.
        for junk in (llm_path, "Expense > AutoTest_Workstream_A"):
            cursor = await db.execute("SELECT id FROM categories WHERE path=?", (junk,))
            assert await cursor.fetchone() is None, f"hierarchical row {junk!r} created"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_chat_correct_recategorize_auto_creates_new_category(test_client, monkeypatch):
    """Workstream A, flat model: a recategorize referencing a category that does
    not exist yet creates it as a flat row AND files the matched rows into it."""
    _import_with_mock(test_client, monkeypatch)

    llm_path = "Expense > AutoTest_Recat_NewBucket > Leaf_Bucket"
    flat_name = "Leaf_Bucket"

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {
            "type": "recategorize",
            "params": {"search": "netflix", "category_path": llm_path},
        },
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": f"put Netflix into {flat_name}"},
    )
    assert resp.status_code == 200
    assert resp.json()["affected_count"] >= 1

    from app.database import get_db
    db = await get_db()
    try:
        row = await (await db.execute(
            "SELECT id, parent_id FROM categories WHERE path=?", (flat_name,)
        )).fetchone()
        assert row is not None, "new category was not auto-created during recategorize"
        assert row["parent_id"] is None

        cursor = await db.execute(
            "SELECT category_path FROM transactions WHERE LOWER(merchant) LIKE '%netflix%'"
        )
        rows = await cursor.fetchall()
        for r in rows:
            assert r["category_path"] == flat_name, (
                f"netflix row category not migrated to {flat_name}: got {r['category_path']!r}"
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_apply_rules_matches_description_not_just_merchant(test_client, monkeypatch):
    """Workstream C: ``apply_rules`` must match patterns against description
    too, not just merchant. EXAMPLE EMPLOYER paycheck rows have merchant='EXAMPLE EMPLOYER INC'
    and the distinguishing string ('Federal Income Tax') lives in description."""
    _import_with_mock(test_client, monkeypatch)

    from app.database import get_db
    from app.routers.categories import ensure_category_path
    from app.services.rules_service import apply_rules

    db = await get_db()
    try:
        # Seed a transaction whose merchant doesn't contain the pattern but
        # whose description does. Force category_path=Uncategorized so the
        # rule can act on it.
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date,
                amount_cents, merchant, description, category_path, entity_type)
               VALUES ('test','t','t','2026-01-01', -12345, 'EXAMPLE EMPLOYER INC',
                       'Federal Income Tax: 2026-01-01 to 2026-01-15',
                       'Uncategorized', 'uncategorized')""",
        )
        new_id = cursor.lastrowid
        # Pre-create the target path & a description-only rule.
        await ensure_category_path(db, "Expense > Tax > Federal Income Tax")
        await db.execute(
            "INSERT INTO rules (pattern, category_path, description) VALUES (?, ?, ?)",
            ("federal income tax", "Expense > Tax > Federal Income Tax",
             "apply_rules description-match test"),
        )
        await db.commit()

        await apply_rules(db)

        row = await (await db.execute(
            "SELECT category_path, entity_type FROM transactions WHERE id=?",
            (new_id,),
        )).fetchone()
        assert row["category_path"] == "Expense > Tax > Federal Income Tax", (
            f"apply_rules failed to fire on description-only match: "
            f"got category_path={row['category_path']!r}"
        )
        assert row["entity_type"] == "expense"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_chat_correct_source_persisted_from_request(test_client, monkeypatch):
    """The floating widget passes source='floating-widget'; that value must
    persist into chat_corrections so the Chat-Correct tab can show where the
    correction came from."""
    _import_with_mock(test_client, monkeypatch)

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _make_chat_mock([
        {"type": "tag", "params": {"search": "NAVARRO", "tags": ["from-widget"]}},
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": "tag from the floating widget", "source": "floating-widget"}
    )
    assert resp.status_code == 200

    list_resp = test_client.get("/api/chat-corrections?limit=5")
    items = list_resp.json()["items"]
    matches = [it for it in items if it["prompt"] == "tag from the floating widget"]
    assert len(matches) == 1
    assert matches[0]["source"] == "floating-widget"
