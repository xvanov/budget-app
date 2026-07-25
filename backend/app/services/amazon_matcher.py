"""Match Amazon export data to card transactions, split charges into items.

Three jobs, all pure-logic functions plus thin DB drivers so the hard parts are
unit-testable without a database:

1. ``match_charges``  — Amazon charges -> shipments / items / orders.
2. ``match_refunds``  — Amazon refunds -> the credit rows on the card, booked as
   contra-expenses so a return actually reduces the category it reverses.
3. ``split_charge``   — one child transaction per matched item, reusing the
   mortgage-splitter pattern (parent neutralised, children carry the expense).

Why the matching is layered rather than one rule: validated against the real
export plus the real Chase 1236 statement, a single "sum the shipment" strategy
matched only ~54% of Amazon charges. Amazon splits an order across shipments,
charges each shipment separately, sometimes captures a partial shipment, and
applies gift-card balances that make the card charge smaller than the order.
Each layer below exists because a real charge in the data needs it.

Amounts: expenses are negative, refunds positive (this repo's convention). A
charge of -$46.01 matches an item whose ``total_cents`` is 4601.

A fourth job, ``attribute_refund``, answers "which item did this refund reverse?"
Amazon's refund exports carry no ASIN and no product name, so item identity is
inferred from the amount inside the refund's own order.
"""
import logging
from collections import Counter
from datetime import datetime, timedelta
from itertools import combinations
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# A card is charged at ship time, but authorisation can land a little earlier
# and posting a little later.
CHARGE_WINDOW_BEFORE_DAYS = 3
CHARGE_WINDOW_AFTER_DAYS = 12
# Refunds post after Amazon issues them, occasionally weeks later.
REFUND_WINDOW_BEFORE_DAYS = 2
REFUND_WINDOW_AFTER_DAYS = 21
# Subset-sum is exponential; refuse to try it on big orders.
SUBSET_MAX_ITEMS = 8
SUBSET_MAX_COMBO = 4

# How far a refund may drift from the item's recorded total and still be that
# item. Amazon refunds the item price plus the tax it actually collected, while
# the export's ``total_cents`` is the price plus the tax it originally quoted;
# promotional rebates and a re-computed local tax rate move the two apart by a
# few cents to a dollar or so. Measured on the real export, $2 recovers 6 more
# refunds and never spans two items in the same order. Wider than that and the
# tolerance starts reaching across genuinely different items.
NEAR_ITEM_TOLERANCE_CENTS = 200

# The strategies ``attribute_refund`` can report, most to least specific.
REFUND_STRATEGY_EXACT = "exact_item"
REFUND_STRATEGY_NEAR = "near_item"
REFUND_STRATEGY_WHOLE_ORDER = "whole_order"
REFUND_STRATEGY_AMBIGUOUS = "ambiguous"
REFUND_STRATEGY_UNATTRIBUTED = "unattributed"

AMAZON_MERCHANT_MARKERS = (
    "amazon",
    "amzn",
    "amazon.com",
    "amzn mktp",
    "amazon mktpl",
    "amazon prime",
    "amazon digital",
    "prime video",
    "whole foods",
    "wholefds",
)

SPLIT_PARENT_CATEGORY = "Transfer"
RESIDUAL_LABEL = "Tax & Shipping"


def is_amazon_transaction(merchant: Optional[str], description: Optional[str]) -> bool:
    """True when a row looks like Amazon on any account."""
    blob = f"{merchant or ''} {description or ''}".lower()
    return any(marker in blob for marker in AMAZON_MERCHANT_MARKERS)


