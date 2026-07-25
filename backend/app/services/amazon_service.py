"""Amazon ingest, matching and splitting against the database.

The decision logic lives in ``amazon_matcher`` (pure, unit-tested). This module
is the thin DB driver: read rows, call the pure functions, write results.

Split model — reuses the mortgage-splitter pattern already proven in this repo
(``services/mortgage_splitter.py``):

  * the parent charge is flipped to ``entity_type='transfer'`` with
    ``category_path='Transfer > Amazon Order (Split)'`` so it stops counting as
    an expense, and
  * one child row per item carries the real expense, linked back via
    ``linked_transaction_id``.

Without neutralising the parent, an order's money would be counted twice.

Refund model — three steps, run together by ``attribute_and_pair_refunds``:

  1. **attribute** (``attribute_refunds``): pin each refund to the ITEM it
     reverses. Amazon's refund exports have no ASIN and no product name, so the
     item is inferred from the amount inside the refund's order — the pure rules
     live in ``amazon_matcher.attribute_refund``. This is what makes a refund
     able to say *which product, bought for whom*.
  2. **book** (``rebook_refunds`` / ``_book_refund``): the refund keeps its
     positive amount and takes the attributed item's own category, so returning a
     $64 item reduces that category by $64 instead of inflating income. No
     special entity type is needed — the type is derived from the sign (see
     ``services/flat_categories``).
  3. **pair** (``pair_refunds``): point the refund credit and the row it reverses
     at each other via ``refund_match_id`` / ``refund_role``, which is what makes
     the two visibly cancel out in the Refunded tab and lets the transactions
     list hide both halves of a wash.
"""
import logging
import os
import re
from typing import Any, Dict, List, Optional

from app.parsers.amazon import parse_all_owners, refund_fingerprint
from app.services.amazon_matcher import (
    SPLIT_PARENT_CATEGORY,
    attribute_refund,
    group_refund_events,
    is_amazon_transaction,
    match_charges,
    match_refunds,
    plan_split,
)

logger = logging.getLogger(__name__)

AMAZON_DIR = os.environ.get("AMAZON_DIR", "/app/samples/amazon")

# The reason string ``apply-similar`` stamps on rows it propagates a category to.
# Matched (not just compared) so a hand-typed note that happens to start the same
# way cannot masquerade as machine output. Kept in sync with the literal in
# ``routers/transactions.py::apply_similar_transactions``.
_APPLY_SIMILAR_REASON_RE = re.compile(
    r"^Applied from user correction on #\d+ \(similar merchant\)$"
)


def item_fingerprint(item: Dict[str, Any]) -> str:
    """Identity of an ordered item, so re-importing an export is idempotent."""
    return "|".join(
        str(part or "")
        for part in (
            item.get("owner"),
            item["order_id"],
            item.get("asin"),
            item.get("ship_date"),
            item.get("total_cents"),
            (item.get("product_name") or "")[:60],
        )
    )


async def ingest_export(db, root: Optional[str] = None) -> Dict[str, Any]:
    """Parse every owner export under ``root`` and upsert orders/items/refunds."""
    root = root or AMAZON_DIR
    parsed_by_owner = parse_all_owners(root)
    if not parsed_by_owner:
        return {"owners": [], "items": 0, "refunds": 0, "orders": 0,
                "message": f"No Amazon export directories found under {root}"}

    totals = {"owners": [], "items": 0, "refunds": 0, "orders": 0, "duplicates": 0}

    for owner, parsed in parsed_by_owner.items():
        items, refunds = parsed["items"], parsed["refunds"]

        order_rows: Dict[str, Dict[str, Any]] = {}
        for item in items:
            order = order_rows.setdefault(
                item["order_id"],
                {
                    "order_id": item["order_id"],
                    "owner": owner,
                    "order_date": item.get("order_date"),
                    "order_status": item.get("order_status"),
                    "payment_method": item.get("payment_method"),
                    "card_last4": item.get("card_last4"),
                    "used_gift_card": 1 if item.get("used_gift_card") else 0,
                    "recipient": item.get("recipient"),
                    "website": item.get("website"),
                    "total_cents": 0,
                    "source_file": item.get("source_file"),
                },
            )
            if not item.get("is_cancelled") and item.get("total_cents"):
                order["total_cents"] += item["total_cents"]

        for order in order_rows.values():
            await db.execute(
                """INSERT INTO amazon_orders
                   (order_id, owner, order_date, order_status, payment_method, card_last4,
                    used_gift_card, recipient, website, total_cents, source_file)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(order_id, owner) DO UPDATE SET
                     order_date=excluded.order_date,
                     order_status=excluded.order_status,
                     payment_method=excluded.payment_method,
                     card_last4=excluded.card_last4,
                     used_gift_card=excluded.used_gift_card,
                     recipient=excluded.recipient,
                     total_cents=excluded.total_cents""",
                tuple(order[k] for k in (
                    "order_id", "owner", "order_date", "order_status", "payment_method",
                    "card_last4", "used_gift_card", "recipient", "website", "total_cents",
                    "source_file")),
            )

        item_count = 0
        for item in items:
            item["owner"] = owner
            fingerprint = item_fingerprint(item)
            cursor = await db.execute(
                "SELECT id FROM amazon_items WHERE fingerprint=?", (fingerprint,)
            )
            if await cursor.fetchone():
                totals["duplicates"] += 1
                continue
            await db.execute(
                """INSERT INTO amazon_items
                   (order_id, owner, asin, product_name, quantity, unit_price_cents,
                    unit_price_tax_cents, shipping_charge_cents, discount_cents, total_cents,
                    ship_date, order_date, shipment_status, order_status, is_cancelled,
                    is_digital, card_last4, used_gift_card, recipient, source_file, fingerprint)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["order_id"], owner, item.get("asin"), item.get("product_name") or "",
                    item.get("quantity") or 1, item.get("unit_price_cents"),
                    item.get("unit_price_tax_cents"), item.get("shipping_charge_cents"),
                    item.get("discount_cents"), item.get("total_cents"), item.get("ship_date"),
                    item.get("order_date"), item.get("shipment_status"), item.get("order_status"),
                    1 if item.get("is_cancelled") else 0, item.get("is_digital") or 0,
                    item.get("card_last4"), 1 if item.get("used_gift_card") else 0,
                    item.get("recipient"), item.get("source_file"), fingerprint,
                ),
            )
            item_count += 1

        refund_count = 0
        for refund in refunds:
            refund["owner"] = owner
            fingerprint = f"{owner}|{refund_fingerprint(refund)}"
            cursor = await db.execute(
                "SELECT id FROM amazon_refunds WHERE fingerprint=?", (fingerprint,)
            )
            if await cursor.fetchone():
                totals["duplicates"] += 1
                continue
            await db.execute(
                """INSERT INTO amazon_refunds
                   (order_id, owner, refund_date, amount_cents, quantity, reason, status,
                    disbursement_type, source_file, fingerprint)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    refund["order_id"], owner, refund.get("refund_date"), refund["amount_cents"],
                    refund.get("quantity") or 1, refund.get("reason"), refund.get("status"),
                    refund.get("disbursement_type"), refund.get("source_file"), fingerprint,
                ),
            )
            refund_count += 1

        totals["owners"].append(
            {"owner": owner, "orders": len(order_rows), "items": item_count, "refunds": refund_count}
        )
        totals["orders"] += len(order_rows)
        totals["items"] += item_count
        totals["refunds"] += refund_count

    await db.commit()
    logger.info("Amazon ingest: %s", totals)
    return totals


