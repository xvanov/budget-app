"""Every endpoint that reports the same money must report the same number.

Two bugs this locks down, both of which put contradictory figures on one screen:

1. **The Cash Flow Sankey disagreed with the KPI cards above it.** ``/api/sankey``
   had no scope filter, so it summed the superseded CSV history alongside the live
   Plaid rows — all-time income read $1,592,352 where the dashboard said $247,707,
   6.4x larger. It also summed each category's inflows and outflows separately, so
   a category with both landed on both sides of the diagram, where the dashboard
   lets the category's NET pick one side.

2. **The Transactions summary line described one page.** It summed the 50 rows the
   client happened to hold and printed that beside the full result count.

The fix in both cases is that the reporting endpoints share one arithmetic: scope,
effective (refund-attributed) amounts, net per category, and no Uncategorized.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

MARK = "Reportconsist"
_INSERTED = []


@pytest.fixture(autouse=True)
def cleanup():
    yield
    if not _INSERTED:
        return
    con = sqlite3.connect(os.environ["DB_PATH"])
    try:
        con.execute(
            f"DELETE FROM transactions WHERE id IN ({','.join('?' * len(_INSERTED))})",
            _INSERTED,
        )
        con.commit()
    finally:
        con.close()
        _INSERTED.clear()


async def _insert(db, *, amount, date, category, platform="plaid", entity=None):
    entity = entity or ("income" if amount > 0 else "expense")
    cursor = await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, category_path, entity_type, business_context)
           VALUES (?, 'rc.csv', 'card-0001', ?, ?, ?, ?, ?, ?, 'personal')""",
        (platform, date, amount, MARK, MARK, category, entity),
    )
    _INSERTED.append(cursor.lastrowid)
    return cursor.lastrowid


@pytest.fixture
def seeded(test_client):
    """Live rows plus a CSV-history row that must NOT reach a default report."""
    import asyncio
    from app.database import get_db

    async def go():
        db = await get_db()
        try:
            ids = {}
            ids["income"] = await _insert(db, amount=500000, date="2033-03-05",
                                          category="Consulting")
            ids["expense"] = await _insert(db, amount=-120000, date="2033-03-08",
                                           category="Home Improvement")
            # Same category, both directions inside one month: gross would report it
            # on both sides, net puts it on one.
            ids["mixed_out"] = await _insert(db, amount=-80000, date="2033-03-10",
                                             category="Shopping")
            ids["mixed_in"] = await _insert(db, amount=30000, date="2033-03-20",
                                            category="Shopping")
            # Stored CSV history: real data, but not the live source.
            ids["csv"] = await _insert(db, amount=-999999, date="2033-03-12",
                                       category="Home Improvement", platform="chase")
            await db.commit()
            return ids
        finally:
            await db.close()

    return asyncio.run(go())


RANGE = "start_date=2033-03-01&end_date=2033-03-31"


def test_sankey_totals_equal_dashboard_totals(seeded, test_client):
    sankey = test_client.get(f"/api/sankey?{RANGE}").json()
    dash = test_client.get(f"/api/dashboard?{RANGE}").json()
    assert sankey["total_income_cents"] == dash["totals"]["total_income_cents"]
    assert sankey["total_expense_cents"] == dash["totals"]["total_expense_cents"]
    assert sankey["net_cents"] == dash["totals"]["net_cents"]


def test_trends_months_reconcile_with_dashboard_on_net(seeded, test_client):
    """Net is additive across months, so it reconciles exactly.

    Gross income/expense cannot: the dashboard nets a category over the whole
    range while the series nets it per month, and monthly buckets cannot cancel
    across months. See test_trends.py::test_trends_identity_sum_of_monthly.
    """
    trends = test_client.get(f"/api/trends?{RANGE}").json()
    dash = test_client.get(f"/api/dashboard?{RANGE}").json()
    income = sum(s["income_cents"] for s in trends["series"])
    expense = sum(s["expense_cents"] for s in trends["series"])
    assert income - expense == dash["totals"]["net_cents"]
    assert income >= dash["totals"]["total_income_cents"]
    assert expense >= dash["totals"]["total_expense_cents"]


