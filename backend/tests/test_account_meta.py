"""Account identity is derived from the data, not hardcoded.

Display names and kinds used to be a dict of real account numbers in
`routers/net_worth.py`. These tests pin down the replacement: defaults come out
of what `transactions` already holds, and anything a user cares about — most
importantly `real_estate`, which cannot be inferred and changes the net-worth
arithmetic — can be loaded as data and edited afterwards.
"""
import asyncio
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.account_meta import (
    derive_defaults,
    derive_display_name,
    entries_from_payload,
    infer_kind,
    load_account_meta,
    seed_account_meta,
)

CHECKING = "seedtest-0001"
CARD = "seedtest-0002"
PAYSTUB = "seedtest-payroll"
MARK = "AcctMetaSeed"


@pytest.fixture(autouse=True)
def cleanup():
    yield
    con = sqlite3.connect(os.environ["DB_PATH"])
    try:
        con.execute("DELETE FROM transactions WHERE merchant = ?", (MARK,))
        con.execute(
            "DELETE FROM account_meta WHERE account_id IN (?, ?, ?)",
            (CHECKING, CARD, PAYSTUB),
        )
        con.commit()
    finally:
        con.close()


async def _insert(db, account_id, amount, day, platform="chase"):
    await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description)
           VALUES (?, 'acctmeta.csv', ?, ?, ?, ?, ?)""",
        (platform, account_id, f"2031-01-{day:02d}", amount, MARK, MARK),
    )


@pytest.fixture
def seeded(test_client):
    """A checking-shaped account, a card-shaped account, and a synthetic one."""
    from app.database import get_db

    async def go():
        db = await get_db()
        try:
            # Checking: deposits interleaved with spending, ends positive.
            for i in range(1, 11):
                await _insert(db, CHECKING, 50000 if i % 2 else -20000, i)
            # Card: 18 charges, 2 payments, running total below zero.
            for i in range(1, 19):
                await _insert(db, CARD, -5000, i)
            await _insert(db, CARD, 20000, 19)
            await _insert(db, CARD, 20000, 20)
            # Paystub components: money already counted in the deposit account.
            for i in range(1, 11):
                await _insert(db, PAYSTUB, -1000, i, platform="paystub")
            await db.commit()
            return await seed_account_meta(db)
        finally:
            await db.close()

    return asyncio.run(go())


def _meta(account_id):
    from app.database import get_db

    async def go():
        db = await get_db()
        try:
            for m in await load_account_meta(db):
                if m["account_id"] == account_id:
                    return m
            return None
        finally:
            await db.close()

    return asyncio.run(go())


def test_seeding_derives_a_readable_name_from_the_id(seeded):
    assert seeded >= 3
    assert _meta(CHECKING)["display_name"] == "Seedtest 0001"


@pytest.mark.parametrize("account_id,expected", [
    ("bank-1234", "Bank 1234"),
    ("us_bank-1234", "Us Bank 1234"),
    ("cash-app", "Cash App"),
    ("paypal", "Paypal"),
    ("", "Account"),
])
def test_derive_display_name(account_id, expected):
    assert derive_display_name(account_id) == expected


def test_seeding_infers_credit_from_the_shape_of_the_rows(seeded):
    """Overwhelmingly-debit rows with a negative running total are a card."""
    assert _meta(CARD)["kind"] == "credit"
    assert _meta(CHECKING)["kind"] == "checking"


def test_seeding_keeps_synthetic_accounts_out_of_net_worth(seeded):
    """Paystub components would double-count the deposit they describe."""
    assert _meta(PAYSTUB)["include_in_net_worth"] is False
    assert _meta(CHECKING)["include_in_net_worth"] is True


def test_seeding_never_infers_real_estate(seeded):
    """A property line is a judgement about an account, not a fact in its rows."""
    from app.database import get_db

    async def go():
        db = await get_db()
        try:
            return [d["kind"] for d in await derive_defaults(db)]
        finally:
            await db.close()

    assert "real_estate" not in asyncio.run(go())


def test_seeding_is_idempotent_and_never_overwrites(seeded, test_client):
    """A user's label survives the next boot."""
    from app.database import get_db

    resp = test_client.patch(f"/api/accounts/{CARD}/meta",
                             json={"display_name": "My Card", "kind": "real_estate"})
    assert resp.status_code == 200

    async def go():
        db = await get_db()
        try:
            return await seed_account_meta(db)
        finally:
            await db.close()

    assert asyncio.run(go()) == 0
    again = _meta(CARD)
    assert again["display_name"] == "My Card"
    assert again["kind"] == "real_estate"


