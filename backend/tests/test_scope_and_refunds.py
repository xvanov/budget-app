"""Data scope + refund arithmetic.

Two owner directives are encoded here:
  1. Plaid is the live source; CSV rows are stored history and must not silently
     mix into default numbers.
  2. A return must REDUCE the category it reverses, not appear as income.
"""
import aiosqlite
import pytest
import pytest_asyncio

from app.services.scope import (
    EXCLUDED_ENTITY_TYPES,
    EXPENSE_SUM,
    INCOME_SUM,
    has_plaid_data,
    resolve_scope,
)
from app.services.sign_invariant import violates_sign_invariant


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    import app.database as database

    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "t.db"))
    await database.init_db()
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    conn.row_factory = aiosqlite.Row
    yield conn
    await conn.close()


async def add(db, *, platform, amount, entity="expense", category="Expense > Personal > Shopping",
              date="2026-03-01"):
    await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, normalized_description, category_path, entity_type)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (platform, "f", "card-0002", date, amount, "M", "D", "d", category, entity),
    )
    await db.commit()


async def totals(db, scope="auto"):
    scope_sql, _, effective, _note = await resolve_scope(db, scope)
    # Use the shared constant rather than a hardcoded list: this helper drifted
    # once already when 'excluded' was added to the app but not here.
    excluded = ",".join(f"'{e}'" for e in EXCLUDED_ENTITY_TYPES)
    cursor = await db.execute(
        f"""SELECT {INCOME_SUM} AS income, {EXPENSE_SUM} AS expense
            FROM transactions t
            WHERE (t.entity_type IS NULL OR t.entity_type NOT IN ({excluded}))
              {scope_sql}"""
    )
    row = await cursor.fetchone()
    return {"income": row["income"], "expense": row["expense"], "scope": effective}


# ------------------------------------------------------------------- scope

@pytest.mark.asyncio
async def test_default_scope_never_falls_back_to_the_csv_history(db):
    """No live data must mean an EMPTY view, not the old CSV numbers.

    An earlier version fell back to `all` so the app would not look blank before
    the first bank was linked. That silently put the CSV history back on screen —
    the exact thing the owner asked twice to stop seeing.
    """
    await add(db, platform="chase", amount=-1000)   # CSV history only
    assert await has_plaid_data(db) is False
    result = await totals(db, "auto")
    assert result["scope"] == "plaid"
    assert result["expense"] == 0, "CSV history must not appear by default"


@pytest.mark.asyncio
async def test_auto_scope_becomes_plaid_only_once_plaid_data_exists(db):
    await add(db, platform="chase", amount=-1000)   # CSV history
    await add(db, platform="plaid", amount=-2500)   # live
    result = await totals(db, "auto")
    assert result["scope"] == "plaid"
    assert result["expense"] == 2500, "CSV history must not mix into the live total"


@pytest.mark.asyncio
async def test_explicit_all_scope_reaches_the_csv_history(db):
    await add(db, platform="chase", amount=-1000)
    await add(db, platform="plaid", amount=-2500)
    result = await totals(db, "all")
    assert result["scope"] == "all"
    assert result["expense"] == 3500


@pytest.mark.asyncio
async def test_invalid_scope_degrades_to_auto(db):
    await add(db, platform="plaid", amount=-100)
    _sql, _params, effective, _note = await resolve_scope(db, "nonsense")
    assert effective == "plaid"


@pytest.mark.asyncio
async def test_scope_note_tells_the_user_why_the_view_is_empty(db):
    await add(db, platform="chase", amount=-100)
    _sql, _params, _effective, note = await resolve_scope(db, "auto")
    assert "nothing synced yet" in note.lower()
    assert "accounts & sync" in note.lower(), "the note must say where to fix it"


# ------------------------------------------------------------------ refunds

@pytest.mark.asyncio
async def test_refund_reduces_its_own_expense_category_instead_of_adding_income(db):
    """The core requirement: a returned item shows up as less spending."""
    await add(db, platform="plaid", amount=-6433)
    await add(db, platform="plaid", amount=6433, entity="refund")
    result = await totals(db, "plaid")
    assert result["expense"] == 0, "buying then returning nets to zero spend"
    assert result["income"] == 0, "a refund is not earnings"


@pytest.mark.asyncio
async def test_partial_refund_leaves_the_remaining_spend(db):
    await add(db, platform="plaid", amount=-10000)
    await add(db, platform="plaid", amount=2500, entity="refund")
    result = await totals(db, "plaid")
    assert result["expense"] == 7500
    assert result["income"] == 0


@pytest.mark.asyncio
async def test_real_income_still_counts_as_income(db):
    await add(db, platform="plaid", amount=500000, entity="income", category="Income > Software Job")
    result = await totals(db, "plaid")
    assert result["income"] == 500000
    assert result["expense"] == 0


@pytest.mark.asyncio
async def test_transfers_are_excluded_from_both_totals(db):
    await add(db, platform="plaid", amount=-82294, entity="transfer",
              category="Transfer > Credit-card Payment")
    result = await totals(db, "plaid")
    assert result["expense"] == 0, "paying the card is not spending — it would double-count"
    assert result["income"] == 0


