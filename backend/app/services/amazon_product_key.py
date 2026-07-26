"""Deterministic PRODUCT identity for Amazon item rows.

Same job as ``merchant_key`` — mechanically propagating a categorization the user
already made — but for a different question. ``merchant_key`` answers *who was
paid*; for an Amazon item row that answer is always "Amazon", which is useless:
the ledger holds 1,325 rows named ``Amazon: <product>`` plus hundreds of bare
``Amazon`` charges, and every one of them shares the token ``amazon``. That is
exactly the live failure this module exists to remove — filing one
``Amazon: Fresh Step … Cat Litter`` row under Pets offered **56 matches**, of
which the great majority were bare ``Amazon`` rows of unrelated amounts
(−$27.96, −$432.33, a +$18.41 refund) and a handful were other people's
products (tracing paper, Halloween costumes). None of them were cat litter.

For an item row the identity that matters is the PRODUCT, and the Amazon export
gives it to us directly (``amazon_items.asin`` / ``product_name``). So:

**ASIN first.** Amazon's own product id. Two rows with the same ASIN are the
same product, full stop — even when the listing text drifts between orders,
which it does: ``B07ZK3DZB3`` arrives as both "Fresh Step Clean Paws Advanced
Multi Cat Clumping Litter with Febreze Freshness, 18.5 lb Each, Pack of 2" and
"Fresh Step Advanced Clean Paws Multi Cat Clumping Cat Litter with Febreze
Freshness, Low Dust, …". Nothing derived from the text could equate those two
strings without also equating things that are not the same product.

**Product name, only as a fallback.** 990 item rows in the live DB have no
usable ``amazon_items`` link — ``amazon_service.reset_matches`` clears
``matched_transaction_id`` / ``split_transaction_id`` whenever the authoritative
transaction source changes, and split children created before a reset keep only
their ``Amazon: <product>`` merchant string. For those the name is all there is.
The relation used is deliberately narrow: one row's normalized name must be a
token-wise PREFIX of the other's. That is the shape the data actually has —
``amazon_service`` writes ``merchant = "Amazon: " + product_name[:60]``, so the
merchant is a genuine truncation of the full name — and it cannot equate two
different products the way a token-overlap score can ("Fresh Step Cat Litter"
vs "Fresh Step Cat Litter Deodorizer" are different SKUs, and both are also
different from "Purina Cat Litter", but an overlap threshold would happily fuse
the first two). The final token of a truncated name is allowed to be a fragment
(``…Clumping Litter wit`` vs ``…Clumping Litter with Febreze``) because a
60-character cut lands mid-word about a fifth of the time.

**A rule pattern from a product name.** ``rules_service.apply_rules`` matches a
pattern as ``LIKE '%pattern%'`` against merchant OR description, so the pattern
persisted from this action decides what future imports get categorized
automatically. The one thing it must never be is the merchant key of an item row
(``amazon fresh step …``, which matches nothing because of the ``Amazon:``
colon) or, worse, anything as short as ``amazon`` — see the "A short key is more
dangerous than no key" section of ``merchant_key``: a live rule with pattern
``us`` matched 1,208 unrelated rows. ``safe_rule_pattern`` therefore returns a
long, literal, wildcard-free prefix of the product name, or ``""`` meaning *no
rule can be expressed safely — persist none and tell the user*.
"""
from __future__ import annotations

import re
from typing import Optional, Sequence, Tuple

# ``amazon_service.split_matched_charges`` writes every item row's merchant as
# this prefix plus the first 60 characters of the product name. It is the only
# way to recognise an item row whose ``amazon_items`` link has been reset.
ITEM_MERCHANT_PREFIX = "Amazon: "

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")

# The residual line of a split ("Tax & Shipping") is not a product: it is
# whatever shipping/tax/rounding the item totals did not account for. Two
# residual rows from unrelated orders share that label, and the categorizer's
# own rule is that a residual takes the category of the LARGEST ITEM IN ITS OWN
# ORDER — so propagating one residual's category across orders is wrong by
# construction. Giving it no identity means it is never a source and never a
# match.
_NON_PRODUCT_NAMES = frozenset({"tax shipping", "tax and shipping", "shipping",
                                "tax", "gift card", "promotion", "discount"})

# A name shorter than this cannot distinguish one product from another, and the
# prefix relation below would make it a match for everything that starts with
# it. Same reasoning as ``merchant_key._MIN_DISTINCTIVE_TOKEN_LEN``, applied to
# a whole name: fail closed, offer no propagation, let the user file by hand.
_MIN_NAME_TOKENS = 3
_MIN_NAME_CHARS = 12
# A name shorter than this is complete, not cut off, so it may only match itself.
# The feed truncates around 60 characters; 40 leaves room for normalisation having
# dropped punctuation and stop words from a genuinely truncated name.
_TRUNCATION_MIN_CHARS = 40

