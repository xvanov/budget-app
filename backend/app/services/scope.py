"""Data scope + expenditure arithmetic, shared by the reporting endpoints.

Owner directive (2026-07-24): the app focuses on EXPENDITURES from Chase
accounts connected through Plaid. The CSV-derived rows stay in the database as
stored history, but they are not the live source and must not silently mix into
the default numbers. Keeping the two in separate scopes is what removes the
between-account dedup problem instead of trying to solve it.

Scopes
------
``plaid`` : the LIVE data — rows Plaid delivered plus anything entered by hand
            (``source_platform IN ('plaid','manual')``). THE DEFAULT. Manual
            entries belong here: cash spending typed into "Add Transaction" is
            current, user-authored data, and excluding it made a manual
            transaction vanish the moment it was created.
``all``   : everything, including the pre-Plaid CSV history. Opt-in only.
``auto``  : alias for ``plaid``, kept so existing callers keep working.

There is deliberately NO fallback to ``all`` when Plaid has no rows yet. An
earlier version fell back so the app would not look empty before the first bank
was linked; that was wrong — it put the CSV history back on screen, which is
exactly what the owner asked twice to stop seeing. An empty dashboard with
"link a bank" is the correct answer to "no live data yet"; showing stale
CSV-derived numbers instead is not.

Expenditure arithmetic
----------------------
Spending is ``sum(expense) + sum(refund)``. A refund is a contra-expense: a
positive row carrying the expense category it reverses (see
``services/sign_invariant.py``), so returning a $64 item must REDUCE its
category by $64. A purely sign-based aggregation would instead book that $64 as
income and leave the category overstated — which is what these SQL fragments
exist to prevent.

Refund attribution across periods
---------------------------------
Netting per category only tells the truth when both halves of a return land
inside the same reporting window. A $46 charge in June refunded by a $46 credit
in July made June overstate spending by $46 AND July show $46 of phantom income,
even though nothing was really spent. On the live data 48 pairs straddle a month
boundary, distorting single months by as much as $430.

So a refund's effect belongs to the period of the CHARGE, not the credit. Every
row therefore reports an *effective* amount (``effective_amount_sql``):

  * the CREDIT half of a pair contributes 0 — its effect is applied at its charge
    instead, so the month the money came back shows no income;
  * the CHARGE half contributes its own amount plus the credits paired to it,
    i.e. what was still genuinely spent — 0 when fully refunded, the outstanding
    portion when partial, booked in the charge's own month;
  * every other row contributes its own amount, exactly as before.

Two consequences worth stating out loud:

1. **A past month's total CHANGES when a later refund arrives.** That is the
   point — June should stop claiming money that came back in July — but it means
   a closed month is not frozen. ``GET /api/refunded`` lists every pair with its
   ``outstanding_cents`` so the movement is inspectable rather than mysterious.
2. **A same-window pair is unaffected.** It already netted to zero (-46 + 46);
   under the effective amount both halves are 0, so the net is identical and
   nothing is subtracted twice. What does change for a same-window pair is the
   GROSS income/expense of the row-level series, which used to report $46 in and
   $46 out for a wash — that now agrees with what the per-category net has always
   said.

Money is conserved: a credit is zeroed only when its partner row is a charge,
and that charge sums exactly the credits pointing back at it, so each credit is
applied once and only once no matter how many point at one charge. The one place
conservation breaks is deliberate — if the charge falls outside the queried date
range, its refund does not re-enter the range through the credit. The scope filter
cannot orphan a credit that way, because pairing runs inside one scope (measured
on the live DB: 0 cross-scope pairs, 0 credits without a charge partner).
"""
import logging
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

VALID_SCOPES = ("auto", "plaid", "all")

# Rows that are internal movement, not spending. Split parents live here too:
# their children carry the real expense, so counting both double-counts.
# 'excluded' is the owner's opt-out bucket: a real transaction that must not
# count anywhere. See services/flat_categories.EXCLUDED_CATEGORY.
EXCLUDED_ENTITY_TYPES = ("transfer", "asset_conversion", "excluded")

# Flat model: a category's NET decides which side it is on, so a refund needs no
# special entity type. +$64 back on Groceries simply reduces the Groceries net.
#
#   net < 0  -> that category is spending of abs(net)
#   net > 0  -> that category is money in
#
# Kept as legacy aliases for callers that still sum row-by-row; the dashboard
# aggregates per category and then sorts by the sign of the net (see
# routers/transactions.py::get_dashboard).
EXPENSE_SUM = (
    "COALESCE(SUM(CASE "
    "  WHEN t.entity_type IN ('refund') THEN -ABS(t.amount_cents) "
    "  WHEN t.amount_cents < 0 THEN ABS(t.amount_cents) "
    "  ELSE 0 END), 0)"
)

INCOME_SUM = (
    "COALESCE(SUM(CASE "
    "  WHEN t.entity_type IN ('refund') THEN 0 "
    "  WHEN t.amount_cents > 0 THEN t.amount_cents "
    "  ELSE 0 END), 0)"
)

# Per-category net — the primitive the flat model actually reports from.
NET_SUM = "COALESCE(SUM(t.amount_cents), 0)"


