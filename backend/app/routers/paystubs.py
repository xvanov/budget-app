from fastapi import APIRouter, HTTPException
from typing import List, Optional
from app.database import get_db

router = APIRouter()


@router.get("/api/paystubs")
async def list_paystubs():
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT p.*, t.merchant as linked_merchant, t.amount_cents as linked_amount
               FROM paystubs p
               LEFT JOIN transactions t ON p.linked_transaction_id = t.id
               ORDER BY p.period_start DESC"""
        )
        rows = await cursor.fetchall()
        paystubs = []
        for row in rows:
            paystubs.append({
                "id": row[0],
                "source_file": row[1],
                "employer": row[2],
                "employee_name": row[3],
                "period_start": row[4],
                "period_end": row[5],
                "pay_date": row[6],
                "gross_cents": row[7],
                "net_cents": row[8],
                "federal_tax_cents": row[9],
                "medicare_cents": row[10],
                "social_security_cents": row[11],
                "state_tax_cents": row[12],
                "retirement_cents": row[13],
                "insurance_cents": row[14],
                "hsa_cents": row[15],
                "other_deductions_cents": row[16],
                "linked_transaction_id": row[17],
                "created_at": row[18],
                "linked_merchant": row[19],
                "linked_amount": row[20],
            })
        return {"paystubs": paystubs}
    finally:
        await db.close()


@router.get("/api/paystubs/{paystub_id}")
async def get_paystub(paystub_id: int):
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT p.*, t.merchant as linked_merchant, t.amount_cents as linked_amount
               FROM paystubs p
               LEFT JOIN transactions t ON p.linked_transaction_id = t.id
               WHERE p.id=?""",
            (paystub_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Paystub not found")

        cursor2 = await db.execute(
            """SELECT id, amount_cents, entity_type, category_path, merchant, description
               FROM transactions
               WHERE source_file=?""",
            (row[1],)
        )
        components = await cursor2.fetchall()

        return {
            "id": row[0],
            "source_file": row[1],
            "employer": row[2],
            "employee_name": row[3],
            "period_start": row[4],
            "period_end": row[5],
            "pay_date": row[6],
            "gross_cents": row[7],
            "net_cents": row[8],
            "federal_tax_cents": row[9],
            "medicare_cents": row[10],
            "social_security_cents": row[11],
            "state_tax_cents": row[12],
            "retirement_cents": row[13],
            "insurance_cents": row[14],
            "hsa_cents": row[15],
            "other_deductions_cents": row[16],
            "linked_transaction_id": row[17],
            "created_at": row[18],
            "linked_merchant": row[19] if len(row) > 19 else None,
            "linked_amount": row[20] if len(row) > 20 else None,
            "components": [
                {
                    "id": c[0],
                    "amount_cents": c[1],
                    "entity_type": c[2],
                    "category_path": c[3],
                    "merchant": c[4],
                    "description": c[5],
                }
                for c in components
            ],
        }
    finally:
        await db.close()
