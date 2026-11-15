"""Banks & cash: every linked bank, its accounts and balances, and the cash total.

Only Plaid-linked accounts appear here. Imported CSV history has no reliable
balance, so it stays out of the cash total (the same rule net worth follows).
Cash is the sum of current balances on depository accounts (checking and
savings). Credit balances are shown separately as owed, never added to cash.
"""
import datetime
from typing import Any, Dict

from fastapi import APIRouter

from app.database import get_db

router = APIRouter()


@router.get("/api/banks")
async def list_banks() -> Dict[str, Any]:
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT i.id AS item_db_id, i.item_id, i.institution_name, i.status,
                      i.last_synced_at, a.plaid_account_id, a.canonical_account_id,
                      a.name, a.official_name, a.mask, a.type, a.subtype,
                      a.include_in_sync, a.current_balance_cents, a.available_balance_cents,
                      a.last_balance_at
               FROM plaid_items i
               JOIN plaid_accounts a ON a.item_db_id = i.id
               ORDER BY i.institution_name, i.id, a.id"""
        )
        rows = [dict(r) for r in await cur.fetchall()]

        canonical_ids = sorted({r["canonical_account_id"] for r in rows if r["canonical_account_id"]})
        counts: Dict[str, int] = {}
        if canonical_ids:
            placeholders = ",".join("?" * len(canonical_ids))
            cur = await db.execute(
                f"SELECT account_id, COUNT(*) AS n FROM transactions WHERE account_id IN ({placeholders}) GROUP BY account_id",
                canonical_ids,
            )
            counts = {r["account_id"]: r["n"] for r in await cur.fetchall()}

        plaid_ids = sorted({r["plaid_account_id"] for r in rows if r["plaid_account_id"]})
        events_by_account: Dict[str, list] = {}
        if plaid_ids:
            placeholders = ",".join("?" * len(plaid_ids))
            cur = await db.execute(
                f"""SELECT account_ref, kind, title, occurred_on, amount_cents
                    FROM campaign_events WHERE account_ref IN ({placeholders})
                    ORDER BY occurred_on DESC""",
                plaid_ids,
            )
            for r in await cur.fetchall():
                events_by_account.setdefault(r["account_ref"], []).append(
                    {"kind": r["kind"], "title": r["title"], "occurred_on": r["occurred_on"],
                     "amount_cents": r["amount_cents"]}
                )

        institutions: Dict[int, Dict[str, Any]] = {}
        cash_total = 0
        owed_total = 0
        for r in rows:
            inst = institutions.setdefault(r["item_db_id"], {
                "item_id": r["item_id"],
                "institution_name": r["institution_name"] or "Linked bank",
                "status": r["status"],
                "last_synced_at": r["last_synced_at"],
                "accounts": [],
                "total_cents": 0,
            })
            if not r["include_in_sync"]:
                continue
            balance = r["current_balance_cents"]
            kind = (r["type"] or "").lower()
            if balance is not None:
                if kind == "depository":
                    cash_total += balance
                    inst["total_cents"] += balance
                elif kind == "credit":
                    owed_total += abs(balance)
            inst["accounts"].append({
                "plaid_account_id": r["plaid_account_id"],
                "canonical_account_id": r["canonical_account_id"],
                "name": r["official_name"] or r["name"] or "Account",
                "mask": r["mask"],
                "type": r["type"],
                "subtype": r["subtype"],
                "current_balance_cents": balance,
                "available_balance_cents": r["available_balance_cents"],
                "last_balance_at": r["last_balance_at"],
                "transaction_count": counts.get(r["canonical_account_id"], 0),
                "events": events_by_account.get(r["plaid_account_id"], []),
            })

        return {
            "as_of": datetime.date.today().isoformat(),
            "cash_total_cents": cash_total,
            "owed_cents": owed_total,
            "institutions": list(institutions.values()),
        }
    finally:
        await db.close()
