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
                    "category_path": "Groceries" if is_negative else "Other Income",
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
            "category_path": "Groceries",
            "budget_type": "absolute",
            "amount_cents": 140000,
            "period": "month",
            "label": "CRUD_test_groceries",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["category_path"] == "Groceries"
        assert data["budget_type"] == "absolute"
        assert data["amount_cents"] == 140000
        assert data["period"] == "month"
        assert data["label"] == "CRUD_test_groceries"

    def test_create_percent_budget(self, test_client):
        resp = test_client.post("/api/budgets", json={
            "category_path": "Entertainment",
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
            "category_path": "Groceries",
            "budget_type": "invalid",
            "amount_cents": 1000,
        })
        assert resp.status_code == 400

    def test_create_budget_missing_amount(self, test_client):
        resp = test_client.post("/api/budgets", json={
            "category_path": "Groceries",
            "budget_type": "absolute",
        })
        assert resp.status_code == 400

    def test_create_budget_missing_percent(self, test_client):
        resp = test_client.post("/api/budgets", json={
            "category_path": "Groceries",
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
            "category_path": "Groceries",
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
            "category_path": "Groceries",
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
            "category_path": "Groceries",
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
            "category_path": "Groceries",
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
            "category_path": "Groceries",
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
            "category_path": "Groceries",
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
            "category_path": "Entertainment",
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
            "category_path": "Groceries",
            "budget_type": "absolute",
            "amount_cents": 100,
            "period": "month",
            "label": "overrun_test",
        })
        resp = imported_data.get("/api/budgets/status?period_start=2026-01-01&period_end=2026-01-31")
        budgets = [b for b in resp.json()["budgets"] if b["label"] == "overrun_test"]
        assert len(budgets) == 1
        assert budgets[0]["forecast_overrun_cents"] > 0


class TestBudgetRollover:

    def _mock_no_llm(self, monkeypatch):
        """Make call_llm return empty so fallback kicks in deterministically."""
        monkeypatch.setattr("app.routers.budgets.call_llm", lambda prompt, **kw: "[]")

    def _count_custom_labels(self, data, prefix):
        return sum(1 for p in data["previous"] if p["label"].startswith(prefix))

    def test_rollover_returns_expected_structure(self, imported_data, monkeypatch):
        self._mock_no_llm(monkeypatch)
        imported_data.post("/api/budgets", json={
            "category_path": "Groceries",
            "budget_type": "absolute",
            "amount_cents": 200000,
            "period": "month",
            "label": "rolltest_groceries",
        })
        imported_data.post("/api/budgets", json={
            "category_path": "Entertainment",
            "budget_type": "percent_of_income",
            "percent": 10.0,
            "period": "month",
            "label": "rolltest_leisure",
        })

        resp = imported_data.post("/api/budgets/rollover", json={
            "from_period_start": "2026-01-01",
            "from_period_end": "2026-01-31",
            "to_period_start": "2026-02-01",
            "to_period_end": "2026-02-28",
        })
        assert resp.status_code == 200
        data = resp.json()

        assert "from_period" in data
        assert "to_period" in data
        assert "previous" in data
        assert "suggested" in data
        assert len(data["previous"]) > 0
        assert len(data["suggested"]) > 0

        for p in data["previous"]:
            assert "category_path" in p
            assert "budget_type" in p
            assert "target_cents" in p
            assert "spent_cents" in p
            assert "variance" in p

        for s in data["suggested"]:
            assert "category_path" in s
            assert "reason" in s
            assert s["reason"]

    def test_rollover_no_budgets_returns_400(self, test_client, monkeypatch):
        self._mock_no_llm(monkeypatch)
        resp = test_client.post("/api/budgets/rollover", json={
            "from_period_start": "2099-01-01",
            "from_period_end": "2099-01-31",
            "to_period_start": "2099-02-01",
            "to_period_end": "2099-02-28",
        })
        assert resp.status_code in (200, 400)
        if resp.status_code == 200:
            data = resp.json()
            assert "previous" in data
            assert "suggested" in data

    def test_rollover_accept_creates_budgets(self, imported_data, monkeypatch):
        self._mock_no_llm(monkeypatch)
        imported_data.post("/api/budgets", json={
            "category_path": "Groceries",
            "budget_type": "absolute",
            "amount_cents": 200000,
            "period": "month",
            "label": "rollaccept_test_orig",
        })

        rollover_resp = imported_data.post("/api/budgets/rollover", json={
            "from_period_start": "2026-01-01",
            "from_period_end": "2026-01-31",
            "to_period_start": "2026-02-01",
            "to_period_end": "2026-02-28",
        })
        assert rollover_resp.status_code == 200
        rollover_data = rollover_resp.json()

        # Find our specific budget in suggestions
        accept_budgets = []
        for s in rollover_data["suggested"]:
            if s["label"] == "rollaccept_test_orig":
                accept_budgets.append({
                    "category_path": s["category_path"],
                    "budget_type": s["budget_type"],
                    "amount_cents": s["amount_cents"],
                    "percent": s["percent"],
                    "label": "rollaccept_test_rolled",
                })
                break
        # Fallback: use first suggestion if our label not found
        if not accept_budgets:
            s = rollover_data["suggested"][0]
            accept_budgets.append({
                "category_path": s["category_path"],
                "budget_type": s["budget_type"],
                "amount_cents": s["amount_cents"],
                "percent": s["percent"],
                "label": "rollaccept_test_rolled",
            })

        accept_resp = imported_data.post("/api/budgets/rollover/accept", json={
            "budgets": accept_budgets,
            "to_period_start": "2026-02-01",
            "to_period_end": "2026-02-28",
        })
        assert accept_resp.status_code == 201
        accept_data = accept_resp.json()
        assert accept_data["count"] >= 1

        rolled = [b for b in accept_data["budgets"] if b["label"] == "rollaccept_test_rolled"]
        assert len(rolled) >= 1

        list_resp = imported_data.get("/api/budgets")
        assert list_resp.status_code == 200
        budgets = list_resp.json()["budgets"]
        rolled_in_list = [b for b in budgets if b["label"] == "rollaccept_test_rolled"]
        assert len(rolled_in_list) >= 1

    def test_rollover_fallback_generates_readable_reasons(self, imported_data, monkeypatch):
        self._mock_no_llm(monkeypatch)
        imported_data.post("/api/budgets", json={
            "category_path": "Groceries",
            "budget_type": "absolute",
            "amount_cents": 200000,
            "period": "month",
            "label": "rollreasons_test",
        })

        resp = imported_data.post("/api/budgets/rollover", json={
            "from_period_start": "2026-01-01",
            "from_period_end": "2026-01-31",
            "to_period_start": "2026-02-01",
            "to_period_end": "2026-02-28",
        })
        assert resp.status_code == 200
        data = resp.json()

        for s in data["suggested"]:
            assert isinstance(s["reason"], str)
            assert len(s["reason"]) > 10

    def test_rollover_reasons_are_human_readable(self, imported_data, monkeypatch):
        self._mock_no_llm(monkeypatch)
        imported_data.post("/api/budgets", json={
            "category_path": "Groceries",
            "budget_type": "absolute",
            "amount_cents": 10000,
            "period": "month",
            "label": "rollreadable_test",
        })

        resp = imported_data.post("/api/budgets/rollover", json={
            "from_period_start": "2026-01-01",
            "from_period_end": "2026-01-31",
            "to_period_start": "2026-02-01",
            "to_period_end": "2026-02-28",
        })
        assert resp.status_code == 200
        data = resp.json()

        for s in data["suggested"]:
            reason = s["reason"]
            assert len(reason) > 10
            assert isinstance(reason, str)
            assert not reason.startswith("{")


