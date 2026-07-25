import os
import re
from typing import List
from app.parsers.base import BaseParser


class MortgagePdfParser(BaseParser):
    platform = "mortgage"
    account_id = "mortgage"

    @classmethod
    def detect(cls, file_path: str) -> bool:
        try:
            with open(file_path, "rb") as f:
                header = f.read(10)
                if not header.startswith(b"%PDF"):
                    return False
                content = open(file_path, "rb").read().lower()
                return b"amort" in content
        except Exception:
            return False

    def parse(self, file_path: str) -> List[dict]:
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        rows = []
        for page in reader.pages:
            text = page.extract_text()
            lines = text.split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue

                match = re.match(
                    r"(\d{2}/\d{2}/\d{4})\s+[$]?([\d,]+\.\d{2})\s+[$]?([\d,]+\.\d{2})\s+[$]?([\d,]+\.\d{2})",
                    line
                )
                if match:
                    date_str = match.group(1)
                    starting_balance = self._parse_dollar(match.group(2))
                    principal = self._parse_dollar(match.group(3))
                    interest = self._parse_dollar(match.group(4))

                    # Try to extract ending balance (last field)
                    remaining = line[match.end():].strip()
                    ending_balance = None
                    ending_match = re.search(r'[$]?([\d,]+\.\d{2})\s*$', remaining)
                    if ending_match:
                        ending_balance = self._parse_dollar(ending_match.group(1))

                    rows.append({
                        "month": self._convert_date(date_str),
                        "principal_cents": principal,
                        "interest_cents": interest,
                        "ending_balance_cents": ending_balance or (starting_balance - principal),
                    })
        return rows

    @staticmethod
    def _parse_dollar(s: str) -> int:
        return int(round(float(s.replace(",", "")) * 100))

    @staticmethod
    def _convert_date(d: str) -> str:
        parts = d.split("/")
        return f"{parts[2]}-{parts[0].zfill(2)}-01"
