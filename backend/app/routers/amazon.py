"""Amazon endpoints: ingest the export, match charges/refunds, split into items."""
import logging

from fastapi import APIRouter, HTTPException

from app.database import get_db
from app.parsers.amazon import AmazonParseError, discover_owner_exports
from app.services import amazon_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/amazon/status")
async def amazon_status():
    """What is on disk, what is ingested, and how much is matched."""
    db = await get_db()
    try:
        async def scalar(sql, params=()):
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()
            return row[0] if row else 0

        items = await scalar("SELECT COUNT(*) FROM amazon_items")
        matched_items = await scalar(
            "SELECT COUNT(*) FROM amazon_items WHERE matched_transaction_id IS NOT NULL"
        )
        split_items = await scalar(
            "SELECT COUNT(*) FROM amazon_items WHERE split_transaction_id IS NOT NULL"
        )
        refunds = await scalar("SELECT COUNT(*) FROM amazon_refunds")
        matched_refunds = await scalar(
            "SELECT COUNT(*) FROM amazon_refunds WHERE matched_transaction_id IS NOT NULL"
        )
        refund_value = await scalar(
            "SELECT COALESCE(SUM(amount_cents),0) FROM amazon_refunds "
            "WHERE matched_transaction_id IS NOT NULL"
        )
        cursor = await db.execute(
            "SELECT owner, COUNT(*) FROM amazon_items GROUP BY owner ORDER BY owner"
        )
        owners = [{"owner": r[0], "items": r[1]} for r in await cursor.fetchall()]

        # Item-level attribution: which product (and whose) each refund reverses.
        # Broken out by strategy because the strategies differ in certainty —
        # 'exact_item' names a product, 'ambiguous' deliberately does not.
        attribution = await amazon_service.refund_attribution_counts(db)
        refunds_with_item = await scalar(
            "SELECT COUNT(*) FROM amazon_refunds WHERE matched_item_id IS NOT NULL"
        )
        refunds_paired = await scalar(
            "SELECT COUNT(*) FROM amazon_refunds r JOIN transactions t "
            "ON t.id = r.matched_transaction_id WHERE t.refund_match_id IS NOT NULL"
        )

        # How far the export reaches, and what has been bought since. An export is
        # a manual download that goes stale the day it is taken: charges after the
        # last order in it CANNOT be matched, no matter how often matching runs,
        # because the data that would explain them does not exist yet. Without
        # this the app reports those rows as "unmatched" beside the ones it
        # genuinely failed on, and the one with an obvious fix looks like a bug.
        covered_through = await scalar(
            "SELECT MAX(COALESCE(ship_date, order_date)) FROM amazon_items"
        )
        charges_after_export = 0
        if covered_through:
            rows = await amazon_service._amazon_transactions(db)
            charges_after_export = sum(
                1 for r in rows
                if (r["amount_cents"] or 0) < 0
                and (r["posted_date"] or "") > covered_through
                and not (r.get("merchant") or "").startswith("Amazon:")
            )

        return {
            "export_covers_orders_through": covered_through,
            "charges_after_export_coverage": charges_after_export,
            "exports_on_disk": sorted(discover_owner_exports(amazon_service.AMAZON_DIR).keys()),
            "owners": owners,
            "items": items,
            "items_matched": matched_items,
            "items_split": split_items,
            "refunds": refunds,
            "refunds_matched": matched_refunds,
            "matched_refund_value_cents": refund_value,
            "refund_attribution": attribution,
            "refunds_attributed_to_item": refunds_with_item,
            "refunds_paired": refunds_paired,
        }
    finally:
        await db.close()


@router.post("/api/amazon/import")
async def amazon_import(root: str = ""):
    db = await get_db()
    try:
        return await amazon_service.ingest_export(db, root or None)
    except AmazonParseError as exc:
        # Surface a format mismatch loudly — a silently empty Amazon ingest is
        # indistinguishable from "you have no orders".
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        await db.close()


@router.post("/api/amazon/match")
async def amazon_match(reset: bool = False):
    """Match Amazon data to card rows.

    ``reset=true`` first clears existing item/refund -> transaction links. Needed
    when the authoritative source changes (Plaid replacing the CSV history), so
    product detail attaches to rows the app actually displays.
    """
    db = await get_db()
    try:
        result = {}
        if reset:
            result["reset"] = await amazon_service.reset_matches(db)
        result.update(await amazon_service.match_all(db))
        return result
    finally:
        await db.close()


@router.post("/api/amazon/rebook-refunds")
async def amazon_rebook_refunds(overwrite_manual: bool = False):
    """Re-derive the category of already-matched Amazon refunds.

    ``match_all`` books a refund only when it first matches, so a correction to
    the booking rule never reaches refunds matched under the old one. Rows the
    owner has since categorized by hand are left alone unless
    ``overwrite_manual=true``.
    """
    db = await get_db()
    try:
        return await amazon_service.rebook_refunds(db, overwrite_manual=overwrite_manual)
    finally:
        await db.close()


