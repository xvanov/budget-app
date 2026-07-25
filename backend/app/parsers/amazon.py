"""Amazon "Request My Data" export parser — orders, digital orders, refunds.

Source: the ``Your Orders.zip`` export, unpacked into ``samples/amazon/``. The
scraper this module originally shared a contract with is gone — the official
export supersedes it and is authoritative.

Field semantics that are NOT obvious and were verified against the real export
before this was written:

* ``Total Amount`` in ``Order History.csv`` is **per item**, not per shipment:
  199.99 unit + 13.50 tax - 20.00 discount = 193.49. It already includes tax and
  discounts, which makes it the right number to reconcile against a card charge.
* ``Shipment Item Subtotal`` is **shipment-level** and repeats across the items
  of one shipment, so summing it per item double-counts. Do not use it as an
  item amount.
* ``Total Discounts`` arrives Excel-escaped as ``"'-20'"`` — leading apostrophe,
  already negative.
* ``Return Amount`` in ``Returns Status.csv`` can be the literal string
  ``No Refund``. Parsing it as a number silently produces 0 and invents refunds
  that never happened.
* ``Payment Method Type`` carries the card: ``Visa - 1236``,
  ``Gift Certificate/Card and Visa - 1236``, ``Gift Certificate/Card``. The
  last-4 maps to an account; a gift-card-only order never hits a bank account
  at all, and a mixed order charges the card LESS than the order total.
* ``Order Status`` of ``Cancelled`` was never charged and must never be matched.
* Dates are ISO-8601 UTC with varying precision
  (``2026-06-16T19:05:51.129Z``, ``2024-01-30T04:16:10Z``).
"""
import csv
import glob
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Filenames inside the export we care about, relative to the export root.
ORDER_HISTORY = "Order History.csv"
DIGITAL_ORDERS = "Digital Content Orders.csv"
REFUND_DETAILS = "Refund Details.csv"
RETURNS_STATUS = "Returns Status.csv"
DIGITAL_RETURNS = "Digital Returns.csv"
ORDERS_RETURNS_EXTRA = "Your Orders.Returns.1.csv"

NON_NUMERIC = {"no refund", "not applicable", "n/a", "na", "none", "-", ""}


class AmazonParseError(ValueError):
    """Raised when a file's headers don't match what this parser understands."""


def parse_money_cents(value: Optional[str]) -> Optional[int]:
    """'$1,234.56' | "'-20'" | '' | 'No Refund' -> cents | None.

    Returns None (not 0) for non-numeric values so callers can tell "no refund"
    apart from "a refund of nothing".
    """
    if value is None:
        return None
    s = str(value).strip().strip("'\"").strip()
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    if s.lower() in NON_NUMERIC:
        return None
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    try:
        cents = int(round(float(s) * 100))
    except (TypeError, ValueError):
        return None
    return -cents if negative else cents


def parse_date(value: Optional[str]) -> Optional[str]:
    """ISO-8601 (any precision, trailing Z) -> 'YYYY-MM-DD'."""
    if not value:
        return None
    s = str(value).strip()
    if not s or s.lower() in NON_NUMERIC:
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if match:
        return f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}"
    return None


def card_last4(payment_method: Optional[str]) -> Optional[str]:
    """'Gift Certificate/Card and Visa - 1236' -> '1236'; gift-card-only -> None."""
    if not payment_method:
        return None
    matches = re.findall(r"(\d{4})\b", payment_method)
    return matches[-1] if matches else None


def uses_gift_card(payment_method: Optional[str]) -> bool:
    """True when a gift balance covered some or all of the order.

    Matters because the card is then charged LESS than the order total, so an
    exact-amount match against the order will legitimately fail.
    """
    return bool(payment_method) and "gift" in payment_method.lower()


def recipient_name(shipping_address: Optional[str]) -> Optional[str]:
    """Leading name from Amazon's flattened address string.

    This is the only buyer signal in the export — the account is shared, so it
    is how an order gets attributed to a person.
    """
    if not shipping_address:
        return None
    text = str(shipping_address).strip()
    if not text:
        return None
    # Address begins at the first number-led token ("8424 EAGLE VIEW DR").
    parts = text.split()
    name_parts: List[str] = []
    for part in parts:
        if re.match(r"^\d", part):
            break
        name_parts.append(part)
        if len(name_parts) >= 5:
            break
    return " ".join(name_parts).strip() or None


def _require_headers(path: str, actual: List[str], required: List[str]) -> None:
    missing = [h for h in required if h not in (actual or [])]
    if missing:
        raise AmazonParseError(
            f"{os.path.basename(path)}: missing expected column(s) {missing}. "
            f"Headers present: {actual}"
        )


