"""Refund pairing — a charge matched to the credit that reversed it.

The rules are deliberately conservative because a wrong pair HIDES two real
transactions from the transactions list. The partial-refund thresholds exist
because of a concrete false positive on live data: a $1.00 credit paired to a
$20.00 charge 29 days earlier (5%), which was actually an unrelated small refund
from the same merchant.
"""
import pytest

from app.services.refund_pairs import (
    PARTIAL_MIN_RATIO,
    PARTIAL_WINDOW_DAYS,
    REFUND_WINDOW_DAYS,
    find_pairs,
    normalize_merchant,
)


def txn(tid, amount, date, merchant="Lowe's", entity=None, category="Shopping"):
    return {
        "id": tid, "amount_cents": amount, "posted_date": date, "merchant": merchant,
        "description": merchant, "category_path": category,
        "entity_type": entity or ("income" if amount > 0 else "expense"),
    }


def test_exact_amount_credit_pairs_as_a_full_refund():
    pairs = find_pairs([txn(1, -14764, "2026-03-01"), txn(2, 14764, "2026-03-10")])
    assert len(pairs) == 1
    assert pairs[0]["charge_id"] == 1 and pairs[0]["credit_id"] == 2
    assert pairs[0]["partial"] is False
    assert pairs[0]["days_to_refund"] == 9


def test_smaller_credit_pairs_as_partial():
    pairs = find_pairs([txn(1, -10000, "2026-03-01"), txn(2, 6000, "2026-03-05")])
    assert len(pairs) == 1
    assert pairs[0]["partial"] is True


def test_tiny_credit_is_not_treated_as_a_partial_refund():
    """The live false positive: $1.00 back on a $20.00 charge is not its refund."""
    pairs = find_pairs([txn(1, -2000, "2026-03-01"), txn(2, 100, "2026-03-30")])
    assert pairs == []


def test_partial_outside_the_short_window_is_rejected():
    beyond = f"2026-06-30"  # ~120 days after, well past PARTIAL_WINDOW_DAYS
    pairs = find_pairs([txn(1, -10000, "2026-03-01"), txn(2, 6000, beyond)])
    assert pairs == []


def test_exact_match_still_allowed_in_the_long_window():
    """An exact amount from the same merchant is near-proof, so it gets 180 days."""
    pairs = find_pairs([txn(1, -10000, "2026-01-01"), txn(2, 10000, "2026-05-01")])
    assert len(pairs) == 1
    assert pairs[0]["partial"] is False


def test_credit_before_the_charge_is_never_a_refund_of_it():
    pairs = find_pairs([txn(1, -5000, "2026-03-10"), txn(2, 5000, "2026-03-01")])
    assert pairs == []


def test_credit_larger_than_the_charge_is_rejected():
    pairs = find_pairs([txn(1, -5000, "2026-03-01"), txn(2, 9000, "2026-03-05")])
    assert pairs == []


def test_one_to_one_two_identical_refunds_claim_two_charges():
    rows = [txn(1, -5000, "2026-03-01"), txn(2, -5000, "2026-03-02"),
            txn(3, 5000, "2026-03-10"), txn(4, 5000, "2026-03-11")]
    pairs = find_pairs(rows)
    assert len(pairs) == 2
    assert {p["charge_id"] for p in pairs} == {1, 2}
    assert {p["credit_id"] for p in pairs} == {3, 4}


def test_a_single_charge_cannot_be_refunded_twice():
    rows = [txn(1, -5000, "2026-03-01"), txn(2, 5000, "2026-03-10"), txn(3, 5000, "2026-03-11")]
    pairs = find_pairs(rows)
    assert len(pairs) == 1


def test_closest_date_wins_among_equal_amounts():
    rows = [txn(1, -5000, "2026-01-01"), txn(2, -5000, "2026-03-08"), txn(3, 5000, "2026-03-10")]
    pairs = find_pairs(rows)
    assert pairs[0]["charge_id"] == 2


def test_different_merchants_never_pair():
    pairs = find_pairs([txn(1, -5000, "2026-03-01", merchant="Lowe's"),
                        txn(2, 5000, "2026-03-05", merchant="Ulta Beauty")])
    assert pairs == []


def test_merchant_spelling_variants_still_pair():
    pairs = find_pairs([txn(1, -2000, "2026-03-01", merchant="SUBSCRIBECO.COM"),
                        txn(2, 2000, "2026-03-05", merchant="Subscribecos")])
    assert len(pairs) == 1


@pytest.mark.parametrize("entity", ["transfer", "excluded", "asset_conversion"])
def test_transfers_and_excluded_rows_never_pair(entity):
    """Internal movement and opted-out rows are not purchases to refund."""
    pairs = find_pairs([txn(1, -5000, "2026-03-01", entity=entity),
                        txn(2, 5000, "2026-03-05", entity=entity)])
    assert pairs == []


def test_normalize_merchant_collapses_variants():
    assert normalize_merchant("SUBSCRIBECO.COM") == normalize_merchant("Subscribecos")
    assert normalize_merchant("Amazon.com") == normalize_merchant("AMAZON")
    assert normalize_merchant("Lowe's") != normalize_merchant("Ulta")


def test_thresholds_are_documented_values():
    assert REFUND_WINDOW_DAYS == 180
    assert PARTIAL_WINDOW_DAYS == 60
    assert PARTIAL_MIN_RATIO == 0.25
