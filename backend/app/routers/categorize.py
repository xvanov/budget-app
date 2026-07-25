from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from fastapi import Query

from app.database import get_db
from app.services.categorizer import categorize_uncategorized

router = APIRouter()


class CategorizeResponse(BaseModel):
    categorized: int
    remaining: int


@router.post("/api/categorize", response_model=CategorizeResponse)
async def run_categorization(limit: Optional[int] = Query(None, description="Max rows to categorize (0 = all)")):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_path IS NULL OR category_path = 'Uncategorized'"
        )
        row = await cursor.fetchone()
        remaining_before = row[0] if row else 0

        if remaining_before == 0:
            return CategorizeResponse(categorized=0, remaining=0)

        categorized = await categorize_uncategorized(db, limit=limit or 0)

        cursor = await db.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_path IS NULL OR category_path = 'Uncategorized'"
        )
        row = await cursor.fetchone()
        remaining_after = row[0] if row else 0

        return CategorizeResponse(
            categorized=categorized,
            remaining=remaining_after,
        )
    finally:
        await db.close()
