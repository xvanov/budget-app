import logging
from fastapi import APIRouter, Query
from typing import Optional
from datetime import datetime, date
from app.database import get_db
from app.services.account_meta import load_account_meta

logger = logging.getLogger(__name__)

router = APIRouter()


async def _net_worth_accounts(db):
    """The accounts net worth is computed over, in display order.

    Identity (name and `kind`) is stored in `account_meta`, not in this module:
    `kind` decides whether a balance is an asset, a liability, or a property
    line with its own mortgage, and `real_estate` in particular is a judgement
    only the account's owner can make. Accounts flagged out of net worth
    (paystub components, hand-entered rows) are dropped here.
    """
    return [a for a in await load_account_meta(db) if a["include_in_net_worth"]]


async def _mortgage_position(db):
    """The real mortgage position: what is owed now, and month by month.

    This used to read the FIRST ROW of the ``amortization`` table for both. That
    table is the lender's forward PROJECTION, issued mid-life after a large
    curtailment, so its opening balance is a mid-loan figure — not what is owed
    and not what was borrowed. Two consequences were visible on the page: a
    $153,538.15 liability on a loan that has been paid off since 2026-07-08, and
    a history that carried that same debt back to 2023-12, years before the loan
    existed.

    ``services.mortgage.analyze`` derives the balance from the actual payment
    stream against the owner's stated terms, so a closed loan reports zero. The
    projection survives only as the fallback for a loan we cannot derive (no
    stated terms and no payments), where a stale anchor still beats nothing.

    Returns ``(balance_cents, property_value_cents, by_month)``, where
    ``by_month`` maps ``YYYY-MM`` to the balance owed at the END of that month
    and starts at the month of the first payment — months before that get no
    property and no debt, which is what "we did not own it yet" means.
    """
    from app.services import mortgage as mortgage_service

    balance_cents = None
    property_value = None
    by_month: dict = {}
    first_month = None

    try:
        analysis = await mortgage_service.analyze(db)
    except Exception as exc:  # a broken loan must not take the whole page down
        logger.warning("Net worth: mortgage analysis unavailable (%s)", exc)
        analysis = None

    if analysis:
        figures = analysis.get("figures") or {}
        balance_cents = (figures.get("final_balance") or {}).get("cents")
        property_value = (figures.get("purchase_price") or {}).get("cents")
        first_payment = analysis.get("first_payment_date")
        first_month = first_payment[:7] if first_payment else None

        # Balance after each real payment, keyed by month. Several payments can
        # land in one month (a monthly ACH plus a curtailment); the last one wins
        # because it is what is owed when the month ends.
        for row in analysis.get("timeline") or []:
            month = (row.get("posted_date") or "")[:7]
            after = row.get("balance_after_cents")
            if month and after is not None:
                by_month[month] = after

    if balance_cents is None:
        cursor = await db.execute(
            "SELECT ending_balance_cents FROM amortization ORDER BY month ASC LIMIT 1"
        )
        row = await cursor.fetchone()
        balance_cents = row["ending_balance_cents"] if row else 0

    # Property value is a stated fact or it is nothing. The old
    # `max(balance * 2, $300,000)` was arithmetic dressed up as an estimate: it
    # moved every time a payment landed, and on a paid-off loan it collapses to
    # the $300,000 floor regardless of what the house is worth.
    return int(balance_cents or 0), property_value, by_month, first_month


async def _compute_account_balances(db):
    cursor = await db.execute(
        "SELECT account_id, SUM(amount_cents) as balance FROM transactions GROUP BY account_id"
    )
    rows = await cursor.fetchall()
    return {r["account_id"]: r["balance"] for r in rows}


async def _compute_current_balances(db):
    balances = await _compute_account_balances(db)
    mortgage_balance, property_value, mortgage_by_month, first_mortgage_month = (
        await _mortgage_position(db)
    )

    # No stated purchase price means no defensible number, so the property line
    # carries 0 and says so rather than inventing a valuation.
    estimated_property_value = int(property_value or 0)
    property_basis = "stated" if property_value else "unknown"

    accounts = []
    for meta in await _net_worth_accounts(db):
        aid = meta["account_id"]
        bal = balances.get(aid, 0)
        if meta["kind"] == "real_estate":
            accounts.append({
                "id": aid,
                "name": meta["display_name"],
                "kind": "real_estate",
                "balance": estimated_property_value,
                "value_basis": property_basis,
                "debt": mortgage_balance,
            })
        else:
            accounts.append({
                "id": aid,
                "name": meta["display_name"],
                "kind": meta["kind"],
                "balance": bal,
            })

    assets = 0
    liabilities = 0
    for a in accounts:
        if a["kind"] == "real_estate":
            assets += a["balance"]
            liabilities += a.get("debt", 0)
        elif a["kind"] == "credit":
            if a["balance"] < 0:
                liabilities += abs(a["balance"])
            else:
                assets += a["balance"]
        else:
            if a["balance"] >= 0:
                assets += a["balance"]
            else:
                liabilities += abs(a["balance"])

    return (accounts, assets, liabilities, estimated_property_value, mortgage_balance,
            mortgage_by_month, first_mortgage_month)


