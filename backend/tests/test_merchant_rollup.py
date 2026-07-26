"""Spending by vendor: the breakdown must add up to the dashboard, exactly.

The whole feature is only worth having if the merchant total and the category
total are the same money counted two ways. Everything here is aimed at that one
identity and at the four ways this data set can break it:

1. **CSV/Plaid twins.** Every live row also exists as a superseded CSV row, so an
   unscoped SUM doubles it.
2. **Amazon split parents.** A combined charge whose items were split into child
   rows must not be counted alongside them. The fixture leaves the parent on
   ``entity_type='expense'`` ON PURPOSE — that is the state the live snapshot's
   parents would be in if the flip to ``'transfer'`` were ever missed, and this
   endpoint has to be immune to it on its own.
3. **Refunds.** A positive row on a spending category reduces that category; it is
   never income, and never an extra vendor.
4. **Rows that name no payee.** They are real spending and must land in an
   explicit bucket, not be dropped — otherwise the breakdown silently stops
   adding up.

Nothing here calls an LLM: the rollup is pure SQL plus ``merchant_key``, and the
autouse fixture below makes a regression that introduces a model call fail loudly
rather than hang (see AGENTS.md).
"""
import os
import sqlite3

import aiosqlite
import pytest
import pytest_asyncio

from app.routers import merchants as merchants_router
from app.routers.transactions import get_dashboard
from app.services import merchant_rollup as roll
from app.services.merchant_key import merchant_key


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the merchant rollup must never call an LLM")

    monkeypatch.setattr("app.llm.call_llm", explode)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    """An isolated DB. The session DB is shared by other modules; these rows are
    engineered to sit on both sides of every total and would break them."""
    path = str(tmp_path / "merchants.db")
    monkeypatch.setenv("DB_PATH", path)
    import app.database as database

    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    yield conn
    await conn.close()


async def add(db, *, merchant="", description="", amount, date, category="Shopping",
              entity="expense", platform="plaid", linked=None, refund_role=None,
              refund_match_id=None):
    cursor = await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, normalized_description, category_path, entity_type,
            linked_transaction_id, refund_role, refund_match_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (platform, "f.csv", "card-0001", date, amount, merchant, description,
         (description or "").lower(), category, entity, linked, refund_role,
         refund_match_id),
    )
    await db.commit()
    return cursor.lastrowid


async def mark_split_child(db, child_id):
    """Register ``child_id`` as an Amazon split child, the way the splitter does.

    ``amazon_items.split_transaction_id`` names the CHILD; the child's
    ``linked_transaction_id`` names its parent. That direction is the reliable one
    — see ``merchant_rollup._SPLIT_PARENT_SQL``.
    """
    await db.execute(
        """INSERT INTO amazon_items (order_id, owner, product_name, total_cents,
                                     split_transaction_id, fingerprint)
           VALUES (?,?,?,?,?,?)""",
        (f"order-{child_id}", "kalin", "item", 100, child_id, f"fp-{child_id}"),
    )
    await db.commit()


