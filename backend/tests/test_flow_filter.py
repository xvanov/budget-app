"""The Income / Expense / Transfer filters on the Transactions tab.

Regression tests for a filter that returned an empty table. The chips used to send
``category_path=Income`` and the endpoint prefix-matched it as
``category_path LIKE 'Income%'``. That worked under the old hierarchical scheme
where every path started with ``Income > `` / ``Expense > ``; once categories
became flat names (``Groceries``, ``Salary``) nothing matched either chip, so both
showed nothing at all.

In the flat model the question is about the SIGN of the amount, and the only
exceptions are the buckets whose whole purpose is to not be income or spending:
the transfer family and ``Excluded``.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

MARK = "Flowtest"
_INSERTED = []


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


async def _seed():
    """One row of each shape, all on one distinctive merchant and one date."""
    from app.database import get_db
    db = await get_db()
    try:
        rows = [
            # (label, amount, category, entity_type)
            ("inflow", 5000, "Salary", "income"),
            ("inflow_refund", 1500, "Groceries", "income"),
            ("outflow", -2500, "Groceries", "expense"),
            ("outflow_other", -700, "Other", "expense"),
            ("transfer_out", -9000, "Transfer", "transfer"),
            ("card_payment", -4000, "Credit-card Payment", "transfer"),
            ("excluded_out", -1200, "Excluded", "excluded"),
            ("uncategorized_out", -300, None, "expense"),
        ]
        ids = {}
        for label, amount, category, etype in rows:
            cursor = await db.execute(
                """INSERT INTO transactions
                   (source_platform, source_file, account_id, posted_date, amount_cents,
                    merchant, description, category_path, entity_type, business_context)
                   VALUES ('plaid','flow.csv','card-0001','2026-04-15',?,?,?,?,?,'personal')""",
                (amount, f"{MARK} {label}", f"{MARK} {label}", category, etype),
            )
            ids[label] = cursor.lastrowid
            _INSERTED.append(cursor.lastrowid)
        await db.commit()
        return ids
    finally:
        await db.close()


def _fetch(test_client, **params):
    qs = {"search": MARK, "start_date": "2026-04-01", "end_date": "2026-04-30",
          "page_size": "100", **params}
    resp = test_client.get("/api/transactions", params=qs)
    assert resp.status_code == 200, resp.text
    return {t["id"] for t in resp.json()["transactions"]}


@pytest.mark.asyncio
async def test_income_is_every_positive_row_regardless_of_category(test_client):
    """Including a refund sitting on a spending category — it is still money in."""
    ids = await _seed()
    got = _fetch(test_client, flow="income")
    assert ids["inflow"] in got
    assert ids["inflow_refund"] in got, "a positive row on Groceries is still an inflow"
    assert ids["outflow"] not in got
    assert ids["transfer_out"] not in got


@pytest.mark.asyncio
async def test_expense_is_every_negative_row_except_the_neutral_buckets(test_client):
    ids = await _seed()
    got = _fetch(test_client, flow="expense")
    assert ids["outflow"] in got
    assert ids["outflow_other"] in got
    assert ids["uncategorized_out"] in got, "an uncategorized outflow is still spending"
    assert ids["inflow"] not in got
    # Neither of these is spending: one is internal movement, one is opted out.
    assert ids["transfer_out"] not in got
    assert ids["card_payment"] not in got
    assert ids["excluded_out"] not in got


@pytest.mark.asyncio
async def test_transfer_is_the_transfer_family(test_client):
    ids = await _seed()
    got = _fetch(test_client, flow="transfer")
    assert ids["transfer_out"] in got
    assert ids["card_payment"] in got
    assert ids["outflow"] not in got
    assert ids["excluded_out"] not in got


@pytest.mark.asyncio
async def test_income_and_expense_partition_everything_that_counts(test_client):
    """Nothing that counts may be in both, and nothing may fall through the gap."""
    ids = await _seed()
    income = _fetch(test_client, flow="income")
    expense = _fetch(test_client, flow="expense")
    transfer = _fetch(test_client, flow="transfer")
    excluded = _fetch(test_client, flow="excluded")
    seeded = set(ids.values())

    assert not (income & expense), "a row cannot be both income and expense"
    covered = (income | expense | transfer | excluded) & seeded
    assert covered == seeded, f"rows in no bucket at all: {seeded - covered}"


@pytest.mark.asyncio
async def test_no_flow_filter_returns_everything(test_client):
    ids = await _seed()
    assert set(ids.values()) <= _fetch(test_client)


@pytest.mark.asyncio
async def test_an_unknown_flow_is_rejected_not_silently_ignored(test_client):
    """Silently ignoring it would show an unfiltered table that looks filtered."""
    await _seed()
    resp = test_client.get("/api/transactions", params={"flow": "sideways"})
    assert resp.status_code == 400
    assert "flow must be" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_category_path_is_an_exact_match_not_a_prefix(test_client):
    """'Other' must not drag in 'Other Income' — they are different categories."""
    ids = await _seed()
    from app.database import get_db
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, category_path, entity_type, business_context)
               VALUES ('plaid','flow.csv','card-0001','2026-04-15',4200,?,?,
                       'Other Income','income','personal')""",
            (f"{MARK} other_income", f"{MARK} other_income"),
        )
        other_income_id = cursor.lastrowid
        _INSERTED.append(other_income_id)
        await db.commit()
    finally:
        await db.close()

    got = _fetch(test_client, category_path="Other")
    assert ids["outflow_other"] in got
    assert other_income_id not in got, "'Other' must not match 'Other Income'"
