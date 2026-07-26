"""Anomaly detection service for Stories 40 & 41.

Per-transaction anomaly detectors:
- recurring-subscription: merchants in 6+ months with consecutive count & price history
- spending-spike: category spend > 2x trailing 6-month average
- duplicate-candidate: same amount, +/-3 days, similar description, same account
- new-merchant: merchants first seen in the last 30 days
- category-anomaly: refund-pair detection
- fraud (within _fraud_detector): known-fraud merchants via Levenshtein matching
"""

from collections import defaultdict
from datetime import datetime, timedelta


def _levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


async def detect_anomalies(db) -> list[dict]:
    """Run all anomaly detectors and return combined anomaly list.

    Runs sequentially (not via asyncio.gather) because aiosqlite connections
    do not support concurrent query execution.
    """
    all_anomalies = []
    for detector in (_recurring_subscriptions, _spending_spikes, _duplicate_candidates, _new_merchants, _fraud_detector, _category_anomalies):
        try:
            result = await detector(db)
            all_anomalies.extend(result)
        except Exception:
            continue
    return all_anomalies


async def _recurring_subscriptions(db) -> list[dict]:
    """Detect subscription merchants that have charged in 6+ different months.

    Returns per-transaction anomalies with consecutive_month_count and
    price_change_history.
    """
    # Categories are FLAT now ('Subscriptions'), not hierarchical
    # ('Expense > Personal > Subscriptions'). This query still asked for the
    # hierarchical form, which matches nothing — so subscription detection
    # silently returned zero findings against 233 real Subscriptions rows, and the
    # Insights tab kept showing the groups it had found before the migration. The
    # legacy prefix is still accepted so a database that predates the migration
    # keeps working.
    cursor = await db.execute("""
        SELECT id, merchant, posted_date, amount_cents
        FROM transactions
        WHERE (category_path = 'Subscriptions'
               OR category_path LIKE 'Expense > Personal > Subscriptions%')
          AND amount_cents < 0
          AND (entity_type IS NULL OR entity_type != 'transfer')
        ORDER BY merchant, posted_date
    """)
    rows = await cursor.fetchall()

    # Group by merchant FAMILY, not by the raw string. Keying on raw text split one
    # subscription into several findings — "NETFLIX" and "NETFLIX.COM" were two,
    # and "PLANET FITNESS M IClub Fees PPD ID: G710602737" was separate from
    # "Planet Fitness". That is not just untidy: each fragment has fewer distinct
    # months, so a genuine long-running subscription can fall under the 6-month bar
    # and disappear entirely.
    #
    # `group_by_merchant` is the same normaliser the Merchants page uses — payee
    # cut, then dominant-spelling consolidation — so a subscription is identified
    # here exactly as it is identified there, and it picks the readable label.
    from app.services.merchant_rollup import group_by_merchant

    families, _assignment = group_by_merchant(
        [{"id": r["id"], "merchant": r["merchant"], "description": None,
          "posted_date": r["posted_date"], "amount_cents": r["amount_cents"]}
         for r in rows]
    )

    merchants: dict = {}
    for anchor, family in families.items():
        if not anchor:
            continue  # a charge that names no payee cannot be audited as a subscription
        merchants[anchor] = {
            "label": family["label"],
            "ids": [r["id"] for r in family["rows"]],
            "months": {r["posted_date"][:7] for r in family["rows"] if r["posted_date"]},
            "amounts": [abs(r["amount_cents"]) for r in family["rows"]],
        }

    anomalies = []
    for key, info in merchants.items():
        months = sorted(info["months"])
        if len(months) < 6:
            continue

        max_consecutive = 1
        streak = 1
        for i in range(1, len(months)):
            y, m = map(int, months[i].split("-"))
            py, pm = map(int, months[i - 1].split("-"))
            if (y * 12 + m) - (py * 12 + pm) == 1:
                streak += 1
                max_consecutive = max(max_consecutive, streak)
            else:
                streak = 1

        seen_amounts = sorted(set(info["amounts"]))
        price_changes = []
        prev = None
        for amt in seen_amounts:
            if prev is not None and amt != prev:
                price_changes.append({"from_cents": prev, "to_cents": amt, "change_cents": amt - prev})
            prev = amt

        severity = "medium" if max_consecutive >= 12 else "low"

        for txn_id in info["ids"]:
            anomalies.append({
                "transaction_id": txn_id,
                "kind": "recurring-subscription",
                "severity": severity,
                "reason": (
                    f"{info['label']} charged in {len(months)} different months "
                    f"({max_consecutive} consecutive)."
                ),
                "suggested_action": f"Audit {info['label']} — cancel if no longer needed.",
                "consecutive_month_count": max_consecutive,
                "price_change_history": price_changes,
            })

    return anomalies


