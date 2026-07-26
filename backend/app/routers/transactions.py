import asyncio
import csv
import io
import json
import logging
from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional, List
from app.database import get_db
from app.services.scope import (
    EXPENSE_SUM,
    INCOME_SUM,
    NET_SUM,
    effective_amount_sql,
    resolve_scope,
)
from app.schemas import (
    ApplySimilarRequest,
    BulkEditRequest,
    BulkEditResponse,
    ManualTransactionCreate,
    TransactionPatch,
)
from app.services.rules_service import add_rule, add_rule_once, determine_entity_type
from app.services.sign_invariant import (
    coerce_sign_safe,
    log_violation_autofix,
    violates_sign_invariant,
)
from app.llm import call_llm
from app.routers.context import append_context_fact

logger = logging.getLogger(__name__)

router = APIRouter()

@router.post("/api/transactions", status_code=201)
async def create_manual_transaction(txn: ManualTransactionCreate):
    db = await get_db()
    try:
        entity_type = (determine_entity_type(txn.category_path, txn.amount_cents)
                       if txn.category_path else determine_entity_type(None, txn.amount_cents))
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, category_path, entity_type, business_context,
                notes, confidence_score, categorization_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (txn.source_platform, txn.source_file, txn.account_id, txn.posted_date,
             txn.amount_cents, txn.merchant, txn.description,
             txn.category_path, entity_type, txn.business_context,
             txn.notes, 1.0, "Manual entry")
        )
        await db.commit()
        new_id = cursor.lastrowid
        cursor = await db.execute("SELECT * FROM transactions WHERE id=?", (new_id,))
        row = await cursor.fetchone()
        return dict(row)
    finally:
        await db.close()

