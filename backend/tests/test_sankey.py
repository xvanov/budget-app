"""Tests for Story 50 — Sankey diagram."""

import pytest


def test_sankey_shape(test_client):
    """GET /api/sankey returns required fields."""
    resp = test_client.get("/api/sankey")
    assert resp.status_code == 200
    data = resp.json()

    assert "nodes" in data
    assert "links" in data
    assert "total_income_cents" in data
    assert "total_expense_cents" in data
    assert "net_cents" in data
    assert isinstance(data["nodes"], list)
    assert isinstance(data["links"], list)
    assert isinstance(data["total_income_cents"], int)
    assert isinstance(data["total_expense_cents"], int)
    assert isinstance(data["net_cents"], int)

    assert data["net_cents"] == data["total_income_cents"] - data["total_expense_cents"]


def test_sankey_node_fields(test_client):
    """Every node has required fields."""
    resp = test_client.get("/api/sankey")
    data = resp.json()

    for node in data["nodes"]:
        assert "id" in node
        assert "label" in node
        assert "value_cents" in node
        assert "color" in node
        assert "col" in node
        assert "kind" in node
        assert isinstance(node["value_cents"], int)
        assert node["value_cents"] > 0
        assert node["col"] in (0, 1, 2, 3)
        assert node["kind"] in ("income-sub", "income-cat", "expense-cat", "expense-sub", "net")


def test_sankey_link_fields(test_client):
    """Every link has required fields and references valid node IDs."""
    resp = test_client.get("/api/sankey")
    data = resp.json()
    node_ids = {n["id"] for n in data["nodes"]}

    for link in data["links"]:
        assert "source" in link
        assert "target" in link
        assert "value_cents" in link
        assert link["source"] in node_ids
        assert link["target"] in node_ids
        assert isinstance(link["value_cents"], int)
        assert link["value_cents"] > 0


def test_sankey_no_uncategorized(test_client):
    """No node named Uncategorized or Unknown."""
    resp = test_client.get("/api/sankey")
    data = resp.json()

    for node in data["nodes"]:
        assert "uncategorized" not in node["label"].lower()
        assert "unknown" not in node["label"].lower()
        assert node["label"] != ""
        assert node["label"] is not None


def test_sankey_consistency_with_dashboard(test_client):
    """Sankey totals match dashboard totals."""
    sankey = test_client.get("/api/sankey").json()
    dash = test_client.get("/api/dashboard").json()

    assert sankey["total_income_cents"] == dash["totals"]["total_income_cents"]
    assert sankey["total_expense_cents"] == dash["totals"]["total_expense_cents"]
    assert sankey["net_cents"] == dash["totals"]["net_cents"]


def test_sankey_income_sum_matches(test_client):
    """Sum of income-sub node values equals total_income_cents."""
    data = test_client.get("/api/sankey").json()
    income_sub_sum = sum(
        n["value_cents"] for n in data["nodes"]
        if n["kind"] == "income-sub"
    )
    income_cat_sum = sum(
        n["value_cents"] for n in data["nodes"]
        if n["kind"] == "income-cat"
    )
    assert income_sub_sum == data["total_income_cents"]
    assert income_cat_sum == data["total_income_cents"]


def test_sankey_links_conserve_income(test_client):
    """Column 2 accounts for every dollar that had to be allocated.

    Column 2 holds the expense categories plus, when the period ran a surplus, a
    "Net Cash Flow" node for what was left over. So:

      surplus  -> expenses + (income - expenses) == income
      deficit  -> expenses alone, which EXCEEDS income

    The old assertion was ``col2_sum == total_income_cents`` flat, which is only
    true in a surplus. It failed whenever the fixture data spent more than it
    earned — asserting an invariant the diagram never claimed. The real invariant
    is the larger of the two sides.
    """
    data = test_client.get("/api/sankey").json()

    col2_sum = sum(
        n["value_cents"] for n in data["nodes"]
        if n["col"] == 2
    )
    income = data["total_income_cents"]
    expense = data["total_expense_cents"]
    assert col2_sum == max(income, expense), (
        f"col2={col2_sum} income={income} expense={expense}")
    if income >= expense:
        assert col2_sum == income


def test_sankey_date_filter(test_client):
    """Date filtering limits the data."""
    full = test_client.get("/api/sankey").json()
    filtered = test_client.get("/api/sankey?start_date=2099-01-01").json()
    assert filtered["total_income_cents"] == 0
    assert filtered["total_expense_cents"] == 0
    assert filtered["net_cents"] == 0
    assert len(filtered["nodes"]) == 0
    assert len(filtered["links"]) == 0
