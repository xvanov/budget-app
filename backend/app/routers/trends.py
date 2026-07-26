from fastapi import APIRouter, Query
from typing import Optional
from app.database import get_db
from app.services.scope import EFFECTIVE_AMOUNT, EFFECTIVE_NET_SUM

router = APIRouter()


def _period_expr(period: str) -> str:
    if period == "quarterly":
        return """substr(t.posted_date, 1, 4) || '-Q' ||
            CASE
                WHEN CAST(substr(t.posted_date, 6, 2) AS INTEGER) BETWEEN 1 AND 3 THEN '1'
                WHEN CAST(substr(t.posted_date, 6, 2) AS INTEGER) BETWEEN 4 AND 6 THEN '2'
                WHEN CAST(substr(t.posted_date, 6, 2) AS INTEGER) BETWEEN 7 AND 9 THEN '3'
                ELSE '4'
            END"""
    return "substr(t.posted_date, 1, 7)"


@router.get("/api/trends")
async def get_trends(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    period: str = Query("monthly"),
    metric: Optional[str] = Query(None),
    scope: Optional[str] = "auto",
):
    from app.services.scope import resolve_scope

    db = await get_db()
    try:
        # Scope, like every other reporting endpoint. Without it the series
        # included the superseded CSV history, so the chart disagreed with the
        # dashboard by several multiples.
        scope_sql, scope_params, _effective, _note = await resolve_scope(db, scope)
        where_clause = scope_sql
        params = list(scope_params)
        if start_date:
            where_clause += " AND t.posted_date >= ?"
            params.append(start_date)
        if end_date:
            where_clause += " AND t.posted_date <= ?"
            params.append(end_date)

        pe = _period_expr(period)

        if metric == "net_worth":
            return await _net_worth_series(db, start_date, end_date)

        series = await _main_series(db, pe, where_clause, params, metric)
        result = {"period": period, "metric": metric or "all", "series": series}

        if metric is None:
            await _attach_categories(db, pe, where_clause, params, series)

        return result
    finally:
        await db.close()


async def _main_series(db, pe, where_clause, params, metric):
    """Income/expense per period, aggregated exactly as ``/api/dashboard`` does.

    Two things this has to get right or the chart contradicts the KPI cards:

    * **Effective amounts, not raw ones** — a purchase refunded in a LATER period
      stops counting in the period it was made, and the period the credit landed in
      does not show it as income. See ``services/scope.py`` → refund attribution.
    * **Net PER CATEGORY, then bucketed by sign** — not gross inflow/outflow per
      period. Those are different numbers: a category with both a purchase and a
      refund inside one month contributes to both sides of a gross sum but only to
      its net side in the dashboard. Summing gross made the monthly series
      irreconcilable with the totals above it, which is what
      ``test_trends_identity_sum_of_monthly`` was catching.
    """
    cursor = await db.execute(
        f"""SELECT period, COALESCE(SUM(CASE WHEN net > 0 THEN net ELSE 0 END), 0) as income,
                   COALESCE(SUM(CASE WHEN net < 0 THEN -net ELSE 0 END), 0) as expense
            FROM (
                SELECT {pe} as period, t.category_path,
                       {EFFECTIVE_NET_SUM} as net
                FROM transactions t
                WHERE (t.entity_type IS NULL OR t.entity_type NOT IN ('transfer', 'asset_conversion', 'excluded'))
                  AND t.category_path IS NOT NULL
                  AND t.category_path != 'Uncategorized'
                  {where_clause}
                GROUP BY period, t.category_path
            )
            GROUP BY period
            ORDER BY period""",
        params,
    )
    rows = await cursor.fetchall()
    series = []
    for row in rows:
        income = row["income"]
        expense = row["expense"]
        net = income - expense
        entry = {
            "period": row["period"],
            "income_cents": income,
            "expense_cents": expense,
            "net_cents": net,
        }
        if metric == "revenue":
            entry["value"] = income
        elif metric == "expense":
            entry["value"] = expense
        elif metric == "profit":
            entry["value"] = net
        else:
            entry["value"] = net
        series.append(entry)
    return series


async def _attach_categories(db, pe, where_clause, params, series):
    cursor = await db.execute(
        f"""SELECT {pe} as period, t.category_path,
                   COALESCE(SUM(CASE WHEN ({EFFECTIVE_AMOUNT}) < 0
                                     THEN -({EFFECTIVE_AMOUNT}) ELSE 0 END), 0) as amount
            FROM transactions t
            WHERE (t.entity_type IS NULL OR t.entity_type NOT IN ('transfer', 'asset_conversion', 'excluded'))
              AND t.category_path IS NOT NULL
              {where_clause}
            GROUP BY period, t.category_path
            HAVING amount > 0
            ORDER BY period""",
        params,
    )
    cat_rows = await cursor.fetchall()
    period_map = {s["period"]: s for s in series}
    for cr in cat_rows:
        p = cr["period"]
        if p in period_map:
            if "categories" not in period_map[p]:
                period_map[p]["categories"] = {}
            period_map[p]["categories"][cr["category_path"]] = cr["amount"]


async def _net_worth_series(db, start_date, end_date):
    from app.routers.net_worth import _compute_current_balances, _compute_history

    (accounts, assets, liabilities, prop_val, mortgage_bal,
     mortgage_by_month, first_mortgage_month, _unlinked) = await _compute_current_balances(db)
    history = await _compute_history(db, accounts,
                                    mortgage_by_month, first_mortgage_month)

    if start_date:
        history = [h for h in history if h["date"] >= start_date[:7] + "-01"]
    if end_date:
        history = [h for h in history if h["date"] <= end_date[:7] + "-01"]

    series = []
    for h in history:
        series.append({
            "period": h["date"][:7],
            "value": h["value"],
            "assets_cents": h.get("assets", 0),
            "liabilities_cents": h.get("liabilities", 0),
        })

    return {"period": "monthly", "metric": "net_worth", "series": series}
