"""Detect refunded purchases — pairing a charge with the credit that reversed it.

Why pairs rather than just "positive amounts": a lone credit tells you money came
back but not what it undid. Pairing it with the original charge is what lets the
Refunded tab show "you bought this, then got it back", and what lets the
Transactions tab hide both halves of a wash.

This does NOT change any total. Totals aggregate each category's net, so a
refunded purchase already cancels itself out (-64 + 64 = 0). The pairing is for
visibility and filtering.

Matching is deliberately conservative — a wrong pair hides two real transactions
from view:

  * same normalized merchant,
  * the credit is not larger than the charge,
  * the credit lands on or after the charge date, within REFUND_WINDOW_DAYS,
  * one-to-one: a charge can be reversed once, a credit reverses one charge,
  * closest date wins, so a repeat purchase pairs with its own refund.

An exact-amount credit is a FULL refund; a smaller one is PARTIAL (a restocking
fee, or one item out of a shipment).
"""
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Refunds can be slow — a disputed subscription may reverse months later.
REFUND_WINDOW_DAYS = 180

# Partial refunds are held to a stricter standard than exact ones. An exact
# amount from the same merchant is near-proof; a smaller credit is a guess, and a
# wrong guess HIDES two real transactions from the list. Inspecting the first run
# showed the failure mode plainly: a $1.00 credit paired to a $20.00 charge 29
# days earlier (5% — actually an unrelated small refund from the same merchant).
# So a partial must be a meaningful fraction of the charge and land soon after it.
PARTIAL_MIN_RATIO = 0.25
PARTIAL_WINDOW_DAYS = 60

# Categories whose rows are internal movement or opted out; never refund pairs.
SKIP_ENTITY_TYPES = ("transfer", "asset_conversion", "excluded")


def normalize_merchant(name: Optional[str]) -> str:
    """Same-merchant test that survives spelling drift between charge and credit."""
    text = (name or "").lower()
    text = re.sub(r"[^a-z0-9]+", "", text)
    text = re.sub(r"(com|net|org|inc|llc)$", "", text)
    return re.sub(r"s$", "", text) or "unknown"


def economic_date(row: Dict[str, Any]) -> Optional[str]:
    """When a transaction actually HAPPENED, not when the bank printed it.

    ``posted_date`` is the settlement date, and for a re-presented charge those can
    be a year apart. A real case: a disputed subscription was provisionally credited,
    then the bank re-debited the whole series on one day — fourteen rows all posted
    2026-07-15 whose ``transaction_date`` values ran from 2025-01-28 to 2026-02-07,
    one per original monthly charge. Comparing posted dates made every one of those
    charges look like it happened AFTER the credits that reversed it, so the
    "a refund cannot precede its charge" rule refused to pair them and $267 of
    already-reversed spending stayed on the books.
    """
    return row.get("transaction_date") or row.get("posted_date")


