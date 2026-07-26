"""Propagating a category from an Amazon ITEM row matches the PRODUCT, not "Amazon".

The bug, in the user's words: "I want to apply the same rule to the other ones
like it — based on what the ITEM was, not the description. Right now it matches
broadly across anything with Amazon in the name… This is a recurring cat-litter
purchase; I want it to automatically match all the actual cat litter stuff, not 57
unrelated items. And the list just shows 'Amazon' — it doesn't show what the other
ones are."

Measured on the real snapshot before the fix: filing txn 8760
(``Amazon: Fresh Step Clean Paws Advanced Multi Cat Clumping Litter wit``, −$35.84)
under Pets offered **56 matches** — 51 of them bare ``Amazon`` rows of amounts from
−$3.63 to −$432.33, four unrelated products (tracing paper, Peru toothpick flags,
blackout curtains, a Halloween costume), one +$18.41 refund — and not one row of
cat litter. The cause: every item row is named ``Amazon: <product>``, so its
merchant key contains ``amazon`` and ``match_strength`` scores a bare ``Amazon``
row a subset "strong" match, while the product tokens decide nothing. The seven
OTHER cat-litter purchases were excluded outright, because a row linked to
``amazon_items`` was treated as untouchable.

After: 7 matches, every one the same ASIN (``B07ZK3DZB3``), each labelled with its
product name.

The fixture below is the snapshot's shape, with its real ASIN, both of the real
listing-text variants Amazon returned for it, the real purchase amounts, and the
real noise rows that used to crowd the list. Same-ASIN-different-name is the case
no string comparison can get right and the reason ASIN is consulted first.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.amazon_product_key import (
    normalize_product_name,
    pattern_matches_every_item_row,
    product_identity,
    product_name_from_merchant,
    safe_rule_pattern,
    same_product,
)

_TXNS = []
_ITEMS = []
_REFUNDS = []
_RULE_PATTERNS = []

# The real ASIN and the two real listing texts for it, from the snapshot.
LITTER_ASIN = "B07ZK3DZB3"
LITTER_NAME_A = ("Fresh Step Clean Paws Advanced Multi Cat Clumping Litter with "
                 "Febreze Freshness, 18.5 lb Each, Pack of 2 (Package May Vary)")
LITTER_NAME_B = ("Fresh Step Advanced Clean Paws Multi Cat Clumping Cat Litter with "
                 "Febreze Freshness, Low Dust, Odor Control with Ammonia Block Tech, "
                 "Compatible in Automatic Litter Boxes, 37 lbs. (2 x 18.5 lb.)")


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("product matching is deterministic; no LLM may be called")

    monkeypatch.setattr("app.routers.transactions.call_llm", explode)


@pytest.fixture(autouse=True)
def clean_up():
    yield
    con = sqlite3.connect(os.environ["DB_PATH"])
    try:
        for table, ids in (("transactions", _TXNS), ("amazon_items", _ITEMS),
                           ("amazon_refunds", _REFUNDS)):
            if ids:
                con.execute(
                    f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(ids))})", ids)
        for pattern in _RULE_PATTERNS:
            con.execute("DELETE FROM rules WHERE pattern=?", (pattern,))
        con.commit()
    finally:
        con.close()
        for bucket in (_TXNS, _ITEMS, _REFUNDS, _RULE_PATTERNS):
            bucket.clear()


async def _txn(db, **kw):
    cursor = await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            merchant, description, category_path, entity_type, business_context,
            linked_transaction_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "plaid", "amazon-product.csv", kw.get("account_id", "card-0001"),
            kw.get("date", "2031-04-01"), kw["amount"], kw.get("merchant", ""),
            kw.get("description", kw.get("merchant", "")), kw.get("category"),
            kw.get("entity_type", "expense"), "personal", kw.get("linked"),
        ),
    )
    _TXNS.append(cursor.lastrowid)
    return cursor.lastrowid


async def _item(db, *, txn_id, asin, product_name, total_cents):
    """An ``amazon_items`` row linked the way a single-item order really is.

    ``split_matched_charges`` relabels the charge itself for a one-item order
    instead of creating a child, so ``matched_transaction_id`` and
    ``split_transaction_id`` are both that charge — the shape all 335 linked item
    rows in the snapshot have.
    """
    cursor = await db.execute(
        """INSERT INTO amazon_items
           (order_id, owner, asin, product_name, total_cents, matched_transaction_id,
            split_transaction_id, match_strategy, fingerprint)
           VALUES (?,?,?,?,?,?,?,'shipment',?)""",
        (f"112-PROD-{txn_id}", "kalin", asin, product_name, total_cents,
         txn_id, txn_id, f"fp-product-key-{txn_id}"),
    )
    _ITEMS.append(cursor.lastrowid)
    return cursor.lastrowid


async def seed():
    """The cat-litter family plus every kind of row that used to crowd its list."""
    from app.database import get_db

    db = await get_db()
    try:
        ids = {}
        # The source: one recurring cat-litter purchase, listing text variant A.
        ids["source"] = await _txn(
            db, merchant=f"Amazon: {LITTER_NAME_A[:60]}", description=LITTER_NAME_A,
            amount=-3584, category="Other", date="2031-04-02")
        await _item(db, txn_id=ids["source"], asin=LITTER_ASIN,
                    product_name=LITTER_NAME_A, total_cents=3584)

        # Same product, Amazon's other listing text for it, three real amounts.
        for key, amount, day in (("litter_b1", -3063, "2031-04-03"),
                                 ("litter_b2", -2868, "2031-04-04"),
                                 ("litter_b3", -2506, "2031-04-05")):
            ids[key] = await _txn(
                db, merchant=f"Amazon: {LITTER_NAME_B[:60]}",
                description=LITTER_NAME_B, amount=amount, category="Other", date=day)
            await _item(db, txn_id=ids[key], asin=LITTER_ASIN,
                        product_name=LITTER_NAME_B, total_cents=-amount)

        # Same product, same listing text, but its links were cleared by
        # ``reset_matches`` — 225 rows in the snapshot are in exactly this state.
        # Only the merchant string is left, and it is a 60-character truncation that
        # ends mid-word ("…Clumping Litter wit"), which is what the name fallback has
        # to cope with.
        ids["litter_orphan"] = await _txn(
            db, merchant=f"Amazon: {LITTER_NAME_A[:60]}", amount=-3383,
            category="Other", date="2031-04-06")

        # Same product, DIFFERENT listing text, and no ASIN to say so. Deliberately
        # NOT matched: with the link gone there is nothing left that ties this text
        # to the source's text, and inventing a match from two differently-worded
        # names is how a matcher starts fusing separate SKUs. Missing a row is
        # recoverable; writing the wrong one is not.
        ids["litter_variant_orphan"] = await _txn(
            db, merchant=f"Amazon: {LITTER_NAME_B[:60]}", amount=-3063,
            category="Other", date="2031-04-06")

        # A different product entirely, with its own ASIN.
        ids["tracing_paper"] = await _txn(
            db, merchant="Amazon: 120 Sheets Tracing Paper for Drawing, 8.5\"x 11\"",
            amount=-859, category="Other", date="2031-04-07")
        await _item(db, txn_id=ids["tracing_paper"], asin="B08TRACING1",
                    product_name="120 Sheets Tracing Paper for Drawing, 8.5\"x 11\"",
                    total_cents=859)

        # Bare Amazon charges — the rows that made up 51 of the 56 offered matches.
        for key, amount in (("bare_small", -2796), ("bare_mid", -8290),
                            ("bare_big", -43233)):
            ids[key] = await _txn(db, merchant="Amazon", description="AMAZON MKTPL*XYZ",
                                  amount=amount, category="Other", date="2031-04-08")

        # A refund whose category comes from what was actually returned.
        ids["refund"] = await _txn(
            db, merchant="Amazon", description="AMAZON.COM REFUND", amount=1841,
            category="Other", entity_type="income", date="2031-04-09")
        cursor = await db.execute(
            """INSERT INTO amazon_refunds
               (order_id, owner, refund_date, amount_cents, matched_transaction_id,
                fingerprint)
               VALUES ('112-PROD-REFUND', 'kalin', '2031-04-09', 1841, ?, ?)""",
            (ids["refund"], f"fp-product-refund-{ids['refund']}"),
        )
        _REFUNDS.append(cursor.lastrowid)

        # A split parent and its residual line: neither is a product.
        ids["parent"] = await _txn(
            db, merchant="Amazon", description="AMAZON MKTPL*SPLIT", amount=-3000,
            category="Transfer", entity_type="transfer", date="2031-04-10")
        ids["residual"] = await _txn(
            db, merchant="Amazon: Tax & Shipping", description="Tax & Shipping",
            amount=-500, category="Other", linked=ids["parent"], date="2031-04-10")
        # ...and one item INSIDE that multi-item order which is the same product as
        # the source. It is a split child, which used to be the only reason a child
        # could ever be offered; now it is offered because of what it IS.
        ids["parent_item"] = await _txn(
            db, merchant=f"Amazon: {LITTER_NAME_A[:60]}", amount=-2500,
            category="Other", linked=ids["parent"], date="2031-04-10")

        # A non-Amazon merchant, to prove that path is untouched.
        ids["target_a"] = await _txn(
            db, merchant="TARGET 00048812 SPRINGFIELD IL", amount=-4400,
            category="Other", date="2031-04-11")
        ids["target_b"] = await _txn(
            db, merchant="Target", amount=-1200, category="Other", date="2031-04-12")

        await db.commit()
        return ids
    finally:
        await db.close()


def similar(test_client, txn_id, category="Pets"):
    resp = test_client.get(f"/api/transactions/{txn_id}/similar",
                           params={"category_path": category})
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# the key itself
# --------------------------------------------------------------------------- #

def test_an_item_row_is_recognised_from_its_merchant_alone():
    assert product_name_from_merchant("Amazon: Genuine Toyota Oil Filter") == \
        "Genuine Toyota Oil Filter"
    # A bare charge names no product. This is the whole defect in one assertion.
    assert product_name_from_merchant("Amazon") == ""
    assert product_name_from_merchant("AMAZON MKTPL*RL5QE5SU3") == ""
    assert product_name_from_merchant("Target") == ""
    assert not product_identity(merchant="Amazon")
    assert not product_identity(merchant="Target 00048812 SPRINGFIELD IL")


def test_normalization_survives_punctuation_and_case():
    assert normalize_product_name("Fresh Step® 18.5 lb.  Box") == "fresh step 18 5 lb box"


def test_the_same_asin_is_the_same_product_whatever_the_listing_says():
    """The case no string comparison can reach — and it is the user's case."""
    a = product_identity(LITTER_ASIN, LITTER_NAME_A)
    b = product_identity(LITTER_ASIN, LITTER_NAME_B)
    assert normalize_product_name(LITTER_NAME_A) != normalize_product_name(LITTER_NAME_B)
    assert same_product(a, b)


