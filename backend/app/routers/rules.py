from fastapi import APIRouter, HTTPException
from app.database import get_db
from app.services.rules_service import list_rules, get_rule

router = APIRouter()


@router.get("/api/rules")
async def get_rules():
    db = await get_db()
    try:
        rules = await list_rules(db)
        return {"rules": rules}
    finally:
        await db.close()


@router.get("/api/rules/{rule_id}")
async def get_rule_endpoint(rule_id: int):
    db = await get_db()
    try:
        rule = await get_rule(db, rule_id)
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
        return rule
    finally:
        await db.close()
