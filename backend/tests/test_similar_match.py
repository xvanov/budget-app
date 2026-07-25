"""Applying one manual categorization to the rows naming the same merchant.

Two properties are load-bearing here and both are asserted directly:

1. **No LLM is involved.** The whole reason this path exists is that the
   semantic-match path in ``PATCH /api/transactions/{id}`` takes seconds and
   cannot explain itself. Every test monkeypatches
   ``app.routers.transactions.call_llm`` to raise, so a regression that
   reintroduces a model call fails loudly instead of quietly getting slow.
2. **A false positive corrupts data.** The matcher is allowed to miss rows; it is
   not allowed to invent them. The exclusion tests (transfers, ``Excluded``,
   blank merchants, forged ids) are the real subject of this file.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.merchant_key import merchant_key, merchant_tokens, match_strength


TARGET_CATEGORY = "Groceries"

# Ids this module inserted, so they can be removed again. The test DB is shared
# by the whole session, and these rows deliberately mix signs inside one category
# (a refund on "Groceries") — exactly the shape ``test_trends`` compares against
# the dashboard. Leaving them behind fails a test that has nothing to do with
# this feature.
_INSERTED = []


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    """Any LLM call from this router during these tests is a bug."""
    def explode(*args, **kwargs):
        raise AssertionError("similar-merchant matching must never call an LLM")

    monkeypatch.setattr("app.routers.transactions.call_llm", explode)


@pytest.fixture(autouse=True)
def clean_up_seeded_rows():
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
            merchant, description, category_path, entity_type, business_context)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            kw.get("platform", "test-similar"),
            "similar.csv",
            kw.get("account_id", "card-0001"),
            kw.get("date", "2026-05-01"),
            kw["amount"],
            kw.get("merchant", ""),
            kw.get("description", ""),
            kw.get("category"),
            kw.get("entity_type", "expense"),
            "personal",
        ),
    )
    _INSERTED.append(cursor.lastrowid)
    return cursor.lastrowid


async def seed(tag):
    """One isolated merchant family per test.

    ``tag`` is baked into the merchant name so tests sharing the session DB (and
    the sample data other test modules import into it) cannot match each other's
    rows. Every row below is a real shape seen in the live exports: a Plaid row
    with a clean merchant, a CSV row with the store number and city appended, an
    inflow refund, and the transfer/excluded rows that must be left alone.
    """
    from app.database import get_db

    name = f"Zorptek{tag}"
    db = await get_db()
    try:
        ids = {
            # Source: CSV shape, store number + city tail.
            "source": await _insert(
                db, merchant=f"{name} SUPPLY 00048812 SPRINGFIELD IL", amount=-5000,
                category="Shopping", date="2026-05-01",
            ),
            # Plaid shape, same payee, uncategorized.
            "sibling_clean": await _insert(
                db, merchant=f"{name} Supply", amount=-2500,
                category="Uncategorized", entity_type="uncategorized", date="2026-05-02",
            ),
            # Merchant blank — identity has to come from the description.
            "sibling_desc_only": await _insert(
                db, merchant="", description=f"{name} SUPPLY T- 4412 07/02 PURCHASE Springfield IL",
                amount=-1000, category=None, entity_type="uncategorized", date="2026-05-03",
            ),
            # A refund from the same merchant: positive amount, must stay coherent.
            "sibling_refund": await _insert(
                db, merchant=f"{name} Supply", amount=1500,
                category="Uncategorized", entity_type="uncategorized", date="2026-05-04",
            ),
            # Already where the user is sending everything: not a change.
            "already": await _insert(
                db, merchant=f"{name} Supply", amount=-700,
                category=TARGET_CATEGORY, date="2026-05-05",
            ),
            # Opted out by hand.
            "excluded": await _insert(
                db, merchant=f"{name} Supply", amount=-800,
                category="Excluded", entity_type="excluded", date="2026-05-06",
            ),
            # Internal movement: recategorizing this would double-count money.
            "transfer": await _insert(
                db, merchant=f"{name} Supply", amount=-900,
                category="Transfer", entity_type="transfer", date="2026-05-07",
            ),
            # Different merchant entirely.
            "unrelated": await _insert(
                db, merchant=f"Blarnix{tag} Cafe", amount=-400,
                category="Uncategorized", entity_type="uncategorized", date="2026-05-08",
            ),
        }
        await db.commit()
        return ids
    finally:
        await db.close()


async def fetch(txn_id):
    from app.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM transactions WHERE id=?", (txn_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


# --------------------------------------------------------------------------- #
# merchant_key
# --------------------------------------------------------------------------- #

def test_store_number_and_city_tail_key_the_same_as_the_clean_merchant():
    """The CSV row and its Plaid twin are one merchant, so they must be one key."""
    assert merchant_key("TARGET 00023991 SPRINGFIELD IL", None) == merchant_key("Target", None)
    assert merchant_key("Target", None) == "target"


def test_leading_terminal_number_does_not_truncate_the_payee_away():
    """A digit run before anything distinctive is not a location boundary."""
    assert "target" in merchant_key("POS DEBIT 1234 TARGET SPRINGFIELD", None)


def test_two_zelle_payees_with_identical_rail_noise_stay_distinct():
    """The rail is shared; the payee is the identity. Fusing these would move a
    payment to one person into another person's category."""
    a = merchant_key("Zelle payment to Teresa Navarro Confirmation# ab12cd", None)
    b = merchant_key("Zelle payment to Ana Reyes Confirmation# zz99qq", None)
    assert a == "teresa navarro"
    assert b == "ana reyes"
    assert a != b
    assert match_strength(a, merchant_tokens(a, None), b, merchant_tokens(b, None)) is None


