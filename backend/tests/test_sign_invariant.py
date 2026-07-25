"""Regression tests for the sign-direction invariant.

The invariant:
  * ``entity_type='expense'`` must have ``amount_cents <= 0``.
  * ``entity_type='income'``  must have ``amount_cents >= 0``.
  * Transfer / asset_conversion / liability_payment may have either sign.

These are value-compare regression tests for the bulk-corruption that
incorrectly tagged positive (incoming) rent payments as
``Expense > Rental > Rent``.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.sign_invariant import (
    coerce_sign_safe,
    violates_sign_invariant,
)


# ───────────────────────── unit tests on the helper ─────────────────────────


def test_violates_when_expense_with_positive_amount():
    assert violates_sign_invariant("expense", 100) is True


def test_violates_when_income_with_negative_amount():
    assert violates_sign_invariant("income", -100) is True


def test_does_not_violate_expense_with_negative_amount():
    assert violates_sign_invariant("expense", -100) is False


def test_does_not_violate_income_with_positive_amount():
    assert violates_sign_invariant("income", 100) is False


def test_does_not_violate_transfer_with_either_sign():
    assert violates_sign_invariant("transfer", 100) is False
    assert violates_sign_invariant("transfer", -100) is False


def test_does_not_violate_asset_conversion_with_either_sign():
    assert violates_sign_invariant("asset_conversion", 50) is False
    assert violates_sign_invariant("asset_conversion", -50) is False


def test_zero_amount_is_safe_for_either_direction():
    # Zero is "<=0" and ">=0" both — neither violates.
    assert violates_sign_invariant("expense", 0) is False
    assert violates_sign_invariant("income", 0) is False


# ─────── path-vs-entity_type invariant (the second failure mode) ───────


def test_path_expense_with_income_entity_is_violation():
    """A row whose category_path begins 'Expense >' must have
    entity_type='expense', regardless of amount sign."""
    # Positive amount → would also fail the sign check, but path check fires first.
    assert (
        violates_sign_invariant("income", 6770, "Expense > Personal > Shopping")
        is True
    )
    # Even with a 'safe' sign for income (positive), the path/entity mismatch
    # is still a violation by itself.
    assert (
        violates_sign_invariant("income", 100, "Expense > Fees & Interest")
        is True
    )


def test_path_income_with_expense_entity_is_violation():
    """Symmetric: 'Income >' path with entity_type='expense' is a violation."""
    assert (
        violates_sign_invariant("expense", -500, "Income > Rentals")
        is True
    )
    assert (
        violates_sign_invariant("expense", 500, "Income > Other Income")
        is True
    )


def test_path_transfer_allows_transfer_family():
    """'Transfer >' path is OK with transfer / asset_conversion /
    liability_payment, but NOT with expense or income."""
    assert (
        violates_sign_invariant("transfer", -1000, "Transfer > Cash Out")
        is False
    )
    assert (
        violates_sign_invariant("asset_conversion", 500, "Transfer > Asset Move")
        is False
    )
    assert (
        violates_sign_invariant(
            "liability_payment", -2000, "Transfer > Credit-card Payment"
        )
        is False
    )
    # Wrong entity_type for a Transfer path:
    assert (
        violates_sign_invariant("expense", -2000, "Transfer > Credit-card Payment")
        is True
    )
    assert (
        violates_sign_invariant("income", 2000, "Transfer > Credit-card Payment")
        is True
    )


def test_path_check_does_not_fire_when_path_is_none():
    """Helper must remain backward-compatible: no path → no path check."""
    # Caller that doesn't pass a path (2-arg form) only gets the sign check.
    assert violates_sign_invariant("income", 100) is False
    # Passing None for path explicitly also disables the path check.
    assert violates_sign_invariant("income", 100, None) is False


def test_coerce_positive_expense_rental_to_income_rentals():
    new_cp, new_et, new_bc, note = coerce_sign_safe(
        "Expense > Rental > Rent", "expense", 280000, "rental"
    )
    assert new_cp == "Income > Rentals"
    assert new_et == "income"
    assert new_bc == "rental"
    assert note is not None
    assert "coerced" in note


def test_coerce_positive_expense_personal_becomes_a_refund():
    """An inflow on a non-rental expense category is a merchant refund.

    This assertion was inverted (it expected 'Uncategorized') until live data
    settled it: all 35 rows that reached this branch were genuine merchant
    credits — Amazon, Apple, a batch of SHEIN reversals, NordVPN. Discarding a
    correct category and marking them Uncategorized stranded real money, so the
    contra-expense booking is now the documented behaviour. Rentals keep the
    income fallback (see the test above) because inbound rent is real income.
    """
    new_cp, new_et, new_bc, note = coerce_sign_safe(
        "Expense > Personal > Shopping", "expense", 5000, "personal"
    )
    assert new_cp == "Expense > Personal > Shopping", "category survives so the refund offsets it"
    assert new_et == "refund"
    assert new_bc == "personal"
    assert note is not None


def test_coerce_negative_income_to_uncategorized():
    new_cp, new_et, new_bc, note = coerce_sign_safe(
        "Income > Rentals", "income", -100000, "rental"
    )
    assert new_cp == "Uncategorized"
    assert new_et == "expense"
    assert note is not None


def test_coerce_safe_input_returns_unchanged_and_no_note():
    new_cp, new_et, new_bc, note = coerce_sign_safe(
        "Expense > Personal > Other", "expense", -100, "personal"
    )
    assert new_cp == "Expense > Personal > Other"
    assert new_et == "expense"
    assert new_bc == "personal"
    assert note is None


# ─────────────────────── integration test (via API) ────────────────────────


def _make_categorizer_mock_that_returns(category_path, entity_type):
    """Return a mock that *forces* the LLM to claim an expense path."""
    def mock_call_llm(prompt, **kwargs):
        import re
        results = []
        for line in prompt.split('\n'):
            m = re.match(r'\s+ID=(\d+):', line)
            if m:
                results.append({
                    "id": int(m.group(1)),
                    "category_path": category_path,
                    "entity_type": entity_type,
                    "confidence_score": 0.9,
                    "categorization_reason": "Forced for sign-invariant test",
                    "business_context": "rental",
                })
        return json.dumps(results)
    return mock_call_llm


@pytest.mark.asyncio
async def test_categorizer_rejects_expense_assignment_for_positive_row(
    test_client, monkeypatch
):
    """A positive-amount row must never end up booked as spending.

    Originally this asserted a coercion to 'Income > Rentals' with a fallback
    note. Under the flat model entity_type is DERIVED from the amount's sign, so
    an inflow cannot be an expense in the first place — the guarantee is
    structural instead of corrective. Any hierarchical path the model emits is
    also flattened on the way in.
    """
    from app.database import get_db
    from app.services.categorizer import categorize_single_transaction

    # Insert a known positive-amount transaction directly. We do NOT wipe
    # the table because of FOREIGN KEY constraints from transfers/linked txns;
    # we just operate on the row we created via its unique id.
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date,
                amount_cents, merchant, description, category_path,
                entity_type, business_context)
               VALUES ('test-sign-inv', 'test.csv', 'test-acct',
                       '2026-04-15', 280000, 'Test Tenant SignInv',
                       'Zelle from Test Tenant rent SignInv',
                       NULL, 'uncategorized', 'rental')""",
        )
        await db.commit()
        txn_id = cursor.lastrowid
    finally:
        await db.close()

    # Force the LLM to try assigning Expense > Rental > Rent.
    monkeypatch.setattr(
        "app.services.categorizer.call_llm",
        _make_categorizer_mock_that_returns(
            "Expense > Rental > Rent", "expense"
        ),
    )

    db = await get_db()
    try:
        ok = await categorize_single_transaction(db, txn_id)
        assert ok is True
    finally:
        await db.close()

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT category_path, entity_type, business_context, "
            "categorization_reason FROM transactions WHERE id=?",
            (txn_id,),
        )
        row = dict(await cursor.fetchone())
    finally:
        await db.close()

    # Value-compare: the categorizer MUST NOT leave this row as expense.
    assert row["entity_type"] == "income", (
        f"sign invariant violated: positive amount ended up as "
        f"entity_type={row['entity_type']!r} category={row['category_path']!r}"
    )
    # Flat model: a positive amount can never be an expense, because
    # entity_type is DERIVED from the sign — the corruption this test
    # guards is now structurally impossible rather than corrected after
    # the fact. Any legacy path the model emits is also flattened on the
    # way in, so "Expense > Rental > Rent" lands as a flat name.
    assert " > " not in (row["category_path"] or ""), "categories are flat"
    # No "Income >" prefix to check any more, and no coercion note either: there
    # is nothing to coerce when the sign IS the source of truth. The protection
    # this test exists for — a positive row must never be booked as spending — is
    # asserted above via entity_type, and is now impossible to violate rather
    # than repaired afterwards.
    assert row["entity_type"] != "expense"


