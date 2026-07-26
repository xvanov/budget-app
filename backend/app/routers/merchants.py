"""Spending by VENDOR — the merchant slice, composable with category and period.

Two endpoints, both period-aware and both filterable by category AND merchant at
the same time, because that is the question the owner actually asked: "if I pick
groceries and then Costco, I see all the different expenses over time for that",
and "all time and those categories, or this month and those categories".

* ``GET /api/merchants``        — the ranked breakdown, plus category facets and a
                                  spend-over-time series for the current filter.
* ``GET /api/merchants/detail`` — one merchant: its own series, its category
                                  split, and the transactions behind the number.

Deliberately a separate module from ``routers/transactions.py``: the reconciling
arithmetic lives in ``services/merchant_rollup``, whose rules are copied from the
dashboard's, and keeping the HTTP layer thin here means the file other agents are
editing needs no changes at all.

The filters are *cumulative* by construction — categories, merchants and the date
window are each an AND, so every combination is a well-defined subset of one row
universe. Two consequences worth stating:

* **Merchant identity is fixed by the period, not by the category filter.** Rows
  are grouped once over the whole window and only then filtered, so "Costco" is
  the same merchant whether or not Groceries is selected. Grouping the filtered
  subset instead would let the dominant-spelling consolidation in
  ``merchant_rollup`` reach a different answer for a different filter, and a
  merchant key that moves under the user's feet cannot be linked to or drilled
  into.
* **Each control is faceted against the OTHER one.** The merchant ranking is
  computed over the category-filtered rows (it is the merchant picker, so it must
  still list the merchants you could pick), and the category facets over the
  merchant-filtered rows. Selecting either therefore shows what it would actually
  yield given the other.
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from app.database import get_db
from app.services import merchant_rollup as roll

router = APIRouter()

# A ranked list is only useful if it is bounded, and it must never be silently
# bounded. The default page is 50 merchants; the response always reports how many
# exist, how many are hidden and what they add up to, and the UI shows that.
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500

_SORTS = {
    "spend": lambda m: (-m["spend_cents"], m["label"].lower()),
    "count": lambda m: (-m["transaction_count"], -m["spend_cents"]),
    "recent": lambda m: (m["last_date"] or "", m["spend_cents"]),
    "label": lambda m: (m["label"].lower(),),
}


def _wanted_keys(merchant: Optional[List[str]]) -> Optional[set]:
    """Requested merchant keys, with the unidentified bucket addressable.

    ``merchant_key`` returns ``""`` for a row that names nobody, and that empty
    string must never behave like a wildcard — it is the whole reason
    ``merchant_key``'s docstring insists an empty key means "no match possible".
    A query string cannot distinguish "parameter absent" from "parameter empty",
    so ``merchant=`` is treated as no filter and the unidentified bucket is
    addressed by an explicit sentinel token instead.
    """
    parts = _split_multi(merchant)
    if not parts:
        return None
    return {roll.UNIDENTIFIED_KEY if p == roll.UNIDENTIFIED_FILTER_TOKEN else p.lower()
            for p in parts}


def _split_multi(values: Optional[List[str]]) -> Optional[List[str]]:
    """Accept both ``?a=x&a=y`` and ``?a=x,y``; the SPA sends the former."""
    if not values:
        return None
    out = []
    for raw in values:
        for part in (raw or "").split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out or None


async def _rollup(db, *, start_date, end_date, scope, categories, merchant_keys,
                  granularity):
    """One row fetch, one grouping pass; everything both endpoints need."""
    rows, meta = await roll.fetch_rows(db, start_date=start_date, end_date=end_date,
                                       scope=scope)
    spending_cats, other_cats = roll.spending_categories(rows)

    # The spending universe: rows in categories whose net over THIS window is
    # negative — the same rule the dashboard uses to decide which side a category
    # lands on, so the two totals agree by construction rather than by luck.
    universe = [r for r in rows if r["category_path"] in spending_cats]
    families, assignment = roll.group_by_merchant(universe)
    period_total = -sum(int(r["effective_amount_cents"] or 0) for r in universe)

    wanted_cats = set(categories) if categories else None
    if wanted_cats:
        unknown = wanted_cats - spending_cats - other_cats
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=("No transactions in this period for category: "
                        + ", ".join(sorted(unknown))
                        + ". Categories are matched exactly, as on the dashboard."),
            )
    cat_ok = (lambda r: r["category_path"] in wanted_cats) if wanted_cats else (lambda r: True)
    merch_ok = ((lambda r: assignment.get(r["id"]) in merchant_keys)
                if merchant_keys is not None else (lambda r: True))

    filtered = [r for r in universe if cat_ok(r) and merch_ok(r)]

    # Merchant ranking: every family, its rows narrowed by the CATEGORY filter
    # only. A family left with no rows drops out of the ranking.
    merchants = []
    family_rows: Dict[str, list] = {}
    for key, group in families.items():
        kept = [r for r in group["rows"] if cat_ok(r)]
        if not kept:
            continue
        family_rows[key] = kept
        merchants.append(roll.summarize_group({**group, "rows": kept}))

    # Category facets over the merchant-filtered rows.
    facets: Dict[str, dict] = {}
    for row in (r for r in universe if merch_ok(r)):
        f = facets.setdefault(row["category_path"],
                              {"category_path": row["category_path"], "spend_cents": 0,
                               "transaction_count": 0, "merchant_keys": set()})
        f["spend_cents"] += -int(row["effective_amount_cents"] or 0)
        f["transaction_count"] += 1
        f["merchant_keys"].add(assignment.get(row["id"]))
    by_category = sorted(
        [{"category_path": f["category_path"], "spend_cents": int(f["spend_cents"]),
          "transaction_count": f["transaction_count"],
          "merchant_count": len(f["merchant_keys"])} for f in facets.values()],
        key=lambda f: -f["spend_cents"],
    )

    gran = roll.choose_granularity(start_date, end_date, granularity or "auto")
    dates = [r["posted_date"] for r in rows if r["posted_date"]]
    first = start_date or (min(dates) if dates else None)
    last = end_date or (max(dates) if dates else None)

    return {
        "meta": meta,
        "universe": universe,
        "filtered": filtered,
        "families": families,
        "family_rows": family_rows,
        "merchants": merchants,
        "by_category": by_category,
        "spending_categories": sorted(spending_cats),
        "other_categories": sorted(other_cats),
        "period_total_spend_cents": int(period_total),
        "granularity": gran,
        "first": first,
        "last": last,
    }


def _by_category(rows_by_key: dict) -> list:
    """Category facets over a {merchant_key: rows} subset, in the shape
    ``_rollup`` returns. Needed because a ``q`` search narrows the vendors after
    the rollup has already faceted the wider set. Taking the rows keyed by vendor
    rather than as one flat list is what lets ``merchant_count`` stay truthful."""
    facets: dict = {}
    for key, rows in rows_by_key.items():
        for row in rows:
            f = facets.setdefault(row["category_path"],
                                 {"category_path": row["category_path"], "spend_cents": 0,
                                  "transaction_count": 0, "merchant_keys": set()})
            f["spend_cents"] += -int(row["effective_amount_cents"] or 0)
            f["transaction_count"] += 1
            f["merchant_keys"].add(key)
    return sorted(
        [{"category_path": f["category_path"], "spend_cents": int(f["spend_cents"]),
          "transaction_count": f["transaction_count"],
          "merchant_count": len(f["merchant_keys"])} for f in facets.values()],
        key=lambda f: -f["spend_cents"],
    )


def _totals(rows) -> dict:
    spend = -sum(int(r["effective_amount_cents"] or 0) for r in rows)
    gross = sum(-int(r["amount_cents"]) for r in rows if int(r["amount_cents"] or 0) < 0)
    refund = sum(int(r["amount_cents"]) for r in rows if int(r["amount_cents"] or 0) > 0)
    return {
        "spend_cents": int(spend),
        "gross_spend_cents": int(gross),
        "refund_cents": int(refund),
        "refund_attribution_cents": int(spend - (gross - refund)),
        "transaction_count": len(rows),
    }


@router.get("/api/merchants")
async def get_merchants(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    scope: Optional[str] = Query("auto"),
    category: Optional[List[str]] = Query(None,
        description="Repeatable or comma-separated. Matched exactly, like the dashboard."),
    merchant: Optional[List[str]] = Query(None,
        description=f"Merchant keys. Use '{roll.UNIDENTIFIED_FILTER_TOKEN}' for the "
                    "unidentified-payee bucket."),
    q: Optional[str] = Query(None, description="Substring filter on the merchant label."),
    sort: str = Query("spend", pattern="^(spend|count|recent|label)$"),
    limit: int = Query(_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    granularity: str = Query("auto", pattern="^(auto|day|week|month)$"),
):
    """Merchants ranked by spend for the period, with the filters composed.

    ``totals.spend_cents`` for an unfiltered call equals ``GET /api/dashboard``'s
    ``totals.total_expense_cents`` for the same window and scope — same rows, same
    effective amounts, same per-category sign rule. The one intentional difference
    is split parents, excluded here unconditionally (see
    ``services/merchant_rollup``); on this data set they already carry
    ``entity_type='transfer'`` so the two numbers match exactly, and whatever was
    removed is reported in ``diagnostics`` either way.
    """
    db = await get_db()
    try:
        categories = _split_multi(category)
        merchant_keys = _wanted_keys(merchant)
        r = await _rollup(db, start_date=start_date, end_date=end_date, scope=scope,
                          categories=categories, merchant_keys=merchant_keys,
                          granularity=granularity)

        merchants = r["merchants"]
        # `q` narrows the vendor set, so it has to narrow the numbers derived from
        # it too. It used to filter only the ranking, leaving totals, the chart,
        # the category facets and hidden_spend_cents on the whole period: a search
        # for one vendor rendered "Total spend $61,386.86" beside "1 vendors" with
        # that vendor drawn as 100% of the pie.
        q_rows = None
        if q:
            needle = q.strip().lower()
            merchants = [m for m in merchants
                         if needle in m["label"].lower() or needle in m["merchant_key"]]
            kept_keys = {m["merchant_key"] for m in merchants}
            q_rows_by_key = {k: r["family_rows"].get(k, []) for k in kept_keys}
            q_rows = [row for rows in q_rows_by_key.values() for row in rows]
        merchants.sort(key=_SORTS.get(sort, _SORTS["spend"]))
        if sort == "recent":
            merchants.reverse()

        total_merchants = len(merchants)
        page = merchants[offset:offset + limit]
        hidden = merchants[:offset] + merchants[offset + limit:]

        # A per-merchant sparkline series for the returned page only. The rows are
        # already grouped in memory, so this is bucketing, not another query.
        for m in page:
            m["series"] = roll.build_series(r["family_rows"].get(m["merchant_key"], []),
                                            r["granularity"], r["first"], r["last"])

        rows_for_totals = r["filtered"] if q_rows is None else q_rows
        totals = _totals(rows_for_totals)
        totals["merchant_count"] = total_merchants
        unidentified = next((m for m in merchants if m["unidentified"]), None)

        return {
            "period": {"start_date": start_date, "end_date": end_date,
                       "granularity": r["granularity"]},
            "scope": r["meta"]["scope"],
            "scope_note": r["meta"]["scope_note"],
            "filters": {
                "categories": categories or [],
                "merchant_keys": sorted(k or roll.UNIDENTIFIED_FILTER_TOKEN
                                        for k in (merchant_keys or [])),
                "q": q or None,
            },
            "totals": totals,
            # The unfiltered period spend, so a filtered view can state its share
            # instead of implying it is everything.
            "period_total_spend_cents": r["period_total_spend_cents"],
            "merchants": page,
            "ranking": {
                "sort": sort, "offset": offset, "limit": limit,
                "returned": len(page),
                "total_merchants": total_merchants,
                "hidden_merchants": len(hidden),
                "hidden_spend_cents": int(sum(m["spend_cents"] for m in hidden)),
            },
            "unidentified": (
                {"merchant_key": roll.UNIDENTIFIED_FILTER_TOKEN,
                 "label": roll.UNIDENTIFIED_LABEL,
                 "spend_cents": unidentified["spend_cents"],
                 "transaction_count": unidentified["transaction_count"]}
                if unidentified else None
            ),
            "by_category": (r["by_category"] if q_rows is None
                            else _by_category(q_rows_by_key)),
            "series": roll.build_series(rows_for_totals, r["granularity"],
                                        r["first"], r["last"]),
            "diagnostics": {
                "split_parent_rows_excluded": r["meta"]["split_parent_rows_excluded"],
                "split_parent_cents_excluded": r["meta"]["split_parent_cents_excluded"],
                # Categories whose net over this window is >= 0. They are not
                # spending, so they are out of this view — named, not hidden.
                "non_spending_categories": r["other_categories"],
                "spending_category_count": len(r["spending_categories"]),
            },
        }
    finally:
        await db.close()


@router.get("/api/merchants/detail")
async def get_merchant_detail(
    merchant_key: str = Query(..., description="A key from GET /api/merchants, or "
                                               f"'{roll.UNIDENTIFIED_FILTER_TOKEN}'."),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    scope: Optional[str] = Query("auto"),
    category: Optional[List[str]] = Query(None),
    granularity: str = Query("auto", pattern="^(auto|day|week|month)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    sort: str = Query("posted_date", pattern="^(posted_date|abs_amount)$"),
    dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    """One merchant: spend over time, category split, and the rows behind it.

    Selects EXACTLY the rows the ranked list counted for this merchant, for the
    same reason ``/api/dashboard/category-detail`` does: a drill-down whose rows do
    not add up to the number that was clicked makes a real discrepancy
    indistinguishable from a mismatched query. ``summary.spend_cents`` here is the
    same figure the list reported for the same filters.
    """
    db = await get_db()
    try:
        categories = _split_multi(category)
        key = (roll.UNIDENTIFIED_KEY if merchant_key == roll.UNIDENTIFIED_FILTER_TOKEN
               else merchant_key.strip().lower())
        r = await _rollup(db, start_date=start_date, end_date=end_date, scope=scope,
                          categories=categories, merchant_keys={key},
                          granularity=granularity)
        rows = r["family_rows"].get(key)
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=(f"No spending for merchant '{merchant_key}' in this period"
                        + (" and category" if categories else "") + ". Merchant keys "
                        "come from GET /api/merchants and depend on the window."),
            )
        group = r["families"][key]
        summary = roll.summarize_group({**group, "rows": rows})
        series = roll.build_series(rows, r["granularity"], r["first"], r["last"])

        if sort == "abs_amount":
            rows_sorted = sorted(rows, key=lambda x: (abs(int(x["amount_cents"] or 0)), x["id"]),
                                 reverse=(dir == "desc"))
        else:
            rows_sorted = sorted(rows, key=lambda x: (x["posted_date"] or "", x["id"]),
                                 reverse=(dir == "desc"))
        start = (page - 1) * page_size
        window = rows_sorted[start:start + page_size]

        return {
            "period": {"start_date": start_date, "end_date": end_date,
                       "granularity": r["granularity"]},
            "scope": r["meta"]["scope"],
            "summary": summary,
            "series": series,
            "transactions": window,
            "total": len(rows_sorted),
            "page": page,
            "page_size": page_size,
            "period_total_spend_cents": r["period_total_spend_cents"],
            "filters": {"categories": categories or []},
        }
    finally:
        await db.close()
