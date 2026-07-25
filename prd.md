# Personal Finance, Budgeting, Net-Worth & Financial Intelligence Platform

**Take a year of messy exports from every bank, card, and payment app you've ever used, and turn them into a budget that finally tells the truth.**

A personal financial operating system that consolidates fragmented financial data — banks, payment platforms, credit cards, payroll, rentals, mortgages, cash — into a unified ledger, then layers intelligent budgeting, longitudinal tracking, anomaly detection, and AI-assisted financial reasoning on top.

The core differentiator: **deep financial understanding through transaction reconciliation + contextual AI categorization + adaptive budgeting**, learning continuously from the user as they correct it.

---

## The five steps

The product (and this PRD, and the codebase, and the testing strategy) is organized into five steps. Each step has its own testing approach. Build them in order — Data is the foundation everything else stands on.

| # | Step | What it is | Testing approach |
|---|------|------------|-----------------|
| 1 | **Data** | Ingest, normalize, reconcile, categorize. Build the canonical ledger. | **Self-math tests, LLM-generated, TDD.** The agent writes tests first that verify identity invariants (sum of monthly = annual, parser round-trips, dedup idempotence) and exact-known values. |
| 2 | **Budget** | Allocations (absolute or %-of-income) tracked against actuals, with rollovers and AI suggestions. | **Back-tests with mock budgets** applied to historical data. e.g. 10% Leisure / 20% Personal / 30% Housing — verify the engine buckets historical spending correctly and triggers status flags. |
| 3 | **Tracking** | Longitudinal metrics — revenue, expense, net worth, cash flow, profit — over arbitrary periods, with comparisons. | **Period back-tests.** Sum of monthly = annual; net worth_t+1 = net worth_t + cash flow_t→t+1; period-over-period deltas correct. |
| 4 | **Activity** | Anomaly detection, fraud flagging, suspicious refunds, recurring-charge intelligence, corrections feedback loop. | **Known-anomaly tests** against named transactions in `samples/` (Exampleco Subs subscription = fraudulent; Examplefind = suspicious; specific refunded transactions). |
| 5 | **Extras** | Sankey, transactions UI, deployment, end-to-end polish. | **Playwright E2E for every human flow** (not "click → 200" — actual data populated where expected). Screenshots sent to Claude Opus 4.7 for design judgment against `frontend-design/screenshots/`. |

---

## Non-negotiables

