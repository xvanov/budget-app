"""Flat category model.

Replaces the hierarchical ``Expense > Personal > Dining`` scheme with plain names
like ``Dining Out``. Two things fall out of that:

1. **Income vs expense is no longer stored in the category.** It is derived from
   the amount's sign: negative is spending, positive is money in. That removes a
   whole class of contradiction — the old model let a category say "Expense"
   while the amount said inflow, which is exactly the corruption
   ``sign_invariant`` existed to catch. It cannot happen when the sign IS the
   answer.

2. **A refund needs no special category.** A positive amount on ``Groceries`` is
   money back on groceries, which nets against that category automatically.

Transfers are the one exception and are kept deliberately, because they are not
a spending category at all — they are what stops internal movement from being
counted as spending. With all three Chase accounts linked, a checking-to-card
payment appears on both accounts; without a transfer category, every purchase on
the card would be counted twice.
"""
import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Categories whose rows are internal movement, never spending or income.
TRANSFER_CATEGORIES = ("Transfer", "Credit-card Payment", "Mortgage Payment")

# The opt-out bucket. Distinct from a transfer: a transfer is money moving between
# the owner's own accounts, whereas an excluded row is a REAL external
# transaction the owner has decided should not count — a bill fronted for someone
# who paid it back, a one-off that would distort a category. It leaves every
# total, chart and budget, but stays visible on its own tab so nothing is
# silently disappeared.
EXCLUDED_CATEGORY = "Excluded"
EXCLUDED_ENTITY_TYPE = "excluded"

# Category colours. A new category picks the least-used colour in this palette so
# it is visually distinct on day one, and the same colour is used everywhere the
# category appears (chart wedge, legend, transaction row) — a category whose
# colour changes between views is worse than no colour at all.
CATEGORY_PALETTE = [
    "#5b8def", "#4ade80", "#f59e0b", "#f43f5e", "#a78bfa", "#22d3ee",
    "#fb923c", "#84cc16", "#e879f9", "#14b8a6", "#facc15", "#38bdf8",
    "#fb7185", "#c084fc", "#2dd4bf", "#eab308", "#60a5fa", "#f472b6",
]


def pick_color(existing: "list[str]") -> str:
    """Least-used palette colour, so new categories stay visually distinct."""
    counts = {c: 0 for c in CATEGORY_PALETTE}
    for used in existing:
        if used in counts:
            counts[used] += 1
    return min(CATEGORY_PALETTE, key=lambda c: (counts[c], CATEGORY_PALETTE.index(c)))

# The default set. Deliberately short: a category list you can hold in your head
# is one you will actually keep tidy.
DEFAULT_CATEGORIES: List[str] = [
    "Groceries",
    "Restaurants",
    "Travel",
    "Shopping",
    "Subscriptions",
    "Utilities",
    "Housing",
    "Transport",
    "Health",
    "Entertainment",
    "Personal Care",
    "Education",
    "Fees & Interest",
    "Business",
    "Gifts & Donations",
    "Taxes",
    "Salary",
    "Rental Income",
    "Other Income",
    "Other",
    *TRANSFER_CATEGORIES,
    EXCLUDED_CATEGORY,
]