async def _spending_spikes(db) -> list[dict]:
    """Detect categories where current-month spend > 2x trailing 6-month avg.

    Uses the most recent complete month as the current period.
    Flags each expense transaction in that category as an anomaly.
    """
    cursor = await db.execute("""
        SELECT MAX(posted_date) AS max_date FROM transactions
        WHERE amount_cents < 0 AND entity_type != 'transfer'
    """)
    row = await cursor.fetchone()
    if not row or not row["max_date"]:
        return []
    max_date = row["max_date"]
    current_start = max_date[:8] + "01"

    # "Spending categories" used to mean `category_path LIKE 'Expense%'`, which in
    # the flat model matches exactly one row in the live DB — so spike detection
    # was dead. A flat spending category is anything that is not a transfer, not
    # Excluded and not Uncategorized; that set is defined in one place.
    from app.services.flat_categories import EXCLUDED_CATEGORY, TRANSFER_CATEGORIES

    non_spending = [*TRANSFER_CATEGORIES, EXCLUDED_CATEGORY, "Uncategorized"]
    placeholders = ",".join("?" * len(non_spending))
    cursor = await db.execute(f"""
        SELECT DISTINCT category_path
        FROM transactions
        WHERE category_path IS NOT NULL
          AND category_path NOT IN ({placeholders})
          AND amount_cents < 0
          AND (entity_type IS NULL OR entity_type != 'transfer')
          AND posted_date >= ?
    """, (*non_spending, current_start))
    active_cats = [r["category_path"] for r in await cursor.fetchall()]

    current_parts = current_start.split("-")
    cy, cm = int(current_parts[0]), int(current_parts[1])
    if cm >= 7:
        trail_start_y, trail_start_m = cy, cm - 6
    else:
        trail_start_y, trail_start_m = cy - 1, cm + 6
    trail_start = f"{trail_start_y}-{trail_start_m:02d}-01"

    anomalies = []
    for cat in active_cats:
        cursor = await db.execute("""
            SELECT COALESCE(SUM(ABS(amount_cents)), 0)
            FROM transactions
            WHERE category_path = ?
              AND amount_cents < 0
              AND (entity_type IS NULL OR entity_type != 'transfer')
              AND posted_date >= ?
              AND posted_date < ?
        """, (cat, trail_start, current_start))
        trail_spend = (await cursor.fetchone())[0] or 0
        trailing_avg = trail_spend / 6.0

        cursor = await db.execute("""
            SELECT COALESCE(SUM(ABS(amount_cents)), 0)
            FROM transactions
            WHERE category_path = ?
              AND amount_cents < 0
              AND (entity_type IS NULL OR entity_type != 'transfer')
              AND posted_date >= ?
        """, (cat, current_start))
        current_spend = (await cursor.fetchone())[0] or 0

        if trailing_avg <= 0 or current_spend < 5000:
            continue
        if current_spend <= 2 * trailing_avg:
            continue

        multiple = current_spend / max(trailing_avg, 1)
        cursor = await db.execute("""
            SELECT id FROM transactions
            WHERE category_path = ?
              AND amount_cents < 0
              AND (entity_type IS NULL OR entity_type != 'transfer')
              AND posted_date >= ?
            ORDER BY posted_date DESC
        """, (cat, current_start))
        txn_ids = [r["id"] for r in await cursor.fetchall()]

        for txn_id in txn_ids:
            anomalies.append({
                "transaction_id": txn_id,
                "kind": "spending-spike",
                "severity": "high",
                "reason": (
                    f"{cat} spend is ${current_spend/100:.0f} this month — "
                    f"{multiple:.1f}x the trailing 6-month monthly avg of ${trailing_avg/100:.0f}."
                ),
                "suggested_action": "Review individual transactions in this category for unusual drivers.",
            })

    return anomalies


