from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import os
from datetime import datetime

from app.database import get_db
from app.llm import BUDGET_CONTEXT_PATH

router = APIRouter()

class ContextResponse(BaseModel):
    content: str
    last_modified: str | None = None

class ContextUpdate(BaseModel):
    content: str

def _read_context_file():
    last_modified = None
    content = ""
    if os.path.exists(BUDGET_CONTEXT_PATH):
        try:
            with open(BUDGET_CONTEXT_PATH, "r") as f:
                content = f.read()
            last_modified = datetime.fromtimestamp(
                os.path.getmtime(BUDGET_CONTEXT_PATH)
            ).isoformat()
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    return content, last_modified

def _write_context_file(content: str):
    os.makedirs(os.path.dirname(BUDGET_CONTEXT_PATH) or ".", exist_ok=True)
    try:
        with open(BUDGET_CONTEXT_PATH, "w") as f:
            f.write(content)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/context", response_model=ContextResponse)
async def get_context():
    content, last_modified = _read_context_file()
    if not content.strip():
        await _infer_context()
        content, last_modified = _read_context_file()
    return ContextResponse(content=content, last_modified=last_modified)

@router.put("/api/context")
async def put_context(update: ContextUpdate):
    _write_context_file(update.content)
    return {"status": "ok", "last_modified": datetime.now().isoformat()}

@router.post("/api/context/infer")
async def infer_context():
    await _infer_context()
    content, last_modified = _read_context_file()
    return ContextResponse(content=content, last_modified=last_modified)

def append_context_fact(fact: str):
    content, _ = _read_context_file()
    if fact not in content:
        new_content = content.rstrip() + "\n\n## User Correction\n" + fact + "\n"
        _write_context_file(new_content)
        return True
    return False


async def _infer_context():
    if os.path.exists(BUDGET_CONTEXT_PATH):
        with open(BUDGET_CONTEXT_PATH) as f:
            if f.read().strip():
                return

    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM transactions")
        row = await cursor.fetchone()
        count = row[0] if row else 0
        if count == 0:
            return

        lines = ["# Budget Context\n"]
        lines.append(f"_Auto-generated from {count} imported transactions_\n")

        cursor = await db.execute(
            "SELECT account_id, source_platform, COUNT(*) as cnt, "
            "MIN(posted_date) as min_d, MAX(posted_date) as max_d "
            "FROM transactions GROUP BY account_id ORDER BY cnt DESC"
        )
        accounts = await cursor.fetchall()
        lines.append("\n## Accounts\n")
        account_names = []
        for a in accounts:
            lines.append(
                f"- {a['account_id']} ({a['source_platform']}): "
                f"{a['cnt']} transactions, {a['min_d']} to {a['max_d']}"
            )
            account_names.append(a['account_id'])

        cursor = await db.execute(
            "SELECT source_platform, COUNT(*) as cnt "
            "FROM transactions GROUP BY source_platform ORDER BY cnt DESC"
        )
        platforms = await cursor.fetchall()
        lines.append("\n## Platforms\n")
        for p in platforms:
            lines.append(f"- {p['source_platform']}: {p['cnt']} transactions")

        cursor = await db.execute(
            "SELECT MIN(posted_date), MAX(posted_date) FROM transactions"
        )
        row = await cursor.fetchone()
        if row and row[0]:
            lines.append(
                f"\n## Time Range\nTransactions span from {row[0]} to {row[1]}.\n"
            )

        cursor = await db.execute(
            "SELECT "
            "SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END) as income, "
            "SUM(CASE WHEN amount_cents < 0 THEN ABS(amount_cents) ELSE 0 END) as expense "
            "FROM transactions"
        )
        row = await cursor.fetchone()
        if row:
            income = row[0] or 0
            expense = row[1] or 0
            lines.append(
                f"\n## Financial Overview\n"
                f"Total income: ${income / 100:,.2f}\n"
                f"Total expenses: ${expense / 100:,.2f}\n"
            )

        content = "".join(lines)
        _write_context_file(content)
    finally:
        await db.close()