# A rule pattern is a substring of future merchant/description text, so it must
# be long enough to name this product and nothing else. 16 characters of a
# product name is many times the 4-character floor ``rules_service`` enforces.
_MIN_RULE_PATTERN_CHARS = 16
# ...and short enough to survive the 60-character merchant truncation, so the
# rule still fires on the merchant field and not only on the description.
_MAX_RULE_PATTERN_CHARS = 44


def product_tokens(name: Optional[str]) -> Tuple[str, ...]:
    """Lower-cased alphanumeric tokens of a product name, in order.

    Order is kept because the relation below is a prefix test, and a prefix is
    only meaningful in the original order.
    """
    if not name:
        return ()
    return tuple(t for t in _TOKEN_SPLIT_RE.split(name.lower()) if t)


def normalize_product_name(name: Optional[str]) -> str:
    """``"Fresh Step® 18.5 lb."`` -> ``"fresh step 18 5 lb"``. ``""`` when nothing survives."""
    return " ".join(product_tokens(name))


def product_name_from_merchant(merchant: Optional[str]) -> str:
    """The product name carried by an item row's merchant, or ``""``.

    ``"Amazon: Genuine Toyota Oil Filter"`` -> ``"Genuine Toyota Oil Filter"``.
    A bare ``"Amazon"`` row returns ``""`` — it names no product, which is the
    whole reason it must never be offered as a match for one.
    """
    text = (merchant or "").strip()
    if not text.lower().startswith(ITEM_MERCHANT_PREFIX.lower()):
        return ""
    return text[len(ITEM_MERCHANT_PREFIX):].strip()


def is_item_merchant(merchant: Optional[str]) -> bool:
    """Whether this merchant string is an Amazon per-item row's."""
    return bool(product_name_from_merchant(merchant))


class ProductIdentity:
    """What identifies one Amazon item row's product: an ASIN, a name, or nothing.

    ``truthy`` exactly when the row can take part in product matching at all.
    """

    __slots__ = ("asin", "name", "tokens")

    def __init__(self, asin: Optional[str], name: str, tokens: Tuple[str, ...]):
        self.asin = asin
        self.name = name
        self.tokens = tokens

    def __bool__(self) -> bool:
        return bool(self.asin or self.tokens)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ProductIdentity(asin={self.asin!r}, name={self.name!r})"

    @property
    def key(self) -> str:
        """A stable string for grouping/suppression. Empty when unidentifiable."""
        if self.asin:
            return f"asin:{self.asin}"
        if self.tokens:
            return f"product:{' '.join(self.tokens)}"
        return ""


_EMPTY = ProductIdentity(None, "", ())


def product_identity(
    asin: Optional[str] = None,
    product_name: Optional[str] = None,
    merchant: Optional[str] = None,
) -> ProductIdentity:
    """Identity for one row, from the best information available.

    ``asin`` / ``product_name`` come from ``amazon_items`` when the row is still
    linked; ``merchant`` is the fallback for the 990 rows whose link was reset.
    A row that is not an Amazon item row at all — a bare ``Amazon`` charge, a
    Target row — has no product identity, and neither does the ``Tax &
    Shipping`` residual.
    """
    name = (product_name or "").strip() or product_name_from_merchant(merchant)
    tokens = product_tokens(name)
    normalized = " ".join(tokens)
    if normalized in _NON_PRODUCT_NAMES:
        return _EMPTY
    usable_name = (
        len(tokens) >= _MIN_NAME_TOKENS and len(normalized) >= _MIN_NAME_CHARS
    )
    clean_asin = (asin or "").strip().upper() or None
    if not clean_asin and not usable_name:
        return _EMPTY
    return ProductIdentity(clean_asin, name, tokens if usable_name else ())