def test_confirmation_code_only_difference_collapses_to_one_key():
    """Two months of the same recurring transfer differ only by the code."""
    jan = merchant_key("Online transfer to Koshiya Confirmation# cutdyyakh", None)
    feb = merchant_key("Online transfer to Koshiya Confirmation# qzlyirds", None)
    assert jan == feb == "koshiya"


def test_pure_noise_merchant_has_no_key():
    """Nothing distinctive survived, so no match is possible — not 'match all'."""
    assert merchant_key("ONLINE TRANSFER TO CHK", None) == ""
    assert merchant_key("Payment Thank You - Web", None) == ""
    assert merchant_key("", None) == ""
    assert merchant_key(None, None) == ""


def test_payee_is_recovered_from_the_description_when_the_merchant_is_just_the_rail():
    """Live rows arrive with merchant='Zelle' and the payee only in the description.

    Keying on the merchant field alone gives an empty key — no match — while the
    name was sitting in the description. Both shapes must reach the same payee.
    """
    # A 4+ character payee on purpose: merchant_key rejects a key with no token of
    # at least four characters, which is what stops a 2-3 letter fragment becoming a
    # rule that matches a fifth of the ledger. A 3-letter payee is legitimately
    # unmatchable, so it cannot be the fixture for "the payee IS recovered".
    from_rail = merchant_key("Zelle", "Zelle payment to Delgado 20729587790")
    from_plaid = merchant_key("Delgado", "Delgado")
    assert from_rail == from_plaid == "delgado"

    # Toast's card prefix, same problem: 61 live rows had merchant='TST'.
    assert merchant_key("TST", "TST*AJUMMA") == "ajumma"

    # The fallback must not manufacture a key out of noise on both sides.
    assert merchant_key("Account Transfer", "Online Transfer to CHK ...0001 transaction") == ""


def test_match_strength_levels():
    def k(text):
        return merchant_key(text, None), merchant_tokens(text, None)

    same_a, same_b = k("Costco"), k("COSTCO 1188 SPRINGFIELD IL")
    assert match_strength(*same_a, *same_b) == "exact"

    sub_a, sub_b = k("Amazon"), k("Amazon Marketplace")
    assert match_strength(*sub_a, *sub_b) == "strong"

    # An empty key can never match, whatever is on the other side.
    assert match_strength("", set(), *sub_b) is None


