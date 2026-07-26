"""A fully-split charge must appear in the transactions list ONCE, as its items.

The bug, in the user's words: "those individual items exist by themselves AND as
part of this bigger combined item. So really the combined item should go away."

Live shape (snapshot of the real DB): txn 8759, 2026-02-12, −$38.15,
``merchant='Amazon'``, already neutralised to ``Transfer``, with three children
(−$10.74 + −$8.11 + −$19.30) carrying the real per-product categories and summing
to exactly −$38.15. The combined charge was still listed as a peer row, so the
same $38.15 was on screen twice and the row that looked most obviously
recategorizable was the one row that could only ever refuse (409).

Totals are the reason this is delicate, and there are two different kinds:

* what the DASHBOARD counts — unchanged by construction, because a split parent
  is a ``Transfer``/``entity_type='transfer'`` row and those are excluded from
  every dashboard total already. Asserted here anyway: the ledger holds 306
  fully-split parents and if one were ever left on a spending category, hiding it
  WOULD move a real total, and this test would say so.
* the list's own gross in/out line, which summed every listed row including the
  parent AND its children. That figure changes, and the change is the removal of
  a double count.

The parent is not deleted. It is the bank's real charge, the anchor for refund
pairing and the Amazon item export, and it stays reachable through
``?include_split_parents=true`` and through the ``split_parent_*`` columns each
item row now carries.

Two shapes must NOT be hidden, and both exist in the live data:

* a PARTIAL split, where the children do not account for the whole charge —
  hiding it would delete the residual from every count (0 such rows in the
  snapshot today, so the fixture builds one);
* a one-way TRANSFER PAIR, where the counterpart points at this row and nobody
  points back. 37 of those exist, and they look like a parent with one child
  under any test that only asks "does something link to me".
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

_INSERTED = []

# Far enough from every other module's fixtures that the date window below
# selects only these rows.
DAY = "2031-03-11"
WINDOW = {"start_date": "2031-03-01", "end_date": "2031-03-31"}


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("listing transactions must never call an LLM")

    monkeypatch.setattr("app.routers.transactions.call_llm", explode)


@pytest.fixture(autouse=True)
def clean_up():
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


async def _insert(db, **kw):
    cursor = await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, category_path, entity_type, business_context,
            linked_transaction_id, categorization_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            # 'plaid' because the list endpoint is scoped to the live source and
            # a test-only platform is filtered out of every default view. The 2031
            # dates keep these rows out of every other module's period windows.
            kw.get("platform", "plaid"),
            "listing.csv",
            kw.get("account_id", "card-0001"),
            kw.get("date", DAY),
            kw["amount"],
            kw.get("merchant", ""),
            kw.get("description", ""),
            kw.get("category"),
            kw.get("entity_type", "expense"),
            "personal",
            kw.get("linked"),
            kw.get("reason"),
        ),
    )
    _INSERTED.append(cursor.lastrowid)
    return cursor.lastrowid


async def seed_ledger():
    """The three shapes, all on one day: full split, partial split, transfer pair.

    Amounts mirror the live rows this fixes — the full split is txn 8759's
    −$38.15 broken into −$10.74 / −$8.11 / −$19.30.
    """
    from app.database import get_db

    db = await get_db()
    try:
        full = await _insert(
            db, merchant="Amazon", description="AMAZON MKTPL*TESTFULL", amount=-3815,
            category="Transfer", entity_type="transfer",
            reason="Split into 3 Amazon line items",
        )
        children = []
        for amount, name, category in (
            (-1074, "Amazon: A-Premium Engine Air Filter", "Car"),
            (-811, "Amazon: Genuine Toyota Oil Filter", "Car"),
            (-1930, "Amazon: CERAKOTE Headlight Restoration Kit", "Car"),
        ):
            children.append(await _insert(
                db, merchant=name, description=name[8:], amount=amount,
                category=category, linked=full,
                reason=f"Amazon item from split of #{full}",
            ))

        # Partial split: −$50.00 charge, one −$20.00 item. $30.00 is unaccounted
        # for and has nowhere else to be counted.
        partial = await _insert(
            db, merchant="Amazon", description="AMAZON MKTPL*TESTPART", amount=-5000,
            category="Transfer", entity_type="transfer",
            reason="Split into 1 Amazon line item",
        )
        partial_child = await _insert(
            db, merchant="Amazon: Half Told Widget", description="Half Told Widget",
            amount=-2000, category="Shopping", linked=partial,
            reason=f"Amazon item from split of #{partial}",
        )

        # A transfer pair linked in ONE direction only: $500 out of checking, $500
        # into savings, and only the inflow got the link written.
        out = await _insert(
            db, merchant="Account Transfer", amount=-50000,
            category="Transfer", entity_type="transfer", account_id="chk-0001",
        )
        into = await _insert(
            db, merchant="Account Transfer", amount=50000,
            category="Transfer", entity_type="transfer", account_id="sav-0002",
            linked=out,
        )

        ordinary = await _insert(
            db, merchant="Zorptek Listing Supply", amount=-1234,
            category="Shopping",
        )
        await db.commit()
        return {
            "full": full, "children": children,
            "partial": partial, "partial_child": partial_child,
            "transfer_out": out, "transfer_in": into,
            "ordinary": ordinary,
        }
    finally:
        await db.close()


def listing(test_client, **extra):
    params = {"page_size": 1000, **WINDOW, **extra}
    resp = test_client.get("/api/transactions", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# the defect
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_a_fully_split_charge_is_listed_as_its_items_not_twice(test_client):
    ids = await seed_ledger()
    data = listing(test_client)
    rows = {t["id"]: t for t in data["transactions"]}

    assert ids["full"] not in rows, (
        "the combined charge must not be a peer row of its own items — that is the "
        "same money on screen twice")
    for child in ids["children"]:
        assert child in rows, "the item rows ARE the transaction now"

    # ...and the count matches what came back, so pagination cannot claim rows the
    # list will not show.
    assert data["total"] == len(data["transactions"])


@pytest.mark.asyncio
async def test_hiding_the_parent_does_not_change_what_is_counted(test_client):
    """The money invariant: exactly the double count disappears, nothing else."""
    ids = await seed_ledger()
    default = listing(test_client)
    audit = listing(test_client, include_split_parents="true")

    hidden = {t["id"] for t in audit["transactions"]} - {
        t["id"] for t in default["transactions"]}
    assert hidden == {ids["full"]}, (
        "only the fully-covered parent is hidden — not the partial split, not the "
        "one-way transfer pair")

    # The hidden row's own money equals its children's, so removing it from the
    # gross-out line removes a duplicate rather than a fact.
    parent = next(t for t in audit["transactions"] if t["id"] == ids["full"])
    child_sum = sum(t["amount_cents"] for t in audit["transactions"]
                    if t["id"] in ids["children"])
    assert child_sum == parent["amount_cents"] == -3815
    assert (audit["totals"]["expense_cents"] - default["totals"]["expense_cents"]
            == 3815)

    # And what the dashboard counts is untouched, because a split parent is a
    # transfer row and transfers are already excluded from every total there.
    countable = [t for t in audit["transactions"]
                 if t["id"] == ids["full"]
                 and (t["entity_type"] or "") not in ("transfer", "excluded",
                                                      "asset_conversion")]
    assert countable == [], "a hidden parent must contribute nothing to any total"


@pytest.mark.asyncio
async def test_a_partial_split_stays_listed_so_the_residual_is_still_counted(test_client):
    """−$50 charge, −$20 of items: the missing $30 has nowhere else to live."""
    ids = await seed_ledger()
    data = listing(test_client)
    rows = {t["id"]: t for t in data["transactions"]}

    assert ids["partial"] in rows, (
        "a charge its items do not fully account for must stay listed — hiding it "
        "would silently drop the residual")
    assert ids["partial_child"] in rows
    assert rows[ids["partial"]]["split_child_count"] == 1


@pytest.mark.asyncio
async def test_a_one_way_transfer_pair_is_not_a_split(test_client):
    """37 live rows have a counterpart linked in one direction only.

    Their "child" sums to the NEGATION of the parent, not to the parent, so an
    equality test separates them from splits with no sign heuristics. Reading them
    as splits would delete a real transfer from the list.
    """
    ids = await seed_ledger()
    rows = {t["id"]: t for t in listing(test_client)["transactions"]}

    assert ids["transfer_out"] in rows
    assert ids["transfer_in"] in rows
    assert rows[ids["transfer_out"]]["split_parent_id"] is None
    assert rows[ids["transfer_in"]]["split_parent_id"] is None


@pytest.mark.asyncio
async def test_an_item_row_carries_the_charge_it_belongs_to(test_client):
    """Reachability: the combined charge is not listed, so the items point at it."""
    ids = await seed_ledger()
    rows = {t["id"]: t for t in listing(test_client)["transactions"]}

    for child in ids["children"]:
        row = rows[child]
        assert row["split_parent_id"] == ids["full"]
        assert row["split_parent_amount_cents"] == -3815
        assert row["split_parent_merchant"] == "Amazon"
        assert row["split_sibling_count"] == 3
        assert row["split_child_count"] == 0

    ordinary = rows[ids["ordinary"]]
    assert ordinary["split_parent_id"] is None
    assert ordinary["split_sibling_count"] == 0

    # And the drill-down that chip opens names the charge, not just its amount.
    detail = test_client.get(f"/api/transactions/{ids['full']}/split-items").json()
    assert detail["parent_amount_cents"] == -3815
    assert detail["parent_merchant"] == "Amazon"
    assert detail["parent_posted_date"] == DAY
    assert detail["item_count"] == 3
    assert detail["balanced"] is True
    assert {i["id"] for i in detail["items"]} == set(ids["children"])


@pytest.mark.asyncio
async def test_split_child_count_ignores_symmetric_links(test_client):
    """The client locks the category picker on ``split_child_count > 0``.

    A plain ``COUNT(*) WHERE linked_transaction_id = t.id`` also counts refund and
    transfer COUNTERPARTS — 1,078 rows in the live DB by that reading against 343
    genuine parents — which would freeze the picker on hundreds of ordinary rows.
    """
    from app.database import get_db

    ids = await seed_ledger()
    db = await get_db()
    try:
        purchase = await _insert(db, merchant="Zymbol Listing", amount=-5000,
                                 category="Shopping")
        refund = await _insert(db, merchant="Zymbol Listing", amount=5000,
                               category="Shopping", entity_type="income",
                               linked=purchase)
        await db.execute("UPDATE transactions SET linked_transaction_id=? WHERE id=?",
                         (refund, purchase))
        await db.commit()
    finally:
        await db.close()

    rows = {t["id"]: t for t in listing(test_client)["transactions"]}
    assert rows[purchase]["split_child_count"] == 0, "a refund pair is not a split"
    assert rows[refund]["split_child_count"] == 0
    assert rows[ids["children"][0]]["split_child_count"] == 0
    assert rows[ids["partial"]]["split_child_count"] == 1


@pytest.mark.asyncio
async def test_the_hidden_parent_is_still_editable_where_it_matters(test_client):
    """Hiding it from the list must not break the escape hatch or the guard."""
    ids = await seed_ledger()

    # The guard still refuses the double count...
    refused = test_client.patch(f"/api/transactions/{ids['full']}",
                                json={"category_path": "Groceries"})
    assert refused.status_code == 409, refused.text

    # ...and 'Excluded' still works, which is what the refusal tells you to do.
    ok = test_client.patch(f"/api/transactions/{ids['full']}",
                           json={"category_path": "Excluded"})
    assert ok.status_code == 200, ok.text

    # Still hidden afterwards: 'Excluded' neutralises just as 'Transfer' did.
    assert ids["full"] not in {t["id"] for t in listing(test_client)["transactions"]}
    # And each item row is individually categorizable, one at a time.
    for child, category in zip(ids["children"], ("Car", "Housing", "Shopping")):
        resp = test_client.patch(f"/api/transactions/{child}",
                                 json={"category_path": category})
        assert resp.status_code == 200, resp.text
        assert resp.json()["after"]["category_path"] == category
