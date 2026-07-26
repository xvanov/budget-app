"""The mortgage, measured from the ledger instead of projected from a PDF.

Why this module exists
---------------------
The Mortgage tab used to be driven by the ``amortization`` table, which holds a
58-row FORWARD PROJECTION the lender issued in 2026 (first row 2026-06,
ending balance $153,538). A projection is not history: read as history it made a
loan that was opened in February 2025 and paid off in July 2026 look like a loan
that starts in mid-2026 and runs to 2031. On top of that, ``mortgage_splitter``
manufactured "principal"/"interest" child rows using a CONSTANT $843.01 of
interest lifted from the projection's first row — a fabricated number presented
as a fact on every payment, including a $100,000 curtailment.

What this module does instead
-----------------------------
1. **Measure.** Find the rows that are actually payments to the loan servicer,
   in ONE scope so the CSV row and its Plaid twin are never both counted, and
   total them. That total is exact: ``total_paid_to_lender``.
2. **Derive the split honestly.** A single $3,722.53 ACH cannot be decomposed by
   looking at it. It can be decomposed by running a real amortization over the
   actual payment stream. The terms come from the owner (``mortgage_facts``):
   $280,000 for 120 months at 6.49%, first payment 2025-04-01. Those three facts
   fix the scheduled P&I at 280000·i/(1-(1+i)^-120) = **$3,177.92**, so escrow on
   any monthly payment is ``amount - P&I``: $544.61 while the autopay was
   $3,722.53, $896.88 for the one-off $4,074.80 in 2026-03, $748.05 after the
   escrow re-analysis settled the autopay at $3,925.97.
3. **Cross-check the terms instead of trusting them.** Independent checks run on
   every request and are returned in ``checks``:
     * the lender's own schedule gives ``interest / opening_balance`` = 0.0054083
       per month on all 58 rows → 6.4900%/yr, agreeing with the stated 6.49%;
     * the schedule's ``principal + interest`` is $3,177.92 on every row, equal
       to the payment the stated terms imply, to the cent;
     * the origination amount is itself re-derivable: with the rate and P&I
       fixed, the final balance is an AFFINE function of the amount borrowed, so
       requiring the loan to close at $0 on the payoff payment solves it. Over
       the owner's real payment stream that solve returns $279,999.99 — the
       stated $280,000, from the ledger alone, without being told.
   The simulation also passes exactly through the lender projection's own opening
   balance of $155,873.06 immediately after the April 2026 $100,000 curtailment.
   Four agreements on numbers that had no reason to agree unless the model is
   right.
4. **Label everything.** Every figure carries a ``basis``:
     ``measured`` — a sum over real transactions;
     ``derived``  — computed from measured rows plus the loan terms;
     ``inferred`` — solved from the data, with the working recorded;
     ``stated``   — supplied by the owner (``mortgage_facts``), which always wins;
     ``unknown``  — not in the data. Reported as null. Never invented.

The escrow refund the owner remembers (~$1,600) is NOT in the ledger and is
deliberately NOT modelled — it comes back ``unknown`` with an affordance to
record it. If the servicer's credit later shows up in the data it is picked up as
``measured``.
"""
import logging
import statistics
from typing import Any, Dict, List, Optional, Tuple

from app.services.scope import resolve_scope

logger = logging.getLogger(__name__)

# ── Loan identity ────────────────────────────────────────────────────────────
# These two strings identify ONE LOAN ACCOUNT in the ledger. This is not
# keyword categorization (which the house rules ban and which the LLM owns at
# ingest): it is the same kind of identity match ``amazon_matcher`` does when it
# ties a shipment to an order. Nothing here decides a category.
#
#   LENDER_ACH_ID  the servicer's ACH company id, stamped on every autopay debit
#                  ("JPMORGAN CHASE  CHASE ACH  PPD ID: 1000008113")
#   LENDER_PAYEE   the online-bill-pay payee: servicer + loan account last four
#                  ("Online Payment 29927167097 To HMG 5476 07/08")
#
# The tokens deliberately do NOT match the fabricated splitter children
# ("Principal split from HMG Mortgage payment"), so they cannot be double
# counted while those rows still exist in an un-migrated database.
LENDER_ACH_ID = "1000008113"
LENDER_PAYEE = "HMG 5476"

# A payment bigger than this multiple of the scheduled P&I is a curtailment
# (extra principal), not a monthly payment. The real stream makes the gap
# obvious: monthly payments are $3,722–$4,075 against a $3,177.92 P&I, while the
# smallest extra is $5,000 and the largest $100,000.
EXTRA_PAYMENT_MULTIPLE = 1.5

_MATCH_SQL = (
    "(t.description LIKE ? OR UPPER(t.description) LIKE ? OR UPPER(t.merchant) LIKE ?)"
)
_MATCH_PARAMS = [f"%{LENDER_ACH_ID}%", f"%{LENDER_PAYEE}%", f"%{LENDER_PAYEE}%"]


