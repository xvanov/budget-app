import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import init_db, get_db
from app.services.mortgage_splitter import split_mortgage_payments


async def _insert_txn(db, **kwargs):
    fields = {
        "source_platform": "chase",
        "source_file": "test.csv",
        "account_id": "home-0001",
        "posted_date": "2026-06-15",
        "amount_cents": -317792,
        "currency": "USD",
        "merchant": "HMG Mortgage",
        "description": "ONLINE PAYMENT TO HMG",
        "normalized_description": "online payment to hmg",
    }
    fields.update(kwargs)
    cols = ", ".join(fields.keys())
    vals = ", ".join("?" * len(fields))
    cursor = await db.execute(
        f"INSERT INTO transactions ({cols}) VALUES ({vals})",
        list(fields.values())
    )
    return cursor.lastrowid


async def _insert_amortization(db, **kwargs):
    fields = {
        "month": "2026-06-01",
        "principal_cents": 233491,
        "interest_cents": 84301,
        "ending_balance_cents": 15353815,
    }
    fields.update(kwargs)
    cols = ", ".join(fields.keys())
    vals = ", ".join("?" * len(fields))
    await db.execute(
        f"INSERT INTO amortization ({cols}) VALUES ({vals})",
        list(fields.values())
    )
    await db.commit()


