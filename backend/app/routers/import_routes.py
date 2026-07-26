import logging
from fastapi import APIRouter, UploadFile, File, HTTPException
from typing import List
import os
import tempfile

from app.schemas import ImportResult, ImportResponse
from app.parsers import detect_parser
from app.parsers.base import BaseParser
from app.database import get_db
from app.services.account_meta import account_ids_by_kinds
from app.services.transfer_detector import detect_transfers
from app.services.categorizer import categorize_uncategorized
from app.services.mortgage import remove_fabricated_splits
from app.services.import_state import import_state

logger = logging.getLogger(__name__)

router = APIRouter()


async def _link_paystub_to_deposit(db, paystub_meta):
    net = paystub_meta["net_cents"]
    pay_date = paystub_meta["pay_date"]
    if not net or not pay_date:
        return

    pay_date_plus_3 = _date_add_days(pay_date, 3)
    pay_date_minus_3 = _date_add_days(pay_date, -3)

    # A paycheque lands in a deposit account, so candidates are narrowed to the
    # accounts classified that way in `account_meta` rather than to one account
    # id written into this file. With no classification stored yet, match on
    # amount and date alone instead of silently linking nothing.
    deposit_ids = await account_ids_by_kinds(db, ("checking", "savings"))
    account_clause = ""
    params = [net, pay_date_minus_3, pay_date_plus_3]
    if deposit_ids:
        account_clause = f"AND account_id IN ({','.join('?' for _ in deposit_ids)})"
        params.extend(deposit_ids)

    cursor = await db.execute(
        f"""SELECT id FROM transactions
         WHERE amount_cents=?
           AND posted_date >= ?
           AND posted_date <= ?
           {account_clause}
         ORDER BY id
         LIMIT 1""",
        params,
    )
    row = await cursor.fetchone()
    if row:
        txn_id = row[0]
        await db.execute(
            "UPDATE paystubs SET linked_transaction_id=? WHERE source_file=?",
            (txn_id, paystub_meta["source_file"])
        )
        await db.execute(
            "UPDATE transactions SET linked_transaction_id=? WHERE id=?",
            (txn_id, txn_id)
        )


def _date_add_days(date_str: str, days: int) -> str:
    from datetime import datetime, timedelta
    dt = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)
    return dt.strftime("%Y-%m-%d")

