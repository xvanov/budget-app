import json
import asyncio
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.llm import call_llm
from app.schemas import ChatCorrectRequest, ChatCorrectResponse
from app.routers.context import append_context_fact
from app.routers.categories import ensure_flat_category
from app.routers.transactions import category_neutralises_split, split_parent_ids
from app.services.flat_categories import (
    EXCLUDED_CATEGORY,
    MIGRATION_MAP,
    TRANSFER_CATEGORIES,
    CategoryDeleted,
    flatten_path,
)
from app.services.rules_service import (
    add_rule,
    determine_entity_type,
    find_equivalent_rule,
    get_rule,
)
from app.services.sign_invariant import (
    coerce_sign_safe,
    log_violation_autofix,
    violates_sign_invariant,
)


class ChatCorrectRequestExt(BaseModel):
    message: str
    source: Optional[str] = "chat-page"

logger = logging.getLogger(__name__)

router = APIRouter()


def _build_chat_prompt(user_message: str, merchants: list[str], categories: list[str],
                        accounts: list[dict], txn_stats: dict) -> str:
    lines = [
        "You are a personal finance assistant. The user gave you a natural-language correction or instruction.",
        "Interpret it and return a JSON object describing what actions to take.",
        "",
        "## Categories are FLAT — this is the most important rule",
        "",
        "A category is a plain single name: `Groceries`, `Restaurants`, `Housing`.",
        "There is no hierarchy and there are no parent categories. A `category_path`",
        "must NEVER contain the `>` character, and must never be prefixed with",
        "`Income`, `Expense` or `Transfer`. Write `Groceries`, not a prefixed path.",
        "",
        "Income vs expense is DERIVED FROM THE TRANSACTION'S AMOUNT SIGN, never from",
        "the category: a negative amount is money out, a positive amount is money in.",
        "So do not try to encode direction in the name, and do not pick a different",
        "category just because a row is positive — a positive amount on `Groceries` is",
        "money back on groceries and nets against that category automatically.",
        "",
        "There are four special buckets:",
        f"  - `{TRANSFER_CATEGORIES[0]}` — money moving between the owner's OWN accounts. Neither",
        "    spending nor income; it is what stops internal movement from being counted",
        "    as spending (a checking-to-card payment appears on both accounts).",
        f"  - `{TRANSFER_CATEGORIES[1]}` — a payment from a bank account to one of the",
        "    owner's own credit cards. Internal movement too, kept separate so card",
        "    payments stay visible.",
        f"  - `{TRANSFER_CATEGORIES[2]}` — principal and escrow moving into the mortgage.",
        f"  - `{EXCLUDED_CATEGORY}` — the opt-out bucket. Unlike a transfer this is a REAL",
        "    external transaction the owner has decided should not count: a bill fronted",
        "    for someone who paid it back, or a one-off that would distort a category. It",
        "    leaves every total, chart and budget but stays visible on its own tab.",
        "",
        "A NEW flat name is allowed and will be created automatically, but prefer an",
        "existing category from the list below whenever one fits.",
        "",
        "## Available actions",
        "",
        '1. **tag**: `{"type": "tag", "params": {"search": "<substring to match in merchant/description>", "tags": ["<tag1>", "<tag2>"]}}`',
        "   Adds the given tags to all transactions whose merchant or description contains the search string (case-insensitive).",
        "",
        '2. **recategorize**: `{"type": "recategorize", "params": {"search": "<substring>", "category_path": "<flat category name>"}}`',
        "   Changes the category of all matching transactions. The ``category_path`` is a",
        "   single flat name; it may be one that does not exist yet, in which case it is",
        "   created. Use this freely when the user asks you to put transactions into a new",
        "   bucket. A rule is created alongside it so future imports match automatically.",
        "",
        '3. **add_rule**: `{"type": "add_rule", "params": {"pattern": "<merchant substring>", "category_path": "<flat category name>", "description": "<human-readable rule>"}}`',
        "   Creates an auto-apply rule so future imports matching the pattern get categorized without LLM.",
        "   The ``category_path`` is a flat name and may be a new one.",
        "",
        '4. **update_context**: `{"type": "update_context", "params": {"fact": "<fact to append to budget_context.md>"}}`',
        "   Records a new fact in the living knowledge base.",
        "",
        '5. **create_category**: `{"type": "create_category", "params": {"category_path": "<flat category name>"}}`',
        "   Ensures a flat category exists without touching any transactions. Use this",
        "   when the user explicitly says \"create a category\" without an immediate re-categorize.",
        "",
        "Return ONLY valid JSON:",
        """{
  "summary": "Brief human-readable summary of what was done",
  "actions": [<action objects>]
}""",
        "",
        "## Current data overview",
        "",
        f"Transaction count: {txn_stats.get('count', 0)}",
        f"Date range: {txn_stats.get('min_date', 'N/A')} to {txn_stats.get('max_date', 'N/A')}",
        f"Total income: ${txn_stats.get('income', 0) / 100:,.2f}",
        f"Total expenses: ${txn_stats.get('expense', 0) / 100:,.2f}",
        "",
        "### Known merchants (sample):",
    ]
    for m in merchants[:80]:
        lines.append(f"  - {m}")
    lines.append("")
    lines.append("### Existing categories (all flat; prefer one of these):")
    for c in categories[:80]:
        lines.append(f"  - {c}")
    lines.append("")
    lines.append(f"### Accounts ({len(accounts)}):")
    for a in accounts[:20]:
        lines.append(f"  - {a['account_id']} ({a['source_platform']}): {a['cnt']} transactions")
    lines.append("")
    lines.append("## User message")
    lines.append(user_message)
    lines.append("")
    lines.append("Respond with ONLY the JSON object. No markdown, no explanation.")
    return "\n".join(lines)


