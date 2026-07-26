"""Spending rolled up per VENDOR, for the "who did I pay?" slice of the ledger.

The dashboard has always answered "what did I spend it on?" (category) and "when
did I spend it?" (period). This module answers the third question the owner asked
for: *which payee took the money*, composable with the other two — "Groceries at
Costco, all time" and "Groceries at Costco, this month" are the same query with a
different window.

Nothing here categorizes anything. Every row already carries the category the LLM
assigned at ingest; this only GROUPS already-categorized rows for display, which
is why a deterministic key is allowed where a keyword map would not be (see the
hard rule in ``AGENTS.md``).

Merchant identity
-----------------
The raw ``merchant`` column is not an identity. The same payee arrives as
``Amazon``, ``Amazon: Fresh Step Cat Litter`` (an item row this app itself wrote
when it split a combined charge), ``SHEIN.COM DE 02/18`` and ``Shein``, or with
the payee only in the description (``merchant='Zelle'`` /
``description='Zelle payment to Sofia …'``). ``services/merchant_key`` already
solves most of that and has been running against this feed, so it is reused
verbatim rather than re-invented. Two things are layered on top:

1. **Detail and domain suffixes.** ``amazon_service`` renames a split item row to
   ``f"Amazon: {product_name[:60]}"``. Keyed as-is, every product becomes its own
   merchant — 745 Amazon rows in the live snapshot shattered into ~1,000 singleton
   "merchants", which is exactly the opposite of a vendor view. A domain suffix
   does the same thing more quietly: ``SHEIN.COM DE 03/06`` keys to
   ``"shein com"`` and ``Shein`` to ``"shein"``, two "vendors" for one shop.

   So the merchant text is cut at the first ``:`` or ``.``, and only that head is
   keyed — but ONLY when the head still yields a non-empty key, which is what
   makes the rule safe. ``"U.S. Bank"``, ``"Dr. Smith Dental"`` and
   ``"Mt. Fuji Sushi"`` all cut to something two characters long, which
   ``merchant_key`` refuses, so the full string is used instead. Measured on the
   live snapshot: 60 rows move, in 14 groups, all of them correct
   (``shein.com``→``shein``, ``apple.com``→``apple``, ``coursera.org``→
   ``coursera``, ``booking.com``→``booking``, ``Air Serbia A.d Beograd``→
   ``air serbia``); the one imperfect case is ``"Gebr. Heinemann Retail"`` keying
   on ``"gebr"``, which costs a slightly odd key on one row and is why the label
   is chosen separately from the key (see ``_choose_label``).

   This is a structural rule about separators, not a merchant dictionary.

2. **Dominant-spelling consolidation.** ``Apple``, ``Apple Store`` and
   ``Apple com`` key to three different strings even though the first is
   overwhelmingly the same vendor. A group is folded into a shorter group whose
   key is a *token-prefix* of its own — but only when that shorter group is
   overwhelmingly dominant (``_CONSOLIDATE_MIN_ROWS`` rows and
   ``_CONSOLIDATE_RATIO``× as many rows as the group being absorbed).

   The thresholds are deliberately steep, and they were set by looking at what a
   loose rule did to the live snapshot: at 3 rows / 3× it also folded
   ``durham womens clinic`` into ``Durham`` (a city, 5 unrelated rows) and
   ``family dollar`` into ``Family``. Those are different vendors sharing a
   generic word, and merging them silently misstates who was paid. At 10 rows /
   8× the only folds left on real data are ``apple store``/``apple com`` →
   ``apple``, ``uber eats`` → ``uber``, and ``sheinusservices kent`` →
   ``sheinusservices``, all correct. Under-merging is the safe direction: two
   rows for one vendor are visibly adjacent in a ranked list, whereas a wrong
   merge hides money under a name that never received it. Every group therefore
   reports its ``variants`` so the collapse is inspectable rather than asserted.

An EMPTY key means the text named nobody — ``"ONLINE TRANSFER TO CHK"``,
``"PURCHASE AUTHORIZED ON …"``. ``merchant_key``'s docstring is emphatic that
callers must read that as "no match is possible", never as "matches everything".
Here it becomes one explicit ``Unidentified payee`` bucket rather than being
dropped: 49 rows / $10.5k of real spending in the live snapshot key to nothing,
and a breakdown that quietly omits them would not add up to the period total.
That bucket is never merged with anything and is never a filter target that
matches other rows.

What counts as spending
-----------------------
Identical to ``GET /api/dashboard`` on purpose, because the two numbers are shown
next to each other and any difference between them is indistinguishable from a
bug:

* scope (``services/scope.resolve_scope``) — the DB holds each live row twice,
  once from the CSV history and once from Plaid, so an unscoped SUM doubles
  everything;
* transfers / ``asset_conversion`` / ``Excluded`` rows are not spending;
* ``NULL`` and ``Uncategorized`` categories are outside the breakdown;
* EFFECTIVE amounts (``scope.effective_amount_sql``), so a refund reduces the
  charge's own period instead of booking phantom income in a later one;
* a category is *spending* when its net over the window is negative — the flat
  model's rule, evaluated the same way the dashboard evaluates it. Income
  categories are excluded from a spending-by-vendor view and reported in
  ``diagnostics.income_categories_excluded`` so nothing vanishes unannounced.

One deliberate addition: **split parents are excluded here.** A combined Amazon
charge that was split into item rows must not be counted alongside its items. In
this snapshot every such parent already carries ``entity_type='transfer'`` and so
was excluded anyway, but the guard is unconditional so this endpoint cannot
double count even if a parent is left on an expense category. See
``_SPLIT_PARENT_SQL`` for why the signal is ``amazon_items.split_transaction_id``
and not ``linked_transaction_id`` alone.
"""
from __future__ import annotations

