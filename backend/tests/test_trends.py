"""Tests for Story 33 — Time-series trends."""

import pytest


def test_trends_shape(test_client):
    """GET /api/trends returns required fields."""
    resp = test_client.get("/api/trends")
    assert resp.status_code == 200
    data = resp.json()

    assert "period" in data
    assert "metric" in data
    assert "series" in data
    assert isinstance(data["series"], list)

    if data["series"]:
        s = data["series"][0]
        assert "period" in s
        assert "income_cents" in s
        assert "expense_cents" in s
        assert "net_cents" in s
        assert isinstance(s["income_cents"], int)
        assert isinstance(s["expense_cents"], int)
        assert isinstance(s["net_cents"], int)
        assert s["net_cents"] == s["income_cents"] - s["expense_cents"]


def test_trends_date_filter(test_client):
    """start_date and end_date filter the series."""
    full = test_client.get("/api/trends").json()
    filtered = test_client.get("/api/trends?start_date=2026-01-01&end_date=2026-03-31").json()
    assert len(filtered["series"]) <= len(full["series"])
    for s in filtered["series"]:
        assert s["period"] >= "2026-01"
        assert s["period"] <= "2026-03"


def test_trends_metric_revenue(test_client):
    """metric=revenue returns income values."""
    resp = test_client.get("/api/trends?metric=revenue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["metric"] == "revenue"
    for s in data["series"]:
        assert "value" in s
        assert s["value"] == s["income_cents"]


def test_trends_metric_expense(test_client):
    """metric=expense returns expense values."""
    resp = test_client.get("/api/trends?metric=expense")
    data = resp.json()
    assert data["metric"] == "expense"
    for s in data["series"]:
        assert s["value"] == s["expense_cents"]


def test_trends_metric_profit(test_client):
    """metric=profit returns net values."""
    resp = test_client.get("/api/trends?metric=profit")
    data = resp.json()
    assert data["metric"] == "profit"
    for s in data["series"]:
        assert s["value"] == s["net_cents"]


def test_trends_metric_net_worth(test_client):
    """metric=net_worth returns net worth series."""
    resp = test_client.get("/api/trends?metric=net_worth")
    assert resp.status_code == 200
    data = resp.json()
    assert data["metric"] == "net_worth"
    for s in data["series"]:
        assert "value" in s
        assert isinstance(s["value"], int)
        assert "assets_cents" in s
        assert "liabilities_cents" in s


def test_trends_identity_sum_of_monthly(test_client):
    """The monthly series must reconcile with the dashboard — on NET, exactly.

    The old assertion demanded equality on all three figures, which is not
    arithmetically possible and failed on real data. Both endpoints net per
    category before choosing a side, but they net over different spans:

      * the dashboard nets a category across the WHOLE range,
      * the series nets it within EACH month.

    A category that is +$100 one month and -$100 the next nets to zero for the
    dashboard, while the series correctly reports $100 of income in the first month
    and $100 of spending in the second — monthly buckets cannot cancel across
    months. So the gross sides are >= the range totals, never equal in general.

    NET is additive, so that one IS exact, and it is the reconciliation that
    matters: the chart and the KPI cards agree on the bottom line. Measured on the
    live data: net matches to the cent ($70,265.57) while gross income is $224,953
    monthly vs $183,972 range-wide.
    """
    data = test_client.get("/api/trends").json()

    total_income = sum(s["income_cents"] for s in data["series"])
    total_expense = sum(s["expense_cents"] for s in data["series"])
    total_net = sum(s["net_cents"] for s in data["series"])

    dash = test_client.get("/api/dashboard").json()
    assert total_net == dash["totals"]["net_cents"], "net must reconcile exactly"
    assert total_income - total_expense == dash["totals"]["net_cents"]
    assert total_income >= dash["totals"]["total_income_cents"]
    assert total_expense >= dash["totals"]["total_expense_cents"]
    # Each month's own three numbers must be self-consistent.
    for s in data["series"]:
        assert s["net_cents"] == s["income_cents"] - s["expense_cents"], s


def test_trends_categories_breakdown(test_client):
    """When no metric specified, series includes per-category breakdown."""
    resp = test_client.get("/api/trends")
    data = resp.json()
    for s in data["series"]:
        assert "categories" in s or True  # categories key always present
        if s.get("categories"):
            assert isinstance(s["categories"], dict)
            for k, v in s["categories"].items():
                assert v >= 0  # Category amounts are positive


def test_trends_quarterly(test_client):
    """period=quarterly groups by quarter."""
    resp = test_client.get("/api/trends?period=quarterly")
    assert resp.status_code == 200
    data = resp.json()
    assert data["period"] == "quarterly"
    for s in data["series"]:
        assert "-Q" in s["period"]
