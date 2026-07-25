import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Flat categories. Principal and escrow are internal movement; interest, taxes
# and insurance are real costs.
MORTGAGE_CATEGORIES = ["Mortgage Payment", "Housing", "Taxes"]

async def _find_mortgage_payments(db):
    cursor = await db.execute("""
        SELECT id, posted_date, amount_cents, description, merchant, account_id,
               source_platform, source_file
        FROM transactions
        WHERE merchant = 'HMG Mortgage'
          AND amount_cents < 0
          AND (category_path IS NULL
               OR category_path NOT IN ('Mortgage Payment', 'Transfer'))
    """)
    return await cursor.fetchall()


async def _find_nearest_amortization(db, payment_month: str) -> Optional[Dict[str, Any]]:
    cursor = await db.execute("""
        SELECT month, principal_cents, interest_cents,
               ABS(julianday(month || '-01') - julianday(?)) as day_diff
        FROM amortization
        ORDER BY day_diff
        LIMIT 1
    """, (payment_month,))
    row = await cursor.fetchone()
    if row:
        return dict(row)
    return None


async def _create_component(db, parent_id: int, amount_cents: int, merchant: str,
                            description: str, category_path: str, entity_type: str,
                            asset_class: Optional[str], source_platform: str,
                            source_file: str, account_id: str, posted_date: str,
                            categorization_reason: str):
    # normalized_description must be populated: it is half of the dedup
    # fingerprint (account, date, amount, normalized_description). Leaving it at
    # the column default of '' gave every split component a blank fingerprint
    # half, so unrelated components sharing a date and amount could collide.
    from app.parsers.base import BaseParser

    await db.execute(
        """INSERT INTO transactions
           (source_platform, source_file, account_id, posted_date, amount_cents,
            currency, merchant, description, normalized_description, category_path,
            entity_type, asset_class, linked_transaction_id, confidence_score,
            categorization_reason, business_context)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_platform, source_file, account_id, posted_date,
         -amount_cents, "USD", merchant, description,
         BaseParser.normalize_description(description),
         category_path, entity_type, asset_class,
         parent_id, 1.0, categorization_reason, "personal")
    )


async def split_mortgage_payments(db) -> Dict[str, Any]:
    payments = await _find_mortgage_payments(db)

    if not payments:
        return {"split": 0, "warnings": []}

    warnings = []
    split_count = 0

    for payment in payments:
        payment_month = payment["posted_date"][:7] + "-01"
        total_cents = abs(payment["amount_cents"])

        cursor = await db.execute(
            "SELECT principal_cents, interest_cents FROM amortization WHERE month = ?",
            (payment_month,)
        )
        amort = await cursor.fetchone()

        if amort:
            principal_cents = amort["principal_cents"]
            interest_cents = amort["interest_cents"]
        else:
            nearest = await _find_nearest_amortization(db, payment_month)
            if nearest:
                interest_cents = nearest["interest_cents"]
                warnings.append(
                    f"Payment on {payment['posted_date']} (${total_cents/100:.2f}): "
                    f"month outside amortization range, estimated interest "
                    f"${interest_cents/100:.2f} from nearest month {nearest['month']}"
                )
            else:
                await db.execute(
                    "UPDATE transactions SET entity_type='transfer', "
                    "category_path='Mortgage Payment', "
                    "asset_class='mortgage_principal' WHERE id=?",
                    (payment["id"],)
                )
                warnings.append(
                    f"Payment on {payment['posted_date']} (${total_cents/100:.2f}): "
                    f"no amortization data, marked as Mortgage Payment"
                )
                split_count += 1
                continue

            principal_cents = total_cents - interest_cents
            if principal_cents < 0:
                principal_cents = 0
                interest_cents = total_cents

        orig_merchant = payment["merchant"] or "HMG Mortgage"
        src_platform = payment["source_platform"] or "chase"
        src_file = payment["source_file"] or ""
        acct = payment["account_id"] or ""
        pdate = payment["posted_date"] or ""

        await db.execute(
            "UPDATE transactions SET entity_type='transfer', "
            "category_path='Transfer', "
            "asset_class=NULL WHERE id=?",
            (payment["id"],)
        )

        if principal_cents > 0:
            await _create_component(
                db, payment["id"], principal_cents, "Mortgage Principal",
                f"Principal split from {orig_merchant} payment",
                "Mortgage Payment", "transfer", "mortgage_principal",
                src_platform, src_file, acct, pdate,
                f"Principal component: ${principal_cents/100:.2f}"
            )

        if interest_cents > 0:
            await _create_component(
                db, payment["id"], interest_cents, "Mortgage Interest",
                f"Interest split from {orig_merchant} payment",
                "Housing", "expense", None,
                src_platform, src_file, acct, pdate,
                f"Interest component: ${interest_cents/100:.2f}"
            )

        escrow_cents = total_cents - principal_cents - interest_cents
        if escrow_cents > 0:
            await _create_component(
                db, payment["id"], escrow_cents, "Mortgage Escrow",
                f"Escrow split from {orig_merchant} payment",
                "Mortgage Payment", "transfer", "escrow",
                src_platform, src_file, acct, pdate,
                f"Escrow component: ${escrow_cents/100:.2f}"
            )

        split_count += 1

    await db.commit()
    logger.info(
        "Mortgage splitter: %d payments split, %d warnings",
        split_count, len(warnings)
    )
    return {"split": split_count, "warnings": warnings}