import datetime as _dt
import logging
from collections import Counter
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.services.merchant_key import merchant_key, merchant_key_and_tokens
from app.services.scope import effective_amount_sql, resolve_scope

logger = logging.getLogger(__name__)

# The bucket for rows whose text identifies no payee. The key is the empty string
# because that is literally what ``merchant_key`` returned; the label is what a
# person reads. Callers filter on the sentinel below rather than on "" so an
# absent query parameter can never be mistaken for "give me the unidentified".
UNIDENTIFIED_KEY = ""
UNIDENTIFIED_LABEL = "Unidentified payee"
UNIDENTIFIED_FILTER_TOKEN = "__unidentified__"

# See the module docstring: steep on purpose.
_CONSOLIDATE_MIN_ROWS = 10
_CONSOLIDATE_RATIO = 8

# Rows that are internal movement or opted out. Same tuple the dashboard inlines.
_NON_SPENDING_ENTITY_TYPES = ("transfer", "asset_conversion", "excluded")

# Split parents, i.e. a combined charge whose real detail lives in item rows.
#
# ``amazon_items.split_transaction_id`` is the reliable half of the signal: it is
# written when the split is performed and it names the CHILD row. A child's
# ``linked_transaction_id`` then names its parent. Going parent -> child the other
# way round is not safe, because ``linked_transaction_id`` is overloaded — it also
# links the two sides of an internal transfer, so "has a linked row" does not mean
# "was split".
#
# The single-item case in ``amazon_service`` sets ``split_transaction_id`` to the
# charge ITSELF (it renames the row instead of splitting it). That row is not a
# child of anything, so requiring ``linked_transaction_id IS NOT NULL`` keeps it
# out of the parent set, which is correct: it carries its own single item.
_SPLIT_PARENT_SQL = """
    SELECT DISTINCT c.linked_transaction_id AS id
      FROM transactions c
     WHERE c.linked_transaction_id IS NOT NULL
       AND c.linked_transaction_id != c.id
       AND EXISTS (SELECT 1 FROM amazon_items ai
                    WHERE ai.split_transaction_id = c.id)
"""

_MAX_BUCKETS = 400


_PAYEE_SEPARATORS = (":", ".")


def _cut(text: str, separators) -> str:
    """``text`` up to the first of ``separators``, if that head still names someone.

    The guard is the whole safety property: a head that yields an empty key is
    discarded and the full string is used, so ``"U.S. Bank"`` and
    ``"Dr. Smith Dental"`` are untouched while ``"SHEIN.COM DE 03/06"`` becomes
    ``"SHEIN"``.
    """
    cuts = [text.index(sep) for sep in separators if sep in text]
    if not cuts:
        return text
    head = text[:min(cuts)].strip()
    if head and merchant_key(head, None):
        return head
    return text


def payee_text(merchant: Optional[str]) -> str:
    """The part of the merchant field used to KEY the payee. See the docstring."""
    return _cut((merchant or "").strip(), _PAYEE_SEPARATORS)


def display_text(merchant: Optional[str]) -> str:
    """The part of the merchant field a person should READ.

    Cuts the ``"Payee: detail"`` suffix but keeps a dot, because the dot is only
    ever cut to make two spellings key alike — ``"Gebr. Heinemann Retail"`` is the
    vendor's actual name and ``"Gebr"`` is not something to put on screen.
    """
    return _cut((merchant or "").strip(), (":",))