async def seed(db):
    """One window of data containing every shape that can break the identity."""
    ids = {}

    # ── Plain vendors, several raw spellings of the same payee ────────────────
    ids["costco1"] = await add(db, merchant="Costco", amount=-5000, date="2026-03-02",
                               category="Groceries")
    # The CSV shape: store number then city. merchant_key reads the store number
    # as the start of the location tail, so this keys the same as plain "Costco".
    ids["costco2"] = await add(db, merchant="COSTCO 00291 DURHAM NC", amount=-3000,
                               date="2026-03-14", category="Groceries")
    # The CSV twin of costco1: same money, superseded source. scope=plaid must
    # ignore it; scope=all must count it once.
    ids["costco_csv"] = await add(db, merchant="Costco", amount=-5000, date="2026-03-02",
                                  category="Groceries", platform="chase")

    # Apple, in the three spellings that key differently. The dominance rule folds
    # the rare ones into the dominant one; ten bare rows is what makes it dominant.
    for i in range(10):
        ids[f"apple{i}"] = await add(db, merchant="Apple", amount=-999,
                                     date=f"2026-03-{i + 1:02d}", category="Subscriptions")
    ids["apple_store"] = await add(db, merchant="Apple Store", amount=-12000,
                                   date="2026-03-20", category="Shopping")

    # ── Amazon: a split parent plus its item children, and a bare charge ──────
    # The parent is flipped to 'transfer' here, which is the state the splitter
    # leaves it in and the state the live snapshot's 343 parents are actually in.
    # ``test_split_parent_is_excluded_...`` flips it BACK to 'expense' to build the
    # trap and prove this endpoint does not need that flip to be correct.
    ids["amz_parent"] = await add(db, merchant="Amazon", description="AMAZON MKTPL*RL5QE5SU3",
                                  amount=-9000, date="2026-03-05", category="Transfer",
                                  entity="transfer")
    for label, cents, cat in (("Cat Litter", -4000, "Pets"),
                              ("USB-C Cable 240W Fast Charging", -3000, "Shopping"),
                              ("Saline Nasal Spray", -2000, "Health")):
        cid = await add(db, merchant=f"Amazon: {label}", description=label, amount=cents,
                        date="2026-03-05", category=cat, linked=ids["amz_parent"])
        ids[f"amz_child_{cat}"] = cid
        await mark_split_child(db, cid)
    ids["amz_bare"] = await add(db, merchant="Amazon",
                                description="AMAZON MKTPLACE PMTS", amount=-2500,
                                date="2026-03-11", category="Shopping")

    # ── A refund inside the window: must net, not add ─────────────────────────
    ids["shein_charge"] = await add(db, merchant="Shein", amount=-8000, date="2026-03-06",
                                    category="Shopping")
    ids["shein_credit"] = await add(db, merchant="SHEIN.COM DE 03/09", amount=3000,
                                    date="2026-03-09", category="Shopping")
    await db.execute(
        "UPDATE transactions SET refund_role='charge', refund_match_id=? WHERE id=?",
        (ids["shein_credit"], ids["shein_charge"]))
    await db.execute(
        "UPDATE transactions SET refund_role='credit', refund_match_id=? WHERE id=?",
        (ids["shein_charge"], ids["shein_credit"]))
    await db.commit()

    # ── The payee only in the description (a payment rail in `merchant`) ──────
    ids["zelle"] = await add(db, merchant="Zelle",
                             description="Zelle payment to Marisol Quintero Confirmation# ab12cd",
                             amount=-40000, date="2026-03-08", category="Housing")

    # ── A row that names nobody: real spending, no identifiable payee ─────────
    ids["nameless"] = await add(db, merchant="ONLINE TRANSFER TO CHK",
                                description="Online Transfer to CHK ...0001",
                                amount=-1500, date="2026-03-10", category="Shopping")

    # ── Rows that are not spending at all ────────────────────────────────────
    ids["transfer"] = await add(db, merchant="Chase Payment", amount=-90000,
                               date="2026-03-12", category="Credit-card Payment",
                               entity="transfer")
    ids["excluded"] = await add(db, merchant="Fronted Bill", amount=-7700,
                                date="2026-03-13", category="Excluded", entity="excluded")
    ids["uncat"] = await add(db, merchant="Mystery Co", amount=-6600, date="2026-03-15",
                             category="Uncategorized")
    ids["salary"] = await add(db, merchant="Acme Payroll", amount=500000,
                              date="2026-03-01", category="Salary", entity="income")
    return ids


async def call_list(db, **params):
    params.setdefault("scope", "plaid")
    params.setdefault("sort", "spend")
    params.setdefault("limit", 500)
    params.setdefault("offset", 0)
    params.setdefault("granularity", "auto")
    params.setdefault("category", None)
    params.setdefault("merchant", None)
    params.setdefault("q", None)
    params.setdefault("start_date", None)
    params.setdefault("end_date", None)
    return await merchants_router.get_merchants(**params)


def by_key(payload):
    return {m["merchant_key"]: m for m in payload["merchants"]}


# ───── (a) the identity ─────