@router.post("/api/import", response_model=ImportResponse)
async def import_files(files: List[UploadFile] = File(...)):
    results = []
    db = await get_db()
    try:
        for upload in files:
            result = ImportResult(file=upload.filename or "unknown")
            if not upload.filename:
                continue

            with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(upload.filename)[1]) as tmp:
                content = await upload.read()
                tmp.write(content)
                tmp_path = tmp.name

            try:
                parser_cls = detect_parser(tmp_path)
                if not parser_cls:
                    result.skipped = 1
                    result.warnings.append("No parser found for this file")
                    results.append(result)
                    os.unlink(tmp_path)
                    continue

                parser = parser_cls()
                parsed_rows = parser.parse(tmp_path)
                result.platform_detected = parser.platform
                result.account_id = parser.account_id

                imported = 0
                dupes = 0

                if parser.platform == "paystub":
                    paystub_meta = parsed_rows[0]
                    component_txns = parsed_rows[1:]

                    exists = await db.execute(
                        "SELECT id FROM paystubs WHERE source_file=?", (paystub_meta["source_file"],)
                    )
                    if await exists.fetchone():
                        dupes = len(component_txns) + 1
                    else:
                        await db.execute(
                            """INSERT INTO paystubs
                            (source_file, employer, employee_name, period_start, period_end, pay_date,
                             gross_cents, net_cents, federal_tax_cents, medicare_cents,
                             social_security_cents, state_tax_cents, retirement_cents,
                             insurance_cents, hsa_cents, other_deductions_cents)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (paystub_meta["source_file"], paystub_meta["employer"],
                             paystub_meta["employee_name"], paystub_meta["period_start"],
                             paystub_meta["period_end"], paystub_meta["pay_date"],
                             paystub_meta["gross_cents"], paystub_meta["net_cents"],
                             paystub_meta["federal_tax_cents"], paystub_meta["medicare_cents"],
                             paystub_meta["social_security_cents"], paystub_meta["state_tax_cents"],
                             paystub_meta["retirement_cents"], paystub_meta["insurance_cents"],
                             paystub_meta["hsa_cents"], paystub_meta["other_deductions_cents"])
                        )
                        for txn in component_txns:
                            norm_desc = BaseParser.normalize_description(txn.get("description", ""))
                            exists_check = await db.execute(
                                "SELECT id FROM transactions WHERE account_id=? AND posted_date=? AND amount_cents=? AND normalized_description=?",
                                (txn["account_id"], txn["posted_date"], txn["amount_cents"], norm_desc)
                            )
                            if await exists_check.fetchone():
                                dupes += 1
                                continue
                            await db.execute(
                                """INSERT INTO transactions
                                (external_id, source_platform, source_file, account_id,
                                 posted_date, transaction_date, amount_cents, currency,
                                 merchant, description, normalized_description,
                                 entity_type, category_path, asset_class,
                                 confidence_score, categorization_reason)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (txn.get("external_id"), txn["source_platform"], txn["source_file"],
                                 txn["account_id"], txn["posted_date"], txn.get("transaction_date"),
                                 txn["amount_cents"], txn["currency"],
                                 txn["merchant"], txn["description"], norm_desc,
                                 txn.get("entity_type"), txn.get("category_path"), txn.get("asset_class"),
                                 txn.get("confidence_score"), txn.get("categorization_reason"))
                            )
                            imported += 1
                        await _link_paystub_to_deposit(db, paystub_meta)
                else:
                    for txn in parsed_rows:
                        norm_desc = BaseParser.normalize_description(txn.get("description", ""))
                        exists = await db.execute(
                            "SELECT id FROM transactions WHERE account_id=? AND posted_date=? AND amount_cents=? AND normalized_description=?",
                            (txn["account_id"], txn["posted_date"], txn["amount_cents"], norm_desc)
                        )
                        existing = await exists.fetchone()
                        if existing:
                            dupes += 1
                            continue

                        await db.execute(
                            """INSERT INTO transactions
                            (external_id, source_platform, source_file, account_id,
                             posted_date, transaction_date, amount_cents, currency,
                             merchant, description, normalized_description)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (txn["external_id"], txn["source_platform"], txn["source_file"],
                             txn["account_id"], txn["posted_date"], txn["transaction_date"],
                             txn["amount_cents"], txn["currency"],
                             txn["merchant"], txn["description"], norm_desc)
                        )
                        imported += 1

                result.imported = imported
                result.duplicates = dupes
                results.append(result)
            except Exception as e:
                result.warnings.append(str(e))
                results.append(result)
            finally:
                os.unlink(tmp_path)

        await categorize_uncategorized(db)
        await db.commit()
    finally:
        await db.close()

    return ImportResponse(results=results)

@router.get("/api/import/status")
async def import_status():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) as c FROM import_log")
        row = await cursor.fetchone()
        total_imports = row[0] if row else 0
        return {**import_state.to_dict(), "total_imports": total_imports}
    finally:
        await db.close()

@router.post("/api/import/samples")
async def import_samples(categorize: bool = True, categorize_limit: int = 0):
    """Import every file in samples/, then run the enrichment pipeline.

    ``categorize=false`` imports and dedups only, leaving the new rows
    uncategorized for an explicit, bounded ``POST /api/categorize`` afterwards.
    ``categorize_limit`` caps the in-pipeline pass the same way. Both exist
    because the default full pass fires one LLM call per uncategorized row —
    with a few thousand rows that is hours of shell-outs (see AGENTS.md), which
    is fine on a first cold import and far too slow when topping up a DB that
    is already categorized.
    """
    import glob
    samples_dir = "/app/samples"
    if not os.path.exists(samples_dir):
        samples_dir = "/home/k/budget-app-v2/samples"
    if not os.path.exists(samples_dir):
        samples_dir = "samples"
    if not os.path.exists(samples_dir):
        raise HTTPException(status_code=404, detail="samples/ directory not found")

    files = sorted(
        glob.glob(os.path.join(samples_dir, "*.CSV")) +
        glob.glob(os.path.join(samples_dir, "*.csv")) +
        glob.glob(os.path.join(samples_dir, "*.html")) +
        glob.glob(os.path.join(samples_dir, "*.pdf")) +
        glob.glob(os.path.join(samples_dir, "**", "*.pdf"), recursive=True)
    )

    import_state.start(len(files))

    results = []
    db = await get_db()
    try:
        for idx, filepath in enumerate(files):
            filename = os.path.basename(filepath)
            result = ImportResult(file=filename)
            import_state.progress(idx, f"Processing {filename} ({idx + 1}/{len(files)})")

            parser_cls = detect_parser(filepath)
            if not parser_cls:
                result.skipped = 1
                result.warnings.append("No parser found")
                results.append(result)
                continue

            parser = parser_cls()
            rows = parser.parse(filepath)
            result.platform_detected = parser.platform
            result.account_id = parser.account_id

            imported = 0
            dupes = 0

            if parser.platform == "mortgage":
                for row in rows:
                    exists = await db.execute(
                        "SELECT id FROM amortization WHERE month=?",
                        (row["month"],)
                    )
                    existing = await exists.fetchone()
                    if existing:
                        dupes += 1
                        continue
                    await db.execute(
                        "INSERT INTO amortization (month, principal_cents, interest_cents, ending_balance_cents) VALUES (?,?,?,?)",
                        (row["month"], row["principal_cents"], row["interest_cents"], row["ending_balance_cents"])
                    )
                    imported += 1
            elif parser.platform == "paystub":
                paystub_meta = rows[0]
                component_txns = rows[1:]

                exists = await db.execute(
                    "SELECT id FROM paystubs WHERE source_file=?",
                    (paystub_meta["source_file"],)
                )
                existing = await exists.fetchone()
                if existing:
                    dupes = len(component_txns) + 1
                else:
                    cur = await db.execute(
                        """INSERT INTO paystubs
                        (source_file, employer, employee_name, period_start, period_end, pay_date,
                         gross_cents, net_cents, federal_tax_cents, medicare_cents,
                         social_security_cents, state_tax_cents, retirement_cents,
                         insurance_cents, hsa_cents, other_deductions_cents)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (paystub_meta["source_file"], paystub_meta["employer"],
                         paystub_meta["employee_name"], paystub_meta["period_start"],
                         paystub_meta["period_end"], paystub_meta["pay_date"],
                         paystub_meta["gross_cents"], paystub_meta["net_cents"],
                         paystub_meta["federal_tax_cents"], paystub_meta["medicare_cents"],
                         paystub_meta["social_security_cents"], paystub_meta["state_tax_cents"],
                         paystub_meta["retirement_cents"], paystub_meta["insurance_cents"],
                         paystub_meta["hsa_cents"], paystub_meta["other_deductions_cents"])
                    )
                    paystub_id = cur.lastrowid

                    for txn in component_txns:
                        norm_desc = BaseParser.normalize_description(txn.get("description", ""))
                        exists_check = await db.execute(
                            "SELECT id FROM transactions WHERE account_id=? AND posted_date=? AND amount_cents=? AND normalized_description=?",
                            (txn["account_id"], txn["posted_date"], txn["amount_cents"], norm_desc)
                        )
                        existing_txn = await exists_check.fetchone()
                        if existing_txn:
                            dupes += 1
                            continue

                        cursor = await db.execute(
                            """INSERT INTO transactions
                            (external_id, source_platform, source_file, account_id,
                             posted_date, transaction_date, amount_cents, currency,
                             merchant, description, normalized_description,
                             entity_type, category_path, asset_class,
                             confidence_score, categorization_reason)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (txn.get("external_id"), txn["source_platform"], txn["source_file"],
                             txn["account_id"], txn["posted_date"], txn.get("transaction_date"),
                             txn["amount_cents"], txn["currency"],
                             txn["merchant"], txn["description"], norm_desc,
                             txn.get("entity_type"), txn.get("category_path"), txn.get("asset_class"),
                             txn.get("confidence_score"), txn.get("categorization_reason"))
                        )
                        imported += 1

                    await _link_paystub_to_deposit(db, paystub_meta)
            else:
                for txn in rows:
                    norm_desc = BaseParser.normalize_description(txn.get("description", ""))
                    exists = await db.execute(
                        "SELECT id FROM transactions WHERE account_id=? AND posted_date=? AND amount_cents=? AND normalized_description=?",
                        (txn["account_id"], txn["posted_date"], txn["amount_cents"], norm_desc)
                    )
                    existing = await exists.fetchone()
                    if existing:
                        dupes += 1
                        continue

                    await db.execute(
                        """INSERT INTO transactions
                        (external_id, source_platform, source_file, account_id,
                         posted_date, transaction_date, amount_cents, currency,
                         merchant, description, normalized_description)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (txn["external_id"], txn["source_platform"], txn["source_file"],
                         txn["account_id"], txn["posted_date"], txn["transaction_date"],
                         txn["amount_cents"], txn["currency"],
                         txn["merchant"], txn["description"], norm_desc)
                    )
                    imported += 1

            result.imported = imported
            result.duplicates = dupes
            results.append(result)

        if categorize:
            import_state.progress(len(files), "Running categorization…")
            await categorize_uncategorized(db, limit=categorize_limit)
        else:
            import_state.progress(len(files), "Skipping categorization (categorize=false)")
        import_state.progress(len(files), "Detecting transfers…")
        await detect_transfers(db)
        # No mortgage split step. Splitting a payment into principal/interest
        # child rows required inventing an interest figure per payment (the old
        # splitter copied a constant $843.01 out of a forward projection); the
        # Mortgage tab now derives that split at read time from the real balance.
        # What remains is removing any children a previous version wrote.
        import_state.progress(len(files), "Clearing fabricated mortgage splits…")
        await remove_fabricated_splits(db)

        await db.commit()

        import_state.progress(len(files), "Import pipeline complete")
        import_state.finish()
    except HTTPException:
        import_state.fail("HTTP error during import — samples directory not found")
        raise
    except Exception as e:
        import_state.fail(str(e))
        raise
    finally:
        await db.close()

    return ImportResponse(results=results)
