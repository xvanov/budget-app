import asyncio
import json
import logging
import time
from typing import List, Dict, Any, Optional

from app.llm import call_llm, clean_response
from app.services.sign_invariant import (
    coerce_sign_safe,
    log_violation_autofix,
    violates_sign_invariant,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 20
MAX_CONCURRENT = 5
DB_WRITE_LOCK = asyncio.Lock()

CATEGORY_TREE = """Groceries
Restaurants
Travel
Shopping
Subscriptions
Utilities
Housing
Transport
Health
Entertainment
Personal Care
Education
Fees & Interest
Business
Gifts & Donations
Taxes
Salary
Rental Income
Other Income
Other
Transfer
Credit-card Payment
Mortgage Payment"""

GROUND_TRUTH_RULES = """CATEGORIES ARE FLAT — a single name, never a path. Never emit '>' in a category.

DO NOT decide income vs expense: it is derived from the amount's sign automatically. A negative amount is spending, a positive amount is money in. Just pick the best-fitting category name.

A positive amount on a spending category is a REFUND of that category (e.g. +$64 on Shopping is money back on shopping). Use the same category the purchase would have had — do not invent a refund category.

1. CREDIT-CARD PAYMENTS ARE NEVER SPENDING => 'Credit-card Payment'. All three Chase accounts are linked, so a payment from checking to a card appears twice: as an outflow on checking and a credit on the card. Both legs are 'Credit-card Payment'. Includes 'AUTOMATIC PAYMENT - THANK YOU', 'Payment Thank You', 'CHASE CREDIT CRD EPAY'. Treating either leg as spending double-counts every purchase already on the card.
2. Payments to a mortgage servicer (e.g. 'To HMG') => 'Mortgage Payment'.
3. Movement between the owner's own accounts ('BOOK TRANSFER', 'Online Transfer to/from <bank>', transfers naming U.S. Bank / BofA / Chase) => 'Transfer'.
4. Amazon item rows (merchant begins 'Amazon: ') are individual products split from one charge — categorize each by its PRODUCT NAME. A phone charger => Shopping; snacks => Groceries; a tool => Housing. A 'Tax & Shipping' row takes the same category as the largest item in its order.
5. Restaurants, cafes, bars, food delivery => 'Restaurants'. Supermarkets and grocers => 'Groceries'.
6. Streaming and software subscriptions (Netflix, Spotify, Apple, OpenAI, VPNs) => 'Subscriptions'.
7. Flights, hotels, trains, car hire, foreign lodging => 'Travel'.
8. Rideshare, fuel, parking, transit, tolls => 'Transport'.
9. Utility companies, phone and internet => 'Utilities'.
10. Rent, mortgage interest, repairs, home maintenance and anything property-related => 'Housing'.
11. Salary and payroll deposits => 'Salary'. Rent received from tenants => 'Rental Income'.
12. Tax payments and payroll tax deductions => 'Taxes'.
13. Bank fees, card interest, FX fees => 'Fees & Interest'.
14. When nothing fits, use 'Other' — do not invent a new category name."""


def determine_entity_type(category_path: str, amount_cents: int = None) -> str:
    from app.services.flat_categories import (
        entity_type_for, is_excluded_category, is_transfer_category,
    )

    if amount_cents is not None:
        return entity_type_for(category_path, amount_cents)
    if is_excluded_category(category_path):
        return "excluded"
    if is_transfer_category(category_path):
        return "transfer"
    if not category_path:
        return "expense"
    if category_path.startswith("Income"):
        return "income"
    if category_path.startswith("Transfer"):
        return "transfer"
    return "expense"


def determine_asset_class(category_path: str) -> Optional[str]:
    if not category_path:
        return None
    if "Mortgage Principal" in category_path:
        return "mortgage_principal"
    if "Mortgage Escrow" in category_path:
        return "escrow"
    return None


def build_categorization_prompt(transactions: List[Dict[str, Any]]) -> str:
    ids = [t["id"] for t in transactions]
    lines = [
        "You are categorizing personal finance transactions. Use the category tree below and the ground-truth rules to assign each transaction to the correct category.",
        "",
        "AVAILABLE CATEGORIES (use one of these exact names — they are FLAT, no '>'):",
        CATEGORY_TREE,
        "",
        "GROUND-TRUTH RULES:",
        GROUND_TRUTH_RULES,
        "",
        "For each transaction respond with: category_path (one exact category NAME from the list above), confidence_score (0-1), categorization_reason (brief). Do NOT send entity_type — income vs expense is derived from the amount sign.",
        "",
        f"You MUST respond with a JSON array containing exactly {len(ids)} objects, one per transaction ID. Do not skip any. Do not truncate the array.",
        "",
        "Transactions:",
    ]

    for txn in transactions:
        amt_str = f"${txn['amount_cents']/100:.2f}"
        desc = (txn.get('description') or '')[:100]
        merch = (txn.get('merchant') or '')[:80]
        acct = txn.get('account_id', '')
        lines.append(f"  ID={txn['id']}: {txn['posted_date']} | {acct} | {merch} | {amt_str} | {desc}")

    lines.append("")
    lines.append("Respond with ONLY a valid JSON array. No other text. Do not wrap in markdown.")
    lines.append("[")
    lines.append('  {"id": 1, "category_path": "Subscriptions", "confidence_score": 0.95, "categorization_reason": "Known subscription service"},')
    lines.append("  ...")
    lines.append("]")

    return "\n".join(lines)


async def _db_execute(db, sql: str, params):
    for attempt in range(3):
        try:
            async with DB_WRITE_LOCK:
                await db.execute(sql, params)
            return
        except Exception as e:
            if "database is locked" in str(e) and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise


async def _categorize_batch(db, batch: List[Dict[str, Any]]) -> int:
    batch_ids = {t["id"] for t in batch}
    prompt = build_categorization_prompt(batch)

    try:
        response = await asyncio.to_thread(call_llm, prompt)
    except Exception as e:
        logger.error("LLM call failed: %s", e)
        response = None

    if not response:
        for txn in batch:
            await _db_execute(
                db,
                "UPDATE transactions SET category_path=?, entity_type=?, confidence_score=?, categorization_reason=? WHERE id=?",
                ("Uncategorized", "expense", 0.0, "LLM unavailable", txn["id"])
            )
        return 0

    cleaned = clean_response(response)
    try:
        results = json.loads(cleaned)
        if isinstance(results, dict):
            results = [results]
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM response as JSON (length=%d): %s...", len(cleaned), cleaned[:200])
        results = []

    if not results or not isinstance(results, list):
        for txn in batch:
            await _db_execute(
                db,
                "UPDATE transactions SET category_path=?, entity_type=?, confidence_score=?, categorization_reason=? WHERE id=?",
                ("Uncategorized", "expense", 0.0, "LLM response parse failed", txn["id"])
            )
        return 0

    result_by_id = {r["id"]: r for r in results if r.get("id") is not None}
    found_ids = set(result_by_id.keys())
    missing_ids = batch_ids - found_ids

    if missing_ids:
        logger.warning("LLM response missing %d/%d IDs: %s", len(missing_ids), len(batch_ids), missing_ids)

    categorized = 0
    for txn in batch:
        txn_id = txn["id"]
        if txn_id in result_by_id:
            r = result_by_id[txn_id]
            category_path = r.get("category_path", "Uncategorized")
            # Categories are flat. If the model emits a legacy path anyway
            # ("Expense > Personal > Dining"), map it rather than storing it —
            # one stray hierarchical value re-splits every total that groups by
            # category.
            from app.services.flat_categories import flatten_path

            flattened = flatten_path(category_path)
            if flattened and flattened != category_path:
                logger.info("Flattened LLM category '%s' -> '%s'", category_path, flattened)
                category_path = flattened
            # Ignore any entity_type the model offers: the sign decides.
            entity_type = determine_entity_type(category_path, txn.get("amount_cents"))
            confidence = r.get("confidence_score", 0.0)
            reason = r.get("categorization_reason", "")
            business_context = r.get("business_context", "personal")

            # Sign-invariant: reject LLM assignments where sign and direction
            # don't agree. Coerce to a sign-appropriate fallback and document.
            amount_cents = int(txn.get("amount_cents") or 0)
            if violates_sign_invariant(entity_type, amount_cents):
                new_cp, new_et, new_bc, note = coerce_sign_safe(
                    category_path, entity_type, amount_cents, business_context
                )
                log_violation_autofix(
                    txn_id,
                    {"category_path": category_path, "entity_type": entity_type,
                     "business_context": business_context, "amount_cents": amount_cents},
                    {"category_path": new_cp, "entity_type": new_et,
                     "business_context": new_bc},
                    note or "sign-invariant autofix",
                )
                category_path, entity_type, business_context = new_cp, new_et, new_bc
                confidence = 0.0
                reason = (note or reason)

            asset_class = r.get("asset_class") or determine_asset_class(category_path)

            await _db_execute(
                db,
                """UPDATE transactions
                   SET category_path=?, entity_type=?, asset_class=?,
                       confidence_score=?, categorization_reason=?, business_context=?
                   WHERE id=?""",
                (category_path, entity_type, asset_class, confidence, reason, business_context, txn_id)
            )
            categorized += 1
        else:
            await _db_execute(
                db,
                "UPDATE transactions SET category_path=?, entity_type=?, confidence_score=?, categorization_reason=? WHERE id=?",
                ("Uncategorized", "expense", 0.0, "Missing from LLM response", txn_id)
            )

    return categorized


async def categorize_uncategorized(db, batch_size: int = BATCH_SIZE, max_concurrent: int = MAX_CONCURRENT, limit: int = 0):
    from app.services.rules_service import apply_rules
    rules_categorized = await apply_rules(db)

    cursor = await db.execute(
        "SELECT id, merchant, description, amount_cents, posted_date, account_id, source_platform "
        "FROM transactions WHERE category_path IS NULL OR category_path = 'Uncategorized'"
    )
    rows = await cursor.fetchall()

    if not rows:
        return rules_categorized

    if limit > 0 and len(rows) > limit:
        rows = rows[:limit]

    dict_rows = [dict(r) for r in rows]
    batches = [dict_rows[i:i+batch_size] for i in range(0, len(dict_rows), batch_size)]

    logger.info("Categorizing %d transactions in %d batches of %d", len(dict_rows), len(batches), batch_size)

    semaphore = asyncio.Semaphore(max_concurrent)

    async def process(batch):
        async with semaphore:
            return await _categorize_batch(db, batch)

    tasks = [process(b) for b in batches]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    total = rules_categorized
    for r in results:
        if isinstance(r, Exception):
            logger.error("Batch failed: %s", r)
        else:
            total += r

    await db.commit()
    return total


async def categorize_single_transaction(db, txn_id: int) -> bool:
    cursor = await db.execute(
        "SELECT id, merchant, description, amount_cents, posted_date, account_id, source_platform "
        "FROM transactions WHERE id=?", (txn_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return False

    txn = dict(row)
    count = await _categorize_batch(db, [txn])
    await db.commit()
    return count > 0
