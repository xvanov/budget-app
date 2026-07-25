import csv
import os
from typing import List
from app.parsers.base import BaseParser


class UsBankParser(BaseParser):
    platform = "us_bank"
    account_id = "us_bank-checking"

    @classmethod
    def detect(cls, file_path: str) -> bool:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
                return '"Date","Transaction","Name","Memo","Amount"' in first
        except Exception:
            return False

    def parse(self, file_path: str) -> List[dict]:
        rows = []
        # US Bank names the export after the account ("Checking - 3262_…"), so
        # the mask comes from the file rather than from a constant.
        self.account_id = self.account_id_from_filename(
            file_path, self.platform, type(self).account_id
        )
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                if len(row) < 5:
                    continue
                date_str, txn_type, name, memo, amount = row
                amount_cents = self.parse_amount(amount)
                if amount_cents == 0:
                    continue

                merchant = name.strip()
                if not merchant or merchant == "MONTHLY MAINTENANCE FEE WAIVED":
                    merchant = memo.strip() or "US Bank Transaction"

                rows.append(self.make_row(
                    source_file=os.path.basename(file_path),
                    account_id=self.account_id,
                    posted_date=self.parse_date(date_str.strip()),
                    amount_cents=amount_cents,
                    merchant=merchant,
                    description=f"{name.strip()} | {memo.strip()}",
                ))
        return rows