async def _duplicate_candidates(db) -> list[dict]:
    """Find rows where (amount, +/-3 days, same account) match another row.

    Similar description is determined by normalized string comparison.
    Flags both rows as anomalies when a match is found.
    """
    cursor = await db.execute("""
        SELECT id, account_id, posted_date, ABS(amount_cents) AS abs_amt,
               merchant, description, normalized_description
        FROM transactions
        ORDER BY account_id, ABS(amount_cents), posted_date
    """)
    rows = await cursor.fetchall()

    grouped = defaultdict(list)
    for r in rows:
        key = (r["account_id"], r["abs_amt"])
        grouped[key].append(r)

    anomalies = []
    flagged_ids = set()

    for key, group in grouped.items():
        for i in range(len(group)):
            if group[i]["id"] in flagged_ids:
                continue
            for j in range(i + 1, len(group)):
                if group[j]["id"] in flagged_ids:
                    continue

                d1 = datetime.strptime(group[i]["posted_date"], "%Y-%m-%d")
                d2 = datetime.strptime(group[j]["posted_date"], "%Y-%m-%d")
                if abs((d2 - d1).days) > 3:
                    continue

                ni = (group[i]["normalized_description"] or group[i]["description"] or "").strip().lower()
                nj = (group[j]["normalized_description"] or group[j]["description"] or "").strip().lower()
                mi = (group[i]["merchant"] or "").strip().lower()
                mj = (group[j]["merchant"] or "").strip().lower()

                tokens_i = set(ni.split()) | set(mi.split())
                tokens_j = set(nj.split()) | set(mj.split())
                common = tokens_i & tokens_j
                total = tokens_i | tokens_j
                similarity = len(common) / max(len(total), 1)

                if similarity < 0.3:
                    continue

                for dup_id in (group[i]["id"], group[j]["id"]):
                    if dup_id not in flagged_ids:
                        anomalies.append({
                            "transaction_id": dup_id,
                            "kind": "duplicate-candidate",
                            "severity": "low",
                            "reason": (
                                f"Potential duplicate in {group[i]['account_id']}: "
                                f"${group[i]['abs_amt']/100:.2f} on {group[i]['posted_date']} "
                                f"and {group[j]['posted_date']} "
                                f"(merchants: {group[i]['merchant']} / {group[j]['merchant']})."
                            ),
                            "suggested_action": "Review and remove the duplicate if confirmed.",
                        })
                        flagged_ids.add(dup_id)
                break

    return anomalies


