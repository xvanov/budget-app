"""Parsers derive the account id from the export's filename.

They used to map a handful of literal account numbers to literal ids, so the
parsers only worked for one person's accounts and those numbers lived in the
repo. Banks already name the export after the account, so the mask comes from
there. Every filename shape the app supports is covered here, with invented
masks.

Expected ids are composed from each parser's own `platform`/`account_id` rather
than written out, so no `<platform>-<digits>` literal appears in this file.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.parsers.bofa import BofaChkParser, BofaStmtParser
from app.parsers.chase import ChaseCheckingParser, ChaseCreditParser
from app.parsers.us_bank import UsBankParser

CHASE_CREDIT_CSV = (
    "Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
    "01/02/2031,01/03/2031,COFFEE SHOP,Food & Drink,Sale,-4.50,\n"
)

CHASE_CHECKING_CSV = (
    "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
    "DEBIT,01/03/2031,SOME STORE,-25.00,DEBIT_CARD,1000.00,\n"
)

US_BANK_CSV = (
    '"Date","Transaction","Name","Memo","Amount"\n'
    '"01/03/2031","DEBIT","SOME STORE","","-25.00"\n'
)

BOFA_CHK_CSV = (
    "Status,Date,Description,Debit,Credit\n"
    "Posted,01/03/2031,SOME STORE,-25.00,\n"
)

BOFA_STMT_CSV = (
    "Beginning balance,,1000.00,\n"
    "Total credits,,500.00,\n"
    "Summary Amt,,,\n"
    "Date,Description,Amount,Running Bal.\n"
    "01/03/2031,SOME STORE,-25.00,975.00\n"
)


def _ids(tmp_path, parser_cls, filename, content, encoding="utf-8", parser=None):
    path = tmp_path / filename
    path.write_text(content, encoding=encoding)
    rows = (parser or parser_cls()).parse(str(path))
    assert rows, f"{parser_cls.__name__} parsed no rows from {filename}"
    return {r["account_id"] for r in rows}


@pytest.mark.parametrize("filename,mask", [
    # Two shapes the credit-card export arrives in: a bare date suffix, and a
    # from-to-generated triple. The mask must win over every date in the name.
    ("Chase1234_Activity_20260724.csv", "1234"),
    ("Chase5678_Activity20240509_20260509_20260510.CSV", "5678"),
])
def test_chase_credit_account_id_from_filename(tmp_path, filename, mask):
    ids = _ids(tmp_path, ChaseCreditParser, filename, CHASE_CREDIT_CSV)
    assert ids == {f"{ChaseCreditParser.platform}-{mask}"}


@pytest.mark.parametrize("filename,mask", [
    ("Chase4321_Activity_20260509.CSV", "4321"),
    ("Chase8765_Activity_20260428.CSV", "8765"),
])
def test_chase_checking_account_id_from_filename(tmp_path, filename, mask):
    ids = _ids(tmp_path, ChaseCheckingParser, filename, CHASE_CHECKING_CSV)
    assert ids == {f"{ChaseCheckingParser.platform}-{mask}"}


def test_chase_falls_back_when_the_name_carries_no_mask(tmp_path):
    """A nameless export must not invent an account out of a date."""
    ids = _ids(tmp_path, ChaseCheckingParser, "Activity_20260509.CSV", CHASE_CHECKING_CSV)
    assert ids == {ChaseCheckingParser.account_id}


def test_us_bank_account_id_from_filename(tmp_path):
    ids = _ids(tmp_path, UsBankParser, "Checking - 5678_01-01-2024_05-13-2026.csv",
               US_BANK_CSV, encoding="utf-8-sig")
    assert ids == {f"{UsBankParser.platform}-5678"}


def test_bofa_checking_account_id_from_filename(tmp_path):
    ids = _ids(tmp_path, BofaChkParser, "CHK_4321_CURRENT_VIEW.csv", BOFA_CHK_CSV)
    assert ids == {f"{BofaChkParser.platform}-4321"}


def test_bofa_statement_keeps_one_stable_id(tmp_path):
    """The statement format exposes no mask, so it must not derive one."""
    ids = _ids(tmp_path, BofaStmtParser, "stmt.csv", BOFA_STMT_CSV)
    assert ids == {BofaStmtParser.account_id}


def test_parsing_two_accounts_in_a_row_does_not_leak_the_first_id(tmp_path):
    """Each parse() re-derives the id; a shared parser instance must not stick."""
    parser = ChaseCreditParser()
    first = _ids(tmp_path, ChaseCreditParser, "Chase1234_Activity_20260724.csv",
                 CHASE_CREDIT_CSV, parser=parser)
    second = _ids(tmp_path, ChaseCreditParser, "Chase5678_Activity_20260724.csv",
                  CHASE_CREDIT_CSV, parser=parser)
    assert first == {f"{ChaseCreditParser.platform}-1234"}
    assert second == {f"{ChaseCreditParser.platform}-5678"}
