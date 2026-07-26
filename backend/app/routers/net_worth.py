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


# Plaid's account `type` vocabulary mapped onto this app's `kind`. Leaking
# Plaid's own words through would put "depository" on screen and break the kind
# whitelist every consumer checks against.
_APP_KIND = {
    "depository": "checking",
    "credit": "credit",
    "investment": "brokerage",
    "brokerage": "brokerage",
    "loan": "credit",
}


async def _plaid_balances(db):
    """The balances Plaid actually reports, per canonical account.

    This is the only trustworthy balance in the database. Summing transaction
    amounts does NOT give one: it gives the cumulative flow over whatever window
    happens to be imported, which for these accounts is off by six figures
    (chase-3600 sums to -$315,523 while Plaid reports +$14,356.70, because the
    import starts mid-life and no opening balance is stored).

    Sign convention of this module: positive is an asset, negative is money owed.
    Plaid reports a card balance as a POSITIVE amount OWED, so credit accounts
    are negated here — getting that backwards turns debt into savings.
    """
    balances = {}
    try:
        cursor = await db.execute(
            """SELECT canonical_account_id AS aid, current_balance_cents AS cents,
                      type AS ptype, name
               FROM plaid_accounts
               WHERE current_balance_cents IS NOT NULL AND include_in_sync = 1"""
        )
    except Exception as exc:  # table absent on a pre-Plaid database
        logger.warning("Net worth: no Plaid balances available (%s)", exc)
        return balances
    for row in await cursor.fetchall():
        cents = int(row["cents"] or 0)
        if (row["ptype"] or "").strip().lower() == "credit":
            cents = -abs(cents)
        balances[row["aid"]] = {"cents": cents, "name": row["name"],
                                "kind": (row["ptype"] or "").strip().lower()}
    return balances


async def _compute_account_balances(db):
    """Cumulative flow per account, per scope.

    Kept only as the fallback for accounts Plaid does not cover, and split by
    scope because the CSV history and its Plaid twins are BOTH in this table:
    an unscoped sum counts every twinned transaction twice. `live` is the
    plaid+manual scope (see services/scope.py), `all` is everything.
    """
    cursor = await db.execute(
        """SELECT account_id,
                  SUM(amount_cents) AS all_cents,
                  SUM(CASE WHEN source_platform IN ('plaid','manual')
                           THEN amount_cents ELSE 0 END) AS live_cents,
                  SUM(CASE WHEN source_platform IN ('plaid','manual')
                           THEN 1 ELSE 0 END) AS live_rows
           FROM transactions GROUP BY account_id"""
    )
    out = {}
    for r in await cursor.fetchall():
        # An account with live rows is deduped to those rows; one without them
        # has only history, so its history is all there is.
        out[r["account_id"]] = {
            "cents": r["live_cents"] if r["live_rows"] else r["all_cents"],
            "deduped": bool(r["live_rows"]),
        }
    return out


async def _compute_current_balances(db):
    balances = await _compute_account_balances(db)
    mortgage_balance, property_value, mortgage_by_month, first_mortgage_month = (
        await _mortgage_position(db)
    )

    # No stated purchase price means no defensible number, so the property line
    # carries 0 and says so rather than inventing a valuation.
    estimated_property_value = int(property_value or 0)
    property_basis = "stated" if property_value else "unknown"

    plaid = await _plaid_balances(db)

    def _line(aid, name, kind):
        """One account line, or None if its balance is not actually known.

        Only a LINKED account has a balance. For anything else the database holds
        the net of whatever transactions were imported, which is not a balance and
        must not be presented as one — the owner's instruction is explicit: "don't
        take into account accounts that aren't connected via Plaid… you shouldn't
        be making up things." So an unlinked account is omitted from net worth
        entirely and reported separately under ``unlinked_accounts``, where it can
        be seen without contaminating the total.
        """
        entry = plaid.get(aid)
        if entry is None:
            return None
        return {"id": aid, "name": name, "kind": kind,
                "balance": entry["cents"], "balance_basis": "plaid"}

    accounts = []
    unlinked = []
    for meta in await _net_worth_accounts(db):
        aid = meta["account_id"]
        if meta["kind"] == "real_estate":
            # The house and the bank account are two different things. They were
            # collapsed into one line, so the property value REPLACED the
            # account's cash instead of being counted alongside it — the cash
            # simply vanished from net worth.
            accounts.append({
                "id": f"{aid}::property",
                "name": meta["display_name"],
                "kind": "real_estate",
                "balance": estimated_property_value,
                "value_basis": property_basis,
                "debt": mortgage_balance,
            })
            entry = plaid.get(aid)
            cash_name = (entry or {}).get("name") or f"Account {aid}"
            line = _line(aid, cash_name, _APP_KIND.get((entry or {}).get("kind"), "checking"))
        else:
            line = _line(aid, meta["display_name"], meta["kind"])

        if line is not None:
            accounts.append(line)
        else:
            # Listed, not counted. Naming them is the honest alternative to either
            # inventing a balance or pretending the account does not exist.
            unlinked.append({"id": aid, "name": meta["display_name"], "kind": meta["kind"]})

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
            mortgage_by_month, first_mortgage_month, unlinked)


