"""The link route must store the bank's name, never its id. Plaid is stubbed."""
from types import SimpleNamespace

from app.services import plaid_client


class _Resp:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class _Client:
    def __init__(self, lookup_ok=True):
        self.lookup_ok = lookup_ok

    def accounts_get(self, req):
        return _Resp({"item": {"institution_id": "ins_56"}, "accounts": [{"account_id": "a1"}]})

    def institutions_get_by_id(self, req):
        if not self.lookup_ok:
            raise RuntimeError("plaid unreachable")
        assert req.institution_id == "ins_56"
        return _Resp({"institution": {"institution_id": "ins_56", "name": "Chase"}})


def test_fetch_item_accounts_returns_the_bank_name(monkeypatch):
    monkeypatch.setattr(plaid_client, "get_plaid_client", lambda: _Client())
    details = plaid_client.fetch_item_accounts("access-token")
    assert details["institution_id"] == "ins_56"
    assert details["institution_name"] == "Chase"


def test_a_failed_name_lookup_gives_none_not_the_id(monkeypatch):
    monkeypatch.setattr(plaid_client, "get_plaid_client", lambda: _Client(lookup_ok=False))
    details = plaid_client.fetch_item_accounts("access-token")
    assert details["institution_name"] is None
    assert details["institution_id"] == "ins_56"
