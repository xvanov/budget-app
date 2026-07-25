"""Collapse per-transaction anomalies into a reviewable, resolvable queue.

``anomaly_detector`` emits one anomaly PER TRANSACTION. On live data that was
~7,000 items across ~3,600 transactions — roughly two "anomalies" per
transaction, which is noise rather than a queue. Fifteen months of the same
fraudulent $20 subscription arrived as fifteen separate identical rows.

This module adds three things on top of that detector:

1. **Grouping.** One issue per merchant / recurring problem, carrying the
   transaction count, total, and date range. A repeat-charging merchant's rows become
   a single issue worth $X across N charges instead of N separate rows.

2. **Refund awareness.** Before telling someone to chase a chargeback, check
   whether the money already came back. Offsetting credits from the same
   merchant are matched to the group, giving refunded_cents and a refund_status
   of none / partial / full. A fully-refunded fraud issue is not an action item —
   it is already closed, and saying otherwise sends the user chasing money they
   already have.

3. **Resolution workflow.** Persistent status (open / task / resolved /
   dismissed) with a note, preserved across re-detection so re-running the
   detector never silently reopens something already dealt with.

Scope: Plaid only by default, consistent with the rest of the app — flagging
transactions that no other view will show is not useful.
"""
import logging
import re
from typing import Any, Dict, List, Optional

from app.services.anomaly_detector import detect_anomalies

logger = logging.getLogger(__name__)

VALID_STATUSES = ("open", "task", "resolved", "dismissed")

# Refunds can land well after the charge, especially on a disputed subscription.
REFUND_LOOKAHEAD_DAYS = 400

SEVERITY_RANK = {"high": 3, "medium": 2, "low": 1}

# Internal movement (card payments, mortgage paydowns, account transfers, split
# parents) is not spending and so is never a spending anomaly.
EXCLUDED_ENTITY_TYPES = ("transfer", "asset_conversion", "excluded")

# A duplicate needs at least two charges to BE a duplicate. The detector runs
# unscoped and matched CSV-history rows against their Plaid twins, so nearly
# every transaction came back as a lone "duplicate candidate" — 1,954 of them.
MIN_DUPLICATE_GROUP_SIZE = 2


def normalize_merchant(name: Optional[str]) -> str:
    """Collapse merchant spelling variants so one issue does not become several.

    The same fraudulent merchant appears as 'SUBSCRIBECO.COM', 'SUBSCRIBECOS.COM',
    'Subscribeco' and 'Subscribecos' across CSV and Plaid. Grouping on the raw
    string would produce four separate issues for one problem.
    """
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9]+", "", text)          # drop punctuation/spacing
    text = re.sub(r"(com|net|org|inc|llc)$", "", text)  # drop TLD/suffix
    text = re.sub(r"s$", "", text)                   # singular/plural variants
    return text or "unknown"


def _group_key(anomaly: Dict[str, Any], txn: Dict[str, Any]) -> str:
    """Identity of an ISSUE (not of a transaction)."""
    kind = anomaly.get("kind") or "unknown"
    merchant = normalize_merchant(txn.get("merchant") or txn.get("description"))
    if kind == "spending-spike":
        # A spike is per category per month, not per merchant.
        return f"{kind}|{txn.get('category_path') or '?'}|{(txn.get('posted_date') or '')[:7]}"
    if kind == "duplicate-candidate":
        return f"{kind}|{merchant}|{abs(int(txn.get('amount_cents') or 0))}|{(txn.get('posted_date') or '')[:10]}"
    return f"{kind}|{merchant}"


def _suggested_action(kind: str, atype: Optional[str], refund_status: str,
                      merchant: str, count: int) -> str:
    """What the user should actually DO — refund state changes the answer."""
    if refund_status == "full":
        return "Already refunded in full — no action needed. Verify the charges have stopped."
    partial = refund_status == "partial"

    if atype == "fraud":
        if partial:
            return (f"Partially refunded. Chase {merchant} for the remainder, and request a "
                    f"chargeback from Chase if they will not refund it.")
        return (f"Flag as fraud: request a refund from {merchant}, and a chargeback from Chase "
                f"if refused. Cancel any active authorisation so it cannot bill again.")
    if kind == "recurring-subscription":
        return (f"{count} charges from {merchant} look like a subscription. Confirm you still "
                f"want it; cancel if not.")
    if kind == "spending-spike":
        return "Review what drove this category up this month; adjust the budget if it is the new normal."
    if kind == "duplicate-candidate":
        if partial:
            return "Partially refunded — confirm the remaining duplicate was reversed."
        return "Verify you were not billed twice; request a reversal if you were."
    if kind == "new-merchant":
        return f"First charge from {merchant} — confirm you recognise it."
    if atype == "refund":
        return "Refund received — confirm it matches what you expected."
    return f"Review {merchant}."


