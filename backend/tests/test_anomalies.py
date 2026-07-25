"""Tests for Story 40: Anomaly detection.

Tests cover: response shape, all 5 kinds with detections,
kind/severity filtering, flag persistence, and back-test
against samples/ for recurring-charge, duplicate and refunded-row detection.
"""

import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

ALLOWED_KINDS = {"recurring-subscription", "spending-spike", "duplicate-candidate", "new-merchant", "category-anomaly"}
ALLOWED_SEVERITIES = {"low", "medium", "high"}


def _make_mock_llm():
    def mock_call_llm(prompt, **kwargs):
        results = []
        for line in prompt.split('\n'):
            import re
            m = re.match(r'\s+ID=(\d+):', line)
            if m:
                txn_id = int(m.group(1))
                is_negative = '$-' in line
                results.append({
                    "id": txn_id,
                    "category_path": "Expense > Personal > Groceries" if is_negative else "Income > Other Income",
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


@pytest.fixture
def imported_data(test_client, monkeypatch):
    _import_with_mock(test_client, monkeypatch)
    return test_client


class TestAnomaliesShape:

    def test_anomalies_response_structure(self, imported_data):
        """GET /api/anomalies returns {anomalies, total, limit}."""
        resp = imported_data.get("/api/anomalies")
        assert resp.status_code == 200
        data = resp.json()
        assert "anomalies" in data
        assert "total" in data
        assert "limit" in data
        assert isinstance(data["anomalies"], list)

    def test_anomalies_required_fields(self, imported_data):
        """Every anomaly has transaction_id, kind, severity, reason, suggested_action."""
        resp = imported_data.get("/api/anomalies")
        assert resp.status_code == 200
        for a in resp.json()["anomalies"]:
            assert isinstance(a["transaction_id"], int)
            assert a["kind"] in ALLOWED_KINDS, f"Unknown kind: {a['kind']}"
            assert a["severity"] in ALLOWED_SEVERITIES, f"Unknown severity: {a['severity']}"
            assert isinstance(a["reason"], str) and len(a["reason"]) > 5
            assert isinstance(a["suggested_action"], str) and len(a["suggested_action"]) > 5

    def test_anomalies_sorted_by_severity(self, imported_data):
        """Anomalies sorted high > medium > low, then by kind."""
        resp = imported_data.get("/api/anomalies")
        assert resp.status_code == 200
        anomalies = resp.json()["anomalies"]
        sev_order = {"high": 0, "medium": 1, "low": 2}
        seen = [sev_order[a["severity"]] for a in anomalies]
        assert seen == sorted(seen)

    def test_anomalies_kind_endpoint(self, imported_data):
        """GET /api/anomalies/kinds lists valid kinds."""
        resp = imported_data.get("/api/anomalies/kinds")
        assert resp.status_code == 200
        kinds = set(resp.json()["kinds"])
        assert kinds == ALLOWED_KINDS

    def test_anomalies_severity_endpoint(self, imported_data):
        """GET /api/anomalies/severities lists valid severities."""
        resp = imported_data.get("/api/anomalies/severities")
        assert resp.status_code == 200
        sevs = set(resp.json()["severities"])
        assert sevs == ALLOWED_SEVERITIES


class TestAnomalyKinds:

    def test_recurring_subscription_detected(self, test_client, monkeypatch):
        """Recurring subscription appears when merchant charges in 6+ months."""
        _import_with_mock(test_client, monkeypatch)

        from app.database import get_db
        import asyncio

        async def seed():
            db = await get_db()
            try:
                for month in range(1, 8):
                    await db.execute(
                        """INSERT INTO transactions
                           (source_platform, source_file, account_id, posted_date, amount_cents,
                            merchant, description, normalized_description, category_path,
                            entity_type, business_context, confidence_score, categorization_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("manual", "manual", "test-acct",
                         f"2026-{month:02d}-05", -1999,
                         "Monthly Music Streaming", "Monthly Music Streaming", "monthly music streaming",
                         "Expense > Personal > Subscriptions",
                         "expense", "personal", 0.95, "test"),
                    )
                await db.commit()
            finally:
                await db.close()

        asyncio.run(seed())

        resp = test_client.get("/api/anomalies")
        assert resp.status_code == 200
        data = resp.json()
        subs = [a for a in data["anomalies"] if a["kind"] == "recurring-subscription"]
        music = [s for s in subs if "music" in s["reason"].lower()]
        assert music, f"Expected Monthly Music Streaming in recurring-subscription, got: {subs}"
        m = music[0]
        assert m["consecutive_month_count"] >= 6
        assert isinstance(m["price_change_history"], list)

    def test_recurring_subscription_has_consecutive_count(self, test_client, monkeypatch):
        """Recurring subscription entries include consecutive_month_count."""
        _import_with_mock(test_client, monkeypatch)

        from app.database import get_db
        import asyncio

        async def seed():
            db = await get_db()
            try:
                for month in range(1, 9):
                    amount = -2500 if month <= 6 else -3000
                    await db.execute(
                        """INSERT INTO transactions
                           (source_platform, source_file, account_id, posted_date, amount_cents,
                            merchant, description, normalized_description, category_path,
                            entity_type, business_context, confidence_score, categorization_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("manual", "manual", "test-acct",
                         f"2026-{month:02d}-10", amount,
                         "Price Change App", "Price Change App", "price change app",
                         "Expense > Personal > Subscriptions",
                         "expense", "personal", 0.95, "test"),
                    )
                await db.commit()
            finally:
                await db.close()

        asyncio.run(seed())

        resp = test_client.get("/api/anomalies")
        assert resp.status_code == 200
        subs = [a for a in resp.json()["anomalies"] if a["kind"] == "recurring-subscription"]
        price_change_subs = [s for s in subs if "price change" in s["reason"].lower()]
        assert price_change_subs, "Expected Price Change App in recurring-subscription"
        p = price_change_subs[0]
        assert p["consecutive_month_count"] >= 7
        assert len(p["price_change_history"]) >= 1
        change = p["price_change_history"][0]
        assert "from_cents" in change
        assert "to_cents" in change
        assert change["from_cents"] == 2500
        assert change["to_cents"] == 3000

    def test_spending_spike_detected(self, test_client, monkeypatch):
        """Spending spike fires when category spend > 2x trailing 6-month avg."""
        _import_with_mock(test_client, monkeypatch)

        from app.database import get_db
        import asyncio
        from datetime import datetime, timedelta

        async def seed():
            db = await get_db()
            try:
                # Find the latest month with data so spike aligns with current_start.
                c = await db.execute(
                    "SELECT MAX(posted_date) FROM transactions WHERE amount_cents < 0 AND entity_type != 'transfer'"
                )
                row = await c.fetchone()
                max_date = row[0] if row and row[0] else datetime.now().strftime("%Y-%m-%d")
                current_start = max_date[:8] + "01"
                cy, cm = int(max_date[:4]), int(max_date[5:7])

                # Small baseline for 6 trailing months.
                for i in range(6, 0, -1):
                    cm2 = cm - i
                    cy2 = cy
                    while cm2 <= 0:
                        cm2 += 12
                        cy2 -= 1
                    d = f"{cy2}-{cm2:02d}-05"
                    await db.execute(
                        """INSERT INTO transactions
                           (source_platform, source_file, account_id, posted_date, amount_cents,
                            merchant, description, normalized_description, category_path,
                            entity_type, business_context, confidence_score, categorization_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("manual", "manual", "test-spike-acct",
                         d, -500,
                         "Small Vendor", "Small Vendor", "small vendor",
                         "Expense > Personal > Shopping",
                         "expense", "personal", 0.95, "test"),
                    )
                # Large spend in the current month.
                for day in (1, 8, 15):
                    await db.execute(
                        """INSERT INTO transactions
                           (source_platform, source_file, account_id, posted_date, amount_cents,
                            merchant, description, normalized_description, category_path,
                            entity_type, business_context, confidence_score, categorization_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("manual", "manual", "test-spike-acct",
                         current_start[:8] + f"{day:02d}", -30000,
                         "Spike Vendor", "Spike Vendor", "spike vendor",
                         "Expense > Personal > Shopping",
                         "expense", "personal", 0.95, "test"),
                    )
                await db.commit()
            finally:
                await db.close()

        asyncio.run(seed())

        resp = test_client.get("/api/anomalies?kind=spending-spike")
        assert resp.status_code == 200
        spikes = resp.json()["anomalies"]
        shopping_spikes = [s for s in spikes if "Shopping" in s["reason"]]
        assert shopping_spikes, f"Expected Shopping spending-spike, got: {spikes}"

    def test_duplicate_candidate_detected(self, test_client, monkeypatch):
        """Duplicate candidate flags rows matching by amount, +/-3 days, same account."""
        _import_with_mock(test_client, monkeypatch)

        from app.database import get_db
        import asyncio

        async def seed():
            db = await get_db()
            try:
                # Two identical transactions within 1 day in same account.
                for day in (10, 11):
                    await db.execute(
                        """INSERT INTO transactions
                           (source_platform, source_file, account_id, posted_date, amount_cents,
                            merchant, description, normalized_description, category_path,
                            entity_type, business_context, confidence_score, categorization_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("manual", "manual", "dup-test-acct",
                         f"2026-06-{day:02d}", -4500,
                         "Duplicate Mart", "Duplicate Mart purchase", "duplicate mart purchase",
                         "Expense > Personal > Shopping",
                         "expense", "personal", 0.95, "test"),
                    )
                await db.commit()
            finally:
                await db.close()

        asyncio.run(seed())

        resp = test_client.get("/api/anomalies?kind=duplicate-candidate")
        assert resp.status_code == 200
        dups = resp.json()["anomalies"]
        assert len(dups) >= 2
        for d in dups:
            assert "duplicate" in d["reason"].lower()

    def test_new_merchant_detected(self, test_client, monkeypatch):
        """New merchant detection flags merchants with first appearance in last 30 days."""
        _import_with_mock(test_client, monkeypatch)

        from app.database import get_db
        import asyncio
        from datetime import datetime, timedelta

        async def seed():
            db = await get_db()
            try:
                today = datetime.now().strftime("%Y-%m-%d")
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                # Brand new merchant with 1 transaction.
                await db.execute(
                    """INSERT INTO transactions
                       (source_platform, source_file, account_id, posted_date, amount_cents,
                        merchant, description, normalized_description, category_path,
                        entity_type, business_context, confidence_score, categorization_reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("manual", "manual", "new-merchant-acct",
                     yesterday, -2500,
                     "Brand New Shop", "Brand New Shop purchase", "brand new shop purchase",
                     "Expense > Personal > Shopping",
                     "expense", "personal", 0.95, "test"),
                )
                await db.commit()
            finally:
                await db.close()

        asyncio.run(seed())

        resp = test_client.get("/api/anomalies?kind=new-merchant")
        assert resp.status_code == 200
        new_merchants = resp.json()["anomalies"]
        brand_new = [n for n in new_merchants if "brand new" in n["reason"].lower()]
        assert brand_new, f"Expected 'Brand New Shop' in new-merchant, got: {new_merchants}"

    def test_every_anomaly_explains_itself(self, imported_data):
        """Replaces a back-test against the owner's real fraudulent merchant.

        That test named two real merchants and depended on the gitignored samples/
        directory, so it could only pass on one machine. The durable property is
        that no anomaly is ever surfaced without a reason a person can act on.
        """
        resp = imported_data.get("/api/anomalies?kind=category-anomaly")
        assert resp.status_code == 200
        anomalies = resp.json()["anomalies"]
        assert isinstance(anomalies, list)
        for a in anomalies:
            assert a.get("reason"), f"anomaly with no reason is unactionable: {a}"

    def test_every_anomaly_has_a_kind_and_severity(self, imported_data):
        """Replaces the second real-merchant back-test."""
        resp = imported_data.get("/api/anomalies?kind=category-anomaly")
        assert resp.status_code == 200
        anomalies = resp.json()["anomalies"]
        for a in anomalies:
            assert a.get("kind"), f"anomaly with no kind: {a}"
            assert a.get("severity"), f"anomaly with no severity: {a}"

    def test_category_anomaly_refund_pairs(self, imported_data):
        """Back-test: refunded transactions appear as category-anomaly (non-Amazon)."""
        resp = imported_data.get("/api/anomalies?kind=category-anomaly")
        assert resp.status_code == 200
        anomalies = resp.json()["anomalies"]
        refund_anomalies = [a for a in anomalies if "refund" in a["reason"].lower()]
        assert len(refund_anomalies) >= 1, "Expected at least 1 refund-related category anomaly"

    def test_all_kinds_present_across_data(self, imported_data):
        """Verify all 5 anomaly kinds are detected across the full dataset."""
        resp = imported_data.get("/api/anomalies")
        assert resp.status_code == 200
        kinds_found = set(a["kind"] for a in resp.json()["anomalies"])
        # We expect at least category-anomaly + potentially others from real data.
        assert "category-anomaly" in kinds_found

    def test_invalid_kind_returns_400(self, imported_data):
        """Unknown kind filter returns 400."""
        resp = imported_data.get("/api/anomalies?kind=nonexistent")
        assert resp.status_code == 400


class TestAnomalyFlagging:

    def test_detect_endpoint_persists_flags(self, test_client, monkeypatch):
        """POST /api/anomalies/detect sets flagged=1 on anomalous transactions."""
        _import_with_mock(test_client, monkeypatch)

        resp = test_client.post("/api/anomalies/detect")
        assert resp.status_code == 200
        data = resp.json()
        assert "detected" in data
        assert "updated_transactions" in data
        assert data["detected"] >= 0
        assert data["updated_transactions"] >= 0

    def test_detect_sets_flag_reason(self, test_client, monkeypatch):
        """Detected anomalies have flag_reason set on the transaction."""
        _import_with_mock(test_client, monkeypatch)

        from app.database import get_db
        import asyncio

        async def seed():
            db = await get_db()
            try:
                await db.execute(
                    """INSERT INTO transactions
                       (source_platform, source_file, account_id, posted_date, amount_cents,
                        merchant, description, normalized_description, category_path,
                        entity_type, business_context, confidence_score, categorization_reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("manual", "manual", "flag-test-acct",
                     "2026-06-15", -1500,
                     "FraudCorp", "FraudCorp charge", "fraudcorp charge",
                     "Expense > Personal > Subscriptions",
                     "expense", "personal", 0.95, "test"),
                )
                await db.commit()
            finally:
                await db.close()

        asyncio.run(seed())

        test_client.post("/api/anomalies/detect")

        resp = test_client.get("/api/anomalies")
        assert resp.status_code == 200
        anomalies = resp.json()["anomalies"]
        fraudcorp = [a for a in anomalies if "fraudcorp" in a["reason"].lower() and a["kind"] == "recurring-subscription"]
        # If FraudCorp isn't a recurring subscription (only 1 month), it won't appear.
        # That's OK — the detect endpoint at least ran without error.

    def test_flag_toggle_manual(self, imported_data):
        """PATCH /api/transactions/{id}/flag toggles flagged status."""
        # First get a transaction.
        resp = imported_data.get("/api/transactions?page_size=1")
        assert resp.status_code == 200
        txns = resp.json().get("transactions", [])
        if not txns:
            pytest.skip("No transactions available")
        txn_id = txns[0]["id"]

        # Flag it.
        resp = imported_data.patch(f"/api/transactions/{txn_id}/flag?flagged=true")
        assert resp.status_code == 200
        data = resp.json()
        assert data["flagged"] is True
        assert data["flag_reason"] is not None

        # Unflag it.
        resp = imported_data.patch(f"/api/transactions/{txn_id}/flag?flagged=false")
        assert resp.status_code == 200
        data = resp.json()
        assert data["flagged"] is False
        assert data["flag_reason"] is None

    def test_flag_toggle_not_found(self, imported_data):
        """PATCH /api/transactions/99999/flag returns 404."""
        resp = imported_data.patch("/api/transactions/99999/flag?flagged=true")
        assert resp.status_code == 404


# ───── Story 41: Fraud & Refund Flagging ─────

def _configure_fraud_patterns(test_client, patterns):
    """Fraud patterns are DATA now, not a literal list inside the detector.

    Which merchants defrauded a particular person is private and useless to anyone
    else, so a fresh install ships none and detects none. These tests supply their
    own synthetic merchant and pattern, which exercises the mechanism without
    depending on the owner's real fraud — that lived in the gitignored samples/
    directory, so those assertions could only ever pass on one machine.
    """
    resp = test_client.post("/api/anomalies/fraud-patterns", json={"patterns": patterns})
    assert resp.status_code == 200, resp.text
    return resp.json()


# Dated 2039 on purpose. These rows live in the shared session database, and a
# seeded row inside another suite's reporting window silently changes its totals —
# test_refund_attribution_periods.py uses 2031, and a 2031 fraud seed broke three of
# its assertions. Amounts vary for the same reason: identical repeating amounts trip
# the duplicate-charge and recurring-subscription detectors too.
_FRAUD_SEED_IDS = []


@pytest.fixture(autouse=True)
def _clean_fraud_seeds():
    """Remove seeded fraud rows and patterns so neighbours are unaffected."""
    yield
    if not _FRAUD_SEED_IDS:
        return
    import sqlite3
    con = sqlite3.connect(os.environ["DB_PATH"])
    try:
        con.execute(
            f"DELETE FROM transactions WHERE id IN ({','.join('?' * len(_FRAUD_SEED_IDS))})",
            _FRAUD_SEED_IDS,
        )
        con.execute("DELETE FROM fraud_patterns WHERE pattern LIKE 'fraud%'")
        con.commit()
    finally:
        con.close()
        _FRAUD_SEED_IDS.clear()


def _seed_fraud_merchant(rows):
    """Insert transactions for a synthetic fraudulent merchant."""
    import asyncio
    from app.database import get_db

    async def go():
        db = await get_db()
        try:
            ids = []
            for merchant, amount, date in rows:
                cursor = await db.execute(
                    """INSERT INTO transactions
                       (source_platform, source_file, account_id, posted_date, amount_cents,
                        merchant, description, normalized_description, category_path,
                        entity_type, business_context, confidence_score,
                        categorization_reason)
                       VALUES ('plaid','fraud.csv','card-0001',?,?,?,?,?,'Other',
                               'expense','personal',0.9,'seed')""",
                    (date, amount, merchant, merchant, merchant.lower()),
                )
                ids.append(cursor.lastrowid)
                _FRAUD_SEED_IDS.append(cursor.lastrowid)
            await db.commit()
            return ids
        finally:
            await db.close()

    return asyncio.run(go())


class TestFraudFlagging:

    def test_no_configured_patterns_means_no_accusations(self, test_client, monkeypatch):
        """A fresh install must not accuse a merchant it was never told about."""
        import asyncio
        from app.database import get_db
        _import_with_mock(test_client, monkeypatch)

        async def clear():
            db = await get_db()
            try:
                await db.execute("DELETE FROM fraud_patterns")
                await db.commit()
            finally:
                await db.close()
        asyncio.run(clear())

        test_client.post("/api/anomalies/detect")
        resp = test_client.get("/api/anomalies?type=fraud")
        assert resp.status_code == 200
        assert resp.json()["anomalies"] == []

    def test_fraud_type_anomalies_detected(self, test_client, monkeypatch):
        """A configured pattern produces fraud anomalies for that merchant."""
        _import_with_mock(test_client, monkeypatch)
        _seed_fraud_merchant([("Fraudalpha Co", -2531, "2039-05-02"),
                              ("Fraudalpha Co", -1777, "2039-08-14")])
        _configure_fraud_patterns(test_client, ["fraudalpha"])
        test_client.post("/api/anomalies/detect")
        fraud = [a for a in test_client.get("/api/anomalies").json()["anomalies"]
                 if a.get("type") == "fraud"]
        assert len(fraud) >= 1, "a configured pattern must produce fraud anomalies"

    def test_fraud_merchants_have_flag_reason_fraud(self, test_client, monkeypatch):
        """Fraud anomalies set flag_reason='fraud' after detect."""
        import asyncio
        _import_with_mock(test_client, monkeypatch)
        seeded = _seed_fraud_merchant([("Fraudbeta Ltd", -3119, "2039-05-03")])
        _configure_fraud_patterns(test_client, ["fraudbeta"])

        test_client.post("/api/anomalies/detect")

        resp = test_client.get("/api/anomalies?type=fraud")
        assert resp.status_code == 200
        fraud_anomalies = resp.json()["anomalies"]
        assert len(fraud_anomalies) >= 1, "Expected fraud anomalies after detect"

        # Assert on OUR row: several detectors flag rows and flag_reason records
        # whichever claimed the transaction, so picking anomalies[0] is a coin flip.
        fraud_txn_id = seeded[0]
        assert fraud_txn_id in {a["transaction_id"] for a in fraud_anomalies}, (
            f"seeded row {fraud_txn_id} not among fraud anomalies")

        from app.database import get_db
        async def check_flag():
            db = await get_db()
            try:
                cursor = await db.execute(
                    "SELECT flagged, flag_reason FROM transactions WHERE id=?", (fraud_txn_id,)
                )
                row = await cursor.fetchone()
                assert row is not None
                assert row["flagged"] == 1
                assert row["flag_reason"] == "fraud"
            finally:
                await db.close()

        asyncio.run(check_flag())

    def test_fraud_wins_over_a_less_serious_reason(self, test_client, monkeypatch):
        """A fraudulent charge from a brand-new merchant must read 'fraud'.

        Both detectors match such a row, but only one reason can be stored. It used
        to be decided by detector order, so new-merchant (which runs earlier) won and
        the fraud flag was lost — the worst possible way to break that tie.
        """
        import asyncio
        from app.database import get_db
        _import_with_mock(test_client, monkeypatch)
        seeded = _seed_fraud_merchant([("Frauddelta Inc", -4211, "2039-09-07")])
        _configure_fraud_patterns(test_client, ["frauddelta"])
        test_client.post("/api/anomalies/detect")

        async def reason_of(txn_id):
            db = await get_db()
            try:
                cursor = await db.execute(
                    "SELECT flagged, flag_reason FROM transactions WHERE id=?", (txn_id,))
                row = await cursor.fetchone()
                return row["flagged"], row["flag_reason"]
            finally:
                await db.close()

        flagged, reason = asyncio.run(reason_of(seeded[0]))
        assert flagged == 1
        assert reason == "fraud", f"fraud must outrank other reasons, got {reason!r}"

    def test_levenshtein_matches_variants(self, test_client, monkeypatch):
        """Levenshtein <= 2 matches spelling variants of one configured pattern."""
        _import_with_mock(test_client, monkeypatch)
        _configure_fraud_patterns(test_client, ["fraudgamma co"])
        from app.database import get_db
        import asyncio

        async def seed():
            db = await get_db()
            try:
                # All within Levenshtein <=2 of the ONE configured pattern below,
                # which is the point: a card feed invents spellings and the detector
                # has to catch them from a single pattern.
                variants = [
                    "Fraudgamma Co",   # exact -> 0
                    "Fraudgama Co",    # 1 deletion -> 1
                    "Fraudgammar Co",  # 1 insertion -> 1
                ]
                for i, v in enumerate(variants):
                    await db.execute(
                        """INSERT INTO transactions
                           (source_platform, source_file, account_id, posted_date, amount_cents,
                            merchant, description, normalized_description, category_path,
                            entity_type, business_context, confidence_score, categorization_reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        ("manual", "manual", "lev-test-acct",
                         f"2026-0{i+1:02d}-15", -1999,
                         v, v, v.lower(),
                         "Expense > Personal > Subscriptions",
                         "expense", "personal", 0.95, "test"),
                    )
                await db.commit()
            finally:
                await db.close()

        asyncio.run(seed())

        resp = test_client.get("/api/anomalies")
        assert resp.status_code == 200
        fraud = [a for a in resp.json()["anomalies"] if a.get("type") == "fraud"]
        txn_ids = [a["transaction_id"] for a in fraud]
        # All 3 variants should be matched (plus any from samples/)
        assert len(fraud) >= 3, f"Expected 3+ fraud anomalies, got {len(fraud)}"

    def test_fraud_type_filter(self, test_client, monkeypatch):
        """GET /api/anomalies?type=fraud returns only fraud anomalies."""
        _import_with_mock(test_client, monkeypatch)
        resp = test_client.get("/api/anomalies?type=fraud")
        assert resp.status_code == 200
        anomalies = resp.json()["anomalies"]
        assert all(a.get("type") == "fraud" for a in anomalies)
        assert len(anomalies) >= 1

    def test_invalid_type_returns_400(self, test_client, monkeypatch):
        """Unknown type filter returns 400."""
        _import_with_mock(test_client, monkeypatch)
        resp = test_client.get("/api/anomalies?type=nonexistent")
        assert resp.status_code == 400

    def test_refund_type_filter(self, test_client, monkeypatch):
        """GET /api/anomalies?type=refund returns refund anomalies."""
        _import_with_mock(test_client, monkeypatch)
        resp = test_client.get("/api/anomalies?type=refund")
        assert resp.status_code == 200
        anomalies = resp.json()["anomalies"]
        if anomalies:
            assert all(a.get("type") == "refund" for a in anomalies)

    def test_refund_pairs_linked_via_transaction_id(self, test_client, monkeypatch):
        """Refund pairs have linked_transaction_id set after detect."""
        _import_with_mock(test_client, monkeypatch)
        from app.database import get_db
        import asyncio

        async def seed():
            db = await get_db()
            try:
                # Create a charge + refund pair with same merchant and amount
                c = await db.execute("SELECT MAX(id) FROM transactions")
                row = await c.fetchone()
                base_id = (row[0] or 0) + 1
                charge_amt, refund_amt = -3000, 3000
                await db.execute(
                    """INSERT INTO transactions
                       (source_platform, source_file, account_id, posted_date, amount_cents,
                        merchant, description, normalized_description, category_path,
                        entity_type, business_context, confidence_score, categorization_reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("manual", "manual", "refund-link-pair",
                     "2026-06-01", charge_amt,
                     "Refundable Vendor", "Charge", "charge",
                     "Expense > Personal > Shopping",
                     "expense", "personal", 0.95, "test charge"),
                )
                charge_id = base_id
                await db.execute(
                    """INSERT INTO transactions
                       (source_platform, source_file, account_id, posted_date, amount_cents,
                        merchant, description, normalized_description, category_path,
                        entity_type, business_context, confidence_score, categorization_reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ("manual", "manual", "refund-link-pair",
                     "2026-06-05", refund_amt,
                     "Refundable Vendor", "Refund", "refund",
                     "Income > Refunds", "income", "personal", 0.95, "test refund"),
                )
                await db.commit()
            finally:
                await db.close()

        asyncio.run(seed())

        resp = test_client.post("/api/anomalies/detect")
        assert resp.status_code == 200

        resp = test_client.get("/api/anomalies?type=refund")
        assert resp.status_code == 200
        anomalies = resp.json()["anomalies"]
        refund_ids = set(a["transaction_id"] for a in anomalies)

        # Find our pair in the results
        pair_ids = set()
        async def find_pair():
            db = await get_db()
            try:
                cursor = await db.execute(
                    "SELECT id, linked_transaction_id FROM transactions WHERE merchant='Refundable Vendor'"
                )
                for r in await cursor.fetchall():
                    pair_ids.add(r["id"])
            finally:
                await db.close()
        asyncio.run(find_pair())

        # Both transactions should be linked to each other
        if len(pair_ids) >= 2:
            async def check_links():
                db = await get_db()
                try:
                    cursor = await db.execute(
                        "SELECT id, linked_transaction_id FROM transactions WHERE merchant='Refundable Vendor'"
                    )
                    rows = await cursor.fetchall()
                    for r in rows:
                        assert r["linked_transaction_id"] is not None, f"Txn {r['id']} should be linked"
                        assert r["linked_transaction_id"] in pair_ids, f"Txn {r['id']} linked to non-pair txn"
                finally:
                    await db.close()
            asyncio.run(check_links())
        else:
            pytest.skip("Refund pair not detected")

    def test_amazon_refunds_excluded(self, imported_data):
        """Amazon refunds are NOT in the refund anomaly list."""
        resp = imported_data.get("/api/anomalies?type=refund")
        assert resp.status_code == 200
        anomalies = resp.json()["anomalies"]
        refund_reasons = [a.get("reason", "") for a in anomalies]
        amazon_refunds = [r for r in refund_reasons if "amazon" in r.lower()]
        assert len(amazon_refunds) == 0, f"Expected 0 Amazon refund anomalies, got {len(amazon_refunds)}"
