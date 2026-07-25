"""Plaid env resolution — the part that can silently do the wrong thing.

Sending a sandbox secret to production (or vice versa) fails in confusing ways,
so these tests pin the resolution rules ported from plaid.ts. No network calls:
only configuration logic is exercised.
"""
import pytest

from app.services import plaid_client
from app.services.plaid_client import PlaidConfigError, is_configured, resolve_env


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "PLAID_ENV",
        "PLAID_CLIENT_ID",
        "PLAID_SECRET",
        "PLAID_PRODUCTION_SECRET",
        "PLAID_SANDBOX_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    plaid_client.reset_client_cache()
    yield
    plaid_client.reset_client_cache()


def test_defaults_to_production_when_a_production_secret_exists(monkeypatch):
    monkeypatch.setenv("PLAID_PRODUCTION_SECRET", "prod-secret")
    assert resolve_env() == "production"


def test_defaults_to_sandbox_without_a_production_secret(monkeypatch):
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "sandbox-secret")
    assert resolve_env() == "sandbox"


def test_explicit_env_wins_over_secret_presence(monkeypatch):
    """Set PLAID_ENV=sandbox and a production secret must NOT be what gets used."""
    monkeypatch.setenv("PLAID_PRODUCTION_SECRET", "prod-secret")
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "sandbox-secret")
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    assert resolve_env() == "sandbox"
    assert plaid_client._resolve_secret("sandbox") == "sandbox-secret"


def test_secret_matches_resolved_env(monkeypatch):
    monkeypatch.setenv("PLAID_PRODUCTION_SECRET", "prod-secret")
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "sandbox-secret")
    assert plaid_client._resolve_secret("production") == "prod-secret"
    assert plaid_client._resolve_secret("sandbox") == "sandbox-secret"


def test_generic_secret_is_the_fallback_for_either_env(monkeypatch):
    monkeypatch.setenv("PLAID_SECRET", "generic-secret")
    assert plaid_client._resolve_secret("production") == "generic-secret"
    assert plaid_client._resolve_secret("sandbox") == "generic-secret"


@pytest.mark.parametrize("bad", ["development", "PROD", "", " staging "])
def test_invalid_env_raises(monkeypatch, bad):
    monkeypatch.setenv("PLAID_ENV", bad)
    if bad == "":
        # Empty string is treated as unset, matching the TS `??` behaviour.
        assert resolve_env() == "sandbox"
    else:
        with pytest.raises(PlaidConfigError):
            resolve_env()


def test_missing_credentials_raise_an_actionable_error(monkeypatch):
    monkeypatch.setenv("PLAID_ENV", "production")
    assert is_configured() is False
    with pytest.raises(PlaidConfigError, match="PLAID_PRODUCTION_SECRET"):
        plaid_client.get_plaid_client()


def test_is_configured_true_when_id_and_matching_secret_present(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "abc123")
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "sandbox-secret")
    assert is_configured() is True


def test_is_configured_false_when_only_the_wrong_env_secret_is_present(monkeypatch):
    """Production env + only a sandbox secret must not look configured."""
    monkeypatch.setenv("PLAID_CLIENT_ID", "abc123")
    monkeypatch.setenv("PLAID_ENV", "production")
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "sandbox-secret")
    assert is_configured() is False


def test_client_is_cached_and_resettable(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "abc123")
    monkeypatch.setenv("PLAID_SANDBOX_SECRET", "sandbox-secret")
    first = plaid_client.get_plaid_client()
    assert plaid_client.get_plaid_client() is first
    plaid_client.reset_client_cache()
    assert plaid_client.get_plaid_client() is not first


def test_days_requested_is_plaid_maximum():
    """730 days is what buys full history on a fresh link."""
    assert plaid_client.DAYS_REQUESTED == 730


def test_error_code_extraction():
    class FakeApiException(Exception):
        body = '{"error_type":"ITEM_ERROR","error_code":"ITEM_LOGIN_REQUIRED"}'

    assert plaid_client.error_code_of(FakeApiException()) == "ITEM_LOGIN_REQUIRED"
    assert plaid_client.error_code_of(Exception("no body")) == "SYNC_ERROR"

    class BadBody(Exception):
        body = "not json"

    assert plaid_client.error_code_of(BadBody()) == "SYNC_ERROR"