def test_different_asins_are_different_products_even_if_the_text_matches():
    a = product_identity("B07ZK3DZB3", LITTER_NAME_A)
    b = product_identity("B0XXXXXXXX", LITTER_NAME_A)
    assert not same_product(a, b)


def test_a_truncated_merchant_matches_the_full_product_name():
    """``merchant = "Amazon: " + name[:60]`` cuts mid-word about a fifth of the time.

    The real snapshot's merchant for this product ends "…Clumping Litter wit".
    """
    truncated = product_identity(merchant=f"Amazon: {LITTER_NAME_A[:60]}")
    assert truncated.name.endswith("Litter wit")
    assert same_product(truncated, product_identity(None, LITTER_NAME_A))
    # ...and a mid-word fragment is not a licence to match a different product.
    assert not same_product(truncated, product_identity(None, LITTER_NAME_B))


def test_a_different_product_of_the_same_brand_is_not_a_match():
    litter = product_identity(None, "Fresh Step Clumping Cat Litter Unscented 25 lb")
    other = product_identity(None, "Fresh Step Odor Shield Litter Box Deodorizer")
    assert not same_product(litter, other)


def test_the_residual_line_of_a_split_is_not_a_product():
    """A "Tax & Shipping" row takes its own order's category, so it must not spread."""
    assert not product_identity(merchant="Amazon: Tax & Shipping")
    assert not product_identity(None, "Tax & Shipping")


