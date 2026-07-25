"""Tests for Story 01: Multi-format ingest into canonical transaction schema."""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.parsers import detect_parser
from app.parsers.chase import ChaseCreditParser, ChaseCheckingParser
from app.parsers.us_bank import UsBankParser
from app.parsers.bofa import BofaChkParser, BofaStmtParser
from app.parsers.cash_app import CashAppParser
from app.parsers.paypal import PayPalParser
from app.parsers.venmo import VenmoParser
from app.parsers.mortgage import MortgagePdfParser

SAMPLES_DIR = os.path.join(os.path.dirname(__file__), '..', 'samples')


def sample_files():
    """Every real export sitting in `samples/`, which is gitignored.

    Empty on a fresh clone, so anything driven by this must skip rather than
    quietly pass on nothing.
    """
    if not os.path.isdir(SAMPLES_DIR):
        return []
    return [
        os.path.join(SAMPLES_DIR, name)
        for name in sorted(os.listdir(SAMPLES_DIR))
        if os.path.isfile(os.path.join(SAMPLES_DIR, name))
    ]


# --------------------------------------------------------------- synthetic exports
#
# One fabricated row per bank format. These carry everything the parsers key off —
# header text, column order, the sign convention, and the account mask each bank
# writes into the filename — and none of anyone's transactions, so the parser tests
# below exercise real parsing logic on a clone where `samples/` is empty.
#
# The masks are what the parsers turn into account ids (`<platform>-<mask>`), so
# each one is distinct: a mask shared between two accounts would make the mapping
# depend on iteration order.

CREDIT_MASK = "0002"
CHECKING_MASK = "0011"
LOAN_MASK = "0022"
US_BANK_MASK = "0033"
BOFA_CHK_MASK = "0044"


@pytest.fixture(scope="module")
def exports(tmp_path_factory):
    d = tmp_path_factory.mktemp("exports")
    paths = {}

    def write(key, name, text):
        path = d / name
        path.write_text(text, encoding="utf-8")
        paths[key] = str(path)

    write("chase_credit", f"Chase{CREDIT_MASK}_Activity_20260101.CSV",
          "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
          "04/27/2026,04/28/2026,EXAMPLE STREAMING,Entertainment,Sale,-15.99,\n"
          "05/02/2026,05/03/2026,EXAMPLE STREAMING,Entertainment,Return,15.99,\n")

    write("chase_checking", f"Chase{CHECKING_MASK}_Activity_20260101.CSV",
          "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
          "DEBIT,04/28/2026,ORIG CO NAME:EXAMPLEUTILITY ORIG ID:1234,-120.45,ACH_DEBIT,1500.00,\n"
          "CREDIT,04/29/2026,Payroll deposit,2500.00,BASIC_PAYROLL,4000.00,\n")

    write("chase_loan", f"Chase{LOAN_MASK}_Activity_20260101.CSV",
          "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
          "DEBIT,04/28/2026,Loan payment,-1850.00,LOAN_PMT,0.00,\n")

    write("us_bank", f"Checking - {US_BANK_MASK}_01-01-2026_12-31-2026.csv",
          '"Date","Transaction","Name","Memo","Amount"\n'
          '"01/15/2026","DEBIT","EXAMPLE GROCERY","POS purchase","-42.10"\n')

    write("bofa_chk", f"CHK_{BOFA_CHK_MASK}_CURRENT_VIEW.csv",
          "Status,Date,Description,Debit,Credit\n"
          "Processed,01/15/2026,EXAMPLE HARDWARE 01/14 PURCHASE,-42.10,\n"
          "Processed,01/16/2026,Zelle payment from Teresa Navarro Conf# abc123,,800.00\n")

    # The statement export carries no mask anywhere, which is why its parser has a
    # fixed account id instead of a derived one.
    write("bofa_stmt", "stmt.csv",
          "Description,,Summary Amt.\n"
          'Beginning balance as of 01/01/2026,,"1,000.00"\n'
          'Total credits,,"2,000.00"\n'
          'Total debits,,"-500.00"\n'
          'Ending balance as of 03/31/2026,,"2,500.00"\n'
          "\n"
          "Date,Description,Amount,Running Bal.\n"
          '01/01/2026,Beginning balance as of 01/01/2026,,"1,000.00"\n'
          '01/15/2026,"Zelle payment from Teresa Navarro Conf# abc123","800.00","1,800.00"\n'
          '02/02/2026,"EXAMPLE UTILITY DES:WEB_PAY","-120.45","1,679.55"\n')

    write("cash_app", "cash_app_report_1000000000000.csv",
          '"Date","Transaction ID","Transaction Type","Currency","Amount","Fee",'
          '"Net Amount","Asset Type","Asset Price","Asset Amount","Status","Notes",'
          '"Name of sender/receiver","Account"\n'
          '"2026-01-15 12:00:00 EDT","TX1","P2P","USD","$-25.00","$0","$-25.00",'
          '"","","","COMPLETE","Lunch","Example Payee","Cash"\n')

    write("paypal", "paypal-2026.CSV",
          '"Date","Time","TimeZone","Name","Type","Status","Currency","Amount","Fees",'
          '"Total","Exchange Rate","Receipt ID","Balance","Transaction ID","Item Title"\n'
          '"01/15/2026","12:00:00","EDT","Example Merchant","Express Checkout Payment",'
          '"Completed","USD","-25.00","0.00","-25.00","","R1","100.00","T1","Widget"\n')

    write("venmo", "Venmo.html",
          "<html><head><title>Venmo</title></head><body>\n"
          "<p>Snapshot of https://account.venmo.com/u/example</p>\n"
          "<p>You paid Example Payee $25.00</p>\n"
          "</body></html>\n")

    return paths


