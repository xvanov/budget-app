from fastapi import APIRouter, Query
from typing import Optional
from datetime import datetime, date
from app.database import get_db
from app.services.account_meta import load_account_meta

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


async def _get_mortgage_balance(db):
    cursor = await db.execute(
        "SELECT ending_balance_cents, month FROM amortization ORDER BY month ASC LIMIT 1"
    )
    row = await cursor.fetchone()
    if row:
        return row["ending_balance_cents"]
    return 0


async def _get_amortization_by_month(db):
    cursor = await db.execute(
        "SELECT month, ending_balance_cents, principal_cents FROM amortization ORDER BY month"
    )
    rows = await cursor.fetchall()
    return {r["month"][:7]: {
        "balance": r["ending_balance_cents"],
        "principal": r["principal_cents"],
    } for r in rows}


async def _compute_account_balances(db):
    cursor = await db.execute(
        "SELECT account_id, SUM(amount_cents) as balance FROM transactions GROUP BY account_id"
    )
    rows = await cursor.fetchall()
    return {r["account_id"]: r["balance"] for r in rows}


async def _compute_current_balances(db):
    balances = await _compute_account_balances(db)
    mortgage_balance = await _get_mortgage_balance(db)

    estimated_property_value = int(max(mortgage_balance * 2.0, 30000000))

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

    return accounts, assets, liabilities, estimated_property_value, mortgage_balance


@router.get("/api/net-worth")
async def get_net_worth():
    db = await get_db()
    try:
        accounts, assets, liabilities, prop_val, mortgage_bal = await _compute_current_balances(db)
        total_net_worth = assets - liabilities

        history = await _compute_history(db, prop_val, mortgage_bal)

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
        accounts, assets, liabilities, prop_val, mortgage_bal = await _compute_current_balances(db)
        history = await _compute_history(db, prop_val, mortgage_bal)

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


async def _compute_history(db, prop_val, current_mortgage):
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

    amort_by_month = await _get_amortization_by_month(db)

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
    for m in amort_by_month:
        if m <= today_str:
            all_months_raw.add(m)
    all_months = sorted(all_months_raw)

    accounts_meta = await _net_worth_accounts(db)
    included = {a["account_id"] for a in accounts_meta}

    cumulative = {}
    history = []

    for m in all_months:
        month_data = monthly_flows.get(m, {})
        for aid, flow in month_data.items():
            if aid not in included:
                continue
            if aid not in cumulative:
                cumulative[aid] = 0
            cumulative[aid] += flow

        assets_m = 0
        liabilities_m = 0

        for meta in accounts_meta:
            cum_bal = cumulative.get(meta["account_id"], 0)

            if meta["kind"] == "real_estate":
                amort_info = amort_by_month.get(m, {})
                bal = amort_info.get("balance", current_mortgage) if amort_info else current_mortgage
                assets_m += prop_val
                liabilities_m += bal
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
