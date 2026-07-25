"""Chat-correct under the FLAT category model.

Chat-correct was the last writer that still spoke hierarchical paths: its prompt
asked the LLM for ``Expense > Groceries`` and ``ensure_category_path`` happily
created the matching junk rows. These tests pin the flat behaviour at both ends —
the prompt the model is given, and the coercion applied to whatever it answers —
plus the endpoint that shows which rows a correction touched.

``app.routers.chat_correct.call_llm`` is monkeypatched in every test that hits the
endpoint: a real shell-out from pytest hangs forever (see AGENTS.md).
"""

import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import get_db
from app.routers.chat_correct import _build_chat_prompt


TXN_KEYS = {"id", "posted_date", "merchant", "description", "amount_cents",
            "account_id", "category_path"}


def _chat_mock(actions, summary="Flat-category test"):
    """Stub for ``call_llm`` returning a fixed action list."""
    def mock_call_llm(prompt, **kwargs):
        return json.dumps({"summary": summary, "actions": actions})
    return mock_call_llm


async def _seed_txn(merchant, amount_cents=-1234, posted_date="2026-05-01"):
    db = await get_db()
    try:
        cursor = await db.execute(
            """INSERT INTO transactions
               (source_platform, source_file, account_id, posted_date, amount_cents,
                merchant, description, category_path, entity_type, business_context)
               VALUES ('test', 'flat-test', 'flat-test-acct', ?, ?, ?, ?,
                       'Uncategorized', 'uncategorized', 'personal')""",
            (posted_date, amount_cents, merchant, merchant),
        )
        new_id = cursor.lastrowid
        await db.commit()
        return new_id
    finally:
        await db.close()


async def _hierarchical_paths():
    """Every category row that still carries a hierarchical path.

    The test DB is seeded with the legacy tree by ``seed_categories`` on startup,
    so "no hierarchical rows exist" would be false from the first line. What
    matters is that chat-correct ADDS none, hence the before/after comparison.
    """
    db = await get_db()
    try:
        cursor = await db.execute("SELECT path FROM categories WHERE path LIKE '% > %'")
        return {r[0] for r in await cursor.fetchall()}
    finally:
        await db.close()


async def _categories_named(name):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, name, parent_id, path, color FROM categories WHERE name=? OR path=?",
            (name, name),
        )
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


async def _rules_for(pattern):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, pattern, category_path FROM rules WHERE LOWER(pattern)=?",
            (pattern.lower(),),
        )
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


async def _txn(txn_id):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, category_path, entity_type FROM transactions WHERE id=?", (txn_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def _latest_correction_id(prompt):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM chat_corrections WHERE prompt=? ORDER BY id DESC LIMIT 1",
            (prompt,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    finally:
        await db.close()


# ───────── sanitization: whatever the LLM says, a flat name is stored ─────────

@pytest.mark.asyncio
async def test_recategorize_legacy_path_stored_flat(test_client, monkeypatch):
    """A recategorize answering ``Expense > Groceries`` stores ``Groceries``."""
    txn_id = await _seed_txn("FLATTEST GROCER ALPHA", amount_cents=-1629)
    hier_before = await _hierarchical_paths()

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _chat_mock([
        {"type": "recategorize",
         "params": {"search": "flattest grocer alpha",
                    "category_path": "Expense > Groceries"}},
    ]))

    resp = test_client.post(
        "/api/chat-correct",
        json={"message": "flattest grocer alpha is groceries"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert txn_id in data["affected_ids"]

    row = await _txn(txn_id)
    assert row["category_path"] == "Groceries"
    assert " > " not in row["category_path"]
    assert row["entity_type"] == "expense"  # derived from the negative amount

    # The coercion is reported so the user can see it happened.
    assert "Coerced hierarchical category 'Expense > Groceries' to flat 'Groceries'" \
        in data["context_diff"]

    assert await _hierarchical_paths() == hier_before, \
        "chat-correct created a hierarchical category row"
    assert not await _categories_named("Expense > Groceries")


@pytest.mark.asyncio
async def test_recategorize_flattening_keeps_the_leaf_not_the_prefix(test_client, monkeypatch):
    """An invented path keeps its LEAF, not the family its prefix maps to.

    ``flatten_path`` on its own answers ``Other`` for ``Expense > Weekend Fun``
    because it matches the legacy ``Expense`` prefix. The leaf is what the model
    actually meant, so filing the row under ``Other`` would lose the correction.
    """
    txn_id = await _seed_txn("FLATTEST LEAFCHECK", amount_cents=-500)

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _chat_mock([
        {"type": "recategorize",
         "params": {"search": "flattest leafcheck",
                    "category_path": "Expense > Weekend Fun"}},
    ]))

    resp = test_client.post("/api/chat-correct", json={"message": "leafcheck is weekend fun"})
    assert resp.status_code == 200, resp.text

    row = await _txn(txn_id)
    assert row["category_path"] == "Weekend Fun"

    created = await _categories_named("Weekend Fun")
    assert len(created) == 1
    assert created[0]["parent_id"] is None
    assert created[0]["path"] == "Weekend Fun"


@pytest.mark.asyncio
async def test_add_rule_legacy_path_stored_flat(test_client, monkeypatch):
    """An add_rule answering a hierarchical path stores a flat rule category."""
    hier_before = await _hierarchical_paths()

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _chat_mock([
        {"type": "add_rule",
         "params": {"pattern": "flattest spectrum",
                    "category_path": "Expense > Rental > Utilities",
                    "description": "flat-test: spectrum is a utility"}},
    ]))

    resp = test_client.post("/api/chat-correct", json={"message": "spectrum is a utility"})
    assert resp.status_code == 200, resp.text

    rules = await _rules_for("flattest spectrum")
    assert len(rules) == 1
    assert rules[0]["category_path"] == "Utilities"
    assert " > " not in rules[0]["category_path"]

    assert await _hierarchical_paths() == hier_before


