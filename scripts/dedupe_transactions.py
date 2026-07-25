#!/usr/bin/env python3
"""Recompute normalized_description and collapse fingerprint duplicates.

Why this exists
---------------
``normalized_description`` is a stored column and half of the dedup fingerprint
``(account_id, posted_date, amount_cents, normalized_description)``. When
``BaseParser.normalize_description`` changes — or when a parser starts emitting
a differently-quoted description for the same row, which is exactly what
``parsers/bofa.py`` did with BofA's unescaped inner double-quotes — previously
imported rows keep their OLD fingerprint. A re-import then fails to recognise
them and inserts a second copy of a transaction that was already there.

Re-importing samples/ on 2026-07-24 did this: 337 phantom ``bofa-stmt`` rows.

What it does
------------
1. Recomputes ``normalized_description`` for every row with the CURRENT
   normalizer, so old and new rows are comparable again.
2. Within each duplicate fingerprint group, keeps the LOWEST id and deletes the
   rest. Lowest id is deliberate: it is the oldest row, the one carrying the
   user's categorization, corrections and any transfer/paystub links.

Safety
------
- Idempotent: a second run finds nothing to do.
- Refuses to delete a row that is referenced by ``transfers``,
  ``paystubs.linked_transaction_id`` or another row's
  ``linked_transaction_id`` — those are reported instead, never silently cut.
- ``--dry-run`` prints the plan and writes nothing. Run it first.

Usage
-----
    docker compose exec -T backend python3 /app/scripts/dedupe_transactions.py --dry-run
    docker compose exec -T backend python3 /app/scripts/dedupe_transactions.py
"""
import argparse
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, "/app")
try:
    from app.parsers.base import BaseParser

    normalize = BaseParser.normalize_description
except ImportError:  # running outside the container / image layout differs
    import re

    def normalize(desc: str) -> str:
        s = desc.lower().strip()
        s = re.sub(r'["\']', "", s)
        s = re.sub(r"\s+", " ", s)
        s = re.sub(r"#\d+", "", s)
        s = re.sub(r"\*+", "", s)
        s = re.sub(r"\d{8,}", "", s)
        return s.strip()


def referenced_ids(conn, ids):
    """Ids that other tables point at — never safe to delete."""
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    args = list(ids)
    referenced = set()
    for sql in (
        f"SELECT from_transaction_id FROM transfers WHERE from_transaction_id IN ({placeholders})",
        f"SELECT to_transaction_id FROM transfers WHERE to_transaction_id IN ({placeholders})",
        f"SELECT linked_transaction_id FROM transactions WHERE linked_transaction_id IN ({placeholders})",
        f"SELECT linked_transaction_id FROM paystubs WHERE linked_transaction_id IN ({placeholders})",
    ):
        referenced.update(r[0] for r in conn.execute(sql, args) if r[0] is not None)
    return referenced


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--db", default=os.environ.get("DB_PATH", "/data/budget.db"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT id, account_id, posted_date, amount_cents, description, "
            "normalized_description, category_path, created_at FROM transactions"
        )
    ]
    print(f"{len(rows)} transactions in {args.db}")

    # 1. Renormalize.
    restated = [
        (normalize(r["description"] or ""), r["id"])
        for r in rows
        if normalize(r["description"] or "") != (r["normalized_description"] or "")
    ]
    print(f"normalized_description differs for {len(restated)} rows")
    if restated and not args.dry_run:
        conn.executemany("UPDATE transactions SET normalized_description=? WHERE id=?", restated)
        conn.commit()

    # 2. Group by fingerprint under the current normalizer.
    groups = defaultdict(list)
    for r in rows:
        key = (
            r["account_id"],
            r["posted_date"],
            r["amount_cents"],
            normalize(r["description"] or ""),
        )
        groups[key].append(r)

    victims, blocked = [], []
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: r["id"])
        victims.extend(r["id"] for r in group[1:])  # keep the oldest

    protected = referenced_ids(conn, victims)
    if protected:
        blocked = sorted(protected)
        victims = [i for i in victims if i not in protected]

    print(f"duplicate rows to delete: {len(victims)}")
    if blocked:
        print(f"SKIPPED {len(blocked)} referenced by transfers/paystubs/links: {blocked}")

    if victims:
        sample = ",".join(map(str, victims[:5]))
        for r in conn.execute(
            f"SELECT id, account_id, posted_date, amount_cents, substr(description,1,55) d "
            f"FROM transactions WHERE id IN ({sample})"
        ):
            print("  delete", dict(r))

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return

    if victims:
        conn.executemany("DELETE FROM transactions WHERE id=?", [(i,) for i in victims])
        conn.commit()
        print(f"deleted {len(victims)} rows")

    remaining = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    print(f"{remaining} transactions remain")


if __name__ == "__main__":
    main()
