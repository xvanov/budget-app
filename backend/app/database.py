import aiosqlite
import logging
import os

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/budget.db")

SEED_CATEGORIES = [
    "Income > Software Job",
    "Income > Rentals",
    "Income > Contracting Business",
    "Income > Investment / Interest",
    "Income > Refunds",
    "Income > Other Income",
    "Expense > Housing > Rent",
    "Expense > Housing > Mortgage Interest",
    "Expense > Housing > Mortgage Taxes",
    "Expense > Housing > Mortgage Insurance",
    "Expense > Housing > Utilities",
    "Expense > Housing > Maintenance",
    "Expense > Housing > Other",
    "Expense > Rental > Utilities",
    "Expense > Rental > Maintenance",
    "Expense > Rental > Property Tax",
    "Expense > Rental > Insurance",
    "Expense > Rental > Management Fees",
    "Expense > Rental > Other",
    "Expense > Personal > Groceries",
    "Expense > Personal > Dining",
    "Expense > Personal > Healthcare",
    "Expense > Personal > Subscriptions",
    "Expense > Personal > Shopping",
    "Expense > Personal > Other",
    "Expense > Leisure > Travel",
    "Expense > Leisure > Entertainment",
    "Expense > Leisure > Dining Out",
    "Expense > Leisure > Groceries",
    "Expense > Business > Contracting Costs",
    "Expense > Business > Software / Tools",
    "Expense > Business > Other",
    "Expense > Fees & Interest",
    "Expense > Other",
    "Transfer > Account-to-account",
    "Transfer > Credit-card Payment",
    "Transfer > Mortgage Principal",
    "Transfer > Mortgage Escrow",
    "Transfer > Cash Out",
]

async def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db