@pytest.mark.asyncio
async def test_create_category_legacy_path_creates_flat_row(test_client, monkeypatch):
    """create_category with a hierarchical path creates ONE flat row."""
    hier_before = await _hierarchical_paths()
    name = "FlatTest Bucket"

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _chat_mock([
        {"type": "create_category", "params": {"category_path": f"Expense > {name}"}},
    ]))

    resp = test_client.post("/api/chat-correct", json={"message": f"create {name}"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["affected_count"] == 0
    assert f"Ensured category exists: {name}" in data["context_diff"]

    rows = await _categories_named(name)
    assert len(rows) == 1
    assert rows[0]["parent_id"] is None
    assert rows[0]["path"] == name == rows[0]["name"]
    assert rows[0]["color"], "a new category must get a colour"

    assert await _hierarchical_paths() == hier_before
    assert not await _categories_named(f"Expense > {name}")


@pytest.mark.asyncio
async def test_new_flat_category_created_once_not_twice(test_client, monkeypatch):
    """A brand-new flat name creates exactly one row, and is idempotent."""
    name = "FlatTest Solo"
    assert not await _categories_named(name), "test precondition: name is unused"

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _chat_mock([
        {"type": "create_category", "params": {"category_path": name}},
    ]))

    first = test_client.post("/api/chat-correct", json={"message": f"create {name}"})
    assert first.status_code == 200, first.text
    assert len(await _categories_named(name)) == 1

    second = test_client.post("/api/chat-correct", json={"message": f"create {name} again"})
    assert second.status_code == 200, second.text
    rows = await _categories_named(name)
    assert len(rows) == 1, f"duplicate category rows created: {rows}"


@pytest.mark.asyncio
async def test_uncategorized_category_skips_the_action(test_client, monkeypatch):
    """``Uncategorized`` flattens to None — the action is skipped, not written."""
    txn_id = await _seed_txn("FLATTEST SKIPME", amount_cents=-777)

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _chat_mock([
        {"type": "recategorize",
         "params": {"search": "flattest skipme", "category_path": "Uncategorized"}},
    ]))

    resp = test_client.post("/api/chat-correct", json={"message": "uncategorize skipme"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["affected_count"] == 0
    assert "Skipped action" in (data["context_diff"] or "")

    row = await _txn(txn_id)
    assert row["category_path"] == "Uncategorized"
    assert not await _rules_for("flattest skipme")


# ───────── rules: wanted, but not duplicated ─────────

@pytest.mark.asyncio
async def test_repeated_recategorize_does_not_duplicate_rule(test_client, monkeypatch):
    """The same correction twice leaves ONE rule, and says it reused it."""
    await _seed_txn("FLATTEST DUPE CAFE", amount_cents=-2200)

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _chat_mock([
        {"type": "recategorize",
         "params": {"search": "flattest dupe cafe", "category_path": "Restaurants"}},
    ]))

    first = test_client.post(
        "/api/chat-correct", json={"message": "flattest dupe cafe is restaurants"})
    assert first.status_code == 200, first.text
    rules = await _rules_for("flattest dupe cafe")
    assert len(rules) == 1
    assert f"Created rule #{rules[0]['id']}" in first.json()["context_diff"]

    second = test_client.post(
        "/api/chat-correct", json={"message": "flattest dupe cafe is restaurants"})
    assert second.status_code == 200, second.text
    rules_after = await _rules_for("flattest dupe cafe")
    assert len(rules_after) == 1, f"duplicate rules created: {rules_after}"
    assert f"Reused existing rule #{rules[0]['id']}" in second.json()["context_diff"]


@pytest.mark.asyncio
async def test_repeated_add_rule_does_not_duplicate_rule(test_client, monkeypatch):
    """add_rule is idempotent on (pattern, category_path) too."""
    monkeypatch.setattr("app.routers.chat_correct.call_llm", _chat_mock([
        {"type": "add_rule",
         "params": {"pattern": "flattest idem", "category_path": "Subscriptions",
                    "description": "flat-test idempotent rule"}},
    ]))

    for _ in range(2):
        resp = test_client.post("/api/chat-correct", json={"message": "flattest idem rule"})
        assert resp.status_code == 200, resp.text

    rules = await _rules_for("flattest idem")
    assert len(rules) == 1, f"duplicate rules created: {rules}"
    assert rules[0]["category_path"] == "Subscriptions"


# ───────── the prompt IS the fix ─────────

def test_prompt_teaches_flat_categories_only():
    """``_build_chat_prompt`` must not ask for hierarchical paths anymore."""
    categories = ["Groceries", "Restaurants", "Housing", "Never Used Category"]
    prompt = _build_chat_prompt(
        "put netflix in subscriptions",
        merchants=["NETFLIX.COM"],
        categories=categories,
        accounts=[{"account_id": "card-0001", "source_platform": "chase", "cnt": 12}],
        txn_stats={"count": 12, "min_date": "2026-01-01", "max_date": "2026-07-01",
                   "income": 100000, "expense": 50000},
    )

    # No hierarchical instruction survives anywhere in the prompt — not as a rule,
    # not as an example the model could copy.
    assert " > " not in prompt, "prompt still contains a hierarchical path"
    assert "MUST start with" not in prompt

    assert "FLAT" in prompt
    assert "must NEVER contain the `>` character" in prompt
    assert "AMOUNT SIGN" in prompt
    # Special buckets are explained.
    for bucket in ("Transfer", "Credit-card Payment", "Mortgage Payment", "Excluded"):
        assert bucket in prompt, f"prompt does not mention {bucket}"
    # A new name is allowed but existing ones are preferred.
    assert "NEW flat name is allowed" in prompt
    assert "prefer an" in prompt
    # Every supplied category is offered, including one with zero transactions.
    for cat in categories:
        assert f"  - {cat}" in prompt


@pytest.mark.asyncio
async def test_prompt_lists_categories_with_zero_transactions(test_client, monkeypatch):
    """The category list comes from the categories table, not from transactions.

    A category nobody has used yet is still one the model should be able to pick.
    """
    await _seed_txn("FLATTEST PROMPTSRC", amount_cents=-101)
    resp = test_client.post("/api/categories", json={"name": "FlatTest Unused Cat"})
    assert resp.status_code == 201, resp.text

    captured = {}

    def capture(prompt, **kwargs):
        captured["prompt"] = prompt
        return json.dumps({"summary": "noop", "actions": []})

    monkeypatch.setattr("app.routers.chat_correct.call_llm", capture)
    resp = test_client.post("/api/chat-correct", json={"message": "what categories exist?"})
    assert resp.status_code == 200, resp.text

    assert "  - FlatTest Unused Cat" in captured["prompt"]
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM transactions WHERE category_path='FlatTest Unused Cat'"
        )
        assert (await cursor.fetchone())[0] == 0
    finally:
        await db.close()
    # Hierarchical rows still sitting in the categories table (the legacy seed)
    # must not be offered back to the model.
    for legacy in ("Expense > Personal > Groceries", "Income > Software Job", "  - Expense"):
        assert legacy not in captured["prompt"], \
            f"hierarchical leftover {legacy!r} leaked into the prompt"