def row_merchant_key(merchant: Optional[str], description: Optional[str]) -> str:
    """The identity key for one row, before consolidation."""
    key, _tokens = merchant_key_and_tokens(payee_text(merchant), description)
    return key


def _choose_label(keys: Sequence[str], labels_by_key: Dict[str, Counter]) -> str:
    """A human name for a group, picked the way ``_find_similar`` picks one.

    Only raw strings that actually KEY to the group's anchor are candidates, and
    the most common one wins (shorter breaks a tie). So the label is the clean
    spelling "Amazon" rather than "Amazon: 100 Pack Peru Flag Toothpick Flags" or
    the rare folded-in variant "Apple Store". The candidate is reduced with
    ``display_text``, not ``payee_text``, which is what keeps
    "Gebr. Heinemann Retail" readable even though it keys on "gebr" — the key
    exists to group, the label to inform.

    When NO raw string keys to the anchor, the merchant field held a payment rail
    ("Zelle", "TST") and the payee came from the description; the key itself,
    title-cased, is then the only honest label. Labelling the group "Zelle" would
    name the network instead of the person, which is exactly the ambiguity this has
    to avoid — and it is why the "only one spelling, just use it" shortcut is NOT
    taken here: a rail row usually has only one spelling, and it is the wrong one.
    """
    anchor = keys[0]
    raws: Counter = Counter()
    for key in keys:
        for raw, count in labels_by_key.get(key, Counter()).items():
            if raw:
                raws[raw] += count
    candidates: Counter = Counter()
    for raw, count in raws.items():
        if merchant_key(payee_text(raw), None) == anchor:
            candidates[display_text(raw)] += count
    if candidates:
        return max(candidates.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
    return anchor.title() if anchor else UNIDENTIFIED_LABEL


def consolidate(groups: Dict[str, dict]) -> Dict[str, List[str]]:
    """Map anchor key -> the keys folded into it. See the module docstring.

    Only a *token-prefix* relation is considered, not mere token overlap: a
    payee's name comes first and qualifiers follow ("apple store"), whereas
    ``"members insurance"`` must never be absorbed by a hypothetical
    ``"insurance"``. The shortest existing prefix that clears the dominance bar
    wins, and the walk is loop-guarded because the relation is only a partial
    order in principle.
    """
    parent: Dict[str, str] = {}
    for key, group in groups.items():
        if not key:
            continue  # the unidentified bucket never merges
        tokens = key.split(" ")
        own = len(group["rows"])
        for i in range(1, len(tokens)):
            cand_key = " ".join(tokens[:i])
            cand = groups.get(cand_key)
            if not cand:
                continue
            cand_rows = len(cand["rows"])
            if cand_rows >= _CONSOLIDATE_MIN_ROWS and cand_rows >= _CONSOLIDATE_RATIO * own:
                parent[key] = cand_key
                break

    def anchor_of(key: str) -> str:
        seen = {key}
        while key in parent and parent[key] not in seen:
            key = parent[key]
            seen.add(key)
        return key

    families: Dict[str, List[str]] = {}
    for key in groups:
        anchor = anchor_of(key) if key else UNIDENTIFIED_KEY
        families.setdefault(anchor, []).append(key)
    for anchor, members in families.items():
        # Anchor first, then the absorbed keys longest-tail-last, so ``_choose_label``
        # and the ``variants`` list are stable between requests.
        members.sort(key=lambda k: (k != anchor, k))
    return families


# ───── Bucketing for the time series ─────

def _parse_date(value: str) -> Optional[_dt.date]:
    try:
        return _dt.date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def choose_granularity(start: Optional[str], end: Optional[str], requested: str = "auto") -> str:
    """Day / week / month, so a one-month window is not a single useless point."""
    if requested in ("day", "week", "month"):
        return requested
    s, e = _parse_date(start or ""), _parse_date(end or "")
    if not s or not e:
        return "month"
    span = (e - s).days
    if span <= 45:
        return "day"
    if span <= 200:
        return "week"
    return "month"


def bucket_of(posted_date: str, granularity: str) -> str:
    if granularity == "month":
        return (posted_date or "")[:7]
    day = _parse_date(posted_date or "")
    if not day:
        return (posted_date or "")[:10]
    if granularity == "week":
        return (day - _dt.timedelta(days=day.weekday())).isoformat()
    return day.isoformat()


def _bucket_sequence(first: str, last: str, granularity: str) -> List[str]:
    """Every bucket between two dates, so gaps read as $0 instead of vanishing.

    A chart that silently omits an empty month draws a straight line across it
    and implies steady spending where there was none.
    """
    start, end = _parse_date(first), _parse_date(last)
    if not start or not end or end < start:
        return []
    out: List[str] = []
    if granularity == "month":
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month) and len(out) <= _MAX_BUCKETS:
            out.append(f"{y:04d}-{m:02d}")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return out
    step = _dt.timedelta(days=7 if granularity == "week" else 1)
    cursor = start - _dt.timedelta(days=start.weekday()) if granularity == "week" else start
    while cursor <= end and len(out) <= _MAX_BUCKETS:
        out.append(cursor.isoformat())
        cursor += step
    return out