@router.get("/api/transactions")
async def get_transactions(
    account_id: Optional[str] = None,
    tag: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category_path: Optional[str] = None,
    category_prefix: Optional[str] = None,
    business_context: Optional[str] = None,
    entity_type: Optional[str] = None,
    merchant: Optional[str] = None,
    description: Optional[str] = None,
    source_file: Optional[str] = None,
    search: Optional[str] = None,
    q: Optional[str] = None,
    flagged: Optional[str] = None,
    amount_min: Optional[float] = None,
    amount_max: Optional[float] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000),
    limit: Optional[int] = Query(None, ge=1, le=1000),
    offset: Optional[int] = Query(None, ge=0),
    sort: str = "posted_date",
    dir: str = "desc",
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    scope: Optional[str] = "auto",
    exclude_refunded: Optional[bool] = False,
    only_refunded: Optional[bool] = False,
    flow: Optional[str] = None,
    include_split_parents: bool = False,
):
    db = await get_db()
    try:
        if limit is not None:
            page_size = limit
        if offset is None:
            offset = (page - 1) * page_size
        else:
            page = (offset // page_size) + 1 if page_size > 0 else 1
        if q and not search:
            search = q

        # Accept both ?sort=...&dir=... and legacy ?sort_by=...&sort_order=...
        effective_sort = sort if sort != "posted_date" else (sort_by or "posted_date")
        effective_dir = dir if dir != "desc" else (sort_order or "desc")

        clauses = []
        params = []

        # Scope: Plaid is the live source, CSV rows are stored history. The
        # fragment is written for a "t." alias; this query has no alias.
        scope_sql, _scope_params, effective_scope, scope_note = await resolve_scope(db, scope)
        if scope_sql:
            clauses.append(scope_sql.replace(" AND t.", "").strip())

        valid_sort = {
            "posted_date", "account_id", "merchant", "description",
            "amount_cents", "category_path", "tags", "source_file",
            "flagged", "normalized_description",
        }
        if effective_sort not in valid_sort:
            effective_sort = "posted_date"
        if effective_dir not in ("asc", "desc"):
            effective_dir = "desc"

        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        if tag:
            clauses.append("tags LIKE ?")
            params.append(f"%{tag}%")
        if start_date:
            clauses.append("posted_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("posted_date <= ?")
            params.append(end_date)
        # ``category_path`` is an EXACT flat name; only the explicit
        # ``category_prefix`` parameter still prefix-matches, for the legacy
        # hierarchical callers that pass one. Treating category_path as a prefix
        # made "Other" also select every "Other Income" row.
        if category_path and not category_prefix:
            clauses.append("category_path = ?")
            params.append(category_path)
        elif category_prefix:
            clauses.append("category_path LIKE ?")
            params.append(f"{category_prefix}%")

        # Income vs expense is the SIGN of the amount, never the category — see
        # services/flat_categories. The old Income/Expense chips filtered
        # ``category_path LIKE 'Income%'`` and matched nothing at all once
        # categories became flat names, so both showed an empty table.
        if flow:
            flow = flow.strip().lower()
            from app.services.flat_categories import (
                EXCLUDED_CATEGORY, TRANSFER_CATEGORIES,
            )
            neutral = (*TRANSFER_CATEGORIES, EXCLUDED_CATEGORY)
            neutral_ph = ",".join("?" * len(neutral))
            if flow == "transfer":
                transfer_ph = ",".join("?" * len(TRANSFER_CATEGORIES))
                clauses.append(f"category_path IN ({transfer_ph})")
                params.extend(TRANSFER_CATEGORIES)
            elif flow in ("income", "expense"):
                # Transfers are internal movement and Excluded is opted out by
                # hand; neither is money in or out, so both stay out of these.
                clauses.append(
                    f"amount_cents {'>' if flow == 'income' else '<'} 0 "
                    f"AND (category_path IS NULL OR category_path NOT IN ({neutral_ph}))"
                )
                params.extend(neutral)
            elif flow == "excluded":
                clauses.append("category_path = ?")
                params.append(EXCLUDED_CATEGORY)
            else:
                raise HTTPException(
                    status_code=400,
                    detail=(f"flow must be income, expense, transfer or excluded — got "
                            f"{flow!r}"),
                )
        if business_context:
            clauses.append("business_context=?")
            params.append(business_context)
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if merchant:
            clauses.append("merchant LIKE ?")
            params.append(f"%{merchant}%")
        if description:
            clauses.append("description LIKE ?")
            params.append(f"%{description}%")
        if source_file:
            clauses.append("source_file LIKE ?")
            params.append(f"%{source_file}%")
        if search:
            clauses.append("(merchant LIKE ? OR description LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if flagged is not None:
            if flagged in ("1", "true", "yes"):
                clauses.append("flagged=1")
            elif flagged in ("0", "false", "no"):
                clauses.append("flagged=0")
        # Refunded purchases are a wash: the charge and its credit cancel out.
        # Hiding both halves declutters the list without changing any total.
        if exclude_refunded:
            clauses.append("refund_match_id IS NULL")
        if only_refunded:
            clauses.append("refund_match_id IS NOT NULL")
        if amount_min is not None:
            clauses.append("ABS(amount_cents) >= ?")
            params.append(int(amount_min * 100))
        if amount_max is not None:
            clauses.append("ABS(amount_cents) <= ?")
            params.append(int(amount_max * 100))

        # A fully-split charge is on screen TWICE today: once as the original
        # combined row and once as the item rows it was broken into. The user hit
        # this on txn 8759 — a −$38.15 Amazon charge listed next to its own three
        # car-part items — and there is no reading of that list under which the
        # $38.15 appears once. The items carry the real categories, so they are
        # the visible truth and the combined row drops out of the listing, its
        # count and its money totals alike (all three share this WHERE).
        #
        # The row is NOT deleted: it is the bank's actual charge and the anchor
        # for reconciliation, refund pairing and the Amazon item export. It stays
        # reachable two ways — ``?include_split_parents=true`` for an audit view,
        # and the ``split_parent_*`` columns below, which let every item row show
        # and expand the combined charge it belongs to.
        #
        # Only FULLY-covered parents are hidden. A partial split would leave
        # residual money with nowhere to be counted, so those stay listed; see
        # ``_FULLY_SPLIT_PARENT_IDS_SQL``.
        if not include_split_parents:
            clauses.append(f"id NOT IN ({_FULLY_SPLIT_PARENT_IDS_SQL})")

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        cursor = await db.execute(f"SELECT COUNT(*) as total FROM transactions {where}", params)
        total_row = await cursor.fetchone()
        total = total_row[0] if total_row else 0

        # Money totals for the WHOLE filtered set, not the page. The client used to
        # sum the 50 rows it had and print that next to the full `total` count, so
        # the line read "3,767 transactions · $2,017 in · $1,962 out" — a real count
        # beside a one-page figure, which is just wrong. Gross in/out of the raw
        # amounts, because this summarises the rows being listed and each row shows
        # its own raw amount; the netted, refund-attributed view is what
        # /api/dashboard is for.
        cursor = await db.execute(
            f"""SELECT COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END), 0),
                       COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END), 0)
                FROM transactions {where}""",
            params,
        )
        money = await cursor.fetchone()
        totals = {
            "income_cents": money[0] or 0,
            "expense_cents": money[1] or 0,
            "net_cents": (money[0] or 0) - (money[1] or 0),
            "transaction_count": total,
        }

        # ``split_child_count`` tells the client this row is a composite charge that
        # was broken into per-item children. Without it a split parent looks like an
        # ordinary transaction, so the obvious thing to do — recategorize it — is
        # the one thing that would double-count the money, and the user only finds
        # out when the write is refused.
        #
        # It counts children the way ``split_child_count()`` / the guard do —
        # asymmetric links only. A plain ``COUNT(*) WHERE linked_transaction_id =
        # t.id`` also counts refund and transfer COUNTERPARTS, which point at each
        # other symmetrically: 1,078 rows in the live DB had children by that
        # reading against 343 by this one, so ~735 ordinary rows wore an "N items"
        # chip and would have had their category picker locked by the client-side
        # check that now keys off this number.
        #
        # ``split_parent_*`` is the reverse view, for the item rows themselves: the
        # combined charge is no longer listed beside them, so each item carries the
        # id, amount and merchant of the charge it is part of, plus how many items
        # that charge has. That is what makes the hidden parent reachable.
        # Correlated subqueries over one page of rows, all id lookups.
        cursor = await db.execute(
            f"""SELECT t.*,
                       (SELECT COUNT(*) FROM transactions k
                         WHERE k.linked_transaction_id = t.id
                           AND k.id != t.id
                           AND (t.linked_transaction_id IS NULL
                                OR t.linked_transaction_id != k.id)) AS split_child_count,
                       ({_SPLIT_PARENT_OF_ROW_SQL.format(column="id")})
                         AS split_parent_id,
                       ({_SPLIT_PARENT_OF_ROW_SQL.format(column="amount_cents")})
                         AS split_parent_amount_cents,
                       ({_SPLIT_PARENT_OF_ROW_SQL.format(column="merchant")})
                         AS split_parent_merchant,
                       (SELECT COUNT(*) FROM transactions s
                         WHERE s.linked_transaction_id = t.linked_transaction_id
                           AND s.id != t.linked_transaction_id) AS split_sibling_count
                FROM transactions t {where}
                ORDER BY {effective_sort} {effective_dir} LIMIT ? OFFSET ?""",
            params + [page_size, offset]
        )
        transactions = [dict(r) for r in await cursor.fetchall()]

        # ``split_parent_id`` above is any asymmetric parent, which is not yet the
        # question the client asks. It wants "which HIDDEN combined charge is this
        # row an item of", because that is the one the user can no longer see. One
        # query for the page's candidate parents settles it — the alternative is
        # embedding the aggregate above and re-running it per row per column.
        #
        # Two shapes drop out here, both correctly: a one-way transfer pair, whose
        # counterpart is nobody's item, and a PARTIAL split, whose combined charge
        # is still listed a row away and needs no pointer back to itself.
        candidate_parents = {
            r["split_parent_id"] for r in transactions if r.get("split_parent_id")
        }
        hidden_parents = set()
        if candidate_parents:
            placeholders = ",".join("?" * len(candidate_parents))
            cursor = await db.execute(
                f"""SELECT id FROM ({_FULLY_SPLIT_PARENT_IDS_SQL})
                     WHERE id IN ({placeholders})""",
                list(candidate_parents),
            )
            hidden_parents = {r[0] for r in await cursor.fetchall()}
        for row in transactions:
            if row.get("split_parent_id") not in hidden_parents:
                row["split_parent_id"] = None
                row["split_parent_amount_cents"] = None
                row["split_parent_merchant"] = None
                row["split_sibling_count"] = 0

        return {"transactions": transactions, "total": total, "totals": totals,
                "page": page, "page_size": page_size,
                "scope": effective_scope, "scope_note": scope_note}
    finally:
        await db.close()

@router.get("/api/transactions/{txn_id}/split-items")
async def get_split_items(txn_id: int):
    """The per-item child rows a composite charge was split into.

    The parent is neutralised so its money is not counted twice, which means the
    children are the only place a split charge can actually be categorized. A
    fully-covered parent is no longer listed beside them either (see
    ``get_transactions``), so this endpoint is now also the drill-down THROUGH an
    item row back to the combined charge — hence the parent's merchant and date in
    the response, not just its amount.

    ``balanced`` is the split's core invariant — the children must sum to the
    parent. Reported rather than assumed: if it is ever false the money is being
    double-counted or lost, and that must be visible.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT id, amount_cents, merchant, description, posted_date,
                      category_path, account_id
                 FROM transactions WHERE id=?""",
            (txn_id,),
        )
        parent = await cursor.fetchone()
        if not parent:
            raise HTTPException(status_code=404, detail="Transaction not found")

        cursor = await db.execute(
            """SELECT id, posted_date, merchant, description, amount_cents, account_id,
                      category_path, entity_type, categorization_reason
               FROM transactions WHERE linked_transaction_id=?
               ORDER BY ABS(amount_cents) DESC, id""",
            (txn_id,),
        )
        items = [dict(r) for r in await cursor.fetchall()]
        child_sum = sum(int(i["amount_cents"] or 0) for i in items)
        return {
            "parent_id": txn_id,
            "parent_amount_cents": parent["amount_cents"],
            "parent_merchant": parent["merchant"],
            "parent_description": parent["description"],
            "parent_posted_date": parent["posted_date"],
            "parent_category_path": parent["category_path"],
            "items": items,
            "item_count": len(items),
            "child_sum_cents": child_sum,
            "balanced": bool(items) and child_sum == parent["amount_cents"],
        }
    finally:
        await db.close()


async def _validate_flat_category(db, category_path: str) -> None:
    """Reject legacy hierarchical paths. Read-only.

    Categories are flat names now and the pickers only offer those. A
    hierarchical path arriving here means a stale client or a hand-made request,
    and storing it would silently re-split every total that groups by category.

    An unknown FLAT name is allowed: "type a name to file this under" is how
    people actually organise spending, and the Categories manager picks it up.
    Deliberately performs NO writes — an earlier version inserted the missing
    category here and mid-request commits deadlocked against the caller's own
    connection.
    """
    if category_path and " > " in category_path:
        raise HTTPException(
            status_code=400,
            detail=(f"'{category_path}' is a legacy hierarchical path. Categories are flat "
                    f"names now (e.g. 'Housing', 'Groceries')."),
        )


# --------------------------------------------------------------------------- #
# Split-parent guard
# --------------------------------------------------------------------------- #
#
# A composite charge is "split" by inserting one child row per real item
# (``amazon_service``) or component (``mortgage_splitter``) and NEUTRALISING the
# original: the parent is flipped to ``Transfer`` / ``entity_type='transfer'`` so
# the money is counted once, through the children.
#
# Nothing used to stop a later recategorization from putting that parent back on
# a spending category, and then BOTH halves count — the live DB had txn 6967
# (−$26.74, two children summing to −$26.74) and txn 6983 (−$46.01, same shape)
# each counted twice, one via "Bulk edit" and one via "User correction".


def _split_safe_categories() -> set:
    """Categories a row with children is allowed to sit on.

    The transfer family neutralises (money counted through the children) and
    ``Excluded`` opts the whole charge out — deliberately counting neither half
    cannot double-count. Everything else would count the parent on top of its
    own items.
    """
    from app.services.amazon_matcher import SPLIT_PARENT_CATEGORY
    from app.services.flat_categories import EXCLUDED_CATEGORY, TRANSFER_CATEGORIES

    return {SPLIT_PARENT_CATEGORY, *TRANSFER_CATEGORIES, EXCLUDED_CATEGORY}


def category_neutralises_split(category_path: Optional[str]) -> bool:
    """Whether ``category_path`` is safe for a row that has split children.

    An empty/missing category is NOT safe: clearing the category un-neutralises
    the parent just as surely as naming a spending bucket does.
    """
    cp = (category_path or "").strip()
    if not cp:
        return False
    return cp in _split_safe_categories() or cp.startswith("Transfer >")


# ``linked_transaction_id`` is overloaded, so "has children" cannot simply be
# "some row points at me":
#
#   * a SPLIT is asymmetric — each child points at the parent, the parent does
#     not point back;
#   * refund pairing (``anomaly_detector._link_refund_pairs``) and transfer
#     pairing (``transfer_detector``) are SYMMETRIC — both rows point at each
#     other, and neither is anybody's split parent;
#   * paystub linking (``import_routes``) points a row at ITSELF.
#
# Counting the symmetric cases as children would freeze every refund-paired
# purchase and every paystub deposit out of recategorization, which is normal
# work the user must keep being able to do. So a child is a row that points at
# this row, is not this row, and is not pointed back at.
_CHILD_COUNT_SQL = """
    SELECT COUNT(*) FROM transactions c
     WHERE c.linked_transaction_id = ?
       AND c.id != ?
       AND c.id IS NOT (SELECT linked_transaction_id FROM transactions WHERE id = ?)
"""

_SPLIT_PARENT_IDS_SQL = """
    SELECT DISTINCT c.linked_transaction_id AS id
      FROM transactions c
      JOIN transactions p ON p.id = c.linked_transaction_id
     WHERE c.linked_transaction_id IS NOT NULL
       AND c.id != c.linked_transaction_id
       AND (p.linked_transaction_id IS NULL OR p.linked_transaction_id != c.id)
"""

# The same asymmetric-link join, narrowed to parents the children FULLY account
# for — ``SUM(children) = parent``, the ``balanced`` invariant of
# ``get_split_items``. That equality is what makes the parent safe to hide from
# the list: every cent of it is already on screen as item rows.
#
# The narrowing is not cosmetic. ``linked_transaction_id`` is asymmetric for
# transfer pairs too when only one side got linked — the live DB holds 37 of
# those, e.g. a +$5,000 "Account Transfer" whose single "child" is the −$5,000
# counterpart on the other account. Those sum to the parent NEGATED, not to the
# parent, and hiding one would delete a real row's money from the list. The
# equality test separates the two shapes exactly, with no sign heuristics.
#
# ``MIN(p.amount_cents)`` is just "the parent's amount": it is constant within
# each group, and naming it through an aggregate keeps the HAVING clause legal
# rather than relying on SQLite's bare-column tolerance.
_FULLY_SPLIT_PARENT_IDS_SQL = """
    SELECT c.linked_transaction_id AS id
      FROM transactions c
      JOIN transactions p ON p.id = c.linked_transaction_id
     WHERE c.linked_transaction_id IS NOT NULL
       AND c.id != c.linked_transaction_id
       AND (p.linked_transaction_id IS NULL OR p.linked_transaction_id != c.id)
     GROUP BY c.linked_transaction_id
    HAVING SUM(c.amount_cents) = MIN(p.amount_cents)
"""

# "Which row is this one an item OF" — the mirror of the above, per row, so a
# child can show the combined charge it came from now that the combined charge is
# no longer a peer row in the list. Asymmetry is re-checked here for the same
# reason: a transfer counterpart also has ``linked_transaction_id`` set, and it is
# nobody's item.
_SPLIT_PARENT_OF_ROW_SQL = """
    SELECT p.{column} FROM transactions p
     WHERE p.id = t.linked_transaction_id
       AND p.id != t.id
       AND (p.linked_transaction_id IS NULL OR p.linked_transaction_id != t.id)
"""


async def split_child_count(db, txn_id: int) -> int:
    """How many split children ``txn_id`` is the parent of (0 for ordinary rows)."""
    cursor = await db.execute(_CHILD_COUNT_SQL, (txn_id, txn_id, txn_id))
    row = await cursor.fetchone()
    return int(row[0] or 0) if row else 0


async def split_parent_ids(db) -> set:
    """Every row that has split children, in ONE query.

    The per-row form is fine for a single PATCH; a bulk path that asked per
    candidate would turn one statement into thousands.
    """
    cursor = await db.execute(_SPLIT_PARENT_IDS_SQL)
    return {r[0] for r in await cursor.fetchall() if r[0] is not None}


def split_parent_refusal(txn_id: int, child_count: int, amount_cents: Optional[int],
                         category_path: str) -> str:
    """The refusal in the user's terms: what the row is, and what to do instead."""
    amount = abs(int(amount_cents or 0)) / 100
    return (
        f"Transaction #{txn_id} (${amount:,.2f}) was split into {child_count} item "
        f"row{'s' if child_count != 1 else ''}, and those item rows carry the real "
        f"categories. This row is only the original combined charge, so putting it on "
        f"'{category_path}' would count the ${amount:,.2f} twice — once here and once "
        f"across its {child_count} item row{'s' if child_count != 1 else ''}. "
        f"Recategorize the individual item rows instead, or set this row to 'Excluded' "
        f"to keep the whole charge out of your totals."
    )


async def guard_split_parent(db, txn_id: int, category_path: str,
                             amount_cents: Optional[int] = None) -> None:
    """Raise 409 when ``category_path`` would make ``txn_id`` double-count."""
    if category_neutralises_split(category_path):
        return
    child_count = await split_child_count(db, txn_id)
    if not child_count:
        return
    if amount_cents is None:
        cursor = await db.execute("SELECT amount_cents FROM transactions WHERE id=?", (txn_id,))
        row = await cursor.fetchone()
        amount_cents = row[0] if row else 0
    raise HTTPException(
        status_code=409,
        detail=split_parent_refusal(txn_id, child_count, amount_cents, category_path),
    )


@router.patch("/api/transactions/{txn_id}")
async def patch_transaction(txn_id: int, patch: TransactionPatch):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM transactions WHERE id=?", (txn_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Transaction not found")

        before = dict(row)
        affected_ids = [txn_id]
        context_diff = None

        # Both refusals are decided BEFORE the first write, so a rejected patch
        # cannot leave a half-applied tag change behind. (The uncommitted
        # transaction would be discarded on close anyway; not relying on that.)
        if patch.category_path is not None:
            await _validate_flat_category(db, patch.category_path)
            # A split parent is already accounted for through its children.
            await guard_split_parent(db, txn_id, patch.category_path,
                                     before.get("amount_cents"))

        if patch.tags is not None:
            tags_json = json.dumps(patch.tags)
            await db.execute(
                "UPDATE transactions SET tags=? WHERE id=?",
                (tags_json, txn_id)
            )

        if patch.category_path is not None:
            entity_type = determine_entity_type(patch.category_path,
                                                before.get("amount_cents"))
            amt = int(before.get("amount_cents") or 0)
            reason = "User correction"
            cp = patch.category_path
            bc = before.get("business_context") or "personal"
            if violates_sign_invariant(entity_type, amt):
                new_cp, new_et, new_bc, note = coerce_sign_safe(cp, entity_type, amt, bc)
                log_violation_autofix(
                    txn_id,
                    {"category_path": cp, "entity_type": entity_type,
                     "business_context": bc, "amount_cents": amt},
                    {"category_path": new_cp, "entity_type": new_et,
                     "business_context": new_bc},
                    note or "patch sign-invariant autofix",
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Sign-invariant violation: amount {amt} cents cannot be "
                        f"'{cp}' (entity_type={entity_type}). {note}"
                    ),
                )
            await db.execute(
                """UPDATE transactions
                   SET category_path=?, entity_type=?, confidence_score=?, categorization_reason=?
                   WHERE id=?""",
                (cp, entity_type, 1.0, reason, txn_id)
            )

        if patch.apply_to_matching and patch.category_path is not None:
            tokens = [t.lower() for t in before.get("merchant", "").split() if len(t) > 2]

            candidates = []
            cursor = await db.execute(
                "SELECT id, merchant, description, amount_cents, posted_date, account_id, source_platform "
                "FROM transactions WHERE id != ?",
                (txn_id,)
            )
            all_others = await cursor.fetchall()

            for other in all_others:
                other_merchant = (other["merchant"] or "").lower()
                for token in tokens:
                    if token in other_merchant:
                        candidates.append(dict(other))
                        break

            if not candidates:
                candidates = [dict(c) for c in all_others[:50]]

            if candidates:
                prompt_lines = [
                    "A user corrected a transaction's category.",
                    "Find all transactions below that are semantically related to the corrected transaction (same person, entity, or logical payor/payee across any channel).",
                    "",
                    "Corrected transaction:",
                    f"  ID={before['id']}: {before['posted_date']} | {before['account_id']} | {before['merchant']} | ${before['amount_cents']/100:.2f} | {before.get('description', '')}",
                    f"  New category: {patch.category_path}",
                    "",
                    "Candidate transactions:",
                ]
                for c in candidates:
                    amt = f"${c['amount_cents']/100:.2f}"
                    desc = (c.get("description") or "")[:80]
                    merch = (c.get("merchant") or "")[:60]
                    prompt_lines.append(
                        f"  ID={c['id']}: {c['posted_date']} | {c['account_id']} | {merch} | {amt} | {desc}"
                    )

                prompt_lines.append("")
                prompt_lines.append(
                    'Respond with ONLY a valid JSON object: '
                    '{"related_ids": [<array of IDs>], '
                    '"pattern": "<merchant substring for rule>", '
                    '"rule_description": "<human readable>", '
                    '"context_fact": "<what to append to context>"}'
                )
                prompt = "\n".join(prompt_lines)

                try:
                    response = await asyncio.to_thread(call_llm, prompt, strong=False)
                except Exception:
                    response = None

                if response:
                    try:
                        result = json.loads(response)
                        related_ids = result.get("related_ids", [])
                        if not isinstance(related_ids, list):
                            related_ids = [related_ids]
                    except (json.JSONDecodeError, TypeError):
                        related_ids = []

                    if related_ids:
                        updated_count = 0
                        # One query, not one per id: the model can return a long
                        # list and this path is already slow enough.
                        protected = (
                            set() if category_neutralises_split(patch.category_path)
                            else await split_parent_ids(db)
                        )
                        for rid in related_ids:
                            if rid == txn_id:
                                continue
                            if rid in protected:
                                # Split parent: its children already carry the
                                # money. See guard_split_parent.
                                continue
                            # Fetch the candidate's amount so we can enforce sign.
                            cur2 = await db.execute(
                                "SELECT amount_cents, business_context FROM transactions WHERE id=?",
                                (rid,),
                            )
                            cand = await cur2.fetchone()
                            if not cand:
                                continue
                            cand_amt = int(cand["amount_cents"] or 0)
                            if violates_sign_invariant(entity_type, cand_amt):
                                log_violation_autofix(
                                    rid,
                                    {"category_path": patch.category_path,
                                     "entity_type": entity_type,
                                     "amount_cents": cand_amt},
                                    {"category_path": "skipped"},
                                    "semantic-match sign-invariant skip",
                                )
                                continue
                            await db.execute(
                                """UPDATE transactions
                                   SET category_path=?, entity_type=?, confidence_score=?, categorization_reason=?
                                   WHERE id=?""",
                                (patch.category_path, entity_type, 1.0,
                                 f"User correction via semantic match (from #{txn_id})", rid)
                            )
                            updated_count += 1
                            if rid not in affected_ids:
                                affected_ids.append(rid)

                        pattern = result.get("pattern", tokens[0] if tokens else before.get("merchant", "").lower()[:20])
                        rule_desc = result.get("rule_description", f"Merchant matching '{pattern}' → {patch.category_path}")
                        rule_id = await add_rule(db, pattern, patch.category_path, rule_desc)

                        context_fact = result.get("context_fact", rule_desc)
                        if context_fact:
                            append_context_fact(f"- {context_fact}")

                        context_diff = f"Added rule #{rule_id}: {rule_desc}"

        await db.commit()

        cursor = await db.execute("SELECT * FROM transactions WHERE id=?", (txn_id,))
        after_row = await cursor.fetchone()
        after = dict(after_row) if after_row else {}

        return {
            "re_categorized_count": len(affected_ids),
            "affected_ids": affected_ids,
            "context_diff": context_diff,
            "before": before,
            "after": after,
        }
    finally:
        await db.close()


