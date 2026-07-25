"""Tests for Story 20: Budget allocations — absolute amounts OR percent-of-income, per period."""

import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import SEED_CATEGORIES


def _make_mock_llm():
    def mock_call_llm(prompt, **kwargs):
        import re
        results = []
        for line in prompt.split('\n'):
            m = re.match(r'\s+ID=(\d+):', line)
            if m:
                txn_id = int(m.group(1))
                is_negative = '$-' in line
                results.append({
                    "id": txn_id,
                    "category_path": "Expense > Personal > Groceries" if is_negative else "Income > Other Income",
                    "entity_type": "expense" if is_negative else "income",
                    "confidence_score": 0.85,
                    "categorization_reason": "Test categorization",
                    "business_context": "personal",
                })
        return json.dumps(results)
    return mock_call_llm


def _import_with_mock(test_client, monkeypatch):
    monkeypatch.setattr("app.services.categorizer.call_llm", _make_mock_llm())
    resp = test_client.post("/api/import/samples")
    assert resp.status_code == 200
    return resp


@pytest.fixture
def imported_data(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    return test_client


class TestBudgetsCRUD:

    def test_create_absolute_budget(self, test_client):
        resp = test_client.post("/api/budgets", json={
            "category_path": "Expense > Personal > Groceries",
            "budget_type": "absolute",
            "amount_cents": 140000,
            "period": "month",
            "label": "CRUD_test_groceries",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["category_path"] == "Expense > Personal > Groceries"
        assert data["budget_type"] == "absolute"
        assert data["amount_cents"] == 140000
        assert data["period"] == "month"
        assert data["label"] == "CRUD_test_groceries"

    def test_create_percent_budget(self, test_client):
        resp = test_client.post("/api/budgets", json={
            "category_path": "Expense > Leisure",
            "budget_type": "percent_of_income",
            "percent": 10.0,
            "period": "month",
            "label": "CRUD_test_leisure",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["budget_type"] == "percent_of_income"
        assert data["percent"] == 10.0

    def test_create_budget_invalid_type(self, test_client):
        resp = test_client.post("/api/budgets", json={
            "category_path": "Expense > Personal > Groceries",
            "budget_type": "invalid",
            "amount_cents": 1000,
        })
        assert resp.status_code == 400

    def test_create_budget_missing_amount(self, test_client):
        resp = test_client.post("/api/budgets", json={
            "category_path": "Expense > Personal > Groceries",
            "budget_type": "absolute",
        })
        assert resp.status_code == 400

    def test_create_budget_missing_percent(self, test_client):
        resp = test_client.post("/api/budgets", json={
            "category_path": "Expense > Personal > Groceries",
            "budget_type": "percent_of_income",
        })
        assert resp.status_code == 400

    def test_list_budgets(self, test_client):
        resp = test_client.get("/api/budgets")
        assert resp.status_code == 200
        data = resp.json()
        assert "budgets" in data

    def test_get_budget_not_found(self, test_client):
        resp = test_client.get("/api/budgets/99999")
        assert resp.status_code == 404

    def test_create_then_get(self, test_client):
        create_resp = test_client.post("/api/budgets", json={
            "category_path": "Expense > Personal > Groceries",
            "budget_type": "absolute",
            "amount_cents": 140000,
            "period": "month",
            "label": "create_then_get_test",
        })
        budget_id = create_resp.json()["id"]
        resp = test_client.get(f"/api/budgets/{budget_id}")
        assert resp.status_code == 200
        assert resp.json()["label"] == "create_then_get_test"

    def test_update_budget(self, test_client):
        create_resp = test_client.post("/api/budgets", json={
            "category_path": "Expense > Personal > Groceries",
            "budget_type": "absolute",
            "amount_cents": 140000,
            "period": "month",
            "label": "update_test",
        })
        budget_id = create_resp.json()["id"]
        resp = test_client.put(f"/api/budgets/{budget_id}", json={"amount_cents": 160000})
        assert resp.status_code == 200
        assert resp.json()["amount_cents"] == 160000

    def test_delete_budget(self, test_client):
        create_resp = test_client.post("/api/budgets", json={
            "category_path": "Expense > Personal > Groceries",
            "budget_type": "absolute",
            "amount_cents": 140000,
            "label": "delete_test",
        })
        budget_id = create_resp.json()["id"]
        resp = test_client.delete(f"/api/budgets/{budget_id}")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        resp = test_client.get(f"/api/budgets/{budget_id}")
        assert resp.status_code == 404


class TestBudgetStatusFields:
    """Status endpoint returns correct computed fields."""

    def test_status_returns_expected_fields(self, imported_data):
        imported_data.post("/api/budgets", json={
            "category_path": "Expense > Personal > Groceries",
            "budget_type": "absolute",
            "amount_cents": 200000,
            "period": "month",
            "label": "field_test",
        })
        resp = imported_data.get("/api/budgets/status?period_start=2026-01-01&period_end=2026-01-31")
        assert resp.status_code == 200
        budgets = [b for b in resp.json()["budgets"] if b["label"] == "field_test"]
        assert len(budgets) == 1
        b = budgets[0]
        assert "id" in b
        assert "category_path" in b
        assert "target_cents" in b
        assert "spent_cents" in b
        assert "pct_used" in b
        assert "color" in b
        assert "forecast_overrun_cents" in b
        assert "expected_pct" in b
        assert b["color"] in ("green", "yellow", "red")
        assert b["spent_cents"] > 0

    def test_status_with_period_start_end(self, imported_data):
        imported_data.post("/api/budgets", json={
            "category_path": "Expense > Personal > Groceries",
            "budget_type": "absolute",
            "amount_cents": 200000,
            "period": "month",
            "label": "period_test",
        })
        resp = imported_data.get("/api/budgets/status?period_start=2026-01-01&period_end=2026-01-31")
        assert resp.status_code == 200
        budgets = [b for b in resp.json()["budgets"] if b["label"] == "period_test"]
        assert len(budgets) == 1
        b = budgets[0]
        assert b["period_start"] == "2026-01-01"
        assert b["period_end"] == "2026-01-31"

    def test_color_green_low_usage(self, imported_data):
        imported_data.post("/api/budgets", json={
            "category_path": "Expense > Personal > Subscriptions",
            "budget_type": "absolute",
            "amount_cents": 99999999,
            "period": "month",
            "label": "green_test",
        })
        resp = imported_data.get("/api/budgets/status?period_start=2000-01-01&period_end=2000-01-31")
        budgets = [b for b in resp.json()["budgets"] if b["label"] == "green_test"]
        assert len(budgets) == 1
        assert budgets[0]["color"] == "green"
        assert budgets[0]["spent_cents"] == 0

    def test_color_red_over_budget(self, imported_data):
        imported_data.post("/api/budgets", json={
            "category_path": "Expense > Personal > Groceries",
            "budget_type": "absolute",
            "amount_cents": 100,
            "period": "month",
            "label": "red_test",
        })
        resp = imported_data.get("/api/budgets/status?period_start=2026-01-01&period_end=2026-01-31")
        budgets = [b for b in resp.json()["budgets"] if b["label"] == "red_test"]
        assert len(budgets) == 1
        b = budgets[0]
        assert b["color"] == "red"
        assert b["pct_used"] > 100
        assert b["forecast_overrun_cents"] > 0

    def test_percent_of_income_recomputes_target(self, imported_data):
        imported_data.post("/api/budgets", json={
            "category_path": "Expense > Leisure",
            "budget_type": "percent_of_income",
            "percent": 10.0,
            "period": "month",
            "label": "pct_income_test",
        })
        resp = imported_data.get("/api/budgets/status?period_start=2026-01-01&period_end=2026-01-31")
        assert resp.status_code == 200
        budgets = [b for b in resp.json()["budgets"] if b["label"] == "pct_income_test"]
        assert len(budgets) == 1
        b = budgets[0]
        assert b["total_income_cents"] > 0
        expected_target = int(b["total_income_cents"] * 10 / 100.0)
        assert abs(b["target_cents"] - expected_target) <= 1

    def test_forecast_overrun_cents(self, imported_data):
        imported_data.post("/api/budgets", json={
            "category_path": "Expense > Personal > Groceries",
            "budget_type": "absolute",
            "amount_cents": 100,
            "period": "month",
            "label": "overrun_test",
        })
        resp = imported_data.get("/api/budgets/status?period_start=2026-01-01&period_end=2026-01-31")
        budgets = [b for b in resp.json()["budgets"] if b["label"] == "overrun_test"]
        assert len(budgets) == 1
        assert budgets[0]["forecast_overrun_cents"] > 0
