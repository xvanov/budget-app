from fastapi import APIRouter, HTTPException
from typing import Optional, List
from pydantic import BaseModel
from app.database import get_db
from app.services.rules_service import determine_entity_type

router = APIRouter()

async def _next_color(db) -> str:
    """Least-used palette colour, so a new category is visually distinct."""
    from app.services.flat_categories import pick_color

    cursor = await db.execute("SELECT color FROM categories WHERE color IS NOT NULL")
    return pick_color([r[0] for r in await cursor.fetchall()])



async def ensure_category_path(db, path: str) -> int:
    """Idempotently ensure every segment of ``path`` exists in the categories
    table. Returns the leaf category id.

    A ``path`` like ``"Expense > Tax > Federal Income Tax"`` will:
      * create ``Expense`` if missing (parent NULL),
      * create ``Expense > Tax`` if missing (parent=Expense),
      * create ``Expense > Tax > Federal Income Tax`` if missing.

    Existing nodes are reused — no duplicates created. ``db.commit()`` is
    called once at the end; the caller is responsible for opening / closing
    the connection.

    Used by ``chat_correct`` so the LLM can reference brand-new category paths
    in a re-categorize action without first issuing a separate create call.
    """
    if not path or not path.strip():
        raise ValueError("ensure_category_path: empty path")
    parts = [p.strip() for p in path.split(">")]
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError(f"ensure_category_path: no segments in {path!r}")
    parent_id: Optional[int] = None
    current_path = ""
    leaf_id: Optional[int] = None
    for part in parts:
        current_path = f"{current_path} > {part}" if current_path else part
        if parent_id is None:
            cursor = await db.execute(
                "SELECT id FROM categories WHERE name=? AND parent_id IS NULL",
                (part,),
            )
        else:
            cursor = await db.execute(
                "SELECT id FROM categories WHERE name=? AND parent_id=?",
                (part, parent_id),
            )
        row = await cursor.fetchone()
        if row:
            node_id = row[0]
        else:
            cursor = await db.execute(
                "INSERT INTO categories (name, parent_id, path, color) VALUES (?, ?, ?, ?)",
                (part, parent_id, current_path, await _next_color(db)),
            )
            node_id = cursor.lastrowid
        parent_id = node_id
        leaf_id = node_id
    await db.commit()
    return leaf_id  # type: ignore[return-value]


async def ensure_flat_category(db, name: str) -> int:
    """Idempotently ensure a single FLAT category named ``name`` exists.

    The flat model has no hierarchy, so there is exactly one row per category:
    ``parent_id`` is NULL and ``path == name``. This is the counterpart of
    ``ensure_category_path`` for callers that must never invent a hierarchy —
    notably ``chat_correct``, which used to run LLM-supplied paths through
    ``ensure_category_path`` and so created junk rows like ``Expense`` and
    ``Expense > Groceries`` in the live DB.

    Returns the category id. Colour comes from the same least-used-palette
    picker every other insert uses, so a new category is distinct on day one.
    """
    flat = (name or "").strip()
    if not flat:
        raise ValueError("ensure_flat_category: empty name")
    if " > " in flat:
        raise ValueError(f"ensure_flat_category: {flat!r} is a hierarchical path")
    cursor = await db.execute(
        "SELECT id FROM categories WHERE path=? OR (name=? AND parent_id IS NULL) "
        "ORDER BY id LIMIT 1",
        (flat, flat),
    )
    row = await cursor.fetchone()
    if row:
        return row[0]
    cursor = await db.execute(
        "INSERT INTO categories (name, parent_id, path, color) VALUES (?, NULL, ?, ?)",
        (flat, flat, await _next_color(db)),
    )
    new_id = cursor.lastrowid
    await db.commit()
    return new_id


class CategoryCreate(BaseModel):
    name: str
    parent_id: Optional[int] = None


class CategoryUpdate(BaseModel):
    name: Optional[str] = None
    parent_id: Optional[int] = None


class CategoryOut(BaseModel):
    id: int
    name: str
    parent_id: Optional[int]
    path: str
    sort_order: int
    children: List["CategoryOut"] = []