async def _refunds_for_merchant(db, merchant_key: str, scope_sql: str) -> List[Dict[str, Any]]:
    """Positive-amount rows from the same merchant — money that came back.

    Deliberately ignores entity_type: a merchant credit may have been booked as
    income, as a refund, or left uncategorized, and the money came back either
    way. Matching on entity_type would miss real refunds.
    """
    cursor = await db.execute(
        f"""SELECT id, posted_date, amount_cents, merchant, description
            FROM transactions t
            WHERE t.amount_cents > 0 {scope_sql}"""
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    return [
        r for r in rows
        if normalize_merchant(r.get("merchant") or r.get("description")) == merchant_key
    ]


async def rebuild_groups(db, scope: Optional[str] = "plaid") -> Dict[str, Any]:
    """Re-detect, regroup, and persist. Existing statuses/notes are preserved."""
    from app.services.scope import resolve_scope

    scope_sql, _params, effective_scope, _note = await resolve_scope(db, scope)
    scope_sql_t = scope_sql                       # for queries aliased as t
    anomalies = await detect_anomalies(db)

    # Only keep anomalies whose transaction is inside the active scope.
    cursor = await db.execute(
        f"""SELECT id, merchant, description, amount_cents, posted_date, category_path,
                   account_id, entity_type
            FROM transactions t WHERE 1=1 {scope_sql_t}"""
    )
    in_scope = {r["id"]: dict(r) for r in await cursor.fetchall()}

    grouped: Dict[str, Dict[str, Any]] = {}
    skipped = {"out_of_scope": 0, "transfer": 0, "refund_class": 0}
    for anomaly in anomalies:
        txn = in_scope.get(anomaly.get("transaction_id"))
        if not txn:
            skipped["out_of_scope"] += 1
            continue  # out of scope — do not surface it

        # Internal movement is never a spending anomaly. Without this, every
        # mortgage paydown and card payment showed up as a six-figure "issue"
        # ($100k, $50k, $40k...), which is exactly the kind of noise that makes a
        # review queue useless.
        if (txn.get("entity_type") or "") in EXCLUDED_ENTITY_TYPES:
            skipped["transfer"] += 1
            continue

        # "This was a refund — take a look" is not an action item. Genuine
        # problems surface as fraud, duplicates, spikes or subscriptions.
        if anomaly.get("type") == "refund":
            skipped["refund_class"] += 1
            continue

        key = _group_key(anomaly, txn)
        group = grouped.setdefault(key, {
            "group_key": key,
            "kind": anomaly.get("kind"),
            "type": anomaly.get("type"),
            "severity": anomaly.get("severity") or "low",
            "detail": anomaly.get("reason"),
            "merchant": txn.get("merchant") or "",
            "merchant_key": normalize_merchant(txn.get("merchant") or txn.get("description")),
            "category_path": txn.get("category_path"),
            "txn_ids": set(),
            "charge_cents": 0,
            "first_date": txn.get("posted_date"),
            "last_date": txn.get("posted_date"),
        })
        if anomaly["transaction_id"] in group["txn_ids"]:
            continue
        group["txn_ids"].add(anomaly["transaction_id"])
        amount = int(txn.get("amount_cents") or 0)
        if amount < 0:
            group["charge_cents"] += abs(amount)
        date = txn.get("posted_date") or ""
        if date and (not group["first_date"] or date < group["first_date"]):
            group["first_date"] = date
        if date and (not group["last_date"] or date > group["last_date"]):
            group["last_date"] = date
        if SEVERITY_RANK.get(anomaly.get("severity"), 0) > SEVERITY_RANK.get(group["severity"], 0):
            group["severity"] = anomaly["severity"]

    # Preserve human decisions across re-detection.
    cursor = await db.execute(
        "SELECT group_key, status, resolution_note FROM anomaly_groups"
    )
    previous = {r["group_key"]: dict(r) for r in await cursor.fetchall()}

    await db.execute("DELETE FROM anomaly_group_transactions")
    await db.execute("DELETE FROM anomaly_groups")

    # A lone "duplicate candidate" is a duplicate of nothing.
    dropped_singletons = 0
    for key in list(grouped):
        if (grouped[key]["kind"] == "duplicate-candidate"
                and len(grouped[key]["txn_ids"]) < MIN_DUPLICATE_GROUP_SIZE):
            del grouped[key]
            dropped_singletons += 1

    refund_cache: Dict[str, List[Dict[str, Any]]] = {}
    written = 0
    for group in grouped.values():
        merchant_key = group["merchant_key"]
        if merchant_key not in refund_cache:
            refund_cache[merchant_key] = await _refunds_for_merchant(db, merchant_key, scope_sql_t)
        refunds = [
            r for r in refund_cache[merchant_key]
            if (r.get("posted_date") or "") >= (group["first_date"] or "")
        ]
        charged = group["charge_cents"]
        # Clamp: a merchant can refund more than the charges inside THIS group
        # (e.g. a bulk dispute covering charges outside the window), and a
        # negative "outstanding" is meaningless in a review queue.
        refunded = min(sum(int(r["amount_cents"]) for r in refunds), charged) if charged else 0
        if charged and refunded >= charged:
            refund_status = "full"
        elif refunded > 0:
            refund_status = "partial"
        else:
            refund_status = "none"

        count = len(group["txn_ids"])
        label = group["merchant"] or "(unknown merchant)"
        title = {
            "recurring-subscription": f"{label} — recurring charge",
            "spending-spike": f"{group['category_path'] or 'Category'} — spending spike",
            "duplicate-candidate": f"{label} — possible duplicate charge",
            "new-merchant": f"{label} — first-time merchant",
        }.get(group["kind"], f"{label} — {'suspected fraud' if group['type'] == 'fraud' else 'needs review'}")

        prior = previous.get(group["group_key"])
        status = prior["status"] if prior else "open"
        note = prior["resolution_note"] if prior else None
        # A fully-refunded issue with no human decision yet is already settled.
        if refund_status == "full" and not prior:
            status = "resolved"
            note = "Auto-resolved: the full amount was refunded."

        cursor = await db.execute(
            """INSERT INTO anomaly_groups
               (group_key, kind, type, severity, title, detail, merchant, category_path,
                transaction_count, total_cents, refunded_cents, refund_status,
                suggested_action, status, resolution_note, first_date, last_date, updated_at,
                resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),
                       CASE WHEN ? IN ('resolved','dismissed') THEN datetime('now') END)""",
            (
                group["group_key"], group["kind"], group["type"], group["severity"], title,
                group["detail"], group["merchant"], group["category_path"], count, charged,
                refunded, refund_status,
                _suggested_action(group["kind"], group["type"], refund_status, label, count),
                status, note, group["first_date"], group["last_date"], status,
            ),
        )
        group_id = cursor.lastrowid
        for txn_id in group["txn_ids"]:
            await db.execute(
                "INSERT OR IGNORE INTO anomaly_group_transactions (group_id, transaction_id, role) "
                "VALUES (?,?,'charge')",
                (group_id, txn_id),
            )
        for refund in refunds:
            await db.execute(
                "INSERT OR IGNORE INTO anomaly_group_transactions (group_id, transaction_id, role) "
                "VALUES (?,?,'refund')",
                (group_id, refund["id"]),
            )
        written += 1

    await db.commit()
    result = {
        "scope": effective_scope,
        "raw_anomalies": len(anomalies),
        "groups": written,
        "statuses_preserved": len([k for k in previous if k in grouped]),
        "skipped": skipped,
        "dropped_duplicate_singletons": dropped_singletons,
    }
    logger.info("Anomaly grouping: %s", result)
    return result


async def list_groups(db, status: Optional[str] = None, kind: Optional[str] = None,
                      limit: int = 200) -> Dict[str, Any]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cursor = await db.execute(
        f"""SELECT * FROM anomaly_groups {where}
            ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'task' THEN 1 ELSE 2 END,
                     CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                     (total_cents - refunded_cents) DESC
            LIMIT ?""",
        (*params, limit),
    )
    groups = [dict(r) for r in await cursor.fetchall()]

    cursor = await db.execute(
        """SELECT status, COUNT(*) n, COALESCE(SUM(total_cents - refunded_cents),0) outstanding
           FROM anomaly_groups GROUP BY status"""
    )
    summary = {r["status"]: {"count": r["n"], "outstanding_cents": r["outstanding"]}
               for r in await cursor.fetchall()}
    return {"groups": groups, "count": len(groups), "summary": summary}


async def group_detail(db, group_id: int) -> Optional[Dict[str, Any]]:
    cursor = await db.execute("SELECT * FROM anomaly_groups WHERE id=?", (group_id,))
    row = await cursor.fetchone()
    if not row:
        return None
    group = dict(row)
    cursor = await db.execute(
        """SELECT agt.role, t.id, t.posted_date, t.amount_cents, t.merchant, t.description,
                  t.category_path, t.account_id
           FROM anomaly_group_transactions agt
           JOIN transactions t ON t.id = agt.transaction_id
           WHERE agt.group_id = ?
           ORDER BY t.posted_date DESC""",
        (group_id,),
    )
    group["transactions"] = [dict(r) for r in await cursor.fetchall()]
    return group


async def set_status(db, group_id: int, status: str, note: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}")
    cursor = await db.execute("SELECT id FROM anomaly_groups WHERE id=?", (group_id,))
    if not await cursor.fetchone():
        return None
    await db.execute(
        """UPDATE anomaly_groups
           SET status=?, resolution_note=COALESCE(?, resolution_note), updated_at=datetime('now'),
               resolved_at=CASE WHEN ? IN ('resolved','dismissed') THEN datetime('now') ELSE NULL END
           WHERE id=?""",
        (status, note, status, group_id),
    )
    await db.commit()
    return await group_detail(db, group_id)
