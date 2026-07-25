"""Regression tests for ``flatten_path``'s resolution order.

These exist because of a real data loss. Chat-correct (before it was migrated to
the flat model) wrote ``Expense > Groceries`` onto 287 Target purchases. Running
``POST /api/categories/flatten`` to clean that up mapped them to ``Other``,
because the longest-prefix scan matched the generic ``"Expense" -> "Other"``
entry instead of noticing that the leaf ``Groceries`` was already a perfectly
good flat category. The user's categorization was silently destroyed.

The leaf is the specific answer; a parent prefix is only a fallback.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.services.flat_categories import DEFAULT_CATEGORIES, MIGRATION_MAP, flatten_path


def test_known_leaf_beats_generic_parent_prefix():
    """The bug that cost 287 rows: leaf wins over the ``Expense`` catch-all."""
    assert flatten_path("Expense > Groceries") == "Groceries"
    assert flatten_path("Expense > Travel") == "Travel"
    assert flatten_path("Income > Salary") == "Salary"
    assert flatten_path("Expense > Restaurants") == "Restaurants"


def test_explicit_mapping_still_wins_over_leaf():
    """Hand-chosen entries are deliberate and must not be overridden.

    ``Dining`` is not a flat category name — ``Restaurants`` is — so the map is
    the only thing that knows where those rows go.
    """
    assert flatten_path("Expense > Personal > Dining") == "Restaurants"
    assert flatten_path("Expense > Housing > Mortgage Taxes") == "Taxes"
    assert flatten_path("Income > Rentals") == "Rental Income"
    assert flatten_path("Transfer > Mortgage Principal") == "Mortgage Payment"


def test_unknown_leaf_falls_back_to_parent_prefix():
    """An invented leaf has no flat equivalent, so the parent decides."""
    assert flatten_path("Expense > Housing > Some New Thing") == "Housing"
    assert flatten_path("Expense > Nonsense Leaf") == "Other"


def test_already_flat_and_empty_inputs_are_untouched():
    assert flatten_path("Groceries") == "Groceries"
    assert flatten_path("Some Custom Bucket") == "Some Custom Bucket"
    assert flatten_path("Uncategorized") is None
    assert flatten_path(None) is None
    assert flatten_path("") is None


def test_every_mapping_target_is_a_real_flat_category():
    """A mapping that points at a name the app does not use is a silent black hole."""
    for source, target in MIGRATION_MAP.items():
        if target is None:
            continue
        assert target in DEFAULT_CATEGORIES, (
            f"MIGRATION_MAP[{source!r}] = {target!r}, which is not a flat category"
        )


def test_flatten_is_idempotent():
    """Running the migration twice must not keep moving rows around."""
    for source in list(MIGRATION_MAP) + ["Expense > Groceries", "Expense > Housing > New"]:
        once = flatten_path(source)
        if once is None:
            continue
        assert flatten_path(once) == once, f"{source!r} -> {once!r} is not stable"