def build_series(rows: Iterable[dict], granularity: str,
                 first: Optional[str], last: Optional[str]) -> List[dict]:
    """Spend per bucket over ``rows``, zero-filled across the window.

    Interior gaps ARE filled — a month with no spending at this vendor is a real
    $0 and drawing a straight line across it would imply steady spending. Leading
    and trailing empty buckets are trimmed instead, because they say nothing: the
    'All Time' preset resolves to 2020-01-01, and 58 empty months before the first
    transaction existed are chart noise, not information. The period label already
    states the window.
    """
    spend: Counter = Counter()
    counts: Counter = Counter()
    for row in rows:
        bucket = bucket_of(row["posted_date"], granularity)
        spend[bucket] += -int(row["effective_amount_cents"] or 0)
        counts[bucket] += 1
    buckets = _bucket_sequence(first, last, granularity) if first and last else []
    if not buckets:
        buckets = sorted(spend)
    else:
        # Any row outside the requested window (there should be none, but a
        # missing start/end means the window came from the data) still shows.
        for extra in sorted(set(spend) - set(buckets)):
            buckets.append(extra)
        buckets.sort()
    out = [{"bucket": b, "spend_cents": int(spend.get(b, 0)),
            "transaction_count": int(counts.get(b, 0))} for b in buckets]
    while out and out[0]["transaction_count"] == 0:
        out.pop(0)
    while out and out[-1]["transaction_count"] == 0:
        out.pop()
    return out


# ───── The query ─────

