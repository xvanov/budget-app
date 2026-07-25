"""Tests for Story 10: Every categorization exposes confidence, reasoning, and applied rules."""

import os
import sys
import json

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _mock_call_llm():
    def mock(prompt, **kwargs):
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
    return mock


def test_every_row_has_confidence_and_reason(test_client, monkeypatch):
    """Acceptance: every row has non-empty categorization_reason and a confidence_score in [0, 1]."""
    monkeypatch.setattr("app.services.categorizer.call_llm", _mock_call_llm())
    resp = test_client.post("/api/import/samples")
    assert resp.status_code == 200

    page = 1
    page_size = 500
    bad = []
    while True:
        r = test_client.get(f"/api/transactions?page={page}&page_size={page_size}&scope=all")
        data = r.json()
        for t in data.get("transactions", []):
            conf = t.get("confidence_score")
            reason = t.get("categorization_reason")
            if conf is None or conf < 0 or conf > 1:
                bad.append((t["id"], "bad confidence", conf))
            if not reason:
                bad.append((t["id"], "missing reason", reason))
        if page * page_size >= data.get("total", 0):
            break
        page += 1
    assert not bad, f"Rows missing confidence/reason: {bad[:5]} (total {len(bad)})"


def test_paystub_components_have_explainability(test_client, monkeypatch):
    """Paystub component rows must have confidence=1.0 and a non-empty reason."""
    monkeypatch.setattr("app.services.categorizer.call_llm", _mock_call_llm())
    test_client.post("/api/import/samples")

    r = test_client.get("/api/transactions?page_size=500&scope=all")
    txns = r.json()["transactions"]
    paystub_rows = [t for t in txns if t.get("source_platform") == "paystub"]
    assert len(paystub_rows) > 0, "Expected some paystub rows"
    for t in paystub_rows:
        assert t.get("confidence_score") == 1.0, f"Paystub row {t['id']} confidence={t.get('confidence_score')}"
        reason = t.get("categorization_reason") or ""
        assert "Paystub" in reason, f"Paystub row {t['id']} reason={reason!r}"


@pytest.mark.asyncio
async def test_get_rule_returns_match_pattern_count_and_timestamp(test_client, monkeypatch):
    """Acceptance: GET /api/rules/{id} returns rule details with match pattern, affected count, and creation timestamp.

    Async rather than sync-with-run_until_complete: pytest-asyncio owns the event
    loop per async test, so a sync test grabbing ``get_event_loop()`` picks up a
    loop that a previous async test already closed. That made this pass alone and
    fail in a full-suite ordering — a test-infrastructure bug, not a product one.
    """
    from app.database import get_db
    from app.services.rules_service import add_rule

    db = await get_db()
    try:
        rule_id = await add_rule(db, "navarro", "Rental Income", "NAVARRO tenant rental income")
    finally:
        await db.close()

    r = test_client.get(f"/api/rules/{rule_id}")
    assert r.status_code == 200
    rule = r.json()
    assert rule["pattern"] == "navarro"
    assert rule["category_path"] == "Rental Income"
    assert rule["description"] == "NAVARRO tenant rental income"
    assert "created_at" in rule and rule["created_at"]
    assert "affected_count" in rule
    assert isinstance(rule["affected_count"], int)

    # 404 for unknown rule
    r2 = test_client.get("/api/rules/99999")
    assert r2.status_code == 404


@pytest.mark.asyncio
async def test_backfill_explainability_fills_existing_rows(test_client, monkeypatch):
    """The startup backfill must populate any pre-existing null rows.

    Async for the same reason as the rule test above: a sync test driving the
    event loop by hand breaks once any async test has run before it.
    """
    from app.database import get_db, backfill_explainability

    monkeypatch.setattr("app.services.categorizer.call_llm", _mock_call_llm())
    test_client.post("/api/import/samples")

    async def break_and_fix():
        db = await get_db()
        try:
            await db.execute("UPDATE transactions SET confidence_score=NULL, categorization_reason=NULL WHERE id IN (SELECT id FROM transactions LIMIT 3)")
            await db.commit()
            cursor = await db.execute("SELECT COUNT(*) FROM transactions WHERE confidence_score IS NULL OR categorization_reason IS NULL OR categorization_reason = ''")
            before = (await cursor.fetchone())[0]
            assert before >= 3
        finally:
            await db.close()
        updated = await backfill_explainability()
        db = await get_db()
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM transactions WHERE confidence_score IS NULL OR categorization_reason IS NULL OR categorization_reason = ''")
            after = (await cursor.fetchone())[0]
        finally:
            await db.close()
        return updated, after

    updated, after = await break_and_fix()
    assert updated >= 3
    assert after == 0