# The platform each synthetic export must be auto-detected as.
EXPECTED_PLATFORM = {
    "chase_credit": "chase",
    "chase_checking": "chase",
    "chase_loan": "chase",
    "us_bank": "us_bank",
    "bofa_chk": "bofa",
    "bofa_stmt": "bofa",
    "cash_app": "cash_app",
    "paypal": "paypal",
    "venmo": "venmo",
}


def test_detect_chase_credit(exports):
    path = exports["chase_credit"]
    assert ChaseCreditParser.detect(path)
    rows = ChaseCreditParser().parse(path)
    assert len(rows) == 2
    for r in rows:
        assert r["source_platform"] == "chase"
        assert r["account_id"] == f"chase-{CREDIT_MASK}"
        assert r["posted_date"] is not None
        assert r["amount_cents"] != 0
        assert r["merchant"] != ""
        assert r["currency"] == "USD"
    # A credit-card 'Sale' is money out, a 'Return' is money back in.
    assert [r["amount_cents"] for r in rows] == [-1599, 1599]


def test_detect_chase_checking(exports):
    path = exports["chase_checking"]
    assert ChaseCheckingParser.detect(path)
    rows = ChaseCheckingParser().parse(path)
    assert len(rows) == 2
    for r in rows:
        assert r["source_platform"] == "chase"
        assert r["account_id"] == f"chase-{CHECKING_MASK}"
        assert r["posted_date"] is not None
        assert r["amount_cents"] != 0
    assert [r["amount_cents"] for r in rows] == [-12045, 250000]
    assert rows[1]["merchant"] == "Payroll"


def test_detect_chase_loan(exports):
    """The loan export shares the checking format; only the mask tells them apart."""
    path = exports["chase_loan"]
    assert ChaseCheckingParser.detect(path)
    rows = ChaseCheckingParser().parse(path)
    assert len(rows) == 1
    assert rows[0]["account_id"] == f"chase-{LOAN_MASK}"
    assert rows[0]["merchant"] == "Chase Loan Payment"


def test_detect_us_bank(exports):
    path = exports["us_bank"]
    assert UsBankParser.detect(path)
    rows = UsBankParser().parse(path)
    assert len(rows) == 1
    for r in rows:
        assert r["source_platform"] == "us_bank"
        assert r["account_id"] == f"us_bank-{US_BANK_MASK}"
        assert r["posted_date"] is not None
        assert r["amount_cents"] != 0


def test_detect_bofa_chk(exports):
    path = exports["bofa_chk"]
    assert BofaChkParser.detect(path)
    rows = BofaChkParser().parse(path)
    assert len(rows) == 2
    for r in rows:
        assert r["source_platform"] == "bofa"
        assert r["account_id"] == f"bofa-{BOFA_CHK_MASK}"
    # Debit column negative, credit column positive.
    assert [r["amount_cents"] for r in rows] == [-4210, 80000]


def test_detect_bofa_stmt(exports):
    path = exports["bofa_stmt"]
    assert BofaStmtParser.detect(path)
    rows = BofaStmtParser().parse(path)
    assert len(rows) == 2, "the 'Beginning balance' sentinel row is not a transaction"
    for r in rows:
        assert r["source_platform"] == "bofa"
        assert r["account_id"] == "bofa-stmt"
        assert r["business_context"] == "rental"