# ── User-stated facts ────────────────────────────────────────────────────────
# The loan terms are not in any bank export — they are in the closing documents.
# So they live in a table the owner can edit rather than in a constant in a
# formula, and every figure computed from them is labelled ``stated`` (or
# ``derived`` when it is arithmetic on top of them).
FACT_FIELDS = (
    "original_principal_cents",
    "annual_rate_bps",
    "term_months",
    "origination_date",
    "first_payment_date",
    "scheduled_payoff_date",
    "payoff_date",
    "purchase_price_cents",
    "down_payment_cents",
    "down_payment_date",
    "down_payment_confidence",
    "escrow_refund_cents",
    "escrow_refund_date",
    "note",
)

# What the owner said on 2026-07-25, seeded ONCE into an empty facts row so the
# tab is right out of the box. Not hardcoded into the arithmetic: it is data, the
# owner can change any of it through PUT /api/mortgage/facts, and if the row
# already exists this seed does nothing.
#
# ``down_payment_confidence='believed'`` is carried deliberately. The owner's
# words were "i do believe this was the down payment. the home cost 400k, and
# there were some closing costs" — and $400,000 purchase - $280,000 borrowed =
# $120,000, against a $123,212.42 wire, i.e. $120,000 down plus $3,212.42 of
# closing costs. That reconciles, but it is corroboration, not a receipt, so the
# UI says "believed" instead of asserting it.
OWNER_STATED_FACTS = {
    "original_principal_cents": 28_000_000,
    "annual_rate_bps": 649,
    "term_months": 120,
    "first_payment_date": "2025-04-01",
    "scheduled_payoff_date": "2035-03-01",
    "purchase_price_cents": 40_000_000,
    "down_payment_cents": 12_000_000,
    "down_payment_confidence": "believed",
    "note": "Terms as stated by the owner on 2026-07-25 (10-year loan, paid off early).",
}


async def ensure_facts_table(db) -> None:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS mortgage_facts (
               id INTEGER PRIMARY KEY CHECK (id = 1),
               original_principal_cents INTEGER,
               annual_rate_bps INTEGER,
               term_months INTEGER,
               origination_date TEXT,
               first_payment_date TEXT,
               scheduled_payoff_date TEXT,
               payoff_date TEXT,
               purchase_price_cents INTEGER,
               down_payment_cents INTEGER,
               down_payment_date TEXT,
               down_payment_confidence TEXT,
               escrow_refund_cents INTEGER,
               escrow_refund_date TEXT,
               note TEXT,
               updated_at TEXT DEFAULT (datetime('now'))
           )"""
    )
    # Columns added after the table first shipped.
    for column, ddl in (
        ("scheduled_payoff_date", "ALTER TABLE mortgage_facts ADD COLUMN scheduled_payoff_date TEXT"),
        ("purchase_price_cents", "ALTER TABLE mortgage_facts ADD COLUMN purchase_price_cents INTEGER"),
        ("down_payment_cents", "ALTER TABLE mortgage_facts ADD COLUMN down_payment_cents INTEGER"),
        ("down_payment_date", "ALTER TABLE mortgage_facts ADD COLUMN down_payment_date TEXT"),
        ("down_payment_confidence", "ALTER TABLE mortgage_facts ADD COLUMN down_payment_confidence TEXT"),
    ):
        try:
            await db.execute(ddl)
        except Exception:
            pass  # already present
    await db.commit()


async def seed_owner_facts(db) -> bool:
    """Write ``OWNER_STATED_FACTS`` if the owner has no facts row yet.

    Returns True when it seeded. Never overwrites: once the row exists, the
    owner's edits are the truth and this is a no-op forever.
    """
    await ensure_facts_table(db)
    cursor = await db.execute("SELECT 1 FROM mortgage_facts WHERE id = 1")
    if await cursor.fetchone():
        return False
    columns = ", ".join(OWNER_STATED_FACTS)
    holders = ", ".join("?" * len(OWNER_STATED_FACTS))
    await db.execute(
        f"INSERT INTO mortgage_facts (id, {columns}) VALUES (1, {holders})",
        list(OWNER_STATED_FACTS.values()),
    )
    await db.commit()
    logger.info("Seeded owner-stated mortgage terms")
    return True


async def load_facts(db) -> Dict[str, Any]:
    """Whatever the owner has told us about the loan. Empty on a fresh install."""
    await ensure_facts_table(db)
    cursor = await db.execute("SELECT * FROM mortgage_facts WHERE id = 1")
    row = await cursor.fetchone()
    if not row:
        return {k: None for k in FACT_FIELDS}
    return {k: row[k] for k in FACT_FIELDS}


async def save_facts(db, values: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert the owner's stated facts. A key present with None CLEARS it."""
    await ensure_facts_table(db)
    fields = [k for k in FACT_FIELDS if k in values]
    if not fields:
        return await load_facts(db)
    await db.execute("INSERT OR IGNORE INTO mortgage_facts (id) VALUES (1)")
    assignments = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"UPDATE mortgage_facts SET {assignments}, updated_at = datetime('now') WHERE id = 1",
        [values[k] for k in fields],
    )
    await db.commit()
    return await load_facts(db)


# ── Amortization arithmetic ──────────────────────────────────────────────────
def _figure(cents: Optional[int], basis: str, note: str = "") -> Dict[str, Any]:
    return {"cents": cents, "basis": basis, "note": note}