@pytest.mark.asyncio
async def test_merchant_total_equals_dashboard_total_spend(db):
    """Same money, two groupings. Zero tolerance — this is a deterministic invariant."""
    await seed(db)
    for scope in ("plaid", "all"):
        for start, end in (("2026-03-01", "2026-03-31"),
                           ("2026-03-01", "2026-03-10"),
                           (None, None)):
            dash = await get_dashboard(start_date=start, end_date=end, scope=scope)
            got = await call_list(db, start_date=start, end_date=end, scope=scope)
            assert got["totals"]["spend_cents"] == dash["totals"]["total_expense_cents"], (
                f"scope={scope} window={start}..{end}")
            # ...and the rows themselves add up to that total, so no merchant is
            # missing from the ranking.
            assert sum(m["spend_cents"] for m in got["merchants"]) == \
                got["totals"]["spend_cents"]
            # ...as do the category facets and the time series.
            assert sum(c["spend_cents"] for c in got["by_category"]) == \
                got["totals"]["spend_cents"]
            assert sum(b["spend_cents"] for b in got["series"]) == \
                got["totals"]["spend_cents"]


@pytest.mark.asyncio
async def test_csv_plaid_twin_is_not_double_counted(db):
    """The Costco twin exists in both sources; each scope counts it exactly once."""
    ids = await seed(db)
    plaid = by_key(await call_list(db, scope="plaid"))["costco"]
    everything = by_key(await call_list(db, scope="all"))["costco"]
    assert plaid["transaction_count"] == 2          # costco1 + costco2
    assert plaid["spend_cents"] == 8000
    assert everything["transaction_count"] == 3     # + the CSV twin
    assert everything["spend_cents"] == 13000
    assert ids["costco_csv"]  # the twin really is in the DB


@pytest.mark.asyncio
async def test_split_parent_is_excluded_even_when_left_on_an_expense_category(db):
    """The combined charge must not be counted next to its item rows.

    The trap is the parent sitting on a spending category — the state a missed
    ``entity_type`` flip leaves it in. Without the exclusion the parent (-$90) and
    its three children (-$40/-$30/-$20) would both land under Amazon, making it
    $205 instead of $115 ($90 of items + the $25 bare charge). The dashboard, which
    has no such exclusion yet, does count it: the gap between the two totals is
    exactly the parent, and it is reported rather than hidden.
    """
    ids = await seed(db)
    await db.execute("UPDATE transactions SET entity_type='expense', category_path='Shopping' "
                     "WHERE id=?", (ids["amz_parent"],))
    await db.commit()

    got = await call_list(db, start_date="2026-03-01", end_date="2026-03-31")
    amazon = by_key(got)["amazon"]
    # 3 children ($90) + the bare charge ($25). The parent's $90 is counted once,
    # through its children, never twice.
    assert amazon["spend_cents"] == 9000 + 2500
    assert amazon["transaction_count"] == 4
    assert got["diagnostics"]["split_parent_rows_excluded"] == 1
    assert got["diagnostics"]["split_parent_cents_excluded"] == -9000

    dash = await get_dashboard(start_date="2026-03-01", end_date="2026-03-31", scope="plaid")
    assert dash["totals"]["total_expense_cents"] - got["totals"]["spend_cents"] == 9000

    detail = await merchants_router.get_merchant_detail(
        merchant_key="amazon", start_date="2026-03-01", end_date="2026-03-31",
        scope="plaid", category=None, granularity="auto", page=1, page_size=100,
        sort="posted_date", dir="desc")
    assert ids["amz_parent"] not in {t["id"] for t in detail["transactions"]}
    assert detail["summary"]["spend_cents"] == amazon["spend_cents"]


# ───── (b) grouping really collapses the variants ─────