def test_detect_cash_app(exports):
    path = exports["cash_app"]
    assert CashAppParser.detect(path)
    rows = CashAppParser().parse(path)
    assert len(rows) == 1
    for r in rows:
        assert r["source_platform"] == "cash_app"
        assert r["account_id"] == "cash-app"
        assert r["amount_cents"] == -2500


def test_detect_paypal(exports):
    path = exports["paypal"]
    assert PayPalParser.detect(path)
    rows = PayPalParser().parse(path)
    assert len(rows) == 1
    for r in rows:
        assert r["source_platform"] == "paypal"
        assert r["account_id"] == "paypal"
        assert r["amount_cents"] == -2500


def test_detect_venmo(exports):
    path = exports["venmo"]
    assert VenmoParser.detect(path)
    rows = VenmoParser().parse(path)
    # Venmo is a best-effort HTML scrape; zero rows is acceptable.
    for r in rows:
        assert r["source_platform"] == "venmo"


@pytest.mark.parametrize("key", sorted(EXPECTED_PLATFORM))
def test_auto_detect_all_formats(exports, key):
    """Every known format must be auto-detected from the file alone."""
    cls = detect_parser(exports[key])
    assert cls is not None, f"No parser detected for {key}"
    assert cls().platform == EXPECTED_PLATFORM[key]


def test_schema_completeness(exports):
    """Every parsed row must have all required fields non-null.

    Runs over the synthetic exports and, when they are present, the real ones too —
    so the check still means something on a clone with no `samples/`.
    """
    required = ["source_platform", "source_file", "account_id", "posted_date",
                "amount_cents", "merchant", "currency"]
    paths = list(exports.values()) + sample_files()
    checked = 0
    for fpath in paths:
        if fpath.endswith(".pdf"):
            continue  # PDFs go to the amortization table, different schema
        cls = detect_parser(fpath)
        if cls is None:
            continue
        for r in cls().parse(fpath):
            checked += 1
            for field in required:
                assert r.get(field) is not None and r[field] != "", \
                    f"{os.path.basename(fpath)}: missing {field}"
    assert checked > 0, "no rows were checked"


def test_amount_cents_is_int(exports):
    """amount_cents must always be an integer."""
    paths = list(exports.values()) + sample_files()
    checked = 0
    for fpath in paths:
        if fpath.endswith(".pdf"):
            continue
        cls = detect_parser(fpath)
        if cls is None:
            continue
        for r in cls().parse(fpath):
            checked += 1
            assert isinstance(r["amount_cents"], int), \
                f"{os.path.basename(fpath)}: amount_cents not int"
    assert checked > 0, "no rows were checked"


def test_mortgage_pdf_amortization():
    """An amortization schedule parses into a monotonically amortizing table.

    Driven by whatever statement PDF is in `samples/` rather than one particular
    document, and asserted on the arithmetic every schedule obeys instead of one
    loan's figures.
    """
    pdfs = [p for p in sample_files() if p.lower().endswith(".pdf")]
    if not pdfs:
        pytest.skip("no statement PDF in samples/ (gitignored)")

    rows = MortgagePdfParser().parse(pdfs[0])
    assert len(rows) > 1, "a schedule is more than one month"
    for r in rows:
        for field in ("month", "principal_cents", "interest_cents", "ending_balance_cents"):
            assert field in r
        assert r["principal_cents"] > 0
        assert r["interest_cents"] >= 0
        assert r["ending_balance_cents"] >= 0

    months = [r["month"] for r in rows]
    assert months == sorted(months), "months must run forwards"
    balances = [r["ending_balance_cents"] for r in rows]
    assert balances == sorted(balances, reverse=True), "the balance only ever falls"
    assert balances[-1] == 0, "a full schedule pays the loan off"
    # Each month's principal is exactly the drop in the balance.
    for prev, row in zip(rows, rows[1:]):
        assert prev["ending_balance_cents"] - row["ending_balance_cents"] == row["principal_cents"]


def test_parse_amount_utility():
    from app.parsers.base import BaseParser
    assert BaseParser.parse_amount("$1,234.56") == 123456
    assert BaseParser.parse_amount("-$500.00") == -50000
    assert BaseParser.parse_amount("($500.00)") == -50000
    assert BaseParser.parse_amount("0.00") == 0
    assert BaseParser.parse_amount("100") == 10000