def _balance_confidence(accounts, unlinked):
    """What the total is built from, and what was deliberately left out of it.

    Every line in the total is now a balance the bank reported, or the owner's
    stated purchase price for the property. Unlinked accounts are NOT estimated
    into the total: the database has no opening balance for them, so the only
    honest options were to omit them or to invent a figure, and the owner asked
    for the former in as many words.
    """
    return {
        "lines_from_real_balances": sum(
            1 for a in accounts if a.get("balance_basis") == "plaid"),
        "lines_estimated_from_flows": 0,
        "excluded_unlinked_accounts": [a["name"] for a in unlinked],
        "note": (
            "Every balance here is reported by the bank, plus the stated purchase "
            "price of the property."
            + (f" {len(unlinked)} account(s) not connected to Plaid are excluded "
               "entirely — no balance for them exists in the data, so counting them "
               "would mean inventing one." if unlinked else "")
        ),
    }


@router.get("/api/net-worth")
async def get_net_worth():
    db = await get_db()
    try:
        (accounts, assets, liabilities, prop_val, mortgage_bal,
         mortgage_by_month, first_mortgage_month, unlinked) = await _compute_current_balances(db)
        total_net_worth = assets - liabilities

        history = await _compute_history(db, accounts,
                                        mortgage_by_month, first_mortgage_month)

        return {
            "as_of": date.today().isoformat(),
            "accounts": accounts,
            "assets": assets,
            "liabilities": liabilities,
            "total_net_worth_cents": total_net_worth,
            "balance_confidence": _balance_confidence(accounts, unlinked),
            "unlinked_accounts": unlinked,
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
         mortgage_by_month, first_mortgage_month, unlinked) = await _compute_current_balances(db)
        history = await _compute_history(db, accounts,
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


async def _compute_history(db, accounts, mortgage_by_month=None, first_mortgage_month=None):
    """Net worth month by month, anchored on the SAME balances the headline uses.

    This used to run its own unscoped ``SUM(amount_cents) GROUP BY account_id``,
    which meant the chart and the KPI above it were computed two different ways.
    Once the headline moved to bank-reported balances they disagreed by
    $211,071.90 — the chart still carried the un-deduped CSV+Plaid flow sums as
    liabilities and omitted the residence account's cash entirely. A chart that
    contradicts the number printed above it is worse than no chart.

    So the direction is reversed: start from today's balance for each account and
    walk BACKWARDS, undoing each month's flows. The last point therefore equals
    the headline by construction, and earlier points are that balance less what
    has happened since.

    One honest limitation: for an account whose imported history starts mid-life,
    back-casting past its first transaction yields a flat line at its
    then-opening balance, because no earlier data exists. That is a gap in the
    data, not an artefact of the arithmetic.
    """
    mortgage_by_month = mortgage_by_month or {}

    cash_lines = [a for a in accounts if not str(a["id"]).endswith("::property")]
    property_lines = [a for a in accounts if str(a["id"]).endswith("::property")]
    if not cash_lines and not property_lines:
        return []

    # Which accounts are deduped to their live rows — the same rule the balances
    # used, so the flows we undo are the flows that produced them.
    cursor = await db.execute(
        "SELECT DISTINCT account_id FROM transactions "
        "WHERE source_platform IN ('plaid','manual')"
    )
    live_accounts = {r[0] for r in await cursor.fetchall()}

    cursor = await db.execute(
        """SELECT account_id, substr(posted_date, 1, 7) AS month,
                  SUM(amount_cents) AS all_cents,
                  SUM(CASE WHEN source_platform IN ('plaid','manual')
                           THEN amount_cents ELSE 0 END) AS live_cents
           FROM transactions
           GROUP BY account_id, substr(posted_date, 1, 7)"""
    )
    flows = {}
    months_seen = set()
    for row in await cursor.fetchall():
        aid, month = row["account_id"], row["month"]
        if not month:
            continue
        months_seen.add(month)
        cents = row["live_cents"] if aid in live_accounts else row["all_cents"]
        flows.setdefault(aid, {})[month] = cents

    today_month = date.today().strftime("%Y-%m")
    months_seen |= {m for m in mortgage_by_month if m <= today_month}
    months = sorted(m for m in months_seen if m <= today_month)
    if not months:
        return []

    # Balance at the END of each month, walking back from today.
    running = {a["id"]: int(a["balance"] or 0) for a in cash_lines}
    balance_by_month = {}
    for month in reversed(months):
        balance_by_month[month] = dict(running)
        for aid in running:
            running[aid] -= (flows.get(aid) or {}).get(month, 0)

    # Mortgage owed at each month end, carried forward between payments.
    mortgage_running = None
    owed_by_month = {}
    for month in months:
        if month in mortgage_by_month:
            mortgage_running = mortgage_by_month[month]
        owed_by_month[month] = mortgage_running

    history = []
    for month in months:
        assets_m = 0
        liabilities_m = 0
        balances = balance_by_month.get(month, {})

        for line in cash_lines:
            bal = balances.get(line["id"], 0)
            if bal >= 0:
                assets_m += bal
            else:
                liabilities_m += abs(bal)

        for line in property_lines:
            owned = first_mortgage_month is None or month >= first_mortgage_month
            if not owned:
                continue
            assets_m += int(line["balance"] or 0)
            owed = owed_by_month.get(month)
            liabilities_m += owed if owed is not None else int(line.get("debt") or 0)

        history.append({
            "date": month + "-01",
            "value": assets_m - liabilities_m,
            "assets": assets_m,
            "liabilities": liabilities_m,
        })

    return history