- **No regex / keyword maps for categorization.** Every category decision goes through the LLM at ingest time, with `budget_context.md` + the PRD ground-truth rules in the system prompt.
- **Reuse the existing UI design.** Components, fonts, colors, layouts come from `frontend-design/src/*.jsx` and `frontend-design/src/app.css`. Do not redesign.
- **`budget_context.md` is a living file.** Starts blank, inferred from `samples/` on first run, then continuously updated by every user correction and chat-correct. The LLM's domain context grows as the user uses the app.
- **Internal transfers collapsed.** Both legs excluded from income and expense totals. Credit-card payments, mortgage payments, Cash-Out withdrawals all reconciled to their counterparts (or surfaced when the counterpart account isn't ingested).
- **Re-importing the same file produces zero new rows.** Cross-file dedup via `(account_id, posted_date, signed_amount_cents, normalized_description)`.
- **Mortgage payments split into ALL components** — principal (asset conversion, excluded from expenses), interest (expense), taxes (expense), insurance (expense), escrow (liability holding).
- **Deterministic acceptance is exact** — zero tolerance. **LLM-judgment acceptance** uses Claude Opus 4.7 as judge; passes when zero REJECT verdicts.
- **Every categorization is explainable.** Each transaction exposes the LLM's confidence, reasoning, and the rule(s) that applied — editable by the user.
- **Idempotent imports + audit log.** Re-importing is safe. Every category change is versioned and reversible.
- **The Ralph loop is fully autonomous.** No story, no test, no setup step may require human-in-the-loop intervention. The agent self-attests; the outer supervisor agent reviews and emits new PRDs if needed.
- **No dummy tests.** Tests must exercise real logic against real (or representative) data. `assert True`, smoke checks that always pass, "did the function get called" mocks of mocks — these are slop. Delete them if you find them. A story with only dummy tests is not done.
- **`samples/` and `budget_context.md` are gitignored.** They contain proprietary financial data. Never commit them, never log their contents to stdout in a way that ends up in a public artifact, never include their contents verbatim in commit messages.

## Tech stack (MVP)

- **Backend:** Python + FastAPI + SQLite (in a named Docker volume so data persists across `docker compose down/up`). Post-MVP migration target: PostgreSQL for OLTP, DuckDB or ClickHouse for analytics.
- **Frontend:** React, ported from `frontend-design/src/`. JSX (not TypeScript) and custom CSS (not Tailwind) — matching what the design package provides.
- **Deploy:** `docker compose up -d --build` boots both services on host ports `:8001` (backend → container 8000) and `:8081` (frontend → container 8080). The non-default host ports avoid colliding with other docker stacks the user runs locally; the docker-compose port mapping (`8001:8000`, `8081:8080`) MUST stay intact.
- **LLM (in-app categorizer):** **No API key, no SDK.** Shell out to the host's local `opencode run` (preferred) or `claude -p` (fallback) — same pattern as `ralph.sh`. The backend container bind-mounts host credentials from `~/.claude/` and `~/.config/opencode/` (or `~/.opencode/`) read-only. A single helper (`app/llm.py::call_llm`) wraps the shell-out, handles the opencode→claude fallback (using the credit/quota/auth-signal regex from `ralph.sh`), times out at 60s for routine calls, and returns structured output. `ANTHROPIC_API_KEY` MUST NOT appear in code, `.env`, or `docker-compose.yml`.
- **LLM (judge / strong-judgment steps):** Claude Opus 4.7, invoked via the same helper with `strong=True` (passes `-m opus` / `--model claude-opus-4-7` and bumps the timeout to 180s). Used by the test suite for categorization grading, by the visual-judgment Playwright step, and by any in-app step that needs stronger reasoning.
- **PDF parsing:** `pdftotext -layout` on PATH; `pypdf` as fallback.

## Source files

- `samples/` — real exports: Chase (two credit cards, a checking account, a loan), US Bank checking, BofA-style checking + multi-section `stmt.csv` (rental LLC account — see below), Cash App, PayPal, Venmo HTML, mortgage amortization PDF. Includes the two known fraudulent merchants (Exampleco Subs, Examplefind).
- `samples/paystubs/2026/` — 8 employer paystub PDFs covering 2026-01-18 through 2026-05-09 (bi-weekly). Used for Story 08 (payroll intelligence).
- `frontend-design/` — React mockup. Components in `src/*.jsx` (Dashboard, Sankey, Budgets, Compare, Transactions, NetWorth, Trends, AppShell), design tokens in `src/colors_and_type.css`, base styles in `src/app.css`, sample data shape in `src/data.js`, screenshots in `screenshots/`.
- `budget_context.md` — the living knowledge base. Starts blank; populated by the backend on first run from `samples/`, then continuously updated.

---

## Canonical transaction schema

Every row across every source platform normalizes into this shape. The agent must enforce this schema at the ingestion boundary.

| Field | Type | Description |
|---|---|---|
| `transaction_id` | UUID | Internal primary key |
| `external_id` | string \| null | Source platform's transaction ID (may be missing for some CSVs) |
| `source_platform` | enum | `chase`, `us_bank`, `bofa`, `cash_app`, `paypal`, `venmo`, `manual`, … |
| `source_file` | string | Filename in `samples/` (or `manual` for hand-entered) |
| `account_id` | string | Linked account (e.g. `card-9xxx`, `cash-app`, …) |
| `posted_date` | date | Transaction posting date |
| `transaction_date` | date \| null | Trade date if distinct from post date |
| `amount_cents` | int | Signed (negative = outflow). Always cents to avoid float drift. |
| `currency` | string | ISO 4217. Default `USD`. |
| `merchant` | string | Parsed merchant name |
| `description` | string | Raw description from the source |
| `category_path` | string | Slash-separated leaf path (e.g. `Income > Rentals > Exampletenant`). Hierarchical. |
| `entity_type` | enum | `income`, `expense`, `transfer`, `asset_conversion`, `liability_payment` |
| `asset_class` | enum \| null | `cash`, `real_estate`, `investment`, `mortgage_principal`, `escrow` — used for net-worth math |
| `business_context` | enum | `personal`, `rental`, `business` — used to separate the user's books |
| `linked_transaction_id` | UUID \| null | Pair this row to its other transfer leg (Cash App → Chase deposit, etc.) |
| `confidence_score` | float | LLM's 0–1 confidence in the assigned category |
| `categorization_reason` | string | Human-readable explanation from the LLM |
| `applied_rule_id` | UUID \| null | Which user-rule applied (if any, skips the LLM call) |
| `tags` | string[] | Free-form: `person:exampletenant`, `property:3323-ridge`, `flagged:fraud-candidate`, … |
| `notes` | string \| null | User annotations |
| `flagged` | bool | True if anomaly detection wants user review |
| `flag_reason` | string \| null | Why it's flagged (`unknown-recurring-subscription`, `refunded`, `spike`, `duplicate-candidate`, …) |

## Default category tree

```
Income
├─ Software Job
├─ Rentals
│   └─ per-person / per-property leaves
├─ Contracting Business
├─ Investment / Interest
├─ Refunds
└─ Other Income

Expense
├─ Housing            ← user's primary residence
│   ├─ Rent
│   ├─ Mortgage Interest
│   ├─ Mortgage Taxes
│   ├─ Mortgage Insurance
│   ├─ Utilities
│   ├─ Maintenance
│   └─ Other
├─ Rental             ← operating costs of rental properties owned by the user
│   ├─ Utilities
│   ├─ Maintenance
│   ├─ Property Tax
│   ├─ Insurance
│   ├─ Management Fees
│   └─ Other
├─ Personal
│   ├─ Groceries
│   ├─ Dining
│   ├─ Healthcare
│   ├─ Subscriptions
│   ├─ Shopping
│   └─ Other
├─ Leisure
│   ├─ Travel
│   ├─ Entertainment
│   ├─ Dining Out
│   └─ Groceries (when traveling / foreign)
├─ Business
│   ├─ Contracting Costs
│   ├─ Software / Tools
│   └─ Other
├─ Fees & Interest
└─ Other

Transfer (excluded from income and expense totals)
├─ Account-to-account
├─ Credit-card Payment
├─ Mortgage Principal      ← asset conversion (builds equity)
├─ Mortgage Escrow         ← liability holding
└─ Cash Out                 ← Cash App / Venmo / PayPal → bank
```

## Ground-truth rules for the judge

These rules are input to Claude Opus 4.7 when it evaluates categorization correctness. They also seed the in-app categorizer's system prompt. New rules are added as the user discovers patterns the system gets wrong.

1. **Cash App inbound payments are rental income.** Every transaction in `samples/cash_app_report_*.csv` that is an inbound payment from a person → `Income > Rentals > <Person>`. Cash App `Withdrawal` / "Cash Out" rows remain Transfers.
2. **Recurring Zelle from a specific name → rental income from that tenant.** When the same person name appears in ≥ 2 inbound Zelle payments across the dataset (any channel), all of them → `Income > Rentals > <Person>`. The classic example is R. EXAMPLE.
3. **Foreign-country transactions are Leisure.** Any merchant in a foreign country (foreign currency, country code, or descriptive hint) → `Expense > Leisure > <sub>`: Restaurants → `Dining Out`; Groceries → `Groceries`; Lodging / transport → `Travel`; Entertainment → `Entertainment`.
4. **Subscriptions → `Expense > Personal > Subscriptions`.** Netflix, Spotify, Hulu, HBO/Max, Disney+, Apple One, YouTube Premium, NYT, etc.
5. **Utility companies for rental properties → `Expense > Rental > Utilities`.** Including but not limited to: Spectrum, Duke Energy, Dominion Energy, Durham Water, Graham Utilities, SFPUC, SMUD, American Water, Xfinity. (Utilities for the user's own residence go under `Expense > Housing > Utilities`.)
6. **Exampleco Subs and Examplefind are known fraudulent — AND refunded.** Both merchants appear in the dataset, both were refunded by the user after discovery, both should be flagged as `fraud`. Use fuzzy matching on the merchant string (case-insensitive substring or Levenshtein ≤ 2) — no other merchant in the dataset sounds remotely like either, so false positives are not a concern. These are the canonical test fixtures for the fraud-flagging story.
7. **Refunded transactions are flagged as candidates for review** — except Amazon refunds, which are routine. Other refunds (where a charge was later credited back) should be surfaced.
8. **The Bank of America account (`stmt.csv`, account historical activity in `samples/`) is the rental LLC business account.** All transactions in this account from the last ~year+ are rental-business activity. Default `business_context = rental` for rows from this account; income rows → `Income > Rentals > ...`, expense rows → `Expense > Rental > ...`. The system can deviate only when a row clearly doesn't fit (with the LLM surfacing its reasoning).
9. *(more rules added by the user during PRD iteration or by the system as it learns)*

---

## Testing & evaluation — the pyramid

Testing escalates from foundational to exhaustive. Each per-iteration test pass runs the foundation first; if the foundation fails, higher levels don't run. A story is not done until everything below AND its step-specific tests are green.

```
        ┌────────────────────────────────┐
   5    │ Visual (Opus 4.7 screenshot)   │   slowest, strongest judgment
        ├────────────────────────────────┤
   4    │ Playwright E2E (real flows)    │
        ├────────────────────────────────┤
   3    │ Integration (HTTP + real DB)   │
        ├────────────────────────────────┤
   2    │ Unit (pure logic, fast)        │
        ├────────────────────────────────┤
   1    │ Smoke (docker compose up + /api/health 200) │   foundation
        └────────────────────────────────┘
```

**Per-iteration test workflow** (the agent runs this before flipping any `passes` flag):

```bash
# Level 1 — smoke. If this fails, nothing else matters.
docker compose up -d --build
curl -fsS http://127.0.0.1:8001/api/health
curl -fsS http://127.0.0.1:8081/

# Level 2 — unit (logic the agent wrote, pure functions, no DB/network)
pytest tests/unit/ -q

# Level 3 — integration (real DB, real HTTP, no mocks)
pytest tests/integration/ -q

# Level 4 — e2e (real browser, real flows)
npx playwright test tests/e2e/

# Level 5 — visual (screenshot vs frontend-design/ via Opus 4.7)
python tests/visual/run_opus_judgment.py
```

The agent must NOT skip levels. If a story doesn't have unit tests yet (early stories), levels 1+5 still run.

## Testing & evaluation — per-step strategy

In addition to the pyramid above, each step has step-specific tests calibrated to its concerns.

### Step 1: Data — LLM-generated self-math tests (TDD)

The agent's first task per Data story is to write the tests. The pattern:

1. Read the data formats (parser specs, expected schema, sample inputs).
2. Generate a `tests/test_data_layer.py` (or equivalent) that asserts:
   - **Parser round-trips** — for each format (Chase credit, Chase checking, BofA, US Bank, Cash App, PayPal, Venmo, mortgage PDF), a fixed sample input produces canonical-schema rows with exact field values.
   - **Identity invariants** — `sum(monthly[m]) == annual` for every aggregation; `sum_by_category(...) == total_expense`; `sum_by_account(...) == total`; `dashboard.net == dashboard.income - dashboard.expense`.
   - **Dedup idempotence** — re-importing yields zero new rows; the the credit card superset = exactly 1001 rows.
   - **Reconciliation correctness** — mortgage 5-way split sums to total payment; transfer pairs are symmetric (`A_to_B.amount == -B_from_A.amount`).
   - **Schema completeness** — every row has all non-null required fields after categorize-on-import.
3. Implementation makes the tests pass. The agent does NOT modify tests to make them pass.
4. **Categorization correctness** is graded separately by Opus judge — see below.

### Step 2: Budget — Back-tests with mock budgets

Define a mock budget set, apply to historical samples/ data, assert behavior:

- **Mock budget A**: 10% Leisure, 20% Personal, 30% Housing, 5% Subscriptions, 35% Savings (sum = 100% of income, percent-of-income).
- **Mock budget B**: $500 Dining Out, $200 Subscriptions, $1000 Groceries (absolute monthly).
- For each, assert: spent_cents/target_cents/pct_used per period match hand-computed values; color flags trigger at 80% and 100%; percent-of-income recomputes per period.
- **Rollover test**: create a budget for month N, then for month N+1 — verify the system preloads N's actuals and suggests adjustments per the user's overrun/underrun history.

### Step 3: Tracking — Period back-tests

Compute longitudinal metrics on historical data, assert identity:

- **Revenue identity**: `sum(monthly_revenue[m]) == annual_revenue`.
- **Net worth recursion**: `net_worth[t+1] == net_worth[t] + cash_flow[t→t+1] + asset_appreciation[t→t+1]`.
- **Profit identity**: `profit[period] == revenue[period] - expense[period]`.
- **Period delta correctness**: `compare(p1, p2).delta_abs_cents == p2.spent - p1.spent` per category.
- Net worth at known checkpoints (start of 2025, end of 2025) hits the exact value computed from raw data + mortgage amortization.

### Step 4: Activity — Known-anomaly tests

The `samples/` dataset contains real anomalies. The system must flag them. Specific assertions:

- `GET /api/anomalies` includes every transaction whose merchant matches `Exampleco Subs` (recurring fraudulent subscription).
- `GET /api/anomalies` includes every transaction whose merchant matches `Examplefind` (suspicious).
- `GET /api/anomalies` includes the specific refunded transactions in the dataset (not Amazon refunds — others). Exact list to be enumerated during the Step 1 data audit.
- **Recurring-charge intelligence**: `GET /api/anomalies?type=recurring` reports every subscription with consecutive-month count and price-change history.
- **Spike detection**: when a category's spend in month M is > 2× its trailing 6-month average, it appears in `GET /api/anomalies?type=spike`.

### Step 5: Extras — Playwright E2E + Opus visual judgment

Every human-doable flow has a Playwright spec. NOT "click button, get 200" — actual end-to-end assertions that the right data lands in the right place.

Flows to cover (non-exhaustive):
- Upload a new CSV → progress shown → transactions appear in the table → totals on dashboard update.
- Edit one transaction's category inline → budget allocation reflects → dashboard total reflects → activity log records the change.
- Apply correction-to-matching for EXAMPLETENANT → ripple to all Exampletenant rows across channels → budget_context.md gets an Exampletenant-tenant entry.
- Chat-correct: "Spectrum bills are rental utilities" → all Spectrum rows recategorize → budget_context.md updates.
- Change the date filter to YTD → dashboard / sankey / transactions / compare / trends all reflect.
- Sankey renders with expected node and link counts; no Uncategorized nodes.
- Manual cash entry adds a row that appears everywhere it should.

**Visual judgment step**: each Playwright spec takes a screenshot at the key state and sends it to Claude Opus 4.7 along with the reference design (`frontend-design/screenshots/`). Opus returns `LOOKS_RIGHT` / `LOOKS_WRONG` with a one-line reason. A spec passes when Playwright assertions are green AND Opus returns `LOOKS_RIGHT`.

**Runtime**: simpler deterministic specs run under OpenCode. The Opus visual-judgment step runs through Claude Code (or any harness that hits Opus 4.7).

---

## Tasks

The JSON list below is the authoritative task checklist, organized by step. Each iteration: pick the highest-priority story whose `depends_on` are all true and `passes` is false. Work on exactly one. Update `passes` to true only when all `acceptance` items are demonstrably true.

```json
[
  {
    "id": "00-budget-context-profile",
    "step": 1,
    "category": "data",
    "priority": 1,
    "passes": true,
    "depends_on": ["01-ingest-multi-format"],
    "title": "Budget context profile — inferred on first run, updated by every correction, fed to every LLM call",
    "description": "budget_context.md starts blank. On first backend startup with non-empty samples/ and empty budget_context.md, the backend infers a summary of the user's life from imported transactions and writes it back. From then on, every user correction (story 05) and every chat-correct (story 13) appends, edits, or prunes entries. The file is read on demand and included in the system prompt of every LLM categorization call.",
    "acceptance": [
      "First-run inference produces a non-empty budget_context.md (> 200 chars) that names at least three of the accounts visible in samples/",
      "GET /api/context returns {content, last_modified}; PUT /api/context writes back to disk",
      "Every outbound /v1/messages call for categorization includes budget_context.md content in the system prompt (verified by logging the outbound payload)",
      "Edits propagate without backend restart",
      "Frontend has an editable context page using the existing design language"
    ]
  },
  {
    "id": "01-ingest-multi-format",
    "step": 1,
    "category": "data",
    "priority": 1,
    "passes": true,
    "depends_on": [],
    "title": "Multi-format ingest into the canonical transaction schema",
    "description": "Parse every file in samples/. Auto-detect platform by header signature. Normalize into the canonical transaction schema (see PRD). Supported: Chase credit (1236, 9944), Chase checking (4573), Chase loan (3600), US Bank checking (3262), BofA CHK_0197, BofA stmt.csv, Cash App, PayPal (UTF-8 BOM), Venmo HTML (best-effort), mortgage amortization PDF.",
    "acceptance": [
      "POST /api/import returns one element per file with {file, platform_detected, account_id, imported, duplicates, skipped, warnings}",
      "Every imported row matches the canonical schema (all required fields non-null)",
      "Venmo.html may emit a parse warning; importer does not crash",
      "Mortgage PDF rows land in an `amortization` table with {month, principal_cents, interest_cents, ending_balance_cents}",
      "GET /api/accounts returns ≥ 6 distinct accounts"
    ]
  },
  {
    "id": "02-cross-file-dedup",
    "step": 1,
    "category": "data",
    "priority": 1,
    "passes": true,
    "depends_on": ["01-ingest-multi-format"],
    "title": "Cross-file deduplication via (account, date, amount, normalized_description) fingerprint",
    "description": "Re-importing same/overlapping files must not create duplicates. Normalize descriptions: lowercase, collapse whitespace, strip store numbers (#1234, 00020693), strip asterisks and trace IDs.",
    "acceptance": [
      "Two consecutive POST /api/import calls yield identical total transaction count (delta = 0)",
      "After importing all three overlapping the credit card CSVs (1001/550/445 rows), GET /api/transactions?account_id=card-9xxx returns exactly 1001 rows",
      "Three overlapping US Bank 3262 files yield the date-range union, not the sum"
    ]
  },
  {
    "id": "03-transfer-detection-and-collapse",
    "step": 1,
    "category": "data",
    "priority": 1,
    "passes": true,
    "depends_on": ["01-ingest-multi-format", "02-cross-file-dedup"],
    "title": "Internal transfer detection AND removal of BOTH legs from income/expense totals",
    "description": "Money moving between two user-owned accounts is a transfer. Detect by: (a) same-amount opposite-sign pair in two owned accounts within ±2 days, (b) description hints (ACCT_XFER, LOAN_PMT, Payment, Cash Out, PayPal Transfer). If only one leg ingested, label that leg as transfer and warn. Linked pairs share `linked_transaction_id` and are persisted.",
    "acceptance": [
      "GET /api/transfers returns matched pairs with linked_transaction_id wired in both directions",
      "GET /api/dashboard total_income_cents excludes credit-card 'Payment' inflows and the 2026-04-28 $100K mortgage payoff",
      "GET /api/transfers/missing-counterparts surfaces transfers whose other side isn't ingested (e.g. 'from BofA 3865')",
      "COUNT(*) FROM transactions WHERE entity_type = 'transfer' = 2 × COUNT(*) FROM transfers (both legs persisted)"
    ]
  },
  {
    "id": "04-llm-categorization-no-regex",
    "step": 1,
    "category": "data",
    "priority": 1,
    "passes": true,
    "depends_on": ["00-budget-context-profile", "01-ingest-multi-format"],
    "title": "LLM categorization with budget_context + ground-truth rules in the system prompt",
    "description": "Every new transaction gets a category via the LLM. System prompt includes: the category tree, active user rules, budget_context.md content, and the PRD ground-truth rules. NO regex / keyword maps. Batched (e.g. 20 per call). Each row gets a category_path, confidence_score, and categorization_reason. The categorizer MUST actually run on the FULL imported dataset, not just on new rows arriving post-build — auto-import on boot should trigger bulk categorization with a sane concurrency cap. Current DB has 5,415/5,771 rows uncategorized which fails the deterministic test below.",
    "acceptance": [
      "Deterministic: SELECT COUNT(*) FROM transactions WHERE category_path IS NULL OR category_path = 'Uncategorized' = 0",
      "Deterministic: static source check — no merchant-keyword regex tables in backend/",
      "Deterministic: every /v1/messages call includes the tree + budget_context + ground-truth rules in the system prompt",
      "Deterministic: every row has confidence_score (0..1) and categorization_reason (non-empty)",
      "Deterministic: full samples/ dataset categorizes in under 5 minutes",
      "Evaluator: Opus 4.7 judge returns zero REJECT on the full categorized set; 100% of Cash App inbound payments → Income > Rentals > <Person>; 100% of foreign-country merchants → Expense > Leisure > <sub>; 100% of rental utility merchants → Expense > Rental > Utilities; 100% of named subscriptions → Expense > Personal > Subscriptions"
    ]
  },
  {
    "id": "05-user-correction-with-semantic-learning",
    "step": 1,
    "category": "data",
    "priority": 1,
    "passes": true,
    "depends_on": ["04-llm-categorization-no-regex"],
    "title": "User corrections re-categorize semantically AND update the living context",
    "description": "PATCH /api/transactions/{id} {category_path, apply_to_matching}. With apply_to_matching=true, the LLM identifies semantically-related rows across channels (Zelle EXAMPLETENANT ≡ Cash App Exampletenant) and re-tags them. The system also appends the underlying fact to budget_context.md so future ingests apply the rule without LLM round-trips. Includes before/after diff in the response.",
    "acceptance": [
      "Deterministic: PATCH returns {re_categorized_count, affected_ids, context_diff, before, after}",
      "Deterministic: after patching one EXAMPLETENANT Zelle row, 100% of EXAMPLETENANT rows have the rental category (exact)",
      "Evaluator: Opus confirms 100% of semantically-Exampletenant rows across all channels are correctly retagged (including 'O. Aguila', 'Exampletenant A.', Cash App variants)",
      "Deterministic: GET /api/rules lists the new rule; restart preserves it; next import applies it BEFORE the LLM call",
      "Deterministic: GET /api/context shows the Exampletenant fact appended to budget_context.md"
    ]
  },
  {
    "id": "06-hierarchical-categories",
    "step": 1,
    "category": "data",
    "priority": 2,
    "passes": true,
    "depends_on": ["04-llm-categorization-no-regex"],
    "title": "Hierarchical, user-extensible category tree with multi-dimensional tags",
    "description": "Categories form a tree (any depth). Each transaction has one category_path + free-form tags. Default tree per the PRD includes Income, Expense > Housing, Expense > Rental, Expense > Personal, Expense > Leisure, Expense > Business, Expense > Fees, and Transfer with all sub-leaves.",
    "acceptance": [
      "GET /api/categories/tree returns the full nested tree (root → children → leaves)",
      "POST /api/categories {name, parent_id} creates a child that persists across restart",
      "PUT /api/categories/{id} renames or moves",
      "PATCH /api/transactions/{id} {tags: ['person:xavier', 'property:3323-ridge']} stores both tags",
      "GET /api/transactions?tag=person:xavier returns the subset carrying that tag"
    ]
  },
  {
    "id": "07-mortgage-full-split",
    "step": 1,
    "category": "data",
    "priority": 2,
    "passes": true,
    "depends_on": ["01-ingest-multi-format", "03-transfer-detection-and-collapse"],
    "title": "Mortgage payment 5-way split: principal / interest / taxes / insurance / escrow",
    "description": "For each mortgage outflow (Chase 3600 LOAN_PMT or checking 'TO HMG'), look up the month in the amortization table and (if available) the monthly statement breakdown. Split the payment into: principal → Transfer > Mortgage Principal (asset conversion, excluded); interest → Expense > Housing > Mortgage Interest (counted); taxes → Expense > Housing > Mortgage Taxes (counted); insurance → Expense > Housing > Mortgage Insurance (counted); escrow → Transfer > Mortgage Escrow (liability holding, excluded).",
    "acceptance": [
      "Each detected mortgage payment produces (up to) 5 component rows that sum exactly to the original outflow amount",
      "Interest/taxes/insurance rows have category paths under Expense > Housing > Mortgage *",
      "Principal and Escrow rows have entity_type = 'transfer' and asset_class = 'mortgage_principal' / 'escrow'",
      "The 2026-04-28 $100K payoff splits with ≤ $1K interest and the rest principal — does NOT appear as a $100K expense",
      "Months outside the amortization range fall back to Transfer with a warning in the import report"
    ]
  },
  {
    "id": "08-payroll-intelligence",
    "step": 1,
    "category": "data",
    "priority": 2,
    "passes": true,
    "depends_on": ["01-ingest-multi-format"],
    "title": "Paystub PDF intelligence — split direct-deposit into gross / taxes / 401k / insurance / net",
    "description": "When a paystub PDF is uploaded (or present in samples/), parse it and link to the corresponding bank direct-deposit transaction. Generate structured rows: gross income, federal/state/FICA taxes, 401k contribution, insurance/benefits, net deposit. The net should match the bank-side deposit to the cent.",
    "acceptance": [
      "POST /api/import on a paystub PDF returns parsed components {gross, taxes, retirement, benefits, net}",
      "The linked bank deposit has linked_transaction_id pointing at the paystub source",
      "GET /api/transactions for that pay period returns the structured breakdown",
      "Sum(components) = gross; gross - sum(deductions) = net (identity invariant)",
      "All 8 paystubs in samples/paystubs/2026/ parse cleanly and link to their corresponding checking-account direct-deposit transactions",
      "Employer is read from the statement itself (the WORKSITE EMPLOYER header), preserving the exact casing the document uses"
    ]
  },
  {
    "id": "09-manual-transaction-entry",
    "step": 1,
    "category": "data",
    "priority": 3,
    "passes": true,
    "depends_on": ["01-ingest-multi-format"],
    "title": "Manual transaction entry — cash income, cash expenses, adjustments",
    "description": "User can manually create rows that won't be in any CSV: cash rental income, cash expenditures, off-platform corrections.",
    "acceptance": [
      "POST /api/transactions {amount_cents, posted_date, category_path, source_platform: 'manual', business_context, notes} creates a row",
      "Manual rows participate in all aggregations (dashboard, sankey, budgets, net worth) identically to imported rows",
      "Manual rows are flagged with source_platform = 'manual' and survive re-imports of CSVs",
      "Frontend has an Add Transaction form using the existing design language"
    ]
  },
  {
    "id": "10-categorization-explainability-and-confidence",
    "step": 1,
    "category": "data",
    "priority": 3,
    "passes": true,
    "depends_on": ["04-llm-categorization-no-regex"],
    "title": "Every categorization exposes confidence, reasoning, and applied rules — editable by the user",
    "description": "GET /api/transactions/{id} returns categorization_reason (the LLM's rationale), confidence_score (0..1), and applied_rule_id (which user rule fired, if any). The frontend shows these on each row. User can click through to see the rule, edit it, or override.",
    "acceptance": [
      "Every row has non-empty categorization_reason and a confidence_score in [0, 1]",
      "GET /api/rules/{id} returns rule details with its match pattern, count of affected transactions, and creation timestamp",
      "Frontend transactions row exposes a 'why?' affordance that opens the reasoning panel"
    ]
  },
  {
    "id": "11-auto-import-on-boot",
    "step": 1,
    "category": "data",
    "priority": 4,
    "passes": true,
    "depends_on": ["01-ingest-multi-format", "04-llm-categorization-no-regex"],
    "title": "Backend auto-imports samples/ on startup if the DB is empty",
    "description": "If transactions is empty and samples/ has files, run the full pipeline (ingest → dedup → categorize → transfer → split mortgage) as a background task. Serve progress at /api/import/status.",
    "acceptance": [
      "After `docker compose down -v && docker compose up -d`, within 5 minutes GET /api/dashboard returns non-zero totals",
      "GET /api/import/status returns {state, progress_pct, last_run}",
      "Frontend shows an importing… state, then dashboard data once complete"
    ]
  },
  {
    "id": "20-budgets-percent-or-absolute",
    "step": 2,
    "category": "budget",
    "priority": 2,
    "passes": true,
    "depends_on": ["06-hierarchical-categories"],
    "title": "Budget allocations — absolute amounts OR percent-of-income, per period, with color status",
    "description": "Per category (any tree node). Each budget is either {type:'absolute', amount_cents} or {type:'percent_of_income', percent}. Period: day/week/month/quarter/year/custom. Status: spent/target/Δ/pct, color green < 80% < yellow < 100% ≤ red.",
    "acceptance": [
      "POST /api/budgets {category, type, amount_cents|percent, period} creates a budget",
      "GET /api/budgets/status?period=2026-04 returns {spent_cents, target_cents, pct_used, color, forecast_overrun_cents} per budget",
      "For percent_of_income budgets, target_cents recomputes per period using actual period income (no stale caching)",
      "Frontend Budgets page uses frontend-design/src/Budgets.jsx",
      "Back-test: applying mock-budget A (10/20/30/5/35%) to samples/ produces exact spent/target values for every month — verified row by row"
    ]
  },
  {
    "id": "21-budget-rollovers",
    "step": 2,
    "category": "budget",
    "priority": 3,
    "passes": true,
    "depends_on": ["20-budgets-percent-or-absolute"],
    "title": "Budget rollover — when creating a new period, preload previous actuals + suggested adjustments",
    "description": "Creating a budget for month N+1 preloads from month N: shows last period's allocation, actual spend, variance, and a suggested next-period allocation (LLM-driven). User can accept all, edit, or override.",
    "acceptance": [
      "POST /api/budgets/rollover {from_period, to_period} returns {previous: [{category, allocated, spent, variance}], suggested: [{category, allocated, reason}]}",
      "Suggestion reasons are human-readable (e.g. 'Dining overran by 30% — increase from $400 to $520')",
      "Accepting the rollover creates budgets for the next period in one shot",
      "Frontend Budgets page surfaces the rollover affordance"
    ]
  },
  {
    "id": "22-budget-intelligence-suggestions",
    "step": 2,
    "category": "budget",
    "priority": 3,
    "passes": true,
    "depends_on": ["20-budgets-percent-or-absolute"],
    "title": "Budget intelligence — AI-suggested allocations, overspending risks, savings opportunities",
    "description": "Mid-period: surface 'you're at 87% of Dining Out with 6 days left'. Cross-period: 'Subscriptions grew 40% YoY — review for waste'. Suggested allocations based on the user's historical pattern, business_context (rental vs personal), and stated savings goals.",
    "acceptance": [
      "GET /api/budgets/intelligence returns a list of insights {kind, severity, category, message, suggested_action}",
      "Insight kinds include: over-budget-risk, recurring-waste, savings-opportunity, historical-anomaly",
      "Each insight cites the underlying transactions or budget rows",
      "Frontend renders insights in a dedicated panel"
    ]
  },
  {
    "id": "30-dashboard-totals-prominent",
    "step": 3,
    "category": "tracking",
    "priority": 2,
    "passes": true,
    "depends_on": ["03-transfer-detection-and-collapse"],
    "title": "Dashboard prominently displays Income / Expense / Net AFTER transfer collapse (uses Dashboard.jsx)",
    "description": "Port frontend-design/src/Dashboard.jsx to a real React route wired to /api/dashboard. Three big numbers at the top — true external income, true external expense, signed net. Below: per-category breakdown, monthly bar/line chart, recent activity.",
    "acceptance": [
      "GET /api/dashboard?start_date=&end_date= returns {totals: {total_income_cents, total_expense_cents, net_cents}, by_category, monthly}",
      "net_cents = income - expense, exactly",
      "Totals exclude transfers and mortgage principal/escrow",
      "Frontend matches the visual design of Dashboard.jsx"
    ]
  },
  {
    "id": "31-date-filter-and-compare",
    "step": 3,
    "category": "tracking",
    "priority": 3,
    "passes": true,
    "depends_on": ["30-dashboard-totals-prominent"],
    "title": "Global date filter + Compare view (uses Compare.jsx)",
    "description": "Whole UI under a date filter (presets: this month, last month, YTD, last year, all time, custom). /compare shows period-vs-period with per-category absolute and % deltas.",
    "acceptance": [
      "GET /api/compare?period1_start=&period1_end=&period2_start=&period2_end= returns per-category {period1, period2, delta_abs, delta_pct}",
      "Presets resolve to correct date ranges",
      "Custom range pickers update all pages",
      "Frontend uses Compare.jsx"
    ]
  },
  {
    "id": "32-net-worth-tracking",
    "step": 3,
    "category": "tracking",
    "priority": 3,
    "passes": true,
    "depends_on": ["07-mortgage-full-split", "03-transfer-detection-and-collapse"],
    "title": "Net worth — assets minus liabilities, tracked over time (uses NetWorth.jsx)",
    "description": "Assets: cash balances across all accounts (current and historical), real estate equity (purchase price - remaining mortgage balance), investments (manual entry until brokerage ingest). Liabilities: outstanding mortgage balance (from amortization), credit-card balances. Net worth = assets - liabilities. Tracked over time using the amortization schedule + transaction history.",
    "acceptance": [
      "GET /api/networth returns {as_of, assets: [{kind, label, value_cents}], liabilities: [{kind, label, value_cents}], total_net_worth_cents}",
      "GET /api/networth/history?start=&end= returns a time series of net worth at end of each month",
      "Net worth at end of month M+1 = net worth at end of month M + cash flow during M+1 + asset appreciation - liability paydown (identity)",
      "Frontend NetWorth page uses NetWorth.jsx"
    ]
  },
  {
    "id": "33-time-series-trends",
    "step": 3,
    "category": "tracking",
    "priority": 3,
    "passes": true,
    "depends_on": ["30-dashboard-totals-prominent"],
    "title": "Time-series trends — revenue, expense, profit, net worth over arbitrary periods (uses Trends.jsx)",
    "description": "Charts: monthly revenue line, monthly expense line, monthly profit (revenue - expense), net worth over time, per-category stacked bars. All under the global date filter.",
    "acceptance": [
      "GET /api/trends?metric=&period=&start=&end= returns a time series for the requested metric",
      "Supported metrics: revenue, expense, profit, net_worth, category_<path>",
      "Identity: sum of monthly[m] in any year = annual value for the same metric",
      "Frontend Trends page uses Trends.jsx"
    ]
  },
  {
    "id": "40-anomaly-detection",
    "step": 4,
    "category": "activity",
    "priority": 3,
    "passes": true,
    "depends_on": ["04-llm-categorization-no-regex"],
    "title": "Anomaly detection — recurring charges, spikes, duplicates, new merchants",
    "description": "Compute per-merchant streaks, per-category trailing averages, duplicate-candidate detection, and new-merchant flags. Surface in /api/anomalies with reason and severity.",
    "acceptance": [
      "GET /api/anomalies returns {transaction_id, kind, severity, reason, suggested_action}",
      "Kinds: recurring-subscription, spending-spike, duplicate-candidate, new-merchant, category-anomaly",
      "Recurring-subscription anomalies include consecutive_month_count and price_change_history",
      "Spikes trigger when a category's spend in month M > 2× trailing 6-month average",
      "Duplicate-candidate flags rows whose (amount, ±3 days, similar description) match another row in the same account",
      "Back-test: against samples/, the output includes every Exampleco Subs row, every Examplefind row, and the known refunded rows"
    ]
  },
  {
    "id": "41-fraud-and-refund-flagging",
    "step": 4,
    "category": "activity",
    "priority": 3,
    "passes": true,
    "depends_on": ["40-anomaly-detection"],
    "title": "Fraud flagging + refund analysis — Exampleco Subs / Examplefind (both fraudulent AND refunded)",
    "description": "Both Exampleco Subs and Examplefind are known fraudulent merchants whose charges were later refunded by the user. The system must flag them via fuzzy match on the merchant string (case-insensitive substring or Levenshtein ≤ 2 — no other merchant in the dataset is close to either, so no false positives). Other refunded transactions (where a charge was later credited back) are surfaced as review candidates, except routine Amazon refunds.",
    "acceptance": [
      "Fuzzy match: every transaction whose merchant matches 'exampleco subs' (case-insensitive, Levenshtein ≤ 2) has flagged=true and flag_reason='fraud'",
      "Fuzzy match: every transaction whose merchant matches 'examplefind' (case-insensitive, Levenshtein ≤ 2) has flagged=true and flag_reason='fraud'",
      "The refund-pair detector identifies each fraud charge AND its later refund row, linking them via linked_transaction_id and reporting them as a refund pair",
      "GET /api/anomalies?type=refund returns the Exampleco Subs + Examplefind pairs plus any other non-Amazon refund pairs found in samples/",
      "Amazon refunds are NOT in the refund anomaly list",
      "Frontend shows a flagged badge on transactions and a dedicated 'Activity' page for review"
    ]
  },
  {
    "id": "42-chat-correction-ui",
    "step": 4,
    "category": "activity",
    "priority": 4,
    "passes": true,
    "depends_on": ["05-user-correction-with-semantic-learning"],
    "title": "Chat-style natural-language corrections that update the living context",
    "description": "Chat panel for free-form corrections. The LLM interprets, applies changes, writes facts to budget_context.md, returns audit summary.",
    "acceptance": [
      "POST /api/chat-correct {message} returns {affected_count, affected_ids, summary, context_diff}",
      "POST /api/chat-correct {message: 'tag all EXAMPLETENANT transactions with person:exampletenant'} affects exactly the count of EXAMPLETENANT rows",
      "After a chat-correct establishing a rule (e.g. 'Spectrum bills are rental utilities'), GET /api/context shows the new fact, and next ingest applies the rule without an LLM call",
      "Frontend chat textarea visible and styled to match frontend-design/"
    ]
  },
  {
    "id": "50-sankey-true-flows",
    "step": 5,
    "category": "extras",
    "priority": 3,
    "passes": true,
    "depends_on": ["03-transfer-detection-and-collapse", "04-llm-categorization-no-regex"],
    "title": "Sankey of true external income → expense flows (uses Sankey.jsx)",
    "description": "Income sources left → through user → expense categories right. Plus a 'Saved' outflow when net > 0. Real category leaves, never Uncategorized. Post-transfer-collapse. SANKEY MUST DISPLAY THE FULL RECONCILED LEDGER, not just the categorized subset — dropping Uncategorized rows to satisfy the 'no Uncategorized node' rule violates the consistency invariant below. The fix is to make 04 actually categorize everything, then Sankey shows real flows over the full dataset.",
    "acceptance": [
      "GET /api/sankey returns {nodes: [{id, name, kind}], links: [{source, target, value_cents}]}",
      "No node named Uncategorized / Unknown / null / empty",
      "sum(income-side link values) = dashboard.total_income_cents (consistency)",
      "sum(expense-side links) + savings_link = total_income_cents (conservation)",
      "Frontend Sankey page uses Sankey.jsx"
    ]
  },
  {
    "id": "51-transactions-page-with-filters",
    "step": 5,
    "category": "extras",
    "priority": 3,
    "passes": true,
    "depends_on": ["06-hierarchical-categories"],
    "title": "Unified transactions page with filters, search, bulk edit, CSV export (uses Transactions.jsx)",
    "description": "Columns: date, account, merchant, description, amount, category (inline-editable), tags (inline-editable), source-file, flag. Filters: date range, account, category, tag, business_context, transaction_type. Search: substring. Pagination. Bulk-edit + bulk-categorize. CSV export.",
    "acceptance": [
      "GET /api/transactions returns {transactions, total, page, page_size}",
      "All filter combos work and AND together",
      "PATCH /api/transactions/{id} updates category or tags",
      "POST /api/transactions/bulk-edit {ids, patch} applies the patch to all listed ids",
      "GET /api/transactions/export.csv returns the current filter set as CSV",
      "Frontend uses Transactions.jsx"
    ]
  },
  {
    "id": "52-docker-deploy-and-persistence",
    "step": 5,
    "category": "extras",
    "priority": 5,
    "passes": true,
    "depends_on": ["01-ingest-multi-format"],
    "title": "Single docker compose up boots stack; data persists",
    "description": "Backend host :8001 → container :8000; frontend host :8081 → container :8080. Named docker volume for SQLite. Healthchecks use Python urllib, not curl-without-installing-curl.",
    "acceptance": [
      "After `docker compose up -d --build`, both containers 'Up' within 30s",
      "GET http://localhost:8081/ returns 200 HTML",
      "GET http://localhost:8001/api/health returns {status: 'ok'}",
      "After `docker compose down` then `up -d`, transactions and rules survive",
      "docker compose config shows a named volume for SQLite"
    ]
  },
  {
    "id": "53-final-integration-and-e2e",
    "step": 5,
    "category": "extras",
    "priority": 6,
    "passes": true,
    "depends_on": ["01-ingest-multi-format", "02-cross-file-dedup", "03-transfer-detection-and-collapse", "04-llm-categorization-no-regex", "30-dashboard-totals-prominent", "50-sankey-true-flows", "52-docker-deploy-and-persistence"],
    "title": "End-to-end Playwright suite + Opus visual judgment passes",
    "description": "Cold-boot the stack. Run the full Playwright suite. Every human flow passes both behavioral assertions AND Opus visual judgment. The numbers across dashboard, sankey, transactions, compare, trends, and net-worth are mutually consistent.",
    "acceptance": [
      "All Playwright specs pass behavioral assertions",
      "Opus 4.7 returns LOOKS_RIGHT on every screenshot vs the frontend-design reference",
      "Cross-page consistency: dashboard.total_income = sankey income-leg sum = sum(monthly.revenue) = trends income series annual total",
      "Re-running the suite from cold produces identical numbers and identical Opus verdicts",
      "Zero uncategorized; transfer pairs symmetric; flagged anomalies match the known list (Exampleco Subs, Examplefind, suspicious refunds)"
    ]
  },
  {
    "id": "60-plaid-link-and-sync",
    "step": 6,
    "category": "live-data",
    "priority": 1,
    "passes": false,
    "depends_on": ["01-ingest-multi-format", "52-docker-deploy-and-persistence"],
    "title": "Plaid Link + cursor-based transactions/sync — the app pulls from Chase continuously instead of eating CSV drops",
    "description": "Port the Plaid layer from /home/k/rental-management (src/lib/integrations/plaid.ts, src/lib/crypto/secrets.ts, src/app/api/plaid/*) to Python. Access tokens are encrypted at rest with AES-256-GCM using the same wire format as the TS implementation (v1:<iv>:<tag>:<ciphertext>, base64 32-byte ENCRYPTION_KEY), so one key serves both apps. Link tokens request days_requested=730 to pull the maximum history Plaid offers. Sync is cursor-based via transactions/sync, follows has_more, persists the cursor, and records item status/error_code (ITEM_LOGIN_REQUIRED => LOGIN_REQUIRED). Credentials come from env only (PLAID_CLIENT_ID, PLAID_PRODUCTION_SECRET, PLAID_SANDBOX_SECRET, ENCRYPTION_KEY) and are never logged. A periodic in-process sync task (PLAID_SYNC_INTERVAL_MIN, default 360, 0 disables) makes the app live with zero host setup. Plaid accounts auto-map by mask to the existing account_id convention (mask 3xxx => bank-3xxx, and so on).",
    "acceptance": [
      "crypto.py round-trips, and decrypts a ciphertext produced by rental-management's secrets.ts (cross-app key compatibility proven, not assumed)",
      "POST /api/plaid/link-token returns a link_token; POST /api/plaid/exchange stores an encrypted access token and never writes plaintext to the DB",
      "GET /api/plaid/items reports institution, status, per-account mapping, last_synced_at and freshness",
      "POST /api/plaid/sync walks paginated transactions/sync (has_more), applies added/modified/removed, and persists the advanced cursor",
      "A Plaid API failure sets status/error_code on the item rather than throwing away the cursor",
      "Post-sync categorization runs on the newly inserted ids ONLY — never a full-table pass (see AGENTS.md)",
      "No secret value appears in logs, in the DB, or in any committed file"
    ]
  },
  {
    "id": "61-plaid-only-expenditure-scope",
    "step": 6,
    "category": "live-data",
    "priority": 1,
    "passes": true,
    "depends_on": ["60-plaid-link-and-sync"],
    "title": "Scope the app to expenditures on Plaid-connected Chase accounts — Plaid is the only live source of truth",
    "description": "Owner directive (2026-07-24): this app previously struggled with net-worth math and with deduplicating transactions BETWEEN accounts, so both are out of scope. The app focuses on expenditures, and only on expenditures from Chase accounts connected through Plaid. The CSV-derived rows already in the DB are retained as a stored backup/history but are NOT the live source and are excluded from the default views; Plaid is. Rather than reconciling CSV rows against Plaid rows (the cross-source dedup this story used to describe), the two sources are simply kept in separate scopes — which removes the entire class of between-account dedup bugs instead of trying to solve it. Dedup WITHIN Plaid still matters and lives in story 60: external_id upsert, pending_transaction_id transition, one-to-one claiming. Double-counting of card payments is prevented at the CATEGORY level (a checking-to-card payment categorizes as Transfer > Credit-card Payment and is excluded from expenditure totals), NOT by pairwise transfer matching.",
    "acceptance": [
      "Every list, total and chart defaults to scope=plaid-expenses: source_platform='plaid', expense rows only",
      "Pre-Plaid CSV rows remain in the DB, are reachable via an explicit scope override, and never contribute to a default total",
      "A checking-to-credit-card payment does not appear in expenditure totals (category-level exclusion, no pairwise matching)",
      "No endpoint performs between-account transfer matching to produce a default view",
      "Switching scope is a single API parameter, so the same endpoints serve both scopes without duplicated logic"
    ]
  },
  {
    "id": "62-amazon-order-ingest",
    "step": 6,
    "category": "amazon",
    "priority": 2,
    "passes": true,
    "depends_on": ["01-ingest-multi-format"],
    "title": "Amazon order ingest from two interchangeable sources — Playwright scraper and the official data export",
    "description": "The Amazon Prime card (card-1xxx) is mostly rows like 'Amazon.com*NO1FM5J33  -46.01', so categorization is a guess. scripts/amazon-scrape.mjs uses Node Playwright (already a repo dependency) headed on the host with a persistent browser profile (~/.budget-app/amazon-profile) so login and 2FA happen once. It walks your-orders per year and extracts order id, order date, order total, recipient/ship-to, per-shipment totals, and per item title / ASIN / unit price / quantity, merging into samples/amazon/orders-<stamp>.json keyed by order id. All CSS selectors live in one CONFIG block because Amazon's DOM churns; a selector miss dumps a screenshot and exits non-zero rather than silently writing a partial file. backend/app/parsers/amazon.py parses BOTH that JSON and Amazon's official Retail.OrderHistory.*.csv export into one canonical order/item shape — the export path is header-name-driven with alternates and raises an error listing the headers it actually saw, since the real export's headers cannot be verified until it arrives.",
    "acceptance": [
      "Scraper JSON and export CSV produce byte-identical canonical output for the same order",
      "Re-running the scraper merges by order id and does not duplicate orders",
      "A changed/missing selector fails loudly with a screenshot and a non-zero exit — never a silent partial write",
      "amazon_orders and amazon_items tables are populated with per-item title, ASIN, unit price, quantity",
      "POST /api/amazon/import ingests whatever is present in samples/amazon/ and reports counts per source"
    ]
  },
  {
    "id": "63-amazon-match-and-split",
    "step": 6,
    "category": "amazon",
    "priority": 2,
    "passes": true,
    "depends_on": ["62-amazon-order-ingest", "04-llm-categorization-no-regex"],
    "title": "Match Amazon charges to orders and split them into per-item rows so category totals are actually true",
    "description": "amazon_matcher.py matches Amazon-normalizing charges (Amazon.com*, AMZN Mktp US*, AMAZON MKTPL, Amazon Prime, AMZN Digital — account-agnostic) to ingested orders in descending confidence, one-to-one, closest date wins: shipment total == charge amount within order_date..order_date+7d (multi-shipment orders produce several charges, so shipment level comes first), then order total, then bounded subset-sum over the order's remaining items (<= 8 items) for partial captures. Unmatched charges are left completely untouched and surfaced for review — never guessed at. Splitting reuses the proven mortgage_splitter._create_component pattern: one child transaction per item with linked_transaction_id = parent, the parent flipped to entity_type='transfer' / 'Transfer > Amazon Order (Split)' so it drops out of expense totals instead of double-counting, and any residual (tax, shipping, gift wrap) as a single explicit 'Tax & Shipping' child. Children are then categorized by the LLM with the product title as the description, which is what finally produces real Amazon category detail.",
    "acceptance": [
      "Children sum to the parent EXACTLY — zero tolerance, same contract as the mortgage splitter",
      "A split parent is excluded from expense totals and the dashboard identity net = income - expense still holds",
      "A multi-shipment order matches its several charges at shipment level, not as one lump",
      "Unmatched Amazon charges are reported by GET /api/amazon/unmatched and left byte-identical in the DB",
      "A $46 charge that is $30 of tools + $16 of snacks lands in two different category buckets",
      "GROUND_TRUTH_RULES instructs the categorizer to categorize Amazon item children from the product title"
    ]
  },
  {
    "id": "64-accounts-page-and-defaults",
    "step": 6,
    "category": "frontend",
    "priority": 3,
    "passes": false,
    "depends_on": ["60-plaid-link-and-sync", "61-plaid-only-expenditure-scope", "63-amazon-match-and-split"],
    "title": "Accounts page — connect a bank, watch sync freshness, drive Amazon ingest; default the app to 2026",
    "description": "New /accounts page and nav entry in frontend/src/App.jsx using the existing design language from frontend-design/ (do not redesign): Plaid items with institution, status, last-synced freshness, a Connect a bank button via react-plaid-link, Sync now, per-account canonical-id mapping, and unlink. The same page hosts the Amazon panel — import / match / split actions plus coverage ('N of M Amazon charges matched') linking to the unmatched list. The default global period becomes 'ytd', which for 2026 resolves to Jan 1 2026 through today via the existing resolvePeriod(); 'All Time' remains available for the CSV history. WhyPanel shows the matched product list for Amazon split parents and children.",
    "acceptance": [
      "Connect a bank opens Plaid Link and a successful link appears in the accounts list without a reload",
      "Sync now updates the freshness indicator and surfaces LOGIN_REQUIRED when Chase needs re-auth",
      "Per-account canonical mapping is editable and persists",
      "Default period is 2026 YTD on a cold load; All Time still reaches the pre-2026 CSV history",
      "WhyPanel lists the matched Amazon items for a split parent and shows the parent link on a child",
      "Opus visual judgment returns LOOKS_RIGHT for the new page against the frontend-design reference"
    ]
  },
  {
    "id": "65-retire-net-worth-and-transfer-matching",
    "step": 6,
    "category": "frontend",
    "priority": 3,
    "passes": true,
    "depends_on": ["61-plaid-only-expenditure-scope"],
    "title": "Retire net worth and between-account transfer matching from the product surface",
    "description": "Owner directive (2026-07-24): net-worth calculation and between-account transfer dedup were persistent sources of wrong numbers, so they stop being part of the product instead of being fixed. Remove Net Worth from the navigation and stop presenting its figures. Stop running pairwise transfer detection as part of the live Plaid pipeline. Existing routers stay registered so nothing 404s or 500s for a stale bookmark or an existing test, and the historical data is untouched — this is a surface-area and pipeline change, not a data migration. Every remaining page must still load without a crash after the removal.",
    "acceptance": [
      "Net Worth is absent from the nav; no default view presents a net-worth figure",
      "The Plaid sync pipeline does not invoke pairwise transfer detection",
      "GET on the retired routers still returns a valid response rather than a 500",
      "Every page in the nav loads with no console errors and no crash after the change",
      "No pre-existing test starts failing because of the removal"
    ]
  }
]
```
