"""Account identity — display name and kind — as DATA, not as source code.

Account ids used to be a module-level dict in ``routers/net_worth.py``, which
meant the owner's real account numbers and their private labels ("Primary
Residence") were part of the application. They are now rows in ``account_meta``.

Three rules keep this honest:

1. **Nothing seeds itself from a hardcoded list of ids.** Defaults are DERIVED
   from what is already in ``transactions`` — the id supplies the name, the
   shape of the rows supplies the kind. A fresh install of this app on someone
   else's data produces sensible accounts with no code change.
2. **``real_estate`` is never inferred.** It is not visible in transaction data:
   an account whose id happens to be a mortgage-servicing checking account can
   be the owner's "Primary Residence" line in net worth, and only the owner
   knows that. It arrives via ``POST /api/accounts/meta`` (or the
   ``ACCOUNT_META_JSON`` override) and changes the net-worth arithmetic, so
   getting it from data rather than a guess matters.
3. **Anything derived is overridable.** Every field can be replaced per account
   without touching the repo.
"""

import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Kinds the net-worth arithmetic understands. `real_estate` carries a separate
# `debt` leg; `credit` flips a negative balance into a liability.
ALLOWED_KINDS = ("checking", "savings", "credit", "brokerage", "retirement", "real_estate")
DEFAULT_KIND = "checking"

# Account ids the app itself manufactures — paystub components and hand-entered
# rows. They describe money that is already counted elsewhere, so they are
# seeded out of net worth. This is a default for seeding only: the stored
# `include_in_net_worth` flag is what net worth actually reads.
SYNTHETIC_ACCOUNTS = frozenset({"payroll", "manual"})
SYNTHETIC_PLATFORMS = frozenset({"paystub"})

# A credit card's rows are overwhelmingly charges, with a thin stream of
# payments and refunds going the other way, and the running total sits below
# zero because the balance IS the debt. A checking account interleaves
# deposits with spending. Measured on real multi-bank exports the two
# credit cards sat at 0.91 and 0.93 debit share against 0.28–0.80 for the
# five checking accounts, so the boundary is drawn at 0.85 with a row-count
# floor so a barely-populated account is not classified off two rows.
CREDIT_DEBIT_SHARE = 0.85
CREDIT_MIN_ROWS = 10


def derive_display_name(account_id: str) -> str:
    """A readable label out of the id itself: ``bank-1234`` -> ``Bank 1234``."""
    tokens = [t for t in re.split(r"[-_\s]+", account_id or "") if t]
    if not tokens:
        return account_id or "Account"
    return " ".join(t if t.isdigit() else t[:1].upper() + t[1:] for t in tokens)


def infer_kind(row_count: int, debit_count: int, net_cents: int) -> str:
    """Guess `checking` vs `credit` from the rows themselves.

    Never returns `real_estate` — see the module docstring.
    """
    if row_count >= CREDIT_MIN_ROWS and net_cents < 0:
        if debit_count / row_count >= CREDIT_DEBIT_SHARE:
            return "credit"
    return DEFAULT_KIND


def normalize_kind(kind: Optional[str]) -> str:
    k = (kind or "").strip().lower()
    return k if k in ALLOWED_KINDS else DEFAULT_KIND


async def derive_defaults(db) -> List[Dict[str, Any]]:
    """Default meta for every account_id that appears in `transactions`."""
    cursor = await db.execute(
        """SELECT account_id,
                  COUNT(*) AS n,
                  SUM(CASE WHEN amount_cents < 0 THEN 1 ELSE 0 END) AS debits,
                  SUM(amount_cents) AS net_cents,
                  (SELECT t2.source_platform FROM transactions t2
                    WHERE t2.account_id = t.account_id
                    GROUP BY t2.source_platform
                    ORDER BY COUNT(*) DESC LIMIT 1) AS platform
             FROM transactions t
            WHERE account_id IS NOT NULL AND account_id != ''
            GROUP BY account_id
            ORDER BY account_id"""
    )
    out = []
    for row in await cursor.fetchall():
        aid = row["account_id"]
        platform = row["platform"]
        synthetic = aid in SYNTHETIC_ACCOUNTS or platform in SYNTHETIC_PLATFORMS
        out.append({
            "account_id": aid,
            "display_name": derive_display_name(aid),
            "kind": infer_kind(row["n"] or 0, row["debits"] or 0, row["net_cents"] or 0),
            "institution": platform,
            "include_in_net_worth": 0 if synthetic else 1,
            "sort_order": 0,
            "derived": True,
        })
    return out


