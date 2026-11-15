"""Read-only integration API for BonusPilot.

BonusPilot does not link bank accounts. Every bank is linked once, here, and
BonusPilot reads the linked accounts and their transactions through this API.

Auth: a shared secret in ``INTEGRATION_TOKEN``, sent as ``Authorization: Bearer``.
If the variable is unset the whole API answers 503, so it fails closed.

Transactions are served as a change feed, not a full dump. The client keeps the
last ``next_cursor`` it saw for an item and asks for what came after it. Each
change is either an upsert (the transaction, in Plaid's shape and sign
convention) or a removal. Upserts are idempotent on the client side.
"""
import hmac
import os
import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.database import get_db
from app.services import plaid_client, plaid_sync
from app.services.crypto import is_encryption_configured

router = APIRouter(prefix="/api/integration/v1", tags=["integration"])

DEFAULT_PAGE = 500
MAX_PAGE = 500


def require_integration_token(authorization: Optional[str] = Header(default=None)) -> None:
    expected = os.environ.get("INTEGRATION_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Integration API is disabled: INTEGRATION_TOKEN is not set")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Missing or invalid bearer token")


async def _item_db_id(db, item_id: str) -> int:
    cursor = await db.execute("SELECT id FROM plaid_items WHERE item_id=?", (item_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Unknown item")
    return row[0]


def _plaid_shaped(row) -> Dict[str, Any]:
    """One stored transaction in Plaid's field names and sign (positive = money out)."""
    counterparties = []
    if row["counterparty_name"]:
        counterparties.append({"name": row["counterparty_name"]})
    return {
        "transaction_id": row["external_id"],
        "account_id": row["plaid_account_id"],
        "amount": round(-row["amount_cents"] / 100, 2),
        "iso_currency_code": row["currency"] or "USD",
        "date": row["posted_date"],
        "authorized_date": row["transaction_date"],
        "pending": bool(row["pending"]),
        "pending_transaction_id": row["pending_external_id"],
        "name": row["description"] or row["merchant"] or "",
        "merchant_name": row["merchant"] or None,
        "counterparties": counterparties,
        "personal_finance_category": {
            "primary": row["plaid_pfc_primary"],
            "detailed": row["plaid_pfc_detailed"],
        },
        "payment_channel": row["payment_channel"],
    }


@router.get("/items", dependencies=[Depends(require_integration_token)])
async def list_items() -> Dict[str, Any]:
    """Every linked institution. ``item_id`` is the handle the other endpoints take."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT item_id, institution_id, institution_name, status, error_code, last_synced_at
               FROM plaid_items ORDER BY id"""
        )
        return {"items": [dict(r) for r in await cursor.fetchall()]}
    finally:
        await db.close()


@router.get("/items/{item_id}/accounts", dependencies=[Depends(require_integration_token)])
async def item_accounts(item_id: str) -> Dict[str, Any]:
    db = await get_db()
    try:
        item_db_id = await _item_db_id(db, item_id)
        cursor = await db.execute(
            """SELECT plaid_account_id, name, official_name, mask, type, subtype,
                      canonical_account_id, include_in_sync, current_balance_cents,
                      available_balance_cents, last_balance_at
               FROM plaid_accounts WHERE item_db_id=? ORDER BY id""",
            (item_db_id,),
        )
        return {"accounts": [dict(r) for r in await cursor.fetchall()]}
    finally:
        await db.close()


@router.get("/items/{item_id}/transactions", dependencies=[Depends(require_integration_token)])
async def item_transaction_changes(
    item_id: str,
    cursor: Optional[str] = Query(default=None, description="next_cursor from the previous page; omit for the start"),
    limit: int = Query(default=DEFAULT_PAGE, ge=1, le=MAX_PAGE),
) -> Dict[str, Any]:
    """Changes to this item's transactions after ``cursor``, oldest first."""
    try:
        after = int(cursor) if cursor else 0
    except ValueError:
        raise HTTPException(status_code=400, detail="cursor must be an integer string")

    db = await get_db()
    try:
        item_db_id = await _item_db_id(db, item_id)
        cur = await db.execute(
            """SELECT seq, op, external_id FROM integration_changes
               WHERE item_db_id=? AND seq>? ORDER BY seq LIMIT ?""",
            (item_db_id, after, limit + 1),
        )
        raw = [dict(r) for r in await cur.fetchall()]
        has_more = len(raw) > limit
        page = raw[:limit]

        # Current row state for every upsert in the page. An upsert whose row
        # has since been deleted is reported as a removal instead.
        ids = sorted({c["external_id"] for c in page if c["op"] == "upsert"})
        rows_by_id: Dict[str, Any] = {}
        if ids:
            placeholders = ",".join("?" * len(ids))
            cur = await db.execute(
                f"""SELECT * FROM transactions
                    WHERE source_platform='plaid' AND external_id IN ({placeholders})""",
                ids,
            )
            rows_by_id = {r["external_id"]: r for r in await cur.fetchall()}

        changes: List[Dict[str, Any]] = []
        for change in page:
            ext = change["external_id"]
            row = rows_by_id.get(ext) if change["op"] == "upsert" else None
            if change["op"] == "upsert" and row is not None:
                changes.append({"seq": change["seq"], "op": "upsert", "transaction": _plaid_shaped(row)})
            else:
                changes.append({"seq": change["seq"], "op": "removed", "transaction_id": ext})

        next_cursor = str(page[-1]["seq"]) if page else str(after)
        return {"changes": changes, "next_cursor": next_cursor, "has_more": has_more}
    finally:
        await db.close()


class ImportItemRequest(BaseModel):
    item_id: str
    access_token: str
    institution_id: Optional[str] = None
    institution_name: Optional[str] = None


@router.post("/items/import", dependencies=[Depends(require_integration_token)])
async def import_item(req: ImportItemRequest) -> Dict[str, Any]:
    """Take over a bank connection that BonusPilot already holds.

    The access token is stored encrypted, the accounts are read, and one sync
    runs. The token is never echoed back and never put in an error message. This
    does NOT call Plaid's remove-item: the connection stays live for both apps.
    Safe to repeat for the same item.
    """
    if not is_encryption_configured():
        raise HTTPException(status_code=503, detail="ENCRYPTION_KEY is not configured")
    db = await get_db()
    try:
        item_db_id = await plaid_sync.store_item(
            db,
            item_id=req.item_id,
            access_token=req.access_token,
            institution_id=req.institution_id,
            institution_name=req.institution_name,
        )
        try:
            details = plaid_client.fetch_item_accounts(req.access_token)
        except Exception:
            raise HTTPException(status_code=502, detail="Plaid could not list this item's accounts")
        accounts = await plaid_sync.store_accounts(db, item_db_id, details.get("accounts") or [])
        try:
            sync = await plaid_sync.sync_item(db, item_db_id)
        except Exception:
            raise HTTPException(status_code=502, detail="The first sync for this item failed")
        return {
            "item_id": req.item_id,
            "account_count": len(accounts),
            "sync": {k: sync[k] for k in ("added", "updated", "removed", "skipped_accounts")},
        }
    finally:
        await db.close()


class CampaignEventIn(BaseModel):
    external_key: str = Field(min_length=1, max_length=200)
    kind: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=300)
    occurred_on: str
    amount_cents: Optional[int] = None
    account_ref: Optional[str] = Field(default=None, max_length=200)
    source: str = Field(default="bonuspilot", max_length=40)


