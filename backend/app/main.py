import asyncio
import logging
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from app.database import init_db, seed_categories, backfill_explainability
from app.routers import health, import_routes, transactions, accounts, context, transfers, categorize, rules, categories, mortgage, paystubs, budgets, compare, net_worth, trends, anomalies, sankey, chat_correct, amazon, plaid_routes
# Own import line on purpose: several agents are editing the shared one above, and
# a one-name addition there is a guaranteed merge conflict for no benefit.
from app.routers import merchants
from app.services.import_state import import_state

logger = logging.getLogger(__name__)
BUDGET_CONTEXT_PATH = os.environ.get("BUDGET_CONTEXT_PATH", "/data/budget_context.md")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_categories()
    # Flat category defaults (idempotent). The hierarchical seed above stays for
    # older databases; migrate_to_flat removes those rows when it runs.
    try:
        from app.database import get_db
        from app.services.flat_categories import seed_flat_categories

        db = await get_db()
        try:
            await seed_flat_categories(db)
        finally:
            await db.close()
    except Exception as exc:
        logger.warning("Flat category seed failed: %s", exc)

    # Account identity. Seeding only fills gaps — it derives a name and kind for
    # accounts that have transactions but no `account_meta` row, and never
    # overwrites one. ACCOUNT_META_JSON is applied after, so an explicit file of
    # labels (which is where `real_estate` has to come from) outranks the guess.
    try:
        from app.database import get_db
        from app.services.account_meta import apply_env_override, seed_account_meta

        db = await get_db()
        try:
            await seed_account_meta(db)
            await apply_env_override(db)
        finally:
            await db.close()
    except Exception as exc:
        logger.warning("Account meta seed failed: %s", exc)

    try:
        await backfill_explainability()
    except Exception:
        pass
    from app.routers.context import _infer_context
    try:
        await _infer_context()
    except Exception:
        pass

    # Auto-import samples if DB is empty (skip in test mode).
    # NOTE: re-categorize-on-boot was REMOVED. It used to fire any time the
    # DB had uncategorized rows, kicking off an N-row LLM loop on every
    # container restart. That loop modified rows non-atomically and once
    # destroyed ~1,566 rows when interrupted mid-flight. Categorization is
    # now only triggered by import (`POST /api/import/samples`) or by an
    # explicit user request (`POST /api/categorize` with `limit` or
    # `transaction_ids`).
    if not os.environ.get("SKIP_AUTO_IMPORT"):
        from app.database import get_db
        db = await get_db()
        try:
            cursor = await db.execute("SELECT COUNT(*) FROM transactions")
            row = await cursor.fetchone()
            txn_count = row[0] if row else 0
            if txn_count == 0:
                logger.info("Transactions table empty — auto-importing samples/ in background")
                import_state.state = "importing"
                import_state.message = "Auto-import started on startup"
                asyncio.create_task(import_routes.import_samples())
        finally:
            await db.close()

    # Periodic Plaid sync — what makes this a live app rather than a CSV viewer.
    # In-process so it needs no host cron; PLAID_SYNC_INTERVAL_MIN=0 disables it.
    sync_task = None
    interval_min = int(os.environ.get("PLAID_SYNC_INTERVAL_MIN", "360") or 0)
    if interval_min > 0 and not os.environ.get("SKIP_AUTO_IMPORT"):
        async def _plaid_sync_loop():
            from app.database import get_db
            from app.services import plaid_client, plaid_sync

            while True:
                # Sleep first: never fire a sync during boot, when the DB may
                # still be migrating or importing.
                await asyncio.sleep(interval_min * 60)
                if not plaid_client.is_configured():
                    continue
                db = await get_db()
                try:
                    cursor = await db.execute("SELECT COUNT(*) FROM plaid_items")
                    row = await cursor.fetchone()
                    if not (row and row[0]):
                        continue  # nothing linked yet
                    result = await plaid_sync.sync_all(db)
                    logger.info("Scheduled Plaid sync: %s",
                                {k: v for k, v in result.items() if k != "new_ids"})
                except Exception as exc:
                    logger.warning("Scheduled Plaid sync failed: %s", exc)
                finally:
                    await db.close()

        sync_task = asyncio.create_task(_plaid_sync_loop())
        logger.info("Plaid periodic sync enabled every %d minutes", interval_min)

    yield

    if sync_task:
        sync_task.cancel()

app = FastAPI(title="Budget App API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(import_routes.router)
app.include_router(transactions.router)
app.include_router(accounts.router)
app.include_router(context.router)
app.include_router(transfers.router)
app.include_router(categorize.router)
app.include_router(rules.router)
app.include_router(categories.router)
app.include_router(mortgage.router)
app.include_router(paystubs.router)
app.include_router(budgets.router)
app.include_router(compare.router)
app.include_router(net_worth.router)
app.include_router(trends.router)
app.include_router(anomalies.router)
app.include_router(sankey.router)
app.include_router(chat_correct.router)
app.include_router(amazon.router)
app.include_router(plaid_routes.router)
# Spending by vendor. Appended rather than slotted in, same merge-conflict reason
# as the import above.
app.include_router(merchants.router)
