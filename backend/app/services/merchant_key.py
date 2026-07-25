"""Deterministic merchant keys, for propagating a categorization the user already made.

This is NOT categorization. Nothing here decides what a transaction *is* — the
LLM owns that at ingest (see the hard rule in ``AGENTS.md``). What this module
answers is a narrower, purely mechanical question: *given a row the human just
filed under "Groceries", which other rows are the same payee?* That answer must
be instant and inspectable, because the user reviews the match list before
anything is written. An LLM round-trip per correction is both too slow (~5-10s)
and too opaque (no way to see why row 4,812 was included).

The approach is ported from the rental-management reconciliation engine
(``src/lib/reconciliation/engine.ts::merchantKey``), which has been running
against real Chase/Plaid feeds. Two kinds of per-transaction noise have to be
removed or a recurring payee never keys the same twice:

**Confirmation / reference codes.** ``"Zelle payment to Koshiya Confirmation#
cutdyyakh"`` and ``"…Confirmation# qzlyirds"`` are the same payee on two months.
The label and the code that follows it are dropped together, so the key is
``"koshiya"`` in both cases instead of two unique keys that never match.

**Store and terminal numbers.** Card feeds append the store's location after its
number: ``"TARGET 00023991 DURHAM NC"``. Stripping only the digits leaves
``"target durham nc"``, which does not equal the key for a plain ``"Target"`` row
from Plaid — so a Durham purchase and a Raleigh purchase at the same chain would
be treated as different merchants. The digit run is therefore read as a
*boundary*: everything from the store number onward is location noise and is
discarded, giving ``"target"``. The boundary only applies once a distinctive
token has already been seen, so a leading terminal number in
``"POS DEBIT 1234 TARGET DURHAM"`` does not truncate the payee away.

An empty key means "nothing distinctive survived". Callers MUST treat that as
*no match is possible* and never as *everything matches* — the whole point of
stripping rail noise is that ``"ONLINE TRANSFER TO CHK"`` is not a merchant, and
bulk-applying a category to every such row would corrupt the ledger silently.

**A short key is more dangerous than no key.** The key is offered to the user as
a rule pattern, and ``rules_service.apply_rules`` matches a pattern as
``LIKE '%pattern%'`` against merchant OR description. So a two-character key is
not a weak match — it is a match against a large fraction of the ledger. Live
incident: ``"US MOBILE"`` keyed to ``"us"`` (``"mobile"`` is a stop word, ``"us"``
was long enough to survive), that key became rule pattern ``us``, and the rule
matched **1,208 rows** — "Costco Wholesale", anything containing "USA", "USB",
"Super", "Cushions". Two length guards below (``_MIN_TOKEN_LEN``,
``_MIN_DISTINCTIVE_TOKEN_LEN``) exist to make that shape return ``""`` instead.
``"US MOBILE"`` is therefore unmatchable now, and that is the intended outcome:
the failure mode of a too-short key is silent mass mis-categorization, while the
failure mode of an empty key is only that no propagation is offered.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional, Set

# Noise that must never enter a key. Three groups, all of them things that
# describe the *mechanics* of a payment rather than who was paid.
MERCHANT_STOP = frozenset({
    # Transfer / memo / reference noise.
    "online", "recurring", "mobile", "transfer", "to", "from", "confirmation",
    "conf", "ref", "reference", "trace", "des", "web", "pmt", "payment",
    "purchase", "debit", "credit", "card", "pos", "the", "of", "and", "co",
    "inc", "llc", "ltd", "corp",
    # Words that describe the *record* rather than a payee. Each one was pulled
    # from live data where it produced a bogus cluster: "Account Transfer" (184
    # rows) keyed on "account"; "ACH Electronic Credit" keyed on "electronic",
    # which is a SUBSET of "ELECTRONIC DEPOSIT ACME INC" and so linked a
    # rental-income row to a payroll row; "Payment Thank You - Web" is a Chase
    # card-payment stub. Dropping them leaves those rows with an empty key, i.e.
    # unmatchable, which is the correct answer for a string that names nobody.
    "account", "accounts", "transaction", "transactions", "electronic",
    "thank", "you",
    # Month names: a recurring memo ("…for July rent") would otherwise key
    # differently every month.
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec",
    # Payment rails. These swamp the real payee: every Zelle row shares
    # "zelle payment to", so keeping the rail would make two unrelated people
    # look like the same merchant. The DISTINCTIVE tokens (the payee's name)
    # are what survive.
    "zelle", "venmo", "paypal", "cashapp", "ach", "eft", "wire", "xfer",
    "autopay", "billpay", "chk", "chq", "check", "echeck", "atm", "deposit",
    "withdrawal", "ppd", "ccd", "tel", "sq", "square", "recur",
    # "tst" is Toast's card-network prefix ("TST*AJUMMA"). On live data 61 rows
    # arrived with the merchant field set to the bare prefix and the restaurant
    # name only in the description — keeping it would fuse dozens of unrelated
    # restaurants into one merchant.
    "tst",
})

# ``confirmation# ab12cd`` / ``ref: 99812`` / ``trace 4471`` — the label and the
# code that follows are dropped as a unit. Matching the label alone would leave
# the unique code behind, which is the bug this exists to prevent.
_CODE_LABEL_RE = re.compile(
    r"\b(?:confirmation|conf|ref|reference|trace|invoice|id)\b\s*[#:]?\s*[a-z0-9]+"
)
# A bare ``#12345`` with no label.
_HASH_CODE_RE = re.compile(r"#\s*[a-z0-9]+")
_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# A pure-digit token this long is a store / terminal / reference number, not part
# of a name. Two digits is left alone because it can be a real name fragment
# (e.g. a "76" gas station).
_STORE_NUMBER_MIN_DIGITS = 3

# Shortest token allowed into a key. Was 2, which is how ``"US MOBILE"`` keyed to
# ``"us"`` and put a 1,208-row rule in the DB (see the module docstring). Two
# characters cannot identify a payee but CAN appear inside thousands of unrelated
# strings, so the substring rule built from it is indiscriminate.
#
# Tradeoff, accepted deliberately: a merchant whose real name is two characters
# becomes unmatchable. That is the safe direction to fail — an empty key means
# "no propagation offered", never "propagate to everything", and the user can
# still categorize the row by hand.
_MIN_TOKEN_LEN = 3

# ...and the surviving tokens must include at least one token of AT LEAST this
# length, or the whole key is discarded. Raising ``_MIN_TOKEN_LEN`` to 3 alone
# still lets a 3-character accident through — a leftover network/rail prefix that
# is not in ``MERCHANT_STOP`` yet, for instance — and ``LIKE '%abc%'`` is still
# wide enough to cluster unrelated merchants. Requiring one genuinely
# distinctive token means a key is either recognisable or absent.
_MIN_DISTINCTIVE_TOKEN_LEN = 4

# Overlap at or above this counts as a weak "partial" signal. Deliberately only
# advisory: the bulk-apply path keeps exact/strong matches only, because a false
# positive there rewrites a category the user never looked at.
PARTIAL_THRESHOLD = 0.6


def _strip_accents(text: str) -> str:
    """``Café`` and ``Cafe`` are the same merchant on two different exports."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _tokens(merchant: Optional[str], description: Optional[str]) -> list:
    """Tokens from the merchant field, falling back to the description.

    Plaid gives a clean ``merchant`` ("Target"); CSV exports often leave it blank
    and put everything in the description ("TARGET 00023991 DURHAM NC"). Both must
    key the same, which is why the fallback exists at all.

    The fallback also fires when the merchant field is present but yields NOTHING
    distinctive, because some feeds put the rail there and the payee only in the
    description: live rows arrive with ``merchant='Zelle'`` /
    ``description='Zelle payment to Sofia 20729587790'``, and ``merchant='TST'``
    (Toast's prefix) / ``description='TST*AJUMMA'``. Keying those on the merchant
    alone gives an empty key, i.e. no match at all, when the payee was sitting
    right there.
    """
    if merchant and merchant.strip():
        tokens = _tokens_from(merchant)
        if tokens:
            return tokens
    return _tokens_from(description or "")