@router.get("/api/net-worth")
async def get_net_worth():
    db = await get_db()
    try:
        (accounts, assets, liabilities, prop_val, mortgage_bal,
         mortgage_by_month, first_mortgage_month) = await _compute_current_balances(db)
        total_net_worth = assets - liabilities

        history = await _compute_history(db, prop_val, mortgage_bal,
                                        mortgage_by_month, first_mortgage_month)

        return {
            "as_of": date.today().isoformat(),
            "accounts": accounts,
            "assets": assets,
            "liabilities": liabilities,
            "total_net_worth_cents": total_net_worth,
            "history": history,
        }
    finally:
        await db.close()


@router.get("/api/net-worth/history")
async def get_net_worth_history(
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    db = await get_db()
    try:
        (accounts, assets, liabilities, prop_val, mortgage_bal,
         mortgage_by_month, first_mortgage_month) = await _compute_current_balances(db)
        history = await _compute_history(db, prop_val, mortgage_bal,
                                        mortgage_by_month, first_mortgage_month)

        if start:
            history = [h for h in history if h["date"] >= start[:7] + "-01"]
        if end:
            history = [h for h in history if h["date"] <= end[:7] + "-01"]

        return {
            "history": history,
            "as_of": date.today().isoformat(),
        }
    finally:
        await db.close()


async def _compute_history(db, prop_val, current_mortgage,
                           mortgage_by_month=None, first_mortgage_month=None):
    cursor = await db.execute(
        "SELECT MIN(posted_date) as min_d, MAX(posted_date) as max_d FROM transactions"
    )
    row = await cursor.fetchone()
    if not row or not row["min_d"]:
        return []

    min_date_str = row["min_d"]
    max_date_str = row["max_d"]

    try:
        start_month = datetime.strptime(min_date_str[:7], "%Y-%m")
    except ValueError:
        start_month = datetime(2024, 1, 1)
    try:
        end_month = datetime.strptime(max_date_str[:7], "%Y-%m")
    except ValueError:
        end_month = datetime.now()

    # Balance owed at the end of each month, from the REAL payment stream. Months
    # between payments inherit the last known balance (carried forward below);
    # months before the first payment get no property and no debt.
    mortgage_by_month = mortgage_by_month or {}

    cursor = await db.execute("""
        SELECT account_id,
               substr(posted_date, 1, 7) as month,
               SUM(amount_cents) as flow
        FROM transactions
        GROUP BY account_id, substr(posted_date, 1, 7)
        ORDER BY month
    """)
    flow_rows = await cursor.fetchall()

    monthly_flows = {}
    for r in flow_rows:
        m = r["month"]
        if m not in monthly_flows:
            monthly_flows[m] = {}
        monthly_flows[m][r["account_id"]] = r["flow"]

    today_str = date.today().strftime("%Y-%m")
    all_months_raw = set(r["month"] for r in flow_rows)
    for m in mortgage_by_month:
        if m <= today_str:
            all_months_raw.add(m)
    all_months = sorted(all_months_raw)

    accounts_meta = await _net_worth_accounts(db)
    included = {a["account_id"] for a in accounts_meta}

    cumulative = {}
    history = []
    # Carried forward between payment months. Starts at None so that months
    # before the loan existed report no debt instead of today's figure.
    running_mortgage = None

    for m in all_months:
        month_data = monthly_flows.get(m, {})
        for aid, flow in month_data.items():
            if aid not in included:
                continue
            if aid not in cumulative:
                cumulative[aid] = 0
            cumulative[aid] += flow

        # A month with a payment sets the balance; a month without one keeps the
        # previous balance, which is what "nothing was paid" means.
        if m in mortgage_by_month:
            running_mortgage = mortgage_by_month[m]

        assets_m = 0
        liabilities_m = 0

        for meta in accounts_meta:
            cum_bal = cumulative.get(meta["account_id"], 0)

            if meta["kind"] == "real_estate":
                # The house is only an asset from the month the loan starts —
                # before that the history was claiming both the property and its
                # mortgage years before either existed.
                owned = first_mortgage_month is None or m >= first_mortgage_month
                if not owned:
                    continue
                assets_m += prop_val
                liabilities_m += running_mortgage if running_mortgage is not None else 0
            elif meta["kind"] == "credit":
                if cum_bal < 0:
                    liabilities_m += abs(cum_bal)
                else:
                    assets_m += cum_bal
            else:
                if cum_bal >= 0:
                    assets_m += cum_bal
                else:
                    liabilities_m += abs(cum_bal)

        nw = assets_m - liabilities_m
        history.append({
            "date": m + "-01",
            "value": nw,
            "assets": assets_m,
            "liabilities": liabilities_m,
        })

    return history
