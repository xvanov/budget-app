from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List
from pydantic import BaseModel
from app.database import get_db
from app.llm import call_llm

router = APIRouter()


class BudgetCreate(BaseModel):
    category_path: str
    budget_type: str
    amount_cents: Optional[int] = None
    percent: Optional[float] = None
    period: str = "month"
    label: str = ""


class BudgetUpdate(BaseModel):
    category_path: Optional[str] = None
    budget_type: Optional[str] = None
    amount_cents: Optional[int] = None
    percent: Optional[float] = None
    period: Optional[str] = None
    label: Optional[str] = None


class RolloverRequest(BaseModel):
    from_period_start: str
    from_period_end: str
    to_period_start: str
    to_period_end: str


class RolloverBudgetCreate(BaseModel):
    category_path: str
    budget_type: str
    amount_cents: Optional[int] = None
    percent: Optional[float] = None
    label: str = ""


class RolloverAcceptRequest(BaseModel):
    budgets: List[RolloverBudgetCreate]
    to_period_start: str
    to_period_end: str


async def _get_period_income(db, period_start: str, period_end: str, scope: str = "auto") -> int:
    """Income for the period, on the same terms as the dashboard.

    Was an unscoped ``SUM(amount_cents)``, so every account with both CSV history
    and a Plaid twin contributed each payslip twice.

    Income is the sum of the categories whose net over the window is POSITIVE —
    the same per-category rule the dashboard applies — so the two pages agree.
    Summing income-signed rows directly instead counts a refund inside a spending
    category as income and overstated the figure by $1,760.51 on one month.
    """
    from app.services.scope import EFFECTIVE_NET_SUM, resolve_scope

    scope_sql, scope_params, _eff, _note = await resolve_scope(db, scope)
    cursor = await db.execute(
        f"""SELECT {EFFECTIVE_NET_SUM} as net
            FROM transactions t
            WHERE (t.entity_type IS NULL
                   OR t.entity_type NOT IN ('transfer', 'asset_conversion', 'excluded'))
              AND t.category_path IS NOT NULL
              AND t.category_path != 'Uncategorized'
              AND t.posted_date >= ? AND t.posted_date <= ?
              {scope_sql}
            GROUP BY t.category_path""",
        (period_start, period_end, *scope_params),
    )
    return sum(max(0, (r["net"] or 0)) for r in await cursor.fetchall())


async def _get_spent_cents(db, category_path: str, period_start: str, period_end: str,
                           scope: str = "auto") -> int:
    """Spend in one category for the period, on the same terms as the dashboard.

    Four things were wrong here, and together they made a $1,000 grocery budget
    read 162% over when it was 37% over:

    * **No scope.** The CSV history and its Plaid twins were both counted, so
      every figure on this page was close to double. Groceries for June 2026 came
      out at $2,623.60 against the dashboard's $1,372.50.
    * **``category_path LIKE 'X%'``** meant a budget on ``Other`` also swept up
      ``Other Income``. Categories are flat and matched exactly everywhere else.
    * **``SUM(ABS(amount_cents))`` with ``amount_cents < 0``** counted charges and
      ignored refunds, so returning an item never reduced what the budget had
      spent — the opposite of how every other total in the app treats a refund.
    * **``entity_type NOT IN (…)``** is false for NULL, so rows with no
      entity_type were silently dropped from the budget while counting elsewhere.

    ``EFFECTIVE_NET_SUM`` fixes the last two together: it is the shared
    contra-expense arithmetic, it books a refund against the period of the charge
    it reverses, and taking the category's NET is exactly how the dashboard
    decides what a category spent. Verified against June 2026: Groceries
    $2,623.60 -> $1,372.50, Restaurants -> $822.59, Other -> $304.36, all three
    now equal to the dashboard's own figure.
    """
    from app.services.scope import EFFECTIVE_NET_SUM, resolve_scope

    scope_sql, scope_params, _eff, _note = await resolve_scope(db, scope)
    cursor = await db.execute(
        f"""SELECT {EFFECTIVE_NET_SUM} as net
            FROM transactions t
            WHERE t.category_path = ?
              AND (t.entity_type IS NULL
                   OR t.entity_type NOT IN ('transfer', 'asset_conversion', 'excluded'))
              AND t.posted_date >= ? AND t.posted_date <= ?
              {scope_sql}""",
        (category_path, period_start, period_end, *scope_params),
    )
    row = await cursor.fetchone()
    # The category's NET, matching the dashboard: a category that netted positive
    # over the window has spent nothing, and a refund reduces what was spent.
    net = (row["net"] if row else 0) or 0
    return -net if net < 0 else 0


