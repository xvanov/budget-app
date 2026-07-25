"""Plaid item storage and cursor-based transaction sync.

Port of ``rental-management/src/lib/integrations/plaid.ts::syncItem``, adapted to
this repo's transaction schema.

Design decisions worth knowing:

* **Plaid is the live source; CSV rows are history.** Per the owner directive,
  the two are kept in separate scopes rather than reconciled. Plaid rows carry
  ``source_platform='plaid'``; the pre-existing CSV rows keep theirs. There is
  deliberately NO between-account transfer matching here — that machinery was a
  persistent source of wrong numbers and is out of scope.
* **Dedup within Plaid still matters** and is exact, not fuzzy:
  ``external_id`` (Plaid's transaction_id) is UNIQUE, and a pending row is
  upgraded in place when it posts under a new id via ``pending_transaction_id``.
* **Amount sign is inverted from Plaid's convention.** Plaid reports a purchase
  as POSITIVE; this repo stores outflows as NEGATIVE. Getting this backwards
  turns every expense into income, so it is asserted in the tests.
* **Categorization after a sync is bounded to the new rows.** An unbounded pass
  fires one LLM call per uncategorized row (see AGENTS.md).
"""
import json
import logging
from typing import Any, Dict, List, Optional

from app.parsers.base import BaseParser
from app.services.crypto import decrypt_secret, encrypt_secret
from app.services import plaid_client

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "ACTIVE"
STATUS_LOGIN_REQUIRED = "LOGIN_REQUIRED"
STATUS_ERROR = "ERROR"


def canonical_account_for_mask(mask: Optional[str], existing_accounts: List[str]) -> str:
    """Map a Plaid account mask onto this app's account_id convention.

    Accounts imported from CSV are already keyed by their mask (``bank-1234``),
    so a newly linked account whose mask matches one of them adopts that id and
    lines up with the existing history instead of creating a parallel account.
    """
    if not mask:
        return "plaid-unknown"
    for account_id in existing_accounts:
        if mask in (account_id or ""):
            return account_id
    return f"plaid-{mask}"


def plaid_transaction_to_row(
    txn: Dict[str, Any],
    account_id: str,
    source_file: str = "plaid",
) -> Dict[str, Any]:
    """Map one Plaid transaction onto this repo's canonical row shape.

    Sign: Plaid uses positive for money leaving the account; this repo uses
    negative. Hence the negation — the single most consequential line here.
    """
    amount = txn.get("amount")
    amount_cents = int(round(float(amount or 0) * 100))
    merchant = (
        txn.get("merchant_name")
        or (txn.get("counterparties") or [{}])[0].get("name")
        or txn.get("name")
        or ""
    )
    description = txn.get("original_description") or txn.get("name") or ""
    return {
        "external_id": txn.get("transaction_id"),
        "source_platform": "plaid",
        "source_file": source_file,
        "account_id": account_id,
        "plaid_account_id": txn.get("account_id"),
        "posted_date": str(txn.get("date") or "")[:10],
        "transaction_date": str(txn.get("authorized_date") or txn.get("date") or "")[:10] or None,
        "amount_cents": -amount_cents,  # Plaid positive = outflow -> negative here
        "currency": txn.get("iso_currency_code") or "USD",
        "merchant": merchant,
        "description": description,
        "normalized_description": BaseParser.normalize_description(description),
        "pending": 1 if txn.get("pending") else 0,
        "pending_transaction_id": txn.get("pending_transaction_id"),
    }


async def list_items(db) -> List[Dict[str, Any]]:
    cursor = await db.execute(
        """SELECT id, item_id, institution_id, institution_name, status, cursor,
                  last_synced_at, error_code, created_at
           FROM plaid_items ORDER BY id"""
    )
    items = [dict(r) for r in await cursor.fetchall()]
    for item in items:
        cursor = await db.execute(
            """SELECT id, plaid_account_id, name, official_name, mask, type, subtype,
                      canonical_account_id, include_in_sync, current_balance_cents,
                      available_balance_cents, last_balance_at
               FROM plaid_accounts WHERE item_db_id=? ORDER BY id""",
            (item["id"],),
        )
        item["accounts"] = [dict(r) for r in await cursor.fetchall()]
        item["has_cursor"] = bool(item.pop("cursor", None))
    return items