def test_a_rule_pattern_is_a_long_literal_prefix_or_nothing():
    pattern = safe_rule_pattern(product_identity(LITTER_ASIN, LITTER_NAME_A))
    assert pattern and pattern in LITTER_NAME_A.lower(), (
        "the pattern is matched with LIKE against the raw text, so it must be a "
        "literal substring of it")
    assert len(pattern) >= 16
    assert "%" not in pattern and "_" not in pattern

    # A real product in the live data: the '%' would be a LIKE wildcard, not a
    # character, and nothing in the rules list would say so.
    assert safe_rule_pattern(product_identity(
        None, "100% Blackout Curtains for Bedroom, Portable DIY")) == ""
    # Too short to name one product.
    assert safe_rule_pattern(product_identity(None, "Cat Litter")) == ""


def test_a_pattern_that_claims_every_item_row_is_refused():
    assert pattern_matches_every_item_row("amazon")
    assert pattern_matches_every_item_row("Amazon:")
    # Amazon-family strings that do NOT match "Amazon: <product>" rows stay usable.
    assert not pattern_matches_every_item_row("amzn mktp")
    assert not pattern_matches_every_item_row("whole foods")
    assert not pattern_matches_every_item_row("")


# --------------------------------------------------------------------------- #
# GET /{id}/similar
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_an_item_row_matches_only_the_same_product(test_client):
    """The cat-litter case: 56 matches of nothing in particular become the product's own."""
    ids = await seed()
    data = similar(test_client, ids["source"])

    assert data["match_kind"] == "amazon_product"
    assert data["source"]["asin"] == LITTER_ASIN
    got = {m["id"] for m in data["matches"]}

    expected = {ids["litter_b1"], ids["litter_b2"], ids["litter_b3"],
                ids["litter_orphan"], ids["parent_item"]}
    assert got == expected, (
        "same-ASIN rows, the reset-orphaned row with the same product name, and the "
        "item row inside a multi-item order — and nothing else. What identifies a "
        "match is the product, not whether it happens to be a split child.")

    for key in ("bare_small", "bare_mid", "bare_big"):
        assert ids[key] not in got, (
            "a bare 'Amazon' charge names no product and must never be offered as a "
            "match for one")
    assert ids["tracing_paper"] not in got, "a different product is not a match"
    assert ids["refund"] not in got, "a refund's category comes from what was returned"
    assert ids["parent"] not in got, "a split parent is counted through its items"
    assert ids["residual"] not in got, "'Tax & Shipping' is not a product"
    assert ids["target_a"] not in got and ids["target_b"] not in got
    assert ids["litter_variant_orphan"] not in got, (
        "no ASIN and differently-worded text: the matcher may miss, it may not guess")