def _tokens_from(text: str) -> list:
    base = _strip_accents(text).lower()
    base = _CODE_LABEL_RE.sub(" ", base)
    base = _HASH_CODE_RE.sub(" ", base)

    kept: list = []
    for raw in _TOKEN_SPLIT_RE.split(base):
        if not raw:
            continue
        if any(ch.isdigit() for ch in raw):
            # A store number marks the start of the location tail — see the
            # "TARGET 00023991 DURHAM NC" example in the module docstring. Only
            # treat it as a boundary once something distinctive precedes it, so a
            # leading terminal number can't truncate the payee away.
            if raw.isdigit() and len(raw) >= _STORE_NUMBER_MIN_DIGITS and kept:
                break
            continue
        if len(raw) < _MIN_TOKEN_LEN:
            continue  # stray initials, split-off letters, and "us" — see above
        if raw in MERCHANT_STOP:
            continue
        kept.append(raw)

    # Nothing distinctive enough to build a substring rule from. Returning ``[]``
    # rather than the short tokens also lets ``_tokens`` fall back to the
    # description, which is where the real payee usually is in this situation.
    if not any(len(tok) >= _MIN_DISTINCTIVE_TOKEN_LEN for tok in kept):
        return []
    return kept


def merchant_key(merchant: Optional[str], description: Optional[str]) -> str:
    """Stable identity key for a payee, or ``""`` when nothing distinctive survives.

    Tokens are joined in their ORIGINAL order so the key stays readable — it is
    shown to the user as the suggested rule pattern, and "rivera plumbing" is
    something a person can recognise where a sorted hash would not be.
    """
    return " ".join(_tokens(merchant, description))