# --------------------------------------------------------------------------- #
# GET /api/transactions/{id}/similar
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_similar_finds_siblings_and_applies_every_exclusion(test_client):
    ids = await seed("Alfa")

    resp = test_client.get(
        f"/api/transactions/{ids['source']}/similar",
        params={"category_path": TARGET_CATEGORY},
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["source"]["id"] == ids["source"]
    assert data["source"]["category_path"] == "Shopping"
    assert data["match_key"] == "zorptekalfa supply"
    assert data["suggested_pattern"] == "zorptekalfa supply"
    assert data["match_label"] == "ZorptekAlfa SUPPLY 00048812 SPRINGFIELD IL"

    got = {m["id"] for m in data["matches"]}
    assert got == {ids["sibling_clean"], ids["sibling_desc_only"], ids["sibling_refund"]}
    for excluded_key in ("source", "already", "excluded", "transfer", "unrelated"):
        assert ids[excluded_key] not in got, f"{excluded_key} must not be offered"

    assert data["count"] == 3
    assert data["returned"] == 3
    assert data["truncated"] is False
    assert all(m["strength"] in ("exact", "strong") for m in data["matches"])
    # posted_date DESC.
    assert [m["posted_date"] for m in data["matches"]] == ["2026-05-04", "2026-05-03", "2026-05-02"]
    # The row shape the frontend is built against.
    assert set(data["matches"][0]) == {
        "id", "posted_date", "merchant", "description", "amount_cents",
        "account_id", "category_path", "strength",
    }


@pytest.mark.asyncio
async def test_similar_truncates_without_lying_about_the_total(test_client):
    ids = await seed("Bravo")

    resp = test_client.get(
        f"/api/transactions/{ids['source']}/similar",
        params={"category_path": TARGET_CATEGORY, "limit": 1},
    )
    data = resp.json()
    assert data["count"] == 3
    assert data["returned"] == 1
    assert data["truncated"] is True


@pytest.mark.asyncio
async def test_similar_on_a_noise_merchant_matches_nothing(test_client):
    """A blank/noise merchant must not degrade into 'everything matches'."""
    from app.database import get_db

    await seed("Charlie")
    db = await get_db()
    try:
        noise_id = await _insert(
            db, merchant="ONLINE TRANSFER TO CHK", description="Online Transfer to CHK ...0001",
            amount=-2000, category="Uncategorized", entity_type="uncategorized",
        )
        await db.commit()
    finally:
        await db.close()

    resp = test_client.get(
        f"/api/transactions/{noise_id}/similar",
        params={"category_path": TARGET_CATEGORY},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["match_key"] == ""
    assert data["count"] == 0
    assert data["matches"] == []


@pytest.mark.asyncio
async def test_similar_labels_a_rail_row_by_its_payee_not_the_network(test_client):
    """merchant='Zelle' must not present the group as "Zelle" — that names no payee."""
    from app.database import get_db

    db = await get_db()
    try:
        rail_id = await _insert(
            db, merchant="Zelle", description="Zelle payment to Fibbly Tenant JPM99cpuh",
            amount=-2500, category="Other", date="2026-06-01",
        )
        twin_id = await _insert(
            db, merchant="Fibbly", description="Zelle payment to Fibbly Tenant JPM99cpgk",
            amount=-2500, category="Uncategorized", entity_type="uncategorized",
            date="2026-06-02",
        )
        await db.commit()
    finally:
        await db.close()

    data = test_client.get(
        f"/api/transactions/{rail_id}/similar", params={"category_path": TARGET_CATEGORY}
    ).json()
    assert data["match_key"] == "fibbly tenant"
    assert data["match_label"] == "Fibbly Tenant"
    assert twin_id in {m["id"] for m in data["matches"]}


@pytest.mark.asyncio
async def test_similar_on_a_missing_transaction_is_404(test_client):
    resp = test_client.get(
        "/api/transactions/99999999/similar", params={"category_path": TARGET_CATEGORY}
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# POST /api/transactions/apply-similar
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_apply_similar_with_explicit_ids_updates_exactly_those(test_client):
    ids = await seed("Delta")

    resp = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["source"],
        "category_path": TARGET_CATEGORY,
        "ids": [ids["sibling_clean"], ids["sibling_desc_only"]],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["updated_count"] == 2
    assert set(data["updated_ids"]) == {ids["sibling_clean"], ids["sibling_desc_only"]}
    assert data["skipped_count"] == 0
    assert data["rule_id"] is None

    for key in ("sibling_clean", "sibling_desc_only"):
        row = await fetch(ids[key])
        assert row["category_path"] == TARGET_CATEGORY
        assert row["entity_type"] == "expense"  # derived from the negative amount
        assert row["confidence_score"] == 1.0
        assert f"#{ids['source']}" in row["categorization_reason"]

    # Not ticked, so not touched.
    untouched = await fetch(ids["sibling_refund"])
    assert untouched["category_path"] == "Uncategorized"


@pytest.mark.asyncio
async def test_apply_similar_all_matching_updates_every_match(test_client):
    ids = await seed("Echo")

    resp = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["source"],
        "category_path": TARGET_CATEGORY,
        "ids": [],
        "all_matching": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert set(data["updated_ids"]) == {
        ids["sibling_clean"], ids["sibling_desc_only"], ids["sibling_refund"]
    }
    assert data["updated_count"] == 3

    for key in ("excluded", "transfer", "unrelated"):
        row = await fetch(ids[key])
        assert row["category_path"] != TARGET_CATEGORY, f"{key} was recategorized"


@pytest.mark.asyncio
async def test_apply_similar_refuses_ids_that_are_not_matches(test_client):
    """A stale or hostile client must not be able to write through this endpoint."""
    ids = await seed("Foxtrot")

    resp = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["source"],
        "category_path": TARGET_CATEGORY,
        "ids": [ids["sibling_clean"], ids["unrelated"]],
    })
    assert resp.status_code == 400
    assert str(ids["unrelated"]) in resp.json()["detail"]

    # Nothing was written, including the id that WAS a genuine match.
    assert (await fetch(ids["sibling_clean"]))["category_path"] == "Uncategorized"
    assert (await fetch(ids["unrelated"]))["category_path"] == "Uncategorized"


@pytest.mark.asyncio
async def test_apply_similar_refuses_transfer_and_excluded_ids(test_client):
    ids = await seed("Golf")

    for key in ("transfer", "excluded"):
        resp = test_client.post("/api/transactions/apply-similar", json={
            "source_id": ids["source"],
            "category_path": TARGET_CATEGORY,
            "ids": [ids[key]],
        })
        assert resp.status_code == 400, f"{key} row was accepted"


@pytest.mark.asyncio
async def test_apply_similar_keeps_a_positive_row_coherent(test_client):
    """A refund from the merchant lands on the category as an inflow, not nonsense.

    Under the flat model a positive amount on ``Groceries`` nets against
    groceries, so the correct outcome is ``entity_type='income'`` with the
    category kept — never a coercion to ``Uncategorized`` and never
    ``entity_type='expense'`` on a +$15.00 row.
    """
    ids = await seed("Hotel")

    resp = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["source"],
        "category_path": TARGET_CATEGORY,
        "ids": [ids["sibling_refund"]],
    })
    assert resp.status_code == 200
    assert resp.json()["updated_count"] == 1

    row = await fetch(ids["sibling_refund"])
    assert row["amount_cents"] == 1500
    assert row["category_path"] == TARGET_CATEGORY
    assert row["entity_type"] == "income"