@router.post("/api/transactions/bulk-edit")
async def bulk_edit_transactions(req: BulkEditRequest):
    """Apply one patch to many rows, skipping the rows it must not be applied to.

    A protected split parent is SKIPPED rather than failing the batch: the user
    selected a range of rows and meant the request for the ordinary ones. Failing
    the whole call would make a legitimate 40-row edit impossible because one row
    in the selection happens to be a split charge. The skipped ids come back in
    ``skipped_ids`` with a ``skipped_reason``, so the UI can say what was left out
    instead of silently reporting a smaller count.
    """
    db = await get_db()
    try:
        updated_ids = []
        skipped_ids = []
        # Only relevant when the patch moves the category, and only then does the
        # query cost anything.
        protected = (
            await split_parent_ids(db)
            if req.patch.category_path is not None
            and not category_neutralises_split(req.patch.category_path)
            else set()
        )
        for txn_id in req.ids:
            if txn_id in protected:
                skipped_ids.append(txn_id)
                continue
            cursor = await db.execute("SELECT * FROM transactions WHERE id=?", (txn_id,))
            row = await cursor.fetchone()
            if not row:
                continue
            row = dict(row)
            sets = []
            params = []
            skip_row = False
            if req.patch.category_path is not None:
                await _validate_flat_category(db, req.patch.category_path)
                entity_type = determine_entity_type(req.patch.category_path,
                                                    row.get("amount_cents"))
                amt = int(row.get("amount_cents") or 0)
                bc = row.get("business_context") or "personal"
                cp = req.patch.category_path
                reason = "Bulk edit"
                if violates_sign_invariant(entity_type, amt):
                    new_cp, new_et, new_bc, note = coerce_sign_safe(cp, entity_type, amt, bc)
                    log_violation_autofix(
                        txn_id,
                        {"category_path": cp, "entity_type": entity_type,
                         "business_context": bc, "amount_cents": amt},
                        {"category_path": new_cp, "entity_type": new_et,
                         "business_context": new_bc},
                        note or "bulk-edit sign-invariant autofix",
                    )
                    if new_cp == "Uncategorized":
                        skip_row = True
                    else:
                        cp, entity_type = new_cp, new_et
                        reason = note or reason
                if skip_row:
                    continue
                sets.append("category_path=?")
                params.append(cp)
                sets.append("entity_type=?")
                params.append(entity_type)
                sets.append("confidence_score=?")
                params.append(1.0)
                sets.append("categorization_reason=?")
                params.append(reason)
            if req.patch.business_context is not None:
                sets.append("business_context=?")
                params.append(req.patch.business_context)
            if req.patch.tags is not None:
                sets.append("tags=?")
                params.append(json.dumps(req.patch.tags))
            if req.patch.notes is not None:
                sets.append("notes=?")
                params.append(req.patch.notes)
            if sets:
                params.append(txn_id)
                await db.execute(
                    f"UPDATE transactions SET {', '.join(sets)} WHERE id=?",
                    params
                )
                updated_ids.append(txn_id)
        await db.commit()
        return {
            "updated_count": len(updated_ids),
            "updated_ids": updated_ids,
            "skipped_ids": skipped_ids,
            "skipped_count": len(skipped_ids),
            "skipped_reason": (
                f"{len(skipped_ids)} row(s) were left unchanged: each is a combined "
                f"charge that was split into item rows, and the item rows carry the "
                f"real categories. Recategorizing the combined row would count the "
                f"same money twice. Edit the item rows instead, or set the combined "
                f"row to 'Excluded'."
            ) if skipped_ids else None,
        }
    finally:
        await db.close()


