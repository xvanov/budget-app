"""Tests for Story 06: Hierarchical, user-extensible category tree with multi-dimensional tags."""

import os
import sys
import json
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.database import SEED_CATEGORIES


def _make_mock_llm():
    """Return a mock call_llm that categorizes by amount sign."""
    def mock_call_llm(prompt, **kwargs):
        import re
        results = []
        for line in prompt.split('\n'):
            m = re.match(r'\s+ID=(\d+):', line)
            if m:
                txn_id = int(m.group(1))
                is_negative = '$-' in line
                results.append({
                    "id": txn_id,
                    "category_path": "Expense > Personal > Other" if is_negative else "Income > Other Income",
                    "entity_type": "expense" if is_negative else "income",
                    "confidence_score": 0.85,
                    "categorization_reason": "Test categorization",
                    "business_context": "personal",
                })
        return json.dumps(results)
    return mock_call_llm


def _import_with_mock(test_client, monkeypatch):
    """Import samples with mocked LLM."""
    monkeypatch.setattr("app.services.categorizer.call_llm", _make_mock_llm())
    resp = test_client.post("/api/import/samples")
    assert resp.status_code == 200
    return resp


def test_categories_tree_returns_seeded_tree(test_client):
    """GET /api/categories/tree returns the full nested tree from seed."""
    resp = test_client.get("/api/categories/tree")
    assert resp.status_code == 200
    data = resp.json()
    assert "tree" in data
    assert len(data["tree"]) > 0

    root_names = {n["name"] for n in data["tree"]}
    assert "Income" in root_names
    assert "Expense" in root_names
    assert "Transfer" in root_names

    expense = [n for n in data["tree"] if n["name"] == "Expense"][0]
    expense_child_names = {c["name"] for c in expense.get("children", [])}
    assert "Housing" in expense_child_names
    assert "Personal" in expense_child_names
    assert "Rental" in expense_child_names

    personal = [c for c in expense["children"] if c["name"] == "Personal"][0]
    personal_leaf_names = {l["name"] for l in personal.get("children", [])}
    assert "Groceries" in personal_leaf_names
    assert "Subscriptions" in personal_leaf_names


def test_list_categories_returns_all_seeded(test_client):
    """GET /api/categories returns the flat list of all seeded categories."""
    resp = test_client.get("/api/categories")
    assert resp.status_code == 200
    data = resp.json()
    assert "categories" in data
    flat_names = {c["name"] for c in data["categories"]}
    assert "Income" in flat_names
    assert "Rentals" in flat_names
    assert "Subscriptions" in flat_names
    assert "Cash Out" in flat_names
    assert len(data["categories"]) >= len(SEED_CATEGORIES)


def test_seeded_paths_are_correct(test_client):
    """Every seeded path should exist in the categories table."""
    resp = test_client.get("/api/categories")
    assert resp.status_code == 200
    categories = resp.json()["categories"]
    stored_paths = {c["path"] for c in categories}
    for path in SEED_CATEGORIES:
        assert path in stored_paths, f"Missing seeded path: {path}"


