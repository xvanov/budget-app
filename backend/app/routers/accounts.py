from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from app.database import get_db
from app.services.account_meta import (
    ALLOWED_KINDS,
    derive_display_name,
    entries_from_payload,
    load_account_meta,
    upsert_account_meta,
)
from app.services.scope import resolve_scope

router = APIRouter()

@router.get("/api/accounts")
async def get_accounts(scope: Optional[str] = "plaid"):
    """Accounts in the live scope, decorated with their stored identity.

    Scoped by default: unscoped, this lists every account the CSV era ever saw
    (a dozen ids that hold no live data, e.g. `bank-1234`), so the Transactions
    filter row offered accounts with nothing behind them. Pass scope=all to see
    the historical accounts.

    `name`/`display_name` and `kind` come from `account_meta`, resolved the same
    way `/api/net-worth` resolves them: a stored row wins, an account with no row
    yet falls back to the default derived from its id. `name` is never null —
    the Transactions account chips render it directly, so a null there would put
    a raw account id in front of the user.
    """
    db = await get_db()
    try:
        scope_sql, _params, _effective, _note = await resolve_scope(db, scope)
        meta = {m["account_id"]: m for m in await load_account_meta(db)}
        cursor = await db.execute(
            f"""SELECT account_id as id, source_platform as platform,
                      COUNT(*) as transaction_count,
                      MIN(posted_date) as min_date,
                      MAX(posted_date) as max_date
               FROM transactions t
               WHERE 1=1 {scope_sql}
               GROUP BY account_id
               ORDER BY account_id"""
        )
        accounts = []
        for row in await cursor.fetchall():
            m = meta.get(row[0]) or {}
            name = m.get("display_name") or derive_display_name(row[0])
            accounts.append({
                "id": row[0],
                "platform": row[1],
                "transaction_count": row[2],
                "min_date": row[3],
                "max_date": row[4],
                # `name` is what the UI reads; `display_name` is the column name.
                # Both are populated so neither caller has to know the other.
                "name": name,
                "display_name": name,
                "kind": m.get("kind") or "checking",
                "include_in_net_worth": bool(m.get("include_in_net_worth", True)),
            })
        return accounts
    finally:
        await db.close()


@router.get("/api/accounts/meta")
async def get_account_meta():
    """Stored identity for every known account, in display order."""
    db = await get_db()
    try:
        return {"kinds": list(ALLOWED_KINDS), "accounts": await load_account_meta(db)}
    finally:
        await db.close()


@router.post("/api/accounts/meta")
async def post_account_meta(payload: Any = Body(...)):
    """Load account identity as DATA.

    Accepts either shape:

        {"accounts": {"bank-1234": {"display_name": "Checking", "kind": "checking"}}}
        [{"account_id": "bank-1234", "display_name": "Checking", "kind": "checking"}]

    This is how private labels — and `real_estate`, which cannot be inferred
    from transaction data but changes the net-worth arithmetic — reach the app
    without being written into the repo. Upsert: named fields win, unnamed
    fields on an existing row are left alone.
    """
    db = await get_db()
    try:
        try:
            entries = entries_from_payload(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        # Reject rather than coerce: a typo'd kind silently becoming `checking`
        # would quietly change the net-worth arithmetic.
        for entry in entries:
            kind = entry.get("kind")
            if kind is not None and kind not in ALLOWED_KINDS:
                raise HTTPException(
                    status_code=400,
                    detail=f"{entry.get('account_id')}: kind must be one of {list(ALLOWED_KINDS)}",
                )
        try:
            result = await upsert_account_meta(db, entries)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {**result, "accounts": await load_account_meta(db)}
    finally:
        await db.close()


@router.patch("/api/accounts/{account_id}/meta")
@router.put("/api/accounts/{account_id}/meta")
async def patch_account_meta(account_id: str, payload: dict = Body(...)):
    """Rename or reclassify one account — the UI path, so nobody edits code."""
    allowed = {"display_name", "kind", "institution", "include_in_net_worth", "sort_order"}
    unknown = set(payload) - allowed - {"name", "account_id"}
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported field(s): {sorted(unknown)}")
    if payload.get("kind") is not None and payload["kind"] not in ALLOWED_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"kind must be one of {list(ALLOWED_KINDS)}",
        )
    entry = {k: v for k, v in payload.items() if k in allowed}
    if "name" in payload and "display_name" not in entry:
        entry["display_name"] = payload["name"]
    if not entry:
        raise HTTPException(status_code=400, detail="Nothing to update")
    entry["account_id"] = account_id

    db = await get_db()
    try:
        await upsert_account_meta(db, [entry])
        for m in await load_account_meta(db):
            if m["account_id"] == account_id:
                return m
        raise HTTPException(status_code=404, detail="Account not found")
    finally:
        await db.close()
