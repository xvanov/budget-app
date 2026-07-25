import csv
import os
import re
from typing import List
from app.parsers.base import BaseParser


class ChaseCreditParser(BaseParser):
    platform = "chase"
    # Fallback id, used only when the export's name carries no account mask.
    account_id = "chase-credit"

    HEADER = ["Transaction Date", "Post Date", "Description", "Category", "Type", "Amount", "Memo"]

    @classmethod
    def detect(cls, file_path: str) -> bool:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
                parts = [p.strip() for p in first.split(",")]
                return len(parts) >= 6 and parts[0] == "Transaction Date" and parts[2] == "Description"
        except Exception:
            return False

    def _account_from_filename(self, filename: str) -> str:
        return self.account_id_from_filename(filename, self.platform, type(self).account_id)

    def parse(self, file_path: str) -> List[dict]:
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            self.account_id = self._account_from_filename(file_path)
            for row in reader:
                if len(row) < 6:
                    continue
                trans_date, post_date, desc, cat, txn_type, amount = row[:6]
                memo = row[6] if len(row) > 6 else ""

                amount_cents = self.parse_amount(amount)
                is_return = txn_type.strip().lower() == "return"

                rows.append(self.make_row(
                    source_file=self._short_name(file_path),
                    account_id=self.account_id,
                    posted_date=self.parse_date(post_date.strip()),
                    transaction_date=self.parse_date(trans_date.strip()),
                    amount_cents=abs(amount_cents) if is_return else -abs(amount_cents),
                    merchant=self._clean_merchant(desc),
                    description=desc.strip(),
                ))
        return rows

    @staticmethod
    def _clean_merchant(desc: str) -> str:
        d = desc.strip()
        for prefix in ["AMZN Mktp US", "Amazon.com", "AMAZON MKTPL"]:
            if d.startswith(prefix):
                return "Amazon"
        return d.split("*")[0].strip().split("#")[0].strip() if "*" in d or "#" in d else d

    @staticmethod
    def _short_name(path: str) -> str:
        import os
        return os.path.basename(path)


class ChaseCheckingParser(BaseParser):
    platform = "chase"
    # Fallback id, used only when the export's name carries no account mask.
    account_id = "chase-checking"

    HEADER = ["Details", "Posting Date", "Description", "Amount", "Type", "Balance", "Check or Slip #"]

    @classmethod
    def detect(cls, file_path: str) -> bool:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
                parts = [p.strip() for p in first.split(",")]
                return len(parts) >= 5 and parts[0] == "Details" and parts[1] == "Posting Date"
        except Exception:
            return False

    def _account_from_filename(self, filename: str) -> str:
        return self.account_id_from_filename(filename, self.platform, type(self).account_id)

    def parse(self, file_path: str) -> List[dict]:
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            self.account_id = self._account_from_filename(file_path)
            for row in reader:
                if len(row) < 6:
                    continue
                details, post_date, desc, amount, txn_type, balance = row[:6]

                amount_cents = self.parse_amount(amount)
                if amount_cents == 0:
                    continue

                merchant = self._extract_merchant(desc, txn_type)

                rows.append(self.make_row(
                    source_file=os.path.basename(file_path),
                    account_id=self.account_id,
                    posted_date=self.parse_date(post_date.strip()),
                    amount_cents=amount_cents,
                    merchant=merchant,
                    description=desc.strip(),
                ))
        return rows

    @staticmethod
    def _extract_merchant(desc: str, txn_type: str) -> str:
        d = desc.strip()
        t = txn_type.strip().upper()

        if t in ("LOAN_PMT",):
            if "HMG" in d:
                return "HMG Mortgage"
            return "Chase Loan Payment"
        if t == "ACCT_XFER":
            if "from" in d.lower() or "to" in d.lower():
                return "Account Transfer"
            return "Account Transfer"
        if t in ("ACH_DEBIT", "MISC_DEBIT"):
            match = re.search(r'ORIG CO NAME:([^\s]+)', d)
            return match.group(1).title() if match else d.split(",")[0].strip()
        if t in ("ACH_CREDIT", "MISC_CREDIT"):
            if "IRS" in d or "TREAS" in d:
                return "IRS Tax Refund"
            if "NCDOR" in d:
                return "NC Tax Refund"
            match = re.search(r'ORIG CO NAME:([^\s]+)', d)
            return match.group(1).title() if match else d.split(",")[0].strip()
        if t in ("DEBIT_CARD",):
            return d.split("  ")[0].strip().split(",")[0].strip()
        if t == "CHASE_TO_PARTNERFI":
            return d.split(" to ")[-1].split(" ")[0].strip() if " to " in d else "Zelle Payment"
        if t == "BASIC_PAYROLL":
            return "Payroll"
        if t in ("CHECK_DEPOSIT", "DSLIP"):
            return "Check Deposit"
        if t in ("WIRE_INCOMING",):
            match = re.search(r'B/O:\s*([^ ]+ [^ ]+)', d)
            return "Wire: " + (match.group(1).title() if match else "Incoming")
        if t == "FEE_TRANSACTION":
            return "Bank Fee"
        return d.split(",")[0].strip()[:40]
