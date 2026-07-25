import csv
import os
import re
from typing import List
from app.parsers.base import BaseParser


class BofaChkParser(BaseParser):
    platform = "bofa"
    account_id = "bofa-checking"

    @classmethod
    def detect(cls, file_path: str) -> bool:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
                return first.startswith("Status,") and "Date" in first and "Description" in first and "Debit" in first
        except Exception:
            return False

    def parse(self, file_path: str) -> List[dict]:
        rows = []
        # The checking export carries the mask in its name (`CHK_1234_….csv`);
        # fall back to the generic id when it does not.
        self.account_id = self.account_id_from_filename(
            file_path, self.platform, type(self).account_id
        )
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                if len(row) < 5:
                    continue
                status, date_str, desc, debit, credit = row[:5]
                amount_cents = 0
                if debit.strip():
                    amount_cents = -abs(self.parse_amount(debit))
                elif credit.strip():
                    amount_cents = abs(self.parse_amount(credit))

                if amount_cents == 0:
                    continue

                merchant = self._extract_merchant(desc.strip())
                rows.append(self.make_row(
                    source_file=os.path.basename(file_path),
                    account_id=self.account_id,
                    posted_date=self.parse_date(date_str.strip()),
                    amount_cents=amount_cents,
                    merchant=merchant,
                    description=desc.strip(),
                ))
        return rows

    @staticmethod
    def _extract_merchant(desc: str) -> str:
        d = desc.strip()
        if "Zelle" in d:
            if "from" in d.lower():
                match = re.search(r'from ([^ ]+ [^ ]+)', d)
                return "Zelle: " + (match.group(1).title() if match else d)
            if "to" in d.lower():
                match = re.search(r'to ([^ ]+ [^ ]+)', d)
                return "Zelle: " + (match.group(1).title() if match else d)
        if "ACH Electronic Credit" in d or "ACH Electronic Debit" in d:
            return d.split(":")[-1].strip() if ":" in d else d
        d = d.replace(":", " ")
        return d.strip()


class BofaStmtParser(BaseParser):
    platform = "bofa"
    # The statement export carries no account mask anywhere in the file or its
    # name, so this format gets one stable id rather than a derived one.
    account_id = "bofa-stmt"

    @classmethod
    def detect(cls, file_path: str) -> bool:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                return "Beginning balance" in content and "Total credits" in content and "Summary Amt" in content
        except Exception:
            return False

    @staticmethod
    def _looks_like_amount(s: str) -> bool:
        """True if `s` parses as a non-empty numeric (handles $, commas, parens, quotes)."""
        cleaned = s.strip().replace('"', '').replace(',', '').replace('$', '').replace(' ', '')
        if cleaned.startswith('(') and cleaned.endswith(')'):
            cleaned = '-' + cleaned[1:-1]
        if cleaned in ('', '-'):
            return False
        try:
            float(cleaned)
            return True
        except (ValueError, TypeError):
            return False

    def parse(self, file_path: str) -> List[dict]:
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            # Use csv.reader so quoted fields containing commas
            # (e.g. amounts like "-3,250.00") are parsed correctly.
            reader = csv.reader(f)
            in_detail = False
            for parts in reader:
                if not parts:
                    continue
                # Detail section starts after the header
                # "Date,Description,Amount,Running Bal."
                if (
                    not in_detail
                    and len(parts) >= 4
                    and parts[0].strip() == "Date"
                    and parts[1].strip() == "Description"
                    and parts[2].strip() == "Amount"
                ):
                    in_detail = True
                    continue
                if not in_detail:
                    continue
                if len(parts) < 3:
                    continue

                # A well-formed BofA detail row has exactly 4 fields:
                # date, description, amount, running balance. BofA's exporter
                # sometimes emits unescaped inner double-quotes (e.g. for
                # `Renta Nov, room 2` inside a "Zelle payment from … for …"
                # description), which makes csv.reader split the description
                # on the inner comma and produce 5+ fields. Recover by treating
                # the last numeric field as balance, the one before it as
                # amount, and everything in between as the description.
                if len(parts) > 4 and self._looks_like_amount(parts[-1]) and self._looks_like_amount(parts[-2]):
                    date_str = parts[0].strip()
                    desc = ",".join(parts[1:-2]).strip().strip('"')
                    amount_str = parts[-2].strip().strip('"')
                else:
                    date_str = parts[0].strip()
                    desc = parts[1].strip().strip('"')
                    amount_str = parts[2].strip().strip('"')

                # Skip the "Beginning balance" sentinel row inside the detail
                # block (date present but amount empty).
                if "Beginning balance" in desc:
                    continue

                amount_cents = self.parse_amount(amount_str)
                if amount_cents == 0:
                    continue

                rows.append(self.make_row(
                    source_file=os.path.basename(file_path),
                    account_id=self.account_id,
                    posted_date=self.parse_date(date_str),
                    amount_cents=amount_cents,
                    merchant=self._extract_merchant(desc),
                    description=desc,
                    business_context="rental",
                ))
        return rows

    @staticmethod
    def _extract_merchant(desc: str) -> str:
        d = desc.strip()
        if "Zelle payment from" in d:
            match = re.search(r'from ([^ ]+ [^ ]+)', d)
            return "Zelle: " + (match.group(1).title() if match else d)
        if "Zelle payment to" in d:
            match = re.search(r'to ([^ ]+ [^ ]+)', d)
            return "Zelle: " + (match.group(1).title() if match else d)
        if "DEPOSIT" in d and "MOBILE" in d:
            match = re.search(r'DEPOSIT \*MOBILE', d)
            return "Mobile Deposit"
        for kw in ["SMUD", "DUKEENERGY", "Spectrum", "DOMINION ENERGY", "DURHAM UTIL"]:
            if kw in d:
                return kw.title()
        clean = d.split(":")[0].strip().split("  ")[0].strip()
        if "DES:" in clean:
            clean = clean.split("DES:")[0].strip()
        return clean[:60]