def _run_async(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestMortgageSplitter:
    @pytest.fixture(autouse=True)
    def _clean_db(self):
        _run_async(self._clean())

    async def _clean(self):
        await init_db()
        db = await get_db()
        try:
            await db.execute("DELETE FROM transfers")
            await db.execute("DELETE FROM transactions")
            await db.execute("DELETE FROM amortization")
            await db.commit()
        finally:
            await db.close()

    # --- Tests ---

    def test_detects_hmg_mortgage_and_splits(self):
        _run_async(self._run_detects())

    async def _run_detects(self):
        await init_db()
        db = await get_db()
        try:
            txn_id = await _insert_txn(db)
            await _insert_amortization(db)
            await db.commit()

            result = await split_mortgage_payments(db)
            assert result["split"] == 1

            cursor = await db.execute("SELECT * FROM transactions WHERE id=?", (txn_id,))
            parent = await cursor.fetchone()
            assert parent["entity_type"] == "transfer"
            assert parent["category_path"] == "Transfer > Mortgage Payment (Split)"
        finally:
            await db.close()

    def test_components_created_correctly(self):
        _run_async(self._run_components())

    async def _run_components(self):
        await init_db()
        db = await get_db()
        try:
            txn_id = await _insert_txn(
                db, posted_date="2026-06-15", amount_cents=-317792,
            )
            await _insert_amortization(db, month="2026-06-01", principal_cents=233491, interest_cents=84301)
            await db.commit()

            await split_mortgage_payments(db)

            cursor = await db.execute(
                "SELECT category_path, entity_type, asset_class, amount_cents "
                "FROM transactions WHERE linked_transaction_id=? AND category_path='Transfer > Mortgage Principal'",
                (txn_id,)
            )
            principal = await cursor.fetchone()
            assert principal is not None
            assert principal["entity_type"] == "transfer"
            assert principal["asset_class"] == "mortgage_principal"
            assert principal["amount_cents"] == -233491

            cursor = await db.execute(
                "SELECT category_path, entity_type, asset_class, amount_cents "
                "FROM transactions WHERE linked_transaction_id=? AND category_path='Expense > Housing > Mortgage Interest'",
                (txn_id,)
            )
            interest = await cursor.fetchone()
            assert interest is not None
            assert interest["entity_type"] == "expense"
            assert interest["asset_class"] is None
            assert interest["amount_cents"] == -84301
        finally:
            await db.close()

    def test_components_sum_to_original(self):
        _run_async(self._run_sum())

    async def _run_sum(self):
        await init_db()
        db = await get_db()
        try:
            txn_id = await _insert_txn(
                db, posted_date="2026-06-15", amount_cents=-317792,
            )
            await _insert_amortization(db, month="2026-06-01", principal_cents=233491, interest_cents=84301)
            await db.commit()

            await split_mortgage_payments(db)

            cursor = await db.execute(
                "SELECT amount_cents FROM transactions WHERE linked_transaction_id=?",
                (txn_id,)
            )
            component_total = sum(abs(r["amount_cents"]) for r in await cursor.fetchall())

            cursor = await db.execute("SELECT amount_cents FROM transactions WHERE id=?", (txn_id,))
            original = await cursor.fetchone()
            assert abs(original["amount_cents"]) == component_total, \
                f"Original {abs(original['amount_cents'])} should equal sum of components {component_total}"
        finally:
            await db.close()

    def test_payoff_splits_with_under_1k_interest(self):
        _run_async(self._run_payoff())

    async def _run_payoff(self):
        await init_db()
        db = await get_db()
        try:
            txn_id = await _insert_txn(
                db, posted_date="2026-04-28", amount_cents=-10000000,
            )
            await _insert_amortization(db, month="2026-06-01", principal_cents=233491, interest_cents=84301)
            await db.commit()

            result = await split_mortgage_payments(db)
            assert result["split"] == 1

            cursor = await db.execute(
                "SELECT amount_cents FROM transactions WHERE linked_transaction_id=? AND category_path='Expense > Housing > Mortgage Interest'",
                (txn_id,)
            )
            interest = await cursor.fetchone()
            assert interest is not None
            assert abs(interest["amount_cents"]) <= 100000, \
                f"Interest {abs(interest['amount_cents'])} should be <= $1K (100000 cents)"

            cursor = await db.execute(
                "SELECT amount_cents FROM transactions WHERE linked_transaction_id=? AND category_path='Transfer > Mortgage Principal'",
                (txn_id,)
            )
            principal = await cursor.fetchone()
            assert principal is not None
            assert abs(principal["amount_cents"]) + abs(interest["amount_cents"]) == 10000000
        finally:
            await db.close()

    def test_splitter_idempotent(self):
        _run_async(self._run_idempotent())

    async def _run_idempotent(self):
        await init_db()
        db = await get_db()
        try:
            txn_id = await _insert_txn(db)
            await _insert_amortization(db)
            await db.commit()

            result1 = await split_mortgage_payments(db)
            assert result1["split"] == 1

            result2 = await split_mortgage_payments(db)
            assert result2["split"] == 0, "Second split should find 0 new payments"

            cursor = await db.execute(
                "SELECT COUNT(*) as c FROM transactions WHERE linked_transaction_id=?",
                (txn_id,)
            )
            count = await cursor.fetchone()
            assert count["c"] == 2, "Should still have exactly 2 component rows"
        finally:
            await db.close()

    def test_escrow_component_created(self):
        _run_async(self._run_escrow())

    async def _run_escrow(self):
        await init_db()
        db = await get_db()
        try:
            total = 400000
            principal_amt = 200000
            interest_amt = 100000
            escrow_amt = total - principal_amt - interest_amt

            txn_id = await _insert_txn(
                db, posted_date="2026-12-15", amount_cents=-total,
            )
            await _insert_amortization(
                db, month="2026-12-01", principal_cents=principal_amt, interest_cents=interest_amt
            )
            await db.commit()

            await split_mortgage_payments(db)

            cursor = await db.execute(
                "SELECT category_path, entity_type, asset_class, amount_cents "
                "FROM transactions WHERE linked_transaction_id=? AND category_path='Transfer > Mortgage Escrow'",
                (txn_id,)
            )
            escrow_row = await cursor.fetchone()
            assert escrow_row is not None, "Escrow component should exist"
            assert escrow_row["entity_type"] == "transfer"
            assert escrow_row["asset_class"] == "escrow"
            assert abs(escrow_row["amount_cents"]) == escrow_amt
        finally:
            await db.close()

    def test_no_amortization_fallback_to_transfer(self):
        _run_async(self._run_no_amort())

    async def _run_no_amort(self):
        await init_db()
        db = await get_db()
        try:
            txn_id = await _insert_txn(
                db, posted_date="2025-01-01", amount_cents=-500000,
            )
            await db.commit()

            result = await split_mortgage_payments(db)
            assert result["split"] == 1
            assert len(result["warnings"]) >= 1

            cursor = await db.execute("SELECT category_path, entity_type FROM transactions WHERE id=?", (txn_id,))
            row = await cursor.fetchone()
            assert row["entity_type"] == "transfer"
            assert "Mortgage Principal" in (row["category_path"] or "")
        finally:
            await db.close()

    def test_nearest_amortization_estimate(self):
        _run_async(self._run_nearest())

    async def _run_nearest(self):
        await init_db()
        db = await get_db()
        try:
            txn_id = await _insert_txn(
                db, posted_date="2026-04-28", amount_cents=-500000,
            )
            await _insert_amortization(db, month="2026-06-01", principal_cents=233491, interest_cents=84301)
            await db.commit()

            result = await split_mortgage_payments(db)
            assert result["split"] == 1
            assert any("estimated interest" in w.lower() for w in result["warnings"]), \
                f"Should contain warning about estimated interest: {result['warnings']}"

            cursor = await db.execute(
                "SELECT amount_cents FROM transactions WHERE linked_transaction_id=? AND category_path='Expense > Housing > Mortgage Interest'",
                (txn_id,)
            )
            interest = await cursor.fetchone()
            assert interest is not None
            assert abs(interest["amount_cents"]) == 84301, "Should use nearest amortization's interest"
        finally:
            await db.close()
