from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List
from app.database import get_db
from app.services import anomaly_groups
from pydantic import BaseModel


class AnomalyStatusPatch(BaseModel):
    status: str
    resolution_note: str | None = None
from app.services.anomaly_detector import detect_anomalies, update_flagged_transactions

router = APIRouter()

ALLOWED_KINDS = {"recurring-subscription", "spending-spike", "duplicate-candidate", "new-merchant", "category-anomaly"}
ALLOWED_SEVERITIES = {"low", "medium", "high"}
ALLOWED_TYPES = {"fraud", "refund"}


@router.get("/api/anomalies")
async def list_anomalies(
    kind: Optional[str] = Query(None, description="Filter by anomaly kind"),
    severity: Optional[str] = Query(None, description="Filter by severity level"),
    type: Optional[str] = Query(None, description="Filter by type (fraud, refund)"),
    limit: int = Query(1000, ge=1, le=5000),
):
    """Return detected anomalies.

    Runs all anomaly detectors on every call for fresh results.
    Optionally filter by kind, severity, or type.
    """
    db = await get_db()
    try:
        all_anomalies = await detect_anomalies(db)

        if kind:
            if kind not in ALLOWED_KINDS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid kind. Allowed: {', '.join(sorted(ALLOWED_KINDS))}",
                )
            all_anomalies = [a for a in all_anomalies if a["kind"] == kind]

        if severity:
            if severity not in ALLOWED_SEVERITIES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid severity. Allowed: {', '.join(sorted(ALLOWED_SEVERITIES))}",
                )
            all_anomalies = [a for a in all_anomalies if a["severity"] == severity]

        if type:
            if type not in ALLOWED_TYPES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid type. Allowed: {', '.join(sorted(ALLOWED_TYPES))}",
                )
            all_anomalies = [a for a in all_anomalies if a.get("type") == type]

        # Sort: high severity first, then by kind.
        sev_order = {"high": 0, "medium": 1, "low": 2}
        all_anomalies.sort(key=lambda a: (sev_order.get(a["severity"], 99), a["kind"], a["transaction_id"]))

        total = len(all_anomalies)
        page = all_anomalies[:limit]

        return {
            "anomalies": page,
            "total": total,
            "limit": limit,
        }
    finally:
        await db.close()


@router.post("/api/anomalies/detect")
async def run_anomaly_detection():
    """Run all anomaly detectors and persist flags to the transactions table.

    Sets flagged=1 and flag_reason on every transaction identified as anomalous.
    """
    db = await get_db()
    try:
        all_anomalies = await detect_anomalies(db)
        updated = await update_flagged_transactions(db, all_anomalies)
        return {
            "detected": len(all_anomalies),
            "updated_transactions": updated,
        }
    finally:
        await db.close()


@router.get("/api/anomalies/fraud-patterns")
async def list_fraud_patterns():
    """Merchants the owner has flagged as fraudulent.

    Data, not code: which merchants defrauded a particular person is private, and
    an app must not ship accusations about merchants it was never told about.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, pattern, note, created_at FROM fraud_patterns ORDER BY pattern"
        )
        return {"patterns": [dict(r) for r in await cursor.fetchall()]}
    finally:
        await db.close()


@router.post("/api/anomalies/fraud-patterns")
async def add_fraud_patterns(body: dict):
    """Add fraud patterns. Body: {"patterns": ["acme co", ...], "note": "..."}.

    Idempotent, and each pattern must be long enough to be a merchant rather than
    a fragment — the detector substring-matches, so a 2-character pattern would
    flag a large slice of the ledger as fraud.
    """
    patterns = [str(p).strip().lower() for p in (body.get("patterns") or [])]
    patterns = [p for p in patterns if p]
    too_short = [p for p in patterns if len(p) < 4]
    if too_short:
        raise HTTPException(
            status_code=400,
            detail=f"fraud pattern must be at least 4 characters: {too_short}",
        )
    if not patterns:
        raise HTTPException(status_code=400, detail="patterns is required")
    db = await get_db()
    try:
        added = 0
        for pattern in patterns:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO fraud_patterns (pattern, note) VALUES (?, ?)",
                (pattern, body.get("note")),
            )
            added += cursor.rowcount or 0
        await db.commit()
        return {"added": added, "total": len(patterns)}
    finally:
        await db.close()


@router.delete("/api/anomalies/fraud-patterns/{pattern_id}")
async def delete_fraud_pattern(pattern_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM fraud_patterns WHERE id=?", (pattern_id,))
        await db.commit()
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="Pattern not found")
        return {"deleted": pattern_id}
    finally:
        await db.close()


@router.get("/api/anomalies/kinds")
async def list_anomaly_kinds():
    """Return the set of possible anomaly kinds."""
    return {"kinds": sorted(ALLOWED_KINDS)}


@router.get("/api/anomalies/severities")
async def list_anomaly_severities():
    """Return the set of possible anomaly severities."""
    return {"severities": sorted(ALLOWED_SEVERITIES)}


@router.patch("/api/transactions/{transaction_id}/flag")
async def toggle_transaction_flag(transaction_id: int, flagged: bool = Query(...)):
    """Manually set or clear the flagged status on a transaction."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT id FROM transactions WHERE id=?", (transaction_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Transaction not found")

        if flagged:
            await db.execute(
                "UPDATE transactions SET flagged=1, flag_reason='Manually flagged by user' WHERE id=?",
                (transaction_id,),
            )
        else:
            await db.execute(
                "UPDATE transactions SET flagged=0, flag_reason=NULL WHERE id=?",
                (transaction_id,),
            )
        await db.commit()

        cursor = await db.execute("SELECT flagged, flag_reason FROM transactions WHERE id=?", (transaction_id,))
        result = await cursor.fetchone()
        return {"id": transaction_id, "flagged": bool(result["flagged"]), "flag_reason": result["flag_reason"]}
    finally:
        await db.close()


# ─── Grouped review queue ────────────────────────────────────────────────────
# The per-transaction endpoints above stay for compatibility; these are what the
# Activity page uses. One issue per merchant/recurring problem, refund-aware,
# with a resolution status that survives re-detection.

@router.get("/api/anomalies/groups")
async def list_anomaly_groups(status: str = "", kind: str = "", limit: int = 200):
    db = await get_db()
    try:
        return await anomaly_groups.list_groups(db, status=status or None,
                                                kind=kind or None, limit=limit)
    finally:
        await db.close()


@router.post("/api/anomalies/groups/rebuild")
async def rebuild_anomaly_groups(scope: str = "plaid"):
    """Re-detect and regroup. Human decisions (status/note) are preserved."""
    db = await get_db()
    try:
        return await anomaly_groups.rebuild_groups(db, scope=scope)
    finally:
        await db.close()


@router.get("/api/anomalies/groups/{group_id}")
async def get_anomaly_group(group_id: int):
    db = await get_db()
    try:
        group = await anomaly_groups.group_detail(db, group_id)
        if not group:
            raise HTTPException(status_code=404, detail="Anomaly group not found")
        return group
    finally:
        await db.close()


@router.patch("/api/anomalies/groups/{group_id}")
async def patch_anomaly_group(group_id: int, patch: AnomalyStatusPatch):
    """Resolve, dismiss, or turn an issue into a task."""
    db = await get_db()
    try:
        try:
            group = await anomaly_groups.set_status(db, group_id, patch.status,
                                                    patch.resolution_note)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not group:
            raise HTTPException(status_code=404, detail="Anomaly group not found")
        return group
    finally:
        await db.close()
