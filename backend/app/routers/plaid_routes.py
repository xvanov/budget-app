"""Plaid endpoints: link, exchange, list, sync, remap, unlink.

Nothing here ever returns an access token — encrypted or otherwise.
"""
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_db
from app.services import plaid_client, plaid_sync
from app.services.crypto import EncryptionError, decrypt_secret, is_encryption_configured
from app.services.plaid_client import PlaidConfigError

logger = logging.getLogger(__name__)

router = APIRouter()


class ExchangeRequest(BaseModel):
    public_token: str


class AccountPatch(BaseModel):
    canonical_account_id: str | None = None
    include_in_sync: bool | None = None


@router.get("/api/plaid/status")
async def plaid_status():
    """Whether Plaid is usable, plus the linked items. Safe to call always."""
    db = await get_db()
    try:
        configured = plaid_client.is_configured()
        env = None
        if configured:
            try:
                env = plaid_client.resolve_env()
            except PlaidConfigError:
                configured = False
        items = await plaid_sync.list_items(db)
        return {
            "configured": configured,
            "environment": env,
            "encryption_configured": is_encryption_configured(),
            "items": items,
            "item_count": len(items),
        }
    finally:
        await db.close()


@router.post("/api/plaid/link-token")
async def create_link_token():
    try:
        return {"link_token": plaid_client.create_link_token()}
    except PlaidConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("link-token failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Plaid link-token failed: {exc}") from exc


@router.post("/api/plaid/exchange")
async def exchange(req: ExchangeRequest):
    if not is_encryption_configured():
        # Refuse rather than store a token we cannot protect.
        raise HTTPException(
            status_code=503,
            detail="ENCRYPTION_KEY is not configured; refusing to store a Plaid access token.",
        )
    db = await get_db()
    try:
        exchanged = plaid_client.exchange_public_token(req.public_token)
        details = plaid_client.fetch_item_accounts(exchanged["access_token"])
        item_db_id = await plaid_sync.store_item(
            db,
            item_id=exchanged["item_id"],
            access_token=exchanged["access_token"],
            institution_id=details.get("institution_id"),
            institution_name=details.get("institution_id"),
        )
        accounts = await plaid_sync.store_accounts(db, item_db_id, details.get("accounts") or [])
        return {"item_db_id": item_db_id, "accounts": accounts, "account_count": len(accounts)}
    except PlaidConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except EncryptionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("exchange failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Plaid exchange failed: {exc}") from exc
    finally:
        await db.close()


@router.get("/api/plaid/items")
async def items():
    db = await get_db()
    try:
        return {"items": await plaid_sync.list_items(db)}
    finally:
        await db.close()


@router.post("/api/plaid/sync")
async def sync(item_db_id: int = 0, categorize: bool = True):
    db = await get_db()
    try:
        if item_db_id:
            return await plaid_sync.sync_item(db, item_db_id)
        return await plaid_sync.sync_all(db, categorize=categorize)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PlaidConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("sync failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Plaid sync failed: {exc}") from exc
    finally:
        await db.close()


@router.patch("/api/plaid/accounts/{account_db_id}")
async def patch_account(account_db_id: int, patch: AccountPatch):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM plaid_accounts WHERE id=?", (account_db_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Plaid account not found")
        if patch.canonical_account_id is not None:
            await db.execute(
                "UPDATE plaid_accounts SET canonical_account_id=? WHERE id=?",
                (patch.canonical_account_id, account_db_id),
            )
        if patch.include_in_sync is not None:
            await db.execute(
                "UPDATE plaid_accounts SET include_in_sync=? WHERE id=?",
                (1 if patch.include_in_sync else 0, account_db_id),
            )
        await db.commit()
        cursor = await db.execute("SELECT * FROM plaid_accounts WHERE id=?", (account_db_id,))
        return dict(await cursor.fetchone())
    finally:
        await db.close()


@router.delete("/api/plaid/items/{item_db_id}")
async def unlink(item_db_id: int, delete_transactions: bool = False):
    """Unlink an item. Transactions are KEPT unless explicitly asked otherwise."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT item_id, access_token_encrypted FROM plaid_items WHERE id=?", (item_db_id,)
        )
        item = await cursor.fetchone()
        if not item:
            raise HTTPException(status_code=404, detail="Plaid item not found")

        try:
            plaid_client.remove_item(decrypt_secret(item[1]))
        except Exception as exc:  # best-effort: still unlink locally
            logger.warning("Plaid item_remove failed (continuing): %s", exc)

        removed = 0
        if delete_transactions:
            cursor = await db.execute(
                """DELETE FROM transactions WHERE plaid_account_id IN
                   (SELECT plaid_account_id FROM plaid_accounts WHERE item_db_id=?)""",
                (item_db_id,),
            )
            removed = cursor.rowcount or 0
        await db.execute("DELETE FROM plaid_accounts WHERE item_db_id=?", (item_db_id,))
        await db.execute("DELETE FROM plaid_items WHERE id=?", (item_db_id,))
        await db.commit()
        return {"unlinked": True, "transactions_deleted": removed}
    finally:
        await db.close()