# Old hierarchical path -> flat name. Longest-prefix wins, so specific paths beat
# their parents.
MIGRATION_MAP: Dict[str, str] = {
    # Income
    "Income > Software Job": "Salary",
    "Income > Rentals": "Rental Income",
    "Income > Contracting Business": "Business",
    "Income > Investment / Interest": "Other Income",
    "Income > Refunds": "Other Income",
    "Income > Other Income": "Other Income",
    "Income": "Other Income",
    # Housing / utilities
    "Expense > Housing > Rent": "Housing",
    "Expense > Housing > Mortgage Interest": "Housing",
    "Expense > Housing > Mortgage Taxes": "Taxes",
    "Expense > Housing > Mortgage Insurance": "Housing",
    "Expense > Housing > Utilities": "Utilities",
    "Expense > Housing > Maintenance": "Housing",
    "Expense > Housing > Other": "Housing",
    "Expense > Housing": "Housing",
    # Rental property expenses collapse into Housing — the owner asked for the
    # rental split to go away.
    "Expense > Rental > Utilities": "Utilities",
    "Expense > Rental > Maintenance": "Housing",
    "Expense > Rental > Property Tax": "Taxes",
    "Expense > Rental > Insurance": "Housing",
    "Expense > Rental > Management Fees": "Housing",
    "Expense > Rental > Rent": "Housing",
    "Expense > Rental > Other": "Housing",
    "Expense > Rental": "Housing",
    # Personal
    "Expense > Personal > Groceries": "Groceries",
    "Expense > Personal > Dining": "Restaurants",
    "Expense > Personal > Healthcare": "Health",
    "Expense > Personal > Subscriptions": "Subscriptions",
    "Expense > Personal > Shopping": "Shopping",
    "Expense > Personal > Other": "Other",
    "Expense > Personal": "Other",
    # Leisure
    "Expense > Leisure > Travel": "Travel",
    "Expense > Leisure > Entertainment": "Entertainment",
    "Expense > Leisure > Dining Out": "Restaurants",
    "Expense > Leisure > Groceries": "Groceries",
    "Expense > Leisure": "Entertainment",
    # Business
    "Expense > Business > Contracting Costs": "Business",
    "Expense > Business > Software / Tools": "Business",
    "Expense > Business > Other": "Business",
    "Expense > Business": "Business",
    # Misc
    "Expense > Fees & Interest": "Fees & Interest",
    "Expense > Tax > Federal Income Tax": "Taxes",
    "Expense > Tax > State Income Tax": "Taxes",
    "Expense > Tax > Social Security Tax": "Taxes",
    "Expense > Tax > Medicare Tax": "Taxes",
    "Expense > Tax": "Taxes",
    "Expense > Other": "Other",
    "Expense": "Other",
    # Transfers
    "Transfer > Account-to-account": "Transfer",
    "Transfer > Cash Out": "Transfer",
    "Transfer > Credit-card Payment": "Credit-card Payment",
    "Transfer > Mortgage Principal": "Mortgage Payment",
    "Transfer > Mortgage Escrow": "Mortgage Payment",
    "Transfer > Mortgage Payment (Split)": "Transfer",
    "Transfer > Amazon Order (Split)": "Transfer",
    "Transfer": "Transfer",
    "Uncategorized": None,  # stays uncategorized
}


def flatten_path(path: Optional[str], known: Optional[set] = None) -> Optional[str]:
    """Map a legacy hierarchical path to a flat category name.

    Order matters, and it cost real data to learn why:

    1. An exact entry in ``MIGRATION_MAP`` always wins — those are hand-chosen
       (``Expense > Personal > Dining`` really does belong in ``Restaurants``).
    2. Otherwise, if the LAST segment is already a known flat category, use it.
       This is the case the original implementation got wrong. A path like
       ``Expense > Groceries`` has no exact entry, so the longest-prefix scan
       below matched the generic ``"Expense" -> "Other"`` rule and dumped 287
       Target purchases the user had just filed as Groceries into ``Other``.
       The leaf is the specific answer; a parent prefix is only a fallback.
    3. Then the longest matching prefix, for genuinely unknown leaves
       (``Expense > Housing > Some New Thing`` -> ``Housing``).
    4. Finally the bare last segment, so a category invented after this map was
       written still lands somewhere sensible rather than vanishing.

    ``known`` lets a caller widen step 2 to the categories that actually exist in
    this user's DB, not just the shipped defaults. Without it, a hierarchical
    ``Expense > Pets`` would fall through to ``Other`` even though the user has a
    ``Pets`` category — the same class of loss as the Groceries case above.
    """
    if not path:
        return None
    path = path.strip()
    if path in MIGRATION_MAP:
        return MIGRATION_MAP[path]
    if " > " not in path:
        return path  # already flat
    leaf = path.split(" > ")[-1].strip()
    if leaf in DEFAULT_CATEGORIES or (known and leaf in known):
        return leaf
    # Longest matching prefix.
    for candidate in sorted(MIGRATION_MAP, key=len, reverse=True):
        if path.startswith(candidate + " > ") or path == candidate:
            return MIGRATION_MAP[candidate]
    return leaf


def is_transfer_category(name: Optional[str]) -> bool:
    return (name or "") in TRANSFER_CATEGORIES


def is_excluded_category(name: Optional[str]) -> bool:
    return (name or "") == EXCLUDED_CATEGORY


def entity_type_for(category: Optional[str], amount_cents: int) -> str:
    """Income vs expense comes from the SIGN; transfers come from the category.

    This is the whole point of the flat model: a positive amount is money in, a
    negative amount is money out, and no category can contradict that.
    """
    if is_excluded_category(category):
        return EXCLUDED_ENTITY_TYPE
    if is_transfer_category(category):
        return "transfer"
    return "income" if int(amount_cents or 0) > 0 else "expense"


