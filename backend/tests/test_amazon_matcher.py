"""Amazon charge/refund matching and per-item splitting.

The matcher is layered because a single strategy demonstrably under-matches: run
against the real export plus the real Chase 1236 statement, shipment-sum alone
matched ~54% of charges and the full ladder matches ~65%. Each layer here has a
test because each one exists for a charge that actually occurred.
"""
import pytest

from app.services.amazon_matcher import (
    RESIDUAL_LABEL,
    account_for_card,
    build_shipments,
    is_amazon_transaction,
    match_charges,
    match_refunds,
    plan_split,
)


def item(order_id, total, *, name="Thing", ship="2026-03-10", asin="A1", qty=1,
         cancelled=False, last4="0002", gift=False, order_date=None):
    return {
        "order_id": order_id,
        "order_date": order_date or ship,
        "ship_date": ship,
        "asin": asin,
        "product_name": name,
        "quantity": qty,
        "total_cents": total,
        "is_cancelled": cancelled,
        "card_last4": last4,
        "used_gift_card": gift,
        "is_digital": 0,
    }


def charge(txn_id, cents, date="2026-03-11", account="card-0002"):
    return {"id": txn_id, "account_id": account, "posted_date": date, "amount_cents": cents}


# --------------------------------------------------------------- merchant match

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Amazon.com*NO1FM5J33", True),
        ("AMZN Mktp US*JY5ZD6HP3", True),
        ("AMAZON MKTPL*RT4XY", True),
        ("Amazon Prime*4D93", True),
        ("WHOLEFDS DUR 123", True),
        ("CHICK-FIL-A #02138", False),
        ("US MOBILE", False),
        ("", False),
    ],
)
def test_is_amazon_transaction(text, expected):
    assert is_amazon_transaction(text, text) is expected


def test_account_for_card_ignores_cards_we_do_not_hold():
    accounts = ["card-0002", "card-0001"]
    assert account_for_card("0002", accounts) == "card-0002"
    assert account_for_card("0001", accounts) == "card-0001"
    assert account_for_card("0412", accounts) is None, "an untracked card must not match"
    assert account_for_card(None, accounts) is None, "gift-card-only orders have no card"


# ------------------------------------------------------------------- shipments

def test_build_shipments_groups_by_order_and_ship_date():
    items = [
        item("A", 1000, ship="2026-03-10", asin="X"),
        item("A", 2000, ship="2026-03-10", asin="Y"),
        item("A", 500, ship="2026-03-14", asin="Z"),
    ]
    shipments = sorted(build_shipments(items), key=lambda s: s["ship_date"])
    assert [s["total_cents"] for s in shipments] == [3000, 500]


def test_build_shipments_excludes_cancelled_items():
    """A cancelled item was never charged; counting it corrupts every total."""
    items = [item("A", 1000, asin="X"), item("A", 9999, asin="Y", cancelled=True)]
    shipments = build_shipments(items)
    assert len(shipments) == 1
    assert shipments[0]["total_cents"] == 1000


# --------------------------------------------------------------- charge layers

def test_layer1_matches_a_whole_shipment():
    items = [item("A", 1200, asin="X"), item("A", 3401, asin="Y")]
    result = match_charges([charge(1, -4601)], items, ["card-0002"])
    assert result["unmatched_charge_ids"] == []
    match = result["matches"][0]
    assert match["strategy"] == "shipment"
    assert len(match["items"]) == 2


def test_layer2_matches_a_single_item_of_a_multi_item_shipment():
    """Amazon captures part of a shipment: the charge equals one item."""
    items = [item("A", 1200, asin="X"), item("A", 3401, asin="Y")]
    result = match_charges([charge(1, -1200)], items, ["card-0002"])
    match = result["matches"][0]
    assert match["strategy"] == "item"
    assert match["items"][0]["asin"] == "X"


def test_layer3_matches_a_whole_order_billed_once():
    items = [
        item("A", 1000, ship="2026-03-10", asin="X"),
        item("A", 2000, ship="2026-03-12", asin="Y"),
    ]
    result = match_charges([charge(1, -3000, date="2026-03-13")], items, ["card-0002"])
    assert result["matches"][0]["strategy"] == "order"


def test_layer4_matches_an_arbitrary_subset():
    items = [
        item("A", 700, ship="2026-03-10", asin="X"),
        item("A", 1300, ship="2026-03-10", asin="Y"),
        item("A", 2500, ship="2026-03-10", asin="Z"),
    ]
    # 700 + 2500 = 3200; no shipment, item or order total equals that.
    result = match_charges([charge(1, -3200)], items, ["card-0002"])
    match = result["matches"][0]
    assert match["strategy"] == "subset"
    assert sorted(i["total_cents"] for i in match["items"]) == [700, 2500]


def test_subset_search_is_bounded_on_large_orders():
    """Refuse the exponential search rather than hang on a 20-item order."""
    items = [item("A", 100 + i, asin=f"A{i}") for i in range(20)]
    target = sum(i["total_cents"] for i in items[:3])
    result = match_charges([charge(1, -target)], items, ["card-0002"])
    assert result["unmatched_charge_ids"] == [1]


# ------------------------------------------------------- one-to-one and windows

def test_two_identical_charges_consume_two_different_shipments():
    """Two $10.00 Amazon charges must not both claim the same shipment."""
    items = [
        item("A", 1000, ship="2026-03-10", asin="X"),
        item("B", 1000, ship="2026-03-12", asin="Y"),
    ]
    result = match_charges(
        [charge(1, -1000, date="2026-03-11"), charge(2, -1000, date="2026-03-13")],
        items,
        ["card-0002"],
    )
    assert len(result["matches"]) == 2
    assert {m["order_id"] for m in result["matches"]} == {"A", "B"}


