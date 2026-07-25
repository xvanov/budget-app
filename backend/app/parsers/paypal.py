import csv
import os
from typing import List
from app.parsers.base import BaseParser


class PayPalParser(BaseParser):
    platform = "paypal"
    account_id = "paypal"

    @classmethod
    def detect(cls, file_path: str) -> bool:
        try:
            with open(file_path, "r", encoding="utf-8-sig") as f:
                first = f.readline().strip()
                return '"Date","Time","TimeZone","Name"' in first and '"Amount"' in first
        except Exception:
            return False

    def parse(self, file_path: str) -> List[dict]:
        rows = []
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            next(reader)
            for row in reader:
                if len(row) < 8:
                    continue
                date_str = row[0]
                time_str = row[1]
                name = row[3]
                txn_type = row[4].strip()
                status = row[5].strip()
                amount_str = row[7]
                item_title = row[14] if len(row) > 14 else ""

                if status.lower() != "completed":
                    continue
                if txn_type.lower() == "bank deposit to pp account":
                    continue

                amount_cents = self.parse_amount(amount_str)
                if amount_cents == 0:
                    continue

                merchant = name.strip() if name.strip() else "PayPal"
                description = item_title.strip() if item_title.strip() else txn_type

                rows.append(self.make_row(
                    source_file=os.path.basename(file_path),
                    account_id=self.account_id,
                    posted_date=self.parse_date(date_str.strip()),
                    amount_cents=amount_cents,
                    merchant=merchant,
                    description=description,
                ))
        return rows