async def _find_similar(
    db,
    txn_id: int,
    category_path: str,
    *,
    skip_already_in_category: bool = True,
) -> dict:
    """The rows that are the same thing as ``txn_id`` and could take its category.

    Shared by ``GET /{id}/similar`` (what the user reviews) and
    ``POST /apply-similar`` (what actually gets written), deliberately as ONE
    function: if the two could drift, the list the user approved would stop being
    the list the server writes.

    No LLM is involved. Matching is a key comparison over a single ``SELECT`` of
    the table, which keeps ~10k rows well inside a few tens of milliseconds — the
    user is waiting on this with a dropdown still open.

    **"The same thing" is not always "the same merchant."** For an Amazon per-item
    row the merchant is "Amazon: <product>", and every Amazon row on the ledger —
    1,325 item rows and hundreds of bare charges — shares the token ``amazon``, so
    a merchant key answers a question nobody asked. Filing one
    ``Amazon: Fresh Step … Cat Litter`` row under Pets offered 56 matches, mostly
    bare ``Amazon`` rows of unrelated amounts and a few unrelated products, and not
    one of them was cat litter. When the source row carries a product identity
    (``services/amazon_product_key``) the match becomes the PRODUCT — same ASIN
    first, a truncation-tolerant product-name prefix otherwise — and candidates
    must be Amazon item rows with the same product. A bare ``Amazon`` row names no
    product and is therefore never a match for one. Everything else on the ledger
    keeps the merchant-key path exactly as it was.

    Six exclusions, each because including the row would corrupt a total rather
    than merely annoy:

    * the source row itself (already written by the caller);
    * rows whose category is a transfer — moving a checking-to-card payment into
      "Groceries" makes the card's own grocery rows count twice;
    * ``Excluded`` rows — the owner opted those out by hand, and a bulk apply
      must not quietly opt them back in;
    * **split parents** — a combined charge already counted through its item
      rows. This is the right layer for that check: a row that cannot legally
      take the category must never be OFFERED, not merely refused on write;
    * **Amazon-matched rows** — their category comes from the item export (the
      actual product, or what was returned), which is strictly better
      information than a merchant key. An apply-similar on "Amazon" overwrote
      107 correctly-booked refunds in the live DB. On the product path this
      exclusion narrows to REFUND-matched rows only: an item row's product is
      precisely what the source row is being matched on, so excluding item rows
      there would exclude the only correct answers, while a refund's category
      still comes from what was returned and must not be swept;
    * rows where the target category would violate the sign invariant.

    ``skip_already_in_category`` drops rows that are already there. The review
    list wants that (they are not changes). Validating a client's ``ids`` does
    NOT, because a row that reached the category by another route is a legitimate
    thing to have ticked, not evidence of a forged request.
    """
    from app.services.amazon_product_key import (
        pattern_matches_every_item_row,
        product_identity,
        safe_rule_pattern,
        same_product,
    )
    from app.services.flat_categories import EXCLUDED_CATEGORY, TRANSFER_CATEGORIES
    from app.services.merchant_key import (
        match_strength,
        merchant_key,
        merchant_key_and_tokens,
    )

    cursor = await db.execute("SELECT * FROM transactions WHERE id=?", (txn_id,))
    row = await cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")
    source = dict(row)

    # Does this row identify a PRODUCT? ``amazon_items.split_transaction_id`` is
    # the authoritative link — it points at the per-item row, and at the charge
    # itself for a single-item order that ``split_matched_charges`` relabelled in
    # place instead of splitting. The merchant string is the fallback for the 990
    # item rows whose links a ``reset_matches`` cleared.
    cursor = await db.execute(
        "SELECT asin, product_name FROM amazon_items WHERE split_transaction_id=? "
        "ORDER BY id LIMIT 1",
        (txn_id,),
    )
    item_row = await cursor.fetchone()
    source_product = product_identity(
        item_row["asin"] if item_row else None,
        item_row["product_name"] if item_row else None,
        source.get("merchant"),
    )

    source_key, source_tokens = merchant_key_and_tokens(
        source.get("merchant"), source.get("description")
    )

    # Label the match by whatever actually identifies the payee. When the
    # merchant field contributed nothing to the key it is a rail name
    # ("Zelle", "TST") and the payee came from the description, so showing the
    # merchant would label the group after the payment network instead of the
    # person — "Apply to 3 similar Zelle transactions" is exactly the ambiguity
    # this feature has to avoid.
    merchant_contributes = bool(merchant_key(source.get("merchant"), None))
    label = (source.get("merchant") or "").strip() if merchant_contributes else ""
    if not label:
        label = source_key.title()

    result = {
        "source": {
            "id": source["id"],
            "merchant": source.get("merchant"),
            "merchant_key": source_key,
            "category_path": source.get("category_path"),
            "product_name": source_product.name or None,
            "asin": source_product.asin,
        },
        # Which identity the match list was built from, so the client can say what
        # it is showing instead of implying "same merchant" for a product match.
        "match_kind": "amazon_product" if source_product else "merchant",
        "match_key": source_key,
        "match_label": label,
        "suggested_pattern": source_key,
        # Set when no rule can be persisted safely — see ``safe_rule_pattern``.
        "rule_note": None,
        "matches": [],
    }

    if source_product:
        # The group is a product, so it must be NAMED after the product: the whole
        # list said "Amazon" before, which is exactly why the user could not tell
        # what was about to be recategorized. The full product name is used, not
        # the 60-character merchant truncation.
        result["match_key"] = source_product.key
        result["match_label"] = source_product.name
        result["suggested_pattern"] = safe_rule_pattern(source_product)
        if not result["suggested_pattern"]:
            result["rule_note"] = (
                "No automatic rule was saved for this product. A rule is matched as "
                "a substring against every future merchant and description, and the "
                "only string this product's rows share with each other is 'Amazon' — "
                "which would recategorize every unrelated Amazon charge on the next "
                "import. The rows you pick here are still updated."
            )
    else:
        if pattern_matches_every_item_row(source_key):
            # A bare "Amazon" charge keys to "amazon", and that pattern matches every
            # "Amazon: <product>" row ever imported. Applying the category to the
            # rows in this list is what the user asked for; persisting it as a rule
            # would overwrite the per-product categories on the next import, before
            # the item export is even read. So: no rule, and say why.
            result["suggested_pattern"] = ""
            result["rule_note"] = (
                "No automatic rule was saved. A rule for 'Amazon' is matched as a "
                "substring, so it would also claim every 'Amazon: <product>' item row "
                "on the next import and replace the per-product categories with this "
                "one. The rows you pick here are still updated."
            )
        if not source_key:
            # An empty key means the text named nobody ("ONLINE TRANSFER TO CHK").
            # The only safe reading is "no match possible" — falling back to a looser
            # rule here would offer to recategorize the whole ledger.
            return result

    skip_categories = {*TRANSFER_CATEGORIES, EXCLUDED_CATEGORY}
    skip_entity_types = {"transfer", "excluded", "asset_conversion"}

    # Rows whose category is derived from better information than the key being
    # matched on, in ONE query rather than one per candidate — this function answers
    # while a dropdown is open and is called on every inline edit. Split parents
    # come from the asymmetric-link join; Amazon rows from the indexed
    # ``matched_transaction_id`` / ``split_transaction_id`` columns.
    #
    # On the PRODUCT path the Amazon-item exclusion is dropped, and only there:
    # those rows are the ones whose product is being compared, so excluding them
    # would exclude every correct answer — it is what left the cat-litter search
    # with 56 matches, none of which were cat litter. Refund-matched rows stay
    # excluded on both paths: a refund's category comes from what was returned.
    amazon_item_exclusion = "" if source_product else """
            UNION
            SELECT matched_transaction_id FROM amazon_items
             WHERE matched_transaction_id IS NOT NULL"""
    cursor = await db.execute(
        f"""{_SPLIT_PARENT_IDS_SQL}{amazon_item_exclusion}
            UNION
            SELECT matched_transaction_id FROM amazon_refunds
             WHERE matched_transaction_id IS NOT NULL"""
    )
    protected_ids = {r[0] for r in await cursor.fetchall() if r[0] is not None}

    # Product identities for every Amazon item row that still has a link, in one
    # query. Rows missing from this map fall back to their merchant string, which
    # is where a reset-orphaned item row's product name lives.
    item_identities = {}
    if source_product:
        cursor = await db.execute(
            "SELECT split_transaction_id AS id, asin, product_name FROM amazon_items "
            "WHERE split_transaction_id IS NOT NULL"
        )
        item_identities = {
            r["id"]: (r["asin"], r["product_name"]) for r in await cursor.fetchall()
        }

    # Split CHILDREN need a source-dependent rule rather than a blanket exclusion.
    #
    # Every per-item child is named "Amazon: <product>", so its merchant key
    # contains the token "amazon" and ``match_strength`` scores it a subset
    # "strong" match of a bare ``Amazon`` charge. Left alone, recategorizing one
    # bare Amazon row offered to sweep 1,342 rows into a single category, flattening
    # the product-level categories (Health, Pets, Housing…) that the split exists to
    # produce.
    #
    # But a blanket exclusion would break the case that IS useful: filing one
    # component row of a split (a mortgage escrow line, say) and applying that to
    # the other rows like it, which are all children too. So children are offered
    # only when the source is itself a child — like propagates to like.
    #
    # This whole rule is now reached only by a source with NO product identity. An
    # Amazon item row goes down the product path instead, where identity decides
    # which children are the same thing; the "source is a child, so any child may
    # match" hatch is precisely what let the product tokens be ignored and put 56
    # unrelated rows in front of the user.
    if not source_product:
        # "Is this a split child?" cannot rely on ``linked_transaction_id`` alone:
        # ``reset_matches`` clears those links when the authoritative source
        # changes, and 490 product rows in the live DB are orphaned that way. Being
        # referenced by ``amazon_items.split_transaction_id`` still identifies them.
        cursor = await db.execute(
            """SELECT id FROM transactions WHERE linked_transaction_id IS NOT NULL
               UNION
               SELECT split_transaction_id FROM amazon_items
                WHERE split_transaction_id IS NOT NULL"""
        )
        split_child_ids = {r[0] for r in await cursor.fetchall() if r[0] is not None}
        source_is_split_child = (
            source.get("linked_transaction_id") is not None
            or source["id"] in split_child_ids
        )
        if not source_is_split_child:
            protected_ids |= split_child_ids

    # Scope, for the same reason every other view has it: with Plaid linked, each
    # real transaction also exists as a superseded CSV row, and offering to
    # recategorize rows the user cannot see anywhere in the app just doubles every
    # count in the review list. The source row's own scope decides, so editing a
    # historical CSV row still propagates within the history.
    scope_sql, scope_params, _effective, _note = await resolve_scope(db, "auto")
    scope_clause = scope_sql.replace(" AND t.", " AND ") if scope_sql else ""
    source_in_scope = True
    if scope_clause:
        cursor = await db.execute(
            f"SELECT 1 FROM transactions WHERE id = ? {scope_clause}",
            (txn_id, *scope_params),
        )
        source_in_scope = await cursor.fetchone() is not None

    cursor = await db.execute(
        f"""SELECT id, posted_date, merchant, description, amount_cents, account_id,
                  category_path, entity_type, business_context
           FROM transactions WHERE id != ? {scope_clause if source_in_scope else ''}""",
        (txn_id, *(scope_params if source_in_scope else ())),
    )
    matches = []
    for cand in await cursor.fetchall():
        cand = dict(cand)
        if cand["id"] in protected_ids:
            continue
        current = cand.get("category_path") or ""
        if current in skip_categories or current.startswith("Transfer >"):
            continue
        if (cand.get("entity_type") or "") in skip_entity_types:
            continue
        already = current == category_path
        if already and skip_already_in_category:
            continue

        if source_product:
            cand_asin, cand_name = item_identities.get(cand["id"], (None, None))
            cand_product = product_identity(cand_asin, cand_name, cand.get("merchant"))
            # No product identity, no match: that is the rule that keeps a bare
            # "Amazon" charge — which names no product at all — out of a specific
            # product's list.
            if not same_product(source_product, cand_product):
                continue
            # Amazon's own product id agreeing is as exact as identity gets; a
            # name-prefix agreement is strong but inferred.
            strength = (
                "exact" if source_product.asin and cand_product.asin else "strong"
            )
            # What this row actually IS. The review list showed "Amazon" for every
            # match, which is unreviewable — the user could not tell cat litter from
            # tracing paper.
            cand["product_name"] = cand_product.name
        else:
            # An item row is a different granularity from a charge: the source names
            # a payee, the candidate names one product inside somebody's order. A
            # merchant key cannot tell them apart — "amazon" is a subset of
            # "amazon fresh step clean paws…" and therefore a "strong" match — so
            # this is the check that stops one bare Amazon edit from flattening
            # every product-level category. The link-based exclusion above cannot do
            # it alone: 265 item rows in the live DB lost their links to a
            # ``reset_matches`` and are identifiable only by the merchant string.
            if product_identity(merchant=cand.get("merchant")):
                continue
            cand_key, cand_tokens = merchant_key_and_tokens(
                cand.get("merchant"), cand.get("description")
            )
            strength = match_strength(source_key, source_tokens, cand_key, cand_tokens)
            # "partial" is computed but never accepted: a wrong row here is written
            # without the user ever seeing it again.
            if strength not in ("exact", "strong"):
                continue
            cand["product_name"] = None

        amt = int(cand.get("amount_cents") or 0)
        if violates_sign_invariant(determine_entity_type(category_path, amt), amt):
            continue

        cand["strength"] = strength
        cand["already_in_category"] = already
        matches.append(cand)

    matches.sort(key=lambda m: (m["posted_date"] or "", m["id"]), reverse=True)
    result["matches"] = matches
    return result