@pytest.mark.asyncio
async def test_patch_endpoint_rejects_expense_for_positive_row(
    test_client, monkeypatch
):
    """PATCH /api/transactions/{id} must 400 when caller tries to set an
    Expense > * category on a positive-amount row."""
    from app.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date,
                amount_cents, merchant, description, category_path,
                entity_type, business_context)
               VALUES ('test', 'test.csv', 'test-acct', '2026-04-16',
                       150000, 'Tenant Two',
                       'Zelle from Tenant Two', 'Income > Rentals',
                       'income', 'rental')""",
        )
        await db.commit()
        txn_id = cursor.lastrowid
    finally:
        await db.close()

    resp = test_client.patch(
        f"/api/transactions/{txn_id}",
        json={"category_path": "Expense > Rental > Rent"},
    )
    # Still a 400, but for a clearer reason: categories are flat now, so a
    # hierarchical path is invalid input at the API boundary rather than a
    # sign contradiction to be repaired.
    assert resp.status_code == 400, (
        f"expected 400 but got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    # The rejection reason changed with the flat model: a hierarchical path is
    # now invalid input, which is caught before any sign reasoning happens.
    assert "legacy hierarchical path" in body["detail"]

    # And the row must NOT have been changed.
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT category_path, entity_type FROM transactions WHERE id=?",
            (txn_id,),
        )
        row = dict(await cursor.fetchone())
    finally:
        await db.close()
    assert row["category_path"] == "Income > Rentals"
    assert row["entity_type"] == "income"


@pytest.mark.asyncio
async def test_sign_violations_endpoint_returns_zero_when_clean(
    test_client,
):
    """GET /api/categories/sign-violations returns count=0 when only
    sign-consistent rows exist. We seed four good rows on top of whatever
    the test DB already has, then assert the audit endpoint does NOT
    surface any of *our* rows (identified by a unique source_file)."""
    from app.database import get_db

    unique_src = "sign-clean-fixture.csv"
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date,
                amount_cents, merchant, description, category_path,
                entity_type, business_context)
               VALUES
                 ('t', ?, 'a','2026-01-01',-1000,'M','desc',
                  'Expense > Other','expense','personal'),
                 ('t', ?, 'a','2026-01-01', 2000,'M','desc',
                  'Income > Other Income','income','personal'),
                 ('t', ?, 'a','2026-01-01',-500,'M','desc',
                  'Transfer > Cash Out','transfer','personal'),
                 ('t', ?, 'a','2026-01-01', 500,'M','desc',
                  'Transfer > Cash Out','transfer','personal')""",
            (unique_src, unique_src, unique_src, unique_src),
        )
        await db.commit()
    finally:
        await db.close()

    resp = test_client.get("/api/categories/sign-violations")
    assert resp.status_code == 200
    data = resp.json()
    # None of OUR fixture rows must show up — they are all sign-consistent.
    our_violations = [
        it for it in data["items"]
        if (it.get("merchant") == "M"
            and it.get("description") == "desc"
            and it.get("posted_date") == "2026-01-01")
    ]
    assert len(our_violations) == 0, (
        f"clean fixture rows surfaced as violations: {our_violations}"
    )