def test_create_category_root(test_client):
    """POST /api/categories {name} creates a root category."""
    resp = test_client.post("/api/categories", json={"name": "Test Root"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Root"
    assert data["parent_id"] is None
    assert data["path"] == "Test Root"


def test_create_category_child(test_client):
    """POST /api/categories {name, parent_id} creates a child."""
    resp = test_client.get("/api/categories")
    categories = resp.json()["categories"]
    income = [c for c in categories if c["name"] == "Income"][0]

    resp = test_client.post("/api/categories", json={
        "name": "Test Child",
        "parent_id": income["id"],
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Child"
    assert data["parent_id"] == income["id"]
    assert data["path"] == "Income > Test Child"


def test_create_category_persists_across_restart(test_client):
    """Category created via POST survives restart (simulated by re-fetching)."""
    resp = test_client.get("/api/categories")
    categories = resp.json()["categories"]
    income = [c for c in categories if c["name"] == "Income"][0]

    resp = test_client.post("/api/categories", json={
        "name": "Persist Test",
        "parent_id": income["id"],
    })
    assert resp.status_code == 201
    new_id = resp.json()["id"]

    resp = test_client.get("/api/categories")
    all_cats = resp.json()["categories"]
    found = [c for c in all_cats if c["id"] == new_id]
    assert len(found) == 1
    assert found[0]["name"] == "Persist Test"
    assert found[0]["path"] == "Income > Persist Test"


def test_rename_category(test_client):
    """PUT /api/categories/{id} {name} renames."""
    resp = test_client.get("/api/categories")
    categories = resp.json()["categories"]
    income = [c for c in categories if c["name"] == "Income"][0]

    resp = test_client.put(f"/api/categories/{income['id']}", json={"name": "Earnings"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Earnings"
    assert data["path"] == "Earnings"

    resp = test_client.get("/api/categories")
    renamed = [c for c in resp.json()["categories"] if c["id"] == income["id"]]
    assert len(renamed) == 1
    assert renamed[0]["name"] == "Earnings"


def test_move_category(test_client):
    """PUT /api/categories/{id} {parent_id} moves the category."""
    resp = test_client.post("/api/categories", json={"name": "MoveSrc"})
    assert resp.status_code == 201
    src_id = resp.json()["id"]

    resp = test_client.post("/api/categories", json={"name": "MoveDst"})
    assert resp.status_code == 201
    dst_id = resp.json()["id"]

    resp = test_client.put(f"/api/categories/{src_id}", json={"parent_id": dst_id})
    assert resp.status_code == 200
    data = resp.json()
    assert data["parent_id"] == dst_id
    assert data["path"] == "MoveDst > MoveSrc"

    resp = test_client.get("/api/categories/tree")
    tree = resp.json()["tree"]
    dst_node = [n for n in tree if n["name"] == "MoveDst"][0]
    moved_children = [c for c in dst_node["children"] if c["name"] == "MoveSrc"]
    assert len(moved_children) == 1


def _any_txn_ids(test_client, count=2):
    """Real transaction ids from the DB.

    Hardcoding id 1 made these tests depend on being the first thing in the
    session to touch the transactions table: any earlier test that inserts and
    deletes rows consumes the low autoincrement ids, and `/api/transactions/1`
    then 404s. Ask for ids instead of assuming them.
    """
    resp = test_client.get("/api/transactions?page_size=50&scope=all")
    assert resp.status_code == 200, resp.text
    ids = [t["id"] for t in resp.json()["transactions"]]
    assert len(ids) >= count, f"need {count} transactions, got {len(ids)}"
    return ids[:count]


def test_tags_update_via_patch(test_client, monkeypatch):
    """PATCH /api/transactions/{id} {tags} stores both tags."""
    _import_with_mock(test_client, monkeypatch)
    (txn_id,) = _any_txn_ids(test_client, 1)

    resp = test_client.patch(
        f"/api/transactions/{txn_id}",
        json={"tags": ["person:xavier", "property:3323-ridge"]}
    )
    assert resp.status_code == 200

    resp = test_client.get(f"/api/transactions/{txn_id}")
    assert resp.status_code == 200
    data = resp.json()
    stored_tags = json.loads(data["tags"])
    assert "person:xavier" in stored_tags
    assert "property:3323-ridge" in stored_tags


def test_tag_filter_on_get(test_client, monkeypatch):
    """GET /api/transactions?tag=person:xavier returns the subset with that tag."""
    _import_with_mock(test_client, monkeypatch)
    first_id, second_id = _any_txn_ids(test_client, 2)

    resp = test_client.patch(
        f"/api/transactions/{first_id}",
        json={"tags": ["person:xavier"]}
    )
    assert resp.status_code == 200

    resp = test_client.patch(
        f"/api/transactions/{second_id}",
        json={"tags": ["person:yuki"]}
    )
    assert resp.status_code == 200

    # scope=all is required: this test's fixture data is CSV-imported history,
    # and the default scope is deliberately live-only (Plaid + manual entries).
    resp = test_client.get("/api/transactions?tag=person:xavier&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    for txn in data["transactions"]:
        tags = json.loads(txn["tags"])
        assert "person:xavier" in tags

    resp = test_client.get("/api/transactions?tag=person:yuki&scope=all")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1

    resp = test_client.get("/api/transactions?tag=nonexistent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0


def test_create_nonexistent_parent_returns_404(test_client):
    """POST /api/categories with nonexistent parent_id returns 404."""
    resp = test_client.post("/api/categories", json={
        "name": "Orphan",
        "parent_id": 99999,
    })
    assert resp.status_code == 404


def test_self_parent_returns_400(test_client):
    """PUT /api/categories/{id} with parent_id=self returns 400."""
    resp = test_client.post("/api/categories", json={"name": "SelfTest"})
    assert resp.status_code == 201
    cat_id = resp.json()["id"]

    resp = test_client.put(f"/api/categories/{cat_id}", json={"parent_id": cat_id})
    assert resp.status_code == 400


def test_nonexistent_category_returns_404(test_client):
    """PUT /api/categories/{id} on nonexistent returns 404."""
    resp = test_client.put("/api/categories/99999", json={"name": "Ghost"})
    assert resp.status_code == 404


def test_creating_a_duplicate_category_is_rejected(test_client):
    """Two rows with the same path break every path-keyed lookup and UI list.

    The category chips are keyed by path, so a duplicate collides as a React key
    and one of the two renders not at all — invisible and undeletable.
    """
    first = test_client.post("/api/categories", json={"name": "Dup Guard Test"})
    assert first.status_code == 201, first.text
    created_id = first.json()["id"]
    try:
        again = test_client.post("/api/categories", json={"name": "Dup Guard Test"})
        assert again.status_code == 409, again.text
        assert "already exists" in again.json()["detail"]

        # Case-insensitive: "groceries" and "Groceries" are the same category to a
        # person, and letting both exist splits every total that groups by name.
        cased = test_client.post("/api/categories", json={"name": "dup guard test"})
        assert cased.status_code == 409, cased.text

        listed = test_client.get("/api/categories").json()["categories"]
        assert sum(1 for c in listed if c["path"] == "Dup Guard Test") == 1
    finally:
        test_client.delete(f"/api/categories/{created_id}?force=true")


def test_creating_a_blank_category_is_rejected(test_client):
    resp = test_client.post("/api/categories", json={"name": "   "})
    assert resp.status_code == 400