@pytest.mark.asyncio
async def test_grouping_collapses_the_raw_variants_of_one_payee(db):
    """Four spellings of Amazon, one vendor; two of Costco, one vendor."""
    await seed(db)
    got = by_key(await call_list(db))

    amazon = got["amazon"]
    assert amazon["label"] == "Amazon"
    raw = {v["merchant"] for v in amazon["variants"]}
    # The bare Plaid spelling and the per-item rows this app wrote itself.
    assert "Amazon" in raw
    assert any(r.startswith("Amazon: ") for r in raw)
    assert amazon["variant_count"] >= 4
    # Keyed as-is, each "Amazon: <product>" is its own merchant. Prove the raw key
    # really does differ, so the collapse is doing work rather than being a no-op.
    assert merchant_key("Amazon: Cat Litter", "Cat Litter") != "amazon"
    assert roll.row_merchant_key("Amazon: Cat Litter", "Cat Litter") == "amazon"

    assert got["costco"]["variant_count"] == 2   # 'Costco' + 'COSTCO 00291 DURHAM NC'
    assert got["costco"]["label"] == "Costco"

    # A domain suffix is the same problem as an item suffix: 'SHEIN.COM DE 03/09'
    # and 'Shein' are one shop, and keyed raw they are two.
    assert merchant_key("SHEIN.COM DE 03/09", "") != "shein"
    assert roll.row_merchant_key("SHEIN.COM DE 03/09", "") == "shein"
    assert got["shein"]["variant_count"] == 2

    # Dominance consolidation: 'apple store' folds into the dominant 'apple'.
    assert "apple store" not in got
    assert set(got["apple"]["merged_keys"]) == {"apple", "apple store"}
    assert got["apple"]["spend_cents"] == 10 * 999 + 12000

    # The payee named only in the description is labelled after the PERSON, never
    # after the rail — "Zelle" identifies nobody.
    zelle = [m for m in got.values() if "quintero" in m["merchant_key"]]
    assert len(zelle) == 1
    assert "Zelle" not in zelle[0]["label"]


@pytest.mark.asyncio
async def test_a_generic_shared_word_does_not_absorb_distinct_vendors(db):
    """The dominance bar exists to stop this, and a loose rule really did do it.

    At 3 rows / 3x, live data folded "durham womens clinic" into "Durham" (a city)
    and "family dollar" into "Family". Two rows named X must not swallow X Y.
    """
    await add(db, merchant="Durham Taproom", amount=-1000, date="2026-03-02",
              category="Restaurants")
    await add(db, merchant="Durham Taproom", amount=-1000, date="2026-03-03",
              category="Restaurants")
    await add(db, merchant="Durham Womens Clinic", amount=-50000, date="2026-03-04",
              category="Health")
    got = by_key(await call_list(db))
    assert "durham taproom" in got
    assert "durham womens clinic" in got
    assert got["durham womens clinic"]["spend_cents"] == 50000


# ───── (c) filters compose ─────

@pytest.mark.asyncio
async def test_category_merchant_and_period_filters_are_cumulative(db):
    await seed(db)
    whole = await call_list(db, start_date="2026-03-01", end_date="2026-03-31")

    # Category only.
    groceries = await call_list(db, start_date="2026-03-01", end_date="2026-03-31",
                                category=["Groceries"])
    assert groceries["totals"]["spend_cents"] == 8000
    assert set(by_key(groceries)) == {"costco"}
    assert groceries["period_total_spend_cents"] == whole["totals"]["spend_cents"]

    # Category AND merchant.
    both = await call_list(db, start_date="2026-03-01", end_date="2026-03-31",
                           category=["Groceries"], merchant=["costco"])
    assert both["totals"]["spend_cents"] == 8000
    assert both["totals"]["transaction_count"] == 2
    # The ranking still lists every vendor in the category, so another can be picked.
    assert set(by_key(both)) == {"costco"}
    # ...and the category facets narrow to the vendor.
    assert [c["category_path"] for c in both["by_category"]] == ["Groceries"]

    # Same filter, narrower window: only the March 2 purchase survives.
    narrowed = await call_list(db, start_date="2026-03-01", end_date="2026-03-05",
                               category=["Groceries"], merchant=["costco"])
    assert narrowed["totals"]["spend_cents"] == 5000
    assert narrowed["totals"]["transaction_count"] == 1

    # A merchant that has nothing in the chosen category yields nothing, not
    # everything — the filters are ANDed.
    empty = await call_list(db, start_date="2026-03-01", end_date="2026-03-31",
                            category=["Groceries"], merchant=["amazon"])
    assert empty["totals"]["spend_cents"] == 0
    assert empty["totals"]["transaction_count"] == 0

    # Multi-select on both axes.
    multi = await call_list(db, start_date="2026-03-01", end_date="2026-03-31",
                            category=["Groceries", "Pets"], merchant=["costco", "amazon"])
    assert multi["totals"]["spend_cents"] == 8000 + 4000

    # An unknown category is a 400 with a sentence, not a silent empty result.
    with pytest.raises(Exception) as err:
        await call_list(db, category=["Nonexistent"])
    assert "matched exactly" in str(getattr(err.value, "detail", err.value))