async def _amazon_transactions(db, scope: Optional[str] = None) -> List[Dict[str, Any]]:
    """Amazon-looking rows that are not themselves split children or parents.

    Restricted to the live scope by default. Plaid is the source of truth, so
    item detail must attach to the Plaid rows — matching against the CSV history
    would hang the product names off rows no default view ever shows.
    """
    from app.services.scope import resolve_scope

    scope_sql, _params, _effective, _note = await resolve_scope(db, scope or "plaid")
    # resolve_scope writes fragments for a "t." alias; this query has none.
    scope_clause = scope_sql.replace(" AND t.", " AND ")
    cursor = await db.execute(
        f"""SELECT id, account_id, posted_date, amount_cents, merchant, description,
                   category_path, entity_type, linked_transaction_id
            FROM transactions
            WHERE linked_transaction_id IS NULL
              AND (category_path IS NULL OR category_path != ?)
              {scope_clause}""",
        (SPLIT_PARENT_CATEGORY,),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    return [r for r in rows if is_amazon_transaction(r.get("merchant"), r.get("description"))]


async def reset_matches(db) -> Dict[str, Any]:
    """Clear item/refund -> transaction links so matching can be re-run.

    Needed when the authoritative transaction source changes (e.g. Plaid replaces
    the CSV history): the links point at rows that are no longer displayed.
    Order/item/refund records themselves are untouched — only the links.
    """
    cursor = await db.execute(
        "UPDATE amazon_items SET matched_transaction_id=NULL, split_transaction_id=NULL, "
        "match_strategy=NULL WHERE matched_transaction_id IS NOT NULL "
        "OR split_transaction_id IS NOT NULL"
    )
    items_cleared = cursor.rowcount or 0
    cursor = await db.execute(
        "UPDATE amazon_refunds SET matched_transaction_id=NULL WHERE matched_transaction_id IS NOT NULL"
    )
    refunds_cleared = cursor.rowcount or 0
    await db.commit()
    logger.info("Amazon match reset: %d items, %d refunds unlinked", items_cleared, refunds_cleared)
    return {"items_unlinked": items_cleared, "refunds_unlinked": refunds_cleared}


async def _known_accounts(db) -> List[str]:
    cursor = await db.execute("SELECT DISTINCT account_id FROM transactions")
    return [r[0] for r in await cursor.fetchall() if r[0]]


async def match_all(db) -> Dict[str, Any]:
    """Match Amazon charges and refunds to card rows, recording the results."""
    transactions = await _amazon_transactions(db)
    accounts = await _known_accounts(db)

    cursor = await db.execute(
        "SELECT * FROM amazon_items WHERE is_cancelled=0 AND total_cents IS NOT NULL"
    )
    items = [dict(r) for r in await cursor.fetchall()]
    for row in items:
        row["is_cancelled"] = bool(row.get("is_cancelled"))
        row["used_gift_card"] = bool(row.get("used_gift_card"))

    cursor = await db.execute("SELECT * FROM amazon_refunds")
    refunds = [dict(r) for r in await cursor.fetchall()]

    charges = [t for t in transactions if (t["amount_cents"] or 0) < 0]
    credits = [t for t in transactions if (t["amount_cents"] or 0) > 0]

    charge_result = match_charges(charges, items, accounts)
    for match in charge_result["matches"]:
        for item in match["items"]:
            if item.get("id"):
                await db.execute(
                    "UPDATE amazon_items SET matched_transaction_id=?, match_strategy=? WHERE id=?",
                    (match["transaction_id"], match["strategy"], item["id"]),
                )

    refund_result = match_refunds(credits, refunds)
    for match in refund_result["matches"]:
        await db.execute(
            "UPDATE amazon_refunds SET matched_transaction_id=? WHERE order_id=? AND amount_cents=? "
            "AND (matched_transaction_id IS NULL)",
            (match["transaction_id"], match["order_id"], match["amount_cents"]),
        )
    await db.commit()

    # Attribute before booking: the category a refund reverses comes from the item
    # it reversed, so a refund booked before attribution would only ever get the
    # order's majority category.
    attribution = await attribute_refunds(db)
    for match in refund_result["matches"]:
        await _book_refund(db, match)
    await db.commit()
    pairing = await pair_refunds(db)
    summary = {
        "charges_seen": len(charges),
        "charges_matched": len(charge_result["matches"]),
        "charges_unmatched": len(charge_result["unmatched_charge_ids"]),
        "unmatched_charge_ids": charge_result["unmatched_charge_ids"][:100],
        "credits_seen": len(credits),
        "refunds_matched": len(refund_result["matches"]),
        "credits_unmatched": len(refund_result["unmatched_credit_ids"]),
        "items_available": len(items),
        "refund_records": len(refunds),
        "refund_attribution": attribution["by_strategy"],
        "refund_pairing": pairing,
    }
    logger.info("Amazon match summary: %s", summary)
    return summary


# Categories that say nothing about what was bought, so they can never be the
# answer to "what did this refund reverse?".
_NON_SPENDING_CATEGORIES = ("Uncategorized", "Transfer", "Credit-card Payment",
                            "Mortgage Payment", "Excluded")


async def _order_items_for_refund(db, refund: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The candidate items a refund could reverse.

    Scoped to the refund's own owner: two people's exports are separate Amazon
    accounts, and attributing across them would credit the wrong person. Falls
    back to the order id alone when the owner labels disagree, which happens for
    refunds imported before the owner column carried the same spelling as the
    items — better to attribute within the right order than not at all.
    """
    cursor = await db.execute(
        "SELECT * FROM amazon_items WHERE order_id=? AND owner=?",
        (refund["order_id"], refund.get("owner")),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    if not rows:
        cursor = await db.execute(
            "SELECT * FROM amazon_items WHERE order_id=?", (refund["order_id"],)
        )
        rows = [dict(r) for r in await cursor.fetchall()]
    for row in rows:
        row["is_cancelled"] = bool(row.get("is_cancelled"))
    return rows


async def attribute_refunds(db) -> Dict[str, Any]:
    """Fill ``matched_item_id`` / ``match_strategy`` on every refund record.

    Works in refund EVENTS, not records: Amazon lists one return in several export
    files, so the same return arrives as two to four rows (see
    ``amazon_matcher.group_refund_events``). Every record of an event gets the same
    answer, and the event claims its item once.

    Oldest event first, and an item already claimed by an earlier event is
    withdrawn from the candidate pool — two genuinely separate returns from one
    order take one item each instead of both landing on whichever item happened to
    be listed first, and no item is ever reported as returned twice.

    Always re-derives every row: attribution is cheap and depends on item data that
    a later import can change, so a stale answer is the only real risk.
    """
    cursor = await db.execute("SELECT * FROM amazon_refunds")
    refunds = [dict(r) for r in await cursor.fetchall()]

    counts: Dict[str, int] = {}
    event_counts: Dict[str, int] = {}
    items_cache: Dict[tuple, List[Dict[str, Any]]] = {}
    claimed_items: set = set()
    events = group_refund_events(refunds)

    for event in events:
        primary = event[0]
        key = (primary["order_id"], primary.get("owner"))
        if key not in items_cache:
            items_cache[key] = await _order_items_for_refund(db, primary)
        candidates = [i for i in items_cache[key] if i.get("id") not in claimed_items]

        result = attribute_refund(primary, candidates)
        item, strategy = result["item"], result["strategy"]
        if item and item.get("id"):
            claimed_items.add(item["id"])

        for record in event:
            await db.execute(
                "UPDATE amazon_refunds SET matched_item_id=?, match_strategy=? WHERE id=?",
                (item.get("id") if item else None, strategy, record["id"]),
            )
            counts[strategy] = counts.get(strategy, 0) + 1
        event_counts[strategy] = event_counts.get(strategy, 0) + 1

    await db.commit()
    summary = {
        "refunds_seen": len(refunds),
        "refund_events": len(events),
        "by_strategy": counts,
        "by_strategy_events": event_counts,
        "attributed_to_an_item": counts.get("exact_item", 0) + counts.get("near_item", 0),
    }
    logger.info("Amazon refund attribution: %s", summary)
    return summary


async def _category_of_transaction(db, txn_id: Optional[int]) -> Optional[str]:
    """A transaction's category, but only if it says something about what was bought."""
    if not txn_id:
        return None
    cursor = await db.execute("SELECT category_path FROM transactions WHERE id=?", (txn_id,))
    row = await cursor.fetchone()
    category = (row[0] if row else None) or ""
    if not category or category in _NON_SPENDING_CATEGORIES:
        return None
    return category


async def refund_category_for_item(db, item_id: Optional[int]) -> Optional[str]:
    """The category one attributed item was filed under, or None.

    The item's SPLIT CHILD first: that row was categorized from the actual product
    name, which is the most specific fact in the system about what was bought. The
    parent charge second — for an unsplit single-item charge it is the same money,
    and after a split it is deliberately neutralised to ``Transfer`` and therefore
    correctly rejected here.
    """
    if not item_id:
        return None
    cursor = await db.execute(
        "SELECT split_transaction_id, matched_transaction_id FROM amazon_items WHERE id=?",
        (item_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    for txn_id in (row[0], row[1]):
        category = await _category_of_transaction(db, txn_id)
        if category:
            return category
    return None


async def person_categories(db) -> List[str]:
    """Existing categories that name a person, longest first.

    Derived from the category table rather than hardcoded, so it keeps working
    when the owner adds or renames a person bucket. A category counts as naming a
    person when its name appears as a whole word inside an Amazon ``recipient``
    somewhere in the export — that is what makes ``Sofia`` a person category and
    ``Shopping`` not, without this module needing a list of names.
    """
    cursor = await db.execute(
        "SELECT path FROM categories WHERE path NOT LIKE '% > %'"
    )
    candidates = [r[0] for r in await cursor.fetchall() if r[0]]
    cursor = await db.execute(
        "SELECT DISTINCT recipient FROM amazon_items "
        "WHERE recipient IS NOT NULL AND recipient != ''"
    )
    recipients = [r[0] for r in await cursor.fetchall()]
    found = []
    for name in candidates:
        if name in _NON_SPENDING_CATEGORIES:
            continue
        pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        if any(pattern.search(r) for r in recipients):
            found.append(name)
    # Longest first so a two-word person category wins over a one-word prefix.
    return sorted(found, key=len, reverse=True)


def person_for_recipient(recipient: Optional[str], people: List[str]) -> Optional[str]:
    """The person category a delivery ``recipient`` names, or None.

    Recipients in a real export are things like ``Ana Delgado``, ``Ana Delgado
    HOUSE``, ``Ana Delgado-Ruiz`` and ``Ben Whitfield`` — several spellings of the
    same few people — mixed with pickup locations (``Amazon Locker - Rania``,
    ``Amazon Hub Counter+ (Stonestown …)``) and the literal ``Not Available``.
    A location tells you nothing about who the order was for, so it must not be
    mistaken for a person.
    """
    text = (recipient or "").strip()
    if not text or text.lower() == "not available":
        return None
    # Pickup points are Amazon infrastructure, not people.
    lowered = text.lower()
    if "amazon locker" in lowered or "amazon hub" in lowered:
        return None
    for person in people:
        if re.search(rf"\b{re.escape(person)}\b", text, re.IGNORECASE):
            return person
    return None


async def refund_person(db, refund: Dict[str, Any]) -> Optional[str]:
    """Who the refunded order was for, from the export's own recipient field.

    The attributed item's recipient first; failing that the order's, but only when
    the order's items agree — a mixed-recipient order genuinely does not have one
    person behind it, and picking the most common would be a guess presented as a
    fact.
    """
    people = await person_categories(db)
    if not people:
        return None

    item_id = refund.get("matched_item_id")
    if item_id:
        cursor = await db.execute(
            "SELECT recipient FROM amazon_items WHERE id=?", (item_id,)
        )
        row = await cursor.fetchone()
        if row:
            person = person_for_recipient(row[0], people)
            if person:
                return person

    cursor = await db.execute(
        "SELECT DISTINCT recipient FROM amazon_items WHERE order_id=? "
        "AND recipient IS NOT NULL AND recipient != ''",
        (refund["order_id"],),
    )
    found = {person_for_recipient(r[0], people) for r in await cursor.fetchall()}
    found.discard(None)
    return found.pop() if len(found) == 1 else None


async def refund_category_for_refund(db, refund: Dict[str, Any]) -> Optional[str]:
    """The category THIS refund should reverse, most specific evidence first.

    1. the attributed item's split child — the real product's own category,
    2. the attributed item's charge row,
    3. the order's most common category (``refund_category_for_order``), which is
       all that is available for a ``whole_order``, ``ambiguous`` or
       ``unattributed`` refund,
    4. the PERSON the order was for, when the export names one — see below,
    5. None — the caller books ``Other Income`` rather than inventing a bucket.

    Non-spending categories (``Transfer``, ``Excluded``, ``Uncategorized``, the
    payment categories) are rejected at every level: they describe money moving,
    not a purchase, so they cannot be what a return reverses.

    **Person categories are cross-checked against the export.** This owner files a
    lot of Amazon spending under the person it was for, and steps 1-3 inherit
    whatever category the purchase rows carry — which may itself have come from a
    merchant-key sweep that knew nothing about who ordered what. On the live data
    four refunds sat on ``Kalin`` while the export said the order was Sofia's. When
    the inherited answer names a person and the recipient names a DIFFERENT one,
    the recipient wins: it is a recorded fact about that order, not an inference.
    """
    category = await refund_category_for_item(db, refund.get("matched_item_id"))
    if not category:
        category = await refund_category_for_order(db, refund["order_id"])

    people = await person_categories(db)
    if category and category not in people:
        # A real spending category (Groceries, Housing…) says what was bought,
        # which is better information than who it was for. Leave it alone.
        return category

    person = await refund_person(db, refund)
    if person and person != category:
        logger.info(
            "Refund for order %s: category %r contradicts export recipient -> %r",
            refund.get("order_id"), category, person,
        )
        return person
    return category or person


async def refund_category_for_order(db, order_id: str) -> Optional[str]:
    """The flat category a refund for ``order_id`` should reverse, or None.

    Looks at the order's own rows — first the charges its items matched, then the
    per-item split children, which is where the real product categories live once
    a charge has been split. Most common category wins.
    """
    placeholders = ",".join("?" * len(_NON_SPENDING_CATEGORIES))
    for id_column in ("matched_transaction_id", "split_transaction_id"):
        cursor = await db.execute(
            f"""SELECT t.category_path, COUNT(*) n
                FROM amazon_items ai
                JOIN transactions t ON t.id = ai.{id_column}
                WHERE ai.order_id = ?
                  AND t.category_path IS NOT NULL
                  AND t.category_path != ''
                  AND t.category_path NOT IN ({placeholders})
                GROUP BY t.category_path ORDER BY n DESC, t.category_path LIMIT 1""",
            (order_id, *_NON_SPENDING_CATEGORIES),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return row[0]
    return None


async def _item_detail(db, item_id: Optional[int]) -> Optional[str]:
    """"``Widget × 2 for Kalin``" — the human half of an attributed refund."""
    if not item_id:
        return None
    cursor = await db.execute(
        "SELECT product_name, quantity, recipient FROM amazon_items WHERE id=?", (item_id,)
    )
    row = await cursor.fetchone()
    if not row or not row[0]:
        return None
    detail = str(row[0])[:60]
    if (row[1] or 1) > 1:
        detail += f" × {row[1]}"
    if row[2]:
        detail += f" for {row[2]}"
    return detail


async def _book_refund(db, match: Dict[str, Any]) -> None:
    """Book a matched refund into the category it reverses.

    A return of a $64 item should reduce the category that item was filed under,
    not appear as income. In the flat model that needs no special entity type: the
    dashboard nets each category, so a POSITIVE amount sitting on ``Health``
    reduces Health automatically (see ``services/flat_categories``).

    This used to select the original category with ``category_path LIKE
    'Expense >%'`` — a hierarchical prefix that stopped existing when the app
    moved to flat category names. Both lookups therefore always missed, and every
    Amazon refund silently fell through to ``Other Income``: 130 rows of the
    user's returns showed up as income instead of reducing what they bought.

    ``match`` may carry ``matched_item_id`` (the attributed item). When it does
    not, the refund record linked to this transaction is consulted, so a caller
    that knows nothing about attribution still gets the item-level answer.
    """
    from app.services.flat_categories import entity_type_for

    refund = {"order_id": match["order_id"], "matched_item_id": match.get("matched_item_id")}
    if not refund["matched_item_id"]:
        cursor = await db.execute(
            "SELECT matched_item_id FROM amazon_refunds WHERE matched_transaction_id=? "
            "AND matched_item_id IS NOT NULL LIMIT 1",
            (match["transaction_id"],),
        )
        row = await cursor.fetchone()
        if row:
            refund["matched_item_id"] = row[0]

    category_path = await refund_category_for_refund(db, refund)
    amount = int(match.get("amount_cents") or 0)

    if category_path:
        reason = f"Amazon refund for order {match['order_id']} — reverses {category_path}"
        detail = await _item_detail(db, refund["matched_item_id"])
        if detail:
            # Naming the product and the person is the whole point of item-level
            # attribution: "who paid for what" has to be readable off the row.
            reason += f" ({detail})"
    else:
        # Nothing about this order is categorized, so there is no bucket to
        # reverse. Keeping it as plain income is honest rather than guessing.
        category_path = "Other Income"
        reason = f"Amazon refund for order {match['order_id']} — original category unknown"

    await db.execute(
        """UPDATE transactions
           SET category_path=?, entity_type=?, confidence_score=?, categorization_reason=?
           WHERE id=?""",
        (category_path, entity_type_for(category_path, amount), 0.95, reason,
         match["transaction_id"]),
    )


async def rebook_refunds(db, overwrite_manual: bool = False) -> Dict[str, Any]:
    """Re-derive the category of every ALREADY-matched Amazon refund.

    ``match_all`` only books a refund at the moment it first matches, so a fix to
    the booking rule does not reach refunds matched under the old rule. That is
    exactly what happened: the ``'Expense >%'`` lookup in ``_book_refund`` broke
    when categories went flat, and 130 refunds were left booked as
    ``Other Income``. Re-running the match would not repair them, because they are
    already linked.

    ``overwrite_manual`` decides whether rows the user has since edited by hand
    are also re-derived. Default False: a category the owner chose deliberately
    outranks anything inferred here.

    Since attribution exists the target category comes from the refund's own
    attributed ITEM where there is one, so re-booking also upgrades a refund that
    was previously booked from the order's majority category to the category of
    the exact product that came back.
    """
    cursor = await db.execute(
        """SELECT r.order_id, r.amount_cents, r.matched_transaction_id, r.matched_item_id,
                  t.category_path, t.categorization_reason
           FROM amazon_refunds r
           JOIN transactions t ON t.id = r.matched_transaction_id
           WHERE r.matched_transaction_id IS NOT NULL"""
    )
    rows = [dict(r) for r in await cursor.fetchall()]

    rebooked, skipped_manual, unchanged, no_category, duplicates = 0, 0, 0, 0, 0
    processed: set = set()
    for row in rows:
        # Amazon lists one return in several export files, so several records can
        # share one credit row. Booking it once is enough and keeps the counts
        # honest (see amazon_matcher.group_refund_events).
        if row["matched_transaction_id"] in processed:
            duplicates += 1
            continue
        processed.add(row["matched_transaction_id"])
        reason = row.get("categorization_reason") or ""
        # A row this function wrote before, or the stale Other-Income fallback, is
        # ours to re-derive. So is a row with no reason at all: an empty reason
        # means nobody has explained this row yet, which is the opposite of a
        # category the owner chose deliberately. Anything else came from the user
        # or another feature and is left alone.
        ours = reason.startswith("Amazon refund for order") or not reason.strip()
        # Also ours: a category propagated by apply-similar. That is a MERCHANT-KEY
        # guess ("this row says Amazon, so give it what the other Amazon rows got"),
        # not a decision anyone made about this row. Item-level attribution knows the
        # actual product and who it was for, which is strictly better evidence, so it
        # should win. On the live data 27 refunds were pinned to `Kalin` by one such
        # sweep and would otherwise stay wrong forever. A single-row
        # "User correction" is a real choice and is still respected.
        ours = ours or _APPLY_SIMILAR_REASON_RE.match(reason) is not None
        if not ours and not overwrite_manual:
            skipped_manual += 1
            continue
        target = await refund_category_for_refund(db, row)
        if not target:
            no_category += 1
            continue
        # Same category is not always the same booking: an attributed refund also
        # names the product and recipient in its reason, so a row booked before
        # attribution still needs rewriting to carry that.
        detail = await _item_detail(db, row.get("matched_item_id"))
        if target == row.get("category_path") and (not detail or detail in reason):
            unchanged += 1
            continue
        await _book_refund(db, {
            "order_id": row["order_id"],
            "amount_cents": row["amount_cents"],
            "transaction_id": row["matched_transaction_id"],
            "matched_item_id": row.get("matched_item_id"),
        })
        rebooked += 1

    await db.commit()
    summary = {
        "matched_refunds": len(rows),
        "rebooked": rebooked,
        "already_correct": unchanged,
        "skipped_manually_categorized": skipped_manual,
        "no_source_category": no_category,
        "duplicate_export_rows": duplicates,
    }
    logger.info("Amazon refund re-book: %s", summary)
    return summary


async def _pair_targets(db, refund: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rows this refund could be the reversal of, best evidence first.

    1. the attributed item's SPLIT CHILD — that row holds this exact item's money
       and its category, so it is the true other half of the wash,
    2. the attributed item's own charge row,
    3. any charge row the order matched, smallest first, so a refund pairs with the
       shipment it came out of rather than the largest charge on the order.

    No ``entity_type`` filter, deliberately. ``services/refund_pairs`` skips
    ``transfer`` rows and must — it pairs on merchant and date, and internal
    movement pairs spuriously. Here the order id ties the two rows together as a
    fact from Amazon's own export, and a split Amazon parent is neutralised to
    ``transfer`` precisely because its children carry the expense. Excluding those
    rows is what left refunds on split orders permanently unpaired.
    """
    candidates: List[Optional[int]] = []
    if refund.get("matched_item_id"):
        cursor = await db.execute(
            "SELECT split_transaction_id, matched_transaction_id FROM amazon_items WHERE id=?",
            (refund["matched_item_id"],),
        )
        row = await cursor.fetchone()
        if row:
            candidates.extend([row[0], row[1]])

    cursor = await db.execute(
        "SELECT DISTINCT matched_transaction_id FROM amazon_items "
        "WHERE order_id=? AND matched_transaction_id IS NOT NULL",
        (refund["order_id"],),
    )
    order_charges = [r[0] for r in await cursor.fetchall()]

    seen: set = set()
    ordered: List[Dict[str, Any]] = []
    for txn_id in [c for c in candidates if c] + sorted(order_charges):
        if txn_id in seen:
            continue
        seen.add(txn_id)
        cursor = await db.execute(
            "SELECT id, amount_cents, refund_match_id, category_path, entity_type "
            "FROM transactions WHERE id=?",
            (txn_id,),
        )
        row = await cursor.fetchone()
        if row:
            ordered.append(dict(row))
    return ordered


async def _release_partner(db, row_id: int, expected_partner: Optional[int]) -> None:
    """Clear the marks on a row that was paired with ``expected_partner``.

    Re-pointing one half of a pair without releasing the other leaves a one-way
    link: the Refunded tab joins ``refund_match_id`` both ways, so a stale half
    shows a pair that no longer exists.
    """
    if not row_id or not expected_partner:
        return
    await db.execute(
        "UPDATE transactions SET refund_match_id=NULL, refund_role=NULL, refund_partial=0 "
        "WHERE id=? AND refund_match_id=?",
        (row_id, expected_partner),
    )


async def pair_refunds(db) -> Dict[str, Any]:
    """Pair every matched Amazon refund credit with the row it cancels out.

    This is what "found as cancelled out" means in the UI: both rows carry
    ``refund_match_id`` / ``refund_role`` / ``refund_partial``, which is what the
    Refunded tab lists and what the "hide refunded" filter suppresses. The totals
    were always right — a category nets -64 + 64 = 0 on its own — so this is
    purely about the pair being visible.

    Kept one-to-one: a transaction is claimed by at most one refund, so two
    returns from one order cannot both point at the same charge and double-count a
    reversal. Refunds attributed to a specific item are placed first, since they
    have the strongest claim on the row they name.
    """
    cursor = await db.execute(
        """SELECT id, order_id, owner, refund_date, amount_cents, matched_transaction_id,
                  matched_item_id, match_strategy
           FROM amazon_refunds WHERE matched_transaction_id IS NOT NULL"""
    )
    refunds = [dict(r) for r in await cursor.fetchall()]
    refunds.sort(key=lambda r: (
        0 if r.get("match_strategy") in ("exact_item", "near_item") else 1,
        r.get("refund_date") or "", r["id"],
    ))

    claimed_charges: set = set()
    seen_credits: set = set()
    paired, already, repointed, overwrote_generic, no_target, duplicates = 0, 0, 0, 0, 0, 0

    for refund in refunds:
        credit_id = refund["matched_transaction_id"]
        # Several export rows describe one return and share one credit row (see
        # amazon_matcher.group_refund_events), and match_all can also link two
        # equal-amount records to the same credit. The credit reverses one thing,
        # so only the first claim on it counts.
        if credit_id in seen_credits:
            duplicates += 1
            continue
        seen_credits.add(credit_id)
        cursor = await db.execute(
            "SELECT id, amount_cents, refund_match_id FROM transactions WHERE id=?", (credit_id,)
        )
        credit = await cursor.fetchone()
        if not credit:
            continue
        credit = dict(credit)
        amount = abs(int(refund["amount_cents"] or 0))

        target = None
        for candidate in await _pair_targets(db, refund):
            if candidate["id"] == credit_id or candidate["id"] in claimed_charges:
                continue
            charge_amount = int(candidate["amount_cents"] or 0)
            if charge_amount >= 0 or amount > abs(charge_amount):
                continue  # not a charge, or too small to be what came back
            target = candidate
            break

        if not target:
            no_target += 1
            continue

        claimed_charges.add(target["id"])
        partial = 1 if amount != abs(int(target["amount_cents"])) else 0

        # Both halves have to agree. A pair whose back-link is missing is worse
        # than no pair — the Refunded tab joins the two rows, so a one-way link
        # shows a pair from one side and nothing from the other.
        if (credit.get("refund_match_id") == target["id"]
                and target.get("refund_match_id") == credit_id):
            already += 1
            continue

        existing = target.get("refund_match_id")
        if existing and existing != credit_id:
            # The generic merchant/date matcher got there first. Amazon's own order
            # id is stronger evidence than a merchant-name coincidence, so this
            # wins — but say so out loud, since it hides a different pair.
            logger.info(
                "Amazon refund %s (order %s): re-pointing charge %s from credit %s to credit %s "
                "— order-level attribution beats the generic merchant match",
                refund["id"], refund["order_id"], target["id"], existing, credit_id,
            )
            overwrote_generic += 1
            await _release_partner(db, existing, target["id"])
        if credit.get("refund_match_id"):
            await _release_partner(db, credit["refund_match_id"], credit_id)
            repointed += 1

        await db.execute(
            "UPDATE transactions SET refund_match_id=?, refund_role='charge', refund_partial=? "
            "WHERE id=?",
            (credit_id, partial, target["id"]),
        )
        await db.execute(
            "UPDATE transactions SET refund_match_id=?, refund_role='credit', refund_partial=? "
            "WHERE id=?",
            (target["id"], partial, credit_id),
        )
        paired += 1

    await db.commit()
    summary = {
        "matched_refunds": len(refunds),
        "paired": paired,
        "already_paired": already,
        "repointed_from_another_charge": repointed,
        "overwrote_generic_pair": overwrote_generic,
        "no_reversible_row": no_target,
        "duplicate_export_rows": duplicates,
    }
    logger.info("Amazon refund pairing: %s", summary)
    return summary


async def attribute_and_pair_refunds(db, overwrite_manual: bool = False) -> Dict[str, Any]:
    """Attribute → book → pair, in the one order that makes each step useful.

    Attribution has to run first because the category a refund is booked to comes
    from the attributed item, and pairing last because the row it pairs to is the
    one attribution named.
    """
    attribution = await attribute_refunds(db)
    booking = await rebook_refunds(db, overwrite_manual=overwrite_manual)
    pairing = await pair_refunds(db)
    return {"attribution": attribution, "booking": booking, "pairing": pairing}


async def refund_attribution_counts(db) -> Dict[str, int]:
    """How many refunds each attribution strategy claimed. Never-attributed rows
    are reported as ``unprocessed`` rather than folded into ``unattributed`` —
    "we looked and found nothing" is a different fact from "we never looked"."""
    cursor = await db.execute(
        "SELECT COALESCE(match_strategy,'unprocessed') s, COUNT(*) FROM amazon_refunds GROUP BY s"
    )
    return {r[0]: r[1] for r in await cursor.fetchall()}


async def list_refunds(db, limit: int = 200) -> List[Dict[str, Any]]:
    """One row per refund record: what came back, for whom, and what it cancels.

    The "who paid for what" view. ``product_name``/``recipient`` are null for a
    refund no single item explains, and ``paired_transaction_id`` is null until the
    refund is paired — both are honest gaps, not errors.
    """
    cursor = await db.execute(
        """SELECT r.id refund_id, r.refund_date, r.amount_cents, r.order_id, r.owner,
                  COALESCE(r.match_strategy, ?) strategy, r.matched_item_id,
                  r.reason refund_reason, r.disbursement_type,
                  i.product_name, i.recipient, i.asin,
                  t.id transaction_id, t.category_path, t.refund_match_id paired_transaction_id,
                  t.refund_partial, t.refund_role
           FROM amazon_refunds r
           LEFT JOIN amazon_items i ON i.id = r.matched_item_id
           LEFT JOIN transactions t ON t.id = r.matched_transaction_id
           ORDER BY r.refund_date DESC, r.id DESC LIMIT ?""",
        ("unprocessed", limit),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def split_matched_charges(db, limit: int = 0) -> Dict[str, Any]:
    """Split every matched Amazon charge into per-item child transactions."""
    cursor = await db.execute(
        """SELECT DISTINCT matched_transaction_id FROM amazon_items
           WHERE matched_transaction_id IS NOT NULL AND split_transaction_id IS NULL"""
    )
    txn_ids = [r[0] for r in await cursor.fetchall() if r[0]]
    if limit > 0:
        txn_ids = txn_ids[:limit]

    split_count = 0
    child_count = 0
    warnings: List[str] = []

    for txn_id in txn_ids:
        cursor = await db.execute("SELECT * FROM transactions WHERE id=?", (txn_id,))
        parent = await cursor.fetchone()
        if not parent:
            continue
        parent = dict(parent)
        if parent.get("category_path") == SPLIT_PARENT_CATEGORY:
            continue  # already split

        cursor = await db.execute(
            "SELECT * FROM amazon_items WHERE matched_transaction_id=? AND split_transaction_id IS NULL",
            (txn_id,),
        )
        items = [dict(r) for r in await cursor.fetchall()]
        if not items:
            continue

        children = plan_split(parent["amount_cents"], items)
        if not children:
            warnings.append(f"txn {txn_id}: no splittable items")
            continue

        # Only split when it adds information: a single item that consumes the
        # whole charge is already fully described by the parent row.
        if len(children) == 1 and not children[0]["is_residual"]:
            await db.execute(
                "UPDATE transactions SET merchant=?, description=? WHERE id=?",
                (
                    f"Amazon: {children[0]['product_name'][:60]}",
                    f"{parent.get('description') or ''} — {children[0]['product_name']}",
                    txn_id,
                ),
            )
            await db.execute(
                "UPDATE amazon_items SET split_transaction_id=? WHERE matched_transaction_id=?",
                (txn_id, txn_id),
            )
            split_count += 1
            continue

        await db.execute(
            "UPDATE transactions SET category_path=?, entity_type='transfer', asset_class=NULL, "
            "categorization_reason=? WHERE id=?",
            (SPLIT_PARENT_CATEGORY,
             f"Split into {len(children)} Amazon line items", txn_id),
        )

        item_queue = list(items)
        for child in children:
            await db.execute(
                """INSERT INTO transactions
                   (source_platform, source_file, account_id, posted_date, transaction_date,
                    amount_cents, currency, merchant, description, normalized_description,
                    category_path, entity_type, linked_transaction_id, confidence_score,
                    categorization_reason, business_context)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    parent.get("source_platform") or "amazon",
                    parent.get("source_file") or "",
                    parent["account_id"],
                    parent["posted_date"],
                    parent.get("transaction_date"),
                    -abs(child["amount_cents"]),
                    parent.get("currency") or "USD",
                    f"Amazon: {child['product_name'][:60]}",
                    child["product_name"],
                    (child["product_name"] or "").lower()[:200],
                    None,  # categorized by the LLM using the product name
                    "expense",
                    txn_id,
                    None,
                    f"Amazon item from split of #{txn_id}",
                    parent.get("business_context") or "personal",
                ),
            )
            cursor = await db.execute("SELECT last_insert_rowid()")
            child_id = (await cursor.fetchone())[0]
            child_count += 1
            if not child["is_residual"] and item_queue:
                match_item = next(
                    (i for i in item_queue if (i.get("product_name") or "") == child["product_name"]),
                    item_queue[0],
                )
                item_queue.remove(match_item)
                await db.execute(
                    "UPDATE amazon_items SET split_transaction_id=? WHERE id=?",
                    (child_id, match_item["id"]),
                )
        split_count += 1

    await db.commit()
    result = {"split": split_count, "children_created": child_count, "warnings": warnings}
    logger.info("Amazon split: %s", result)
    return result