@router.get("/api/transactions/{txn_id}/similar")
async def get_similar_transactions(
    txn_id: int,
    category_path: str = Query(..., description="The category the user just assigned"),
    limit: int = Query(500, ge=1, le=5000),
):
    """Preview which other rows would take the category just assigned to ``txn_id``.

    Read-only on purpose: the user sees the count and the rows before anything is
    written, which is what makes a deterministic matcher preferable to an LLM
    here — the reasoning is a key they can read: a merchant key, or a product
    (``match_kind``), in which case every row carries the ``product_name`` that put
    it in the list.
    """
    db = await get_db()
    try:
        found = await _find_similar(db, txn_id, category_path)
        matches = found.pop("matches")
        page = [
            {
                "id": m["id"],
                "posted_date": m["posted_date"],
                "merchant": m["merchant"],
                "description": m["description"],
                "amount_cents": m["amount_cents"],
                "account_id": m["account_id"],
                "category_path": m["category_path"],
                "strength": m["strength"],
                # What the row is, when the match was made on a product. Never a
                # fallback to the merchant: "Amazon" is the label that made this
                # list unreviewable in the first place.
                "product_name": m.get("product_name"),
            }
            for m in matches[:limit]
        ]
        return {
            **found,
            # ``count`` is the true total; ``matches`` is a page of it. A UI that
            # showed len(matches) as the total would understate what an
            # ``all_matching`` apply is about to touch.
            "count": len(matches),
            "returned": len(page),
            "truncated": len(matches) > len(page),
            "matches": page,
        }
    finally:
        await db.close()