@pytest.mark.parametrize("rows,debits,net,expected", [
    (20, 19, -100000, "credit"),
    (20, 19, 100000, "checking"),   # paying more than charging is not a card
    (20, 10, -100000, "checking"),  # deposits interleaved -> deposit account
    (4, 4, -100000, "checking"),    # too few rows to classify off
])
def test_infer_kind(rows, debits, net, expected):
    assert infer_kind(rows, debits, net) == expected


def test_payload_accepts_both_shapes():
    keyed = entries_from_payload({"accounts": {"bank-1234": {"name": "Checking"}}})
    listed = entries_from_payload([{"account_id": "bank-1234", "display_name": "Checking"}])
    assert keyed == listed == [{"account_id": "bank-1234", "display_name": "Checking"}]


def test_env_override_applies_labels_as_data(tmp_path, test_client, seeded):
    """ACCOUNT_META_JSON is the documented way to keep private labels out of the repo."""
    from app.database import get_db
    from app.services.account_meta import apply_env_override

    path = tmp_path / "account_meta.json"
    path.write_text('{"%s": {"display_name": "Primary Home", "kind": "real_estate"}}' % CHECKING)
    os.environ["ACCOUNT_META_JSON"] = str(path)

    async def go():
        db = await get_db()
        try:
            return await apply_env_override(db)
        finally:
            await db.close()

    try:
        assert asyncio.run(go()) == 1
    finally:
        os.environ.pop("ACCOUNT_META_JSON", None)

    applied = _meta(CHECKING)
    assert applied["display_name"] == "Primary Home"
    assert applied["kind"] == "real_estate"


def test_bulk_post_rejects_an_unknown_kind(test_client):
    """A typo must not quietly become `checking` and change net worth."""
    resp = test_client.post("/api/accounts/meta",
                            json={"accounts": {CHECKING: {"kind": "reeal_estate"}}})
    assert resp.status_code == 400
    assert "kind must be one of" in resp.json()["detail"]


def test_bulk_post_rejects_a_malformed_entry(test_client):
    resp = test_client.post("/api/accounts/meta", json={"accounts": {CHECKING: "checking"}})
    assert resp.status_code == 400


def test_put_is_accepted_as_well_as_patch(test_client, seeded):
    """Both verbs update one account, so either client style works."""
    resp = test_client.put(f"/api/accounts/{CHECKING}/meta",
                           json={"display_name": "Renamed", "kind": "savings"})
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Renamed"
    assert resp.json()["kind"] == "savings"


def test_patch_rejects_an_unsupported_field(test_client, seeded):
    resp = test_client.patch(f"/api/accounts/{CHECKING}/meta", json={"balance": 100})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# GET /api/accounts — the Transactions filter chips render `name` directly, so a
# null there puts a raw account id in front of the user.
# --------------------------------------------------------------------------- #

def test_accounts_list_never_returns_a_null_name(test_client, seeded):
    accounts = test_client.get("/api/accounts?scope=all").json()
    assert accounts, "expected the seeded accounts to be listed"
    for a in accounts:
        assert a.get("name"), f"{a['id']} has no display name: {a!r}"
        assert a["name"] == a["display_name"]
        assert a["kind"] in ("checking", "savings", "credit",
                             "brokerage", "retirement", "real_estate")


def test_accounts_list_uses_the_stored_name_over_the_derived_one(test_client, seeded):
    """Same resolution net worth uses: a stored row wins, otherwise the default."""
    listed = {a["id"]: a for a in test_client.get("/api/accounts?scope=all").json()}
    assert listed[CHECKING]["name"] == "Seedtest 0001"  # derived

    test_client.patch(f"/api/accounts/{CHECKING}/meta", json={"display_name": "Everyday"})
    listed = {a["id"]: a for a in test_client.get("/api/accounts?scope=all").json()}
    assert listed[CHECKING]["name"] == "Everyday"


def test_accounts_list_names_an_account_with_no_meta_row(test_client):
    """An account that appears in transactions before seeding still gets a name."""
    import asyncio as _asyncio
    from app.database import get_db

    async def go():
        db = await get_db()
        try:
            await _insert(db, CHECKING, -1500, 4)
            await db.commit()
        finally:
            await db.close()

    _asyncio.run(go())
    listed = {a["id"]: a for a in test_client.get("/api/accounts?scope=all").json()}
    assert listed[CHECKING]["name"] == "Seedtest 0001"