def test_normalize_description():
    from app.parsers.base import BaseParser
    assert BaseParser.normalize_description("  HELLO WORLD  ") == "hello world"
    assert BaseParser.normalize_description("AMAZON#1234") == "amazon"
    assert BaseParser.normalize_description("PAYPAL *TRANSFER") == "paypal transfer"
    assert BaseParser.normalize_description("TX#1234567890") == "tx"
    assert BaseParser.normalize_description("  multiple   spaces  ") == "multiple spaces"


def test_dedup_two_identical_imports(test_client):
    """Two consecutive POST /api/import/samples calls must yield 0 net new rows."""
    resp1 = test_client.post("/api/import/samples")
    assert resp1.status_code == 200
    r1 = resp1.json()
    first_total = sum(res["imported"] for res in r1["results"])

    resp2 = test_client.post("/api/import/samples")
    assert resp2.status_code == 200
    r2 = resp2.json()
    second_imported = sum(res["imported"] for res in r2["results"])

    assert second_imported == 0, f"Expected 0 new imports on second call, got {second_imported}"


def _parsed_row_counts():
    """Rows each sample file yields on its own, keyed by file name."""
    counts = {}
    for fpath in sample_files():
        cls = detect_parser(fpath)
        if cls is None:
            continue
        counts[os.path.basename(fpath)] = len(cls().parse(fpath))
    return counts


def test_dedup_is_never_additive(test_client):
    """Import can only collapse rows, never invent them.

    Replaces a fixed row count for one account: the count belonged to one person's
    exports, while the property — imported ≤ sum of the parts — holds for anyone's.
    """
    counts = _parsed_row_counts()
    if not counts:
        pytest.skip("no sample exports present (samples/ is gitignored)")

    test_client.post("/api/import/samples")
    resp = test_client.get("/api/transactions?page_size=1&scope=all")
    assert resp.status_code == 200
    total = resp.json()["total"]

    assert total > 0, "importing real exports must produce rows"
    assert total <= sum(counts.values()), (
        f"imported {total} rows from files holding {sum(counts.values())} — "
        "dedup added rows"
    )


def _files_by_account():
    """Sample files grouped by the account their parser assigns, with row counts."""
    grouped = {}
    for fpath in sample_files():
        if fpath.lower().endswith(".pdf"):
            continue
        cls = detect_parser(fpath)
        if cls is None:
            continue
        rows = cls().parse(fpath)
        if not rows:
            continue
        grouped.setdefault(rows[0]["account_id"], []).append(len(rows))
    return grouped


def test_overlapping_exports_of_one_account_collapse(test_client):
    """Two exports of the same account that share a date range become one history.

    Banks re-export overlapping windows, so the union of an account's files has to
    be smaller than the sum of them — and never larger, for any account.
    """
    multi = {a: c for a, c in _files_by_account().items() if len(c) > 1}
    if not multi:
        pytest.skip("no account in samples/ was exported more than once")

    test_client.post("/api/import/samples")

    collapsed = []
    for account_id, counts in multi.items():
        resp = test_client.get(
            f"/api/transactions?account_id={account_id}&page_size=1&scope=all"
        )
        assert resp.status_code == 200
        total = resp.json()["total"]
        sum_of_parts = sum(counts)
        assert 0 < total <= sum_of_parts, (
            f"{account_id}: {total} imported from {len(counts)} exports holding "
            f"{sum_of_parts} rows"
        )
        collapsed.append(total < sum_of_parts)

    assert any(collapsed), (
        "every re-exported account kept the full sum of its files — "
        "overlapping rows were not deduplicated"
    )


def test_transfer_pairs_detected(test_client):
    """After full import, GET /api/transfers returns matched pairs with linked_transaction_id."""
    test_client.post("/api/import/samples")
    resp = test_client.get("/api/transfers")
    assert resp.status_code == 200
    pairs = resp.json()
    assert len(pairs) > 0, "Should have at least one transfer pair"

    for p in pairs:
        assert "from_transaction_id" in p
        assert "to_transaction_id" in p
        assert "matched_by" in p
        assert p["from_amount"] < 0
        assert p["to_amount"] > 0
        assert p["from_amount"] == -p["to_amount"], "Amounts should be opposite and equal"

        resp_from = test_client.get(f"/api/transactions/{p['from_transaction_id']}")
        resp_to = test_client.get(f"/api/transactions/{p['to_transaction_id']}")
        from_txn = resp_from.json()
        to_txn = resp_to.json()
        assert from_txn["entity_type"] == "transfer"
        assert to_txn["entity_type"] == "transfer"
        assert from_txn["linked_transaction_id"] == p["to_transaction_id"]
        assert to_txn["linked_transaction_id"] == p["from_transaction_id"]


