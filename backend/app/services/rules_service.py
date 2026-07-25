import json
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Shortest rule pattern that may be persisted. ``apply_rules`` below matches a
# pattern as ``LIKE '%pattern%'`` against merchant OR description, so a short
# pattern is not a weak rule — it is a rule that fires on a large fraction of the
# ledger, silently, on every future import. Live incident: a rule with pattern
# ``us`` (from ``merchant_key("US MOBILE")`` before that was fixed) matched 1,208
# rows — "Costco Wholesale", anything containing "USA", "USB", "Super",
# "Cushions".
#
# ``merchant_key`` now refuses to emit such a key, but it is not the only source
# of patterns: chat-correct passes an LLM-supplied ``search`` / ``pattern`` string
# straight into ``add_rule``, and a model is every bit as capable of proposing
# ``"us"``. This guard is the second line of defence, at the point of write.
MIN_RULE_PATTERN_LEN = 4


def determine_entity_type(category_path: str, amount_cents: int = None) -> str:
    """Entity type for a category.

    Flat model: income vs expense comes from the amount's SIGN, not the category,
    so a category can never contradict the money. Transfers are the exception —
    internal movement is neither, and the category is the only thing that can say
    so. ``amount_cents`` is optional so legacy callers keep working; without it
    the legacy prefixes are honoured.
    """
    from app.services.flat_categories import is_excluded_category, is_transfer_category

    # The opt-out bucket must win over the sign: an excluded row is neither
    # income nor expense. Without this, setting a category to "Excluded" made the
    # row an ordinary expense and ADDED it to spending — the exact opposite of
    # what excluding means.
    if is_excluded_category(category_path):
        return "excluded"
    if is_transfer_category(category_path):
        return "transfer"
    if amount_cents is not None:
        return "income" if int(amount_cents) > 0 else "expense"
    if not category_path:
        return "expense"
    if category_path.startswith("Income"):
        return "income"
    if category_path.startswith("Transfer"):
        return "transfer"
    return "expense"


async def add_rule(db, merchant_pattern: str, category_path: str, description: str) -> int:
    """Persist an auto-apply rule, refusing patterns too short to be safe.

    Raises ``ValueError`` for a pattern shorter than ``MIN_RULE_PATTERN_LEN``
    (blank and whitespace-only included). Callers must treat rule creation as a
    best-effort side effect and catch this — the user's actual categorization has
    usually already succeeded by the time we get here, and it must not be turned
    into a 500 because the suggested pattern was junk.
    """
    pattern = (merchant_pattern or "").strip()
    if len(pattern) < MIN_RULE_PATTERN_LEN:
        raise ValueError(
            f"Refusing to create a rule with pattern {pattern!r}: patterns are "
            f"matched as a substring against merchant AND description, so fewer "
            f"than {MIN_RULE_PATTERN_LEN} characters matches a large share of the "
            f"ledger indiscriminately."
        )
    cursor = await db.execute(
        "INSERT INTO rules (pattern, category_path, description) VALUES (?, ?, ?)",
        (pattern.lower(), category_path, description)
    )
    await db.commit()
    return cursor.lastrowid


