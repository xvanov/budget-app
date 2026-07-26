"""The mortgage tab must report the loan, not a projection.

Every test here fails against the code this replaces:

* the totals used to be summed from ``category_path='Transfer > Mortgage
  Principal'`` — a category the flat model dropped and which the retired splitter
  wrote — so they were either zero or double-counted across the CSV/Plaid twins;
* interest came from a CONSTANT $843.01 copied out of the projection's first row
  and applied to every payment, including a $100,000 curtailment;
* the balance was the projection's mid-life anchor ($153,538) on a loan that is
  closed, and there was no payoff date anywhere;
* unknown facts were filled in from whatever row happened to be at hand rather
  than reported as unknown.

Fixtures synthesise the owner's real payment stream shape: a servicer autopay
identified by its ACH company id, online bill-pay curtailments to the same loan,
and each row present TWICE (once from the CSV import, once from Plaid) which is
what makes an unscoped sum double.
"""
import os
import sqlite3

import pytest

from app.services.mortgage import (
    LENDER_ACH_ID,
    analyze,
    payment_from_terms,
    plan_schedule,
    remove_fabricated_splits,
    schedule_terms,
    solve_origination_cents,
)

# The owner's loan: $280,000 at 6.49% over 120 months, first payment 2025-04-01.
TERMS = {
    "original_principal_cents": 28_000_000,
    "annual_rate_bps": 649,
    "term_months": 120,
    "first_payment_date": "2025-04-01",
}
MONTHLY_ACH_CENTS = 372_253          # P&I 3,177.92 + escrow 544.61
LATER_ACH_CENTS = 392_597            # after the escrow re-analysis
MONTHLY_DATES = [
    "2025-04-02", "2025-04-09", "2025-05-09", "2025-06-09", "2025-07-09",
    "2025-08-11", "2025-09-09", "2025-10-09", "2025-11-10", "2025-12-09",
    "2026-01-09", "2026-02-09",
]
CURTAILMENTS = [
    ("2026-04-28", 10_000_000), ("2026-05-18", 500_000), ("2026-06-01", 600_000),
    ("2026-06-04", 3_000_000), ("2026-06-23", 4_000_000), ("2026-06-24", 5_000_000),
    ("2026-07-08", 1_996_887),
]
EXPECTED_TOTAL_PAID = 31_149_194
EXPECTED_INTEREST = 2_181_559
EXPECTED_ESCROW = 967_635


def _conn():
    return sqlite3.connect(os.environ["DB_PATH"])


def _wipe(con):
    con.execute("DELETE FROM transactions")
    con.execute("DELETE FROM amortization")
    con.execute("DELETE FROM mortgage_facts")
    con.commit()


def _insert(con, posted_date, amount_cents, description, platform, account="chase-3600"):
    con.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, normalized_description, category_path, entity_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (platform, "fixture.csv", account, posted_date, amount_cents,
         "HMG Mortgage", description, description.lower(), "Mortgage Payment", "transfer"),
    )


def _seed_loan(con, *, with_facts=True, with_schedule=True, twins=True):
    """Every payment written twice — once as CSV history, once as its Plaid twin."""
    platforms = ("chase", "plaid") if twins else ("plaid",)
    ach = f"JPMORGAN CHASE   CHASE ACH                  PPD ID: {LENDER_ACH_ID}"
    for platform in platforms:
        for i, date in enumerate(MONTHLY_DATES):
            if i == 0:
                _insert(con, date, -MONTHLY_ACH_CENTS,
                        "Online Payment 24267959357 To HMG 5476 04/02", platform)
            else:
                _insert(con, date, -MONTHLY_ACH_CENTS, ach, platform)
        _insert(con, "2026-03-09", -407_480, ach, platform)   # escrow catch-up month
        for date in ("2026-04-09", "2026-05-11", "2026-06-09"):
            _insert(con, date, -LATER_ACH_CENTS, ach, platform)
        for date, cents in CURTAILMENTS:
            _insert(con, date, -cents,
                    f"Online Payment 29999999999 To HMG 5476 {date[5:7]}/{date[8:]}", platform)

    if with_schedule:
        # A slice of the real lender projection: it opens mid-life, in 2026-06.
        balance = 15_353_815
        for month, principal, interest in (
            ("2026-06-01", 233_491, 84_301),
            ("2026-07-01", 234_753, 83_039),
            ("2026-08-01", 236_023, 81_769),
        ):
            con.execute(
                "INSERT INTO amortization (month, principal_cents, interest_cents, ending_balance_cents)"
                " VALUES (?,?,?,?)",
                (month, principal, interest, balance),
            )
            balance -= principal
    if with_facts:
        cols = ", ".join(TERMS)
        con.execute(
            f"INSERT INTO mortgage_facts (id, {cols}) VALUES (1, {', '.join('?' * len(TERMS))})",
            list(TERMS.values()),
        )
    con.commit()


