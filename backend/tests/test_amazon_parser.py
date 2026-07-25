"""Amazon export parsing, including the traps in the real file format.

Every quirk asserted here was observed in the real ``Your Orders.zip`` export
before the parser was written — Excel-escaped discounts, 'No Refund' strings,
per-item vs shipment-level amounts, gift-card payment methods.
"""
import csv
import os

import pytest

from app.parsers.amazon import (
    AmazonParseError,
    card_last4,
    parse_date,
    parse_export,
    parse_money_cents,
    parse_order_history,
    parse_refund_details,
    parse_returns_status,
    recipient_name,
    refund_fingerprint,
    uses_gift_card,
)

ORDER_HEADERS = [
    "ASIN", "Billing Address", "Carrier Name & Tracking Number", "Currency", "Gift Message",
    "Gift Recipient Contact", "Gift Sender Name", "Item Serial Number", "Order Date", "Order ID",
    "Order Status", "Original Quantity", "Payment Method Type", "Product Condition",
    "Product Name", "Purchase Order Number", "Ship Date", "Shipment Item Subtotal",
    "Shipment Item Subtotal Tax", "Shipment Status", "Shipping Address", "Shipping Charge",
    "Shipping Option", "Total Amount", "Total Discounts", "Unit Price", "Unit Price Tax", "Website",
]


def _write_orders(tmp_path, rows):
    path = tmp_path / "Order History.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=ORDER_HEADERS)
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in ORDER_HEADERS})
    return str(path)


def _order_row(**overrides):
    row = {
        "Order ID": "111-2223334-5556667",
        "Order Date": "2026-04-27T00:50:45Z",
        "Ship Date": "2026-04-28T10:00:00Z",
        "ASIN": "B0CC629SJK",
        "Product Name": "Fireproof Key Safe",
        "Original Quantity": "1",
        "Unit Price": "199.99",
        "Unit Price Tax": "13.5",
        "Shipment Item Subtotal": "199.99",
        "Shipment Item Subtotal Tax": "13.5",
        "Total Amount": "193.49",
        "Shipping Charge": "0",
        "Total Discounts": "'-20'",
        "Payment Method Type": "Visa - 0002",
        "Order Status": "Closed",
        "Shipment Status": "Shipped",
        "Shipping Address": "Ben Whitfield Jr 120 MAPLE ST SPRINGFIELD IL 62704-1234 United States",
        "Website": "Amazon.com",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------- money parsing

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("199.99", 19999),
        ("0", 0),
        ("'-20'", -2000),          # Excel text-escape, already negative
        ("$1,234.56", 123456),
        ("13.5", 1350),
        ("No Refund", None),        # Returns Status.csv literal
        ("Not Applicable", None),
        ("", None),
        (None, None),
        ("(45.00)", -4500),
    ],
)
def test_parse_money_cents(raw, expected):
    assert parse_money_cents(raw) == expected


def test_no_refund_is_none_not_zero():
    """None vs 0 is the difference between 'no refund' and 'a refund of $0'."""
    assert parse_money_cents("No Refund") is None
    assert parse_money_cents("0") == 0


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-06-16T19:05:51.129Z", "2026-06-16"),
        ("2024-01-30T04:16:10Z", "2024-01-30"),
        ("2026-06-16", "2026-06-16"),
        ("4/27/2026", "2026-04-27"),
        ("", None),
        (None, None),
        ("No Refund", None),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    "method,last4,gift",
    [
        ("Visa - 0002", "0002", False),
        ("Gift Certificate/Card and Visa - 0002", "0002", True),
        ("Gift Certificate/Card", None, True),
        ("MasterCard - 0412", "0412", False),
        ("", None, False),
    ],
)
def test_payment_method_parsing(method, last4, gift):
    assert card_last4(method) == last4
    assert uses_gift_card(method) is gift


def test_recipient_name_stops_at_the_street_number():
    assert (
        recipient_name("Ben Whitfield Jr 120 MAPLE ST SPRINGFIELD IL 62704-1234 United States")
        == "Ben Whitfield Jr"
    )
    assert recipient_name("") is None
    assert recipient_name(None) is None


# ---------------------------------------------------------------- order history

def test_total_amount_is_item_level_and_includes_tax_less_discount(tmp_path):
    """199.99 + 13.50 - 20.00 = 193.49 — verified against the real export."""
    items = parse_order_history(_write_orders(tmp_path, [_order_row()]))
    assert len(items) == 1
    item = items[0]
    assert item["total_cents"] == 19349
    assert item["unit_price_cents"] == 19999
    assert item["unit_price_tax_cents"] == 1350
    assert item["discount_cents"] == -2000
    assert item["unit_price_cents"] + item["unit_price_tax_cents"] + item["discount_cents"] == item["total_cents"]


def test_order_fields_are_normalised(tmp_path):
    item = parse_order_history(_write_orders(tmp_path, [_order_row()]))[0]
    assert item["order_id"] == "111-2223334-5556667"
    assert item["order_date"] == "2026-04-27"
    assert item["ship_date"] == "2026-04-28"
    assert item["card_last4"] == "0002"
    assert item["recipient"] == "Ben Whitfield Jr"
    assert item["is_cancelled"] is False
    assert item["is_digital"] == 0


def test_cancelled_orders_are_flagged(tmp_path):
    rows = [_order_row(**{"Order Status": "Cancelled"})]
    assert parse_order_history(_write_orders(tmp_path, rows))[0]["is_cancelled"] is True


def test_quantity_defaults_to_one_when_blank(tmp_path):
    rows = [_order_row(**{"Original Quantity": ""})]
    assert parse_order_history(_write_orders(tmp_path, rows))[0]["quantity"] == 1


