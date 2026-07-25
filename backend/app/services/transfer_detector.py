import logging

from app.services.account_meta import credit_account_ids

logger = logging.getLogger(__name__)

async def detect_transfers(db):
    already_paired_ids = set()
    cursor = await db.execute(
        "SELECT from_transaction_id, to_transaction_id FROM transfers"
    )
    for row in await cursor.fetchall():
        already_paired_ids.add(row["from_transaction_id"])
        already_paired_ids.add(row["to_transaction_id"])

    cursor = await db.execute(
        "SELECT id FROM transactions WHERE entity_type='transfer' AND linked_transaction_id IS NULL"
    )
    for row in await cursor.fetchall():
        already_paired_ids.add(row["id"])

    new_pairs = 0
    singles = 0
    seen_ids = set(already_paired_ids)

    cursor = await db.execute("""
        SELECT t1.id AS from_id, t2.id AS to_id
        FROM transactions t1
        JOIN transactions t2
            ON t1.amount_cents = -t2.amount_cents
            AND t1.account_id != t2.account_id
            AND ABS(julianday(t1.posted_date) - julianday(t2.posted_date)) <= 2
            AND t1.amount_cents < 0
            AND t2.amount_cents > 0
    """)
    for pair in await cursor.fetchall():
        fid, tid = pair["from_id"], pair["to_id"]
        if fid in seen_ids or tid in seen_ids:
            continue
        seen_ids.add(fid)
        seen_ids.add(tid)
        await db.execute(
            "INSERT INTO transfers (from_transaction_id, to_transaction_id, matched_by) VALUES (?,?,?)",
            (fid, tid, "amount_opposite")
        )
        await db.execute(
            "UPDATE transactions SET entity_type='transfer', linked_transaction_id=?, category_path='Transfer > Account-to-account' WHERE id=?",
            (tid, fid)
        )
        await db.execute(
            "UPDATE transactions SET entity_type='transfer', linked_transaction_id=?, category_path='Transfer > Account-to-account' WHERE id=?",
            (fid, tid)
        )
        new_pairs += 1

    cursor = await db.execute("""
        SELECT t1.id AS id1, t2.id AS id2
        FROM transactions t1
        JOIN transactions t2
            ON t1.amount_cents = t2.amount_cents
            AND t1.account_id != t2.account_id
            AND ABS(julianday(t1.posted_date) - julianday(t2.posted_date)) <= 2
            AND t1.amount_cents < 0
            AND t2.amount_cents < 0
            AND (
                (LOWER(t1.description) LIKE '%payment%' AND LOWER(t2.description) LIKE '%acct_xfer%')
                OR (LOWER(t2.description) LIKE '%payment%' AND LOWER(t1.description) LIKE '%acct_xfer%')
            )
    """)
    for pair in await cursor.fetchall():
        id1, id2 = pair["id1"], pair["id2"]
        if id1 in seen_ids or id2 in seen_ids:
            continue
        seen_ids.add(id1)
        seen_ids.add(id2)
        await db.execute(
            "INSERT INTO transfers (from_transaction_id, to_transaction_id, matched_by) VALUES (?,?,?)",
            (id1, id2, "same_sign_payment_xfer")
        )
        await db.execute(
            "UPDATE transactions SET entity_type='transfer', linked_transaction_id=?, category_path='Transfer > Credit-card Payment' WHERE id=?",
            (id2, id1)
        )
        await db.execute(
            "UPDATE transactions SET entity_type='transfer', linked_transaction_id=?, category_path='Transfer > Credit-card Payment' WHERE id=?",
            (id1, id2)
        )
        new_pairs += 1

    cursor = await db.execute("""
        SELECT t.id, t.account_id, t.amount_cents, t.description, t.merchant
        FROM transactions t
        WHERE (t.entity_type IS NULL OR t.entity_type != 'transfer')
          AND t.amount_cents < 0
          AND (
               LOWER(t.description) LIKE '%acct_xfer%'
               OR LOWER(t.description) LIKE '%loan_pmt%'
               OR LOWER(COALESCE(t.merchant, '')) LIKE '%cash out%'
               OR LOWER(t.description) LIKE '%general withdrawal%'
          )
    """)
    for t in await cursor.fetchall():
        if t["id"] in seen_ids:
            continue
        cat = "Transfer"
        desc_lower = (t["description"] or "").lower()
        merch_lower = (t["merchant"] or "").lower()
        if "loan_pmt" in desc_lower:
            cat = "Credit-card Payment"
        elif "cash out" in merch_lower or "cash out" in desc_lower:
            cat = "Transfer"
        await db.execute(
            "UPDATE transactions SET entity_type='transfer', category_path=? WHERE id=?", (cat, t["id"])
        )
        singles += 1

    # A credit on a CREDIT CARD described as a payment is the card being paid
    # off, not income — but the same wording on a checking account often is
    # income, so which accounts are cards has to be known. That comes from
    # `account_meta.kind`, not from a list of account ids in this file.
    credit_ids = await credit_account_ids(db)
    if credit_ids:
        placeholders = ",".join("?" for _ in credit_ids)
        cursor = await db.execute(f"""
            SELECT t.id, t.account_id, t.amount_cents, t.description, t.merchant
            FROM transactions t
            WHERE (t.entity_type IS NULL OR t.entity_type != 'transfer')
              AND t.amount_cents > 0
              AND t.account_id IN ({placeholders})
              AND LOWER(t.description) LIKE '%payment%'
              AND LOWER(t.description) NOT LIKE '%refund%'
              AND LOWER(t.description) NOT LIKE '%return%'
              AND LOWER(COALESCE(t.merchant, '')) NOT LIKE '%refund%'
              AND LOWER(COALESCE(t.merchant, '')) NOT LIKE '%return%'
        """, credit_ids)
        for t in await cursor.fetchall():
            if t["id"] in seen_ids:
                continue
            await db.execute(
                "UPDATE transactions SET entity_type='transfer', category_path='Transfer > Credit-card Payment' WHERE id=?", (t["id"],)
            )
            singles += 1

    await db.commit()
    logger.info(
        "Transfer detection: %d new pairs, %d singles", new_pairs, singles
    )
    return {"new_pairs": new_pairs, "description_singles": singles}