async def seed_account_meta(db) -> int:
    """Insert a derived default row for every unmapped account. Idempotent.

    Existing rows are never overwritten — the owner's edits and any applied
    backfill outrank a derivation.
    """
    cursor = await db.execute("SELECT account_id FROM account_meta")
    known = {r["account_id"] for r in await cursor.fetchall()}
    inserted = 0
    for d in await derive_defaults(db):
        if d["account_id"] in known:
            continue
        await db.execute(
            """INSERT INTO account_meta
                 (account_id, display_name, kind, institution,
                  include_in_net_worth, sort_order)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (d["account_id"], d["display_name"], d["kind"], d["institution"],
             d["include_in_net_worth"], d["sort_order"]),
        )
        inserted += 1
    if inserted:
        await db.commit()
        logger.info("account_meta: seeded %d derived account(s)", inserted)
    return inserted


async def upsert_account_meta(db, entries: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    """Apply explicit account meta. Present fields win; absent fields persist.

    This is the load-as-data path: the labels and kinds a user cares about
    (including `real_estate`) come in here instead of living in the repo.
    """
    created = 0
    updated = 0
    for entry in entries:
        aid = (entry.get("account_id") or "").strip()
        if not aid:
            raise ValueError("account_id is required for every entry")

        cursor = await db.execute(
            "SELECT account_id FROM account_meta WHERE account_id = ?", (aid,)
        )
        exists = await cursor.fetchone() is not None

        if not exists:
            await db.execute(
                """INSERT INTO account_meta
                     (account_id, display_name, kind, institution,
                      include_in_net_worth, sort_order)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    aid,
                    entry.get("display_name") or derive_display_name(aid),
                    normalize_kind(entry.get("kind")),
                    entry.get("institution"),
                    1 if entry.get("include_in_net_worth", 1) else 0,
                    int(entry.get("sort_order") or 0),
                ),
            )
            created += 1
            continue

        sets, params = [], []
        if entry.get("display_name") is not None:
            sets.append("display_name = ?")
            params.append(str(entry["display_name"]))
        if entry.get("kind") is not None:
            sets.append("kind = ?")
            params.append(normalize_kind(entry["kind"]))
        if "institution" in entry:
            sets.append("institution = ?")
            params.append(entry["institution"])
        if entry.get("include_in_net_worth") is not None:
            sets.append("include_in_net_worth = ?")
            params.append(1 if entry["include_in_net_worth"] else 0)
        if entry.get("sort_order") is not None:
            sets.append("sort_order = ?")
            params.append(int(entry["sort_order"]))
        if sets:
            sets.append("updated_at = datetime('now')")
            params.append(aid)
            await db.execute(
                f"UPDATE account_meta SET {', '.join(sets)} WHERE account_id = ?",
                params,
            )
            updated += 1

    await db.commit()
    return {"created": created, "updated": updated}