@pytest.mark.asyncio
async def test_apply_similar_create_rule_persists_a_rule_and_a_context_fact(test_client):
    ids = await seed("India")

    resp = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["source"],
        "category_path": TARGET_CATEGORY,
        "ids": [],
        "all_matching": True,
        "create_rule": True,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["rule_id"] is not None

    from app.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT pattern, category_path FROM rules WHERE id=?", (data["rule_id"],)
        )
        rule = await cursor.fetchone()
    finally:
        await db.close()
    assert rule["pattern"] == "zorptekindia supply"
    assert rule["category_path"] == TARGET_CATEGORY

    assert data["context_updated"] is True
    context = test_client.get("/api/context").json()["content"]
    assert "zorptekindia supply" in context.lower()


@pytest.mark.asyncio
async def test_apply_similar_rejects_a_legacy_hierarchical_category(test_client):
    ids = await seed("Juliet")

    resp = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["source"],
        "category_path": "Expense > Personal > Groceries",
        "ids": [ids["sibling_clean"]],
    })
    assert resp.status_code == 400
    assert (await fetch(ids["sibling_clean"]))["category_path"] == "Uncategorized"


@pytest.mark.asyncio
async def test_apply_similar_does_not_stack_duplicate_rules(test_client):
    """Accepting the offer twice for one merchant reuses the rule it already made.

    The offer appears on every inline category edit, so a user who fixes two rows
    from the same merchant in a row would otherwise end up with two identical
    rules — which is exactly how the live DB ended up with rules #25 and #26 both
    saying `target`.
    """
    ids = await seed("Dedupe")

    first = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["source"], "category_path": TARGET_CATEGORY,
        "ids": [ids["sibling_clean"]], "all_matching": False, "create_rule": True,
    })
    assert first.status_code == 200, first.text
    rule_id = first.json()["rule_id"]
    assert rule_id is not None

    second = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["source"], "category_path": TARGET_CATEGORY,
        "ids": [ids["sibling_refund"]], "all_matching": False, "create_rule": True,
    })
    assert second.status_code == 200, second.text
    assert second.json()["rule_id"] == rule_id, "second apply must reuse the same rule"

    rules = test_client.get("/api/rules").json()["rules"]
    pattern = first.json().get("rule_pattern") or "zorptekdedupe supply"
    dupes = [r for r in rules
             if r["pattern"] == pattern and r["category_path"] == TARGET_CATEGORY]
    assert len(dupes) == 1, f"expected one rule for {pattern!r}, got {dupes}"