def merchant_tokens(merchant: Optional[str], description: Optional[str]) -> Set[str]:
    """The same tokens as a set, for overlap tests where order is irrelevant."""
    return set(_tokens(merchant, description))


def merchant_key_and_tokens(merchant: Optional[str], description: Optional[str]):
    """Both forms from ONE tokenization pass.

    The matcher calls this once per row over the whole table; asking for the key
    and the token set separately tokenizes every row twice for no reason.
    """
    tokens = _tokens(merchant, description)
    return " ".join(tokens), set(tokens)


def similarity(a_tokens: Set[str], b_tokens: Set[str]) -> float:
    """Jaccard overlap of two token sets. Empty on either side scores 0."""
    if not a_tokens or not b_tokens:
        return 0.0
    shared = len(a_tokens & b_tokens)
    if not shared:
        return 0.0
    return shared / len(a_tokens | b_tokens)


def match_strength(
    source_key: str,
    source_tokens: Set[str],
    cand_key: str,
    cand_tokens: Set[str],
) -> Optional[str]:
    """How confidently two rows name the same payee: exact / strong / partial / None.

    * ``exact``   — identical keys. "Target" (Plaid) vs "TARGET 00023991 DURHAM NC" (CSV).
    * ``strong``  — one token set contains the other: "amazon" vs "amazon marketplace".
    * ``partial`` — merely overlapping. Advisory only; never safe to bulk-apply,
      because "duke energy" and "dominion energy" overlap on a word that names
      no company.

    An empty key on either side is never a match — see the module docstring.
    """
    if not source_key or not cand_key:
        return None
    if source_key == cand_key:
        return "exact"
    shared = source_tokens & cand_tokens
    if shared and (source_tokens <= cand_tokens or cand_tokens <= source_tokens):
        return "strong"
    if similarity(source_tokens, cand_tokens) >= PARTIAL_THRESHOLD:
        return "partial"
    return None