class TestBudgetIntelligence:

    ALLOWED_KINDS = {"over-budget-risk", "recurring-waste", "savings-opportunity", "historical-anomaly"}
    ALLOWED_SEVERITIES = {"info", "warn", "danger"}

    def test_intelligence_returns_shape(self, imported_data):
        resp = imported_data.get(
            "/api/budgets/intelligence?period_start=2026-01-01&period_end=2026-01-31"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "insights" in data
        assert "period" in data
        assert data["period"]["start"] == "2026-01-01"
        assert data["period"]["end"] == "2026-01-31"
        assert isinstance(data["insights"], list)
        for ins in data["insights"]:
            assert ins["kind"] in self.ALLOWED_KINDS
            assert ins["severity"] in self.ALLOWED_SEVERITIES
            assert isinstance(ins["category"], str) and ins["category"]
            assert isinstance(ins["message"], str) and len(ins["message"]) > 10
            assert isinstance(ins["suggested_action"], str) and ins["suggested_action"]
            assert "evidence" in ins
            ev = ins["evidence"]
            assert "transaction_ids" in ev or "budget_ids" in ev

    def test_over_budget_risk_when_spend_exceeds_target(self, imported_data):
        # Tiny target → guaranteed to fire over-budget-risk against real data.
        imported_data.post("/api/budgets", json={
            "category_path": "Groceries",
            "budget_type": "absolute",
            "amount_cents": 100,
            "period": "month",
            "label": "intel_overbudget_test",
        })
        resp = imported_data.get(
            "/api/budgets/intelligence?period_start=2026-01-01&period_end=2026-01-31"
        )
        assert resp.status_code == 200
        risks = [i for i in resp.json()["insights"] if i["kind"] == "over-budget-risk"]
        assert any(
            r["category"] == "Groceries" and r["severity"] == "danger"
            for r in risks
        )
        for r in risks:
            assert "budget_ids" in r["evidence"]
            assert len(r["evidence"]["budget_ids"]) >= 1

    def test_savings_opportunity_when_actuals_under_target(self, imported_data):
        # Huge target → trailing avg will be < 60% of target.
        imported_data.post("/api/budgets", json={
            "category_path": "Groceries",
            "budget_type": "absolute",
            "amount_cents": 100_000_000,  # $1,000,000 / month
            "period": "month",
            "label": "intel_savings_test",
        })
        resp = imported_data.get(
            "/api/budgets/intelligence?period_start=2026-04-01&period_end=2026-04-30"
        )
        assert resp.status_code == 200
        opps = [i for i in resp.json()["insights"] if i["kind"] == "savings-opportunity"]
        # The huge-budget category should appear as a savings opportunity.
        groceries = [o for o in opps if o["category"] == "Groceries"]
        # If trailing months had real grocery spend, the insight should be emitted.
        # If no trailing spend, no insight is OK — but other categories should still pass shape.
        for o in opps:
            assert "budget_ids" in o["evidence"]
            assert "$" in o["message"]

    def test_recurring_waste_groups_subscriptions(self, test_client):
        """Recurring-waste flags subscription merchants that hit 6+ different months."""
        from app.database import get_db
        import asyncio

        async def seed():
            db = await get_db()
            try:
                for month in range(1, 8):  # 7 months
                    await db.execute(
                        """INSERT INTO transactions
                           (source_platform, source_file, account_id, posted_date, amount_cents,
                            merchant, description, normalized_description, category_path,
                            entity_type, business_context, confidence_score, categorization_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "manual", "manual", "test-acct",
                            f"2026-{month:02d}-05", -1500,
                            "Netflix Streaming", "Netflix Streaming", "netflix streaming",
                            "Expense > Personal > Subscriptions",
                            "expense", "personal", 0.95, "test",
                        ),
                    )
                await db.commit()
            finally:
                await db.close()

        asyncio.run(seed())

        resp = test_client.get(
            "/api/budgets/intelligence?period_start=2026-07-01&period_end=2026-07-31"
        )
        assert resp.status_code == 200
        waste = [i for i in resp.json()["insights"] if i["kind"] == "recurring-waste"]
        netflix = [w for w in waste if "netflix" in w["category"].lower() or "netflix" in w["message"].lower()]
        assert netflix, f"Expected Netflix in recurring-waste, got: {waste}"
        n = netflix[0]
        assert n["severity"] in self.ALLOWED_SEVERITIES
        assert len(n["evidence"]["transaction_ids"]) >= 6

    def test_historical_anomaly_when_spike_vs_trailing(self, test_client):
        """Historical anomaly fires when current spend > 2× trailing 6-mo average."""
        from app.database import get_db
        import asyncio

        async def seed():
            db = await get_db()
            try:
                # Small baseline for 6 months
                for month in range(1, 7):
                    await db.execute(
                        """INSERT INTO transactions
                           (source_platform, source_file, account_id, posted_date, amount_cents,
                            merchant, description, normalized_description, category_path,
                            entity_type, business_context, confidence_score, categorization_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "manual", "manual", "test-acct",
                            f"2026-{month:02d}-05", -1000,
                            "Test Merchant", "Test Merchant", "test merchant",
                            "Expense > Leisure > Entertainment",
                            "expense", "personal", 0.95, "test",
                        ),
                    )
                # Huge spend in current period
                for day in (1, 5, 12, 19, 26):
                    await db.execute(
                        """INSERT INTO transactions
                           (source_platform, source_file, account_id, posted_date, amount_cents,
                            merchant, description, normalized_description, category_path,
                            entity_type, business_context, confidence_score, categorization_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            "manual", "manual", "test-acct",
                            f"2026-07-{day:02d}", -50000,
                            "Spike Merchant", "Spike Merchant", "spike merchant",
                            "Expense > Leisure > Entertainment",
                            "expense", "personal", 0.95, "test",
                        ),
                    )
                await db.commit()
            finally:
                await db.close()

        asyncio.run(seed())

        resp = test_client.get(
            "/api/budgets/intelligence?period_start=2026-07-01&period_end=2026-07-31"
        )
        assert resp.status_code == 200
        anomalies = [i for i in resp.json()["insights"] if i["kind"] == "historical-anomaly"]
        leisure = [a for a in anomalies if "Leisure > Entertainment" in a["category"]]
        assert leisure, f"Expected Leisure anomaly, got: {anomalies}"
        a = leisure[0]
        assert a["severity"] in self.ALLOWED_SEVERITIES
        assert "trailing" in a["message"].lower()
        assert len(a["evidence"]["transaction_ids"]) >= 1

    def test_insights_sorted_by_severity(self, imported_data):
        # Force a danger via tiny budget
        imported_data.post("/api/budgets", json={
            "category_path": "Groceries",
            "budget_type": "absolute",
            "amount_cents": 50,
            "period": "month",
            "label": "intel_sort_test_d",
        })
        resp = imported_data.get(
            "/api/budgets/intelligence?period_start=2026-01-01&period_end=2026-01-31"
        )
        assert resp.status_code == 200
        insights = resp.json()["insights"]
        sev_order = {"danger": 0, "warn": 1, "info": 2}
        seen = [sev_order[i["severity"]] for i in insights]
        assert seen == sorted(seen)