def _days_between(later: Optional[str], earlier: Optional[str]) -> Optional[int]:
    try:
        a = datetime.strptime(str(later)[:10], "%Y-%m-%d")
        b = datetime.strptime(str(earlier)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None
    return (a - b).days


def find_pairs(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pure pairing over a list of transactions. Returns pair dicts.

    Kept free of DB access so the matching rules are unit-testable on their own.
    """
    charges: Dict[str, List[Dict[str, Any]]] = {}
    credits: List[Dict[str, Any]] = []

    for row in rows:
        if (row.get("entity_type") or "") in SKIP_ENTITY_TYPES:
            continue
        amount = int(row.get("amount_cents") or 0)
        if amount < 0:
            charges.setdefault(normalize_merchant(row.get("merchant") or row.get("description")), []).append(row)
        elif amount > 0:
            credits.append(row)

    claimed_charges: set = set()
    pairs: List[Dict[str, Any]] = []

    # Oldest credit first so an early refund claims the charge it actually undid.
    for credit in sorted(credits, key=lambda r: (economic_date(r) or "", r.get("id") or 0)):
        key = normalize_merchant(credit.get("merchant") or credit.get("description"))
        credit_amount = int(credit["amount_cents"])
        candidates = []
        for charge in charges.get(key, []):
            if charge["id"] in claimed_charges:
                continue
            charge_amount = abs(int(charge["amount_cents"]))
            if credit_amount > charge_amount:
                continue  # a credit bigger than the charge is not that charge's refund
            # Economic dates, not settlement dates — see economic_date().
            gap = _days_between(economic_date(credit), economic_date(charge))
            if gap is None or gap < 0 or gap > REFUND_WINDOW_DAYS:
                continue
            is_partial = charge_amount != credit_amount
            if is_partial:
                if gap > PARTIAL_WINDOW_DAYS:
                    continue
                if credit_amount / charge_amount < PARTIAL_MIN_RATIO:
                    continue
            candidates.append((gap, is_partial, charge))

        if not candidates:
            continue
        # Prefer an exact-amount match, then the closest date.
        candidates.sort(key=lambda c: (c[1], c[0]))
        gap, is_partial, charge = candidates[0]
        claimed_charges.add(charge["id"])
        pairs.append({
            "charge_id": charge["id"],
            "credit_id": credit["id"],
            "charge_cents": int(charge["amount_cents"]),
            "credit_cents": credit_amount,
            "partial": bool(is_partial),
            "days_to_refund": gap,
            "merchant": charge.get("merchant") or credit.get("merchant") or "",
            "category_path": charge.get("category_path"),
            "charge_date": charge.get("posted_date"),
            "credit_date": credit.get("posted_date"),
        })

    return pairs


def reconcile_exact(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pair one merchant's charges and credits by EXACT amount, oldest first. Pure.

    A separate rule from ``find_pairs`` because it answers a different question.
    ``find_pairs`` asks "did this purchase get refunded?" across the whole ledger,
    where a wrong guess hides two real transactions — hence its 180-day cap and its
    extra hurdles for partial amounts.

    This asks "settle up with ONE vendor", and the real case that needed it could
    never pass those hurdles. A subscription billed $20 a month from 2025-01 to
    2026-04 was refunded in a single batch on 2026-05-07: fifteen $20 credits at
    once, the oldest charge 460 days back. The window rejected everything older than
    six months, so twelve months of spending stayed on the books as real while the
    money was sitting back in the account — and the batch of credits showed up as
    $308 of "income" in May.

    Deliberately conservative in the ways that matter:

    * **exact amounts only.** No partial matching, so nothing is ever guessed at.
      An equal and opposite amount from the same vendor is near-proof.
    * **the credit must not precede the charge.** A refund cannot come first.
    * **one-to-one, oldest-to-oldest.** FIFO, which is how a vendor settling a
      backlog actually pays it down, and which makes the outcome deterministic
      rather than dependent on row order.
    * **no date cap** — that is the entire point — but the vendor identity and the
      exact amount are doing the work the window used to do.

    Leftovers are returned unpaired rather than force-matched: after the real
    reconciliation above, one $20 and one $7 charge remain, and that $27 is money
    genuinely spent.
    """
    charges = sorted(
        (r for r in rows
         if int(r.get("amount_cents") or 0) < 0
         and (r.get("entity_type") or "") not in SKIP_ENTITY_TYPES),
        key=lambda r: (economic_date(r) or "", r.get("id") or 0),
    )
    credits = sorted(
        (r for r in rows
         if int(r.get("amount_cents") or 0) > 0
         and (r.get("entity_type") or "") not in SKIP_ENTITY_TYPES),
        key=lambda r: (economic_date(r) or "", r.get("id") or 0),
    )

    by_amount: Dict[int, List[Dict[str, Any]]] = {}
    for charge in charges:
        by_amount.setdefault(abs(int(charge["amount_cents"])), []).append(charge)

    pairs: List[Dict[str, Any]] = []
    for credit in credits:
        amount = int(credit["amount_cents"])
        queue = by_amount.get(amount) or []
        match = None
        for index, charge in enumerate(queue):
            gap = _days_between(economic_date(credit), economic_date(charge))
            if gap is None or gap < 0:
                continue  # a refund cannot predate its charge
            match = queue.pop(index)
            break
        if not match:
            continue
        pairs.append({
            "charge_id": match["id"],
            "credit_id": credit["id"],
            "charge_cents": int(match["amount_cents"]),
            "credit_cents": amount,
            "partial": False,
            "days_to_refund": _days_between(economic_date(credit), economic_date(match)),
            "merchant": match.get("merchant") or credit.get("merchant") or "",
            "category_path": match.get("category_path"),
            "charge_date": match.get("posted_date"),
            "credit_date": credit.get("posted_date"),
        })
    return pairs


async def reconcile_merchant(db, pattern: str, scope: Optional[str] = "plaid",
                             dry_run: bool = False) -> Dict[str, Any]:
    """Settle up with one vendor: pair its charges and credits by exact amount.

    ``pattern`` is matched case-insensitively against merchant OR description, so
    it catches the spelling drift real feeds have (``SUBSCRIBECO.COM``,
    ``SUBSCRIBECOS.COM``, ``Subscribeco``).

    Only rows that are not already paired are considered, so this ADDS to whatever
    ``detect_and_store`` found rather than re-deciding it. ``dry_run`` reports what
    it would do and writes nothing — worth using first, since pairing is what makes
    a charge stop counting.
    """
    from app.services.scope import resolve_scope

    if not pattern or not pattern.strip():
        raise ValueError("reconcile_merchant: pattern is required")
    like = f"%{pattern.strip().lower()}%"

    scope_sql, _p, effective_scope, _n = await resolve_scope(db, scope)
    cursor = await db.execute(
        f"""SELECT id, posted_date, transaction_date, amount_cents, merchant, description,
                   category_path, entity_type, account_id
            FROM transactions t
            WHERE linked_transaction_id IS NULL
              AND refund_match_id IS NULL
              AND (LOWER(merchant) LIKE ? OR LOWER(description) LIKE ?)
              {scope_sql}
            ORDER BY posted_date, id""",
        (like, like),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    pairs = reconcile_exact(rows)

    paired_ids = {p["charge_id"] for p in pairs} | {p["credit_id"] for p in pairs}
    leftover = [r for r in rows if r["id"] not in paired_ids]

    if not dry_run:
        for pair in pairs:
            await db.execute(
                "UPDATE transactions SET refund_match_id=?, refund_role='charge', "
                "refund_partial=0 WHERE id=?",
                (pair["credit_id"], pair["charge_id"]),
            )
            await db.execute(
                "UPDATE transactions SET refund_match_id=?, refund_role='credit', "
                "refund_partial=0 WHERE id=?",
                (pair["charge_id"], pair["credit_id"]),
            )
        # Remember the intent. ``detect_and_store`` wipes every pair in scope before
        # re-marking, so without this the next refund detection would quietly undo
        # the reconciliation and twelve months of refunded spending would come back.
        await db.execute(
            "INSERT OR IGNORE INTO refund_reconciliations (pattern, scope, note) "
            "VALUES (?, ?, ?)",
            (pattern.strip(), effective_scope,
             f"Exact-amount reconciliation: {len(pairs)} pairs"),
        )
        await db.commit()

    result = {
        "pattern": pattern,
        "scope": effective_scope,
        "dry_run": dry_run,
        "considered": len(rows),
        "pairs": len(pairs),
        "reconciled_value_cents": sum(p["credit_cents"] for p in pairs),
        "leftover_rows": len(leftover),
        # What is still genuinely owed or spent once the vendor has settled up.
        "leftover_net_cents": sum(int(r["amount_cents"]) for r in leftover),
        "leftover": [
            {"id": r["id"], "posted_date": r["posted_date"],
             "amount_cents": r["amount_cents"], "merchant": r["merchant"],
             "category_path": r["category_path"]}
            for r in leftover
        ],
    }
    logger.info("Merchant reconciliation %r: %s pairs, leftover net %s",
                pattern, len(pairs), result["leftover_net_cents"])
    return result


async def detect_and_store(db, scope: Optional[str] = "plaid") -> Dict[str, Any]:
    """Re-detect refund pairs and record them on both transactions."""
    from app.services.scope import resolve_scope

    scope_sql, _p, effective_scope, _n = await resolve_scope(db, scope)
    cursor = await db.execute(
        f"""SELECT id, posted_date, transaction_date, amount_cents, merchant, description,
                   category_path, entity_type, account_id
            FROM transactions t
            WHERE linked_transaction_id IS NULL {scope_sql}"""
    )
    rows = [dict(r) for r in await cursor.fetchall()]

    pairs = find_pairs(rows)

    # Clear previous marks before re-marking, so an unpaired row does not keep a
    # stale flag forever. Two conditions on this wipe, each of which cost real
    # correctness when it was missing:
    #
    # 1. ``linked_transaction_id IS NULL`` mirrors the candidate query above. The
    #    wipe used to clear every row in scope while only unlinked rows could ever
    #    be re-paired, so a split item row that was correctly paired lost its mark
    #    permanently on the next pass. 628 rows in the live DB sit in that
    #    wipe-but-never-repair set. Nothing was paired yet, so nothing was lost —
    #    but detection now runs after every sync rather than only when a human
    #    clicks it, so the window was about to become hourly and unattended.
    #
    # 2. The partner is cleared too, even when it lives outside this scope. A pair
    #    spans two rows and a scoped wipe could clear one half and leave the other
    #    pointing at it — 12 such half-pairs already exist, and one of them makes
    #    /api/refunded report a $235.36 charge as 25% refunded when it was never
    #    refunded at all. A pair is cleared whole or not at all.
    await db.execute(
        f"""UPDATE transactions SET refund_match_id=NULL, refund_role=NULL, refund_partial=0
            WHERE refund_match_id IS NOT NULL
              AND (
                id IN (SELECT id FROM transactions t
                        WHERE t.linked_transaction_id IS NULL {scope_sql})
                OR refund_match_id IN (SELECT id FROM transactions t
                                        WHERE t.linked_transaction_id IS NULL {scope_sql})
              )"""
    )

    for pair in pairs:
        await db.execute(
            "UPDATE transactions SET refund_match_id=?, refund_role='charge', refund_partial=? WHERE id=?",
            (pair["credit_id"], 1 if pair["partial"] else 0, pair["charge_id"]),
        )
        await db.execute(
            "UPDATE transactions SET refund_match_id=?, refund_role='credit', refund_partial=? WHERE id=?",
            (pair["charge_id"], 1 if pair["partial"] else 0, pair["credit_id"]),
        )
    await db.commit()

    full = sum(1 for p in pairs if not p["partial"])
    result = {
        "scope": effective_scope,
        "pairs": len(pairs),
        "full_refunds": full,
        "partial_refunds": len(pairs) - full,
        "refunded_value_cents": sum(p["credit_cents"] for p in pairs),
    }

    # Re-apply every vendor reconciliation the owner has done. The wipe above is
    # what makes this necessary: without it, re-running detection would undo a
    # settled-up vendor and put months of already-refunded spending back on the
    # books. Re-applying is safe because ``reconcile_merchant`` only pairs exact
    # amounts among rows this pass left unpaired.
    cursor = await db.execute(
        "SELECT pattern FROM refund_reconciliations WHERE scope=? ORDER BY id",
        (effective_scope,),
    )
    patterns = [r[0] for r in await cursor.fetchall()]
    reapplied = []
    for pattern in patterns:
        try:
            again = await reconcile_merchant(db, pattern, scope=scope)
            reapplied.append({"pattern": pattern, "pairs": again["pairs"]})
        except Exception:  # pragma: no cover - defensive
            logger.exception("failed to re-apply reconciliation for %r", pattern)
    if reapplied:
        result["reapplied_reconciliations"] = reapplied
        # Reported alongside, NOT folded into `pairs`. Adding them made `pairs`
        # (58) stop equalling `full_refunds + partial_refunds` (21+28=49), so the
        # sync summary claimed nine more pairs than detection had classified.
        result["reapplied_pairs"] = sum(r["pairs"] for r in reapplied)
        result["total_pairs"] = result["pairs"] + result["reapplied_pairs"]

    logger.info("Refund pairing: %s", result)
    return result


async def list_pairs(db, start_date: Optional[str] = None, end_date: Optional[str] = None,
                     scope: Optional[str] = "plaid", limit: int = 500) -> Dict[str, Any]:
    """Refunded purchases, newest refund first."""
    from app.services.scope import resolve_scope

    scope_sql, _p, effective_scope, _n = await resolve_scope(db, scope)
    clauses = ["c.refund_role = 'credit'"]
    params: List[Any] = []
    if start_date:
        clauses.append("c.posted_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("c.posted_date <= ?")
        params.append(end_date)
    where = " AND ".join(clauses) + scope_sql.replace(" AND t.", " AND c.")

    cursor = await db.execute(
        f"""SELECT c.id credit_id, c.posted_date credit_date, c.amount_cents credit_cents,
                   c.merchant credit_merchant, c.account_id,
                   p.id charge_id, p.posted_date charge_date, p.amount_cents charge_cents,
                   p.merchant charge_merchant, p.category_path, c.refund_partial
            FROM transactions c
            JOIN transactions p ON p.id = c.refund_match_id
            WHERE {where}
            ORDER BY c.posted_date DESC LIMIT ?""",
        (*params, limit),
    )
    pairs = [dict(r) for r in await cursor.fetchall()]
    for pair in pairs:
        pair["days_to_refund"] = _days_between(pair["credit_date"], pair["charge_date"])
        pair["fully_refunded"] = not pair["refund_partial"]
        pair["outstanding_cents"] = max(0, abs(pair["charge_cents"]) - pair["credit_cents"])

    return {
        "pairs": pairs,
        "count": len(pairs),
        "totals": {
            "refunded_cents": sum(p["credit_cents"] for p in pairs),
            "original_spend_cents": sum(abs(p["charge_cents"]) for p in pairs),
            "still_outstanding_cents": sum(p["outstanding_cents"] for p in pairs),
            "partial_count": sum(1 for p in pairs if p["refund_partial"]),
        },
        "scope": effective_scope,
    }