def test_multi_quantity_is_parsed_as_an_integer(tmp_path):
    rows = [_order_row(**{"Original Quantity": "2", "Unit Price": "25.99", "Total Amount": "55.88"})]
    item = parse_order_history(_write_orders(tmp_path, rows))[0]
    assert item["quantity"] == 2
    assert item["total_cents"] == 5588


def test_missing_required_column_raises_listing_actual_headers(tmp_path):
    path = tmp_path / "Order History.csv"
    path.write_text("Order ID,Something Else\n1,2\n", encoding="utf-8")
    with pytest.raises(AmazonParseError) as excinfo:
        parse_order_history(str(path))
    message = str(excinfo.value)
    assert "Total Amount" in message
    assert "Something Else" in message, "the error must show what WAS present, to debug a format change"


# --------------------------------------------------------------------- refunds

def test_refund_details_parsing(tmp_path):
    path = tmp_path / "Refund Details.csv"
    path.write_text(
        "Creation Date,Currency,Direct Debit Refund Amount,Disbursement Type,Order ID,Payment Status,"
        "Quantity,Refund Amount,Refund Date,Reversal Amount State,Reversal Reason,Reversal Status,Website\n"
        "2026-06-16T19:05:51.129Z,USD,,Refund,111-8976293-9682633,Completed,1,64.33,"
        "2026-06-16T22:11:21.199Z,,Customer return,,Amazon.com\n",
        encoding="utf-8",
    )
    refunds = parse_refund_details(str(path))
    assert len(refunds) == 1
    assert refunds[0]["amount_cents"] == 6433
    assert refunds[0]["amount_cents"] > 0, "refunds are positive — money coming back"
    assert refunds[0]["refund_date"] == "2026-06-16"
    assert refunds[0]["order_id"] == "111-8976293-9682633"


def test_returns_status_skips_no_refund_rows(tmp_path):
    path = tmp_path / "Returns Status.csv"
    path.write_text(
        "Order ID,Date of Return,Return Amount,Return Reason,Return Resolution,Return Receivable State\n"
        "111-1111111-1111111,2026-01-05T00:00:00Z,No Refund,Changed mind,Refund,Closed\n"
        "222-2222222-2222222,2026-01-06T00:00:00Z,25.00,Defective,Refund,Closed\n"
        "333-3333333-3333333,2026-01-07T00:00:00Z,30.00,Defective,Replacement,Closed\n",
        encoding="utf-8",
    )
    refunds = parse_returns_status(str(path))
    order_ids = {r["order_id"] for r in refunds}
    assert "111-1111111-1111111" not in order_ids, "'No Refund' must not become a $0 refund"
    assert "222-2222222-2222222" in order_ids
    assert "333-3333333-3333333" not in order_ids, "a Replacement returns goods, not money"


def test_refund_fingerprint_collapses_the_same_refund_from_two_files():
    a = {"order_id": "X", "refund_date": "2026-01-01", "amount_cents": 1000, "source_file": "Refund Details.csv"}
    b = {"order_id": "X", "refund_date": "2026-01-01", "amount_cents": 1000, "source_file": "Returns Status.csv"}
    different = {"order_id": "X", "refund_date": "2026-01-01", "amount_cents": 1001}
    assert refund_fingerprint(a) == refund_fingerprint(b)
    assert refund_fingerprint(a) != refund_fingerprint(different)


def test_parse_export_requires_a_recognised_file(tmp_path):
    with pytest.raises(AmazonParseError, match="No recognised Amazon export files"):
        parse_export(str(tmp_path))


def test_parse_export_finds_nested_files_and_dedups_refunds(tmp_path):
    orders_dir = tmp_path / "Your Amazon Orders"
    orders_dir.mkdir()
    _write_orders(orders_dir, [_order_row()])
    refunds_dir = tmp_path / "Your Returns & Refunds"
    refunds_dir.mkdir()
    (refunds_dir / "Refund Details.csv").write_text(
        "Order ID,Refund Amount,Refund Date,Creation Date,Payment Status,Disbursement Type,Reversal Reason\n"
        "111-2223334-5556667,193.49,2026-05-02T00:00:00Z,2026-05-01T00:00:00Z,Completed,Refund,Customer return\n",
        encoding="utf-8",
    )
    (refunds_dir / "Returns Status.csv").write_text(
        "Order ID,Date of Return,Return Amount,Return Reason,Return Resolution,Return Receivable State\n"
        "111-2223334-5556667,2026-05-02T00:00:00Z,193.49,Changed mind,Refund,Closed\n",
        encoding="utf-8",
    )
    result = parse_export(str(tmp_path))
    assert len(result["items"]) == 1
    assert len(result["refunds"]) == 1, "same refund in two files must collapse to one"


# ------------------------------------------------- the real export, if present

REAL_EXPORT = os.path.join(
    os.path.dirname(__file__), "..", "..", "samples", "amazon"
)


@pytest.mark.skipif(
    not os.path.exists(os.path.join(REAL_EXPORT, "Your Amazon Orders", "Order History.csv")),
    reason="real Amazon export not unpacked in samples/amazon (gitignored)",
)
def test_real_export_parses_cleanly():
    result = parse_export(REAL_EXPORT)
    items, refunds = result["items"], result["refunds"]
    assert len(items) > 100
    assert all(i["order_id"] for i in items)
    assert all(i["total_cents"] is not None for i in items), "every item needs a matchable amount"
    assert any(i["card_last4"] for i in items), "the payment method's card last-4 should be parsed"
    assert refunds and all(r["amount_cents"] > 0 for r in refunds)
    assert any(i["is_cancelled"] for i in items), "cancelled orders exist and must be flagged"