def test_a_single_shipment_cannot_be_claimed_twice():
    items = [item("A", 1000, ship="2026-03-10")]
    result = match_charges(
        [charge(1, -1000, date="2026-03-11"), charge(2, -1000, date="2026-03-12")],
        items,
        ["card-0002"],
    )
    assert len(result["matches"]) == 1
    assert result["unmatched_charge_ids"] == [2]


def test_charge_far_outside_the_window_is_not_matched():
    items = [item("A", 1000, ship="2026-01-01")]
    result = match_charges([charge(1, -1000, date="2026-06-01")], items, ["card-0002"])
    assert result["matches"] == []
    assert result["unmatched_charge_ids"] == [1]


def test_closest_date_wins_among_equal_amounts():
    items = [
        item("A", 5000, ship="2026-03-01", asin="far"),
        item("B", 5000, ship="2026-03-10", asin="near"),
    ]
    result = match_charges([charge(1, -5000, date="2026-03-11")], items, ["card-0002"])
    assert result["matches"][0]["order_id"] == "B"


def test_positive_amounts_are_ignored_by_charge_matching():
    """Credits are refunds; match_refunds owns them."""
    result = match_charges([charge(1, 4601)], [item("A", 4601)], ["card-0002"])
    assert result["matches"] == []
    assert result["unmatched_charge_ids"] == []


def test_unmatched_charges_are_reported_not_guessed():
    result = match_charges([charge(1, -777)], [item("A", 1000)], ["card-0002"])
    assert result["matches"] == []
    assert result["unmatched_charge_ids"] == [1]


# ----------------------------------------------------------------- refunds

def _refund(order_id, cents, date="2026-06-16", reason="Customer return"):
    return {"order_id": order_id, "amount_cents": cents, "refund_date": date, "reason": reason}


def test_refund_matches_a_card_credit():
    credits = [charge(10, 6433, date="2026-06-18")]
    result = match_refunds(credits, [_refund("A", 6433)])
    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["transaction_id"] == 10
    assert match["order_id"] == "A"
    assert match["amount_cents"] == 6433


def test_refund_to_gift_balance_has_no_card_credit_and_is_not_invented():
    """Amazon refunds to gift balance never touch the bank — no row to match."""
    result = match_refunds([], [_refund("A", 5000)])
    assert result["matches"] == []
    assert result["unmatched_credit_ids"] == []


def test_card_credit_with_no_matching_refund_record_is_reported():
    result = match_refunds([charge(10, 1234)], [])
    assert result["matches"] == []
    assert result["unmatched_credit_ids"] == [10]


def test_refund_one_to_one_across_identical_amounts():
    credits = [charge(10, 5000, date="2026-06-17"), charge(11, 5000, date="2026-06-25")]
    refunds = [_refund("A", 5000, date="2026-06-16"), _refund("B", 5000, date="2026-06-24")]
    result = match_refunds(credits, refunds)
    assert len(result["matches"]) == 2
    assert {m["order_id"] for m in result["matches"]} == {"A", "B"}


def test_refund_outside_window_is_not_matched():
    result = match_refunds([charge(10, 5000, date="2026-09-01")], [_refund("A", 5000, date="2026-01-01")])
    assert result["matches"] == []
    assert result["unmatched_credit_ids"] == [10]


def test_negative_amounts_are_ignored_by_refund_matching():
    result = match_refunds([charge(10, -5000)], [_refund("A", 5000)])
    assert result["matches"] == []


# ------------------------------------------------------------------- splitting

def test_split_children_sum_exactly_to_parent():
    items = [item("A", 1246, name="Phone holder"), item("A", 1074, name="Copy paper"),
             item("A", 17199, name="Laser printer")]
    children = plan_split(-19519, items)
    assert sum(c["amount_cents"] for c in children) == 19519


def test_split_adds_a_residual_line_for_shipping_and_tax():
    children = plan_split(-1500, [item("A", 1000, name="Widget")])
    assert len(children) == 2
    residual = [c for c in children if c["is_residual"]]
    assert len(residual) == 1
    assert residual[0]["amount_cents"] == 500
    assert residual[0]["product_name"] == RESIDUAL_LABEL
    assert sum(c["amount_cents"] for c in children) == 1500


def test_split_with_no_residual_has_no_extra_line():
    children = plan_split(-1000, [item("A", 600, name="A"), item("A", 400, name="B")])
    assert len(children) == 2
    assert not any(c["is_residual"] for c in children)


def test_split_when_items_exceed_the_charge_still_preserves_value():
    """Gift-card balance makes the card charge smaller than the items."""
    children = plan_split(-1000, [item("A", 800, name="A"), item("A", 700, name="B")])
    assert sum(c["amount_cents"] for c in children) == 1000
    assert all(c["amount_cents"] >= 0 for c in children)


def test_split_carries_product_identity_for_categorization():
    children = plan_split(-17199, [item("A", 17199, name="Brother HL-L2460DW Laser Printer", asin="B0XYZ")])
    assert children[0]["product_name"] == "Brother HL-L2460DW Laser Printer"
    assert children[0]["asin"] == "B0XYZ"


def test_split_of_zero_value_items_returns_nothing():
    assert plan_split(-1000, [item("A", 0, name="Free")]) == []
    assert plan_split(-1000, []) == []


@pytest.mark.parametrize("parent,totals", [
    (-4601, [1200, 3401]),
    (-19519, [1246, 1074, 17199]),
    (-100, [33, 33, 33]),
    (-9999, [10000]),
    (-5000, [1, 1, 1]),
])
def test_split_value_preservation_is_unconditional(parent, totals):
    children = plan_split(parent, [item("A", t, name=f"i{t}", asin=f"a{t}") for t in totals])
    assert sum(c["amount_cents"] for c in children) == abs(parent)
