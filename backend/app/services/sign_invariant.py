"""Sign-direction invariant for transaction categorization.

Rule:
  entity_type='expense'  => amount_cents must be <= 0 (outflow)
  entity_type='income'   => amount_cents must be >= 0 (inflow)
  entity_type in {'transfer', 'asset_conversion', 'liability_payment', 'refund'}
                         => either sign allowed.

'refund' is deliberately allowed to carry an `Expense >` category_path with a
positive amount — see the comment on _FLEXIBLE_ENTITY_TYPES. It is the one
entity type where a positive amount on an expense path is correct rather than
corrupt, because it offsets that category instead of adding to income.

The historical corruption that motivated this module: bulk SQL UPDATEs
re-categorized rows by description-substring only (no amount sign filter),
so incoming Zelle rent payments (positive amounts) ended up tagged
`Expense > Rental > Rent`. This invariant is enforced wherever a category
is assigned at the application layer to prevent recurrence.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Entity types for which either sign is acceptable.
#
# 'refund' is a CONTRA-EXPENSE: a positive (inflow) row that belongs to the same
# expense category as the purchase it reverses. Returning a $64 item must reduce
# `Expense > Personal > Shopping` by $64 — booking it as `Income > Refunds`
# instead (the pre-existing coercion) leaves the spend category overstated and
# inflates income, which misreports what was actually spent on what. Expenditure
# totals are therefore defined as sum(expense) + sum(refund).
_FLEXIBLE_ENTITY_TYPES = frozenset({
    "transfer",
    "asset_conversion",
    "liability_payment",
    "refund",
    # The owner's opt-out bucket: neither income nor expense, so either sign.
    "excluded",
})

# Entity types that are valid for category_path beginning with "Transfer >".
_TRANSFER_PATH_ENTITY_TYPES = frozenset({
    "transfer",
    "asset_conversion",
    "liability_payment",
})

# Entity types valid for a category_path beginning with "Expense >". A refund
# keeps the expense path on purpose — that is what makes it net out against the
# category it reverses.
_EXPENSE_PATH_ENTITY_TYPES = frozenset({
    "expense",
    "refund",
})


def violates_sign_invariant(
    entity_type: Optional[str],
    amount_cents: int,
    category_path: Optional[str] = None,
) -> bool:
    """Return True when the row violates the sign/path invariant.

    Two independent failure modes:

      1. Sign vs. entity_type mismatch:
         * ``entity_type='expense'`` must have ``amount_cents <= 0``.
         * ``entity_type='income'``  must have ``amount_cents >= 0``.
         * Transfer / asset_conversion / liability_payment allow either sign.

      2. Category-path vs. entity_type mismatch (only enforced when
         ``category_path`` is supplied):
         * ``category_path`` starting ``Income >``   ⇒ entity_type must be ``income``.
         * ``category_path`` starting ``Expense >``  ⇒ entity_type must be ``expense``.
         * ``category_path`` starting ``Transfer >`` ⇒ entity_type must be
           one of ``transfer`` / ``asset_conversion`` / ``liability_payment``.

    Either failure mode is sufficient to mark the row incoherent.
    """
    et = (entity_type or "").strip().lower()

    # Flat categories carry no flow in the name, so there is nothing for the sign
    # to contradict: entity_type is DERIVED from the amount. Only the legacy
    # hierarchical paths ("Expense > ...") can disagree with a sign.
    if category_path and " > " not in category_path:
        from app.services.flat_categories import is_transfer_category

        from app.services.flat_categories import is_excluded_category

        if is_transfer_category(category_path) or is_excluded_category(category_path):
            return False
        if et in ("refund", "excluded"):
            return False
        if et == "expense" and amount_cents > 0:
            return True
        if et == "income" and amount_cents < 0:
            return True
        return False

    # --- (2) path vs. entity_type --------------------------------------------
    if category_path:
        cp = category_path.strip()
        if cp.startswith("Income >") and et != "income":
            return True
        if cp.startswith("Expense >") and et not in _EXPENSE_PATH_ENTITY_TYPES:
            return True
        if cp.startswith("Transfer >") and et not in _TRANSFER_PATH_ENTITY_TYPES:
            return True

    # --- (1) sign vs. entity_type --------------------------------------------
    if not entity_type:
        return False
    if et in _FLEXIBLE_ENTITY_TYPES:
        return False
    if et == "expense" and amount_cents > 0:
        return True
    if et == "income" and amount_cents < 0:
        return True
    return False


def coerce_sign_safe(
    category_path: Optional[str],
    entity_type: Optional[str],
    amount_cents: int,
    business_context: Optional[str] = None,
) -> Tuple[str, str, str, Optional[str]]:
    """Return a sign-safe (category_path, entity_type, business_context, note).

    If the original (entity_type, amount_cents) pair is consistent, the inputs
    are returned unchanged with ``note=None``. Otherwise a sign-appropriate
    fallback is returned along with a human-readable note that callers should
    record in ``categorization_reason``.

    Fallback policy:
      * positive amount tagged as 'expense'  -> `Uncategorized`/`income` if the
        category clearly belongs to rentals/refunds, else `Uncategorized`.
      * negative amount tagged as 'income'   -> `Uncategorized`/`expense`.
    """
    cp = category_path or "Uncategorized"
    bc = business_context or "personal"
    if not violates_sign_invariant(entity_type, amount_cents):
        return cp, (entity_type or "expense"), bc, None

    et = (entity_type or "").strip().lower()
    if et == "expense" and amount_cents > 0:
        # The row is an inflow but was tagged as an expense.
        #
        # Rentals stay an income fallback: the corruption this module exists to
        # prevent was inbound Zelle RENT (a real inflow) being tagged
        # `Expense > Rental > Rent`, and that must never become a rent "refund".
        if cp and "rental" in cp.lower():
            note = (
                f"sign-invariant fallback: amount=+{amount_cents/100:.2f} cannot be"
                f" '{cp}' (expense); coerced to 'Income > Rentals'"
            )
            return "Income > Rentals", "income", "rental", note

        # Otherwise an inflow on an EXPENSE category is a merchant refund — the
        # contra-expense case. Keep the category so the money comes back off the
        # bucket it was spent from; only the entity_type changes. Coercing these
        # to 'Uncategorized' (the previous behaviour) discarded a correct
        # category and left real refunds stranded: every one of the 35 rows this
        # hit on live data was a genuine merchant credit (Amazon, Apple, SHEIN
        # reversals, NordVPN), not an income row.
        if cp and cp.startswith("Expense >"):
            note = (
                f"inflow of +{amount_cents/100:.2f} on an expense category — booked as a"
                f" refund against '{cp}' rather than income"
            )
            return cp, "refund", bc, note

        if cp and "refund" in cp.lower():
            new_cp = "Income > Refunds"
        else:
            new_cp = "Uncategorized"
        note = (
            f"sign-invariant fallback: amount=+{amount_cents/100:.2f} cannot be"
            f" '{cp}' (expense); coerced to '{new_cp}'"
        )
        return new_cp, ("income" if new_cp.startswith("Income") else "expense"), bc, note

    if et == "income" and amount_cents < 0:
        new_cp = "Uncategorized"
        note = (
            f"sign-invariant fallback: amount={amount_cents/100:.2f} cannot be"
            f" '{cp}' (income); coerced to 'Uncategorized'"
        )
        return new_cp, "expense", bc, note

    # Defensive: shouldn't reach here because violates_sign_invariant already
    # only flags the two cases above.
    return cp, (entity_type or "expense"), bc, None


def log_violation_autofix(txn_id: Optional[int], original: dict, fixed: dict, reason: str) -> None:
    """Emit a structured warning when the invariant auto-corrects a write."""
    logger.warning(
        "sign-invariant autofix txn_id=%s original=%s fixed=%s reason=%s",
        txn_id, original, fixed, reason,
    )