def _entries_from_payload(payload: Any) -> List[Dict[str, Any]]:
    """Accept either {"<id>": {...}} or [{"account_id": ..., ...}]."""
    if isinstance(payload, dict) and "accounts" in payload:
        payload = payload["accounts"]
    if isinstance(payload, dict) and "account_id" in payload:
        payload = [payload]  # a single entry, not a map keyed by id
    if isinstance(payload, dict):
        entries = []
        for aid, meta in payload.items():
            if meta is not None and not isinstance(meta, dict):
                raise ValueError(f"Meta for {aid!r} must be an object")
            item = dict(meta or {})
            item["account_id"] = aid
            # Tolerate the old constant's key name.
            if "display_name" not in item and "name" in item:
                item["display_name"] = item.pop("name")
            entries.append(item)
        return entries
    if isinstance(payload, list):
        entries = []
        for item in payload:
            item = dict(item or {})
            if "display_name" not in item and "name" in item:
                item["display_name"] = item.pop("name")
            entries.append(item)
        return entries
    raise ValueError("Expected an object keyed by account_id, or a list of entries")


def entries_from_payload(payload: Any) -> List[Dict[str, Any]]:
    return _entries_from_payload(payload)


async def apply_env_override(db) -> int:
    """Load account meta from ``ACCOUNT_META_JSON`` if it is set.

    The value is either a path to a JSON file or inline JSON. This is how a
    user keeps private labels out of the repo but still under version control
    of their own choosing (a mounted file, a secret). Applied on every boot, so
    the file stays authoritative for the accounts it names.
    """
    raw = os.environ.get("ACCOUNT_META_JSON", "").strip()
    if not raw:
        return 0
    try:
        if raw.startswith("{") or raw.startswith("["):
            payload = json.loads(raw)
        else:
            with open(raw, "r", encoding="utf-8") as f:
                payload = json.load(f)
        entries = _entries_from_payload(payload)
        result = await upsert_account_meta(db, entries)
    except Exception as exc:
        logger.warning("ACCOUNT_META_JSON could not be applied: %s", exc)
        return 0
    total = result["created"] + result["updated"]
    logger.info("account_meta: applied %d account(s) from ACCOUNT_META_JSON", total)
    return total


async def load_account_meta(db) -> List[Dict[str, Any]]:
    """Every account the app knows about, stored rows first.

    Accounts that have transactions but no stored row still appear, carrying
    their derived defaults, so an account can never silently vanish from net
    worth just because seeding has not run yet.
    """
    cursor = await db.execute(
        """SELECT account_id, display_name, kind, institution,
                  include_in_net_worth, sort_order
             FROM account_meta
            ORDER BY sort_order, account_id"""
    )
    rows = [
        {
            "account_id": r["account_id"],
            "display_name": r["display_name"] or derive_display_name(r["account_id"]),
            "kind": normalize_kind(r["kind"]),
            "institution": r["institution"],
            "include_in_net_worth": bool(r["include_in_net_worth"]),
            "sort_order": r["sort_order"] or 0,
            "derived": False,
        }
        for r in await cursor.fetchall()
    ]
    known = {r["account_id"] for r in rows}
    # Deriving means aggregating the whole transactions table, and this runs on
    # every dashboard load, so only pay for it when an account is actually
    # unmapped. The DISTINCT reads the account_id index.
    cursor = await db.execute(
        "SELECT DISTINCT account_id FROM transactions "
        "WHERE account_id IS NOT NULL AND account_id != ''"
    )
    missing = {r[0] for r in await cursor.fetchall()} - known
    if missing:
        for d in await derive_defaults(db):
            if d["account_id"] in missing:
                rows.append({**d, "include_in_net_worth": bool(d["include_in_net_worth"])})
    return rows


async def credit_account_ids(db) -> List[str]:
    """Accounts classified `credit`, for heuristics that only make sense there.

    Falls back to derivation when nothing is stored yet, so credit-card logic
    does not quietly go dark on a database that has not been seeded.
    """
    return [a["account_id"] for a in await load_account_meta(db) if a["kind"] == "credit"]


async def account_ids_by_kinds(db, kinds: Iterable[str]) -> List[str]:
    wanted = {normalize_kind(k) for k in kinds}
    return [a["account_id"] for a in await load_account_meta(db) if a["kind"] in wanted]
