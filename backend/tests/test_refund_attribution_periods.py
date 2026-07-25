"""Refunds are attributed to the period of the CHARGE, not of the credit.

The distortion this fixes, in the owner's words: "an expense made in June doesn't
count as it was refunded in July." Before, every row landed in its own month, so a
$46 June charge reversed by a $46 July credit made June overstate spending by $46
AND July show $46 of phantom income — two wrong months for one wash. On the live
data 48 pairs straddle a month boundary, moving single months by up to $430.

Every total over a date range now reads the effective amount from
``services/scope.py``: a credit half contributes 0, a charge half contributes what
is still genuinely spent (0 when fully refunded, the outstanding portion when
partial), in the charge's own month.

The rows here are seeded directly, in a year no other test touches, so the
expected cents are exact rather than "roughly".
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.scope import effective_amount_cents

MARK = "Refundattr"
_INSERTED = []

# The main window. One month per shape so a wrong attribution cannot hide in an
# aggregate: charges in 06, their credits in 07, a same-month wash in 08, a real
# inflow in 09.
START, END = "2031-06-01", "2031-09-30"

CHARGE_FULL = -4600      # 2031-06, fully reversed in 2031-07
CHARGE_PARTIAL = -10000  # 2031-06, $30 back in 2031-07 -> $70 still spent
CREDIT_PARTIAL = 3000
PLAIN_EXPENSE = -2500    # 2031-06, never refunded
SAME_MONTH = -5000       # 2031-08 charge and credit both
REAL_INCOME = 7700       # 2031-09, an unpaired credit: genuine money in


async def _insert(db, *, amount, date, category, entity, label):
    cursor = await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, normalized_description, category_path,
            entity_type, business_context)
           VALUES ('plaid','refundattr.csv','card-0003',?,?,?,?,?,?,?,'personal')""",
        (date, amount, f"{MARK} {label}", f"{MARK} {label}", f"{MARK} {label}".lower(),
         category, entity),
    )
    _INSERTED.append(cursor.lastrowid)
    return cursor.lastrowid


async def _pair(db, charge_id, credit_id, *, partial):
    """Mark a charge/credit pair exactly as services/refund_pairs.py does."""
    await db.execute(
        "UPDATE transactions SET refund_match_id=?, refund_role='charge', refund_partial=? WHERE id=?",
        (credit_id, 1 if partial else 0, charge_id),
    )
    await db.execute(
        "UPDATE transactions SET refund_match_id=?, refund_role='credit', refund_partial=? WHERE id=?",
        (charge_id, 1 if partial else 0, credit_id),
    )


@pytest.fixture(autouse=True)
def cleanup():
    yield
    if not _INSERTED:
        return
    con = sqlite3.connect(os.environ["DB_PATH"])
    try:
        con.execute(
            f"DELETE FROM transactions WHERE id IN ({','.join('?' * len(_INSERTED))})",
            _INSERTED,
        )
        con.commit()
    finally:
        con.close()
        _INSERTED.clear()


@pytest.fixture
def seeded(test_client):
    """Seed the four-month scenario; returns the row ids by name."""
    import asyncio

    from app.database import get_db

    async def go():
        db = await get_db()
        try:
            ids = {}
            ids["charge_full"] = await _insert(
                db, amount=CHARGE_FULL, date="2031-06-10", category="Home Improvement",
                entity="expense", label="full charge")
            ids["credit_full"] = await _insert(
                db, amount=-CHARGE_FULL, date="2031-07-05", category="Home Improvement",
                entity="income", label="full credit")
            ids["charge_partial"] = await _insert(
                db, amount=CHARGE_PARTIAL, date="2031-06-12", category="Shopping",
                entity="expense", label="partial charge")
            ids["credit_partial"] = await _insert(
                db, amount=CREDIT_PARTIAL, date="2031-07-08", category="Shopping",
                entity="income", label="partial credit")
            ids["plain"] = await _insert(
                db, amount=PLAIN_EXPENSE, date="2031-06-20", category="Groceries",
                entity="expense", label="plain expense")
            ids["same_charge"] = await _insert(
                db, amount=SAME_MONTH, date="2031-08-03", category="Dining Out",
                entity="expense", label="same month charge")
            ids["same_credit"] = await _insert(
                db, amount=-SAME_MONTH, date="2031-08-20", category="Dining Out",
                entity="income", label="same month credit")
            ids["real_income"] = await _insert(
                db, amount=REAL_INCOME, date="2031-09-15", category="Consulting",
                entity="income", label="real inflow")

            await _pair(db, ids["charge_full"], ids["credit_full"], partial=False)
            await _pair(db, ids["charge_partial"], ids["credit_partial"], partial=True)
            await _pair(db, ids["same_charge"], ids["same_credit"], partial=False)
            await db.commit()
            return ids
        finally:
            await db.close()

    return asyncio.run(go())


