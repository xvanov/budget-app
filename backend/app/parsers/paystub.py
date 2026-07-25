import os
import re
import subprocess
from typing import List, Optional
from app.parsers.base import BaseParser


class PaystubPdfParser(BaseParser):
    platform = "paystub"
    account_id = "payroll"

    @classmethod
    def detect(cls, file_path: str) -> bool:
        try:
            fname = os.path.basename(file_path).lower()
            if fname.startswith("paystub-") and fname.endswith(".pdf"):
                return True
            with open(file_path, "rb") as f:
                header = f.read(10)
                if not header.startswith(b"%PDF"):
                    return False
            try:
                result = subprocess.run(
                    ["pdftotext", "-layout", file_path, "-"],
                    capture_output=True, text=True, timeout=15
                )
                if result.returncode == 0:
                    text = result.stdout.lower()
                    return "earnings statement" in text and "worksite employer" in text
            except Exception:
                pass
            from pypdf import PdfReader
            try:
                reader = PdfReader(file_path)
                text = "".join(p.extract_text() for p in reader.pages).lower()
                return "earnings" in text and "employer" in text
            except Exception:
                return False
        except Exception:
            return False

    def parse(self, file_path: str) -> List[dict]:
        return self._parse_paystub(file_path)

    def _extract_text(self, file_path: str) -> str:
        try:
            result = subprocess.run(
                ["pdftotext", "-layout", file_path, "-"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                return result.stdout
        except Exception:
            pass
        from pypdf import PdfReader
        reader = PdfReader(file_path)
        return "\n".join(page.extract_text() for page in reader.pages)

    def _re_dollar(self, text: str, pat: str) -> Optional[int]:
        m = re.search(pat, text)
        if m:
            return int(round(float(m.group(1).replace(",", "")) * 100))
        return None

    def _parse_date(self, date_str: str) -> str:
        parts = date_str.split("/")
        return f"{parts[2].strip()}-{parts[0].strip().zfill(2)}-{parts[1].strip().zfill(2)}"

    # The statement names the employer itself, under a header like
    #   WORKSITE EMPLOYER:
    #   <COMPANY NAME>
    # so there is no reason to hardcode one — and a hardcoded name is both this
    # user's private information and a guarantee the parser works for nobody else.
    # Blank lines between the label and the value are normal in extracted PDF text.
    # Ordered by preference. A PEO-processed statement names TWO companies: the
    # WORKSITE employer (who you actually work for, and what belongs on the
    # transaction) and the PEO that runs payroll. Taking whichever appears first
    # would label every paycheque with the payroll processor.
    _EMPLOYER_LABELS = (
        "WORKSITE EMPLOYER",
        "EMPLOYER OF RECORD",
        "PEO EMPLOYER",
        "COMPANY",
        "EMPLOYER",
    )

    def _parse_employer(self, text: str) -> str:
        """Employer name from the statement, or a neutral fallback.

        Line-based rather than one regex: ``pdftotext -layout`` keeps the columns,
        so the value sits on a later line and may share that line with an unrelated
        column. Take the next line that carries actual content, then cut it at the
        first wide gap, which is the column separator.

        ``PAYSTUB_EMPLOYER`` overrides for a layout this cannot read. The final
        fallback is deliberately generic: labelling a paycheque "Employer" is
        honest, whereas guessing a company name would attribute income to an entity
        that may not exist.
        """
        lines = text.split("\n")
        for label in self._EMPLOYER_LABELS:
            for index, line in enumerate(lines):
                if label not in line.upper():
                    continue
                for candidate in lines[index + 1:index + 6]:
                    value = re.split(r"\s{3,}", candidate.strip())[0].strip()
                    if not value:
                        continue
                    # An address block can follow the label before the name does.
                    if value.lower().startswith(("(legal", "(address")):
                        continue
                    return value
        return os.environ.get("PAYSTUB_EMPLOYER", "Employer")

    def _parse_paystub(self, file_path: str) -> List[dict]:
        text = self._extract_text(file_path)
        fname = os.path.basename(file_path)

        employer = self._parse_employer(text)

        employee_name = ""
        m = re.search(r"PAID TO:\s*\n\s*([^\n]+)", text)
        if m:
            employee_name = m.group(1).strip()

        period_start = period_end = None
        pm = re.search(r'PAY PERIOD:.*?(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})', text, re.DOTALL)
        if pm:
            period_start = self._parse_date(pm.group(1))
            period_end = self._parse_date(pm.group(2))

        pay_date = None
        pdm = re.search(r'PAY DATE:.*?(\d{2}/\d{2}/\d{4})', text, re.DOTALL)
        if pdm:
            pay_date = self._parse_date(pdm.group(1))

        gross = self._re_dollar(text, r'GROSS PAY:[\s\S]*?\$?([\d,]+\.\d{2})')
        net = self._re_dollar(text, r'NET PAY:[\s\S]*?\$?([\d,]+\.\d{2})')

        federal = self._re_dollar(text, r'Federal Income Tax[\s\S]*?\$?([\d,]+\.\d{2})') or 0
        medicare = self._re_dollar(text, r'Medicare[\s\S]*?\$?([\d,]+\.\d{2})') or 0
        social_security = self._re_dollar(text, r'Social Security[\s\S]*?\$?([\d,]+\.\d{2})') or 0
        state = self._re_dollar(text, r'(?:State Withholding|North Carolina)[\s\S]*?\$?([\d,]+\.\d{2})') or 0

        insurance_sum = 0
        for pat in [r'Hospital Insurance[\s\S]*?\$?([\d,]+\.\d{2})',
                     r'Dental Deductions[\s\S]*?\$?([\d,]+\.\d{2})',
                     r'Medical Deductions[\s\S]*?\$?([\d,]+\.\d{2})',
                     r'Life Deductions[\s\S]*?\$?([\d,]+\.\d{2})',
                     r'Short Term[\s\S]*?\$?([\d,]+\.\d{2})']:
            v = self._re_dollar(text, pat) or 0
            insurance_sum += v

        hsa = self._re_dollar(text, r'HSA[\s\S]*?\$?([\d,]+\.\d{2})') or 0

        retirement = 0
        r401k = self._re_dollar(text, r'401K[\s\S]*?\$?([\d,]+\.\d{2})') or 0
        roth = self._re_dollar(text, r'Roth 401K[\s\S]*?\$?([\d,]+\.\d{2})') or 0
        retirement = r401k + roth

        total_deductions = federal + medicare + social_security + state + retirement + insurance_sum + hsa
        computed_net = (gross or 0) - total_deductions
        other = 0
        if net and computed_net != net:
            other = net - computed_net

        paystub_row = {
            "source_platform": self.platform,
            "source_file": fname,
            "account_id": self.account_id,
            "employer": employer,
            "employee_name": employee_name,
            "period_start": period_start,
            "period_end": period_end,
            "pay_date": pay_date,
            "gross_cents": gross or 0,
            "net_cents": net or 0,
            "federal_tax_cents": federal,
            "medicare_cents": medicare,
            "social_security_cents": social_security,
            "state_tax_cents": state,
            "retirement_cents": retirement,
            "insurance_cents": insurance_sum,
            "hsa_cents": hsa,
            "other_deductions_cents": other,
        }

        components = self._build_components(paystub_row)
        components.insert(0, paystub_row)
        return components

    def _txn(self, posted_date: str, amount_cents: int, merchant: str,
             description: str, entity_type: str, category_path: str,
             asset_class: str = None, source_file: str = "",
             reason: str = "") -> dict:
        row = self.make_row(
            source_file=source_file,
            posted_date=posted_date,
            amount_cents=amount_cents,
            merchant=merchant,
            description=description,
        )
        row["entity_type"] = entity_type
        row["category_path"] = category_path
        if asset_class:
            row["asset_class"] = asset_class
        row["confidence_score"] = 1.0
        row["categorization_reason"] = (
            reason
            or f"Paystub component derived from {os.path.basename(source_file)} — deterministic mapping, no LLM call."
        )
        return row

    def _build_components(self, p: dict) -> List[dict]:
        rows = []
        pd = p["pay_date"] or p["period_end"]
        sf = p["source_file"]

        if p["gross_cents"] > 0:
            rows.append(self._txn(pd, p["gross_cents"], p["employer"],
                f"Payroll: {p['period_start']} to {p['period_end']}",
                "income", "Salary", source_file=sf,
                reason=f"Paystub gross pay from {p["employer"]} for {p['period_start']} to {p['period_end']}."))

        for name, cents in [("Federal Income Tax", p["federal_tax_cents"]),
                            ("Medicare Tax", p["medicare_cents"]),
                            ("Social Security Tax", p["social_security_cents"]),
                            ("State Income Tax", p["state_tax_cents"])]:
            if cents and cents > 0:
                rows.append(self._txn(pd, -cents, p["employer"],
                    f"{name}: {p['period_start']} to {p['period_end']}",
                    "expense", "Expense > Fees & Interest", source_file=sf,
                    reason=f"Paystub {name} deduction for {p['period_start']} to {p['period_end']}."))

        if p["retirement_cents"] > 0:
            rows.append(self._txn(pd, -p["retirement_cents"], p["employer"],
                f"401k/Roth 401k: {p['period_start']} to {p['period_end']}",
                "transfer", "Transfer",
                asset_class="investment", source_file=sf,
                reason=f"Paystub 401k/Roth 401k contribution for {p['period_start']} to {p['period_end']} — internal transfer to retirement account."))

        if p["insurance_cents"] > 0:
            rows.append(self._txn(pd, -p["insurance_cents"], p["employer"],
                f"Insurance: {p['period_start']} to {p['period_end']}",
                "expense", "Health", source_file=sf,
                reason=f"Paystub insurance premiums (hospital/dental/medical/life/short-term) for {p['period_start']} to {p['period_end']}."))

        if p["hsa_cents"] > 0:
            rows.append(self._txn(pd, -p["hsa_cents"], p["employer"],
                f"HSA: {p['period_start']} to {p['period_end']}",
                "transfer", "Transfer",
                asset_class="investment", source_file=sf,
                reason=f"Paystub HSA contribution for {p['period_start']} to {p['period_end']} — internal transfer."))

        if p["other_deductions_cents"] > 0:
            rows.append(self._txn(pd, -p["other_deductions_cents"], p["employer"],
                f"Other deductions: {p['period_start']} to {p['period_end']}",
                "expense", "Expense > Other", source_file=sf,
                reason=f"Paystub other deductions reconciling gross - known deductions = net for {p['period_start']} to {p['period_end']}."))

        return rows
