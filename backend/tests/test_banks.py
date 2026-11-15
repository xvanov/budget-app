"""Banks & cash: grouping, the cash total, owed-on-cards, and campaign facts per account."""
import aiosqlite
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient


@pytest_asyncio.fixture
async def seeded(tmp_path, monkeypatch):
    path = str(tmp_path / "banks.db")
    import app.database as database

    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    db = await aiosqlite.connect(path)
    await db.execute("INSERT INTO plaid_items (item_id, access_token_encrypted, institution_name, status) VALUES ('i1','x','PNC','ACTIVE')")
    await db.execute("INSERT INTO plaid_items (item_id, access_token_encrypted, institution_name, status) VALUES ('i2','x','Chase','ACTIVE')")
    accounts = [
        # item, plaid id, canonical, name, type, subtype, current, include
        (1, "p-chk", "plaid-1111", "Virtual Wallet", "depository", "checking", 100000, 1),
        (1, "p-sav", "plaid-2222", "Savings", "depository", "savings", 50000, 1),
        (2, "p-card", "plaid-3333", "Sapphire", "credit", "credit card", -25000, 1),
        (2, "p-old", "plaid-4444", "Closed", "depository", "checking", 99900, 0),
    ]
    for item, pid, canon, name, typ, sub, cur, inc in accounts:
        await db.execute(
            """INSERT INTO plaid_accounts (item_db_id, plaid_account_id, name, mask, type, subtype,
                 canonical_account_id, include_in_sync, current_balance_cents, available_balance_cents)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (item, pid, name, "0000", typ, sub, canon, inc, cur, cur),
        )
    for n in range(3):
        await db.execute(
            "INSERT INTO transactions (external_id, source_platform, source_file, account_id, posted_date, amount_cents, merchant, description, normalized_description) "
            "VALUES (?, 'plaid', 'plaid', 'plaid-1111', '2026-10-01', 100, 'm', 'd', 'd')",
            (f"t-{n}",),
        )
    await db.execute(
        "INSERT INTO campaign_events (external_key, source, kind, title, occurred_on, amount_cents, account_ref) "
        "VALUES ('bp:a:bonus','bonuspilot','bonus_received','Bonus received: PNC Virtual Wallet','2026-10-10',40000,'p-chk')"
    )
    await db.commit()
    await db.close()
    from app.main import app

    with TestClient(app) as client:
        yield client


def test_groups_accounts_by_bank_and_totals_cash_and_owed(seeded):
    body = seeded.get("/api/banks").json()
    names = [i["institution_name"] for i in body["institutions"]]
    assert names == ["Chase", "PNC"]  # sorted by name


def test_cash_is_depository_only_and_excluded_accounts_do_not_count(seeded):
    body = seeded.get("/api/banks").json()
    assert body["cash_total_cents"] == 150000  # 1000.00 checking + 500.00 savings
    assert body["owed_cents"] == 25000  # card owed, shown separately, never in cash


def test_each_account_shows_its_transactions_and_campaign_facts(seeded):
    body = seeded.get("/api/banks").json()
    pnc = next(i for i in body["institutions"] if i["institution_name"] == "PNC")
    checking = next(a for a in pnc["accounts"] if a["plaid_account_id"] == "p-chk")
    assert checking["transaction_count"] == 3
    assert checking["events"][0]["kind"] == "bonus_received"
    assert checking["events"][0]["amount_cents"] == 40000
    assert pnc["total_cents"] == 150000


def test_excluded_account_is_left_out_of_its_bank(seeded):
    body = seeded.get("/api/banks").json()
    chase = next(i for i in body["institutions"] if i["institution_name"] == "Chase")
    ids = [a["plaid_account_id"] for a in chase["accounts"]]
    assert "p-old" not in ids and "p-card" in ids