def _build_tree(rows):
    by_id = {}
    for r in rows:
        by_id[r["id"]] = {
            "id": r["id"],
            "name": r["name"],
            "parent_id": r["parent_id"],
            "path": r["path"],
            "sort_order": r["sort_order"],
            "children": [],
        }
    roots = []
    for node in by_id.values():
        pid = node["parent_id"]
        if pid is None:
            roots.append(node)
        elif pid in by_id:
            by_id[pid]["children"].append(node)
    return roots


async def _recompute_path(db, cat_id: int) -> str:
    cursor = await db.execute(
        "WITH RECURSIVE cat_path AS ("
        "  SELECT id, name, parent_id, name AS path_str FROM categories WHERE id=?"
        "  UNION ALL"
        "  SELECT c.id, c.name, c.parent_id, c.name || ' > ' || cp.path_str"
        "  FROM categories c JOIN cat_path cp ON c.id = cp.parent_id"
        ") SELECT path_str FROM cat_path WHERE parent_id IS NULL",
        (cat_id,),
    )
    row = await cursor.fetchone()
    if row:
        return row[0]
    cursor2 = await db.execute("SELECT name FROM categories WHERE id=?", (cat_id,))
    r = await cursor2.fetchone()
    return r[0] if r else ""


@router.get("/api/categories/sign-violations")
async def list_sign_violations():
    """Return transactions whose ``entity_type`` is incoherent with either
    the ``amount_cents`` sign or the ``category_path`` family.

    Two failure modes are surfaced:

      1. Sign vs. entity_type:
         * ``entity_type='expense'`` must have ``amount_cents <= 0``.
         * ``entity_type='income'``  must have ``amount_cents >= 0``.
         * Transfer / asset_conversion / liability_payment allow either sign.

      2. category_path vs. entity_type:
         * ``Income > …``   ⇒ entity_type must be ``income``.
         * ``Expense > …``  ⇒ entity_type must be ``expense``.
         * ``Transfer > …`` ⇒ entity_type ∈ {transfer, asset_conversion,
           liability_payment}.

    Used to find historical violations introduced by bulk SQL updates that
    skipped either the sign filter or the path filter.
    """
    from app.services.sign_invariant import violates_sign_invariant

    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT id, posted_date, amount_cents, description, merchant,
                      category_path, entity_type, business_context
               FROM transactions
               ORDER BY id"""
        )
        rows = await cursor.fetchall()
        items = [
            dict(r) for r in rows
            if violates_sign_invariant(
                r["entity_type"],
                r["amount_cents"] or 0,
                r["category_path"],
            )
        ]
        return {"count": len(items), "items": items}
    finally:
        await db.close()


@router.get("/api/categories")
async def list_categories():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM categories ORDER BY path")
        rows = await cursor.fetchall()
        return {"categories": [dict(r) for r in rows]}
    finally:
        await db.close()


@router.get("/api/categories/tree")
async def get_category_tree():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM categories ORDER BY sort_order, id")
        rows = await cursor.fetchall()
        tree = _build_tree(rows)
        return {"tree": tree}
    finally:
        await db.close()


@router.post("/api/categories", status_code=201)
async def create_category(cat: CategoryCreate):
    from app.services.flat_categories import pick_color

    db = await get_db()
    try:
        if cat.parent_id is not None:
            cursor = await db.execute(
                "SELECT id, path FROM categories WHERE id=?", (cat.parent_id,)
            )
            parent = await cursor.fetchone()
            if not parent:
                raise HTTPException(status_code=404, detail="Parent category not found")
            parent_path = parent["path"]
            new_path = f"{parent_path} > {cat.name}"
        else:
            new_path = cat.name

        # A duplicate path is not a harmless extra row. Every category list in the
        # UI is keyed by path, so two rows with the same path collide as React keys
        # and only one chip renders — the other is invisible and undeletable. It
        # also makes "which category is this?" ambiguous for every lookup that
        # resolves a path to an id.
        name = (cat.name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="Category name is required")
        cursor = await db.execute(
            "SELECT id FROM categories WHERE path=? OR (LOWER(name)=LOWER(?) AND "
            "((parent_id IS NULL AND ? IS NULL) OR parent_id=?))",
            (new_path, name, cat.parent_id, cat.parent_id),
        )
        if await cursor.fetchone():
            raise HTTPException(
                status_code=409, detail=f"A category named '{new_path}' already exists",
            )

        cursor = await db.execute(
            "INSERT INTO categories (name, parent_id, path, color) VALUES (?, ?, ?, ?)",
            (name, cat.parent_id, new_path, await _next_color(db)),
        )
        new_id = cursor.lastrowid
        await db.commit()

        cursor = await db.execute("SELECT * FROM categories WHERE id=?", (new_id,))
        row = await cursor.fetchone()
        return dict(row)
    finally:
        await db.close()


@router.put("/api/categories/{cat_id}")
async def update_category(cat_id: int, update: CategoryUpdate):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM categories WHERE id=?", (cat_id,))
        existing = await cursor.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Category not found")

        new_name = update.name if update.name is not None else existing["name"]
        new_parent_id = update.parent_id if update.parent_id is not None else existing["parent_id"]

        if update.parent_id is not None:
            if update.parent_id == cat_id:
                raise HTTPException(status_code=400, detail="Cannot set self as parent")
            cursor = await db.execute(
                "SELECT id, path FROM categories WHERE id=?", (update.parent_id,)
            )
            parent = await cursor.fetchone()
            if not parent:
                raise HTTPException(status_code=404, detail="Parent category not found")
            parent_path = parent["path"]
            new_path = f"{parent_path} > {new_name}"
        elif update.name is not None:
            parent_id = existing["parent_id"]
            if parent_id is not None:
                cursor = await db.execute(
                    "SELECT path FROM categories WHERE id=?", (parent_id,)
                )
                parent_row = await cursor.fetchone()
                parent_path = parent_row[0] if parent_row else ""
                new_path = f"{parent_path} > {new_name}"
            else:
                new_path = new_name
        else:
            new_path = existing["path"]

        await db.execute(
            "UPDATE categories SET name=?, parent_id=?, path=? WHERE id=?",
            (new_name, new_parent_id, new_path, cat_id),
        )

        if new_path != existing["path"]:
            cursor = await db.execute(
                "SELECT id FROM categories WHERE path LIKE ?",
                (existing["path"] + " > %",),
            )
            descendants = await cursor.fetchall()
            for desc in descendants:
                old_pfx = existing["path"] + " > "
                new_pfx = new_path + " > "
                cursor2 = await db.execute(
                    "SELECT path FROM categories WHERE id=?", (desc["id"],)
                )
                old_desc_path = (await cursor2.fetchone())[0]
                new_desc_path = new_pfx + old_desc_path[len(old_pfx):]
                await db.execute(
                    "UPDATE categories SET path=? WHERE id=?",
                    (new_desc_path, desc["id"]),
                )

        await db.commit()

        cursor = await db.execute("SELECT * FROM categories WHERE id=?", (cat_id,))
        row = await cursor.fetchone()
        return dict(row)
    finally:
        await db.close()


@router.delete("/api/categories/{cat_id}")
async def delete_category(cat_id: int, reassign_to: str = "", force: bool = False):
    """Delete a category.

    A category in use cannot simply vanish — the transactions filed under it would
    be orphaned and silently drop out of every total. So:

      * ``reassign_to`` moves its transactions (and those of its descendants) to
        another existing category first. This is the safe path.
      * ``force=true`` sets them to Uncategorized instead.
      * With neither, the request is refused and reports how many transactions
        (and child categories) are in the way.
    """
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM categories WHERE id=?", (cat_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Category not found")
        category = dict(row)
        path = category["path"]

        cursor = await db.execute(
            "SELECT id, path FROM categories WHERE path = ? OR path LIKE ?",
            (path, f"{path} > %"),
        )
        subtree = [dict(r) for r in await cursor.fetchall()]
        paths = [c["path"] for c in subtree]

        placeholders = ",".join("?" * len(paths))
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM transactions WHERE category_path IN ({placeholders})", paths
        )
        in_use = (await cursor.fetchone())[0]

        if in_use and not reassign_to and not force:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{path}' is used by {in_use} transaction(s)"
                    + (f" across {len(subtree)} categories (including children)" if len(subtree) > 1 else "")
                    + ". Pass reassign_to=<category path> to move them, or force=true to set them "
                      "Uncategorized."
                ),
            )

        moved = 0
        if in_use:
            if reassign_to:
                cursor = await db.execute("SELECT path FROM categories WHERE path=?", (reassign_to,))
                if not await cursor.fetchone():
                    raise HTTPException(
                        status_code=400, detail=f"reassign_to '{reassign_to}' is not an existing category"
                    )
                # entity_type follows the AMOUNT'S SIGN in the flat model, not the
                # category — except for the transfer/excluded buckets, which the
                # category alone decides. Deriving it from `reassign_to` for the
                # whole batch stamped one type on every row, so reassigning into
                # e.g. Groceries recorded a refund (positive amount) as an expense
                # and it went missing from income.
                from app.services.flat_categories import (
                    EXCLUDED_CATEGORY, EXCLUDED_ENTITY_TYPE, is_transfer_category,
                )
                if is_transfer_category(reassign_to):
                    entity_sql, entity_params = "?", ["transfer"]
                elif reassign_to == EXCLUDED_CATEGORY:
                    entity_sql, entity_params = "?", [EXCLUDED_ENTITY_TYPE]
                else:
                    entity_sql, entity_params = (
                        "CASE WHEN amount_cents > 0 THEN 'income' ELSE 'expense' END", []
                    )
                cursor = await db.execute(
                    f"""UPDATE transactions
                        SET category_path=?, entity_type={entity_sql},
                            categorization_reason='Reassigned from ' || category_path || ' (category deleted)'
                        WHERE category_path IN ({placeholders})""",
                    (reassign_to, *entity_params, *paths),
                )
            else:
                # Same reasoning: a row forced to Uncategorized must not keep a
                # stale 'transfer'/'excluded' type, or it stays silently out of
                # every total while claiming to be uncategorized.
                cursor = await db.execute(
                    f"""UPDATE transactions
                        SET category_path='Uncategorized',
                            entity_type=CASE WHEN amount_cents > 0 THEN 'income' ELSE 'expense' END,
                            categorization_reason='Category ' || category_path || ' was deleted'
                        WHERE category_path IN ({placeholders})""",
                    paths,
                )
            moved = cursor.rowcount or 0

        for child in sorted(subtree, key=lambda c: -len(c["path"])):
            await db.execute("DELETE FROM categories WHERE id=?", (child["id"],))
        await db.commit()

        return {
            "deleted": path,
            "categories_removed": len(subtree),
            "transactions_moved": moved,
            "moved_to": reassign_to or ("Uncategorized" if in_use else None),
        }
    finally:
        await db.close()


@router.post("/api/categories/flatten")
async def flatten_categories():
    """Migrate to the flat category model (idempotent).

    Rewrites hierarchical paths to flat names, recomputes entity_type from the
    amount sign, remaps budgets, and removes the old hierarchical rows.
    """
    from app.services.flat_categories import migrate_to_flat

    db = await get_db()
    try:
        result = await migrate_to_flat(db)
        # The mapping can be large; summarise it.
        mapping = result.pop("mapping", {})
        result["sample_mapping"] = dict(list(mapping.items())[:25])
        return result
    finally:
        await db.close()


class CategoryColorPatch(BaseModel):
    color: str


@router.patch("/api/categories/{cat_id}/color")
async def set_category_color(cat_id: int, patch: CategoryColorPatch):
    """Set a category's colour. Accepts any #rrggbb value."""
    import re as _re

    if not _re.fullmatch(r"#[0-9a-fA-F]{6}", patch.color or ""):
        raise HTTPException(status_code=400, detail="color must be a #rrggbb hex value")
    db = await get_db()
    try:
        cursor = await db.execute("SELECT 1 FROM categories WHERE id=?", (cat_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Category not found")
        await db.execute("UPDATE categories SET color=? WHERE id=?", (patch.color, cat_id))
        await db.commit()
        cursor = await db.execute("SELECT * FROM categories WHERE id=?", (cat_id,))
        return dict(await cursor.fetchone())
    finally:
        await db.close()


@router.get("/api/categories/colors")
async def category_colors():
    """path -> colour, for clients that render category chips."""
    from app.services.flat_categories import CATEGORY_PALETTE

    db = await get_db()
    try:
        cursor = await db.execute("SELECT path, color FROM categories WHERE color IS NOT NULL")
        return {
            "colors": {r[0]: r[1] for r in await cursor.fetchall()},
            "palette": CATEGORY_PALETTE,
        }
    finally:
        await db.close()