def schedule_terms(schedule: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[int]]:
    """(monthly_rate, scheduled_P&I_cents) read off the lender's own schedule.

    Both are over-determined by 58 rows, so the median is used and the spread is
    what tells us the schedule is internally consistent: every row of the owner's
    schedule gives a monthly rate between 0.0054080 and 0.0054183, and every row
    gives the same $3,177.92 of P&I.
    """
    rates = []
    pi_values = []
    for row in schedule:
        principal = row.get("principal_cents") or 0
        interest = row.get("interest_cents") or 0
        ending = row.get("ending_balance_cents")
        if ending is None:
            continue
        opening = ending + principal
        if opening > 0 and interest > 0:
            rates.append(interest / opening)
        if principal + interest > 0:
            pi_values.append(principal + interest)
    if not rates or not pi_values:
        return None, None
    # The final row of a schedule is a short "pay the remainder" payment, so the
    # mode (not the mean) is the contractual P&I.
    pi = statistics.mode(pi_values)
    return statistics.median(rates), pi


def _simulate(principal_cents: float, rate: float, pi_cents: int,
              payments: List[Dict[str, Any]], *, exact: bool) -> Dict[str, Any]:
    """Run the real payment stream against a candidate origination balance.

    ``exact=False`` keeps the balance as a float and never clamps, which makes
    the final balance an AFFINE function of ``principal_cents`` — that is what
    lets the origination amount be solved in closed form. ``exact=True`` rounds
    interest to the cent, clamps at zero and produces the reportable rows.
    """
    balance = float(principal_cents)
    rows: List[Dict[str, Any]] = []
    totals = {"interest": 0, "principal": 0, "escrow": 0, "unexplained": 0}

    for pay in payments:
        amount = pay["amount_cents"]
        if pay["kind"] == "scheduled":
            interest = balance * rate
            if exact:
                interest = float(round(interest))
                interest = min(interest, float(pi_cents))
            principal = pi_cents - interest
            escrow = amount - pi_cents
            if exact:
                # A final short payment can be smaller than the contractual P&I.
                if escrow < 0:
                    escrow = 0.0
                    principal = max(0.0, amount - interest)
                if principal > balance:
                    totals["unexplained"] += int(round(principal - balance))
                    principal = balance
        else:
            interest = 0.0
            escrow = 0.0
            principal = float(amount)
            if exact and principal > balance:
                # Curtailment bigger than the debt: the excess is not principal.
                # On a payoff quote it is the per-diem interest and any recording
                # fee, which the ledger cannot separate — so it is reported as
                # unexplained rather than folded into principal.
                totals["unexplained"] += int(round(principal - balance))
                principal = balance

        balance -= principal
        if exact and abs(balance) < 1:
            # Sub-CENT float noise only (the unit here is cents). Anything a
            # reader would notice survives and is reported as the residual; the
            # balance is never nudged to zero to make the story tidy.
            balance = 0.0
        totals["interest"] += interest
        totals["principal"] += principal
        totals["escrow"] += escrow
        rows.append({
            **{k: pay[k] for k in ("id", "posted_date", "amount_cents", "kind",
                                   "description", "merchant", "account_id",
                                   "source_platform")},
            "interest_cents": int(round(interest)),
            "principal_cents": int(round(principal)),
            "escrow_cents": int(round(escrow)),
            "balance_after_cents": int(round(balance)),
        })

    return {
        "final_balance": balance,
        "rows": rows,
        "interest_cents": int(round(totals["interest"])),
        "principal_cents": int(round(totals["principal"])),
        "escrow_cents": int(round(totals["escrow"])),
        "unexplained_cents": int(totals["unexplained"]),
    }


def solve_origination_cents(rate: float, pi_cents: int,
                            payments: List[Dict[str, Any]]) -> Optional[float]:
    """The origination balance that makes this payment stream end at exactly $0.

    Without clamping, ``final_balance(L) = a*L + b`` — each scheduled payment
    multiplies the balance by (1+rate) and subtracts the P&I, each curtailment
    subtracts its amount. Two probes therefore give the exact root; no search,
    no tolerance.
    """
    if not payments or not pi_cents or not rate:
        return None
    f0 = _simulate(0.0, rate, pi_cents, payments, exact=False)["final_balance"]
    f1 = _simulate(1_000_000_00.0, rate, pi_cents, payments, exact=False)["final_balance"]
    slope = (f1 - f0) / 1_000_000_00.0
    if slope <= 0:
        return None
    root = -f0 / slope
    return root if root > 0 else None


def _snap(cents: float) -> Tuple[int, str]:
    """Round a solved origination amount to something a lender would have written.

    Loans are written in round amounts. If the solve lands within $100 of a
    round $1,000 it is reported as that round figure (and the leftover shows up
    as the reconciliation residual, which on the owner's data is one cent);
    otherwise it is reported to the dollar and left un-snapped, which is the
    signal that the terms behind the solve are shakier than they look.
    """
    thousand = round(cents / 100_000.0) * 100_000
    if abs(cents - thousand) <= 10_000:
        return int(thousand), "snapped to the nearest $1,000"
    return int(round(cents / 100.0) * 100), "rounded to the nearest dollar"