def _compute_color(pct_used: float) -> str:
    if pct_used >= 100:
        return "red"
    elif pct_used >= 80:
        return "yellow"
    else:
        return "green"


def _parse_period(period: str, ref_date: Optional[str] = None) -> tuple:
    from datetime import datetime, timedelta
    if ref_date:
        ref = datetime.strptime(ref_date, "%Y-%m-%d")
    else:
        ref = datetime.now()
    if period == "week":
        start = ref - timedelta(days=ref.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    elif period == "year":
        start = ref.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = ref.replace(month=12, day=31, hour=23, minute=59, second=59)
    else:
        start = ref.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if ref.month == 12:
            end = ref.replace(year=ref.year + 1, month=1, day=1) - timedelta(seconds=1)
        else:
            end = ref.replace(month=ref.month + 1, day=1) - timedelta(seconds=1)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


@router.post("/api/budgets", status_code=201)
async def create_budget(budget: BudgetCreate):
    if budget.budget_type not in ("absolute", "percent_of_income"):
        raise HTTPException(status_code=400, detail="budget_type must be 'absolute' or 'percent_of_income'")
    if budget.budget_type == "absolute" and budget.amount_cents is None:
        raise HTTPException(status_code=400, detail="amount_cents required for absolute budgets")
    if budget.budget_type == "percent_of_income" and budget.percent is None:
        raise HTTPException(status_code=400, detail="percent required for percent_of_income budgets")
    if budget.period not in ("week", "month", "year"):
        raise HTTPException(status_code=400, detail="period must be 'week', 'month', or 'year'")

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO budgets (category_path, budget_type, amount_cents, percent, period, label) VALUES (?, ?, ?, ?, ?, ?)",
            (budget.category_path, budget.budget_type, budget.amount_cents, budget.percent, budget.period, budget.label),
        )
        new_id = cursor.lastrowid
        await db.commit()
        cursor = await db.execute("SELECT * FROM budgets WHERE id=?", (new_id,))
        row = await cursor.fetchone()
        return dict(row)
    finally:
        await db.close()


@router.get("/api/budgets")
async def list_budgets():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM budgets ORDER BY id")
        rows = await cursor.fetchall()
        return {"budgets": [dict(r) for r in rows]}
    finally:
        await db.close()


@router.get("/api/budgets/status")
async def budget_status(period: Optional[str] = Query(None), period_start: Optional[str] = Query(None),
                       period_end: Optional[str] = Query(None),
                       scope: Optional[str] = Query("auto")):
    """Budget usage for a period.

    ``scope`` exists here for the same reason it exists on the dashboard: the DB
    holds the pre-Plaid CSV history alongside its Plaid twins, and an unscoped sum
    counts twinned transactions twice. It defaults to ``auto`` (live data), so this
    page and the dashboard now report the same spend; pass ``all`` to include the
    CSV history.
    """
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM budgets ORDER BY id")
        budgets = await cursor.fetchall()
        if not budgets:
            return {"budgets": []}

        if period_start and period_end:
            ps, pe = period_start, period_end
        elif period:
            ps, pe = _parse_period(period)
        else:
            ps, pe = _parse_period("month")

        total_income = await _get_period_income(db, ps, pe, scope)

        results = []
        for b in budgets:
            b = dict(b)
            if b["budget_type"] == "absolute":
                target_cents = b["amount_cents"] or 0
            else:
                pct = b["percent"] or 0
                target_cents = int(total_income * pct / 100.0)

            spent_cents = await _get_spent_cents(db, b["category_path"], ps, pe, scope)
            pct_used = (spent_cents / target_cents * 100) if target_cents > 0 else 0
            color = _compute_color(pct_used)
            forecast_overrun_cents = max(0, spent_cents - target_cents)

            from datetime import datetime, timedelta
            now = datetime.now()
            try:
                n = datetime.strptime(pe, "%Y-%m-%d")
                total_days = max(1, (n - datetime.strptime(ps, "%Y-%m-%d")).days + 1)
                elapsed_days = max(0, min(total_days, (now - datetime.strptime(ps, "%Y-%m-%d")).days + 1))
            except Exception:
                total_days = 30
                elapsed_days = 15

            expected_pct = (elapsed_days / total_days) * 100

            results.append({
                "id": b["id"],
                "category_path": b["category_path"],
                "budget_type": b["budget_type"],
                "amount_cents": b["amount_cents"],
                "percent": b["percent"],
                "period": b["period"],
                "label": b["label"],
                "target_cents": target_cents,
                "spent_cents": spent_cents,
                "pct_used": round(pct_used, 1),
                "color": color,
                "forecast_overrun_cents": forecast_overrun_cents,
                "expected_pct": round(expected_pct, 1),
                "period_start": ps,
                "period_end": pe,
                "total_income_cents": total_income,
            })

        return {"budgets": results}
    finally:
        await db.close()