@pytest.fixture
def loan_db(test_client):
    con = _conn()
    try:
        _wipe(con)
        _seed_loan(con)
        yield con
    finally:
        _wipe(con)
        con.close()


# ── (a) totals equal a scoped SQL sum, and are NOT doubled ───────────────────
def test_total_paid_equals_scoped_sql_sum_and_is_not_doubled(test_client, loan_db):
    resp = test_client.get("/api/mortgage")
    assert resp.status_code == 200
    data = resp.json()

    scoped = loan_db.execute(
        """SELECT COALESCE(SUM(-amount_cents), 0) FROM transactions
            WHERE amount_cents < 0
              AND (description LIKE ? OR UPPER(description) LIKE '%HMG 5476%')
              AND source_platform IN ('plaid','manual')""",
        (f"%{LENDER_ACH_ID}%",),
    ).fetchone()[0]
    unscoped = loan_db.execute(
        """SELECT COALESCE(SUM(-amount_cents), 0) FROM transactions
            WHERE amount_cents < 0
              AND (description LIKE ? OR UPPER(description) LIKE '%HMG 5476%')""",
        (f"%{LENDER_ACH_ID}%",),
    ).fetchone()[0]

    assert scoped == EXPECTED_TOTAL_PAID
    assert unscoped == 2 * scoped, "fixture must contain the CSV/Plaid twins"
    assert data["figures"]["total_paid_to_lender"]["cents"] == scoped
    assert data["figures"]["total_paid_to_lender"]["basis"] == "measured"
    assert len(data["timeline"]) == 23


def test_scope_all_reports_the_doubling_rather_than_hiding_it(test_client, loan_db):
    """scope=all deliberately sees both sources; the twin warning must fire."""
    data = test_client.get("/api/mortgage?scope=all").json()
    assert data["figures"]["total_paid_to_lender"]["cents"] == 2 * EXPECTED_TOTAL_PAID
    # and the derived split cannot balance, which shows up as leftover money
    assert data["reconciliation"]["unexplained_cents"] > 0


# ── (b) no fabricated constant interest anywhere in the output ───────────────
def test_no_constant_interest_across_payments(test_client, loan_db):
    """$843.01 was the projection's first-row interest, pasted onto every payment.

    Two things must hold: interest varies with the balance, and a curtailment
    carries none at all.
    """
    data = test_client.get("/api/mortgage").json()
    scheduled = [t for t in data["timeline"] if t["kind"] == "scheduled"]
    extras = [t for t in data["timeline"] if t["kind"] == "extra"]

    interests = {t["interest_cents"] for t in scheduled}
    assert len(interests) == len(scheduled), "interest must fall as the balance falls"
    assert all(t["interest_cents"] > 0 for t in scheduled)
    assert all(t["interest_cents"] == 0 for t in extras), \
        "a principal-only curtailment accrues no interest"
    assert all(t["principal_cents"] == t["amount_cents"] for t in extras)

    # The fabricated constant must not appear as EVERY payment's interest.
    assert [t["interest_cents"] for t in scheduled].count(84_301) <= 1

    # Interest is monotonically decreasing across consecutive scheduled payments
    # on a falling balance — the property a constant cannot have.
    for earlier, later in zip(scheduled, scheduled[1:]):
        assert later["interest_cents"] <= earlier["interest_cents"]