@pytest.mark.asyncio
async def test_detail_selects_exactly_the_rows_the_list_counted(db):
    await seed(db)
    listed = by_key(await call_list(db, start_date="2026-03-01", end_date="2026-03-31",
                                    category=["Pets"]))["amazon"]
    detail = await merchants_router.get_merchant_detail(
        merchant_key="amazon", start_date="2026-03-01", end_date="2026-03-31",
        scope="plaid", category=["Pets"], granularity="auto", page=1, page_size=100,
        sort="abs_amount", dir="desc")
    assert detail["summary"]["spend_cents"] == listed["spend_cents"]
    assert detail["total"] == listed["transaction_count"] == 1
    assert {t["category_path"] for t in detail["transactions"]} == {"Pets"}


# ───── (d) refunds net ─────

@pytest.mark.asyncio
async def test_refund_reduces_the_vendor_instead_of_adding_to_it(db):
    """$80 charged, $30 back: the vendor is $50, and $30 is never income."""
    await seed(db)
    got = by_key(await call_list(db, start_date="2026-03-01", end_date="2026-03-31"))
    shein = got["shein"]
    assert shein["spend_cents"] == 5000
    assert shein["gross_spend_cents"] == 8000
    assert shein["refund_cents"] == 3000
    assert shein["transaction_count"] == 2
    # The credit does not become a merchant of its own, and does not show up as a
    # positive contribution anywhere.
    assert all(m["spend_cents"] <= 0 or "shein" in m["merchant_key"] or m["spend_cents"] > 0
               for m in got.values())
    assert shein["merchant_key"] == "shein"


@pytest.mark.asyncio
async def test_a_refund_whose_charge_is_in_an_earlier_window_is_booked_at_the_charge(db):
    """Same rule as the dashboard: the credit's own month shows no phantom income.

    $60 charged in February, $20 back in March. February must read $40 — what was
    genuinely spent — and March must read $0 for that vendor, not $20 of income.
    """
    charge = await add(db, merchant="Ross Stores", amount=-6000, date="2026-02-10",
                       category="Shopping")
    credit = await add(db, merchant="Ross Stores", amount=2000, date="2026-03-04",
                       category="Shopping")
    await db.execute("UPDATE transactions SET refund_role='charge', refund_match_id=? "
                     "WHERE id=?", (credit, charge))
    await db.execute("UPDATE transactions SET refund_role='credit', refund_match_id=? "
                     "WHERE id=?", (charge, credit))
    await db.commit()

    feb = await call_list(db, start_date="2026-02-01", end_date="2026-02-28")
    mar = await call_list(db, start_date="2026-03-01", end_date="2026-03-31")
    assert by_key(feb)["ross stores"]["spend_cents"] == 4000
    assert feb["totals"]["spend_cents"] == 4000
    # March: the vendor is present and reads $0 — not $20 of income, and not absent
    # (a vendor that vanishes is indistinguishable from a bug).
    assert by_key(mar)["ross stores"]["spend_cents"] == 0
    assert mar["totals"]["spend_cents"] == 0
    assert mar["totals"]["refund_cents"] == 2000
    assert mar["totals"]["refund_attribution_cents"] == 2000
    # ...and the dashboard says the same thing about both windows.
    for start, end, expected in (("2026-02-01", "2026-02-28", 4000),
                                 ("2026-03-01", "2026-03-31", 0)):
        dash = await get_dashboard(start_date=start, end_date=end, scope="plaid")
        assert dash["totals"]["total_expense_cents"] == expected
        assert dash["totals"]["total_income_cents"] == 0


