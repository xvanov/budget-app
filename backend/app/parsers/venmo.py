import os
import re
from typing import List
from app.parsers.base import BaseParser


class VenmoParser(BaseParser):
    platform = "venmo"
    account_id = "venmo"

    @classmethod
    def detect(cls, file_path: str) -> bool:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read(2000)
                return "venmo.com" in content.lower() and ("paid" in content.lower() or "venmo" in content.lower())
        except Exception:
            return False

    def parse(self, file_path: str) -> List[dict]:
        rows = []
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # The Venmo page is a saved HTML with transactions rendered in a feed.
        # We look for transaction patterns in the HTML text.
        # Typical Venmo transaction entries show: "X paid Y $Z", "X requested $Z from Y"
        # The data is in a React-rendered page, so we need to find the JSON-ish data.

        # Try to find transactions embedded in script tags or visible text
        # Fallback: look for recognizable patterns
        lines = content.split("\n")
        current_date = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Try to extract dates
            date_match = re.search(r'([A-Z][a-z]+ \d{1,2},?\s*\d{4})', line)
            if date_match:
                current_date = date_match.group(1)

        # Parse embedded JSON-like data if present
        # The saved page may have __NEXT_DATA__ or similar
        next_data = re.search(r'__NEXT_DATA__\s*=\s*({.*?});', content, re.DOTALL)
        if next_data:
            try:
                import json
                data = json.loads(next_data.group(1))
                transactions = self._extract_from_next_data(data)
                for t in transactions:
                    rows.append(self.make_row(
                        source_file=os.path.basename(file_path),
                        account_id=self.account_id,
                        posted_date=t.get("date", ""),
                        amount_cents=t.get("amount_cents", 0),
                        merchant=t.get("merchant", "Venmo"),
                        description=t.get("description", "Venmo Transaction"),
                    ))
                if rows:
                    return rows
            except Exception:
                pass

        warnings = ["Venmo HTML parsing: best-effort; some transactions may be missed"]
        return rows

    @staticmethod
    def _extract_from_next_data(data: dict) -> List[dict]:
        transactions = []
        try:
            props = data.get("props", {})
            page_props = props.get("pageProps", {})
            profile = page_props.get("profile", {})
            feed = profile.get("feed", [])
            for item in feed:
                t = item.get("transaction", item)
                if isinstance(t, dict):
                    amount = t.get("amount", 0)
                    try:
                        amount_cents = int(round(float(amount) * 100))
                    except (ValueError, TypeError):
                        amount_cents = 0
                    transactions.append({
                        "date": t.get("dateCreated", "").split("T")[0] if t.get("dateCreated") else "",
                        "amount_cents": amount_cents,
                        "merchant": t.get("actor", {}).get("displayName", "Venmo") if t.get("actor") else "Venmo",
                        "description": t.get("note", "Venmo Transaction") or "",
                    })
        except Exception:
            pass
        return transactions