# --------------------------------------------------------------------------- #
# A key too short to be distinctive is worse than no key at all.
#
# Live incident: merchant_key("US MOBILE") returned "us" ("mobile" is a stop
# word, "us" cleared the old 2-char floor). That key was offered as a rule
# pattern, the rule was created, and because apply_rules matches
# LIKE '%pattern%' against merchant OR description it hit 1,208 rows — Costco
# Wholesale, anything spelling "USA"/"USB"/"Super"/"Cushions". These tests pin
# both halves of the fix: the key is never emitted, and the rule is never
# written even if the pattern arrives from somewhere else.
# --------------------------------------------------------------------------- #

async def _rule_count():
    from app.database import get_db

    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM rules")
        return (await cursor.fetchone())[0]
    finally:
        await db.close()


def test_a_two_char_token_never_becomes_a_key():
    """"us" is not a merchant; it is a substring of a large share of the ledger."""
    assert merchant_key("US MOBILE", None) == ""
    # Specifically: it must not be "us". The old 2-char floor let it through and
    # the resulting rule matched 1,208 rows.
    assert merchant_key("US MOBILE", None) != "us"
    # Unmatchable is the correct answer here, not a looser fallback: no
    # propagation is offered, rather than propagation to everything.
    assert match_strength(
        *(merchant_key("US MOBILE", None), merchant_tokens("US MOBILE", None)),
        *(merchant_key("Costco Wholesale", None), merchant_tokens("Costco Wholesale", None)),
    ) is None

    # Same stop-word interaction, one token shorter.
    assert merchant_key("T-Mobile", None) == ""


def test_a_key_of_only_short_tokens_is_discarded():
    """Three characters still matches indiscriminately as `LIKE '%abc%'`.

    A leftover rail/network prefix that is not in ``MERCHANT_STOP`` yet reduces
    to exactly this shape, so the key needs at least one token long enough to
    name somebody.
    """
    # Every surviving token is 3 chars — nothing distinctive, so no key.
    assert merchant_key("ABC XYZ", None) == ""
    assert merchant_key("QRS", "QRS") == ""
    # One token of 4+ is enough to make the key trustworthy again.
    assert merchant_key("ABCD XYZ", None) == "abcd xyz"


def test_the_length_guards_do_not_break_the_keys_that_must_keep_working():
    """Real shapes from the live exports. Every one of these has to still cluster."""
    # Store number + city tail vs the clean Plaid merchant.
    assert merchant_key("TARGET 00023991 SPRINGFIELD IL", None) == "target"
    assert merchant_key("Target", None) == "target"

    # Three spellings of one restaurant: the two card rows key identically, and
    # the Plaid row is a superset, which match_strength reads as "strong".
    card_long = merchant_key("CHIPOTLE 1791 SPRINGFIELD IL", None)
    card_short = merchant_key("CHIPOTLE 1791", None)
    plaid = merchant_key("Chipotle Mexican Grill", None)
    assert card_long == card_short == "chipotle"
    assert plaid == "chipotle mexican grill"
    assert match_strength(
        card_long, merchant_tokens("CHIPOTLE 1791 SPRINGFIELD IL", None),
        plaid, merchant_tokens("Chipotle Mexican Grill", None),
    ) == "strong"

    # The seeded family this module's endpoint tests rely on.
    assert (merchant_key("Zorptek SUPPLY 00048812 SPRINGFIELD IL", None)
            == merchant_key("Zorptek Supply", None) == "zorptek supply")

    # Two Zelle payees behind identical rail noise stay two merchants.
    navarro = merchant_key("Zelle payment to Teresa Navarro Confirmation# ab12cd", None)
    ana = merchant_key("Zelle payment to Ana Reyes Confirmation# zz99qq", None)
    assert navarro == "teresa navarro"
    assert ana == "ana reyes"
    assert navarro != ana

    # Pure noise still names nobody.
    assert merchant_key("ONLINE TRANSFER TO CHK", None) == ""