@router.post("/api/amazon/attribute-refunds")
async def amazon_attribute_refunds(overwrite_manual: bool = False):
    """Attribute every refund to the item it reverses, book it, and pair it.

    One step because the three are useless apart: the item decides the category,
    and the item's row is what the refund pairs with so the two visibly cancel
    out. Safe to re-run — attribution is re-derived from the export each time, and
    a category the owner set by hand is left alone unless
    ``overwrite_manual=true``.
    """
    db = await get_db()
    try:
        return await amazon_service.attribute_and_pair_refunds(
            db, overwrite_manual=overwrite_manual
        )
    finally:
        await db.close()


@router.get("/api/amazon/refunds")
async def amazon_refunds(limit: int = 200):
    """Every refund record with the item, person and transaction behind it."""
    db = await get_db()
    try:
        rows = await amazon_service.list_refunds(db, limit=limit)
        return {"refunds": rows, "count": len(rows)}
    finally:
        await db.close()


@router.post("/api/amazon/split")
async def amazon_split(limit: int = 0):
    db = await get_db()
    try:
        return await amazon_service.split_matched_charges(db, limit=limit)
    finally:
        await db.close()


@router.get("/api/amazon/orders")
async def amazon_orders(limit: int = 100, owner: str = "", order_id: str = ""):
    db = await get_db()
    try:
        clauses, params = [], []
        if owner:
            clauses.append("o.owner = ?")
            params.append(owner)
        if order_id:
            clauses.append("o.order_id = ?")
            params.append(order_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = await db.execute(
            f"""SELECT o.*,
                   (SELECT COUNT(*) FROM amazon_items i
                     WHERE i.order_id=o.order_id AND i.owner=o.owner) AS item_count,
                   (SELECT COUNT(*) FROM amazon_items i
                     WHERE i.order_id=o.order_id AND i.owner=o.owner
                       AND i.matched_transaction_id IS NOT NULL) AS matched_count
                FROM amazon_orders o {where}
                ORDER BY o.order_date DESC LIMIT ?""",
            (*params, limit),
        )
        orders = [dict(r) for r in await cursor.fetchall()]
        return {"orders": orders, "count": len(orders)}
    finally:
        await db.close()


@router.get("/api/amazon/items")
async def amazon_items_for_transaction(transaction_id: int):
    """Items behind one charge — powers the 'why' panel for an Amazon row."""
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT * FROM amazon_items
               WHERE matched_transaction_id=? OR split_transaction_id=?
               ORDER BY total_cents DESC""",
            (transaction_id, transaction_id),
        )
        items = [dict(r) for r in await cursor.fetchall()]
        cursor = await db.execute(
            "SELECT * FROM amazon_refunds WHERE matched_transaction_id=?", (transaction_id,)
        )
        refunds = [dict(r) for r in await cursor.fetchall()]
        return {"items": items, "refunds": refunds}
    finally:
        await db.close()


@router.get("/api/amazon/unmatched")
async def amazon_unmatched(limit: int = 200):
    """Amazon rows we could not explain, and order data with no card charge.

    Reported rather than guessed at — an unmatched charge is left byte-identical
    in the DB.
    """
    db = await get_db()
    try:
        transactions = await amazon_service._amazon_transactions(db)
        cursor = await db.execute(
            "SELECT DISTINCT matched_transaction_id FROM amazon_items "
            "WHERE matched_transaction_id IS NOT NULL"
        )
        matched_ids = {r[0] for r in await cursor.fetchall()}
        cursor = await db.execute(
            "SELECT DISTINCT matched_transaction_id FROM amazon_refunds "
            "WHERE matched_transaction_id IS NOT NULL"
        )
        matched_ids |= {r[0] for r in await cursor.fetchall()}

        unmatched = [t for t in transactions if t["id"] not in matched_ids]
        charges = [t for t in unmatched if (t["amount_cents"] or 0) < 0][:limit]
        credits = [t for t in unmatched if (t["amount_cents"] or 0) > 0][:limit]

        cursor = await db.execute(
            """SELECT order_id, owner, order_date, total_cents, card_last4, used_gift_card
               FROM amazon_orders o
               WHERE NOT EXISTS (SELECT 1 FROM amazon_items i
                                 WHERE i.order_id=o.order_id AND i.owner=o.owner
                                   AND i.matched_transaction_id IS NOT NULL)
               ORDER BY order_date DESC LIMIT ?""",
            (limit,),
        )
        orders_without_charge = [dict(r) for r in await cursor.fetchall()]

        return {
            "unmatched_charges": charges,
            "unmatched_charge_count": len(charges),
            "unmatched_credits": credits,
            "unmatched_credit_count": len(credits),
            "orders_without_a_matched_charge": orders_without_charge,
            "note": (
                "Unmatched rows are left untouched. Common legitimate causes: a gift-card "
                "balance made the card charge smaller than the order, the order predates the "
                "imported statements, or Amazon refunded to gift balance (never hits the bank)."
            ),
        }
    finally:
        await db.close()