async def find_equivalent_rule(db, merchant_pattern: str, category_path: str) -> Optional[int]:
    """Id of an existing rule with the same (pattern, category_path), if any.

    Every path that records a user decision — chat-correct, apply-similar — also
    creates a rule so the next import inherits it. Without this lookup, telling
    the app the same thing twice leaves two identical rules behind, and the rules
    list becomes noise the user stops reading. ``add_rule`` lowercases the pattern
    on insert, so the comparison does too.
    """
    cursor = await db.execute(
        "SELECT id FROM rules WHERE LOWER(pattern)=? AND category_path=? ORDER BY id LIMIT 1",
        ((merchant_pattern or "").strip().lower(), category_path),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def add_rule_once(db, merchant_pattern: str, category_path: str,
                        description: str) -> tuple:
    """``add_rule`` that reuses an equivalent existing rule.

    Returns ``(rule_id, created)`` so callers can tell the user "created" vs
    "already knew that".
    """
    existing = await find_equivalent_rule(db, merchant_pattern, category_path)
    if existing is not None:
        return existing, False
    return await add_rule(db, merchant_pattern, category_path, description), True


async def list_rules(db) -> List[Dict[str, Any]]:
    # ``affected_count`` reflects current matches against the same predicate
    # ``apply_rules`` uses below: merchant OR description, case-insensitive.
    cursor = await db.execute(
        """SELECT r.*,
           (SELECT COUNT(*) FROM transactions t
            WHERE LOWER(t.merchant)    LIKE '%' || LOWER(r.pattern) || '%'
               OR LOWER(t.description) LIKE '%' || LOWER(r.pattern) || '%') as affected_count
        FROM rules r ORDER BY r.created_at DESC"""
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_rule(db, rule_id: int) -> Optional[Dict[str, Any]]:
    cursor = await db.execute(
        """SELECT r.*,
           (SELECT COUNT(*) FROM transactions t
            WHERE LOWER(t.merchant)    LIKE '%' || LOWER(r.pattern) || '%'
               OR LOWER(t.description) LIKE '%' || LOWER(r.pattern) || '%') as affected_count
        FROM rules r WHERE r.id=?""",
        (rule_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def apply_rules(db) -> int:
    """Apply every rule to uncategorized transactions.

    Pattern match is case-insensitive substring on EITHER the merchant or
    the description. This is what the chat-correct UI promises and what the
    paycheck-deduction rules require — the merchant on those rows is just the
    employer's name and the distinguishing string (``Federal Income
    Tax`` etc.) lives in the description.

    The sign-invariant is consulted before each write so a rule that would
    place a positive-amount row into an Expense bucket is skipped.
    """
    from app.services.sign_invariant import (
        coerce_sign_safe,
        log_violation_autofix,
        violates_sign_invariant,
    )

    cursor = await db.execute("SELECT * FROM rules")
    rules = await cursor.fetchall()
    if not rules:
        return 0

    total = 0
    for rule in rules:
        pattern = rule["pattern"].lower()
        cursor = await db.execute(
            """SELECT id, amount_cents, business_context FROM transactions
               WHERE (category_path IS NULL OR category_path = 'Uncategorized')
                 AND (LOWER(merchant)    LIKE ?
                   OR LOWER(description) LIKE ?)""",
            (f"%{pattern}%", f"%{pattern}%")
        )
        matches = await cursor.fetchall()
        for txn in matches:
            cat_path = rule["category_path"]
            entity_type = determine_entity_type(cat_path, txn["amount_cents"])
            amt = int(txn["amount_cents"] or 0)
            bc = txn["business_context"] or "personal"
            reason = f"Matched rule: {rule['description']}"
            if violates_sign_invariant(entity_type, amt, cat_path):
                new_cp, new_et, new_bc, note = coerce_sign_safe(
                    cat_path, entity_type, amt, bc
                )
                log_violation_autofix(
                    txn["id"],
                    {"category_path": cat_path, "entity_type": entity_type,
                     "business_context": bc, "amount_cents": amt},
                    {"category_path": new_cp, "entity_type": new_et,
                     "business_context": new_bc},
                    note or "apply_rules sign-invariant autofix",
                )
                if new_cp == "Uncategorized":
                    # Skip rather than destroy a coherent uncategorized row.
                    continue
                cat_path, entity_type, bc = new_cp, new_et, new_bc
                reason = note or reason
            await db.execute(
                """UPDATE transactions
                   SET category_path=?, entity_type=?, business_context=?,
                       applied_rule_id=?, confidence_score=?, categorization_reason=?
                   WHERE id=?""",
                (cat_path, entity_type, bc, rule["id"], 0.95,
                 reason, txn["id"])
            )
            total += 1

    if total:
        await db.commit()
        logger.info("Applied %d rule-based categorizations", total)
    return total
