from abc import ABC, abstractmethod
from typing import List, Optional
import os
import re

class BaseParser(ABC):
    platform: str = ""
    account_id: str = ""

    @staticmethod
    def account_id_from_filename(path: str, platform: str, fallback: str) -> str:
        """`<platform>-<mask>` from the digits banks already put in the filename.

        Exports name themselves after the account: `Chase9944_Activity_….csv`,
        `Checking - 3262_01-01-2024_….csv`. Reading the mask out of the name is
        what lets this parser work for anyone's export instead of only for the
        accounts someone once listed in code.

        Only the FIRST 4-digit group is considered, because every one of these
        filenames carries dates after the mask and a date would otherwise win.
        `fallback` is returned when the name has no mask at all — a real case
        (`stmt.csv`), so it must not produce a bogus account.
        """
        name = os.path.basename(path or "")
        match = re.search(r"(?<!\d)(\d{4})(?!\d)", name)
        if not match:
            return fallback
        return f"{platform}-{match.group(1)}"

    @classmethod
    @abstractmethod
    def detect(cls, file_path: str) -> bool:
        pass

    @abstractmethod
    def parse(self, file_path: str) -> List[dict]:
        pass

    @staticmethod
    def normalize_description(desc: str) -> str:
        # Quote stripping is load-bearing for dedup, not cosmetic. BofA's
        # exporter emits unescaped inner double-quotes (`... for "May rent";
        # Conf# X`), so how many quotes survive depends on how csv.reader
        # happened to split the row — see the parts[1:-2] join in
        # parsers/bofa.py. The same real transaction therefore reached the DB
        # as both `for "may rent"` and `for may rent"`, fingerprinted
        # differently, and re-importing stmt.csv inserted 337 phantom
        # duplicates. Normalizing quotes away collapses both forms.
        s = desc.lower().strip()
        collapsed = re.sub(r'\s+', ' ', s)  # kept as a fallback, see below
        s = re.sub(r'["\']', '', collapsed)
        s = re.sub(r'#\d+', '', s)
        s = re.sub(r'\*+', '', s)
        s = re.sub(r'\d{8,}', '', s)
        s = s.strip()
        # Never blank out a description that had content. Stripping quotes/digits
        # can empty a string that was only punctuation (a bare `""` field), and a
        # blank fingerprint half would make unrelated rows collide on dedup.
        return s or collapsed

    @staticmethod
    def parse_amount(amount_str: str) -> int:
        cleaned = amount_str.strip().replace('"', '').replace(',', '').replace('$', '').replace(' ', '')
        if cleaned.startswith('(') and cleaned.endswith(')'):
            cleaned = '-' + cleaned[1:-1]
        if cleaned == '' or cleaned == '-':
            return 0
        try:
            return int(round(float(cleaned) * 100))
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def parse_date(date_str: str) -> Optional[str]:
        from datetime import datetime
        for fmt in ['%m/%d/%Y', '%Y-%m-%d', '%m-%d-%Y', '%m/%d/%y']:
            try:
                return datetime.strptime(date_str.strip(), fmt).strftime('%Y-%m-%d')
            except (ValueError, AttributeError):
                continue
        return None

    def make_row(self, **kwargs) -> dict:
        row = {
            "external_id": kwargs.get("external_id"),
            "source_platform": self.platform,
            "source_file": kwargs.get("source_file", ""),
            "account_id": kwargs.get("account_id", self.account_id),
            "posted_date": kwargs.get("posted_date", ""),
            "transaction_date": kwargs.get("transaction_date"),
            "amount_cents": kwargs.get("amount_cents", 0),
            "currency": "USD",
            "merchant": kwargs.get("merchant", ""),
            "description": kwargs.get("description", ""),
            "category_path": None,
            "entity_type": None,
            "asset_class": None,
            "business_context": kwargs.get("business_context", "personal"),
            "linked_transaction_id": None,
            "confidence_score": None,
            "categorization_reason": None,
            "applied_rule_id": None,
            "tags": "[]",
            "notes": None,
            "flagged": 0,
            "flag_reason": None,
        }
        return row