def _read(path: str) -> Tuple[List[Dict[str, str]], List[str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        headers = list(reader.fieldnames or [])
    return rows, headers


def parse_order_history(path: str) -> List[Dict[str, Any]]:
    """One dict per ordered item (physical goods)."""
    rows, headers = _read(path)
    _require_headers(
        path, headers, ["Order ID", "Order Date", "Product Name", "Total Amount", "ASIN"]
    )
    items: List[Dict[str, Any]] = []
    for row in rows:
        order_id = (row.get("Order ID") or "").strip()
        if not order_id:
            continue
        quantity = parse_money_cents(row.get("Original Quantity"))
        quantity = int(quantity / 100) if quantity else 1
        payment_method = (row.get("Payment Method Type") or "").strip()
        items.append(
            {
                "order_id": order_id,
                "order_date": parse_date(row.get("Order Date")),
                "ship_date": parse_date(row.get("Ship Date")) or parse_date(row.get("Order Date")),
                "asin": (row.get("ASIN") or "").strip() or None,
                "product_name": (row.get("Product Name") or "").strip(),
                "quantity": max(quantity, 1),
                "unit_price_cents": parse_money_cents(row.get("Unit Price")),
                "unit_price_tax_cents": parse_money_cents(row.get("Unit Price Tax")),
                "shipping_charge_cents": parse_money_cents(row.get("Shipping Charge")),
                "discount_cents": parse_money_cents(row.get("Total Discounts")),
                # Item-level, tax- and discount-inclusive. The number to match on.
                "total_cents": parse_money_cents(row.get("Total Amount")),
                "order_status": (row.get("Order Status") or "").strip(),
                "shipment_status": (row.get("Shipment Status") or "").strip(),
                "payment_method": payment_method,
                "card_last4": card_last4(payment_method),
                "used_gift_card": uses_gift_card(payment_method),
                "recipient": recipient_name(row.get("Shipping Address")),
                "website": (row.get("Website") or "").strip(),
                "is_digital": 0,
                "is_cancelled": (row.get("Order Status") or "").strip().lower() == "cancelled",
                "source_file": os.path.basename(path),
            }
        )
    return items


def parse_digital_orders(path: str) -> List[Dict[str, Any]]:
    """Digital purchases (Kindle, apps, video). These hit the card too."""
    rows, headers = _read(path)
    _require_headers(path, headers, ["Order ID", "Order Date", "Product Name"])
    items: List[Dict[str, Any]] = []
    for row in rows:
        order_id = (row.get("Order ID") or "").strip()
        if not order_id:
            continue
        status = (row.get("Order Status") or "").strip()
        # Prefer the amount actually transacted; fall back to list price + tax.
        total = parse_money_cents(row.get("Transaction Amount"))
        if total is None:
            price = parse_money_cents(row.get("Price")) or 0
            tax = parse_money_cents(row.get("Price Tax")) or 0
            total = price + tax if (price or tax) else None
        quantity = parse_money_cents(row.get("Quantity Ordered"))
        quantity = int(quantity / 100) if quantity else 1
        payment_method = (row.get("Payment Information") or "").strip()
        items.append(
            {
                "order_id": order_id,
                "order_date": parse_date(row.get("Order Date")),
                "ship_date": parse_date(row.get("Fulfilled Date")) or parse_date(row.get("Order Date")),
                "asin": (row.get("ASIN") or "").strip() or None,
                "product_name": (row.get("Product Name") or "").strip(),
                "quantity": max(quantity, 1),
                "unit_price_cents": parse_money_cents(row.get("Price")),
                "unit_price_tax_cents": parse_money_cents(row.get("Price Tax")),
                "shipping_charge_cents": 0,
                "discount_cents": None,
                "total_cents": total,
                "order_status": status,
                "shipment_status": (row.get("Fullfilment Status") or "").strip(),
                "payment_method": payment_method,
                "card_last4": card_last4(payment_method),
                "used_gift_card": uses_gift_card(payment_method),
                "recipient": None,
                "website": (row.get("Marketplace") or "").strip(),
                "is_digital": 1,
                "is_cancelled": status.lower() in ("cancelled", "canceled"),
                "source_file": os.path.basename(path),
            }
        )
    return items


def parse_refund_details(path: str) -> List[Dict[str, Any]]:
    """``Refund Details.csv`` — the authoritative money-back record.

    Amount is positive (money returned to the payment method).
    """
    rows, headers = _read(path)
    _require_headers(path, headers, ["Order ID", "Refund Amount"])
    refunds: List[Dict[str, Any]] = []
    for row in rows:
        order_id = (row.get("Order ID") or "").strip()
        amount = parse_money_cents(row.get("Refund Amount"))
        if not order_id or not amount:
            continue
        refunds.append(
            {
                "order_id": order_id,
                "refund_date": parse_date(row.get("Refund Date"))
                or parse_date(row.get("Creation Date")),
                "amount_cents": abs(amount),
                "quantity": 1,
                "reason": (row.get("Reversal Reason") or "").strip(),
                "status": (row.get("Payment Status") or "").strip(),
                "disbursement_type": (row.get("Disbursement Type") or "").strip(),
                "source_file": os.path.basename(path),
            }
        )
    return refunds


def parse_returns_status(path: str) -> List[Dict[str, Any]]:
    """``Returns Status.csv`` — return events, some with no refund at all.

    Only rows with a real ``Return Amount`` and a refund resolution become
    refund candidates. ``Refund Details.csv`` is preferred where both describe
    the same money; the dedup in the ingest layer reconciles them.
    """
    rows, headers = _read(path)
    _require_headers(path, headers, ["Order ID", "Return Amount"])
    refunds: List[Dict[str, Any]] = []
    for row in rows:
        order_id = (row.get("Order ID") or "").strip()
        amount = parse_money_cents(row.get("Return Amount"))  # 'No Refund' -> None
        if not order_id or not amount:
            continue
        resolution = (row.get("Return Resolution") or "").strip()
        if resolution and "refund" not in resolution.lower():
            # Replacements and repairs return goods, not money.
            continue
        refunds.append(
            {
                "order_id": order_id,
                "refund_date": parse_date(row.get("Date of Return"))
                or parse_date(row.get("Return Creation Date")),
                "amount_cents": abs(amount),
                "quantity": 1,
                "reason": (row.get("Return Reason") or "").strip(),
                "status": (row.get("Return Receivable State") or "").strip(),
                "disbursement_type": resolution or "Return",
                "source_file": os.path.basename(path),
            }
        )
    return refunds


def parse_digital_returns(path: str) -> List[Dict[str, Any]]:
    rows, headers = _read(path)
    _require_headers(path, headers, ["Order ID"])
    refunds: List[Dict[str, Any]] = []
    for row in rows:
        order_id = (row.get("Order ID") or "").strip()
        amount = parse_money_cents(row.get("Amount Refunded")) or parse_money_cents(
            row.get("Transaction Amount")
        )
        if not order_id or not amount:
            continue
        refunds.append(
            {
                "order_id": order_id,
                "refund_date": parse_date(row.get("Return Date")),
                "amount_cents": abs(amount),
                "quantity": 1,
                "reason": (row.get("Reason Code") or "").strip(),
                "status": (row.get("Return Status") or row.get("Status") or "").strip(),
                "disbursement_type": "Digital Refund",
                "source_file": os.path.basename(path),
            }
        )
    return refunds


def parse_orders_returns_extra(path: str) -> List[Dict[str, Any]]:
    """``Additional Data/Your Orders.Returns.1.csv`` — per-return-item refunds."""
    rows, headers = _read(path)
    _require_headers(path, headers, ["Order ID", "Refund Amount"])
    refunds: List[Dict[str, Any]] = []
    for row in rows:
        order_id = (row.get("Order ID") or "").strip()
        amount = parse_money_cents(row.get("Refund Amount"))
        if not order_id or not amount:
            continue
        status = (row.get("Refund Status") or "").strip()
        if status and "not" in status.lower() and "refund" in status.lower():
            continue  # explicit non-refund
        refunds.append(
            {
                "order_id": order_id,
                "refund_date": parse_date(row.get("Request Date")),
                "amount_cents": abs(amount),
                "quantity": 1,
                "reason": (row.get("Reason for Non-Refund") or "").strip(),
                "status": status,
                "disbursement_type": (row.get("Refund Destination") or "Refund").strip(),
                "source_file": os.path.basename(path),
            }
        )
    return refunds


def refund_fingerprint(refund: Dict[str, Any]) -> str:
    """Identity for a refund across the several files that describe it.

    ``Refund Details.csv``, ``Returns Status.csv`` and the Additional Data file
    all describe overlapping refunds, and re-importing must not re-add them.
    Order + date + amount is the strongest identity available — none of the
    files carry a usable stable refund id across all three.
    """
    return f"{refund['order_id']}|{refund.get('refund_date') or ''}|{refund['amount_cents']}"


def discover_export_files(root: str) -> Dict[str, List[str]]:
    """Locate the known files anywhere under an unpacked export."""
    found: Dict[str, List[str]] = {}
    for name, key in (
        (ORDER_HISTORY, "orders"),
        (DIGITAL_ORDERS, "digital_orders"),
        (REFUND_DETAILS, "refunds"),
        (RETURNS_STATUS, "returns_status"),
        (DIGITAL_RETURNS, "digital_returns"),
        (ORDERS_RETURNS_EXTRA, "returns_extra"),
    ):
        matches = sorted(glob.glob(os.path.join(root, "**", name), recursive=True))
        matches += sorted(glob.glob(os.path.join(root, name)))
        if matches:
            found[key] = sorted(set(matches))
    return found


# Directory names Amazon itself uses inside the export. Their presence means
# "this directory IS one person's export", as opposed to a parent directory
# holding one subdirectory per person.
EXPORT_STRUCTURE_DIRS = {
    "Your Amazon Orders",
    "Your Returns & Refunds",
    "Additional Data",
    "Whole Foods Orders",
    "Your Subscriptions & Payment Plans",
}

DEFAULT_OWNER = os.environ.get("AMAZON_DEFAULT_OWNER", "kalin")


def discover_owner_exports(root: str) -> Dict[str, str]:
    """Map owner -> export directory.

    The account is shared but each person's data arrives as its own export, and
    only one person's is available at a time. Two supported layouts:

        samples/amazon/Your Amazon Orders/...        -> {DEFAULT_OWNER: root}
        samples/amazon/kalin/Your Amazon Orders/...  -> {'kalin': ...}
        samples/amazon/sophia/Your Amazon Orders/... -> {'sophia': ...}

    Distinguishing them matters: a recursive scan of a parent directory would
    silently merge two people's orders into one pile, and the owner is then
    unrecoverable because the export carries no reliable buyer field (the
    shipping name is the account holder's on every row).
    """
    if not os.path.isdir(root):
        return {}
    entries = sorted(os.listdir(root))
    if any(entry in EXPORT_STRUCTURE_DIRS for entry in entries):
        return {DEFAULT_OWNER: root}
    owners = {
        entry: os.path.join(root, entry)
        for entry in entries
        if os.path.isdir(os.path.join(root, entry)) and not entry.startswith(".")
    }
    # A directory with no Amazon structure and no subdirectories is still worth
    # attempting as a single export so the error message comes from parse_export.
    return owners or {DEFAULT_OWNER: root}


def parse_export(root: str) -> Dict[str, List[Dict[str, Any]]]:
    """Parse every recognised file under ``root`` into items + refunds.

    A file whose headers don't match raises rather than being skipped quietly —
    a silently-empty Amazon ingest looks identical to "you have no orders".
    """
    files = discover_export_files(root)
    if not files:
        raise AmazonParseError(
            f"No recognised Amazon export files under {root}. Expected at least "
            f"'{ORDER_HISTORY}' (unpack Your Orders.zip into this directory)."
        )

    items: List[Dict[str, Any]] = []
    refunds: List[Dict[str, Any]] = []
    for path in files.get("orders", []):
        items += parse_order_history(path)
    for path in files.get("digital_orders", []):
        items += parse_digital_orders(path)
    for path in files.get("refunds", []):
        refunds += parse_refund_details(path)
    for path in files.get("returns_status", []):
        refunds += parse_returns_status(path)
    for path in files.get("digital_returns", []):
        refunds += parse_digital_returns(path)
    for path in files.get("returns_extra", []):
        refunds += parse_orders_returns_extra(path)

    # Collapse the same refund described by several files.
    deduped: Dict[str, Dict[str, Any]] = {}
    for refund in refunds:
        deduped.setdefault(refund_fingerprint(refund), refund)

    logger.info(
        "Amazon export at %s: %d items, %d refunds (%d before cross-file dedup)",
        root, len(items), len(deduped), len(refunds),
    )
    return {"items": items, "refunds": list(deduped.values()), "files": files}


def parse_all_owners(root: str) -> Dict[str, Dict[str, Any]]:
    """Parse every owner export under ``root``, tagging rows with their owner."""
    result: Dict[str, Dict[str, Any]] = {}
    for owner, path in discover_owner_exports(root).items():
        parsed = parse_export(path)
        for row in parsed["items"]:
            row["owner"] = owner
        for row in parsed["refunds"]:
            row["owner"] = owner
        result[owner] = parsed
    return result