# --------------------------------------------------------------------------- #
# rules_service.add_rule — the second line of defence
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("pattern", ["us", "a", "", "   ", "abc"])
@pytest.mark.asyncio
async def test_add_rule_refuses_a_pattern_too_short_to_be_safe(pattern):
    """merchant_key is not the only source of patterns.

    chat-correct hands an LLM-supplied ``search`` string straight to
    ``add_rule``, and a model can propose "us" just as easily as the tokenizer
    did. The write itself has to refuse, and refuse without leaving a row.
    """
    from app.database import get_db
    from app.services.rules_service import add_rule

    before = await _rule_count()
    db = await get_db()
    try:
        with pytest.raises(ValueError):
            await add_rule(db, pattern, TARGET_CATEGORY, "should never be persisted")
    finally:
        await db.close()
    assert await _rule_count() == before, f"a rule was persisted for {pattern!r}"


@pytest.mark.asyncio
async def test_add_rule_still_accepts_a_normal_pattern():
    """The guard is a floor on length, not a new approval step."""
    from app.database import get_db
    from app.services.rules_service import add_rule

    before = await _rule_count()
    rule_id = None
    db = await get_db()
    try:
        rule_id = await add_rule(db, "target", TARGET_CATEGORY, "normal pattern")
        assert rule_id is not None
        assert await _rule_count() == before + 1

        cursor = await db.execute("SELECT pattern FROM rules WHERE id=?", (rule_id,))
        assert (await cursor.fetchone())["pattern"] == "target"
    finally:
        # A live "target" rule would auto-categorize sample rows other modules
        # assert against, so it must not outlive this test.
        if rule_id is not None:
            await db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            await db.commit()
        await db.close()
    assert await _rule_count() == before


@pytest.mark.asyncio
async def test_apply_similar_with_create_rule_on_a_non_distinctive_merchant(test_client):
    """The endpoint must survive a merchant that keys to nothing, and write no rule.

    This is the end of the incident path: the user edits a "US MOBILE" row and
    accepts the "also apply to similar" offer. Before the guards that created
    rule pattern ``us``. Now there is no key, so there is nothing to offer and
    nothing to persist — and the user's own edit must still not 500.
    """
    from app.database import get_db

    db = await get_db()
    try:
        source_id = await _insert(
            db, merchant="US MOBILE", description="US MOBILE 07/12 PURCHASE",
            amount=-3200, category="Other", date="2026-06-10",
        )
        sibling_id = await _insert(
            db, merchant="US MOBILE", description="US MOBILE 08/12 PURCHASE",
            amount=-3200, category="Uncategorized", entity_type="uncategorized",
            date="2026-07-10",
        )
        await db.commit()
    finally:
        await db.close()

    before = await _rule_count()

    resp = test_client.post("/api/transactions/apply-similar", json={
        "source_id": source_id,
        "category_path": TARGET_CATEGORY,
        "ids": [],
        "all_matching": True,
        "create_rule": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["rule_id"] is None
    assert data["updated_count"] == 0
    assert await _rule_count() == before, "a rule was created from a non-distinctive key"

    # No key means no matches, so the sibling is left for the LLM / the user.
    assert (await fetch(sibling_id))["category_path"] == "Uncategorized"

    # And the offer itself reports honestly that there is nothing to apply.
    similar = test_client.get(
        f"/api/transactions/{source_id}/similar",
        params={"category_path": TARGET_CATEGORY},
    ).json()
    assert similar["match_key"] == ""
    assert similar["suggested_pattern"] == ""
    assert similar["count"] == 0
