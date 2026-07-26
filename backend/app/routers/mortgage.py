"""Mortgage endpoints.

``GET /api/mortgage`` is the only source the Mortgage tab reads. It reports the
loan as the ledger records it — see ``services/mortgage`` for what is measured,
what is derived and what is left unknown.

The two endpoints that used to live here are gone:

* ``POST /api/mortgage/split`` drove ``mortgage_splitter``, which manufactured
  principal/interest child transactions using a constant $843.01 of interest
  lifted from a forward projection. The split is now computed at read time from
  the real balance, so nothing needs to be written into the ledger — and nothing
  fabricated stays in it.
* ``GET /api/mortgage/components`` listed those children by their pre-flat
  category paths (``Expense > Housing > Mortgage Interest`` …), paths the flat
  category model no longer has. With the children gone it could only ever return
  an empty list.
"""
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_db
from app.services.mortgage import FACT_FIELDS, analyze, load_facts, save_facts

logger = logging.getLogger(__name__)

router = APIRouter()


class MortgageFacts(BaseModel):
    """Loan terms only the owner can supply. Every field is optional.

    Anything set here is reported with ``basis: "stated"`` and overrides the
    value the app derived, which is the point: the app should never argue with
    the closing documents. Sending an explicit ``null`` clears a fact and hands
    the figure back to the derivation.
    """
    original_principal_cents: Optional[int] = None
    annual_rate_bps: Optional[int] = None
    term_months: Optional[int] = None
    origination_date: Optional[str] = None
    first_payment_date: Optional[str] = None
    payoff_date: Optional[str] = None
    escrow_refund_cents: Optional[int] = None
    escrow_refund_date: Optional[str] = None
    note: Optional[str] = None


@router.get("/api/mortgage")
async def get_mortgage(scope: Optional[str] = "plaid"):
    db = await get_db()
    try:
        return await analyze(db, scope)
    finally:
        await db.close()


@router.get("/api/mortgage/facts")
async def get_mortgage_facts():
    db = await get_db()
    try:
        return await load_facts(db)
    finally:
        await db.close()


@router.put("/api/mortgage/facts")
async def put_mortgage_facts(facts: MortgageFacts):
    """Record what the owner knows. Only the keys actually sent are touched."""
    sent = facts.model_dump(exclude_unset=True)
    unknown = set(sent) - set(FACT_FIELDS)
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown fields: {sorted(unknown)}")
    for key in ("original_principal_cents", "escrow_refund_cents"):
        if sent.get(key) is not None and sent[key] < 0:
            raise HTTPException(status_code=400, detail=f"{key} must be positive cents")
    db = await get_db()
    try:
        return await save_facts(db, sent)
    finally:
        await db.close()
