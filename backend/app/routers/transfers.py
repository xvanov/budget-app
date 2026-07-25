from fastapi import APIRouter
from app.database import get_db
from app.services.transfer_detector import detect_transfers

router = APIRouter()

@router.get("/api/transfers")
async def get_transfers():
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT tr.id, tr.from_transaction_id, tr.to_transaction_id, tr.matched_by,
                   t1.account_id AS from_account, t1.amount_cents AS from_amount,
                   t1.posted_date AS from_date, t1.description AS from_desc,
                   t2.account_id AS to_account, t2.amount_cents AS to_amount,
                   t2.posted_date AS to_date, t2.description AS to_desc
            FROM transfers tr
            JOIN transactions t1 ON tr.from_transaction_id = t1.id
            JOIN transactions t2 ON tr.to_transaction_id = t2.id
        """)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()

@router.get("/api/transfers/missing-counterparts")
async def get_missing_counterparts():
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT id, account_id, posted_date, amount_cents, description, merchant,
                   entity_type, linked_transaction_id
            FROM transactions
            WHERE entity_type = 'transfer'
              AND linked_transaction_id IS NULL
        """)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()

@router.post("/api/transfers/detect")
async def run_detect():
    db = await get_db()
    try:
        result = await detect_transfers(db)
        return result
    finally:
        await db.close()