# ───── (e) rows with no identifiable payee ─────

@pytest.mark.asyncio
async def test_unidentified_rows_are_bucketed_not_dropped(db):
    await seed(db)
    got = await call_list(db, start_date="2026-03-01", end_date="2026-03-31")
    keyed = by_key(got)
    assert roll.UNIDENTIFIED_KEY in keyed
    bucket = keyed[roll.UNIDENTIFIED_KEY]
    assert bucket["unidentified"] is True
    assert bucket["label"] == roll.UNIDENTIFIED_LABEL
    assert bucket["spend_cents"] == 1500
    assert got["unidentified"]["spend_cents"] == 1500
    # The bucket is part of the total, which is the point.
    assert sum(m["spend_cents"] for m in got["merchants"]) == got["totals"]["spend_cents"]


@pytest.mark.asyncio
async def test_an_empty_merchant_key_never_behaves_as_a_wildcard(db):
    """``merchant_key`` returns '' for a row naming nobody; '' must match nothing.

    A blank/absent parameter is "no filter"; the unidentified bucket is reachable
    only through the explicit sentinel.
    """
    await seed(db)
    unfiltered = await call_list(db, start_date="2026-03-01", end_date="2026-03-31")
    blank = await call_list(db, start_date="2026-03-01", end_date="2026-03-31",
                            merchant=[""])
    assert blank["totals"] == unfiltered["totals"]

    only = await call_list(db, start_date="2026-03-01", end_date="2026-03-31",
                           merchant=[roll.UNIDENTIFIED_FILTER_TOKEN])
    assert only["totals"]["spend_cents"] == 1500
    assert only["totals"]["transaction_count"] == 1
    assert only["totals"]["spend_cents"] < unfiltered["totals"]["spend_cents"]


# ───── non-spending rows, degenerate windows, capping ─────

@pytest.mark.asyncio
async def test_transfers_excluded_uncategorized_and_income_stay_out(db):
    await seed(db)
    got = await call_list(db, start_date="2026-03-01", end_date="2026-03-31")
    labels = {m["label"] for m in got["merchants"]}
    assert "Chase Payment" not in labels        # transfer
    assert "Fronted Bill" not in labels         # Excluded
    assert "Mystery Co" not in labels           # Uncategorized
    assert "Acme Payroll" not in labels         # income category
    assert "Salary" in got["diagnostics"]["non_spending_categories"]
    assert all(c["category_path"] != "Salary" for c in got["by_category"])


@pytest.mark.asyncio
async def test_empty_period_is_an_answer_not_an_error(db):
    await seed(db)
    got = await call_list(db, start_date="2019-01-01", end_date="2019-01-31")
    assert got["totals"]["spend_cents"] == 0
    assert got["merchants"] == []
    assert got["ranking"]["total_merchants"] == 0
    assert got["series"] == []
    with pytest.raises(Exception) as err:
        await merchants_router.get_merchant_detail(
            merchant_key="costco", start_date="2019-01-01", end_date="2019-01-31",
            scope="plaid", category=None, granularity="auto", page=1, page_size=10,
            sort="posted_date", dir="desc")
    assert "No spending for merchant" in str(getattr(err.value, "detail", err.value))


@pytest.mark.asyncio
async def test_capping_the_ranking_reports_what_it_hid(db):
    """Truncation is allowed; silent truncation is not."""
    await seed(db)
    full = await call_list(db, start_date="2026-03-01", end_date="2026-03-31")
    capped = await call_list(db, start_date="2026-03-01", end_date="2026-03-31", limit=2)
    assert capped["ranking"]["returned"] == 2
    assert capped["ranking"]["total_merchants"] == full["ranking"]["total_merchants"]
    assert capped["ranking"]["hidden_merchants"] == full["ranking"]["total_merchants"] - 2
    # Shown + hidden is the whole total, so the tail is accounted for rather than lost.
    assert sum(m["spend_cents"] for m in capped["merchants"]) \
        + capped["ranking"]["hidden_spend_cents"] == capped["totals"]["spend_cents"]


