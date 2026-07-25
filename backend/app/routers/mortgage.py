import logging
from fastapi import APIRouter
from app.database import get_db
from app.services.mortgage_splitter import split_mortgage_payments

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/api/mortgage/split")
async def trigger_mortgage_split():
    db = await get_db()
    try:
        result = await split_mortgage_payments(db)
        return {"split": result["split"], "warnings": result["warnings"]}
    finally:
        await db.close()

@router.get("/api/mortgage/components")
async def get_mortgage_components():
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT t.*
            FROM transactions t
            WHERE t.category_path IN (
                'Transfer > Mortgage Principal',
                'Transfer > Mortgage Escrow',
                'Expense > Housing > Mortgage Interest',
                'Expense > Housing > Mortgage Taxes',
                'Expense > Housing > Mortgage Insurance'
            )
            ORDER BY t.posted_date DESC
        """)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()