def test_reports_exclude_the_csv_history_by_default(seeded, test_client):
    """The $9,999.99 CSV row must not appear in any default report."""
    dash = test_client.get(f"/api/dashboard?{RANGE}").json()
    assert dash["scope"] == "plaid"
    assert dash["totals"]["total_expense_cents"] == 120000 + 80000 - 30000, dash["totals"]
    sankey = test_client.get(f"/api/sankey?{RANGE}").json()
    assert sankey["total_expense_cents"] == dash["totals"]["total_expense_cents"]
    trends = test_client.get(f"/api/trends?{RANGE}").json()
    # One month of fixture data, so per-month and range-wide netting coincide here.
    assert sum(s["expense_cents"] for s in trends["series"]) \
        == dash["totals"]["total_expense_cents"]


def test_scope_all_reaches_the_csv_history_consistently(seeded, test_client):
    """Opting in must move every report by the same amount, not just one."""
    dash = test_client.get(f"/api/dashboard?{RANGE}&scope=all").json()
    sankey = test_client.get(f"/api/sankey?{RANGE}&scope=all").json()
    trends = test_client.get(f"/api/trends?{RANGE}&scope=all").json()
    assert dash["totals"]["total_expense_cents"] == 120000 + 80000 - 30000 + 999999
    assert sankey["total_expense_cents"] == dash["totals"]["total_expense_cents"]
    assert sum(s["expense_cents"] for s in trends["series"]) \
        == dash["totals"]["total_expense_cents"]


def test_a_mixed_category_lands_on_one_side_only(seeded, test_client):
    """Shopping is -800 + 300 = -500 net: an expense of 500, never also income."""
    sankey = test_client.get(f"/api/sankey?{RANGE}").json()
    labels_income = {n["label"] for n in sankey["nodes"] if n["kind"].startswith("income")}
    labels_expense = {n["label"] for n in sankey["nodes"] if n["kind"].startswith("expense")}
    assert "Shopping" in labels_expense
    assert "Shopping" not in labels_income, "a category cannot be on both sides"


def test_transactions_totals_cover_the_whole_filtered_set(seeded, test_client):
    """Not just the page. Ask for one row and the totals must still be complete."""
    full = test_client.get(f"/api/transactions?{RANGE}&search={MARK}&page_size=100")
    assert full.status_code == 200, full.text
    full_body = full.json()

    one = test_client.get(f"/api/transactions?{RANGE}&search={MARK}&page_size=1")
    one_body = one.json()
    assert len(one_body["transactions"]) == 1
    assert one_body["total"] == full_body["total"]
    # The point of the fix: identical totals despite a single row on the page.
    assert one_body["totals"] == full_body["totals"], (
        f"page size changed the money totals: {one_body['totals']} vs {full_body['totals']}")


def test_transactions_totals_match_the_rows_they_describe(seeded, test_client):
    """Gross in/out of the listed rows — the summary describes this list."""
    resp = test_client.get(f"/api/transactions?{RANGE}&search={MARK}&page_size=100")
    body = resp.json()
    rows = body["transactions"]
    income = sum(t["amount_cents"] for t in rows if t["amount_cents"] > 0)
    expense = sum(-t["amount_cents"] for t in rows if t["amount_cents"] < 0)
    assert body["totals"]["income_cents"] == income
    assert body["totals"]["expense_cents"] == expense
    assert body["totals"]["net_cents"] == income - expense
    assert body["totals"]["transaction_count"] == body["total"]


def test_transactions_totals_respect_the_flow_filter(seeded, test_client):
    """A filtered list must report the filtered money, not everything."""
    inc = test_client.get(f"/api/transactions?{RANGE}&search={MARK}&flow=income").json()
    assert inc["totals"]["expense_cents"] == 0, inc["totals"]
    assert inc["totals"]["income_cents"] == 500000 + 30000, inc["totals"]