@router.post("/api/budgets/rollover")
async def budget_rollover(req: RolloverRequest):
    """Compute rollover data: previous period budgets + suggested next-period allocations."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM budgets ORDER BY id")
        budgets = await cursor.fetchall()
        if not budgets:
            raise HTTPException(status_code=400, detail="No budgets exist to roll over")

        total_income = await _get_period_income(db, req.from_period_start, req.from_period_end)

        previous = []
        for b in budgets:
            b = dict(b)
            if b["budget_type"] == "absolute":
                target_cents = b["amount_cents"] or 0
            else:
                pct = b["percent"] or 0
                target_cents = int(total_income * pct / 100.0)

            spent_cents = await _get_spent_cents(db, b["category_path"], req.from_period_start, req.from_period_end)
            variance = spent_cents - target_cents

            previous.append({
                "category_path": b["category_path"],
                "budget_type": b["budget_type"],
                "amount_cents": b["amount_cents"],
                "percent": b["percent"],
                "target_cents": target_cents,
                "spent_cents": spent_cents,
                "variance": variance,
                "label": b["label"],
            })

        suggested = _generate_suggestions(previous)

        return {
            "from_period": {"start": req.from_period_start, "end": req.from_period_end},
            "to_period": {"start": req.to_period_start, "end": req.to_period_end},
            "previous": previous,
            "suggested": suggested,
        }
    finally:
        await db.close()


@router.post("/api/budgets/rollover/accept", status_code=201)
async def budget_rollover_accept(req: RolloverAcceptRequest):
    """Accept rollover suggestions and create budgets for the new period."""
    db = await get_db()
    try:
        created = []
        for budget in req.budgets:
            cursor = await db.execute(
                "INSERT INTO budgets (category_path, budget_type, amount_cents, percent, period, label) VALUES (?, ?, ?, ?, ?, ?)",
                (budget.category_path, budget.budget_type, budget.amount_cents, budget.percent, "month", budget.label),
            )
            new_id = cursor.lastrowid
            await db.commit()
            cursor = await db.execute("SELECT * FROM budgets WHERE id=?", (new_id,))
            row = await cursor.fetchone()
            created.append(dict(row))
        return {"budgets": created, "count": len(created)}
    finally:
        await db.close()


def _generate_suggestions(previous):
    """Generate human-readable rollover suggestions.

    Uses LLM if available, otherwise falls back to deterministic logic
    that keeps the same allocation but notes overruns/underruns.
    """
    lines = []
    for p in previous:
        if p["budget_type"] == "absolute":
            amt_label = f"${(p['amount_cents'] or 0) / 100:.0f}"
        else:
            amt_label = f"{p['percent']}% of income"

        if p["variance"] > 0:
            over_pct = (p["variance"] / p["target_cents"] * 100) if p["target_cents"] > 0 else 0
            lines.append(
                f"  - {p['category_path']}: previously {amt_label}, spent ${p['spent_cents']/100:.0f} ({over_pct:.0f}% over). "
                f"Suggestion: increase allocation to cover the overrun."
            )
        elif p["variance"] < 0 and abs(p["variance"]) / max(p["target_cents"], 1) > 0.2:
            under_pct = abs(p["variance"]) / p["target_cents"] * 100 if p["target_cents"] > 0 else 0
            lines.append(
                f"  - {p['category_path']}: previously {amt_label}, spent ${p['spent_cents']/100:.0f} ({under_pct:.0f}% under). "
                f"Allocation could be reduced."
            )
        else:
            lines.append(
                f"  - {p['category_path']}: previously {amt_label}, spent ${p['spent_cents']/100:.0f} (on track). "
                f"Keep same allocation."
            )

    prompt = (
        "You are a budget rollover assistant. Given previous-period budget allocations and their actual spend, "
        "suggest allocations for the next period. Respond with a JSON array of objects with fields: "
        "category_path, budget_type, amount_cents (or percent), label, and reason (human-readable string).\n\n"
        "Previous period data:\n" + "\n".join(lines) + "\n\n"
        "Return a JSON array. If no data, return []."
    )

    suggestions = []
    try:
        raw = call_llm(prompt, strong=False)
        import json as _json
        parsed = _json.loads(raw)
        if isinstance(parsed, list):
            for item in parsed:
                suggestions.append({
                    "category_path": item.get("category_path", ""),
                    "budget_type": item.get("budget_type", "absolute"),
                    "amount_cents": item.get("amount_cents"),
                    "percent": item.get("percent"),
                    "label": item.get("label", ""),
                    "reason": item.get("reason", "Rolled over from previous period"),
                })
    except Exception:
        suggestions = []

    if not suggestions:
        suggestions = _fallback_suggestions(previous)

    return suggestions


def _fallback_suggestions(previous):
    """Deterministic fallback: carry forward same allocation with variance note."""
    suggestions = []
    for p in previous:
        if p["variance"] > 0:
            over_pct = (p["variance"] / p["target_cents"] * 100) if p["target_cents"] > 0 else 0
            suggestions.append({
                "category_path": p["category_path"],
                "budget_type": p["budget_type"],
                "amount_cents": p["amount_cents"],
                "percent": p["percent"],
                "label": p["label"],
                "reason": f"Overran by {over_pct:.0f}% (${p['variance']/100:.0f}) — carrying forward same allocation",
            })
        elif p["variance"] < 0 and abs(p["variance"]) / max(p["target_cents"], 1) > 0.2:
            under_pct = abs(p["variance"]) / p["target_cents"] * 100 if p["target_cents"] > 0 else 0
            suggestions.append({
                "category_path": p["category_path"],
                "budget_type": p["budget_type"],
                "amount_cents": p["amount_cents"],
                "percent": p["percent"],
                "label": p["label"],
                "reason": f"Underran by {under_pct:.0f}% (${abs(p['variance'])/100:.0f}) — carrying forward same allocation",
            })
        else:
            suggestions.append({
                "category_path": p["category_path"],
                "budget_type": p["budget_type"],
                "amount_cents": p["amount_cents"],
                "percent": p["percent"],
                "label": p["label"],
                "reason": "On track — same allocation carried forward",
            })
    return suggestions


def _prior_period_window(period_start: str, period_end: str, months_back: int) -> tuple:
    from datetime import datetime, timedelta
    ps = datetime.strptime(period_start, "%Y-%m-%d")
    pe = datetime.strptime(period_end, "%Y-%m-%d")
    span = pe - ps
    end = ps - timedelta(days=1)
    start = end - timedelta(days=span.days + 30 * (months_back - 1))
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


async def _category_spend_in_window(db, category_path: str, start: str, end: str) -> int:
    cursor = await db.execute(
        """SELECT COALESCE(SUM(ABS(amount_cents)), 0)
           FROM transactions
           WHERE category_path LIKE ?
             AND amount_cents < 0
             AND entity_type NOT IN ('transfer', 'excluded')
             AND posted_date >= ?
             AND posted_date <= ?""",
        (category_path + "%", start, end),
    )
    row = await cursor.fetchone()
    return row[0] if row else 0


async def _recent_txn_ids_for_category(db, category_path: str, start: str, end: str, limit: int = 10) -> list:
    cursor = await db.execute(
        """SELECT id FROM transactions
           WHERE category_path LIKE ?
             AND amount_cents < 0
             AND entity_type NOT IN ('transfer', 'excluded')
             AND posted_date >= ?
             AND posted_date <= ?
           ORDER BY posted_date DESC
           LIMIT ?""",
        (category_path + "%", start, end, limit),
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]


@router.get("/api/budgets/intelligence")
async def budget_intelligence(
    period_start: Optional[str] = Query(None),
    period_end: Optional[str] = Query(None),
    scope: Optional[str] = Query("auto"),
):
    """Compute mid-period and cross-period budget insights.

    Returns insights of kinds: over-budget-risk, recurring-waste,
    savings-opportunity, historical-anomaly. Each cites the underlying
    transactions or budget rows via the `evidence` field.
    """
    if period_start and period_end:
        ps, pe = period_start, period_end
    else:
        ps, pe = _parse_period("month")

    insights = []

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM budgets ORDER BY id")
        budget_rows = [dict(r) for r in await cursor.fetchall()]
        total_income = await _get_period_income(db, ps, pe)

        # ── over-budget-risk: pacing ahead of expected by > 15 pp, or already over.
        from datetime import datetime
        try:
            total_days = max(1, (datetime.strptime(pe, "%Y-%m-%d") - datetime.strptime(ps, "%Y-%m-%d")).days + 1)
            elapsed_days = max(0, min(total_days, (datetime.now() - datetime.strptime(ps, "%Y-%m-%d")).days + 1))
            expected_pct = (elapsed_days / total_days) * 100
        except Exception:
            expected_pct = 50.0

        for b in budget_rows:
            target = b["amount_cents"] if b["budget_type"] == "absolute" else int(total_income * (b["percent"] or 0) / 100.0)
            if not target or target <= 0:
                continue
            spent = await _get_spent_cents(db, b["category_path"], ps, pe, scope)
            pct_used = spent / target * 100
            if pct_used >= 100:
                severity = "danger"
                msg = (
                    f"{b['category_path']} is at {pct_used:.0f}% of its ${target/100:.0f} budget "
                    f"(${spent/100:.0f} spent) with {int(max(0, total_days - elapsed_days))} days left in the period."
                )
                action = f"Review the recent {b['category_path']} transactions or raise the allocation."
            elif pct_used >= expected_pct + 15:
                severity = "warn"
                msg = (
                    f"{b['category_path']} is at {pct_used:.0f}% of its ${target/100:.0f} budget — "
                    f"{pct_used - expected_pct:.0f} points ahead of the {expected_pct:.0f}% expected pace."
                )
                action = "Slow down spending in this category for the rest of the period."
            else:
                continue
            evidence_txns = await _recent_txn_ids_for_category(db, b["category_path"], ps, pe)
            insights.append({
                "kind": "over-budget-risk",
                "severity": severity,
                "category": b["category_path"],
                "message": msg,
                "suggested_action": action,
                "evidence": {"budget_ids": [b["id"]], "transaction_ids": evidence_txns},
            })

        # ── recurring-waste: subscriptions that appear in many consecutive months.
        cursor = await db.execute(
            """SELECT id, merchant, posted_date, ABS(amount_cents) AS amt
               FROM transactions
               WHERE category_path LIKE 'Expense > Personal > Subscriptions%'
                 AND amount_cents < 0
                 AND entity_type NOT IN ('transfer', 'excluded')
                 AND posted_date <= ?
               ORDER BY merchant, posted_date""",
            (pe,),
        )
        sub_rows = await cursor.fetchall()
        merchant_history: dict = {}
        for r in sub_rows:
            key = (r["merchant"] or "").strip().lower()
            if not key:
                continue
            merchant_history.setdefault(key, {"label": r["merchant"], "ids": [], "months": set(), "amounts": []})
            merchant_history[key]["ids"].append(r["id"])
            merchant_history[key]["months"].add(r["posted_date"][:7])
            merchant_history[key]["amounts"].append(r["amt"])

        for key, info in merchant_history.items():
            month_count = len(info["months"])
            if month_count < 6:
                continue
            total_spent = sum(info["amounts"])
            insights.append({
                "kind": "recurring-waste",
                "severity": "info",
                "category": "Expense > Personal > Subscriptions",
                "message": (
                    f"{info['label']} has charged in {month_count} different months "
                    f"(${total_spent/100:.0f} cumulative). Worth a review if it's still in use."
                ),
                "suggested_action": f"Audit {info['label']} — cancel if unused.",
                "evidence": {"transaction_ids": info["ids"][:15]},
            })

        # ── historical-anomaly: category's current spend > 2× trailing 6-month average.
        cursor = await db.execute(
            """SELECT category_path, COUNT(*) AS n
               FROM transactions
               WHERE category_path LIKE 'Expense%'
                 AND amount_cents < 0
                 AND entity_type NOT IN ('transfer', 'excluded')
                 AND posted_date >= ?
                 AND posted_date <= ?
               GROUP BY category_path
               HAVING n >= 1""",
            (ps, pe),
        )
        active_categories = [r["category_path"] for r in await cursor.fetchall()]

        if active_categories:
            trail_start, trail_end = _prior_period_window(ps, pe, months_back=6)
            for cat in active_categories:
                current_spend = await _category_spend_in_window(db, cat, ps, pe)
                trailing_spend = await _category_spend_in_window(db, cat, trail_start, trail_end)
                # Approximate monthly trailing average.
                trailing_avg = trailing_spend / 6.0
                if trailing_avg <= 0 or current_spend < 5000:
                    continue
                if current_spend > 2 * trailing_avg:
                    multiple = current_spend / max(trailing_avg, 1)
                    evidence_txns = await _recent_txn_ids_for_category(db, cat, ps, pe)
                    insights.append({
                        "kind": "historical-anomaly",
                        "severity": "warn",
                        "category": cat,
                        "message": (
                            f"{cat} spend is ${current_spend/100:.0f} this period — "
                            f"{multiple:.1f}× the trailing 6-month monthly average of ${trailing_avg/100:.0f}."
                        ),
                        "suggested_action": "Open the category and review the spike drivers.",
                        "evidence": {"transaction_ids": evidence_txns},
                    })

        # ── savings-opportunity: budgets where spend is consistently < 60% of target.
        for b in budget_rows:
            target = b["amount_cents"] if b["budget_type"] == "absolute" else int(total_income * (b["percent"] or 0) / 100.0)
            if not target or target <= 0:
                continue
            current_spend = await _get_spent_cents(db, b["category_path"], ps, pe, scope)
            trail_start, trail_end = _prior_period_window(ps, pe, months_back=3)
            trailing_spend = await _category_spend_in_window(db, b["category_path"], trail_start, trail_end)
            trailing_avg = trailing_spend / 3.0
            if trailing_avg > 0 and trailing_avg < target * 0.6 and current_spend < target * 0.6:
                slack = target - int(trailing_avg)
                insights.append({
                    "kind": "savings-opportunity",
                    "severity": "info",
                    "category": b["category_path"],
                    "message": (
                        f"{b['category_path']} has been averaging ${trailing_avg/100:.0f}/mo against a "
                        f"${target/100:.0f} budget. Roughly ${slack/100:.0f} could be redirected to savings."
                    ),
                    "suggested_action": "Lower the allocation or set up an automatic transfer for the slack.",
                    "evidence": {"budget_ids": [b["id"]]},
                })

        # Sort by severity (danger > warn > info), then kind for stability.
        sev_order = {"danger": 0, "warn": 1, "info": 2}
        insights.sort(key=lambda i: (sev_order.get(i["severity"], 99), i["kind"]))

        return {
            "period": {"start": ps, "end": pe},
            "insights": insights,
        }
    finally:
        await db.close()


@router.get("/api/budgets/{budget_id}")
async def get_budget(budget_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM budgets WHERE id=?", (budget_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Budget not found")
        return dict(row)
    finally:
        await db.close()


@router.put("/api/budgets/{budget_id}")
async def update_budget(budget_id: int, update: BudgetUpdate):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM budgets WHERE id=?", (budget_id,))
        existing = await cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Budget not found")

        fields = {}
        for key in ("category_path", "budget_type", "amount_cents", "percent", "period", "label"):
            val = getattr(update, key, None)
            if val is not None:
                fields[key] = val

        if not fields:
            return dict(existing)

        set_clause = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values()) + [budget_id]
        await db.execute(f"UPDATE budgets SET {set_clause} WHERE id=?", values)
        await db.commit()

        cursor = await db.execute("SELECT * FROM budgets WHERE id=?", (budget_id,))
        row = await cursor.fetchone()
        return dict(row)
    finally:
        await db.close()


@router.delete("/api/budgets/{budget_id}")
async def delete_budget(budget_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM budgets WHERE id=?", (budget_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Budget not found")
        await db.execute("DELETE FROM budgets WHERE id=?", (budget_id,))
        await db.commit()
        return {"ok": True}
    finally:
        await db.close()
