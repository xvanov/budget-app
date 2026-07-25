"""Tests for Story 09: Manual transaction entry."""

import pytest


def test_create_manual_transaction(test_client):
    """POST /api/transactions creates a manual row with source_platform='manual'."""
    res = test_client.post("/api/transactions", json={
        "amount_cents": -150000,
        "posted_date": "2026-05-15",
        "merchant": "Cash rent payment",
        "category_path": "Expense > Personal > Other",
        "business_context": "personal",
        "notes": "Paid in cash",
    })
    assert res.status_code == 201
    data = res.json()
    assert data["source_platform"] == "manual"
    assert data["source_file"] == "manual"
    assert data["amount_cents"] == -150000
    assert data["posted_date"] == "2026-05-15"
    assert data["merchant"] == "Cash rent payment"
    assert data["category_path"] == "Expense > Personal > Other"
    assert data["entity_type"] == "expense"
    assert data["business_context"] == "personal"
    assert data["notes"] == "Paid in cash"
    assert data["confidence_score"] == 1.0
    assert data["categorization_reason"] == "Manual entry"


def test_create_manual_income(test_client):
    """Manual income rows get entity_type='income'."""
    res = test_client.post("/api/transactions", json={
        "amount_cents": 500000,
        "posted_date": "2026-05-14",
        "merchant": "Cash rental income",
        "category_path": "Income > Rentals",
    })
    assert res.status_code == 201
    data = res.json()
    assert data["entity_type"] == "income"
    assert data["source_platform"] == "manual"


def test_default_values(test_client):
    """Defaults for optional fields are set correctly."""
    res = test_client.post("/api/transactions", json={
        "amount_cents": -5000,
        "posted_date": "2026-05-13",
    })
    assert res.status_code == 201
    data = res.json()
    assert data["merchant"] == ""
    assert data["description"] == ""
    assert data["business_context"] == "personal"
    assert data["account_id"] == "manual"
    assert data["entity_type"] == "expense"
    assert data["notes"] is None


def test_create_manual_transfer(test_client):
    """Manual transfer rows get entity_type='transfer'."""
    res = test_client.post("/api/transactions", json={
        "amount_cents": -100000,
        "posted_date": "2026-05-12",
        "merchant": "Cash deposit",
        "category_path": "Transfer > Account-to-account",
    })
    assert res.status_code == 201
    data = res.json()
    assert data["entity_type"] == "transfer"


def test_manual_row_in_dashboard(test_client):
    """Manual rows participate in dashboard aggregations."""
    res = test_client.post("/api/transactions", json={
        "amount_cents": 200000,
        "posted_date": "2026-05-10",
        "merchant": "Cash income",
        "category_path": "Income > Other Income",
    })
    assert res.status_code == 201

    res = test_client.post("/api/transactions", json={
        "amount_cents": -50000,
        "posted_date": "2026-05-10",
        "merchant": "Cash expense",
        "category_path": "Expense > Personal > Other",
    })
    assert res.status_code == 201

    res = test_client.get("/api/dashboard?start_date=2026-05-01&end_date=2026-05-31")
    assert res.status_code == 200
    data = res.json()
    assert data["totals"]["total_income_cents"] >= 200000
    assert data["totals"]["total_expense_cents"] >= 50000
    assert data["totals"]["net_cents"] == data["totals"]["total_income_cents"] - data["totals"]["total_expense_cents"]


def test_manual_rows_listed_in_transactions(test_client):
    """Manual rows appear in GET /api/transactions."""
    test_client.post("/api/transactions", json={
        "amount_cents": -7500,
        "posted_date": "2026-05-09",
        "merchant": "Coffee cash",
    })
    res = test_client.get("/api/transactions?page_size=100")
    assert res.status_code == 200
    data = res.json()
    manual_txns = [t for t in data["transactions"] if t["source_platform"] == "manual"]
    assert len(manual_txns) >= 1
    match = [t for t in manual_txns if t["merchant"] == "Coffee cash"]
    assert len(match) == 1


def test_manual_row_survives_reimport(test_client):
    """Manual rows are NOT dedup'd away by file re-imports."""
    test_client.post("/api/transactions", json={
        "amount_cents": -2500,
        "posted_date": "2026-05-08",
        "merchant": "Manual unique entry",
    })
    res = test_client.get("/api/transactions?page_size=1000")
    assert res.status_code == 200
    count_before = len([t for t in res.json()["transactions"] if t["source_platform"] == "manual"])

    res = test_client.get("/api/transactions?page_size=1000")
    assert res.status_code == 200
    count_after = len([t for t in res.json()["transactions"] if t["source_platform"] == "manual"])
    assert count_after == count_before


def test_create_no_date_error(test_client):
    """Missing required posted_date causes validation error."""
    res = test_client.post("/api/transactions", json={
        "amount_cents": -5000,
    })
    assert res.status_code == 422


def test_create_no_amount_error(test_client):
    """Missing required amount_cents causes validation error."""
    res = test_client.post("/api/transactions", json={
        "posted_date": "2026-05-07",
    })
    assert res.status_code == 422