@router.post("/campaign-events", dependencies=[Depends(require_integration_token)])
async def upsert_campaign_event(event: CampaignEventIn) -> Dict[str, Any]:
    """Store or update one campaign fact. Same external_key = same row."""
    try:
        datetime.date.fromisoformat(event.occurred_on)
    except ValueError:
        raise HTTPException(status_code=422, detail="occurred_on must be an ISO date (YYYY-MM-DD)")
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO campaign_events
                 (external_key, source, kind, title, occurred_on, amount_cents, account_ref, updated_at)
               VALUES (?,?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(external_key) DO UPDATE SET
                 source=excluded.source, kind=excluded.kind, title=excluded.title,
                 occurred_on=excluded.occurred_on, amount_cents=excluded.amount_cents,
                 account_ref=excluded.account_ref, updated_at=datetime('now')""",
            (event.external_key, event.source, event.kind, event.title,
             event.occurred_on, event.amount_cents, event.account_ref),
        )
        await db.commit()
        return {"ok": True, "external_key": event.external_key}
    finally:
        await db.close()


@router.get("/campaign-events", dependencies=[Depends(require_integration_token)])
async def list_campaign_events() -> Dict[str, Any]:
    db = await get_db()
    try:
        cur = await db.execute(
            """SELECT external_key, source, kind, title, occurred_on, amount_cents, account_ref
               FROM campaign_events ORDER BY occurred_on DESC, id DESC"""
        )
        return {"events": [dict(r) for r in await cur.fetchall()]}
    finally:
        await db.close()
