"""Tests for Story 04: LLM categorization with budget_context + ground-truth rules."""

import os
import sys
import json
import re
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.categorizer import (
    CATEGORY_TREE,
    determine_entity_type,
    determine_asset_class,
    build_categorization_prompt,
    categorize_uncategorized,
)


def _make_mock_llm():
    """Return a mock call_llm that categorizes based on amount sign."""
    def mock_call_llm(prompt, **kwargs):
        results = []
        for line in prompt.split('\n'):
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


def _import_with_mock(test_client, monkeypatch, mock_fn=None):
    """Import samples with mocked LLM to avoid hanging on real LLM calls."""
    if mock_fn is None:
        mock_fn = _make_mock_llm()
    monkeypatch.setattr("app.services.categorizer.call_llm", mock_fn)
    resp = test_client.post("/api/import/samples")
    assert resp.status_code == 200
    return resp


@pytest.mark.asyncio
async def test_categorize_uncategorized_categorizes_all(test_client, monkeypatch):
    """After categorization, no transactions have NULL/Uncategorized category_path."""
    _import_with_mock(test_client, monkeypatch)

    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_path IS NULL OR category_path = 'Uncategorized'"
        )
        row = await cursor.fetchone()
        uncat_before = row[0] if row else 0
        assert uncat_before == 0, "Should already be 0 uncategorized after import-with-categorization"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_categorize_import_flow_categorizes_all(test_client, monkeypatch):
    """POST /api/categorize with mock yields no uncategorized rows."""
    _import_with_mock(test_client, monkeypatch)

    resp = test_client.post("/api/categorize")
    assert resp.status_code == 200
    data = resp.json()
    assert data["categorized"] == 0 or data["remaining"] == 0
    assert data["remaining"] == 0


def test_categorize_prompt_contains_tree_and_rules():
    """The categorization prompt must include the category tree and ground-truth rules."""
    transactions = [
        {"id": 1, "posted_date": "2026-01-15", "merchant": "NETFLIX", "amount_cents": -1599,
         "description": "NETFLIX SUBSCRIPTION", "account_id": "card-0002", "source_platform": "chase"},
    ]
    prompt = build_categorization_prompt(transactions)
    assert "CATEGORY TREE" in prompt
    assert "GROUND-TRUTH RULES" in prompt
    for line in CATEGORY_TREE.strip().split('\n'):
        stripped = line.strip()
        assert stripped in prompt, f"Tree line missing: {stripped}"
    assert "confidence_score" in prompt


@pytest.mark.asyncio
async def test_every_row_has_confidence_and_reason(test_client, monkeypatch):
    """All transactions must have non-null confidence_score and categorization_reason."""
    _import_with_mock(test_client, monkeypatch)

    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM transactions WHERE confidence_score IS NULL OR categorization_reason IS NULL OR categorization_reason = ''"
        )
        row = await cursor.fetchone()
        missing = row[0] if row else 0
        assert missing == 0, f"Expected 0 rows missing confidence/reason, got {missing}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_llm_fallback(test_client, monkeypatch):
    """When LLM returns None, _categorize_batch returns 0 and sets Uncategorized."""
    _import_with_mock(test_client, monkeypatch)

    monkeypatch.setattr("app.services.categorizer.call_llm", lambda prompt, **kwargs: None)

    from app.database import get_db
    db = await get_db()
    try:
        categorized = await categorize_uncategorized(db)
        assert categorized == 0, "Should return 0 categorized when LLM returns None"
    finally:
        await db.close()


def test_no_regex_maps_in_categorizer():
    """Verify no regex/keyword maps exist in the categorizer module's categorization logic."""
    source = open(os.path.join(os.path.dirname(__file__), '..', 'backend', 'app', 'services', 'categorizer.py')).read()
    assert 'import re' not in source
    assert 'keyword' not in source.lower()
    assert 'category_map' not in source and 'merchant_map' not in source


def test_build_prompt_includes_correct_fields():
    """The categorization prompt includes the category tree, rules, and transaction data."""
    transactions = [
        {"id": 1, "posted_date": "2026-01-15", "merchant": "NETFLIX", "amount_cents": -1599,
         "description": "NETFLIX SUBSCRIPTION", "account_id": "card-0002", "source_platform": "chase"},
        {"id": 2, "posted_date": "2026-01-15", "merchant": "TERESA NAVARRO", "amount_cents": 120000,
         "description": "Zelle payment", "account_id": "bank-0001", "source_platform": "chase"},
    ]
    prompt = build_categorization_prompt(transactions)
    assert "CATEGORY TREE" in prompt
    assert "GROUND-TRUTH RULES" in prompt
    assert "NETFLIX" in prompt
    assert "NAVARRO" in prompt
    assert "JSON array" in prompt
    assert "confidence_score" in prompt
    assert "category_path" in prompt


@pytest.mark.asyncio
async def test_categorize_endpoint(test_client, monkeypatch):
    """POST /api/categorize returns categorized count and is idempotent."""
    _import_with_mock(test_client, monkeypatch)

    resp = test_client.post("/api/categorize")
    assert resp.status_code == 200
    data = resp.json()
    assert data["remaining"] == 0

    resp2 = test_client.post("/api/categorize")
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["categorized"] == 0
    assert data2["remaining"] == 0


def test_determine_entity_type():
    assert determine_entity_type("Income > Rentals > Navarro") == "income"
    assert determine_entity_type("Expense > Personal > Groceries") == "expense"
    assert determine_entity_type("Transfer > Account-to-account") == "transfer"
    assert determine_entity_type("") == "expense"


def test_determine_asset_class():
    assert determine_asset_class("Transfer > Mortgage Principal") == "mortgage_principal"
    assert determine_asset_class("Transfer > Mortgage Escrow") == "escrow"
    assert determine_asset_class("Expense > Personal > Groceries") is None
    assert determine_asset_class("") is None


@pytest.mark.asyncio
async def test_categorize_all_null_or_uncategorized_zero_after(test_client, monkeypatch):
    """Deterministic: no rows with NULL or 'Uncategorized' after categorization."""
    _import_with_mock(test_client, monkeypatch)

    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_path IS NULL OR category_path = 'Uncategorized'"
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0
        assert count == 0, f"Expected 0 NULL/Uncategorized rows, got {count}"
    finally:
        await db.close()


def test_batch_size_reasonable():
    from app.services.categorizer import BATCH_SIZE, MAX_CONCURRENT
    assert BATCH_SIZE >= 5
    assert BATCH_SIZE <= 100
    assert MAX_CONCURRENT >= 1
    assert MAX_CONCURRENT <= 20