# ───────── which rows did a correction touch? ─────────

@pytest.mark.asyncio
async def test_correction_transactions_returns_affected_rows(test_client, monkeypatch):
    """GET /api/chat-corrections/{id}/transactions lists the rows it touched."""
    ids = [
        await _seed_txn("FLATTEST ENDPOINT ROW", amount_cents=-100, posted_date="2026-03-01"),
        await _seed_txn("FLATTEST ENDPOINT ROW", amount_cents=-200, posted_date="2026-03-05"),
        await _seed_txn("FLATTEST ENDPOINT ROW", amount_cents=-300, posted_date="2026-03-09"),
    ]

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _chat_mock([
        {"type": "recategorize",
         "params": {"search": "flattest endpoint row", "category_path": "Expense > Groceries"}},
    ]))
    message = "flattest endpoint rows are groceries"
    post = test_client.post("/api/chat-correct", json={"message": message})
    assert post.status_code == 200, post.text
    assert sorted(post.json()["affected_ids"]) == sorted(ids)

    correction_id = await _latest_correction_id(message)
    assert correction_id is not None

    resp = test_client.get(f"/api/chat-corrections/{correction_id}/transactions?limit=200")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == correction_id
    assert body["total"] == 3
    assert body["returned"] == 3
    assert body["truncated"] is False
    assert [t["id"] for t in body["transactions"]] == sorted(ids, reverse=True)

    for txn in body["transactions"]:
        assert set(txn.keys()) == TXN_KEYS
        assert txn["merchant"] == "FLATTEST ENDPOINT ROW"
        assert txn["account_id"] == "flat-test-acct"
        assert txn["category_path"] == "Groceries"
    # Newest first by posted_date.
    dates = [t["posted_date"] for t in body["transactions"]]
    assert dates == sorted(dates, reverse=True)
    assert dates[0] == "2026-03-09"