def countable_rows_fragment(alias: str = "t") -> str:
    """The "this row counts" test: not internal movement, not opted out."""
    excluded = ", ".join(f"'{e}'" for e in EXCLUDED_ENTITY_TYPES)
    return f"({alias}.entity_type IS NULL OR {alias}.entity_type NOT IN ({excluded}))"


def effective_amount_sql(alias: str = "t") -> str:
    """Signed amount a row contributes to its OWN period, refunds attributed.

    See "Refund attribution across periods" in the module docstring for the why.
    Substituted into any query that totals over a date range; the aggregating
    helpers below wrap it. Requires the ``transactions`` table aliased as
    ``alias`` (the correlated subqueries reference ``alias.id``).

    The subqueries only run for rows in a refund pair — the CASE arms are
    evaluated lazily — so on live data this is ~222 lookups (111 pairs), not one
    per row, and they ride ``idx_tx_refund_match``. Without that index the same
    aggregation measured 297 ms instead of 3 ms on 10.4k rows.
    """
    a = alias
    return f"""CASE
        -- Credit half: contributes nothing here; it is applied at its charge.
        -- Guarded on the partner actually being a charge so a half-cleared pair
        -- cannot swallow a real inflow.
        WHEN {a}.refund_role = 'credit' AND EXISTS (
                 SELECT 1 FROM transactions rp
                  WHERE rp.id = {a}.refund_match_id AND rp.refund_role = 'charge')
            THEN 0
        -- Charge half: what is still genuinely spent. Summing the credits that
        -- point AT this charge (rather than following the charge's own
        -- refund_match_id, which names only one of them) is what keeps the
        -- arithmetic right, and single-counted, when several credits reverse one
        -- charge. A credit the owner has opted out of is skipped, so it neither
        -- counts itself nor reduces its charge.
        WHEN {a}.refund_role = 'charge' AND {a}.refund_match_id IS NOT NULL
            THEN {a}.amount_cents + COALESCE((
                 SELECT SUM(rc.amount_cents) FROM transactions rc
                  WHERE rc.refund_match_id = {a}.id
                    AND rc.refund_role = 'credit'
                    AND {countable_rows_fragment('rc')}), 0)
        ELSE {a}.amount_cents
    END"""


def effective_net_sum(alias: str = "t") -> str:
    return f"COALESCE(SUM({effective_amount_sql(alias)}), 0)"


def effective_income_sum(alias: str = "t") -> str:
    expr = effective_amount_sql(alias)
    return f"COALESCE(SUM(CASE WHEN ({expr}) > 0 THEN ({expr}) ELSE 0 END), 0)"


def effective_expense_sum(alias: str = "t") -> str:
    expr = effective_amount_sql(alias)
    return f"COALESCE(SUM(CASE WHEN ({expr}) < 0 THEN -({expr}) ELSE 0 END), 0)"


# Ready-made for the reporting endpoints, all of which alias the table as ``t``.
# income - expense == net holds per group by construction: the three read the
# same effective amount and only differ in which sign they keep.
EFFECTIVE_AMOUNT = effective_amount_sql("t")
EFFECTIVE_NET_SUM = effective_net_sum("t")
EFFECTIVE_INCOME_SUM = effective_income_sum("t")
EFFECTIVE_EXPENSE_SUM = effective_expense_sum("t")


def effective_amount_cents(amount_cents: int, refund_role: Optional[str] = None,
                           paired_credit_cents: int = 0) -> int:
    """Python twin of ``effective_amount_sql``, for callers without SQL.

    ``paired_credit_cents`` is the total of the credits paired to this row when it
    is a charge (positive cents); ignored otherwise.
    """
    if refund_role == "credit":
        return 0
    if refund_role == "charge":
        return int(amount_cents) + int(paired_credit_cents)
    return int(amount_cents)


async def has_plaid_data(db) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM transactions WHERE source_platform='plaid' LIMIT 1"
    )
    return (await cursor.fetchone()) is not None


async def resolve_scope(db, scope: Optional[str] = "auto") -> Tuple[str, List, str, str]:
    """Return (sql_fragment, params, effective_scope, explanation).

    The fragment is written to be appended to a WHERE clause that already has a
    condition, i.e. it starts with " AND ".
    """
    requested = (scope or "plaid").strip().lower()
    if requested not in VALID_SCOPES:
        requested = "plaid"

    # 'auto' is an alias for 'plaid'. It does NOT fall back to 'all' — see the
    # module docstring. Only an explicit scope=all reaches the CSV history.
    effective = "plaid" if requested in ("auto", "plaid") else "all"

    if effective == "plaid":
        note = "Live data only — Plaid plus manual entries (pre-Plaid CSV history excluded)."
        if not await has_plaid_data(db):
            note = (
                "Live Plaid data only — nothing synced yet. Link a bank on the "
                "Accounts & Sync page, or pass scope=all to inspect the stored "
                "CSV history."
            )
        return " AND t.source_platform IN ('plaid','manual')", [], effective, note

    return "", [], effective, "All sources, including the pre-Plaid CSV history (explicitly requested)."


def expenditure_only_fragment() -> str:
    """Restrict to rows that represent spending (expenses and their refunds)."""
    return " AND (t.entity_type IS NULL OR t.entity_type IN ('expense', 'refund'))"