def _parse(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        return datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _within(charge_date: Optional[str], anchor_date: Optional[str], before: int, after: int) -> bool:
    charge, anchor = _parse(charge_date), _parse(anchor_date)
    if not charge or not anchor:
        return False
    delta = (charge - anchor).days
    return -before <= delta <= after


def _day_gap(charge_date: Optional[str], anchor_date: Optional[str]) -> int:
    charge, anchor = _parse(charge_date), _parse(anchor_date)
    if not charge or not anchor:
        return 10_000
    return abs((charge - anchor).days)


def account_for_card(last4: Optional[str], known_accounts: Iterable[str]) -> Optional[str]:
    """Map an Amazon 'Visa - 1236' last-4 onto an account id we actually hold.

    Returns None for cards the app doesn't track (and for gift-card-only
    orders), which is what keeps their items out of matching entirely.
    """
    if not last4:
        return None
    for account_id in known_accounts:
        if last4 in (account_id or ""):
            return account_id
    return None


def build_shipments(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group items into shipments: the unit Amazon actually charges for.

    Cancelled items are dropped — they were never charged, and including them
    inflates every total they touch.
    """
    groups: Dict[Tuple[str, Optional[str]], List[Dict[str, Any]]] = {}
    for item in items:
        if item.get("is_cancelled"):
            continue
        if item.get("total_cents") is None:
            continue
        key = (item["order_id"], item.get("ship_date"))
        groups.setdefault(key, []).append(item)

    shipments = []
    for (order_id, ship_date), group in groups.items():
        shipments.append(
            {
                "order_id": order_id,
                "ship_date": ship_date,
                "order_date": group[0].get("order_date"),
                "items": group,
                "total_cents": sum(i["total_cents"] for i in group),
                "card_last4": group[0].get("card_last4"),
                "used_gift_card": any(i.get("used_gift_card") for i in group),
            }
        )
    return shipments


def _subset_matching(items: Sequence[Dict[str, Any]], target: int) -> Optional[List[Dict[str, Any]]]:
    """Smallest subset of items summing exactly to target, or None.

    Bounded hard: Amazon orders can hold dozens of items and the search is
    exponential.
    """
    usable = [i for i in items if i.get("total_cents")]
    if not usable or len(usable) > SUBSET_MAX_ITEMS:
        return None
    for size in range(2, min(len(usable), SUBSET_MAX_COMBO) + 1):
        for combo in combinations(usable, size):
            if sum(i["total_cents"] for i in combo) == target:
                return list(combo)
    return None


def match_charges(
    charges: Sequence[Dict[str, Any]],
    items: Sequence[Dict[str, Any]],
    known_accounts: Sequence[str],
) -> Dict[str, Any]:
    """Match Amazon charges to shipments/items. Pure function.

    ``charges``: dicts with id, account_id, posted_date, amount_cents (negative).
    ``items``:   parser output from app.parsers.amazon.

    Returns {'matches': [...], 'unmatched_charge_ids': [...]}. Every match
    records which layer produced it, so a low-confidence strategy can be
    reviewed or disabled later without re-deriving anything.
    """
    shipments = build_shipments(items)

    # Index by amount for the exact-match layers.
    by_shipment_total: Dict[int, List[Dict[str, Any]]] = {}
    for shipment in shipments:
        by_shipment_total.setdefault(shipment["total_cents"], []).append(shipment)

    by_item_total: Dict[int, List[Dict[str, Any]]] = {}
    for item in items:
        if item.get("is_cancelled") or item.get("total_cents") is None:
            continue
        by_item_total.setdefault(item["total_cents"], []).append(item)

    order_totals: Dict[str, int] = {}
    order_items: Dict[str, List[Dict[str, Any]]] = {}
    for shipment in shipments:
        order_totals[shipment["order_id"]] = order_totals.get(shipment["order_id"], 0) + shipment["total_cents"]
        order_items.setdefault(shipment["order_id"], []).extend(shipment["items"])

    consumed_shipments: set = set()
    consumed_items: set = set()
    consumed_orders: set = set()
    matches: List[Dict[str, Any]] = []
    unmatched: List[int] = []

    def item_key(item: Dict[str, Any]) -> Tuple:
        return (item["order_id"], item.get("asin"), item.get("ship_date"), item.get("total_cents"),
                item.get("product_name"))

    # Deterministic order: oldest charge first, so repeat amounts consume the
    # closest shipment in time rather than whichever the dict happened to yield.
    for charge in sorted(charges, key=lambda c: (c.get("posted_date") or "", c.get("id") or 0)):
        amount = int(charge.get("amount_cents") or 0)
        if amount >= 0:
            continue  # refunds are handled by match_refunds
        target = -amount
        charge_date = charge.get("posted_date")
        matched: Optional[Dict[str, Any]] = None

        # Layer 1 — a whole shipment. The most common real case.
        candidates = [
            s for s in by_shipment_total.get(target, [])
            if id(s) not in consumed_shipments
            and _within(charge_date, s["ship_date"], CHARGE_WINDOW_BEFORE_DAYS, CHARGE_WINDOW_AFTER_DAYS)
        ]
        if candidates:
            best = min(candidates, key=lambda s: _day_gap(charge_date, s["ship_date"]))
            consumed_shipments.add(id(best))
            consumed_items.update(item_key(i) for i in best["items"])
            matched = {"strategy": "shipment", "order_id": best["order_id"], "items": best["items"]}

        # Layer 2 — a single item (partial capture of a multi-item shipment).
        if not matched:
            candidates = [
                i for i in by_item_total.get(target, [])
                if item_key(i) not in consumed_items
                and _within(charge_date, i.get("ship_date"), CHARGE_WINDOW_BEFORE_DAYS, CHARGE_WINDOW_AFTER_DAYS)
            ]
            if candidates:
                best = min(candidates, key=lambda i: _day_gap(charge_date, i.get("ship_date")))
                consumed_items.add(item_key(best))
                matched = {"strategy": "item", "order_id": best["order_id"], "items": [best]}

        # Layer 3 — the whole order billed as one charge.
        if not matched:
            for order_id, total in order_totals.items():
                if total != target or order_id in consumed_orders:
                    continue
                # Only claim the order if it still has unclaimed items. Without
                # this a second identical charge matched the same order and
                # produced a match carrying an EMPTY item list — a phantom match
                # that would double-count the charge while explaining nothing.
                fresh = [i for i in order_items[order_id] if item_key(i) not in consumed_items]
                if not fresh:
                    continue
                anchor = min(
                    (i.get("ship_date") or i.get("order_date") or "" for i in order_items[order_id]),
                    default=None,
                )
                if _within(charge_date, anchor, CHARGE_WINDOW_BEFORE_DAYS, CHARGE_WINDOW_AFTER_DAYS):
                    consumed_orders.add(order_id)
                    consumed_items.update(item_key(i) for i in fresh)
                    matched = {"strategy": "order", "order_id": order_id, "items": fresh}
                    break

        # Layer 4 — an arbitrary subset of one order's items.
        if not matched:
            for order_id, candidate_items in order_items.items():
                anchor = min(
                    (i.get("ship_date") or i.get("order_date") or "" for i in candidate_items),
                    default=None,
                )
                if not _within(charge_date, anchor, CHARGE_WINDOW_BEFORE_DAYS, CHARGE_WINDOW_AFTER_DAYS):
                    continue
                fresh = [i for i in candidate_items if item_key(i) not in consumed_items]
                subset = _subset_matching(fresh, target)
                if subset:
                    consumed_items.update(item_key(i) for i in subset)
                    matched = {"strategy": "subset", "order_id": order_id, "items": subset}
                    break

        # A match with no items explains nothing and must never be recorded —
        # it would neutralise the parent charge without any child to carry it.
        if matched and matched.get("items"):
            matched["transaction_id"] = charge.get("id")
            matched["amount_cents"] = amount
            matches.append(matched)
        else:
            unmatched.append(charge.get("id"))

    logger.info(
        "Amazon charge matching: %d matched, %d unmatched (of %d charges)",
        len(matches), len(unmatched), len(charges),
    )
    return {"matches": matches, "unmatched_charge_ids": unmatched}


def match_refunds(
    credits: Sequence[Dict[str, Any]],
    refunds: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Match Amazon refund records to credit rows on the card. Pure function.

    ``credits``: positive-amount Amazon rows (Chase marks these Return/credit).
    ``refunds``: parser output from app.parsers.amazon.

    One-to-one, exact amount, closest date wins. A refund with no matching card
    credit is reported rather than invented — Amazon also refunds to gift
    balance, which never touches the bank.
    """
    by_amount: Dict[int, List[Dict[str, Any]]] = {}
    for refund in refunds:
        by_amount.setdefault(int(refund["amount_cents"]), []).append(refund)

    consumed: set = set()
    matches: List[Dict[str, Any]] = []
    unmatched_credits: List[int] = []

    for credit in sorted(credits, key=lambda c: (c.get("posted_date") or "", c.get("id") or 0)):
        amount = int(credit.get("amount_cents") or 0)
        if amount <= 0:
            continue
        candidates = [
            r for r in by_amount.get(amount, [])
            if id(r) not in consumed
            and _within(credit.get("posted_date"), r.get("refund_date"),
                        REFUND_WINDOW_BEFORE_DAYS, REFUND_WINDOW_AFTER_DAYS)
        ]
        if not candidates:
            unmatched_credits.append(credit.get("id"))
            continue
        best = min(candidates, key=lambda r: _day_gap(credit.get("posted_date"), r.get("refund_date")))
        consumed.add(id(best))
        matches.append(
            {
                "transaction_id": credit.get("id"),
                "order_id": best["order_id"],
                "amount_cents": amount,
                "refund_date": best.get("refund_date"),
                "reason": best.get("reason"),
            }
        )

    matched_refunds = len(matches)
    logger.info(
        "Amazon refund matching: %d card credits matched to refunds, %d credits unmatched, "
        "%d refund records had no card credit (gift-balance refunds land here)",
        matched_refunds, len(unmatched_credits), len(refunds) - matched_refunds,
    )
    return {"matches": matches, "unmatched_credit_ids": unmatched_credits}


def group_refund_events(refunds: Sequence[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Collapse refund RECORDS into the real refund EVENTS behind them.

    Amazon reports one return in up to four different files — ``Refund
    Details.csv``, ``Returns Status.csv``, ``Your Orders.Returns.*.csv`` and
    ``Digital Returns.csv`` — with a different date and a different status
    vocabulary in each ("Completed" / "Closed" / "RefundInitiated"). Nothing in the
    rows is a shared id, so the ingest's fingerprint cannot tell them apart and all
    of them land in ``amazon_refunds``: in the real export 295 records describe 170
    returns.

    Grouping them matters because attribution is one-to-one against items. Treated
    as separate returns, the first record claims the item and its own duplicates
    are then pushed to ``unattributed`` — the same return would be reported once as
    "Vitamins for Elena" and twice as "we don't know", and half the pairs would be
    lost. The evidence that these really are one event: in the live data every
    multi-record group resolves to the SAME card credit, and none to two.

    Records are keyed on (order, owner, amount) plus their ordinal WITHIN one
    source file, so the Nth row of ``Refund Details`` groups with the Nth row of
    ``Returns Status`` while an order that genuinely refunded the same amount twice
    (two rows in one file) stays two events.

    Returns one list of records per event, oldest event first.
    """
    seen: Counter = Counter()
    events: Dict[Tuple, List[Dict[str, Any]]] = {}
    for refund in refunds:
        base = (refund.get("order_id"), refund.get("owner"),
                abs(int(refund.get("amount_cents") or 0)))
        within_file = (*base, refund.get("source_file"))
        ordinal = seen[within_file]
        seen[within_file] += 1
        events.setdefault((*base, ordinal), []).append(refund)

    def event_sort_key(records: List[Dict[str, Any]]) -> Tuple:
        dates = [str(r.get("refund_date") or "") for r in records if r.get("refund_date")]
        return (min(dates) if dates else "", min(r.get("id") or 0 for r in records))

    return sorted(events.values(), key=event_sort_key)


def attribute_refund(
    refund: Dict[str, Any],
    order_items: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Which ITEM of its order does this refund reverse? Pure function.

    ``refund``: a row from ``amazon_refunds`` (``amount_cents`` positive).
    ``order_items``: the items of that refund's order — ``amazon_items`` rows.

    Returns ``{"item": <the item dict or None>, "strategy": <str>}``.

    Amazon's ``Refund Details.csv`` and ``Returns Status.csv`` carry an order id,
    a date and an amount — no ASIN, no product name. So the item has to be
    inferred, and the only signal available is the amount inside that order.
    Precedence, most to least specific:

    ``exact_item``    exactly one non-cancelled item in the order costs exactly
                      the refund amount. The common case (80% of the real data).
    ``near_item``     exactly one item within ``NEAR_ITEM_TOLERANCE_CENTS``, which
                      absorbs the tax drift documented on that constant.
    ``whole_order``   the refund equals the order's whole item total. The order is
                      certain but the item is not one of them, it is all of them,
                      so ``item`` is None and the caller falls back to
                      order-level facts.
    ``ambiguous``     several items in the order match the amount. ``item`` is
                      None: the function will NOT pick one. A refund's attributed
                      item decides both the category it is booked to and the
                      recipient it is reported against, so guessing between two
                      $24.99 items means confidently telling the owner the wrong
                      person got the wrong thing. "We don't know which" is a true
                      answer; a coin flip that looks certain is not.
    ``unattributed``  nothing in the order matches. Real and expected: partial
                      refunds, refunded shipping, and gift-balance adjustments
                      have no single item behind them.

    Cancelled items are excluded throughout — they were never charged, so they
    can never have been refunded, and leaving them in would let a cancelled twin
    of a shipped item turn an exact match into a false ``ambiguous``.
    """
    amount = abs(int(refund.get("amount_cents") or 0))
    usable = [
        item for item in order_items
        if not item.get("is_cancelled") and item.get("total_cents") is not None
    ]

    if amount <= 0 or not usable:
        return {"item": None, "strategy": REFUND_STRATEGY_UNATTRIBUTED}

    exact = [i for i in usable if abs(int(i["total_cents"])) == amount]
    if len(exact) == 1:
        return {"item": exact[0], "strategy": REFUND_STRATEGY_EXACT}

    near = [
        i for i in usable
        if abs(abs(int(i["total_cents"])) - amount) <= NEAR_ITEM_TOLERANCE_CENTS
    ]
    if not exact and len(near) == 1:
        return {"item": near[0], "strategy": REFUND_STRATEGY_NEAR}

    # Checked before ``ambiguous``: "the whole order came back" is a certain fact
    # about the order even when the individual items are indistinguishable.
    if sum(abs(int(i["total_cents"])) for i in usable) == amount:
        return {"item": None, "strategy": REFUND_STRATEGY_WHOLE_ORDER}

    if exact or near:
        return {"item": None, "strategy": REFUND_STRATEGY_AMBIGUOUS}

    return {"item": None, "strategy": REFUND_STRATEGY_UNATTRIBUTED}


def plan_split(parent_amount_cents: int, items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Children for one matched charge. Pure function; children sum EXACTLY to parent.

    The residual line absorbs whatever the item totals don't account for
    (shipping, tax rounding, a gift-card offset). Without it the split silently
    changes the transaction's value, which is the one thing a splitter must
    never do — same contract as mortgage_splitter.
    """
    total = abs(int(parent_amount_cents))
    children: List[Dict[str, Any]] = []
    for item in items:
        amount = abs(int(item.get("total_cents") or 0))
        if amount == 0:
            continue
        children.append(
            {
                "amount_cents": amount,
                "product_name": item.get("product_name") or "Amazon item",
                "asin": item.get("asin"),
                "quantity": item.get("quantity") or 1,
                "is_residual": False,
            }
        )

    allocated = sum(c["amount_cents"] for c in children)
    residual = total - allocated

    if not children:
        return []

    if residual > 0:
        children.append(
            {
                "amount_cents": residual,
                "product_name": RESIDUAL_LABEL,
                "asin": None,
                "quantity": 1,
                "is_residual": True,
            }
        )
    elif residual < 0:
        # Items exceed the charge (gift-card balance or a partial capture).
        # Scale the last child down so the sum still lands exactly on the parent
        # rather than fabricating value.
        shortfall = -residual
        for child in reversed(children):
            take = min(shortfall, child["amount_cents"] - 1)
            if take <= 0:
                continue
            child["amount_cents"] -= take
            shortfall -= take
            if shortfall == 0:
                break
        if shortfall:
            children[-1]["amount_cents"] = max(children[-1]["amount_cents"] - shortfall, 0)

    assert sum(c["amount_cents"] for c in children) == total, (
        f"split must preserve value: {sum(c['amount_cents'] for c in children)} != {total}"
    )
    return children