async def seed_flat_categories(db) -> Dict[str, int]:
    """Insert the default flat categories. Idempotent; never deletes."""
    cursor = await db.execute("SELECT path FROM categories")
    existing = {r[0] for r in await cursor.fetchall()}
    cursor = await db.execute("SELECT color FROM categories WHERE color IS NOT NULL")
    used = [r[0] for r in await cursor.fetchall()]
    added = 0
    for index, name in enumerate(DEFAULT_CATEGORIES):
        if name in existing:
            continue
        color = CATEGORY_PALETTE[index % len(CATEGORY_PALETTE)]
        await db.execute(
            "INSERT INTO categories (name, parent_id, path, sort_order, color) VALUES (?, NULL, ?, ?, ?)",
            (name, name, index, color),
        )
        used.append(color)
        added += 1

    # Backfill any category that predates the colour column.
    cursor = await db.execute("SELECT id FROM categories WHERE color IS NULL ORDER BY id")
    for row in await cursor.fetchall():
        color = pick_color(used)
        await db.execute("UPDATE categories SET color=? WHERE id=?", (color, row[0]))
        used.append(color)
    await db.commit()
    return {"added": added, "total_defaults": len(DEFAULT_CATEGORIES)}


async def migrate_to_flat(db) -> Dict[str, object]:
    """Rewrite transactions, budgets and the category table to flat names.

    Idempotent: a row already flat is left alone.
    """
    await seed_flat_categories(db)

    # The user's own flat categories count as valid destinations, so a
    # hierarchical path ending in one of them keeps its meaning.
    cursor = await db.execute("SELECT path FROM categories WHERE path NOT LIKE '% > %'")
    known = {r[0] for r in await cursor.fetchall()}

    cursor = await db.execute(
        "SELECT DISTINCT category_path FROM transactions WHERE category_path IS NOT NULL"
    )
    paths = [r[0] for r in await cursor.fetchall()]

    remapped: Dict[str, Optional[str]] = {}
    updated_rows = 0
    for path in paths:
        flat = flatten_path(path, known)
        if flat == path:
            continue
        remapped[path] = flat
        if flat is None:
            cursor = await db.execute(
                "UPDATE transactions SET category_path='Uncategorized' WHERE category_path=?",
                (path,),
            )
        else:
            cursor = await db.execute(
                "UPDATE transactions SET category_path=? WHERE category_path=?", (flat, path)
            )
        updated_rows += cursor.rowcount or 0

    # entity_type is now a function of sign + transfer-ness, so recompute it for
    # everything. This is what makes refunds work without a special category.
    placeholders = ",".join("?" * len(TRANSFER_CATEGORIES))
    await db.execute(
        f"UPDATE transactions SET entity_type='transfer' WHERE category_path IN ({placeholders})",
        TRANSFER_CATEGORIES,
    )
    await db.execute(
        "UPDATE transactions SET entity_type=? WHERE category_path=?",
        (EXCLUDED_ENTITY_TYPE, EXCLUDED_CATEGORY),
    )
    await db.execute(
        f"""UPDATE transactions SET entity_type = CASE WHEN amount_cents > 0 THEN 'income' ELSE 'expense' END
            WHERE category_path IS NOT NULL
              AND category_path != 'Uncategorized'
              AND category_path != '{EXCLUDED_CATEGORY}'
              AND category_path NOT IN ({placeholders})""",
        TRANSFER_CATEGORIES,
    )

    # Budgets point at category paths too.
    cursor = await db.execute("SELECT id, category_path FROM budgets")
    for row in await cursor.fetchall():
        flat = flatten_path(row[1], known)
        if flat and flat != row[1]:
            await db.execute("UPDATE budgets SET category_path=? WHERE id=?", (flat, row[0]))

    # Rules assign categories on import, so a legacy rule would keep writing
    # hierarchical paths back into new data.
    cursor = await db.execute("SELECT id, category_path FROM rules")
    rules_updated = 0
    for row in await cursor.fetchall():
        flat = flatten_path(row[1], known)
        if flat and flat != row[1]:
            await db.execute("UPDATE rules SET category_path=? WHERE id=?", (flat, row[0]))
            rules_updated += 1

    # Drop the old hierarchical rows from the category table.
    cursor = await db.execute("DELETE FROM categories WHERE path LIKE '% > %'")
    removed_categories = cursor.rowcount or 0

    await db.commit()
    result = {
        "paths_remapped": len(remapped),
        "transactions_updated": updated_rows,
        "hierarchical_categories_removed": removed_categories,
        "rules_updated": rules_updated,
        "mapping": remapped,
    }
    logger.info(
        "Flat category migration: %d paths -> flat, %d transactions updated, %d old categories removed",
        len(remapped), updated_rows, removed_categories,
    )
    return result