def test_money_is_conserved(test_client, loan_db):
    data = test_client.get("/api/mortgage").json()
    f = data["figures"]
    assert (f["principal_paid"]["cents"] + f["interest_paid"]["cents"]
            + f["escrow_paid"]["cents"]) == f["total_paid_to_lender"]["cents"]
    assert f["interest_paid"]["cents"] == EXPECTED_INTEREST
    assert f["escrow_paid"]["cents"] == EXPECTED_ESCROW
    assert data["reconciliation"]["unexplained_cents"] == 0
    # Per-row conservation too.
    for row in data["timeline"]:
        assert (row["principal_cents"] + row["interest_cents"]
                + row["escrow_cents"]) == row["amount_cents"]


# ── (c) a paid-off loan reports balance 0 and a payoff date ──────────────────
def test_closed_loan_reports_zero_balance_and_payoff_date(test_client, loan_db):
    data = test_client.get("/api/mortgage").json()
    assert data["status"] == "closed"
    assert data["figures"]["final_balance"]["cents"] == 0
    assert data["payoff_date"] == "2026-07-08"
    assert data["payoff_date_basis"] == "measured"
    # The principal repaid over the life of a closed loan IS the loan.
    assert data["figures"]["principal_paid"]["cents"] == TERMS["original_principal_cents"]
    # And the projection is never presented as the current position.
    assert data["lender_projection"][0]["is_projection"] is True
    assert data["figures"]["final_balance"]["cents"] != 15_353_815


def test_open_loan_still_reports_a_balance(test_client, loan_db):
    """Drop the curtailments: the same code must report an OPEN loan, not force 0."""
    loan_db.execute("DELETE FROM transactions WHERE posted_date >= '2026-04-28'")
    loan_db.commit()
    data = test_client.get("/api/mortgage").json()
    assert data["status"] == "open"
    assert data["figures"]["final_balance"]["cents"] > 0
    assert data["payoff_date"] is None


# ── (d) unknown facts surface as null, never as an invented number ───────────
def test_unknown_escrow_refund_is_null_not_zero(test_client, loan_db):
    data = test_client.get("/api/mortgage").json()
    refund = data["figures"]["escrow_refunded"]
    assert refund["cents"] is None
    assert refund["basis"] == "unknown"
    assert "record" in refund["note"].lower()


def test_stated_escrow_refund_round_trips_and_is_labelled_stated(test_client, loan_db):
    resp = test_client.put("/api/mortgage/facts",
                           json={"escrow_refund_cents": 160_000,
                                 "escrow_refund_date": "2026-07-20"})
    assert resp.status_code == 200
    data = test_client.get("/api/mortgage").json()
    assert data["figures"]["escrow_refunded"] == {
        "cents": 160_000, "basis": "stated",
        "note": "Recorded by you (2026-07-20) — not found in the ledger.",
    }
    # Clearing it hands the figure back to "unknown" rather than leaving a ghost.
    test_client.put("/api/mortgage/facts", json={"escrow_refund_cents": None})
    assert test_client.get("/api/mortgage").json()["figures"]["escrow_refunded"]["cents"] is None


def test_measured_servicer_credit_outranks_a_stated_refund(test_client, loan_db):
    test_client.put("/api/mortgage/facts", json={"escrow_refund_cents": 160_000})
    _insert(loan_db, "2026-08-01", 162_318,
            "Online Payment refund From HMG 5476 08/01", "plaid")
    loan_db.commit()
    refund = test_client.get("/api/mortgage").json()["figures"]["escrow_refunded"]
    assert refund["basis"] == "measured"
    assert refund["cents"] == 162_318


def test_without_terms_or_schedule_the_split_is_unknown_not_guessed(test_client, loan_db):
    """No rate anywhere = no defensible split. The cash total is still exact."""
    loan_db.execute("DELETE FROM mortgage_facts")
    loan_db.execute("DELETE FROM amortization")
    loan_db.commit()
    data = test_client.get("/api/mortgage").json()
    f = data["figures"]
    assert f["total_paid_to_lender"]["cents"] == EXPECTED_TOTAL_PAID
    assert f["total_paid_to_lender"]["basis"] == "measured"
    for key in ("original_loan", "principal_paid", "interest_paid", "escrow_paid"):
        assert f[key]["cents"] is None, key
        assert f[key]["basis"] == "unknown", key
    assert data["status"] == "unknown"
    assert all(row["interest_cents"] is None for row in data["timeline"])