@pytest.mark.asyncio
async def test_split_parent_is_excluded_so_items_are_not_double_counted(db):
    await add(db, platform="plaid", amount=-19519, entity="transfer",
              category="Transfer > Amazon Order (Split)")
    await add(db, platform="plaid", amount=-17199)
    await add(db, platform="plaid", amount=-2320)
    result = await totals(db, "plaid")
    assert result["expense"] == 19519, "children only, parent excluded"


def test_refund_entity_type_may_carry_an_expense_path_with_a_positive_amount():
    """This is the invariant exception that makes contra-expenses possible."""
    assert violates_sign_invariant("refund", 6433, "Expense > Personal > Shopping") is False
    # And the original protection is intact:
    assert violates_sign_invariant("expense", 6433, "Expense > Personal > Shopping") is True
    assert violates_sign_invariant("income", -100, "Income > Rentals") is True


# ------------------------------------- inflow-on-expense-category coercion

def test_merchant_credit_on_an_expense_category_becomes_a_refund():
    """An inflow on an expense category is a refund, not income or a dead row.

    On live data this path hit 35 rows — all genuine merchant credits (Amazon,
    Apple, SHEIN reversals, NordVPN). Coercing them to 'Uncategorized' threw away
    a correct category and stranded real money.
    """
    from app.services.sign_invariant import coerce_sign_safe

    cp, et, bc, note = coerce_sign_safe(
        "Expense > Personal > Shopping", "expense", 6771, "personal"
    )
    assert cp == "Expense > Personal > Shopping", "the category must survive"
    assert et == "refund"
    assert bc == "personal"
    assert "refund" in note.lower()
    # And the result must itself be invariant-clean.
    assert violates_sign_invariant(et, 6771, cp) is False


def test_inbound_rental_money_is_still_protected_as_income():
    """The original corruption: inbound Zelle rent tagged as a rental EXPENSE.

    That must become rental income — never a rent 'refund' reducing expenses.
    """
    from app.services.sign_invariant import coerce_sign_safe

    cp, et, bc, _note = coerce_sign_safe(
        "Expense > Rental > Rent", "expense", 550000, "rental"
    )
    assert cp == "Income > Rentals"
    assert et == "income"
    assert bc == "rental"


def test_outflow_tagged_as_income_is_still_rejected():
    from app.services.sign_invariant import coerce_sign_safe

    cp, et, _bc, note = coerce_sign_safe("Income > Rentals", "income", -5000, "rental")
    assert cp == "Uncategorized"
    assert et == "expense"
    assert "cannot be" in note


@pytest.mark.asyncio
async def test_manual_entries_are_live_data_not_history(db):
    """A hand-entered transaction must not vanish the moment it is created.

    Scoping to source_platform='plaid' alone hid every manual entry, so cash
    spending typed into "Add Transaction" disappeared from the list it was just
    added to. Manual rows are current, user-authored data; CSV rows are history.
    """
    await add(db, platform="manual", amount=-4200)
    await add(db, platform="chase", amount=-9900)   # CSV history
    result = await totals(db, "plaid")
    assert result["expense"] == 4200, "manual entry counts, CSV history does not"


# ------------------------------------------------------- the Excluded bucket

@pytest.mark.asyncio
async def test_excluded_category_leaves_every_total(db):
    """An excluded row must not count as spending OR income.

    This is the owner's opt-out for one-offs — a bill fronted for someone who
    paid it back. It has to disappear from totals and charts while remaining
    visible on its own tab.
    """
    await add(db, platform="plaid", amount=-5000, category="Groceries")
    await add(db, platform="plaid", amount=-9900, category="Excluded", entity="excluded")
    await add(db, platform="plaid", amount=7700, category="Excluded", entity="excluded")
    result = await totals(db, "plaid")
    assert result["expense"] == 5000, "excluded outflow must not count as spending"
    assert result["income"] == 0, "excluded inflow must not count as income"


def test_excluded_category_maps_to_the_excluded_entity_type():
    """Regression: excluding a row once turned it into an ordinary expense.

    determine_entity_type didn't know about the Excluded category, so setting a
    transaction's category to "Excluded" derived 'expense' from the negative
    amount and ADDED $100,000 to spending — the exact opposite of excluding it.
    The opt-out bucket must win over the sign.
    """
    from app.services.flat_categories import entity_type_for
    from app.services.rules_service import determine_entity_type

    assert entity_type_for("Excluded", -9900) == "excluded"
    assert entity_type_for("Excluded", 9900) == "excluded"
    # Both entry points agree — patch/bulk-edit go through determine_entity_type.
    assert determine_entity_type("Excluded") == "excluded"
    assert determine_entity_type("Excluded", -9900) == "excluded"
    # And a normal category still follows the sign.
    assert determine_entity_type("Groceries", -100) == "expense"
    assert determine_entity_type("Groceries", 100) == "income"


def test_excluded_rows_pass_the_sign_invariant_either_way():
    assert violates_sign_invariant("excluded", -9900, "Excluded") is False
    assert violates_sign_invariant("excluded", 9900, "Excluded") is False