def _coerce_flat_category(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Coerce an LLM-supplied ``category_path`` to a flat name.

    Returns ``(flat_name, note)``. ``flat_name`` is ``None`` when the value is
    empty or maps to Uncategorized — the caller must then skip the action rather
    than write ``None`` over a good category. ``note`` is non-``None`` whenever
    the value was rewritten, so the coercion shows up in ``context_diff``.

    The prompt tells the model categories are flat, but a prompt is not a
    guarantee, so every path is sanitized here as well. Legacy paths the migration
    map knows (``Expense > Personal > Dining``) use its answer (``Restaurants``).
    For anything else the LEAF is the name the model meant, and it wins over the
    prefix: on a path like ``Expense > Weekend Fun`` — a brand-new bucket the user
    asked for — ``flatten_path``'s longest-prefix fallback matches the generic
    ``Expense`` rule and answers ``Other``, quietly discarding the request.
    """
    original = (raw or "").strip()
    if not original:
        return None, None
    if " > " in original and original not in MIGRATION_MAP:
        flat = flatten_path(original.split(" > ")[-1].strip())
    else:
        flat = flatten_path(original)
    if flat == original:
        return flat, None
    if not flat:
        return None, (
            f"Skipped action: category '{original}' resolves to Uncategorized"
        )
    return flat, (
        f"Coerced hierarchical category '{original}' to flat '{flat}' "
        "(categories are flat names now)"
    )


@router.get("/api/chat-corrections")
async def list_chat_corrections(limit: int = 50):
    """Return chat-correct history newest-first."""
    if limit < 1:
        limit = 1
    if limit > 500:
        limit = 500
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, created_at, prompt, response, affected_count, affected_ids, "
            "context_diff, source FROM chat_corrections ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        items = []
        for r in rows:
            try:
                aff = json.loads(r["affected_ids"]) if r["affected_ids"] else []
            except (json.JSONDecodeError, TypeError):
                aff = []
            items.append({
                "id": r["id"],
                "created_at": r["created_at"],
                "prompt": r["prompt"],
                "response": r["response"],
                "affected_count": r["affected_count"],
                "affected_ids": aff,
                "context_diff": r["context_diff"],
                "source": r["source"],
            })
        return {"items": items, "total": len(items)}
    finally:
        await db.close()


@router.get("/api/chat-corrections/{correction_id}/transactions")
async def chat_correction_transactions(correction_id: int, limit: int = 200):
    """The transactions a past chat correction actually touched.

    "Affected 287" is not reviewable on its own — the point of this is that the
    user can see WHICH rows a correction moved. Ids stored on the correction that
    no longer exist (row deleted since) are simply omitted from ``transactions``
    but still counted in ``total``, so the number never silently shrinks.
    """
    if limit < 1:
        limit = 1
    if limit > 1000:
        limit = 1000
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, affected_ids FROM chat_corrections WHERE id=?", (correction_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"Chat correction {correction_id} not found"
            )
        try:
            raw_ids = json.loads(row["affected_ids"]) if row["affected_ids"] else []
        except (json.JSONDecodeError, TypeError):
            raw_ids = []
        if not isinstance(raw_ids, list):
            raw_ids = []

        ids: list[int] = []
        seen: set[int] = set()
        for value in raw_ids:
            try:
                txn_id = int(value)
            except (TypeError, ValueError):
                continue
            if txn_id not in seen:
                seen.add(txn_id)
                ids.append(txn_id)
        total = len(ids)

        transactions: list[dict] = []
        if ids:
            # One query per correction, not one per id. Chunked only because
            # SQLite caps bound parameters and a bulk correction can affect
            # thousands of rows; the ordering/limit is applied after the merge.
            chunk_size = 900
            rows: list[dict] = []
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start:start + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                cursor = await db.execute(
                    f"""SELECT id, posted_date, merchant, description, amount_cents,
                               account_id, category_path
                        FROM transactions WHERE id IN ({placeholders})
                        ORDER BY posted_date DESC, id DESC
                        LIMIT ?""",
                    (*chunk, limit),
                )
                rows.extend(dict(r) for r in await cursor.fetchall())
            rows.sort(key=lambda r: (r["posted_date"] or "", r["id"]), reverse=True)
            transactions = rows[:limit]

        return {
            "id": correction_id,
            "total": total,
            "returned": len(transactions),
            "truncated": total > len(transactions),
            "transactions": transactions,
        }
    finally:
        await db.close()


@router.post("/api/chat-correct", response_model=ChatCorrectResponse)
async def chat_correct(req: ChatCorrectRequestExt):
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="Message is required")

    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM transactions")
        count_row = await cursor.fetchone()
        txn_count = count_row["cnt"] if count_row else 0
        if txn_count == 0:
            raise HTTPException(status_code=400, detail="No transactions to correct. Import data first.")

        cursor = await db.execute(
            "SELECT DISTINCT merchant FROM transactions WHERE merchant != '' ORDER BY merchant"
        )
        merchants = [r["merchant"] for r in await cursor.fetchall()]

        # From the categories TABLE, not DISTINCT category_path on transactions:
        # a category with zero transactions is still one the user can file things
        # under, so the model must be offered it. Hierarchical leftovers are
        # filtered out — offering them back would re-teach the model the old
        # shape. That means paths containing " > ", the legacy family roots
        # `Income` / `Expense` (never flat categories; `Transfer` genuinely is
        # one), and any row that is still someone's parent.
        cursor = await db.execute(
            """SELECT DISTINCT path FROM categories c
               WHERE path IS NOT NULL AND path != ''
                 AND path NOT LIKE '% > %'
                 AND path NOT IN ('Income', 'Expense')
                 AND NOT EXISTS (SELECT 1 FROM categories k WHERE k.parent_id = c.id)
               ORDER BY path"""
        )
        categories = [r["path"] for r in await cursor.fetchall()]

        cursor = await db.execute(
            "SELECT account_id, source_platform, COUNT(*) as cnt FROM transactions GROUP BY account_id ORDER BY cnt DESC"
        )
        accounts = [dict(r) for r in await cursor.fetchall()]

        cursor = await db.execute(
            "SELECT MIN(posted_date) as min_d, MAX(posted_date) as max_d FROM transactions"
        )
        dr = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT SUM(CASE WHEN amount_cents > 0 THEN amount_cents ELSE 0 END) as inc, "
            "SUM(CASE WHEN amount_cents < 0 THEN ABS(amount_cents) ELSE 0 END) as exp FROM transactions"
        )
        fin = await cursor.fetchone()
        txn_stats = {
            "count": txn_count,
            "min_date": dr["min_d"] if dr else None,
            "max_date": dr["max_d"] if dr else None,
            "income": fin["inc"] if fin else 0,
            "expense": fin["exp"] if fin else 0,
        }

        prompt = _build_chat_prompt(req.message, merchants, categories, accounts, txn_stats)
        response = await asyncio.to_thread(call_llm, prompt, strong=False)

        if not response:
            raise HTTPException(status_code=500, detail="LLM returned empty response")

        try:
            result = json.loads(response)
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(status_code=500, detail="LLM returned invalid JSON")

        actions = result.get("actions", [])
        summary = result.get("summary", "No changes made.")
        if not isinstance(actions, list):
            actions = [actions]

        affected_ids = []
        context_diff_parts = []

        for action in actions:
            action_type = action.get("type", "")
            params = action.get("params", {})

            if action_type == "tag":
                search = params.get("search", "").lower()
                tags = params.get("tags", [])
                if search and tags:
                    cursor = await db.execute(
                        "SELECT id, tags FROM transactions WHERE LOWER(merchant) LIKE ? OR LOWER(description) LIKE ?",
                        (f"%{search}%", f"%{search}%")
                    )
                    rows = await cursor.fetchall()
                    for row in rows:
                        existing_tags = json.loads(row["tags"]) if row["tags"] and row["tags"] != "[]" else []
                        new_tags = list(set(existing_tags + tags))
                        await db.execute(
                            "UPDATE transactions SET tags=? WHERE id=?",
                            (json.dumps(new_tags), row["id"])
                        )
                        if row["id"] not in affected_ids:
                            affected_ids.append(row["id"])
                    context_diff_parts.append(f"Tagged {len(rows)} transactions with {tags}")

            elif action_type == "recategorize":
                search = params.get("search", "").lower()
                category_path, coercion = _coerce_flat_category(params.get("category_path", ""))
                if coercion:
                    context_diff_parts.append(coercion)
                if search and category_path:
                    # Create the flat category if it's new, so the LLM can route
                    # transactions to a brand-new bucket in a single action — unless
                    # the user DELETED that name, in which case the whole action is
                    # abandoned. Writing the name onto transactions anyway would put
                    # the deleted category straight back into every breakdown.
                    try:
                        await ensure_flat_category(db, category_path)
                        context_diff_parts.append(
                            f"Ensured category exists: {category_path}"
                        )
                    except CategoryDeleted as exc:
                        logger.info("Refusing chat recategorize into deleted category: %s", exc)
                        context_diff_parts.append(
                            f"Skipped: “{category_path}” was deleted. Add it back on the "
                            f"Expenses or Income tab first if you want to use it again."
                        )
                        continue
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning(
                            "ensure_flat_category failed for %r: %s",
                            category_path, exc,
                        )
                    cursor = await db.execute(
                        "SELECT id, amount_cents, business_context FROM transactions "
                        "WHERE LOWER(merchant) LIKE ? OR LOWER(description) LIKE ?",
                        (f"%{search}%", f"%{search}%")
                    )
                    rows = await cursor.fetchall()
                    # A split parent is already counted through its item rows;
                    # moving it onto a spending category counts the same charge
                    # twice. Resolved in ONE query, not one per matched row —
                    # a chat correction routinely matches thousands.
                    protected = (
                        set() if category_neutralises_split(category_path)
                        else await split_parent_ids(db)
                    )
                    skipped = 0
                    split_skipped = 0
                    autofixed = 0
                    for row in rows:
                        if row["id"] in protected:
                            split_skipped += 1
                            continue
                        amt = int(row["amount_cents"] or 0)
                        row_cp = category_path
                        # Per row: income vs expense follows THIS row's sign.
                        row_et = determine_entity_type(category_path, amt)
                        row_bc = row["business_context"] or "personal"
                        row_reason = f"Chat-correct: {req.message[:120]}"
                        if violates_sign_invariant(row_et, amt):
                            new_cp, new_et, new_bc, note = coerce_sign_safe(
                                row_cp, row_et, amt, row_bc
                            )
                            log_violation_autofix(
                                row["id"],
                                {"category_path": row_cp, "entity_type": row_et,
                                 "business_context": row_bc, "amount_cents": amt},
                                {"category_path": new_cp, "entity_type": new_et,
                                 "business_context": new_bc},
                                note or "chat-correct sign-invariant autofix",
                            )
                            # If the autofix lands on Uncategorized we skip the
                            # update entirely so a wrong bulk request doesn't
                            # destroy good existing data.
                            if new_cp == "Uncategorized":
                                skipped += 1
                                continue
                            row_cp, row_et, row_bc = new_cp, new_et, new_bc
                            row_reason = note or row_reason
                            autofixed += 1
                        await db.execute(
                            """UPDATE transactions
                               SET category_path=?, entity_type=?, business_context=?,
                                   confidence_score=?, categorization_reason=?
                               WHERE id=?""",
                            (row_cp, row_et, row_bc, 1.0, row_reason, row["id"])
                        )
                        if row["id"] not in affected_ids:
                            affected_ids.append(row["id"])
                    applied = len(rows) - skipped - split_skipped
                    msg = f"Re-categorized {applied} transactions to {category_path}"
                    if autofixed:
                        msg += f" ({autofixed} sign-invariant autofixes)"
                    if skipped:
                        msg += f" ({skipped} rows skipped: would violate sign invariant)"
                    if split_skipped:
                        msg += (
                            f" ({split_skipped} rows skipped: split into item rows, "
                            f"which carry the real categories — recategorizing the "
                            f"combined charge would count it twice)"
                        )
                    context_diff_parts.append(msg)

                    # The rule is what makes future imports auto-categorize, but
                    # the same correction sent twice must not stack up duplicates.
                    existing_rule_id = await find_equivalent_rule(db, search, category_path)
                    if existing_rule_id is not None:
                        context_diff_parts.append(
                            f"Reused existing rule #{existing_rule_id} for '{search}' → {category_path}"
                        )
                    else:
                        rule_id = await add_rule(db, search, category_path,
                                                 f"Chat correction: {req.message[:200]}")
                        context_diff_parts.append(
                            f"Created rule #{rule_id} for '{search}' → {category_path}"
                        )

            elif action_type == "add_rule":
                pattern = params.get("pattern", "")
                category_path, coercion = _coerce_flat_category(params.get("category_path", ""))
                description = params.get("description", "")
                if coercion:
                    context_diff_parts.append(coercion)
                if pattern and category_path:
                    try:
                        await ensure_flat_category(db, category_path)
                    except CategoryDeleted as exc:
                        # A rule into a deleted category is a resurrection machine:
                        # it would re-stamp the name on every future import.
                        logger.info("Refusing rule into deleted category: %s", exc)
                        context_diff_parts.append(
                            f"Skipped rule: “{category_path}” was deleted. Add it back "
                            f"first if you want new transactions filed there."
                        )
                        continue
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning(
                            "ensure_flat_category failed for %r: %s",
                            category_path, exc,
                        )
                    existing_rule_id = await find_equivalent_rule(db, pattern, category_path)
                    if existing_rule_id is not None:
                        context_diff_parts.append(
                            f"Reused existing rule #{existing_rule_id}: {description}"
                        )
                    else:
                        rule_id = await add_rule(db, pattern, category_path, description)
                        context_diff_parts.append(f"Created rule #{rule_id}: {description}")

            elif action_type == "create_category":
                category_path, coercion = _coerce_flat_category(params.get("category_path", ""))
                if coercion:
                    context_diff_parts.append(coercion)
                if category_path:
                    try:
                        # `create_category` IS the explicit request, so it is the one
                        # action allowed to bring a deleted category back — the same
                        # licence POST /api/categories has. Deletion is a decision,
                        # not a permanent ban.
                        leaf_id = await ensure_flat_category(
                            db, category_path, allow_revive=True
                        )
                        context_diff_parts.append(
                            f"Ensured category exists: {category_path} (id={leaf_id})"
                        )
                    except Exception as exc:
                        logger.warning(
                            "create_category failed for %r: %s",
                            category_path, exc,
                        )
                        context_diff_parts.append(
                            f"create_category failed: {category_path} ({exc})"
                        )

            elif action_type == "update_context":
                fact = params.get("fact", "")
                if fact:
                    appended = append_context_fact(f"- {fact}")
                    if appended:
                        context_diff_parts.append(f"Context updated: {fact}")

        await db.commit()

        context_diff = "\n".join(context_diff_parts) if context_diff_parts else None

        # Persist the chat correction into the history table.
        # Best-effort: never fail the user's request if logging breaks.
        try:
            await db.execute(
                "INSERT INTO chat_corrections "
                "(prompt, response, affected_count, affected_ids, context_diff, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    req.message,
                    summary,
                    len(affected_ids),
                    json.dumps(affected_ids),
                    context_diff,
                    req.source or "chat-page",
                ),
            )
            await db.commit()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("failed to persist chat correction history: %s", exc)

        return ChatCorrectResponse(
            affected_count=len(affected_ids),
            affected_ids=affected_ids,
            summary=summary,
            context_diff=context_diff,
        )
    finally:
        await db.close()