def _name_prefix_match(a: Sequence[str], b: Sequence[str]) -> bool:
    """Whether one token sequence is the other, possibly truncated mid-word.

    ``("fresh","step","clumping","litter","wit")`` matches
    ``("fresh","step","clumping","litter","with","febreze")`` — the shorter is a
    60-character cut of the longer. ``("fresh","step","clumping","deodorizer")``
    matches neither: a different product differs at a leading token (brand,
    model, size), so requiring the whole leading run to be equal is what keeps
    two SKUs apart.

    Known, accepted looseness: a merchant truncated BEFORE the token that
    distinguishes two variants of one listing ("…Ethernet Cable," cut at 60
    characters, then 10 ft vs 25 ft) is a prefix of both. Those are variants of
    the same product and take the same category in practice, and whenever both
    rows still carry an ASIN the ASIN decides instead.
    """
    if not a or not b:
        return False
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < _MIN_NAME_TOKENS:
        return False
    # The shorter name has to look like a TRUNCATION, not merely a prefix. A prefix
    # alone fuses a product with its accessory: "Apple Lightning Cable" is a token
    # prefix of "Apple Lightning Cable Adapter 2 Pack", and "Kirkland Signature
    # Bath Tissue" of "…Bath Tissue Holder Stand" — different SKUs, and where
    # neither row carries an ASIN nothing else would separate them. A real
    # truncation comes from the ~60-character merchant cut, so a short name well
    # under that length is a complete name and must match only itself.
    # (No live row hits this today; the rule exists so that a persisted
    # apply-similar pattern cannot start claiming accessories on a future import.)
    if len(" ".join(short)) < _TRUNCATION_MIN_CHARS and len(short) != len(long_):
        return False
    if tuple(long_[: len(short) - 1]) != tuple(short[: -1]):
        return False
    # The last token of a truncated name may be a fragment of a longer word.
    return long_[len(short) - 1].startswith(short[-1])


def same_product(a: ProductIdentity, b: ProductIdentity) -> bool:
    """Whether two item rows are the same product.

    ASIN wins whenever both sides have one — including when it says NO: two rows
    with different ASINs are different products even if their listing text is
    identical, and quietly overriding Amazon's own id with a string comparison
    would be a worse answer, not a more generous one.
    """
    if not a or not b:
        return False
    if a.asin and b.asin:
        return a.asin == b.asin
    return _name_prefix_match(a.tokens, b.tokens)


def safe_rule_pattern(identity: ProductIdentity) -> str:
    """A ``LIKE '%…%'`` pattern that matches this product and little else, or ``""``.

    Returned lower-case because ``add_rule`` stores patterns lower-cased and
    ``apply_rules`` compares ``LOWER(merchant)`` / ``LOWER(description)``.

    ``""`` — no rule — is returned whenever the name cannot produce something
    both specific and literal:

    * too short to be distinctive (a 5-character pattern is a substring rule
      against the whole ledger);
    * containing ``%`` or ``_``, which are LIKE wildcards, not characters. A
      real product in this DB is named "100% Blackout Curtains…"; persisting
      that verbatim would store a pattern with a wildcard in the middle, and the
      user would have no way to tell from the rules list.

    The caller must treat ``""`` as *say so and persist nothing*, never as
    *fall back to something broader* — the broader thing here is ``amazon``.
    """
    if not identity or not identity.tokens:
        return ""
    # A LITERAL prefix of the name, not the normalized token join: the pattern is
    # compared with LIKE against the raw merchant/description, and those strings
    # are built from this exact name ("Amazon: " + name[:60] / the name itself).
    # Normalizing would drop the punctuation that is really in the text —
    # "freshness low dust" never matches "Freshness, Low Dust".
    pattern = identity.name.strip().lower()
    if len(pattern) > _MAX_RULE_PATTERN_CHARS:
        # Cut at a token boundary; a mid-word cut would still be a literal
        # substring, but it reads as a typo in the rules list.
        pattern = pattern[:_MAX_RULE_PATTERN_CHARS].rsplit(" ", 1)[0].strip()
    if len(pattern) < _MIN_RULE_PATTERN_CHARS:
        return ""
    if "%" in pattern or "_" in pattern:
        return ""
    return pattern


def pattern_matches_every_item_row(pattern: Optional[str]) -> bool:
    """Whether a rule pattern would fire on EVERY Amazon item row.

    ``"amazon"`` is such a pattern: every item row's merchant is
    ``"Amazon: <product>"``, so ``LIKE '%amazon%'`` matches all 1,325 of them, and
    ``apply_rules`` runs against uncategorized rows on every import — which is
    where per-product categories come from. A rule like that does not just
    over-match, it PREEMPTS the item export it is competing with, replacing Pets /
    Health / Car with one bucket forever.

    Deliberately narrow: it asks only whether the pattern is a substring of the
    item-row prefix. ``"whole foods"`` and ``"amzn mktp"`` are Amazon-family
    strings that do NOT match item rows, and rules built from them stay allowed.
    """
    text = (pattern or "").strip().lower()
    if not text:
        return False
    return text in ITEM_MERCHANT_PREFIX.strip().lower()