def payment_from_terms(principal_cents: int, rate: float, term_months: int) -> Optional[int]:
    """The contractual P&I the stated terms imply, in whole cents.

    280000 at 6.49%/12 over 120 months = $3,177.9189, which a lender writes as
    $3,177.92 — and $3,177.92 is exactly what every row of the owner's schedule
    carries. Rounding to the cent (not truncating) is what makes the two agree.
    """
    if not (principal_cents and rate and term_months):
        return None
    factor = 1 - (1 + rate) ** -term_months
    if factor <= 0:
        return None
    return int(round(principal_cents * rate / factor))


def derive_term_months(principal_cents: int, rate: float, pi_cents: int) -> Optional[int]:
    """Contractual term implied by (loan, rate, payment). 280000 @ 6.49% -> 120."""
    import math

    if not (principal_cents and rate and pi_cents):
        return None
    ratio = 1 - (principal_cents * rate) / pi_cents
    if ratio <= 0:
        return None
    n = -math.log(ratio) / math.log(1 + rate)
    return int(round(n)) if abs(n - round(n)) < 0.05 else None


def _add_months(iso_date: str, months: int) -> str:
    y, m, d = (int(x) for x in iso_date.split("-"))
    total = (y * 12 + (m - 1)) + months
    return f"{total // 12:04d}-{total % 12 + 1:02d}-{d:02d}"


def plan_schedule(principal_cents: int, rate: float, pi_cents: int,
                  first_payment_date: Optional[str] = None,
                  max_months: int = 600) -> List[Dict[str, Any]]:
    """The loan as originally written: no curtailments, payment every month.

    This is the comparison line on the chart — what the balance would have done
    if the owner had simply kept paying $3,177.92 of P&I for 120 months. The gap
    between it and the measured curve IS the effect of the extra principal.
    """
    rows = []
    balance = float(principal_cents)
    month = 0
    while balance > 0 and month < max_months:
        interest = float(round(balance * rate))
        principal = min(pi_cents - interest, balance)
        balance -= principal
        rows.append({
            "month_index": month + 1,
            "date": _add_months(first_payment_date, month) if first_payment_date else None,
            "interest_cents": int(interest),
            "principal_cents": int(round(principal)),
            "ending_balance_cents": int(round(balance)),
        })
        month += 1
    return rows