@pytest.mark.asyncio
async def test_correction_transactions_truncates_at_limit(test_client, monkeypatch):
    """``limit`` smaller than the affected set caps rows and flags truncated."""
    await _seed_txn("FLATTEST TRUNCATE ME", amount_cents=-100, posted_date="2026-04-01")
    await _seed_txn("FLATTEST TRUNCATE ME", amount_cents=-200, posted_date="2026-04-02")
    await _seed_txn("FLATTEST TRUNCATE ME", amount_cents=-300, posted_date="2026-04-03")

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _chat_mock([
        {"type": "tag", "params": {"search": "flattest truncate me", "tags": ["flat-test"]}},
    ]))
    message = "tag the truncate rows"
    post = test_client.post("/api/chat-correct", json={"message": message})
    assert post.status_code == 200, post.text
    assert post.json()["affected_count"] == 3

    correction_id = await _latest_correction_id(message)

    resp = test_client.get(f"/api/chat-corrections/{correction_id}/transactions?limit=2")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 3
    assert body["returned"] == 2
    assert body["truncated"] is True
    assert len(body["transactions"]) == 2
    # The two newest of the three.
    assert [t["posted_date"] for t in body["transactions"]] == ["2026-04-03", "2026-04-02"]

    full = test_client.get(
        f"/api/chat-corrections/{correction_id}/transactions?limit=200").json()
    assert full["returned"] == 3
    assert full["truncated"] is False


@pytest.mark.asyncio
async def test_correction_transactions_unknown_id_404(test_client):
    resp = test_client.get("/api/chat-corrections/99999999/transactions")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_correction_transactions_omits_deleted_rows_but_still_counts_them(
        test_client, monkeypatch):
    """A row deleted after the correction drops out of the list, not out of total."""
    keep = await _seed_txn("FLATTEST DELETED PAIR", amount_cents=-100, posted_date="2026-06-01")
    doomed = await _seed_txn("FLATTEST DELETED PAIR", amount_cents=-200, posted_date="2026-06-02")

    monkeypatch.setattr("app.routers.chat_correct.call_llm", _chat_mock([
        {"type": "tag", "params": {"search": "flattest deleted pair", "tags": ["flat-test"]}},
    ]))
    message = "tag the pair that loses a row"
    post = test_client.post("/api/chat-correct", json={"message": message})
    assert post.status_code == 200, post.text
    correction_id = await _latest_correction_id(message)

    db = await get_db()
    try:
        await db.execute("DELETE FROM transactions WHERE id=?", (doomed,))
        await db.commit()
    finally:
        await db.close()

    body = test_client.get(
        f"/api/chat-corrections/{correction_id}/transactions").json()
    assert body["total"] == 2
    assert body["returned"] == 1
    assert [t["id"] for t in body["transactions"]] == [keep]
