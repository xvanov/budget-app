import csv
import os
import re
from typing import List
from app.parsers.base import BaseParser


class CashAppParser(BaseParser):
    platform = "cash_app"
    account_id = "cash-app"

    @classmethod
    def detect(cls, file_path: str) -> bool:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                first = f.readline().strip()
                return '"Date","Transaction ID","Transaction Type"' in first
        except Exception:
            return False

    def parse(self, file_path: str) -> List[dict]:
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                if len(row) < 14:
                    continue
                date_str = row[0]
                txn_id = row[1]
                txn_type = row[2].strip()
                currency = row[3]
                amount_str = row[4]
                notes = row[11]
                name = row[12]

                if txn_type.lower() in ("account notifications", "account notification"):
                    continue

                amount_cents = self.parse_amount(amount_str)
                if amount_cents == 0:
                    continue

                merchant = name.strip() if name.strip() else "Cash App"
                posted_date = date_str.split(" ")[0]

                rows.append(self.make_row(
                    external_id=txn_id if txn_id and txn_id != "None" else None,
                    source_file=os.path.basename(file_path),
                    account_id=self.account_id,
                    posted_date=self.parse_date(posted_date),
                    amount_cents=amount_cents,
                    merchant=merchant,
                    description=f"{txn_type}: {notes}".strip(),
                ))
        return rows