@pytest.mark.asyncio
async def test_the_review_list_says_what_each_match_is(test_client):
    """A column of identical "Amazon" rows is a dialog nobody can actually review."""
    ids = await seed()
    data = similar(test_client, ids["source"])

    assert data["match_label"] == LITTER_NAME_A, "the group is named after the product"
    for m in data["matches"]:
        assert m["product_name"], f"match {m['id']} has no product name to show"
        assert "Fresh Step" in m["product_name"]
    # count stays the honest total of what an apply would touch
    assert data["count"] == len(data["matches"]) == 5
    assert data["truncated"] is False


@pytest.mark.asyncio
async def test_the_rule_offered_for_a_product_is_the_product_not_amazon(test_client):
    ids = await seed()
    data = similar(test_client, ids["source"])

    pattern = data["suggested_pattern"]
    assert pattern
    assert "amazon" not in pattern
    assert pattern in LITTER_NAME_A.lower()
    assert data["rule_note"] is None


@pytest.mark.asyncio
async def test_a_bare_amazon_charge_is_offered_no_rule_at_all(test_client):
    """Its merchant key is 'amazon', and that pattern claims every item row.

    ``apply_rules`` runs on uncategorized rows at every import, so such a rule
    would overwrite the per-product categories before the item export is even read.
    The rows in the list are still applied; the rule is not persisted, and the
    response says why.
    """
    ids = await seed()
    data = similar(test_client, ids["bare_small"], category="Shopping")

    assert data["match_kind"] == "merchant"
    assert data["suggested_pattern"] == ""
    assert data["rule_note"] and "Amazon" in data["rule_note"]
    # It still finds the other bare charges — that part was never wrong.
    got = {m["id"] for m in data["matches"]}
    assert ids["bare_mid"] in got and ids["bare_big"] in got
    # ...and still does not sweep the product rows.
    for key in ("source", "litter_b1", "litter_orphan", "tracing_paper"):
        assert ids[key] not in got


