from app.parsers.chase import ChaseCreditParser, ChaseCheckingParser
from app.parsers.us_bank import UsBankParser
from app.parsers.bofa import BofaChkParser, BofaStmtParser
from app.parsers.cash_app import CashAppParser
from app.parsers.paypal import PayPalParser
from app.parsers.venmo import VenmoParser
from app.parsers.mortgage import MortgagePdfParser
from app.parsers.paystub import PaystubPdfParser

PARSERS = [
    ChaseCreditParser,
    ChaseCheckingParser,
    UsBankParser,
    BofaChkParser,
    BofaStmtParser,
    CashAppParser,
    PayPalParser,
    VenmoParser,
    MortgagePdfParser,
    PaystubPdfParser,
]

def detect_parser(file_path: str):
    for cls in PARSERS:
        if cls.detect(file_path):
            return cls
    return None