# ── The measurement ──────────────────────────────────────────────────────────
async def _fetch_lender_rows(db, scope_sql: str) -> List[Dict[str, Any]]:
    cursor = await db.execute(
        f"""SELECT t.id, t.posted_date, t.amount_cents, t.description, t.merchant,
                   t.account_id, t.source_platform
              FROM transactions t
             WHERE {_MATCH_SQL} {scope_sql}
             ORDER BY t.posted_date, t.id""",
        list(_MATCH_PARAMS),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def _origination_context(db, before_date: Optional[str],
                               down_payment_cents: Optional[int]) -> List[Dict[str, Any]]:
    """Closing-table money: related to the loan, NOT part of it.

    Two narrow, documented lookups rather than a guess:
      * an outgoing wire of $25k or more in the 90 days before the first payment
        (the owner's is a $123,212.42 wire to a title company in Portsmouth on
        2025-02-10 — cash to close, which never touches the loan balance);
      * a charge from the lender's origination arm ("CHASE HOME LEND"), which is
        an application/appraisal deposit, not a payment against principal.

    Deliberately UNSCOPED. Closing happened in February 2025, before Plaid was
    connected, so the live scope structurally cannot contain it — scoping this
    lookup would hide the down payment from the page that most needs it. Safe
    because these rows are displayed only and never added to a loan total; the
    CSV/Plaid twin risk that scoping exists to prevent does not apply. Twins are
    collapsed on (date, amount) anyway.
    """
    if not before_date:
        return []
    from datetime import date, timedelta

    try:
        y, m, d = (int(x) for x in before_date.split("-"))
        window_start = (date(y, m, d) - timedelta(days=90)).isoformat()
    except Exception:
        return []

    found: Dict[Tuple[str, int], Dict[str, Any]] = {}

    async def collect(sql: str, label: str) -> None:
        cursor = await db.execute(sql, [window_start, before_date])
        for row in await cursor.fetchall():
            item = dict(row)
            key = (item["posted_date"], item["amount_cents"])
            if key in found:
                continue
            item["label"] = label
            item["confidence"] = "believed"
            item["counts_toward_loan"] = False
            found[key] = item

    await collect(
        """SELECT t.id, t.posted_date, t.amount_cents, t.description, t.account_id,
                  t.source_platform
             FROM transactions t
            WHERE t.amount_cents <= -2500000
              AND UPPER(t.description) LIKE '%WIRE TRANSFER%'
              AND t.posted_date BETWEEN ? AND ?
            ORDER BY t.source_platform = 'plaid' DESC, t.posted_date""",
        "Wire out before the first payment — believed to be the down payment / cash to close",
    )
    await collect(
        """SELECT t.id, t.posted_date, t.amount_cents, t.description, t.account_id,
                  t.source_platform
             FROM transactions t
            WHERE UPPER(t.description) LIKE '%HOME LEND%'
              AND t.posted_date BETWEEN ? AND ?
            ORDER BY t.source_platform = 'plaid' DESC, t.posted_date""",
        "Charge from the lender's origination arm — a loan fee, not principal",
    )

    items = sorted(found.values(), key=lambda r: r["posted_date"])
    # If the owner stated a down payment, say what the difference would be
    # instead of leaving the reader to subtract two numbers on screen.
    if down_payment_cents:
        for item in items:
            if abs(item["amount_cents"]) >= down_payment_cents:
                item["implied_closing_costs_cents"] = (
                    abs(item["amount_cents"]) - down_payment_cents)
    return items


async def _scope_coverage_warning(db, scope_sql: str, scoped_dates: List[str]) -> Optional[str]:
    """Guard against a scope that hides part of the payment stream.

    The derived split is only as complete as the rows the scope can see. If the
    CSV history contains a payment date the live scope does not, the solve would
    be anchored on an incomplete stream — so say so instead of quietly being
    wrong.
    """
    if not scope_sql:
        return None
    cursor = await db.execute(
        f"""SELECT DISTINCT t.posted_date FROM transactions t
             WHERE {_MATCH_SQL} AND t.amount_cents < 0""",
        list(_MATCH_PARAMS),
    )
    all_dates = {r[0] for r in await cursor.fetchall()}
    missing = sorted(all_dates - set(scoped_dates))
    if not missing:
        return None
    return (
        f"{len(missing)} payment date(s) to the servicer exist only outside this scope "
        f"({', '.join(missing[:6])}{'…' if len(missing) > 6 else ''}). The split below is "
        f"computed from the payments this scope can see, so it is incomplete."
    )


async def analyze(db, scope: Optional[str] = "plaid") -> Dict[str, Any]:
    """Everything the Mortgage tab shows, with a basis on every number."""
    scope_sql, _params, effective_scope, scope_note = await resolve_scope(db, scope)
    facts = await load_facts(db)

    cursor = await db.execute("SELECT * FROM amortization ORDER BY month")
    schedule = [dict(r) for r in await cursor.fetchall()]

    rows = await _fetch_lender_rows(db, scope_sql)
    debits = [r for r in rows if r["amount_cents"] < 0]
    credits = [r for r in rows if r["amount_cents"] > 0]

    warnings: List[str] = []
    notes: List[str] = [scope_note]

    # ── terms: what the owner stated, checked against the lender's schedule ──
    schedule_rate, schedule_pi = schedule_terms(schedule)
    stated_principal = facts.get("original_principal_cents")
    stated_term = facts.get("term_months")

    rate = schedule_rate
    rate_basis = "derived" if schedule_rate else "unknown"
    if facts.get("annual_rate_bps"):
        rate = facts["annual_rate_bps"] / 10_000.0 / 12.0
        rate_basis = "stated"

    # P&I comes from the stated terms when they are complete — that is the
    # contract. The lender's schedule is then a check on it, not the source.
    terms_pi = payment_from_terms(stated_principal, rate, stated_term) if rate else None
    pi_cents = terms_pi or schedule_pi
    pi_basis = "derived" if terms_pi else ("derived" if schedule_pi else "unknown")
    pi_source = ("the stated terms (loan x rate over the term)" if terms_pi
                 else "the lender's schedule (principal + interest per row)" if schedule_pi
                 else None)

    # Classify each payment. A monthly payment is P&I plus escrow; anything much
    # larger in a single hit is extra principal, which is why a 10-year loan
    # taken out in 2025 closed in 2026 — eight and a half years early.
    payments: List[Dict[str, Any]] = []
    for row in debits:
        amount = -row["amount_cents"]
        is_ach = LENDER_ACH_ID in (row["description"] or "")
        scheduled = bool(pi_cents) and (
            is_ach or amount <= round(pi_cents * EXTRA_PAYMENT_MULTIPLE)
        )
        payments.append({**row, "amount_cents": amount,
                         "kind": "scheduled" if scheduled else "extra"})

    total_paid = sum(p["amount_cents"] for p in payments)
    scheduled_payments = [p for p in payments if p["kind"] == "scheduled"]
    extra_payments = [p for p in payments if p["kind"] == "extra"]

    coverage = await _scope_coverage_warning(db, scope_sql, [p["posted_date"] for p in payments])
    if coverage:
        warnings.append(coverage)

    # ── what was borrowed: stated by the owner, and re-solved as a check ─────
    #
    # The solve runs whether or not the amount was stated. When it was, the solve
    # is an independent verification (it reads $279,999.99 against a stated
    # $280,000); when it was not, it is the only way to know, and the figure is
    # labelled ``inferred`` instead of ``stated``.
    solved_cents: Optional[float] = None
    if rate and pi_cents and payments:
        solved_cents = solve_origination_cents(rate, pi_cents, payments)

    original_cents: Optional[int] = None
    original_basis = "unknown"
    inference: Optional[Dict[str, Any]] = None

    if stated_principal:
        original_cents = int(stated_principal)
        original_basis = "stated"
    elif solved_cents:
        original_cents, how = _snap(solved_cents)
        original_basis = "inferred"
        inference = {
            "method": (
                "The ending balance is an affine function of the origination "
                "amount, so requiring the loan to close at $0 on the final "
                "payment determines it exactly. Rate and scheduled P&I come "
                "from the lender's own schedule."
            ),
            "solved_cents": int(round(solved_cents)),
            "rounding": how,
            "monthly_rate": rate,
            "scheduled_pi_cents": pi_cents,
            "payments_used": len(payments),
        }

    result_terms = {
        "monthly_rate": rate,
        "annual_rate_pct": round(rate * 12 * 100, 4) if rate else None,
        "annual_rate_basis": rate_basis,
        "scheduled_pi_cents": pi_cents,
        "scheduled_pi_basis": pi_basis,
        "scheduled_pi_source": pi_source,
        "term_months": (stated_term
                        or (derive_term_months(original_cents, rate, pi_cents)
                            if (original_cents and rate and pi_cents) else None)),
        "term_months_basis": ("stated" if stated_term
                              else "derived" if original_cents and rate and pi_cents
                              else "unknown"),
        "scheduled_payoff_date": facts.get("scheduled_payoff_date"),
        "scheduled_payoff_date_basis": (
            "stated" if facts.get("scheduled_payoff_date") else "unknown"),
    }

    # ── run the stream ──────────────────────────────────────────────────────
    sim = None
    if original_cents and rate and pi_cents and payments:
        sim = _simulate(original_cents, rate, pi_cents, payments, exact=True)
        if sim["unexplained_cents"]:
            warnings.append(
                f"${sim['unexplained_cents']/100:,.2f} of the payments cannot be applied to "
                f"principal, interest or escrow under these terms. On a payoff that is usually "
                f"per-diem interest and recording fees; if it is large, a stated origination "
                f"amount or rate is probably wrong."
            )

    timeline = sim["rows"] if sim else [
        {**p, "interest_cents": None, "principal_cents": None,
         "escrow_cents": None, "balance_after_cents": None} for p in payments
    ]

    balance_cents: Optional[int] = None
    balance_basis = "unknown"
    if sim:
        balance_cents = int(round(sim["final_balance"]))
        balance_basis = "derived"

    status = "unknown"
    payoff_date = facts.get("payoff_date")
    payoff_basis = "stated" if payoff_date else "unknown"
    if balance_cents == 0 and payments:
        status = "closed"
        if not payoff_date:
            payoff_date = payments[-1]["posted_date"]
            payoff_basis = "measured"
    elif balance_cents is not None:
        status = "open"

    # ── escrow refunded: measured if the servicer sent it, else stated, else no ──
    #
    # The owner remembers roughly $1,600 coming back. It is not in the ledger —
    # no credit from the servicer at any date, in any account, and the closest
    # inflow of the right size is unrelated. Deliberately NOT modelled: an
    # approximate figure typed into a total is exactly the kind of invented
    # number this rewrite exists to remove. The UI offers to record it.
    escrow_refund_cents: Optional[int] = None
    escrow_refund_basis = "unknown"
    escrow_refund_note = (
        "Not in the ledger — no credit from the servicer has arrived in the data we hold. "
        "Record it here (or add it as a manual transaction) and it will be counted."
    )
    if credits:
        escrow_refund_cents = sum(c["amount_cents"] for c in credits)
        escrow_refund_basis = "measured"
        escrow_refund_note = f"{len(credits)} credit(s) from the servicer in the ledger."
    elif facts.get("escrow_refund_cents"):
        escrow_refund_cents = int(facts["escrow_refund_cents"])
        escrow_refund_basis = "stated"
        escrow_refund_note = (
            "Recorded by you"
            + (f" ({facts['escrow_refund_date']})" if facts.get("escrow_refund_date") else "")
            + " — not found in the ledger."
        )

    # ── what the extra principal bought ─────────────────────────────────────
    plan_rows: List[Dict[str, Any]] = []
    plan_interest: Optional[int] = None
    interest_saved: Optional[int] = None
    months_saved: Optional[int] = None
    if original_cents and rate and pi_cents:
        plan_rows = plan_schedule(original_cents, rate, pi_cents,
                                  facts.get("first_payment_date"))
        plan_interest = sum(r["interest_cents"] for r in plan_rows)
        if sim:
            interest_saved = plan_interest - sim["interest_cents"]
            months_saved = max(0, len(plan_rows) - len(scheduled_payments))

    figures = {
        "original_loan": _figure(
            original_cents, original_basis,
            "Solved from the real payment stream — see `inference`."
            if original_basis == "inferred" else
            ("Supplied by you." if original_basis == "stated"
             else "Not derivable: no lender schedule, so no rate to solve with."),
        ),
        "total_paid_to_lender": _figure(
            total_paid, "measured",
            f"{len(payments)} payments to the servicer, {effective_scope} scope.",
        ),
        "principal_paid": _figure(
            sim["principal_cents"] if sim else None,
            "derived" if sim else "unknown",
            "Equals the original loan when the loan is closed.",
        ),
        "interest_paid": _figure(
            sim["interest_cents"] if sim else None,
            "derived" if sim else "unknown",
            "Interest accrued on the real balance at the lender's rate, payment by payment.",
        ),
        "escrow_paid": _figure(
            sim["escrow_cents"] if sim else None,
            "derived" if sim else "unknown",
            "Each monthly payment above the scheduled P&I. Escrow is not a cost of the loan — "
            "it funds tax and insurance bills.",
        ),
        "escrow_refunded": _figure(escrow_refund_cents, escrow_refund_basis, escrow_refund_note),
        "final_balance": _figure(
            balance_cents, balance_basis,
            "The loan is closed." if status == "closed" else "Outstanding under these terms.",
        ),
        "scheduled_payments_paid": _figure(
            sum(p["amount_cents"] for p in scheduled_payments), "measured",
            f"{len(scheduled_payments)} monthly payments.",
        ),
        "extra_principal_paid": _figure(
            sum(p["amount_cents"] for p in extra_payments), "measured",
            f"{len(extra_payments)} curtailments — this is why the loan closed early.",
        ),
        "interest_saved_vs_plan": _figure(
            interest_saved, "derived" if interest_saved is not None else "unknown",
            (f"The original {len(plan_rows)}-payment schedule would have charged "
             f"${plan_interest/100:,.2f} of interest; {len(scheduled_payments)} monthly payments "
             f"and the curtailments cost ${sim['interest_cents']/100:,.2f}."
             if (interest_saved is not None and plan_interest is not None and sim) else ""),
        ),
        "plan_interest_total": _figure(
            plan_interest, "derived" if plan_interest is not None else "unknown",
            "Interest over the full original term, had the loan simply run its course.",
        ),
        "purchase_price": _figure(
            facts.get("purchase_price_cents"),
            "stated" if facts.get("purchase_price_cents") else "unknown",
            "What the house cost.",
        ),
        "down_payment": _figure(
            facts.get("down_payment_cents"),
            "stated" if facts.get("down_payment_cents") else "unknown",
            "Purchase price less the amount borrowed. The owner believes the wire below is it, "
            "but was not certain — the extra over this figure would be closing costs.",
        ),
    }

    # Money must add up: every cent paid is principal, interest, escrow or
    # explicitly unexplained. Reported rather than asserted, so a bad stated
    # fact shows as a residual instead of silently redistributing itself.
    accounted = None
    if sim:
        accounted = (sim["principal_cents"] + sim["interest_cents"]
                     + sim["escrow_cents"] + sim["unexplained_cents"])

    first_payment = payments[0]["posted_date"] if payments else None
    related = await _origination_context(db, first_payment, facts.get("down_payment_cents"))

    # ── cross-checks ────────────────────────────────────────────────────────
    # Numbers that had no reason to agree unless the model of this loan is right.
    # They are returned so the page can show its own working instead of asking to
    # be believed. ``agrees`` uses a tolerance appropriate to each check: cents
    # for money, a basis point for the rate.
    checks: List[Dict[str, Any]] = []
    if schedule_pi and pi_cents:
        checks.append({
            "name": "Scheduled P&I",
            "left_label": "from the stated terms",
            "left": pi_cents,
            "right_label": "from the lender's schedule",
            "right": schedule_pi,
            "unit": "cents",
            "agrees": abs(pi_cents - schedule_pi) <= 1,
        })
    if schedule_rate and rate:
        checks.append({
            "name": "Interest rate",
            "left_label": "stated" if rate_basis == "stated" else "from the schedule",
            "left": round(rate * 12 * 100, 4),
            "right_label": "implied by the schedule's own interest column",
            "right": round(schedule_rate * 12 * 100, 4),
            "unit": "percent",
            "agrees": abs(rate - schedule_rate) * 12 * 10_000 <= 1,
        })
    if solved_cents and original_cents:
        checks.append({
            "name": "Amount borrowed",
            "left_label": "stated" if original_basis == "stated" else "inferred",
            "left": original_cents,
            "right_label": "solved from the ledger (the balance that closes at $0)",
            "right": int(round(solved_cents)),
            "unit": "cents",
            "agrees": abs(original_cents - solved_cents) <= 100,
        })
    if sim and schedule:
        # The lender's projection opens at a balance it computed itself. If our
        # simulated balance passes through that number on the right date, the
        # whole reconstruction is confirmed by a document we did not fit to.
        anchor_opening = (schedule[0]["ending_balance_cents"]
                          + schedule[0]["principal_cents"])
        nearest = min(sim["rows"], key=lambda r: abs(r["balance_after_cents"] - anchor_opening))
        checks.append({
            "name": f"Lender projection opening balance ({schedule[0]['month'][:7]})",
            "left_label": f"our balance after {nearest['posted_date']}",
            "left": nearest["balance_after_cents"],
            "right_label": "the projection's own opening balance",
            "right": anchor_opening,
            "unit": "cents",
            "agrees": abs(nearest["balance_after_cents"] - anchor_opening) <= 100,
        })
    if (facts.get("purchase_price_cents") and original_cents
            and facts.get("down_payment_cents")):
        checks.append({
            "name": "Down payment",
            "left_label": "purchase price less the amount borrowed",
            "left": facts["purchase_price_cents"] - original_cents,
            "right_label": "stated down payment",
            "right": facts["down_payment_cents"],
            "unit": "cents",
            "agrees": (facts["purchase_price_cents"] - original_cents
                       == facts["down_payment_cents"]),
        })

    notes.append(
        "Total paid to the servicer is a straight sum over one scope, so the CSV history and "
        "its Plaid twin can never both be counted."
    )
    if sim:
        notes.append(
            "Principal, interest and escrow are DERIVED: a real amortization run over the "
            "actual payment dates at the lender's rate. They are not stored on the "
            "transactions, and no constant interest figure is used anywhere."
        )
    if status == "closed":
        notes.append(
            f"The loan closed on {payoff_date}. There is no next payment due, and the lender "
            f"schedule below is the plan it was on BEFORE the payoff — kept for comparison only."
        )
    if escrow_refund_basis == "unknown":
        notes.append(
            "Escrow refunded is unknown. Nothing from the servicer has come back in the data "
            "we hold — record the amount if you have the check."
        )
    if sim and abs(int(round(sim["final_balance"]))) <= 100 and not sim["unexplained_cents"]:
        notes.append(
            f"Reconciliation: ${total_paid/100:,.2f} paid = ${sim['principal_cents']/100:,.2f} "
            f"principal + ${sim['interest_cents']/100:,.2f} interest + "
            f"${sim['escrow_cents']/100:,.2f} escrow, ending at "
            f"${int(round(sim['final_balance']))/100:,.2f}. Nothing is left over."
        )

    return {
        "scope": effective_scope,
        "status": status,
        "servicer_tokens": {"ach_company_id": LENDER_ACH_ID, "payee": LENDER_PAYEE},
        "account_ids": sorted({p["account_id"] for p in payments}),
        "first_payment_date": first_payment,
        "first_payment_basis": "measured" if first_payment else "unknown",
        "payoff_date": payoff_date,
        "payoff_date_basis": payoff_basis,
        "origination_date": facts.get("origination_date"),
        "origination_date_basis": "stated" if facts.get("origination_date") else "unknown",
        "terms": result_terms,
        "figures": figures,
        "inference": inference,
        "solved_origination_cents": int(round(solved_cents)) if solved_cents else None,
        "checks": checks,
        "months_saved": months_saved,
        "reconciliation": {
            "total_paid_cents": total_paid,
            "accounted_cents": accounted,
            "unexplained_cents": sim["unexplained_cents"] if sim else None,
            "residual_cents": balance_cents,
        },
        "timeline": timeline,
        "plan_schedule": plan_rows,
        "lender_projection": [
            {**row, "is_projection": True} for row in schedule
        ],
        "related": related,
        "facts": facts,
        "warnings": warnings,
        "notes": notes,
    }


# ── One-time cleanup of the fabricated split rows ───────────────────────────
SPLIT_MERCHANTS = ("Mortgage Principal", "Mortgage Interest", "Mortgage Escrow")


async def remove_fabricated_splits(db) -> Dict[str, Any]:
    """Delete the splitter's invented principal/interest children.

    ``mortgage_splitter`` wrote child rows whose interest was a CONSTANT $843.01
    copied from one row of a forward projection — including on a $100,000
    curtailment, where the true interest is zero. They also carry hierarchical
    category paths that no longer exist in the flat model, so they show up as
    "Other" expenses. The split is now derived at read time by ``analyze``, so
    these rows have no reason to exist; removing them takes fabricated interest
    out of every report that sums the ledger.

    Idempotent, and it restores each parent to the category its Plaid twin
    already carries so the payment is still visible as a mortgage payment.
    """
    cursor = await db.execute(
        """SELECT id, linked_transaction_id FROM transactions
            WHERE linked_transaction_id IS NOT NULL
              AND merchant IN (?, ?, ?)
              AND description LIKE '%split from%'""",
        SPLIT_MERCHANTS,
    )
    children = [dict(r) for r in await cursor.fetchall()]
    if not children:
        return {"removed": 0, "parents_restored": 0}

    parent_ids = sorted({c["linked_transaction_id"] for c in children if c["linked_transaction_id"]})
    await db.execute(
        "DELETE FROM transactions WHERE id IN ({})".format(
            ",".join("?" * len(children))),
        [c["id"] for c in children],
    )
    restored = 0
    if parent_ids:
        cursor = await db.execute(
            "UPDATE transactions SET category_path='Mortgage Payment', "
            "entity_type='transfer', asset_class='mortgage_principal' "
            "WHERE id IN ({}) AND category_path IN ('Transfer', 'Transfer > Mortgage Payment (Split)')".format(
                ",".join("?" * len(parent_ids))),
            parent_ids,
        )
        restored = cursor.rowcount or 0
    await db.commit()
    logger.info("Removed %d fabricated mortgage split rows, restored %d parents",
                len(children), restored)
    return {"removed": len(children), "parents_restored": restored}