@pytest.mark.asyncio
async def test_a_non_amazon_merchant_behaves_exactly_as_before(test_client):
    ids = await seed()
    data = similar(test_client, ids["target_a"], category="Shopping")

    assert data["match_kind"] == "merchant"
    assert data["match_key"] == "target"
    assert data["suggested_pattern"] == "target"
    assert data["rule_note"] is None
    got = {m["id"] for m in data["matches"]}
    assert ids["target_b"] in got
    assert not got & {ids["source"], ids["bare_small"], ids["tracing_paper"]}
    assert all(m["product_name"] is None for m in data["matches"])


# --------------------------------------------------------------------------- #
# POST /apply-similar
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_apply_similar_from_a_product_writes_only_that_product(test_client):
    ids = await seed()
    _RULE_PATTERNS.append(safe_rule_pattern(product_identity(LITTER_ASIN, LITTER_NAME_A)))

    resp = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["source"], "category_path": "Pets",
        "ids": [], "all_matching": True, "create_rule": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert set(data["updated_ids"]) == {ids["litter_b1"], ids["litter_b2"],
                                        ids["litter_b3"], ids["litter_orphan"],
                                        ids["parent_item"]}
    assert data["rule_id"] is not None

    con = sqlite3.connect(os.environ["DB_PATH"])
    try:
        pattern, category = con.execute(
            "SELECT pattern, category_path FROM rules WHERE id=?",
            (data["rule_id"],)).fetchone()
        assert category == "Pets"
        assert "amazon" not in pattern
        # The rule must not be able to claim unrelated Amazon rows.
        hits = con.execute(
            """SELECT COUNT(*) FROM transactions
                WHERE LOWER(merchant) LIKE '%' || ? || '%'
                   OR LOWER(description) LIKE '%' || ? || '%'""",
            (pattern, pattern)).fetchone()[0]
        assert hits <= 6, f"pattern {pattern!r} reaches {hits} rows"

        for key in ("bare_small", "bare_mid", "bare_big", "tracing_paper", "refund",
                    "target_a", "target_b"):
            got = con.execute("SELECT category_path FROM transactions WHERE id=?",
                              (ids[key],)).fetchone()[0]
            assert got != "Pets", f"{key} must not have been swept into Pets"
    finally:
        con.close()


@pytest.mark.asyncio
async def test_apply_similar_from_a_bare_charge_persists_no_rule(test_client):
    ids = await seed()

    resp = test_client.post("/api/transactions/apply-similar", json={
        "source_id": ids["bare_small"], "category_path": "Shopping",
        "ids": [ids["bare_mid"]], "all_matching": False, "create_rule": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["updated_ids"] == [ids["bare_mid"]]
    assert data["rule_id"] is None
    assert data["rule_note"], "silence here would read as 'rule saved'"
