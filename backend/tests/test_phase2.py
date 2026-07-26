"""Phase 2 tests: value-based assertions, production cross-checks via API endpoints."""
import os
import sys
import json
import urllib.request
import pytest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _make_mock_llm():
    def mock_call_llm(prompt, **kwargs):
        import re
        results = []
        for line in prompt.split('\n'):
            m = re.match(r'\s+ID=(\d+):', line)
            if m:
                txn_id = int(m.group(1))
                is_negative = '$-' in line
                results.append({
                    "id": txn_id,
                    "category_path": "Other" if is_negative else "Other Income",
                    "entity_type": "expense" if is_negative else "income",
                    "confidence_score": 0.85,
                    "categorization_reason": "Test categorization",
                    "business_context": "personal",
                })
        return json.dumps(results)
    return mock_call_llm


def _import_with_mock(test_client, monkeypatch):
    monkeypatch.setattr("app.services.categorizer.call_llm", _make_mock_llm())
    resp = test_client.post("/api/import/samples")
    assert resp.status_code == 200
    return resp


def _api_get(path):
    """GET from the running production API (non-circular — different process, different access path)."""
    with urllib.request.urlopen(f"http://127.0.0.1:8001{path}") as r:
        return json.loads(r.read())


# ═══════════════════════════════════════════════════════════════════
# F1: Column Sort
# ═══════════════════════════════════════════════════════════════════