def test_empty_ledger_says_nothing_rather_than_something_wrong(test_client):
    """A fresh install has no loan. Every figure must be null, and nothing 500s."""
    con = _conn()
    try:
        _wipe(con)
        data = test_client.get("/api/mortgage").json()
        assert data["figures"]["total_paid_to_lender"]["cents"] == 0
        assert data["figures"]["final_balance"]["cents"] is None
        assert data["status"] == "unknown"
        assert data["payoff_date"] is None
        assert data["timeline"] == []
        assert data["related"] == []
    finally:
        con.close()


def test_origination_is_inferred_from_the_ledger_when_not_stated(test_client, loan_db):
    """Terms cleared but the lender schedule kept: the amount is solvable.

    This is the check that the arithmetic is real — the solve gets to $280,000
    from the payment stream alone, and it is labelled `inferred`, not `measured`.
    """
    loan_db.execute("DELETE FROM mortgage_facts")
    loan_db.commit()
    data = test_client.get("/api/mortgage").json()
    original = data["figures"]["original_loan"]
    assert original["basis"] == "inferred"
    assert original["cents"] == 28_000_000
    # The raw solve lands within a few dollars: this fixture keeps only 3 rows of
    # the lender schedule, so the median rate it recovers is a hair coarser than
    # the real 58-row schedule (which solves to $279,999.99, one cent out).
    assert abs(data["solved_origination_cents"] - 28_000_000) <= 500
    assert data["inference"]["payments_used"] == 23
    assert data["inference"]["rounding"] == "snapped to the nearest $1,000"


def test_cross_checks_all_agree_on_the_owners_loan(test_client, loan_db):
    data = test_client.get("/api/mortgage").json()
    names = {c["name"] for c in data["checks"]}
    assert {"Scheduled P&I", "Interest rate", "Amount borrowed"} <= names
    for check in data["checks"]:
        assert check["agrees"], check


# ── the arithmetic, unit level ───────────────────────────────────────────────
def test_payment_from_terms_matches_the_lender_schedule():
    rate = 649 / 10_000 / 12
    assert payment_from_terms(28_000_000, rate, 120) == 317_792


def test_schedule_terms_recovers_rate_and_payment_from_a_projection():
    schedule = [
        {"month": "2026-06-01", "principal_cents": 233_491, "interest_cents": 84_301,
         "ending_balance_cents": 15_353_815},
        {"month": "2026-07-01", "principal_cents": 234_753, "interest_cents": 83_039,
         "ending_balance_cents": 15_119_062},
    ]
    rate, pi = schedule_terms(schedule)
    assert pi == 317_792
    assert round(rate * 12 * 100, 2) == 6.49


def test_solve_is_exact_on_a_synthetic_loan():
    """Build a payment stream from known terms; the solve must return the loan."""
    rate = 6.0 / 100 / 12
    pi = payment_from_terms(20_000_000, rate, 180)
    payments = [{"kind": "scheduled", "amount_cents": pi, "posted_date": f"2020-{m:02d}-01",
                 "id": m, "description": "", "merchant": "", "account_id": "a",
                 "source_platform": "plaid"} for m in range(1, 13)]
    balance = 20_000_000
    for _ in payments:
        balance = balance - (pi - round(balance * rate))
    payments.append({"kind": "extra", "amount_cents": balance, "posted_date": "2021-01-01",
                     "id": 99, "description": "", "merchant": "", "account_id": "a",
                     "source_platform": "plaid"})
    solved = solve_origination_cents(rate, pi, payments)
    assert abs(solved - 20_000_000) < 100


def test_plan_schedule_runs_the_full_term_and_ends_at_zero():
    rate = 649 / 10_000 / 12
    rows = plan_schedule(28_000_000, rate, 317_792, "2025-04-01")
    assert len(rows) == 120
    assert rows[-1]["ending_balance_cents"] == 0
    assert rows[-1]["date"] == "2035-03-01"
    assert sum(r["interest_cents"] for r in rows) == 10_135_029