@router.post("/api/transactions/apply-similar")
async def apply_similar_transactions(req: ApplySimilarRequest):
    """Write the user's category onto the rows that name the same merchant.

    Same safety as ``bulk_edit_transactions``: flat-category validation,
    sign-derived ``entity_type``, and a row skipped rather than mangled when the
    invariant would coerce it to ``Uncategorized``.
    """
    db = await get_db()
    try:
        await _validate_flat_category(db, req.category_path)
        # Validation needs rows already in the category to count as genuine
        # matches; the apply set does not.
        found = await _find_similar(
            db, req.source_id, req.category_path, skip_already_in_category=False
        )
        by_id = {m["id"]: m for m in found["matches"]}

        if req.all_matching:
            targets = [m for m in found["matches"] if not m["already_in_category"]]
        else:
            unknown = [i for i in req.ids if i not in by_id]
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{len(unknown)} id(s) are not similar-merchant matches of "
                        f"#{req.source_id}: {unknown[:10]}. Re-fetch the match list."
                    ),
                )
            targets = [by_id[i] for i in req.ids]

        updated_ids = []
        skipped = 0
        for row in targets:
            amt = int(row.get("amount_cents") or 0)
            cp = req.category_path
            entity_type = determine_entity_type(cp, amt)
            bc = row.get("business_context") or "personal"
            reason = f"Applied from user correction on #{req.source_id} (similar merchant)"
            if violates_sign_invariant(entity_type, amt):
                new_cp, new_et, new_bc, note = coerce_sign_safe(cp, entity_type, amt, bc)
                log_violation_autofix(
                    row["id"],
                    {"category_path": cp, "entity_type": entity_type,
                     "business_context": bc, "amount_cents": amt},
                    {"category_path": new_cp, "entity_type": new_et,
                     "business_context": new_bc},
                    note or "apply-similar sign-invariant autofix",
                )
                if new_cp == "Uncategorized":
                    skipped += 1
                    continue
                cp, entity_type = new_cp, new_et
                reason = note or reason
            await db.execute(
                """UPDATE transactions
                   SET category_path=?, entity_type=?, confidence_score=?,
                       categorization_reason=?
                   WHERE id=?""",
                (cp, entity_type, 1.0, reason, row["id"]),
            )
            updated_ids.append(row["id"])
        await db.commit()

        # Rule + context are how the decision survives the next import. Both are
        # best-effort: the categorization above is the user's actual request, and
        # a failed side-effect must not roll it back or return an error for work
        # that succeeded.
        rule_id = None
        context_updated = False
        is_product = found.get("match_kind") == "amazon_product"
        key_kind = "product" if is_product else "merchant key"
        if req.create_rule and found["suggested_pattern"]:
            pattern = found["suggested_pattern"]
            try:
                # Reuse an equivalent rule: this offer appears on every inline
                # edit, so accepting it twice for the same merchant must not stack
                # up identical rules.
                rule_id, _created = await add_rule_once(
                    db, pattern, req.category_path,
                    f"'{found['match_label']}' → {req.category_path} (from user correction)",
                )
            except Exception:
                logger.exception("apply-similar: failed to persist rule for %r", pattern)
            try:
                context_updated = bool(append_context_fact(
                    f"- Transactions from '{found['match_label']}' "
                    f"({key_kind} '{pattern}') are categorized as {req.category_path}."
                ))
            except Exception:
                logger.exception("apply-similar: failed to append context fact")

        return {
            "updated_count": len(updated_ids),
            "updated_ids": updated_ids,
            "skipped_count": skipped,
            "rule_id": rule_id,
            "context_updated": context_updated,
            # Why no rule exists, when the client asked for one and the matcher
            # could not express one safely. Silence here would read as "saved".
            "rule_note": (
                found.get("rule_note") if req.create_rule and rule_id is None else None
            ),
        }
    finally:
        await db.close()


@router.get("/api/transactions/export.csv")
async def export_transactions_csv(
    account_id: Optional[str] = None,
    tag: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category_path: Optional[str] = None,
    business_context: Optional[str] = None,
    entity_type: Optional[str] = None,
    merchant: Optional[str] = None,
    search: Optional[str] = None,
    flagged: Optional[bool] = None,
):
    db = await get_db()
    try:
        clauses = []
        params = []
        if account_id:
            clauses.append("account_id=?")
            params.append(account_id)
        if tag:
            clauses.append("tags LIKE ?")
            params.append(f"%{tag}%")
        if start_date:
            clauses.append("posted_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("posted_date <= ?")
            params.append(end_date)
        if category_path:
            clauses.append("category_path LIKE ?")
            params.append(f"{category_path}%")
        if business_context:
            clauses.append("business_context=?")
            params.append(business_context)
        if entity_type:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if merchant:
            clauses.append("merchant LIKE ?")
            params.append(f"%{merchant}%")
        if search:
            clauses.append("(merchant LIKE ? OR description LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%"])
        if flagged is not None:
            clauses.append("flagged=?")
            params.append(1 if flagged else 0)

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        cursor = await db.execute(
            f"SELECT * FROM transactions {where} ORDER BY posted_date DESC",
            params
        )
        rows = await cursor.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "id", "date", "account", "merchant", "description", "amount",
            "category", "entity_type", "business_context", "tags", "notes",
            "flagged", "flag_reason", "confidence", "reason"
        ])
        for r in rows:
            writer.writerow([
                r["id"], r["posted_date"], r["account_id"], r["merchant"],
                r["description"], r["amount_cents"], r["category_path"],
                r["entity_type"], r["business_context"], r["tags"],
                r["notes"], r["flagged"], r["flag_reason"],
                r["confidence_score"], r["categorization_reason"],
            ])

        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=transactions.csv"},
        )
    finally:
        await db.close()