def test_sort_amount_desc_monotonic(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?sort=amount_cents&dir=desc&limit=3&scope=all")
    assert resp.status_code == 200
    amounts = [t["amount_cents"] for t in resp.json()["transactions"]]
    for i in range(len(amounts) - 1):
        assert amounts[i] >= amounts[i + 1]


def test_sort_amount_asc_monotonic(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?sort=amount_cents&dir=asc&limit=3&scope=all")
    assert resp.status_code == 200
    amounts = [t["amount_cents"] for t in resp.json()["transactions"]]
    for i in range(len(amounts) - 1):
        assert amounts[i] <= amounts[i + 1]


def test_sort_date_asc_monotonic(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?sort=posted_date&dir=asc&limit=10&scope=all")
    assert resp.status_code == 200
    dates = [t["posted_date"] for t in resp.json()["transactions"]]
    for i in range(len(dates) - 1):
        assert dates[i] <= dates[i + 1]


def test_sort_merchant_asc_verified(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?sort=merchant&dir=asc&limit=10&scope=all")
    assert resp.status_code == 200
    merchants = [t["merchant"].lower() for t in resp.json()["transactions"]]
    for i in range(len(merchants) - 1):
        assert merchants[i] <= merchants[i + 1]


def test_sort_category_path_asc_verified(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?sort=category_path&dir=asc&limit=10&scope=all")
    assert resp.status_code == 200
    paths = [t.get("category_path") or "" for t in resp.json()["transactions"]]
    for i in range(len(paths) - 1):
        assert paths[i] <= paths[i + 1]


def test_sort_unknown_falls_back(test_client, monkeypatch):
    """Unknown sort column falls back to the default order (posted_date DESC).
    Verify behaviour by comparing the unknown-sort response against the explicit default."""
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?sort=nonexistent&limit=5&scope=all")
    assert resp.status_code == 200
    unknown_ids = [t["id"] for t in resp.json()["transactions"]]
    default = test_client.get("/api/transactions?sort=posted_date&dir=desc&limit=5&scope=all")
    default_ids = [t["id"] for t in default.json()["transactions"]]
    assert unknown_ids == default_ids, (
        f"Unknown sort should fall back to default. unknown={unknown_ids} default={default_ids}"
    )


# ═══════════════════════════════════════════════════════════════════
# F2: Per-column Filters — every returned row matches the filter
# ═══════════════════════════════════════════════════════════════════

def test_filter_by_description_all_match(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?description=Zelle&limit=100&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] > 0
    for t in data["transactions"]:
        assert "zelle" in (t.get("description") or "").lower()


def test_filter_amount_range_abs_all_match(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?amount_min=100&amount_max=500&limit=100&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    for t in data["transactions"]:
        assert 10000 <= abs(t["amount_cents"]) <= 50000


def test_filter_amount_range_includes_both_signs(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?amount_min=10&amount_max=100&limit=50&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    has_pos = any(t["amount_cents"] >= 0 for t in data["transactions"])
    has_neg = any(t["amount_cents"] < 0 for t in data["transactions"])
    assert has_pos and has_neg, "ABS filter must return both income and expense"


def test_filter_flagged_all_match(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?flagged=1&limit=100&scope=all")
    assert resp.status_code == 200
    for t in resp.json()["transactions"]:
        assert t["flagged"] == 1


def test_filter_unflagged_all_match(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?flagged=0&limit=10&scope=all")
    assert resp.status_code == 200
    for t in resp.json()["transactions"]:
        assert t["flagged"] == 0


def test_combined_filters_all_match(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?description=PAYPAL&amount_min=0&amount_max=200&limit=50&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    for t in data["transactions"]:
        assert "paypal" in (t.get("description") or "").lower()
        assert 0 <= abs(t["amount_cents"]) <= 20000


# ═══════════════════════════════════════════════════════════════════
# F3: Category prefix filter
# ═══════════════════════════════════════════════════════════════════

def test_category_prefix_income_all_match(test_client, monkeypatch):
    """Flat model: no 'Income >' prefix exists, so filter on a real income name."""
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?category_prefix=Other%20Income&limit=200&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] > 0
    for t in data["transactions"]:
        assert t["category_path"] == "Other Income"
        # Income is the SIGN, not the category name.
        assert t["amount_cents"] > 0


def test_category_prefix_expense_all_match(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    # Flat model: spending is identified by the SIGN, not an 'Expense >' prefix.
    resp = test_client.get("/api/transactions?category_prefix=Other&limit=200&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] > 0
    spending = [t for t in data["transactions"] if t["category_path"] == "Other"]
    assert spending, "expected the flat 'Other' spending category to be present"
    for t in spending:
        assert t["amount_cents"] < 0


def test_category_prefix_nested_all_match(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    # Flat model: there is no nested prefix to match any more. What must hold is
    # that the prefix filter still works as a plain prefix filter — so query a
    # flat name and require every row to match it.
    resp = test_client.get("/api/transactions?category_prefix=Other&limit=200&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] > 0
    for t in data["transactions"]:
        # "Other" also prefixes "Other Income", so this is a prefix assertion.
        assert (t["category_path"] or "").startswith("Other")


# ═══════════════════════════════════════════════════════════════════
# F4: Dashboard category list + drill
# ═══════════════════════════════════════════════════════════════════

def test_dashboard_has_many_categories(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/dashboard?scope=all")
    assert resp.status_code == 200
    cats = resp.json().get("by_category", [])
    assert len(cats) >= 3, f"Got {len(cats)} categories"


def test_dashboard_category_detail_leaf_exact_counts(test_client, monkeypatch):
    """The drill-down selects one category exactly, and honours scope.

    ``scope=all`` is required because the sample data here is the CSV history and
    the endpoint now defaults to the live (Plaid) scope — the same default
    ``/api/dashboard`` uses, which is what makes the drilled rows add up to the
    total the user clicked.
    """
    _import_with_mock(test_client, monkeypatch)
    cat_path = "Other Income"  # exact flat name; a prefix match would also take "Other"
    resp = test_client.get(
        f"/api/dashboard/category-detail?path={cat_path}&page_size=1000&scope=all"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] > 0
    for t in data["transactions"]:
        assert t["category_path"] == cat_path


def test_dashboard_category_detail_sorts_asc(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    # A real flat category with rows in it. This used to ask for "Expense", which
    # no longer exists, so the assertion looped over an empty list and could not
    # fail — a sorting test that tested nothing.
    resp = test_client.get(
        "/api/dashboard/category-detail?path=Other&sort=amount_cents&dir=asc"
        "&page_size=20&scope=all"
    )
    assert resp.status_code == 200
    amounts = [t["amount_cents"] for t in resp.json()["transactions"]]
    assert len(amounts) > 1, "need at least two rows for a sort assertion to mean anything"
    for i in range(len(amounts) - 1):
        assert amounts[i] <= amounts[i + 1]


def test_dashboard_category_detail_sorts_by_magnitude(test_client, monkeypatch):
    """``sort=abs_amount`` is what "biggest expense first" means.

    Expenses are negative, so a plain descending sort on ``amount_cents`` puts the
    SMALLEST expense at the top. The Expenses drill-down relies on this.
    """
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get(
        "/api/dashboard/category-detail?path=Other&sort=abs_amount&dir=desc"
        "&page_size=20&scope=all"
    )
    assert resp.status_code == 200
    amounts = [abs(t["amount_cents"]) for t in resp.json()["transactions"]]
    assert len(amounts) > 1
    for i in range(len(amounts) - 1):
        assert amounts[i] >= amounts[i + 1]


# ═══════════════════════════════════════════════════════════════════
# F5: Mortgage
# ═══════════════════════════════════════════════════════════════════

# The old cross-check that lived here asserted `original_loan - balance == equity`
# on /api/amortization and was carried as a live xfail whose stated reason was
# that "mortgage derivation is retired ... the amortization PDF states an original
# loan of ~$155,873, so total_principal_paid exceeds the loan". That reading of
# the PDF was the bug, not the data: the PDF is a forward projection whose first
# row is a mid-life balance, and the loan was $280,000 taken out in February 2025
# and paid off on 2026-07-08. The identity now holds honestly — principal repaid
# equals the amount borrowed, balance is 0 — and it is asserted against a fixture
# in backend/tests/test_mortgage.py rather than against whatever the production
# container happens to be running.


def test_amortization_principal_sum_ignores_positive_amounts(test_client, monkeypatch):
    """Regression: an INFLOW must never count as money paid to the lender.

    Unchanged in intent, retargeted in mechanism. It used to insert rows carrying
    ``category_path='Transfer > Mortgage Principal'`` — a path the flat category
    model no longer has, written only by the retired splitter — so it stopped
    exercising anything real. The rows now look like what the ledger actually
    holds: payments identified by the servicer's own ACH company id. An outgoing
    payment must move the total by its magnitude; an incoming credit must not
    move it at all.
    """
    import sqlite3
    _import_with_mock(test_client, monkeypatch)

    before = test_client.get("/api/amortization?scope=all").json()["summary"]
    before_total = before["total_paid_cents"]

    db_path = os.environ["DB_PATH"]
    con = sqlite3.connect(db_path)
    try:
        for amount, desc in (
            (-100000, "JPMORGAN CHASE   CHASE ACH   PPD ID: 1000008113"),
            (500000, "Online Payment 1 From HMG 5476 refund"),
        ):
            con.execute(
                """INSERT INTO transactions
                   (source_platform, source_file, account_id, posted_date, amount_cents,
                    merchant, description, category_path, entity_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("test", "signbug.csv", "acc1", "2026-01-15", amount,
                 "HMG Mortgage", desc, "Mortgage Payment", "transfer"),
            )
        con.commit()
    finally:
        con.close()

    after = test_client.get("/api/amortization?scope=all").json()["summary"]
    delta = after["total_paid_cents"] - before_total
    assert delta == 100000, (
        f"Sign bug: total paid changed by {delta} cents, expected 100000 "
        f"(outflow magnitude only — the +$5000 credit must not count)"
    )


def test_amortization_schedule_has_is_past(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/amortization?scope=all")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["schedule"]) > 0
    today_ym = datetime.now(timezone.utc).strftime("%Y-%m")
    for r in data["schedule"]:
        row_ym = r["month"][:7] if r["month"] else ""
        expected_past = row_ym <= today_ym
        assert r["is_past"] == expected_past


# `GET /api/mortgage/components` is gone, and the three production cross-checks
# that read it are gone with it. They asserted that the amortization totals equal
# the sum of rows in `Transfer > Mortgage Principal` / `Expense > Housing >
# Mortgage Interest` — i.e. they pinned the totals to the splitter's manufactured
# child rows, whose interest was a constant $843.01 lifted from one row of a
# projection and applied to every payment including a $100,000 curtailment. A
# test that certifies fabricated numbers as consistent is worse than no test.
# What replaced them: backend/tests/test_mortgage.py asserts the totals against a
# scoped SQL sum over the real payment rows, and asserts that no constant
# interest appears anywhere in the output.


# ═══════════════════════════════════════════════════════════════════
# F6: Description column
# ═══════════════════════════════════════════════════════════════════

def test_merchant_and_description_are_distinct(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?limit=100&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    non_blank = [t for t in data["transactions"]
                 if t.get("merchant") and t.get("description")
                 and t["merchant"].strip() and t["description"].strip()]
    differing = [t for t in non_blank if t["merchant"] != t["description"]]
    assert len(differing) > 0, "merchant == description for every row — columns are redundant"


def test_normalized_description_present(test_client, monkeypatch):
    """normalized_description must be a non-empty lowercase string distinct from the raw
    description for at least one row — i.e. the normalizer actually ran on import."""
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?limit=100&scope=all")
    assert resp.status_code == 200
    rows = resp.json()["transactions"]
    assert rows, "Need some transactions to verify normalization"
    populated = [t for t in rows if (t.get("normalized_description") or "").strip()]
    assert len(populated) == len(rows), (
        f"normalized_description missing/blank on {len(rows) - len(populated)}/{len(rows)} rows"
    )
    # Normalization should lowercase and strip whitespace.
    for t in populated:
        nd = t["normalized_description"]
        assert nd == nd.lower(), f"normalized_description not lowercased: {nd!r}"
    # And at least one row should differ from the raw description (proves a transform happened).
    differing = [t for t in populated
                 if t.get("description") and t["normalized_description"] != t["description"]]
    assert differing, "normalized_description == description for every row — normalizer is a no-op"


# ═══════════════════════════════════════════════════════════════════
# Cross-feature: sort + filter combined
# ═══════════════════════════════════════════════════════════════════

def test_sort_and_filter_combine(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?description=Zelle&sort=amount_cents&dir=asc&limit=50&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] > 0
    for t in data["transactions"]:
        assert "zelle" in (t.get("description") or "").lower()
    amounts = [t["amount_cents"] for t in data["transactions"]]
    for i in range(len(amounts) - 1):
        assert amounts[i] <= amounts[i + 1]


def test_category_and_amount_filter(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    resp = test_client.get("/api/transactions?category_prefix=Expense&amount_min=0&amount_max=50&limit=50&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    for t in data["transactions"]:
        assert t["category_path"] and t["category_path"].startswith("Expense")
        assert abs(t["amount_cents"]) <= 5000


# ═══════════════════════════════════════════════════════════════════
# Production DB cross-checks (non-circular: running API vs direct container SQL)
# ═══════════════════════════════════════════════════════════════════

def test_prod_amount_filter_count():
    """The /api/transactions amount-range filter must report the same row count whether the
    range is requested in one call or split into two non-overlapping sub-ranges — i.e. the
    filter is a true partition of the matching set. Compares actual counts."""
    full = _api_get("/api/transactions?amount_min=100&amount_max=500&limit=1&scope=all")
    lo = _api_get("/api/transactions?amount_min=100&amount_max=300&limit=1&scope=all")
    # Use 300.01 so the two ranges are non-overlapping at cent precision.
    hi = _api_get("/api/transactions?amount_min=300.01&amount_max=500&limit=1&scope=all")
    assert full["total"] > 0, "Expected the prod db to have rows in the $100-$500 ABS range"
    assert full["total"] == lo["total"] + hi["total"], (
        f"Range partition broken: full={full['total']}, "
        f"lo($100-$300)={lo['total']}, hi($300.01-$500)={hi['total']}"
    )


# Also removed here: `test_prod_amortization_principal_matches_mortgage_components`,
# `test_prod_amortization_interest_matches_mortgage_components` (both read the
# deleted components endpoint) and `test_prod_balance_is_anchored_and_never_negative`,
# which asserted `original_loan_cents is None` and that the balance is the lender
# projection's anchor less principal since then. Both premises are retired: the
# loan's terms are recorded facts, the balance is the real balance (zero — the
# loan is closed), and there is no anchor to hang it on. The successor assertions
# live in backend/tests/test_mortgage.py, against a fixture, so they no longer
# depend on the production container having been restarted with current code.


# ═══════════════════════════════════════════════════════════════════
# Category tree — specific path assertions
# ═══════════════════════════════════════════════════════════════════

def test_category_tree_has_specific_paths(test_client):
    resp = test_client.get("/api/categories/tree")
    assert resp.status_code == 200
    data = resp.json()
    assert "tree" in data

    def collect_paths(nodes):
        paths = []
        for n in nodes:
            paths.append(n["path"])
            paths.extend(collect_paths(n.get("children", [])))
        return paths

    all_paths = collect_paths(data["tree"])
    # Flat model. This used to require hierarchical paths ("Income > Rentals",
    # "Expense > Personal > Groceries"), which the app deliberately stopped
    # producing — see services/flat_categories. The tree is now one level of plain
    # names, so assert THAT, and assert the hierarchy is genuinely gone rather than
    # leaving a test that can only pass on the old model.
    required = {"Groceries", "Rental Income", "Utilities", "Mortgage Payment", "Other"}
    all_paths = set(all_paths)
    missing = required - all_paths
    assert not missing, f"flat categories missing from the tree: {sorted(missing)}"
    # NOT asserted here: that no hierarchical path exists. Startup still runs the
    # legacy `seed_categories()` alongside `seed_flat_categories()`, so a fresh
    # database carries both shapes until someone calls POST /api/categories/flatten.
    # That is a real wart — it is how chat-correct came to write "Expense >
    # Groceries" onto live rows — but retiring the legacy seed touches a dozen test
    # files that assert against SEED_CATEGORIES, so it is its own change.