def _dashboard(test_client, start=START, end=END):
    resp = test_client.get(f"/api/dashboard?start_date={start}&end_date={end}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _months(data):
    return {m["month"]: m for m in data["monthly"]}


def _legacy_net(start, end):
    """What SUM(amount_cents) said before this change, over countable rows.

    The whole-window net must not move: attribution re-times a refund, it does not
    invent or destroy money.
    """
    con = sqlite3.connect(os.environ["DB_PATH"])
    try:
        row = con.execute(
            """SELECT COALESCE(SUM(amount_cents), 0) FROM transactions
                WHERE posted_date >= ? AND posted_date <= ?
                  AND source_platform IN ('plaid','manual')
                  AND (entity_type IS NULL OR
                       entity_type NOT IN ('transfer','asset_conversion','excluded'))
                  AND category_path IS NOT NULL AND category_path != 'Uncategorized'""",
            (start, end),
        ).fetchone()
        return row[0]
    finally:
        con.close()


# ------------------------------------------------- the cross-month distortion

def test_fully_refunded_charge_stops_counting_in_its_own_month(seeded, test_client):
    """June must not be billed for a purchase that came back in July."""
    months = _months(_dashboard(test_client))
    june = months["2031-06"]
    # $25 groceries + $70 still owed on the partial. The $46 wash is gone.
    assert june["expense_cents"] == -PLAIN_EXPENSE + (-CHARGE_PARTIAL - CREDIT_PARTIAL)
    assert june["income_cents"] == 0


def test_the_credit_month_shows_no_phantom_income(seeded, test_client):
    """July received $76 of credits; none of it is earnings."""
    months = _months(_dashboard(test_client))
    assert "2031-07" in months, "the month must still be reported, just at zero"
    assert months["2031-07"]["income_cents"] == 0
    assert months["2031-07"]["expense_cents"] == 0


def test_partial_refund_leaves_the_outstanding_amount_in_the_charge_month(seeded, test_client):
    """$100 spent, $30 back next month -> $70 of spending, booked in June."""
    data = _dashboard(test_client)
    shopping = next(c for c in data["by_category"] if c["category_path"] == "Shopping")
    assert shopping["expense_cents"] == 7000
    assert shopping["income_cents"] == 0
    june_only = _dashboard(test_client, "2031-06-01", "2031-06-30")
    assert june_only["totals"]["total_expense_cents"] == 7000 + 2500
    july_only = _dashboard(test_client, "2031-07-01", "2031-07-31")
    assert july_only["totals"]["total_income_cents"] == 0


def test_a_fully_refunded_category_reports_nothing_on_either_side(seeded, test_client):
    data = _dashboard(test_client)
    home = [c for c in data["by_category"] if c["category_path"] == "Home Improvement"]
    assert home, "the category is still listed — a wash is not a missing row"
    assert home[0]["expense_cents"] == 0 and home[0]["income_cents"] == 0
    assert home[0]["net_cents"] == 0


# -------------------------------------------------------- no double-applying

def test_same_month_pair_is_unchanged_by_attribution(seeded, test_client):
    """The case that already netted correctly must not be netted twice.

    A -$50 charge and its +$50 credit in the same month netted to zero before this
    change. It still nets to zero — not -$100, not +$100.
    """
    aug = _dashboard(test_client, "2031-08-01", "2031-08-31")
    assert aug["totals"]["net_cents"] == 0
    assert aug["totals"]["net_cents"] == _legacy_net("2031-08-01", "2031-08-31")
    assert aug["totals"]["total_expense_cents"] == 0
    assert aug["totals"]["total_income_cents"] == 0
    dining = next(c for c in aug["by_category"] if c["category_path"] == "Dining Out")
    assert dining["net_cents"] == 0
    assert dining["net_cents"] != 2 * SAME_MONTH, "a same-month pair must not be double-applied"


def test_whole_window_net_is_the_same_money_as_before(seeded, test_client):
    """Attribution re-times refunds; over a window holding both halves it is a no-op."""
    data = _dashboard(test_client)
    assert data["totals"]["net_cents"] == _legacy_net(START, END)
    expected = (CHARGE_FULL - CHARGE_FULL + CHARGE_PARTIAL + CREDIT_PARTIAL
                + PLAIN_EXPENSE + SAME_MONTH - SAME_MONTH + REAL_INCOME)
    assert data["totals"]["net_cents"] == expected == -1800


# ------------------------------------------------------------- real inflows

def test_an_unpaired_credit_is_still_income(seeded, test_client):
    """Zeroing credits must only touch the ones that reverse a charge."""
    months = _months(_dashboard(test_client))
    assert months["2031-09"]["income_cents"] == REAL_INCOME
    assert months["2031-09"]["expense_cents"] == 0
    data = _dashboard(test_client)
    assert data["totals"]["total_income_cents"] == REAL_INCOME


def test_a_credit_whose_partner_is_not_a_charge_still_counts(seeded, test_client):
    """Half-cleared pair: the guard must not silently eat a real inflow."""
    import asyncio

    from app.database import get_db

    async def half_clear():
        db = await get_db()
        try:
            # Drop the charge's side of the full pair, as a partial re-detect would.
            await db.execute(
                "UPDATE transactions SET refund_match_id=NULL, refund_role=NULL WHERE id=?",
                (seeded["charge_full"],),
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(half_clear())
    months = _months(_dashboard(test_client))
    assert months["2031-07"]["income_cents"] == -CHARGE_FULL
    assert months["2031-06"]["expense_cents"] == -PLAIN_EXPENSE + 7000 + -CHARGE_FULL


# ---------------------------------------------------------------- identities

def test_net_equals_income_minus_expense_everywhere(seeded, test_client):
    data = _dashboard(test_client)
    t = data["totals"]
    assert t["net_cents"] == t["total_income_cents"] - t["total_expense_cents"]
    for cat in data["by_category"]:
        assert cat["net_cents"] == cat["income_cents"] - cat["expense_cents"]
    trends = test_client.get(f"/api/trends?start_date={START}&end_date={END}").json()
    for s in trends["series"]:
        assert s["net_cents"] == s["income_cents"] - s["expense_cents"]


def test_dashboard_months_sum_to_the_dashboard_totals(seeded, test_client):
    """The per-month series and the per-category totals must tell one story."""
    data = _dashboard(test_client)
    assert sum(m["income_cents"] for m in data["monthly"]) == data["totals"]["total_income_cents"]
    assert sum(m["expense_cents"] for m in data["monthly"]) == data["totals"]["total_expense_cents"]


# ------------------------------------------------ every reporting endpoint agrees

def test_trends_agrees_with_the_dashboard_month_by_month(seeded, test_client):
    dash = _months(_dashboard(test_client))
    trends = test_client.get(f"/api/trends?start_date={START}&end_date={END}").json()
    series = {s["period"]: s for s in trends["series"]}
    assert set(series) == set(dash)
    for period, s in series.items():
        assert s["income_cents"] == dash[period]["income_cents"], period
        assert s["expense_cents"] == dash[period]["expense_cents"], period


def test_trends_category_breakdown_drops_the_refunded_purchase(seeded, test_client):
    trends = test_client.get(f"/api/trends?start_date={START}&end_date={END}").json()
    june = next(s for s in trends["series"] if s["period"] == "2031-06")
    cats = june.get("categories") or {}
    assert cats.get("Groceries") == -PLAIN_EXPENSE
    assert cats.get("Shopping") == 7000, "only the outstanding portion is spending"
    assert "Home Improvement" not in cats, "a fully refunded purchase is not spending"


def test_sankey_agrees_with_the_dashboard(seeded, test_client):
    dash = _dashboard(test_client)["totals"]
    sankey = test_client.get(f"/api/sankey?start_date={START}&end_date={END}").json()
    assert sankey["total_income_cents"] == dash["total_income_cents"]
    assert sankey["total_expense_cents"] == dash["total_expense_cents"]
    assert sankey["net_cents"] == dash["net_cents"]
    assert not [n for n in sankey["nodes"] if n["label"] == "Home Improvement"], \
        "a fully refunded purchase must not appear as a flow"


def test_compare_agrees_with_the_dashboard(seeded, test_client):
    dash = _dashboard(test_client)["totals"]
    resp = test_client.get(
        "/api/compare"
        f"?period1_start={START}&period1_end={END}"
        "&period2_start=2032-01-01&period2_end=2032-12-31"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    p1 = data["period1"]["totals"]
    assert p1["income_cents"] == dash["total_income_cents"]
    assert p1["expense_cents"] == dash["total_expense_cents"]
    assert p1["net_cents"] == dash["net_cents"]
    shopping = next(c for c in data["categories"] if c["category_path"] == "Shopping")
    assert shopping["period1_cents"] == 7000


def test_compare_splits_a_cross_month_pair_the_same_way_as_the_dashboard(seeded, test_client):
    """June vs July, the exact shape of the owner's complaint."""
    data = test_client.get(
        "/api/compare?period1_start=2031-06-01&period1_end=2031-06-30"
        "&period2_start=2031-07-01&period2_end=2031-07-31"
    ).json()
    assert data["period1"]["totals"]["expense_cents"] == 2500 + 7000
    assert data["period2"]["totals"]["income_cents"] == 0
    assert data["period2"]["totals"]["expense_cents"] == 0


# --------------------------------------------------------- audit trail stays

def test_the_pair_is_still_inspectable_on_the_refunded_tab(seeded, test_client):
    """A month total that moves must be explainable, so the pair stays listed."""
    data = test_client.get(
        f"/api/refunded?start_date={START}&end_date={END}").json()
    by_charge = {p["charge_id"]: p for p in data["pairs"]}
    partial = by_charge[seeded["charge_partial"]]
    assert partial["outstanding_cents"] == 7000, "exactly what June still counts"
    assert partial["fully_refunded"] is False
    assert partial["charge_date"] == "2031-06-12" and partial["credit_date"] == "2031-07-08"
    full = by_charge[seeded["charge_full"]]
    assert full["outstanding_cents"] == 0 and full["fully_refunded"] is True


# --------------------------------------------------- several credits, one charge

def test_two_credits_on_one_charge_are_each_counted_once(test_client):
    """The sum must follow the credits pointing AT the charge, not its own pointer.

    A charge names only one of its credits in ``refund_match_id``; reading the sum
    from that side would either miss the second credit or, if applied per credit,
    count the charge twice.
    """
    import asyncio

    from app.database import get_db

    async def seed():
        db = await get_db()
        try:
            charge = await _insert(db, amount=-10000, date="2031-11-04",
                                   category="Shopping", entity="expense", label="multi charge")
            first = await _insert(db, amount=2000, date="2031-12-02",
                                  category="Shopping", entity="income", label="multi credit a")
            second = await _insert(db, amount=3000, date="2031-12-09",
                                   category="Shopping", entity="income", label="multi credit b")
            await _pair(db, charge, first, partial=True)
            # The second credit points at the same charge; the charge still names
            # the first. This is what a re-detect can leave behind.
            await db.execute(
                "UPDATE transactions SET refund_match_id=?, refund_role='credit', refund_partial=1 WHERE id=?",
                (charge, second),
            )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(seed())
    data = _dashboard(test_client, "2031-11-01", "2031-12-31")
    assert data["totals"]["total_expense_cents"] == 5000, "$100 spent, $50 back"
    assert data["totals"]["total_income_cents"] == 0
    months = _months(data)
    assert months["2031-11"]["expense_cents"] == 5000
    assert months["2031-12"]["income_cents"] == 0


# ---------------------------------------------------- the arithmetic on its own

def test_effective_amount_helper_matches_the_documented_arithmetic():
    assert effective_amount_cents(-4600) == -4600, "an unpaired row is itself"
    assert effective_amount_cents(4600) == 4600
    assert effective_amount_cents(4600, "credit", 0) == 0, "the credit is applied at its charge"
    assert effective_amount_cents(-4600, "charge", 4600) == 0, "fully refunded is no spend"
    assert effective_amount_cents(-10000, "charge", 3000) == -7000, "partial leaves the rest"
    assert effective_amount_cents(-10000, "charge", 0) == -10000


def test_the_drill_down_net_matches_the_dashboard_breakdown(seeded, test_client):
    """The drill-down must use the SAME arithmetic as the number it drills into.

    When refund attribution was added to ``/api/dashboard`` but not to
    ``/api/dashboard/category-detail``, the two silently disagreed on live data:
    Shopping read $12,532.04 on the breakdown and $12,891.06 in the drill-down — a
    $359.02 gap that was exactly the refunds whose credit lands in another month.
    Two endpoints answering one question differently is worse than either answer
    alone, because nothing tells you which is lying.
    """
    board = _dashboard(test_client)
    checked = 0
    for cat in board["by_category"]:
        path = cat["category_path"]
        detail = test_client.get(
            f"/api/dashboard/category-detail?path={path}"
            f"&start_date={START}&end_date={END}&page_size=1")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        expected = cat["expense_cents"] or cat["income_cents"]
        assert abs(body["net_cents"]) == expected, (
            f"{path}: breakdown says {expected}, drill-down says "
            f"{abs(body['net_cents'])} (raw {abs(body['raw_net_cents'])})")
        checked += 1
    assert checked, "the fixture must produce at least one category to compare"


def test_the_drill_down_explains_a_moved_number(seeded, test_client):
    """A total that moved must be explainable, not merely correct.

    Queried over JUNE ALONE — the month of the charge — because that is the case
    the user described: "the expense made in June doesn't count, it was refunded in
    July". Over a range containing both months the raw sum already nets to zero, so
    there is nothing moved to explain.
    """
    detail = test_client.get(
        "/api/dashboard/category-detail?path=Home Improvement"
        "&start_date=2031-06-01&end_date=2031-06-30&page_size=50")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    # June still lists the purchase, but it no longer counts: the July refund is
    # applied here, at the charge.
    assert body["raw_net_cents"] == CHARGE_FULL, body
    assert body["net_cents"] == 0, body
    by_id = {t["id"]: t for t in body["transactions"]}
    charge = by_id.get(seeded["charge_full"])
    assert charge is not None, sorted(by_id)
    assert charge["effective_amount_cents"] == 0, charge
    assert charge["amount_cents"] == CHARGE_FULL, "the real amount is still reported"
    # The credit is in July, so it is not in this window at all.
    assert seeded["credit_full"] not in by_id


def test_a_partial_refund_leaves_the_outstanding_amount_in_the_charge_month_drill_down(
        seeded, test_client):
    """$100 spent in June, $30 back in July -> June's drill-down nets $70."""
    detail = test_client.get(
        "/api/dashboard/category-detail?path=Shopping"
        "&start_date=2031-06-01&end_date=2031-06-30&page_size=50")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["raw_net_cents"] == CHARGE_PARTIAL, body
    assert body["net_cents"] == CHARGE_PARTIAL + CREDIT_PARTIAL, body
