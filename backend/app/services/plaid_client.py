"""Plaid API client factory and thin call wrappers.

A deliberate port of ``rental-management/src/lib/integrations/plaid.ts`` — same
env-resolution rules, same 730-day history request — so the two apps behave
identically against the same Plaid account.

Credentials come from env and are never read anywhere else:
    PLAID_CLIENT_ID
    PLAID_PRODUCTION_SECRET / PLAID_SANDBOX_SECRET / PLAID_SECRET
    PLAID_ENV (production | sandbox)

Env resolution (mirrors getPlaidClient() in the TS module): explicit PLAID_ENV
wins; otherwise production when a production secret is present, else sandbox.
The secret is then chosen to MATCH the resolved env, which is what stops a
sandbox secret from being sent to production and vice versa.

Every wrapper here returns plain dicts rather than Plaid model objects, so the
sync service and its tests never depend on the SDK's generated classes.
"""
import logging
import os
from typing import Any, Dict, Optional

import plaid
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.products import Products
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.transactions_sync_request_options import TransactionsSyncRequestOptions

logger = logging.getLogger(__name__)

# Plaid's maximum. Requesting it is what backfills years of history on a fresh
# link instead of the ~90-day default.
DAYS_REQUESTED = 730
CLIENT_NAME = "Budget App"

_cached_client: Optional[plaid_api.PlaidApi] = None


class PlaidConfigError(RuntimeError):
    """Missing or contradictory Plaid configuration."""


def resolve_env() -> str:
    explicit = os.environ.get("PLAID_ENV")
    if explicit:
        env = explicit.strip().lower()
        if env not in ("production", "sandbox"):
            raise PlaidConfigError(
                f'Invalid PLAID_ENV "{explicit}" (expected production or sandbox).'
            )
        return env
    return "production" if os.environ.get("PLAID_PRODUCTION_SECRET") else "sandbox"


def _resolve_secret(env: str) -> Optional[str]:
    generic = os.environ.get("PLAID_SECRET")
    if env == "production":
        return os.environ.get("PLAID_PRODUCTION_SECRET") or generic
    return os.environ.get("PLAID_SANDBOX_SECRET") or generic


def get_plaid_client() -> plaid_api.PlaidApi:
    global _cached_client
    if _cached_client is not None:
        return _cached_client

    client_id = os.environ.get("PLAID_CLIENT_ID")
    env = resolve_env()
    secret = _resolve_secret(env)
    if not client_id or not secret:
        expected = "PLAID_PRODUCTION_SECRET" if env == "production" else "PLAID_SANDBOX_SECRET"
        raise PlaidConfigError(
            f"PLAID_CLIENT_ID and a matching secret are required "
            f"(env={env}; looked for {expected} or PLAID_SECRET)."
        )

    host = plaid.Environment.Production if env == "production" else plaid.Environment.Sandbox
    configuration = plaid.Configuration(
        host=host,
        api_key={"clientId": client_id, "secret": secret, "plaidVersion": "2020-09-14"},
    )
    # Log the resolved environment only — never the credentials.
    logger.info("Plaid client initialised (env=%s)", env)
    _cached_client = plaid_api.PlaidApi(plaid.ApiClient(configuration))
    return _cached_client


def reset_client_cache() -> None:
    """Drop the cached client so a changed env is picked up (used by tests)."""
    global _cached_client
    _cached_client = None


def is_configured() -> bool:
    try:
        env = resolve_env()
        return bool(os.environ.get("PLAID_CLIENT_ID") and _resolve_secret(env))
    except PlaidConfigError:
        return False


def create_link_token(user_id: str = "budget-app-owner") -> str:
    """Create a Link token for the browser Plaid Link flow."""
    request = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=user_id),
        client_name=CLIENT_NAME,
        products=[Products("transactions")],
        country_codes=[CountryCode("US")],
        language="en",
        transactions=LinkTokenTransactions(days_requested=DAYS_REQUESTED),
    )
    return get_plaid_client().link_token_create(request).to_dict()["link_token"]


def exchange_public_token(public_token: str) -> Dict[str, str]:
    """Exchange a Link public_token for a durable access_token + item_id."""
    response = get_plaid_client().item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    ).to_dict()
    return {"access_token": response["access_token"], "item_id": response["item_id"]}


def fetch_item_accounts(access_token: str) -> Dict[str, Any]:
    """Accounts + institution for a linked item."""
    response = get_plaid_client().accounts_get(
        AccountsGetRequest(access_token=access_token)
    ).to_dict()
    return {
        "institution_id": (response.get("item") or {}).get("institution_id"),
        "accounts": response.get("accounts") or [],
    }


def transactions_sync(access_token: str, cursor: Optional[str] = None, count: int = 500) -> Dict[str, Any]:
    """One page of transactions/sync. The caller follows has_more."""
    kwargs: Dict[str, Any] = {
        "access_token": access_token,
        "count": count,
        "options": TransactionsSyncRequestOptions(include_personal_finance_category=True),
    }
    # Plaid rejects an explicit null cursor; omit it entirely for the first page.
    if cursor:
        kwargs["cursor"] = cursor
    return get_plaid_client().transactions_sync(TransactionsSyncRequest(**kwargs)).to_dict()


def remove_item(access_token: str) -> None:
    """Revoke an item's access token on Plaid's side (best-effort teardown)."""
    get_plaid_client().item_remove(ItemRemoveRequest(access_token=access_token))


def error_code_of(exc: Exception) -> str:
    """Best-effort extraction of Plaid's error_code from an ApiException.

    Plaid returns a JSON body carrying error_code; the SDK surfaces it as a
    string on the exception. Parsing it is what lets the sync service mark an
    item LOGIN_REQUIRED (re-auth needed) rather than a generic failure.
    """
    body = getattr(exc, "body", None)
    if body:
        try:
            import json

            parsed = json.loads(body if isinstance(body, str) else body.decode())
            code = parsed.get("error_code")
            if code:
                return str(code)
        except Exception:
            pass
    return "SYNC_ERROR"
