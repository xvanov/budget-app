"""Tests for Story 00: Budget context profile."""

import os
import sys
import json
import tempfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.llm import read_budget_context, build_system_prompt, log_outbound_payload, BUDGET_CONTEXT_PATH


def test_read_empty_context():
    content = read_budget_context()
    assert isinstance(content, str)


def test_build_system_prompt_with_context(monkeypatch, tmp_path):
    ctx_file = tmp_path / "budget_context.md"
    ctx_file.write_text("# Test Context\nAccounts: card-0001, cash-app, bank-0001\n")
    monkeypatch.setattr("app.llm.BUDGET_CONTEXT_PATH", str(ctx_file))

    prompt = build_system_prompt()
    assert "Budget Context" in prompt
    assert "# Test Context" in prompt
    assert "card-0001" in prompt


def test_build_system_prompt_empty_context(monkeypatch, tmp_path):
    ctx_file = tmp_path / "budget_context.md"
    ctx_file.write_text("")
    monkeypatch.setattr("app.llm.BUDGET_CONTEXT_PATH", str(ctx_file))

    prompt = build_system_prompt()
    assert "Budget Context" not in prompt
    assert "categorization assistant" in prompt


def test_build_system_prompt_with_extra_context(monkeypatch, tmp_path):
    ctx_file = tmp_path / "budget_context.md"
    ctx_file.write_text("# Test")
    monkeypatch.setattr("app.llm.BUDGET_CONTEXT_PATH", str(ctx_file))

    prompt = build_system_prompt(extra_context="## Ground Truth Rules\n1. Cash App = rental income\n")
    assert "## Ground Truth Rules" in prompt
    assert "Cash App = rental income" in prompt


def test_log_outbound_payload_includes_context(caplog):
    import logging
    caplog.set_level(logging.INFO)
    log_outbound_payload("some prompt with budget context mentioned", "haiku")
    assert "LLM call" in caplog.text
    assert "system_prompt_includes_context=True" in caplog.text or "True" in caplog.text


def test_context_file_persistence(monkeypatch, tmp_path):
    ctx_file = tmp_path / "budget_context.md"
    ctx_file.write_text("initial content")
    monkeypatch.setattr("app.routers.context.BUDGET_CONTEXT_PATH", str(ctx_file))

    from app.routers.context import _read_context_file, _write_context_file

    content, last_mod = _read_context_file()
    assert content == "initial content"
    assert last_mod is not None

    _write_context_file("updated content")
    content2, _ = _read_context_file()
    assert content2 == "updated content"


@pytest.mark.asyncio
async def test_context_api_get(monkeypatch, tmp_path, test_client):
    ctx_file = tmp_path / "budget_context.md"
    ctx_file.write_text("api test content")
    monkeypatch.setattr("app.routers.context.BUDGET_CONTEXT_PATH", str(ctx_file))
    monkeypatch.setattr("app.llm.BUDGET_CONTEXT_PATH", str(ctx_file))

    response = test_client.get("/api/context")
    assert response.status_code == 200
    data = response.json()
    assert data["content"] == "api test content"
    assert data["last_modified"] is not None


@pytest.mark.asyncio
async def test_context_api_put(monkeypatch, tmp_path, test_client):
    ctx_file = tmp_path / "budget_context.md"
    ctx_file.write_text("")
    monkeypatch.setattr("app.routers.context.BUDGET_CONTEXT_PATH", str(ctx_file))
    monkeypatch.setattr("app.llm.BUDGET_CONTEXT_PATH", str(ctx_file))

    response = test_client.put("/api/context", json={"content": "new content"})
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    get_resp = test_client.get("/api/context")
    assert get_resp.json()["content"] == "new content"