@pytest.mark.asyncio
async def test_series_fills_interior_gaps_and_trims_the_empty_ends(db):
    """A month with no spending is a real $0; empty months before any data are noise."""
    await add(db, merchant="Petco", amount=-1000, date="2026-01-15", category="Pets")
    await add(db, merchant="Petco", amount=-2000, date="2026-03-15", category="Pets")
    got = await call_list(db, start_date="2025-01-01", end_date="2026-06-30",
                          granularity="month")
    buckets = [b["bucket"] for b in got["series"]]
    assert buckets == ["2026-01", "2026-02", "2026-03"]
    assert [b["spend_cents"] for b in got["series"]] == [1000, 0, 2000]


@pytest.mark.asyncio
async def test_granularity_adapts_to_the_window(db):
    await seed(db)
    day = await call_list(db, start_date="2026-03-01", end_date="2026-03-20")
    assert day["period"]["granularity"] == "day"
    week = await call_list(db, start_date="2026-01-01", end_date="2026-06-01")
    assert week["period"]["granularity"] == "week"
    month = await call_list(db, start_date="2024-01-01", end_date="2026-06-01")
    assert month["period"]["granularity"] == "month"


# ───── the same identity, against the real snapshot ─────

_SNAPSHOT = os.environ.get("MERCHANT_SNAPSHOT_DB", "")


@pytest.mark.skipif(not _SNAPSHOT or not os.path.exists(_SNAPSHOT),
                    reason="set MERCHANT_SNAPSHOT_DB to a COPY of a real database")
@pytest.mark.asyncio
async def test_reconciles_against_a_real_snapshot(monkeypatch, tmp_path):
    """The identity, on real data with thousands of merchants.

    Synthetic fixtures cannot produce 545 vendors, ~1,000 ``Amazon: <product>``
    rows or 111 refund pairs, and those are exactly the conditions under which the
    grouping and the netting can drift. Point ``MERCHANT_SNAPSHOT_DB`` at a COPY of
    a real DB (never the live one) to run this.
    """
    import shutil

    import app.database as database

    copy = str(tmp_path / "snapshot.db")
    shutil.copy(_SNAPSHOT, copy)
    monkeypatch.setenv("DB_PATH", copy)
    monkeypatch.setattr(database, "DB_PATH", copy)

    windows = [("2026-07-01", "2026-07-25"), ("2026-06-01", "2026-06-30"),
               ("2026-01-01", "2026-07-25"), ("2024-07-01", "2026-07-25")]
    for scope in ("plaid", "all"):
        for start, end in windows:
            dash = await get_dashboard(start_date=start, end_date=end, scope=scope)
            got = await merchants_router.get_merchants(
                start_date=start, end_date=end, scope=scope, category=None,
                merchant=None, q=None, sort="spend", limit=500, offset=0,
                granularity="auto")
            assert got["totals"]["spend_cents"] == dash["totals"]["total_expense_cents"], \
                f"scope={scope} {start}..{end}"
            assert sum(m["spend_cents"] for m in got["merchants"]) \
                + got["ranking"]["hidden_spend_cents"] == got["totals"]["spend_cents"]
            assert sum(b["spend_cents"] for b in got["series"]) == got["totals"]["spend_cents"]

    # The variants of a payee that really does arrive under many raw strings.
    everything = await merchants_router.get_merchants(
        start_date="2024-07-01", end_date="2026-07-25", scope="plaid", category=None,
        merchant=None, q="amazon", sort="spend", limit=50, offset=0, granularity="auto")
    amazon = [m for m in everything["merchants"] if m["merchant_key"] == "amazon"]
    assert amazon, "the snapshot should have an Amazon vendor"
    assert amazon[0]["variant_count"] > 50, \
        "hundreds of raw 'Amazon: <product>' strings must collapse to one vendor"
    assert amazon[0]["transaction_count"] > 100

    # And the unidentified bucket is present and accounted for rather than dropped.
    con = sqlite3.connect(copy)
    try:
        assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] > 1000
    finally:
        con.close()