@router.get("/api/dashboard/category-detail")
async def get_dashboard_category_detail(
    path: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000),
    sort: str = "posted_date",
    dir: str = "desc",
    scope: Optional[str] = "auto",
):
    """The transactions behind one category total.

    This is the drill-down under the Income/Expenses breakdown, so it must select
    EXACTLY the rows ``/api/dashboard`` counted for that category — otherwise the
    listed rows do not add up to the number the user clicked, and there is no way
    to tell a real discrepancy from a mismatched query. Three things that used to
    break that, all fixed here:

    * **Prefix matching.** ``category_path LIKE 'Other%'`` also matched
      ``Other Income``. Categories are flat names now, so the match is exact.
    * **Scope.** The dashboard restricts rows to the live accounts; this did not,
      so superseded CSV rows showed up under a Plaid-scoped total.
    * **Transfers / excluded.** The dashboard leaves those out of every total.

    ``sort=abs_amount`` orders by magnitude, which is what "biggest first" means
    for spending: expenses are negative, so a plain descending sort on
    ``amount_cents`` would put the SMALLEST expense at the top.
    """
    db = await get_db()
    try:
        sort_exprs = {
            "posted_date": "t.posted_date",
            "amount_cents": "t.amount_cents",
            "abs_amount": "ABS(t.amount_cents)",
            "merchant": "t.merchant",
            "description": "t.description",
        }
        order_expr = sort_exprs.get(sort, "t.posted_date")
        if dir not in ("asc", "desc"):
            dir = "desc"

        offset = (page - 1) * page_size

        scope_sql, scope_params, effective_scope, _note = await resolve_scope(db, scope)

        clauses = [
            "t.category_path = ?",
            "(t.entity_type IS NULL OR t.entity_type NOT IN "
            "('transfer', 'asset_conversion', 'excluded'))",
        ]
        params = [path]
        if start_date:
            clauses.append("t.posted_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("t.posted_date <= ?")
            params.append(end_date)

        # resolve_scope returns a fragment that already begins with " AND ".
        where = "WHERE " + " AND ".join(clauses) + scope_sql
        params = params + list(scope_params)

        cursor = await db.execute(f"SELECT COUNT(*) as total FROM transactions t {where}", params)
        total_row = await cursor.fetchone()
        total = total_row[0] if total_row else 0

        # The net is what reconciles against the dashboard, so it must use the SAME
        # arithmetic — including refund attribution across periods. A plain
        # SUM(amount_cents) here disagreed with the dashboard the moment refunds
        # started being applied at their charge: Shopping read $12,532.04 on the
        # breakdown and $12,891.06 in the drill-down, a $359.02 gap that is exactly
        # the refunds whose credit falls in another month.
        cursor = await db.execute(
            f"SELECT COALESCE(SUM({effective_amount_sql('t')}), 0), "
            f"       COALESCE(SUM(t.amount_cents), 0) "
            f"FROM transactions t {where}",
            params,
        )
        net_row = await cursor.fetchone()
        net_cents = net_row[0] or 0
        raw_net_cents = net_row[1] or 0

        # ``effective_amount_cents`` per row, so the UI can show WHY a listed
        # amount does not contribute what it appears to — a refunded charge is
        # still a real row the user made, it just no longer counts.
        cursor = await db.execute(
            f"SELECT t.id, t.posted_date, t.merchant, t.description, t.amount_cents, "
            f"t.category_path, t.account_id, t.source_file, t.flagged, "
            f"t.refund_match_id, t.refund_role, t.refund_partial, "
            f"{effective_amount_sql('t')} AS effective_amount_cents "
            f"FROM transactions t {where} ORDER BY {order_expr} {dir}, t.id DESC "
            f"LIMIT ? OFFSET ?",
            params + [page_size, offset]
        )
        transactions = [dict(r) for r in await cursor.fetchall()]

        return {
            "transactions": transactions,
            "total": total,
            "net_cents": net_cents,
            # The un-attributed sum, so a caller can show the difference rather
            # than wonder about it.
            "raw_net_cents": raw_net_cents,
            "page": page,
            "page_size": page_size,
            "scope": effective_scope,
        }
    finally:
        await db.close()


@router.get("/api/amortization")
async def get_amortization(scope: Optional[str] = "plaid"):
    """Mortgage position derived from ACTUAL transactions, with an honest anchor.

    Two bugs this replaces:

    1. The paid totals were summed UNSCOPED, so the CSV history and its Plaid
       twins were both counted — $271,821 + $274,007 for the same payments.
    2. ``original_loan`` was taken from the first row of the amortization PDF.
       That PDF is a FORWARD PROJECTION starting 2026-06-01, not the loan's
       history, so its first row is a mid-life balance. Treating it as
       origination is what made the tab look like the loan began in 2026, and it
       made "principal paid" exceed the loan.

    What is actually knowable here:
      * The lender schedule gives a BALANCE AT A KNOWN DATE (the anchor).
      * The ledger gives real principal payments, which reduce that balance from
        the anchor forward.
      * The original loan amount is NOT derivable from either — payments predate
        the imported window. It is reported as null rather than invented.
    """
    from datetime import datetime, timezone

    db = await get_db()
    try:
        scope_sql, _p, effective_scope, _n = await resolve_scope(db, scope)
        today_ym = datetime.now(timezone.utc).strftime("%Y-%m")

        cursor = await db.execute("SELECT * FROM amortization ORDER BY month")
        rows = [dict(r) for r in await cursor.fetchall()]

        anchor_month = rows[0]["month"][:7] if rows else None
        anchor_balance_cents = rows[0]["ending_balance_cents"] if rows else None

        async def paid_since(category_path, since_month=None):
            clauses = ["category_path = ?"]
            params = [category_path]
            if since_month:
                clauses.append("posted_date >= ?")
                params.append(f"{since_month}-01")
            cursor = await db.execute(
                f"""SELECT COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END), 0) total,
                           COUNT(*) n
                    FROM transactions t
                    WHERE {' AND '.join(clauses)} {scope_sql}""",
                params,
            )
            row = await cursor.fetchone()
            return row[0], row[1]

        principal_all, principal_count = await paid_since("Transfer > Mortgage Principal")
        interest_all, _ = await paid_since("Expense > Housing > Mortgage Interest")
        escrow_all, _ = await paid_since("Transfer > Mortgage Escrow")
        principal_since_anchor, payments_since_anchor = await paid_since(
            "Transfer > Mortgage Principal", anchor_month
        )


        # Actual payments by month, straight from the ledger.
        cursor = await db.execute(
            f"""SELECT substr(posted_date, 1, 7) month,
                       COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END), 0) principal_cents,
                       COUNT(*) payment_count
                FROM transactions t
                WHERE category_path = 'Transfer > Mortgage Principal' {scope_sql}
                GROUP BY month ORDER BY month"""
        )
        actual_by_month = [dict(r) for r in await cursor.fetchall()]

        # Split each month's payments into principal vs interest using the lender
        # schedule, instead of crediting the whole payment to principal.
        #
        # A regular monthly payment is principal + interest (+ escrow) — counting
        # all of it as principal understates the balance, and it is why
        # total_interest_paid read $0.00 for a mortgage that obviously accrues
        # interest. Anything paid ABOVE the scheduled P&I in a month is an extra
        # paydown and is 100% principal. No merchant matching needed:
        #   principal = min(paid, scheduled_P) + max(0, paid - scheduled_P - scheduled_I)
        by_month_paid = {m["month"]: m["principal_cents"] for m in actual_by_month}
        sched_by_month = {r["month"][:7]: r for r in rows}

        principal_applied = 0
        interest_applied = 0
        unscheduled_months = []
        for month, paid in sorted(by_month_paid.items()):
            if anchor_month and month < anchor_month:
                continue  # already reflected in the anchor balance
            sched = sched_by_month.get(month)
            if not sched:
                # No schedule for this month: cannot separate interest, so treat
                # the payment as principal and say so rather than inventing a split.
                principal_applied += paid
                unscheduled_months.append(month)
                continue
            sched_p = sched["principal_cents"]
            sched_i = sched["interest_cents"]
            scheduled_principal = min(paid, sched_p)
            extra = max(0, paid - sched_p - sched_i)
            principal_applied += scheduled_principal + extra
            interest_applied += min(max(0, paid - scheduled_principal), sched_i)

        current_balance_cents = None
        if anchor_balance_cents is not None:
            current_balance_cents = max(0, anchor_balance_cents - principal_applied)

        # Individual payments, so the page can show what it is based on.
        cursor = await db.execute(
            f"""SELECT id, posted_date, amount_cents, merchant, description, account_id
                FROM transactions t
                WHERE category_path = 'Transfer > Mortgage Principal' {scope_sql}
                ORDER BY posted_date DESC LIMIT 200"""
        )
        payments = [dict(r) for r in await cursor.fetchall()]

        # The lender schedule stays a projection and is labelled as one.
        schedule = []
        for r in rows:
            row_month = r["month"][:7] if r["month"] else ""
            schedule.append({
                "id": r["id"],
                "month": r["month"],
                "principal_cents": r["principal_cents"],
                "interest_cents": r["interest_cents"],
                "ending_balance_cents": r["ending_balance_cents"],
                "is_past": row_month <= today_ym,
                "is_projection": True,
            })

        return {
            "schedule": schedule,
            "actual_by_month": actual_by_month,
            "payments": payments,
            "summary": {
                # Not derivable from the data we hold — say so instead of guessing.
                "original_loan_cents": None,
                "anchor_month": anchor_month,
                "anchor_balance_cents": anchor_balance_cents,
                "current_balance_cents": current_balance_cents,
                "principal_paid_since_anchor_cents": principal_applied,
                "interest_paid_since_anchor_cents": interest_applied,
                "payments_since_anchor_total_cents": principal_since_anchor,
                "payments_since_anchor": payments_since_anchor,
                "total_principal_paid_cents": principal_all,
                "total_principal_payment_count": principal_count,
                "total_interest_paid_cents": interest_all,
                "total_escrow_paid_cents": escrow_all,
                "scope": effective_scope,
            },
            "unscheduled_payment_months": unscheduled_months,
            "notes": [
                (f"Balance is anchored to the lender schedule at {anchor_month} "
                 f"(${(anchor_balance_cents or 0)/100:,.2f}) and reduced by actual principal "
                 f"payments recorded since then."
                 if anchor_month else
                 "No lender schedule imported, so the outstanding balance cannot be stated."),
                ("The original loan amount is not derivable from the imported data — the loan "
                 "predates the transaction window — so it is not shown rather than guessed."),
                (f"Totals use the {effective_scope} scope. Summing every source would count the "
                 f"same payment twice, once from the CSV history and once from Plaid."),
                ("Each month's payments are split using the lender schedule: up to the scheduled "
                 "principal counts as principal, the scheduled interest as interest, and anything "
                 "paid above scheduled P&I is an extra paydown (100% principal). Crediting the "
                 "whole monthly payment to principal would understate the balance."),
            ],
        }
    finally:
        await db.close()


@router.get("/api/transactions/{txn_id}")
async def get_transaction(txn_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM transactions WHERE id=?", (txn_id,))
        row = await cursor.fetchone()
        if not row:
            return {"error": "not found"}
        return dict(row)
    finally:
        await db.close()

@router.get("/api/dashboard")
async def get_dashboard(start_date: Optional[str] = None, end_date: Optional[str] = None,
                        scope: Optional[str] = "auto"):
    # Imported locally to keep the module's shared import line untouched while
    # several agents are editing this file.
    from app.services.scope import (
        EFFECTIVE_EXPENSE_SUM,
        EFFECTIVE_INCOME_SUM,
        EFFECTIVE_NET_SUM,
    )

    db = await get_db()
    try:
        scope_sql, scope_params, effective_scope, scope_note = await resolve_scope(db, scope)
        where_clause = scope_sql
        params = list(scope_params)
        if start_date:
            where_clause += " AND t.posted_date >= ?"
            params.append(start_date)
        if end_date:
            where_clause += " AND t.posted_date <= ?"
            params.append(end_date)

        # Flat model: aggregate per category, then let each category's NET sign
        # decide which side it lands on. A refund (positive row on a spending
        # category) therefore reduces that category instead of inflating income,
        # with no special entity type involved.
        #
        # The net is over EFFECTIVE amounts, so a purchase refunded in a later
        # month is netted at the charge rather than only inside a window that
        # happens to contain both halves. Consequence, stated plainly: a past
        # month's total changes when a later refund arrives — see
        # services/scope.py and GET /api/refunded, which lists every pair with its
        # outstanding amount so that movement is inspectable.
        cursor = await db.execute(
            f"""SELECT t.category_path,
                       {EFFECTIVE_NET_SUM} as net,
                       COUNT(*) as n
                FROM transactions t
                WHERE (t.entity_type IS NULL OR t.entity_type NOT IN ('transfer', 'asset_conversion', 'excluded'))
                  AND t.category_path IS NOT NULL
                  AND t.category_path != 'Uncategorized'
                  {where_clause}
                GROUP BY t.category_path""",
            params
        )
        by_category = []
        total_income = 0
        total_expense = 0
        for row in await cursor.fetchall():
            net = row["net"] or 0
            expense_cents = -net if net < 0 else 0
            income_cents = net if net > 0 else 0
            total_income += income_cents
            total_expense += expense_cents
            by_category.append({
                "category_path": row["category_path"],
                "income_cents": income_cents,
                "expense_cents": expense_cents,
                "net_cents": net,
                "transaction_count": row["n"],
            })
        by_category.sort(key=lambda c: (-c["expense_cents"], -c["income_cents"]))

        cursor = await db.execute(
            f"""SELECT COUNT(*) as cnt
                FROM transactions t
                WHERE (t.entity_type IS NULL OR t.entity_type NOT IN ('transfer', 'asset_conversion', 'excluded'))
                  {where_clause}""",
            params
        )
        row = await cursor.fetchone()
        transaction_count = row[0] if row else 0

        cursor = await db.execute(
            f"""SELECT substr(t.posted_date, 1, 7) as month,
                       {EFFECTIVE_INCOME_SUM} as income,
                       {EFFECTIVE_EXPENSE_SUM} as expense
                FROM transactions t
                WHERE (t.entity_type IS NULL OR t.entity_type NOT IN ('transfer', 'asset_conversion', 'excluded'))
                  {where_clause}
                GROUP BY month
                ORDER BY month""",
            params
        )
        monthly = []
        for row in await cursor.fetchall():
            monthly.append({
                "month": row["month"],
                "income_cents": row["income"],
                "expense_cents": row["expense"],
            })

        return {
            "totals": {
                "total_income_cents": total_income,
                "total_expense_cents": total_expense,
                "net_cents": total_income - total_expense,
            },
            "by_category": by_category,
            "monthly": monthly,
            "transaction_count": transaction_count,
            # Always report the scope actually applied — an auto-resolved scope
            # must never be invisible, or a number can't be interpreted.
            "scope": effective_scope,
            "scope_note": scope_note,
        }
    finally:
        await db.close()


@router.get("/api/excluded")
async def get_excluded(start_date: Optional[str] = None, end_date: Optional[str] = None,
                       scope: Optional[str] = "plaid", limit: int = 500):
    """Transactions the owner has opted out of every total.

    These are real external transactions deliberately not counted — a bill
    fronted for someone who paid it back, a one-off that would distort a
    category. They are excluded from charts, totals and budgets, but listed here
    so nothing is silently disappeared: an amount that vanishes with no way to
    see it is indistinguishable from a bug.
    """
    from app.services.flat_categories import EXCLUDED_CATEGORY

    db = await get_db()
    try:
        scope_sql, _p, effective_scope, _n = await resolve_scope(db, scope)
        clauses = ["(category_path = ? OR entity_type = 'excluded')"]
        params = [EXCLUDED_CATEGORY]
        if start_date:
            clauses.append("posted_date >= ?")
            params.append(start_date)
        if end_date:
            clauses.append("posted_date <= ?")
            params.append(end_date)
        where = " AND ".join(clauses) + scope_sql.replace(" AND t.", " AND ")

        cursor = await db.execute(
            f"""SELECT id, posted_date, amount_cents, merchant, description, account_id,
                       category_path, entity_type, categorization_reason, notes
                FROM transactions WHERE {where}
                ORDER BY posted_date DESC LIMIT ?""",
            (*params, limit),
        )
        rows = [dict(r) for r in await cursor.fetchall()]

        cursor = await db.execute(
            f"""SELECT COUNT(*) n,
                       COALESCE(SUM(CASE WHEN amount_cents < 0 THEN -amount_cents ELSE 0 END), 0) out_cents,
                       COALESCE(SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END), 0) in_cents
                FROM transactions WHERE {where}""",
            params,
        )
        totals = await cursor.fetchone()

        return {
            "transactions": rows,
            "count": len(rows),
            "totals": {
                "transaction_count": totals[0],
                "excluded_outflow_cents": totals[1],
                "excluded_inflow_cents": totals[2],
                "net_cents": totals[2] - totals[1],
            },
            "scope": effective_scope,
        }
    finally:
        await db.close()


@router.post("/api/refunds/detect")
async def detect_refunds(scope: str = "plaid"):
    """Pair refunded purchases with the credits that reversed them."""
    from app.services import refund_pairs

    db = await get_db()
    try:
        return await refund_pairs.detect_and_store(db, scope=scope)
    finally:
        await db.close()


@router.post("/api/refunds/reconcile-merchant")
async def reconcile_merchant_refunds(
    pattern: str = Query(..., description="Merchant/description substring, e.g. 'exampleco'"),
    scope: str = "plaid",
    dry_run: bool = False,
):
    """Settle up with one vendor: pair its charges and credits by exact amount.

    For the case the ordinary detector cannot reach — a vendor that refunds a long
    run of charges in one batch, well outside the 180-day window. Exact amounts
    only and never partial, so nothing is guessed. Returns what remains unpaired,
    which is the amount genuinely still spent.

    Use ``dry_run=true`` first: pairing is what makes a charge stop counting.
    """
    from app.services import refund_pairs

    db = await get_db()
    try:
        return await refund_pairs.reconcile_merchant(
            db, pattern, scope=scope, dry_run=dry_run
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await db.close()


@router.get("/api/refunded")
async def list_refunded(start_date: Optional[str] = None, end_date: Optional[str] = None,
                        scope: str = "plaid", limit: int = 500):
    """Refunded purchases — the audit trail for refund attribution.

    Totals book a refund against the CHARGE's period (services/scope.py), so a
    past month's spending drops when a credit arrives later. Each pair here
    carries both dates and ``outstanding_cents``, which is exactly the amount the
    charge's month still counts — that makes the movement checkable instead of
    mysterious.
    """
    from app.services import refund_pairs

    db = await get_db()
    try:
        return await refund_pairs.list_pairs(db, start_date, end_date, scope, limit)
    finally:
        await db.close()