async def init_db():
    db = await get_db()
    try:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT,
                source_platform TEXT NOT NULL,
                source_file TEXT NOT NULL,
                account_id TEXT NOT NULL,
                posted_date TEXT NOT NULL,
                transaction_date TEXT,
                amount_cents INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USD',
                merchant TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                normalized_description TEXT NOT NULL DEFAULT '',
                category_path TEXT,
                entity_type TEXT DEFAULT 'uncategorized',
                asset_class TEXT,
                business_context TEXT DEFAULT 'personal',
                linked_transaction_id INTEGER,
                confidence_score REAL,
                categorization_reason TEXT,
                applied_rule_id INTEGER,
                tags TEXT DEFAULT '[]',
                notes TEXT,
                flagged INTEGER DEFAULT 0,
                flag_reason TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_transaction_id INTEGER NOT NULL,
                to_transaction_id INTEGER NOT NULL,
                matched_by TEXT NOT NULL,
                FOREIGN KEY (from_transaction_id) REFERENCES transactions(id),
                FOREIGN KEY (to_transaction_id) REFERENCES transactions(id)
            );

            CREATE TABLE IF NOT EXISTS amortization (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month TEXT NOT NULL,
                principal_cents INTEGER NOT NULL,
                interest_cents INTEGER NOT NULL,
                ending_balance_cents INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                category_path TEXT NOT NULL,
                description TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- Merchants the owner has identified as fraudulent. These used to be a
            -- literal list in services/anomaly_detector.py, which put "who
            -- defrauded this particular person" into shared source code — private,
            -- and useless to anyone else. A fresh install has none, which is the
            -- correct default: an app should not accuse a merchant it was never
            -- told about.
            CREATE TABLE IF NOT EXISTS fraud_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL UNIQUE,
                note TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- Vendors the owner has settled up with, so the reconciliation
            -- survives. `refund_pairs.detect_and_store` CLEARS every pair in scope
            -- before re-marking, which would silently undo a merchant
            -- reconciliation the next time refund detection ran; recording the
            -- intent lets detection re-apply it instead of destroying it.
            CREATE TABLE IF NOT EXISTS refund_reconciliations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pattern TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'plaid',
                note TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(pattern, scope)
            );

            -- Account identity. The display name and `kind` of an account used
            -- to be a dict in routers/net_worth.py, which put real account
            -- numbers and private labels into the application. They are data:
            -- `kind` drives the net-worth arithmetic (a `credit` balance below
            -- zero is a liability; `real_estate` carries a separate debt leg),
            -- and it cannot be inferred for real estate, so it has to be
            -- storable. Rows are seeded from whatever accounts `transactions`
            -- already contains — see services/account_meta.py.
            CREATE TABLE IF NOT EXISTS account_meta (
                account_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'checking',
                institution TEXT,
                include_in_net_worth INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS import_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                platform TEXT,
                account_id TEXT,
                imported INTEGER DEFAULT 0,
                duplicates INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0,
                warnings TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                parent_id INTEGER,
                path TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                FOREIGN KEY (parent_id) REFERENCES categories(id)
            );

            -- Categories the owner has DELETED. A tombstone, not a soft delete:
            -- the `categories` row really goes away (its transactions are
            -- reassigned first), and this table only records the decision.
            --
            -- It exists because `seed_flat_categories` re-INSERTs every name in
            -- DEFAULT_CATEGORIES on every boot. "Idempotent with respect to the
            -- defaults" is exactly what resurrected the owner's deletions: Travel
            -- and Housing came back on the next restart, twice. Every path that
            -- creates a category behind the user's back — the seed, the chat
            -- `ensure_flat_category`, an import rule, the categorizer — consults
            -- this table, so a deletion survives a restart.
            --
            -- A soft-delete column on `categories` was the alternative and was
            -- rejected: every read (`SELECT * FROM categories`, the tree builder,
            -- /colors, the duplicate-name check, reassign_to validation, and each
            -- consumer that joins on `path`) would need a new filter, and a single
            -- missed site shows a category the user deleted. A separate table is
            -- additive, empty by default, and consulted only where a row is
            -- CREATED. It also records the intent for a default that was never
            -- seeded in this database, which no column on a missing row can.
            --
            -- NOCASE so "travel" and "Travel" are the same decision, matching the
            -- case-insensitive duplicate check in POST /api/categories.
            CREATE TABLE IF NOT EXISTS deleted_categories (
                name TEXT PRIMARY KEY COLLATE NOCASE,
                deleted_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_path TEXT NOT NULL,
                budget_type TEXT NOT NULL,
                amount_cents INTEGER,
                percent REAL,
                period TEXT NOT NULL DEFAULT 'month',
                label TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS chat_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                prompt TEXT NOT NULL,
                response TEXT NOT NULL,
                affected_count INTEGER NOT NULL DEFAULT 0,
                affected_ids TEXT NOT NULL DEFAULT '[]',
                context_diff TEXT,
                source TEXT NOT NULL DEFAULT 'chat-page'
            );

            CREATE INDEX IF NOT EXISTS idx_chat_corrections_created ON chat_corrections(created_at DESC);

            CREATE TABLE IF NOT EXISTS paystubs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_file TEXT NOT NULL,
                employer TEXT NOT NULL,
                employee_name TEXT,
                period_start TEXT,
                period_end TEXT,
                pay_date TEXT,
                gross_cents INTEGER NOT NULL,
                net_cents INTEGER NOT NULL,
                federal_tax_cents INTEGER DEFAULT 0,
                medicare_cents INTEGER DEFAULT 0,
                social_security_cents INTEGER DEFAULT 0,
                state_tax_cents INTEGER DEFAULT 0,
                retirement_cents INTEGER DEFAULT 0,
                insurance_cents INTEGER DEFAULT 0,
                hsa_cents INTEGER DEFAULT 0,
                other_deductions_cents INTEGER DEFAULT 0,
                linked_transaction_id INTEGER,
                created_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (linked_transaction_id) REFERENCES transactions(id)
            );

            -- ── Plaid ────────────────────────────────────────────────────────
            -- access_token_encrypted is AES-256-GCM ciphertext from
            -- app/services/crypto.py. NEVER store a plaintext Plaid token.
            CREATE TABLE IF NOT EXISTS plaid_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id TEXT NOT NULL UNIQUE,
                access_token_encrypted TEXT NOT NULL,
                institution_id TEXT,
                institution_name TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                cursor TEXT,
                last_synced_at TEXT,
                error_code TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS plaid_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_db_id INTEGER NOT NULL,
                plaid_account_id TEXT NOT NULL UNIQUE,
                name TEXT,
                official_name TEXT,
                mask TEXT,
                type TEXT,
                subtype TEXT,
                canonical_account_id TEXT,
                include_in_sync INTEGER NOT NULL DEFAULT 1,
                current_balance_cents INTEGER,
                available_balance_cents INTEGER,
                last_balance_at TEXT,
                FOREIGN KEY (item_db_id) REFERENCES plaid_items(id)
            );

            -- Change log for the BonusPilot integration. Every Plaid row that is
            -- added, modified or removed by a sync gets one entry here, numbered
            -- by seq. A consumer keeps the last seq it saw per item and asks for
            -- everything after it. The entry stores only the external_id; the
            -- current row is read when the change is served, and a missing row
            -- is reported as removed.
            CREATE TABLE IF NOT EXISTS integration_changes (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                item_db_id INTEGER NOT NULL,
                external_id TEXT NOT NULL,
                op TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_integration_changes_item
                ON integration_changes(item_db_id, seq);

            -- ── Amazon ───────────────────────────────────────────────────────
            -- owner exists because the Amazon account is shared and each
            -- person's export arrives separately; the export carries no
            -- reliable buyer field, so ownership must come from ingest.
            CREATE TABLE IF NOT EXISTS amazon_orders (
                order_id TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT 'kalin',
                order_date TEXT,
                order_status TEXT,
                payment_method TEXT,
                card_last4 TEXT,
                used_gift_card INTEGER DEFAULT 0,
                recipient TEXT,
                website TEXT,
                total_cents INTEGER,
                source_file TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (order_id, owner)
            );

            CREATE TABLE IF NOT EXISTS amazon_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT 'kalin',
                asin TEXT,
                product_name TEXT NOT NULL DEFAULT '',
                quantity INTEGER DEFAULT 1,
                unit_price_cents INTEGER,
                unit_price_tax_cents INTEGER,
                shipping_charge_cents INTEGER,
                discount_cents INTEGER,
                total_cents INTEGER,
                ship_date TEXT,
                order_date TEXT,
                shipment_status TEXT,
                order_status TEXT,
                is_cancelled INTEGER DEFAULT 0,
                is_digital INTEGER DEFAULT 0,
                card_last4 TEXT,
                used_gift_card INTEGER DEFAULT 0,
                recipient TEXT,
                match_strategy TEXT,
                matched_transaction_id INTEGER,
                split_transaction_id INTEGER,
                source_file TEXT,
                fingerprint TEXT UNIQUE,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS amazon_refunds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                owner TEXT NOT NULL DEFAULT 'kalin',
                refund_date TEXT,
                amount_cents INTEGER NOT NULL,
                quantity INTEGER DEFAULT 1,
                reason TEXT,
                status TEXT,
                disbursement_type TEXT,
                matched_transaction_id INTEGER,
                source_file TEXT,
                fingerprint TEXT NOT NULL UNIQUE,
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- ── Anomaly review queue ─────────────────────────────────────────
            -- The detector emits one row PER TRANSACTION, which produced ~7,000
            -- items across ~3,600 transactions — noise, not a queue. These tables
            -- collapse them into one reviewable issue per merchant/recurring
            -- problem, track how much has already been refunded, and carry a
            -- resolution status so an issue can be closed or turned into a task.
            CREATE TABLE IF NOT EXISTS anomaly_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,
                type TEXT,
                severity TEXT NOT NULL DEFAULT 'low',
                title TEXT NOT NULL,
                detail TEXT,
                merchant TEXT,
                category_path TEXT,
                transaction_count INTEGER NOT NULL DEFAULT 0,
                total_cents INTEGER NOT NULL DEFAULT 0,
                refunded_cents INTEGER NOT NULL DEFAULT 0,
                refund_status TEXT NOT NULL DEFAULT 'none',
                suggested_action TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                resolution_note TEXT,
                first_date TEXT,
                last_date TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS anomaly_group_transactions (
                group_id INTEGER NOT NULL,
                transaction_id INTEGER NOT NULL,
                role TEXT NOT NULL DEFAULT 'charge',
                PRIMARY KEY (group_id, transaction_id, role),
                FOREIGN KEY (group_id) REFERENCES anomaly_groups(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_anomaly_groups_status ON anomaly_groups(status, severity);
            CREATE INDEX IF NOT EXISTS idx_anomaly_group_txns ON anomaly_group_transactions(group_id);

            CREATE INDEX IF NOT EXISTS idx_amazon_items_order ON amazon_items(order_id, owner);
            CREATE INDEX IF NOT EXISTS idx_amazon_items_matched ON amazon_items(matched_transaction_id);
            CREATE INDEX IF NOT EXISTS idx_amazon_refunds_matched ON amazon_refunds(matched_transaction_id);
            CREATE INDEX IF NOT EXISTS idx_plaid_accounts_item ON plaid_accounts(item_db_id);

            CREATE INDEX IF NOT EXISTS idx_tx_account ON transactions(account_id);
            CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(posted_date);
            CREATE INDEX IF NOT EXISTS idx_tx_entity ON transactions(entity_type);
            CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category_path);
            -- Split / pair links. Every "what are this row's children" and "which
            -- combined charge is this row an item of" question joins the table to
            -- itself on this column, and the transactions list asks it per row. On
            -- the live DB (11k rows) a page of 50 took 128 ms without this index and
            -- 9 ms with it; a 1,000-row page went from 2.1 s to 53 ms.
            CREATE INDEX IF NOT EXISTS idx_tx_linked ON transactions(linked_transaction_id);
        """)
        await db.commit()

        # Migrations for schema changes across versions
        # Add normalized_description column if missing
        try:
            await db.execute("ALTER TABLE transactions ADD COLUMN normalized_description TEXT NOT NULL DEFAULT ''")
            await db.commit()
        except Exception:
            pass  # Column already exists

        # Plaid columns on transactions. `pending` distinguishes an authorised
        # charge from a posted one; `plaid_account_id` keeps the provider's own
        # account key so a re-mapped canonical account_id stays traceable.
        for column, ddl in (
            ("pending", "ALTER TABLE transactions ADD COLUMN pending INTEGER DEFAULT 0"),
            ("plaid_account_id", "ALTER TABLE transactions ADD COLUMN plaid_account_id TEXT"),
            # Refund pairing: a charge and the credit that reversed it point at
            # each other, so the Refunded tab can show the pair and the
            # Transactions tab can hide both halves of a wash.
            ("refund_match_id", "ALTER TABLE transactions ADD COLUMN refund_match_id INTEGER"),
            ("refund_role", "ALTER TABLE transactions ADD COLUMN refund_role TEXT"),
            ("refund_partial", "ALTER TABLE transactions ADD COLUMN refund_partial INTEGER DEFAULT 0"),
            # Plaid fields the BonusPilot integration needs. Plaid's personal
            # finance category is what tells a paycheck from a transfer, so it
            # must be kept, not discarded after the sync.
            ("plaid_pfc_primary", "ALTER TABLE transactions ADD COLUMN plaid_pfc_primary TEXT"),
            ("plaid_pfc_detailed", "ALTER TABLE transactions ADD COLUMN plaid_pfc_detailed TEXT"),
            ("payment_channel", "ALTER TABLE transactions ADD COLUMN payment_channel TEXT"),
            ("counterparty_name", "ALTER TABLE transactions ADD COLUMN counterparty_name TEXT"),
            ("pending_external_id", "ALTER TABLE transactions ADD COLUMN pending_external_id TEXT"),
        ):
            try:
                await db.execute(ddl)
                await db.commit()
            except Exception:
                pass  # column already exists

        # Reporting totals attribute a refund to its charge's period, which asks
        # "which credits point at this charge?" once per paired charge row (see
        # services/scope.py::effective_amount_sql). Without this index each of
        # those lookups scans the whole table: measured on the live DB (10.4k rows,
        # 222 paired) the monthly aggregation took 297 ms instead of ~5 ms.
        try:
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_tx_refund_match "
                "ON transactions(refund_match_id) WHERE refund_match_id IS NOT NULL"
            )
            await db.commit()
        except Exception as exc:
            logger.warning("Could not create refund_match_id index: %s", exc)

        # Item-level refund attribution. Amazon's refund exports carry no ASIN and
        # no product name, so which item a refund reverses is INFERRED from the
        # amount inside its order (see amazon_matcher.attribute_refund).
        # `matched_item_id` is the amazon_items row it reverses — the item that
        # supplies both the category the refund should reduce and the recipient it
        # was bought for. `match_strategy` records how it was pinned
        # ('exact_item' | 'near_item' | 'whole_order' | 'ambiguous' |
        # 'unattributed') so a weak inference stays distinguishable from a certain
        # one instead of both looking equally authoritative.
        for column, ddl in (
            ("matched_item_id", "ALTER TABLE amazon_refunds ADD COLUMN matched_item_id INTEGER"),
            ("match_strategy", "ALTER TABLE amazon_refunds ADD COLUMN match_strategy TEXT"),
        ):
            try:
                await db.execute(ddl)
                await db.commit()
            except Exception:
                pass  # column already exists

        # account_meta gained its optional columns after the first version that
        # shipped the table; additive so an existing database keeps its rows.
        for column, ddl in (
            ("institution", "ALTER TABLE account_meta ADD COLUMN institution TEXT"),
            ("include_in_net_worth",
             "ALTER TABLE account_meta ADD COLUMN include_in_net_worth INTEGER NOT NULL DEFAULT 1"),
            ("sort_order", "ALTER TABLE account_meta ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0"),
        ):
            try:
                await db.execute(ddl)
                await db.commit()
            except Exception:
                pass  # column already exists

        # Per-category colour, used identically in charts and lists.
        try:
            await db.execute("ALTER TABLE categories ADD COLUMN color TEXT")
            await db.commit()
        except Exception:
            pass

        # external_id is Plaid's transaction_id. UNIQUE (partial, so the many
        # CSV rows with NULL are unaffected) is what makes the sync upsert
        # idempotent and stops one Plaid transaction becoming two rows.
        try:
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_external_id "
                "ON transactions(external_id) WHERE external_id IS NOT NULL"
            )
            await db.commit()
        except Exception as exc:
            logger.warning("Could not create unique external_id index: %s", exc)

        # Recreate dedup index on correct column (handle old merchant-based index)
        try:
            await db.execute("DROP INDEX IF EXISTS idx_tx_dedup")
        except Exception:
            pass
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tx_dedup ON transactions(account_id, posted_date, amount_cents, normalized_description)")
        await db.commit()
    finally:
        await db.close()

async def backfill_explainability():
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, source_platform, source_file, description, category_path "
            "FROM transactions "
            "WHERE confidence_score IS NULL OR categorization_reason IS NULL OR categorization_reason = ''"
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0
        updated = 0
        for r in rows:
            if r["source_platform"] == "paystub":
                desc = r["description"] or ""
                reason = (
                    f"Paystub component derived from {r['source_file']} — "
                    f"deterministic mapping ({desc})."
                )
                confidence = 1.0
            else:
                reason = (
                    "Backfilled — original categorization predates the explainability story."
                )
                confidence = 0.5
            await db.execute(
                "UPDATE transactions SET confidence_score=?, categorization_reason=? WHERE id=?",
                (confidence, reason, r["id"]),
            )
            updated += 1
        await db.commit()
        return updated
    finally:
        await db.close()


async def seed_categories():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM categories")
        row = await cursor.fetchone()
        if row[0] > 0:
            return

        node_cache = {}
        for path_str in SEED_CATEGORIES:
            parts = path_str.split(" > ")
            current_path = ""
            parent_id = None
            for i, part in enumerate(parts):
                if current_path:
                    current_path = f"{current_path} > {part}"
                else:
                    current_path = part
                key = current_path.lower()
                if key not in node_cache:
                    cursor = await db.execute(
                        "INSERT INTO categories (name, parent_id, path) VALUES (?, ?, ?)",
                        (part, parent_id, current_path),
                    )
                    new_id = cursor.lastrowid
                    node_cache[key] = new_id
                parent_id = node_cache[key]
        await db.commit()
    finally:
        await db.close()