async def _new_merchants(db) -> list[dict]:
    """Flag merchants first seen in the last 30 days with <= 3 total txns."""
    thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    cursor = await db.execute("""
        SELECT id, merchant, account_id, posted_date, amount_cents,
               (SELECT COUNT(*) FROM transactions AS t2
                WHERE LOWER(t2.merchant) = LOWER(t.merchant)
                  AND t2.account_id = t.account_id) AS txn_count
        FROM transactions AS t
        WHERE merchant != ''
          AND posted_date >= ?
        ORDER BY merchant, account_id
    """, (thirty_days_ago,))
    rows = await cursor.fetchall()

    seen_keys = set()
    anomalies = []
    for r in rows:
        key = (r["merchant"].strip().lower(), r["account_id"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if r["txn_count"] > 3:
            continue

        first_cursor = await db.execute("""
            SELECT MIN(posted_date) FROM transactions
            WHERE LOWER(merchant) = ? AND account_id = ?
        """, (r["merchant"].strip().lower(), r["account_id"]))
        first_row = await first_cursor.fetchone()
        first_date = first_row[0] if first_row else ""

        if first_date and first_date >= thirty_days_ago:
            anomalies.append({
                "transaction_id": r["id"],
                "kind": "new-merchant",
                "severity": "low",
                "reason": (
                    f"'{r['merchant']}' first appeared on {first_date} in "
                    f"{r['account_id']} ({r['txn_count']} transactions so far)."
                ),
                "suggested_action": "Verify this merchant is legitimate.",
            })

    return anomalies


async def _fraud_detector(db) -> list[dict]:
    """Detect known-fraud merchants via Levenshtein + substring matching.

    Matches the owner's configured fraud patterns (case-insensitive, from the
    `fraud_patterns` table) using two strategies:
    1. Levenshtein distance ≤ 2 (close variants like 'Furnishd Finder')
    2. Substring containment after normalization, which handles the trailing
       noise a card feed appends ('EXAMPLECO.COM 05/07 PURCHASE ...')
    Sets type=fraud and flag_reason='fraud' for use by update_flagged_transactions.
    """
    cursor = await db.execute("""
        SELECT DISTINCT LOWER(merchant) AS merchant_lower
        FROM transactions
        WHERE merchant != ''
    """)
    distinct_merchants = [r["merchant_lower"] for r in await cursor.fetchall()]

    # Loaded from the DB, never hardcoded: which merchants defrauded THIS owner is
    # their private information, and an app should not accuse a merchant nobody
    # told it about. No configured patterns means no fraud matches, which is the
    # right answer for a fresh install rather than a reason to guess.
    cursor = await db.execute("SELECT pattern FROM fraud_patterns")
    configured = [
        (r["pattern"] or "").strip().lower() for r in await cursor.fetchall()
    ]
    configured = [p for p in configured if p]
    if not configured:
        return []
    # A configured pattern is matched two ways: fuzzily, to catch the spelling
    # variants a card feed invents, and as a substring of the merchant text.
    fraud_patterns = configured
    fraud_substrings = configured
    matched_merchants = set()
    for dm in distinct_merchants:
        for pattern in fraud_patterns:
            if _levenshtein_distance(dm, pattern) <= 2:
                matched_merchants.add(dm)
                break
        else:
            for sub in fraud_substrings:
                if sub in dm:
                    matched_merchants.add(dm)
                    break

    if not matched_merchants:
        return []

    placeholders = ",".join("?" for _ in matched_merchants)
    cursor = await db.execute(f"""
        SELECT id, merchant, posted_date, amount_cents, category_path
        FROM transactions
        WHERE LOWER(merchant) IN ({placeholders})
    """, list(matched_merchants))
    rows = await cursor.fetchall()

    anomalies = []
    for r in rows:
        anomalies.append({
            "transaction_id": r["id"],
            "kind": "category-anomaly",
            "type": "fraud",
            "severity": "high",
            "reason": (
                f"'{r['merchant']}' is a known fraudulent merchant. "
                f"Amount: ${abs(r['amount_cents'])/100:.2f} on {r['posted_date']}."
            ),
            "suggested_action": "Flag as fraud and request chargeback if not already refunded.",
        })
    return anomalies


async def _category_anomalies(db) -> list[dict]:
    """Detect refund-pair anomalies.

    Refund pairs: same merchant, opposite-signed amounts within 30 days.
    """
    anomalies = []

    # Refund-pair detection: opposite-signed amounts, same merchant, within 30 days.
    cursor = await db.execute("""
        SELECT t1.id AS id1, t1.merchant, t1.posted_date AS d1, t1.amount_cents AS a1,
               t2.id AS id2, t2.posted_date AS d2, t2.amount_cents AS a2
        FROM transactions t1
        JOIN transactions t2 ON LOWER(t2.merchant) = LOWER(t1.merchant)
            AND t2.id != t1.id
            AND t2.amount_cents > 0
            AND t1.amount_cents < 0
        WHERE t1.amount_cents < 0
          AND ABS(JULIANDAY(t2.posted_date) - JULIANDAY(t1.posted_date)) <= 30
          AND ABS(t2.amount_cents) BETWEEN ABS(t1.amount_cents) * 0.5 AND ABS(t1.amount_cents) * 1.5
          AND LOWER(t1.merchant) NOT LIKE '%amazon%'
        ORDER BY t1.merchant, t1.posted_date
    """)
    rows = await cursor.fetchall()

    seen_refund_ids = set()
    for r in rows:
        if r["id1"] not in seen_refund_ids:
            anomalies.append({
                "transaction_id": r["id1"],
                "kind": "category-anomaly",
                "type": "refund",
                "severity": "medium",
                "reason": (
                    f"Transaction from '{r['merchant']}' on {r['d1']} "
                    f"(${abs(r['a1'])/100:.2f}) was refunded on {r['d2']} "
                    f"(${r['a2']/100:.2f}). Refunded charges are candidates for review."
                ),
                "suggested_action": "Verify the refund was processed correctly.",
            })
            seen_refund_ids.add(r["id1"])
        if r["id2"] not in seen_refund_ids:
            anomalies.append({
                "transaction_id": r["id2"],
                "kind": "category-anomaly",
                "type": "refund",
                "severity": "low",
                "reason": (
                    f"Refund of ${r['a2']/100:.2f} from '{r['merchant']}' on {r['d2']} "
                    f"corresponding to original charge on {r['d1']} (${abs(r['a1'])/100:.2f})."
                ),
                "suggested_action": "Confirm the refund matches the original charge.",
            })
            seen_refund_ids.add(r["id2"])

    return anomalies


async def update_flagged_transactions(db, anomalies: list[dict]) -> int:
    """Persist anomaly flags to the transactions table.

    Sets flagged=1 and flag_reason on each transaction referenced in anomalies.
    For fraud-type anomalies, flag_reason is set to 'fraud'.
    Refund-pair anomalies get flag_reason='refund-pair'.
    Other anomalies get f"[{kind}] {reason}".
    Returns the count of distinct transactions updated.

    A transaction can trip several detectors, and only one reason can be stored, so
    the MOST serious claim wins. This used to be decided by detector order — first
    writer kept the row — which meant a fraudulent charge from a merchant seen for
    the first time was labelled "new merchant", because that detector runs earlier.
    Losing a fraud flag to a routine one is the worst possible way to break the tie.
    """
    def severity_rank(a: dict) -> int:
        kind = a.get("type")
        if kind == "fraud":
            return 0
        if kind == "refund":
            return 1
        return 2

    anomalies = sorted(anomalies, key=severity_rank)

    seen = set()
    updated = 0
    refund_pairs = []
    for a in anomalies:
        txn_id = a["transaction_id"]
        if txn_id in seen:
            continue
        seen.add(txn_id)
        if a.get("type") == "fraud":
            reason = "fraud"
        elif a.get("type") == "refund":
            reason = "refund-pair"
            refund_pairs.append(a)
        else:
            reason = f"[{a['kind']}] {a['reason']}"
        await db.execute(
            "UPDATE transactions SET flagged=1, flag_reason=? WHERE id=?",
            (reason, txn_id),
        )
        updated += 1
    await db.commit()

    # Link refund pairs via linked_transaction_id
    linked = await _link_refund_pairs(db, refund_pairs)
    return updated


async def _link_refund_pairs(db, refund_anomalies: list[dict]) -> int:
    """Find refund pairs among refund-type anomalies and link them.

    Uses the same SQL as _category_anomalies to find charge↔refund pairs
    and sets linked_transaction_id in both directions.
    Returns the number of links created.
    """
    cursor = await db.execute("""
        SELECT t1.id AS id1, t2.id AS id2
        FROM transactions t1
        JOIN transactions t2 ON LOWER(t2.merchant) = LOWER(t1.merchant)
            AND t2.id != t1.id
            AND t2.amount_cents > 0
            AND t1.amount_cents < 0
        WHERE t1.amount_cents < 0
          AND ABS(JULIANDAY(t2.posted_date) - JULIANDAY(t1.posted_date)) <= 30
          AND ABS(t2.amount_cents) BETWEEN ABS(t1.amount_cents) * 0.5 AND ABS(t1.amount_cents) * 1.5
          AND LOWER(t1.merchant) NOT LIKE '%amazon%'
    """)
    rows = await cursor.fetchall()

    linked = 0
    seen = set()
    for r in rows:
        id1, id2 = r["id1"], r["id2"]
        if id1 in seen or id2 in seen:
            continue
        seen.add(id1)
        seen.add(id2)
        await db.execute(
            "UPDATE transactions SET linked_transaction_id=? WHERE id=?",
            (id2, id1),
        )
        await db.execute(
            "UPDATE transactions SET linked_transaction_id=? WHERE id=?",
            (id1, id2),
        )
        linked += 1
    if linked:
        await db.commit()
    return linked