# ── the fabricated split rows must be removable, idempotently ───────────────
def test_remove_fabricated_splits(test_client, loan_db):
    import asyncio

    from app.database import get_db

    parent_id = loan_db.execute(
        "SELECT id FROM transactions WHERE posted_date='2026-04-28' AND source_platform='chase'"
    ).fetchone()[0]
    for merchant, cents, path in (
        ("Mortgage Principal", -9_915_699, "Mortgage Payment"),
        ("Mortgage Interest", -84_301, "Other"),
    ):
        loan_db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, normalized_description, category_path,
                entity_type, linked_transaction_id)
               VALUES ('chase','fixture.csv','chase-3600','2026-04-28',?,?,?,?,?,?,?)""",
            (cents, merchant, f"{merchant.split()[1]} split from HMG Mortgage payment",
             "split", path, "expense", parent_id),
        )
    loan_db.execute("UPDATE transactions SET category_path='Transfer' WHERE id=?", (parent_id,))
    loan_db.commit()

    async def run():
        db = await get_db()
        try:
            first = await remove_fabricated_splits(db)
            second = await remove_fabricated_splits(db)
            return first, second
        finally:
            await db.close()

    first, second = asyncio.new_event_loop().run_until_complete(run())
    assert first["removed"] == 2
    assert first["parents_restored"] == 1
    assert second == {"removed": 0, "parents_restored": 0}, "must be idempotent"

    remaining = loan_db.execute(
        "SELECT COUNT(*) FROM transactions WHERE description LIKE '%split from%'"
    ).fetchone()[0]
    assert remaining == 0
    assert loan_db.execute(
        "SELECT category_path FROM transactions WHERE id=?", (parent_id,)
    ).fetchone()[0] == "Mortgage Payment"


def test_fabricated_split_children_never_enter_the_payment_stream(test_client, loan_db):
    """Even in a database that still has them, they must not be counted.

    The splitter's children say "split from HMG Mortgage payment", which does not
    contain the loan's payee token, so the identity match excludes them. Without
    this the $100,000 curtailment would be counted twice over.
    """
    parent_id = loan_db.execute(
        "SELECT id FROM transactions WHERE posted_date='2026-04-28' AND source_platform='plaid'"
    ).fetchone()[0]
    loan_db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, normalized_description, category_path,
            entity_type, linked_transaction_id)
           VALUES ('plaid','fixture.csv','chase-3600','2026-04-28',-84301,
                   'Mortgage Interest','Interest split from HMG Mortgage payment',
                   'split','Other','expense',?)""",
        (parent_id,),
    )
    loan_db.commit()
    data = test_client.get("/api/mortgage").json()
    assert data["figures"]["total_paid_to_lender"]["cents"] == EXPECTED_TOTAL_PAID
    assert len(data["timeline"]) == 23


# ── the deprecated alias keeps telling the same truth ───────────────────────
def test_amortization_alias_agrees_with_the_mortgage_endpoint(test_client, loan_db):
    amort = test_client.get("/api/amortization").json()
    mortgage = test_client.get("/api/mortgage").json()
    s = amort["summary"]
    assert s["total_paid_cents"] == mortgage["figures"]["total_paid_to_lender"]["cents"]
    assert s["total_interest_paid_cents"] == EXPECTED_INTEREST
    assert s["current_balance_cents"] == 0
    assert s["payoff_date"] == "2026-07-08"
    assert s["status"] == "closed"
    # The schedule it returns is still flagged as the projection it is.
    assert all(row["is_projection"] for row in amort["schedule"])


def test_amortization_alias_ignores_inflows_on_the_loan(test_client, loan_db):
    """Regression from the old endpoint: an incoming wire counted as principal."""
    before = test_client.get("/api/amortization").json()["summary"]["total_paid_cents"]
    _insert(loan_db, "2026-02-20", 500_000,
            "Online Payment 1 From HMG 5476 refund", "plaid")
    loan_db.commit()
    after = test_client.get("/api/amortization").json()["summary"]["total_paid_cents"]
    assert after == before, "a credit is not money paid to the lender"