async def store_item(db, item_id: str, access_token: str, institution_id: Optional[str],
                     institution_name: Optional[str]) -> int:
    """Persist an item with its access token ENCRYPTED. Never plaintext."""
    ciphertext = encrypt_secret(access_token)
    await db.execute(
        """INSERT INTO plaid_items (item_id, access_token_encrypted, institution_id, institution_name)
           VALUES (?,?,?,?)
           ON CONFLICT(item_id) DO UPDATE SET
             access_token_encrypted=excluded.access_token_encrypted,
             institution_id=excluded.institution_id,
             institution_name=excluded.institution_name,
             status='ACTIVE', error_code=NULL""",
        (item_id, ciphertext, institution_id, institution_name),
    )
    await db.commit()
    cursor = await db.execute("SELECT id FROM plaid_items WHERE item_id=?", (item_id,))
    row = await cursor.fetchone()
    return row[0]


async def store_accounts(db, item_db_id: int, accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cursor = await db.execute("SELECT DISTINCT account_id FROM transactions")
    existing = [r[0] for r in await cursor.fetchall() if r[0]]

    stored = []
    for account in accounts:
        mask = account.get("mask")
        canonical = canonical_account_for_mask(mask, existing)
        balances = account.get("balances") or {}

        def cents(value):
            return None if value is None else int(round(float(value) * 100))

        await db.execute(
            """INSERT INTO plaid_accounts
               (item_db_id, plaid_account_id, name, official_name, mask, type, subtype,
                canonical_account_id, current_balance_cents, available_balance_cents,
                last_balance_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'))
               ON CONFLICT(plaid_account_id) DO UPDATE SET
                 name=excluded.name, official_name=excluded.official_name,
                 mask=excluded.mask, type=excluded.type, subtype=excluded.subtype,
                 current_balance_cents=excluded.current_balance_cents,
                 available_balance_cents=excluded.available_balance_cents,
                 last_balance_at=datetime('now')""",
            (
                item_db_id, account.get("account_id"), account.get("name"),
                account.get("official_name"), mask, str(account.get("type") or ""),
                str(account.get("subtype") or ""), canonical,
                cents(balances.get("current")), cents(balances.get("available")),
            ),
        )
        stored.append({"plaid_account_id": account.get("account_id"), "mask": mask,
                       "canonical_account_id": canonical})
    await db.commit()
    return stored


async def _account_map(db, item_db_id: int) -> Dict[str, str]:
    cursor = await db.execute(
        "SELECT plaid_account_id, canonical_account_id, include_in_sync "
        "FROM plaid_accounts WHERE item_db_id=?",
        (item_db_id,),
    )
    return {
        r[0]: r[1]
        for r in await cursor.fetchall()
        if r[2]  # excluded accounts drop out of sync entirely
    }


async def _upsert_transaction(db, row: Dict[str, Any]) -> str:
    """Insert or update one Plaid row. Returns 'added' | 'updated'.

    Exact identity only: the Plaid transaction_id, or the pending id it replaces.

    The insert is a single atomic UPSERT rather than SELECT-then-INSERT. Two
    concurrent syncs of the same item (a manual "Sync now" overlapping a
    scheduled or scripted one) both passed the SELECT and then both inserted,
    and the second died on `UNIQUE constraint failed: transactions.external_id`
    — leaving the item marked ERROR even though the data was intact. ON CONFLICT
    makes a re-sync of the same transaction idempotent instead of fatal.
    """
    # A pending row that has now posted arrives under a NEW transaction_id and
    # points back at the old one, so retarget that row before upserting.
    if row.get("pending_transaction_id"):
        cursor = await db.execute(
            "SELECT id FROM transactions WHERE external_id=?",
            (row["pending_transaction_id"],),
        )
        pending_row = await cursor.fetchone()
        if pending_row:
            await db.execute(
                """UPDATE transactions
                   SET external_id=?, posted_date=?, transaction_date=?, amount_cents=?,
                       merchant=?, description=?, normalized_description=?, pending=?,
                       plaid_account_id=?, account_id=?
                   WHERE id=?""",
                (
                    row["external_id"], row["posted_date"], row["transaction_date"],
                    row["amount_cents"], row["merchant"], row["description"],
                    row["normalized_description"], row["pending"], row["plaid_account_id"],
                    row["account_id"], pending_row[0],
                ),
            )
            return "updated"

    # Existence check is for REPORTING only (added vs updated counts).
    # Correctness no longer depends on it — the UPSERT below is atomic, so a
    # concurrent sync that wins this race produces an update, not a crash.
    cursor = await db.execute(
        "SELECT 1 FROM transactions WHERE external_id=?", (row["external_id"],)
    )
    already_present = (await cursor.fetchone()) is not None

    await db.execute(
        """INSERT INTO transactions
           (external_id, source_platform, source_file, account_id, plaid_account_id,
            posted_date, transaction_date, amount_cents, currency, merchant, description,
            normalized_description, pending)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(external_id) WHERE external_id IS NOT NULL DO UPDATE SET
             posted_date=excluded.posted_date,
             transaction_date=excluded.transaction_date,
             amount_cents=excluded.amount_cents,
             merchant=excluded.merchant,
             description=excluded.description,
             normalized_description=excluded.normalized_description,
             pending=excluded.pending,
             plaid_account_id=excluded.plaid_account_id,
             account_id=excluded.account_id""",
        (
            row["external_id"], row["source_platform"], row["source_file"], row["account_id"],
            row["plaid_account_id"], row["posted_date"], row["transaction_date"],
            row["amount_cents"], row["currency"], row["merchant"], row["description"],
            row["normalized_description"], row["pending"],
        ),
    )
    return "updated" if already_present else "added"


async def sync_item(db, item_db_id: int, client=None) -> Dict[str, Any]:
    """Pull new/changed transactions for one stored item.

    ``client`` is injectable so tests drive canned transactions/sync pages
    without touching the network.
    """
    client = client or plaid_client
    cursor = await db.execute(
        "SELECT id, item_id, access_token_encrypted, cursor FROM plaid_items WHERE id=?",
        (item_db_id,),
    )
    item = await cursor.fetchone()
    if not item:
        raise ValueError(f"plaid_item {item_db_id} not found")
    item = dict(item)

    access_token = decrypt_secret(item["access_token_encrypted"])
    account_map = await _account_map(db, item_db_id)
    cursor_value = item.get("cursor")
    result = {"added": 0, "updated": 0, "removed": 0, "skipped_accounts": 0, "new_ids": []}

    try:
        has_more = True
        while has_more:
            page = client.transactions_sync(access_token, cursor_value)
            for txn in list(page.get("added") or []) + list(page.get("modified") or []):
                account_id = account_map.get(txn.get("account_id"))
                if not account_id:
                    result["skipped_accounts"] += 1
                    continue
                row = plaid_transaction_to_row(txn, account_id, source_file=f"plaid:{item['item_id']}")
                outcome = await _upsert_transaction(db, row)
                result[outcome] += 1
                if outcome == "added":
                    cur = await db.execute("SELECT last_insert_rowid()")
                    result["new_ids"].append((await cur.fetchone())[0])

            for removed in page.get("removed") or []:
                txn_id = removed.get("transaction_id")
                if not txn_id:
                    continue
                await db.execute("DELETE FROM transactions WHERE external_id=?", (txn_id,))
                result["removed"] += 1

            cursor_value = page.get("next_cursor")
            has_more = bool(page.get("has_more"))

        await db.execute(
            "UPDATE plaid_items SET cursor=?, last_synced_at=datetime('now'), status=?, "
            "error_code=NULL WHERE id=?",
            (cursor_value, STATUS_ACTIVE, item_db_id),
        )
        await db.commit()
    except Exception as exc:
        code = plaid_client.error_code_of(exc)
        status = STATUS_LOGIN_REQUIRED if code == "ITEM_LOGIN_REQUIRED" else STATUS_ERROR
        # Keep the existing cursor: losing it would force a full re-pull.
        await db.execute(
            "UPDATE plaid_items SET status=?, error_code=? WHERE id=?",
            (status, str(code), item_db_id),
        )
        await db.commit()
        logger.error("Plaid sync failed for item %s: %s (%s)", item["item_id"], code, exc)
        raise

    logger.info("Plaid sync item %s: %s", item["item_id"], {k: v for k, v in result.items() if k != "new_ids"})
    return result


async def sync_all(db, client=None, categorize: bool = True) -> Dict[str, Any]:
    """Sync every stored item, then categorize ONLY the newly added rows."""
    cursor = await db.execute("SELECT id FROM plaid_items")
    item_ids = [r[0] for r in await cursor.fetchall()]

    totals = {"items": len(item_ids), "added": 0, "updated": 0, "removed": 0,
              "errors": [], "categorized": 0}
    new_ids: List[int] = []

    for item_db_id in item_ids:
        try:
            result = await sync_item(db, item_db_id, client=client)
            totals["added"] += result["added"]
            totals["updated"] += result["updated"]
            totals["removed"] += result["removed"]
            new_ids += result["new_ids"]
        except Exception as exc:
            totals["errors"].append({"item_db_id": item_db_id, "error": str(exc)[:200]})

    if categorize and new_ids:
        # Bounded to the rows this sync created — never a full-table pass.
        from app.services.categorizer import categorize_uncategorized

        try:
            totals["categorized"] = await categorize_uncategorized(db, limit=len(new_ids))
        except Exception as exc:
            logger.warning("Post-sync categorization failed: %s", exc)
            totals["errors"].append({"stage": "categorize", "error": str(exc)[:200]})

    return totals