@pytest.mark.asyncio
async def test_sign_violations_endpoint_finds_planted_violation(test_client):
    """Audit endpoint must surface a planted sign-violation row exactly."""
    from app.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date,
                amount_cents, merchant, description, category_path,
                entity_type, business_context)
               VALUES ('t-violation','t-violation.csv','a','2026-01-01',
                       12345, 'BadMerchantSignInv',
                       'planted violation row', 'Expense > Rental > Rent',
                       'expense', 'rental')""",
        )
        await db.commit()
        bad_id = cursor.lastrowid
    finally:
        await db.close()

    resp = test_client.get("/api/categories/sign-violations")
    assert resp.status_code == 200
    data = resp.json()
    matches = [it for it in data["items"] if it["id"] == bad_id]
    assert len(matches) == 1
    item = matches[0]
    assert item["amount_cents"] == 12345
    assert item["entity_type"] == "expense"
    assert item["category_path"] == "Expense > Rental > Rent"


@pytest.mark.asyncio
async def test_audit_endpoint_surfaces_path_entity_mismatch(test_client):
    """Audit endpoint must surface rows where category_path family disagrees
    with entity_type, even when the (entity_type, amount_cents) sign rule is
    technically satisfied.

    This is the second failure mode added by the path-invariant tightening:
    e.g. category_path='Expense > Personal > Shopping' with
    entity_type='income' and a positive amount — the sign rule alone says
    'fine' (positive amount on income is OK) but the path/entity disagreement
    is incoherent and must be surfaced.
    """
    from app.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date,
                amount_cents, merchant, description, category_path,
                entity_type, business_context)
               VALUES ('t-path-mismatch','t-path-mismatch.csv','a',
                       '2026-01-01', 6770, 'PathMismatchMerchant',
                       'planted path/entity mismatch',
                       'Expense > Personal > Shopping',
                       'income', 'personal')""",
        )
        await db.commit()
        bad_id = cursor.lastrowid
    finally:
        await db.close()

    resp = test_client.get("/api/categories/sign-violations")
    assert resp.status_code == 200
    data = resp.json()
    matches = [it for it in data["items"] if it["id"] == bad_id]
    assert len(matches) == 1, (
        f"path/entity mismatch row not surfaced by audit endpoint: "
        f"got {len(matches)} matches in {len(data['items'])} items"
    )
    item = matches[0]
    # Value-compare: the planted contradiction is preserved verbatim in the
    # audit response so a human can review it.
    assert item["amount_cents"] == 6770
    assert item["entity_type"] == "income"
    assert item["category_path"] == "Expense > Personal > Shopping"