async def fetch_rows(
    db,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    scope: Optional[str] = "auto",
) -> Tuple[List[dict], dict]:
    """Every countable row in the window, with its effective amount.

    Returned as Python dicts rather than aggregated in SQL because the grouping
    key is a Python function. ~3.2k rows for the live plaid scope over two years,
    which measures in single-digit milliseconds; the alternative (a merchant-key
    UDF registered on the connection) would hide the rule inside the query plan
    for no measurable gain.
    """
    scope_sql, scope_params, effective_scope, scope_note = await resolve_scope(db, scope)

    non_spending = ", ".join(f"'{e}'" for e in _NON_SPENDING_ENTITY_TYPES)
    clauses = [
        f"(t.entity_type IS NULL OR t.entity_type NOT IN ({non_spending}))",
        "t.category_path IS NOT NULL",
        "t.category_path != 'Uncategorized'",
    ]
    params: List = []
    if start_date:
        clauses.append("t.posted_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("t.posted_date <= ?")
        params.append(end_date)
    where = "WHERE " + " AND ".join(clauses) + scope_sql
    params += list(scope_params)

    # Split parents, measured before they are removed so the exclusion is
    # reportable instead of invisible.
    cursor = await db.execute(
        f"""SELECT COUNT(*), COALESCE(SUM({effective_amount_sql('t')}), 0)
              FROM transactions t {where}
               AND t.id IN ({_SPLIT_PARENT_SQL})""",
        params,
    )
    row = await cursor.fetchone()
    split_parent_rows = int(row[0] or 0) if row else 0
    split_parent_cents = int(row[1] or 0) if row else 0

    cursor = await db.execute(
        f"""SELECT t.id, t.posted_date, t.merchant, t.description, t.amount_cents,
                   t.category_path, t.account_id, t.entity_type, t.flagged, t.flag_reason,
                   t.refund_role, t.refund_match_id, t.source_platform,
                   -- Carried so the drill-down can reuse TxnTable's "why this
                   -- category?" panel instead of a second per-row fetch.
                   t.confidence_score, t.categorization_reason, t.applied_rule_id,
                   {effective_amount_sql('t')} AS effective_amount_cents
              FROM transactions t {where}
               AND t.id NOT IN ({_SPLIT_PARENT_SQL})""",
        params,
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    meta = {
        "scope": effective_scope,
        "scope_note": scope_note,
        "split_parent_rows_excluded": split_parent_rows,
        "split_parent_cents_excluded": split_parent_cents,
    }
    return rows, meta


def spending_categories(rows: Iterable[dict]) -> Tuple[set, set]:
    """(spending, non-spending) category names, by the sign of each one's net.

    The dashboard's rule — it aggregates per category and lets the net's sign
    decide the side — so the two views' totals agree by construction rather than by
    coincidence.

    A category netting EXACTLY zero counts as spending here even though it
    contributes nothing to the total. That is deliberate and it costs nothing: a
    fully-refunded purchase (charge -$60, credit +$60) nets its category to zero,
    and treating that as "not spending" made the vendor disappear from the
    breakdown entirely rather than showing the $0 that is the truth. Including it
    cannot move the total, since the group it adds sums to zero, and the dashboard
    lists such a category too (income 0, expense 0).
    """
    nets: Counter = Counter()
    for row in rows:
        nets[row["category_path"]] += int(row["effective_amount_cents"] or 0)
    spending = {c for c, net in nets.items() if net <= 0}
    other = {c for c, net in nets.items() if net > 0}
    return spending, other


def group_by_merchant(rows: Sequence[dict]) -> Tuple[Dict[str, dict], Dict[str, str]]:
    """Group rows into merchant families. Returns (families, row_id -> anchor key)."""
    raw: Dict[str, dict] = {}
    labels_by_key: Dict[str, Counter] = {}
    for row in rows:
        key = row_merchant_key(row.get("merchant"), row.get("description"))
        group = raw.setdefault(key, {"rows": []})
        group["rows"].append(row)
        labels_by_key.setdefault(key, Counter())[(row.get("merchant") or "").strip()] += 1

    families = consolidate(raw)
    out: Dict[str, dict] = {}
    assignment: Dict[str, str] = {}
    for anchor, member_keys in families.items():
        member_rows = [r for k in member_keys for r in raw[k]["rows"]]
        variants: Counter = Counter()
        for k in member_keys:
            variants.update(labels_by_key.get(k, Counter()))
        for r in member_rows:
            assignment[r["id"]] = anchor
        out[anchor] = {
            "merchant_key": anchor,
            "label": (UNIDENTIFIED_LABEL if not anchor
                      else _choose_label(member_keys, labels_by_key)),
            "unidentified": not anchor,
            "keys": member_keys,
            "rows": member_rows,
            "variants": [{"merchant": raw_name or "(blank)", "transaction_count": n}
                         for raw_name, n in variants.most_common(12)],
            "variant_count": len(variants),
        }
    return out, assignment


def summarize_group(group: dict) -> dict:
    """The reportable numbers for one merchant family.

    ``spend_cents`` is the reconciling figure: the negated sum of EFFECTIVE
    amounts, which is what the dashboard's category net is built from. It can be
    negative — a vendor whose returns outweighed its purchases in this window
    genuinely gave money back, and clamping that to zero would break the identity
    ``sum(merchant spend) == period spend``.

    ``gross_spend_cents`` / ``refund_cents`` are RAW sums, there so a person can
    see the two halves. They do not always subtract to ``spend_cents``: when a
    refund's charge falls in an earlier window the credit contributes 0 here by
    design (see ``services/scope``), so the gap between
    ``gross_spend - refund`` and ``spend`` is exactly the cross-period refund
    attribution and is reported as ``refund_attribution_cents`` rather than left
    as a mystery.
    """
    rows = group["rows"]
    spend = -sum(int(r["effective_amount_cents"] or 0) for r in rows)
    gross = sum(-int(r["amount_cents"]) for r in rows if int(r["amount_cents"] or 0) < 0)
    refund = sum(int(r["amount_cents"]) for r in rows if int(r["amount_cents"] or 0) > 0)
    dates = [r["posted_date"] for r in rows if r["posted_date"]]
    cats: Counter = Counter()
    cat_counts: Counter = Counter()
    for r in rows:
        cats[r["category_path"]] += -int(r["effective_amount_cents"] or 0)
        cat_counts[r["category_path"]] += 1
    return {
        "merchant_key": group["merchant_key"],
        "label": group["label"],
        "unidentified": group["unidentified"],
        "spend_cents": int(spend),
        "gross_spend_cents": int(gross),
        "refund_cents": int(refund),
        "refund_attribution_cents": int(spend - (gross - refund)),
        "transaction_count": len(rows),
        "first_date": min(dates) if dates else None,
        "last_date": max(dates) if dates else None,
        "categories": [
            {"category_path": c, "spend_cents": int(v),
             "transaction_count": int(cat_counts[c])}
            for c, v in sorted(cats.items(), key=lambda kv: -kv[1])
        ],
        "variants": group["variants"],
        "variant_count": group["variant_count"],
        "merged_keys": group["keys"],
    }