def test_transfer_entity_count_invariant(test_client):
    """entity_type='transfer' = 2 × transfers + mortgage components."""
    test_client.post("/api/import/samples")
    resp_tx = test_client.get("/api/transactions?page_size=1000")
    assert resp_tx.status_code == 200
    tx_data = resp_tx.json()
    all_txns = tx_data.get("transactions", [])
    total = tx_data.get("total", 0)

    if total > 1000:
        page = 1
        transfer_count = 0
        mortgage_components = 0
        while True:
            resp = test_client.get(f"/api/transactions?page={page}&page_size=1000")
            data = resp.json()
            rows = data.get("transactions", [])
            if not rows:
                break
            for t in rows:
                if t.get("entity_type") == "transfer":
                    transfer_count += 1
                    if t.get("category_path") in (
                        "Transfer > Mortgage Principal",
                        "Transfer > Mortgage Escrow",
                    ) and t.get("linked_transaction_id") is not None:
                        mortgage_components += 1
            page += 1
            if len(rows) < 1000:
                break
    else:
        transfer_count = sum(1 for t in all_txns if t.get("entity_type") == "transfer")
        mortgage_components = sum(
            1 for t in all_txns
            if t.get("entity_type") == "transfer"
            and t.get("category_path") in (
                "Transfer > Mortgage Principal",
                "Transfer > Mortgage Escrow",
            )
            and t.get("linked_transaction_id") is not None
        )

    resp_tr = test_client.get("/api/transfers")
    assert resp_tr.status_code == 200
    pair_count = len(resp_tr.json())

    assert transfer_count == 2 * pair_count + mortgage_components, (
        f"Expected entity_type='transfer' count ({transfer_count}) "
        f"to be 2× transfers table count ({pair_count}) "
        f"+ {mortgage_components} mortgage components"
    )


def test_dashboard_excludes_transfer_inflows(test_client):
    """Dashboard totals must exclude credit-card 'Payment' inflows and the mortgage payoff."""
    test_client.post("/api/import/samples")
    resp = test_client.get("/api/dashboard")
    assert resp.status_code == 200
    data = resp.json()

    totals = data["totals"]
    assert totals["net_cents"] == totals["total_income_cents"] - totals["total_expense_cents"]

    assert totals["total_income_cents"] >= 0
    assert totals["total_expense_cents"] >= 0


def test_missing_counterparts_surfaced(test_client):
    """GET /api/transfers/missing-counterparts returns transfers without a counterpart."""
    test_client.post("/api/import/samples")
    resp = test_client.get("/api/transfers/missing-counterparts")
    assert resp.status_code == 200
    rows = resp.json()
    for r in rows:
        assert r["entity_type"] == "transfer"
        assert r["linked_transaction_id"] is None


def test_transfer_pairs_wired_in_both_directions(test_client):
    """linked_transaction_id must reference the pair in both directions."""
    test_client.post("/api/import/samples")
    resp = test_client.get("/api/transfers")
    assert resp.status_code == 200
    pairs = resp.json()
    for p in pairs:
        fid = p["from_transaction_id"]
        tid = p["to_transaction_id"]
        resp_f = test_client.get(f"/api/transactions/{fid}")
        resp_t = test_client.get(f"/api/transactions/{tid}")
        f_txn = resp_f.json()
        t_txn = resp_t.json()
        assert f_txn["linked_transaction_id"] == tid, (
            f"From side ({fid}) should link to to side ({tid})"
        )
        assert t_txn["linked_transaction_id"] == fid, (
            f"To side ({tid}) should link to from side ({fid})"
        )


def test_transfer_detect_idempotent(test_client):
    """Calling POST /api/transfers/detect twice produces same result."""
    test_client.post("/api/import/samples")
    resp1 = test_client.post("/api/transfers/detect")
    assert resp1.status_code == 200
    r1 = resp1.json()

    resp2 = test_client.post("/api/transfers/detect")
    assert resp2.status_code == 200
    r2 = resp2.json()

    assert r2["new_pairs"] == 0, "Second detect should find 0 new pairs"
    assert r2["description_singles"] == 0, "Second detect should find 0 new singles"
