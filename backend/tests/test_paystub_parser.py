import glob
import os
import subprocess
import sys
import json

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.parsers.paystub import PaystubPdfParser


SAMPLES_DIR = os.environ.get("SAMPLES_DIR", os.path.join(os.path.dirname(__file__), "..", "..", "samples"))
PAYSTUBS_DIR = os.path.join(SAMPLES_DIR, "paystubs", "2026")


def _find_paystubs():
    return sorted(glob.glob(os.path.join(PAYSTUBS_DIR, "*.pdf")))


class TestPaystubDetection:
    def test_detect_paystub_files(self):
        for path in _find_paystubs():
            assert PaystubPdfParser.detect(path), f"Should detect {path}"

    def test_detect_non_paystub_pdf(self):
        assert not PaystubPdfParser.detect(__file__), "Should not detect non-PDF"

    def test_detect_mortgage_pdf(self):
        mortgage_files = glob.glob(os.path.join(SAMPLES_DIR, "*.pdf"))
        for m in mortgage_files:
            if "amort" in m.lower() or "mortgage" in m.lower():
                assert not PaystubPdfParser.detect(m), f"Should not detect mortgage file {m}"

    def test_detect_by_content(self):
        path = _find_paystubs()[0]
        # Should still detect even if renamed without prefix
        import shutil, tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tmp.close()
        shutil.copy2(path, tmp.name)
        try:
            assert PaystubPdfParser.detect(tmp.name), "Should detect by content"
        finally:
            os.unlink(tmp.name)


class TestPaystubParsing:
    def test_all_paystubs_parse(self):
        for path in _find_paystubs():
            parser = PaystubPdfParser()
            rows = parser.parse(path)
            assert len(rows) >= 7, f"{os.path.basename(path)} should have >=7 rows"
            paystub = rows[0]
            assert "employer" in paystub
            # The employer is READ from the statement, not hardcoded, so assert the
            # invariant rather than one user's company: a real, non-placeholder name.
            assert paystub["employer"], "employer must be parsed from the statement"
            assert paystub["employer"] != "Employer", (
                f"{os.path.basename(path)}: fell back to the placeholder employer")
            assert paystub["gross_cents"] > 0
            assert paystub["net_cents"] > 0
            assert paystub["period_start"] is not None
            assert paystub["period_end"] is not None

    def test_employer_is_the_worksite_employer_not_the_payroll_processor(self):
        """A PEO statement names two companies; the worksite one is the employer.

        Taking whichever label appeared first picked up the payroll processor and
        would have labelled every paycheque with it.
        """
        parser = PaystubPdfParser()
        text = parser._extract_text(_find_paystubs()[0])
        rows = parser.parse(_find_paystubs()[0])
        employer = rows[0]["employer"]
        assert employer and employer != "Employer"
        assert employer == employer.upper(), f"expected an uppercase name, got {employer!r}"
        if "PEO EMPLOYER" in text.upper():
            lines = text.split("\n")
            peo_index = next(i for i, l in enumerate(lines) if "PEO EMPLOYER" in l.upper())
            peo = next((l.strip() for l in lines[peo_index + 1:peo_index + 4] if l.strip()), "")
            assert employer not in peo or not peo, (
                f"employer {employer!r} looks like the PEO line {peo!r}")

    def test_identity_invariant_gross_minus_deductions_equals_net(self):
        for path in _find_paystubs():
            parser = PaystubPdfParser()
            rows = parser.parse(path)
            p = rows[0]
            deductions = (p["federal_tax_cents"] + p["medicare_cents"] +
                          p["social_security_cents"] + p["state_tax_cents"] +
                          p["retirement_cents"] + p["insurance_cents"] +
                          p["hsa_cents"] + p["other_deductions_cents"])
            assert p["gross_cents"] - deductions == p["net_cents"], (
                f"{os.path.basename(path)}: gross({p['gross_cents']}) - ded({deductions}) != net({p['net_cents']})"
            )

    def test_component_rows_sum_to_net(self):
        for path in _find_paystubs():
            parser = PaystubPdfParser()
            rows = parser.parse(path)
            p = rows[0]
            txns = rows[1:]
            gross_row = [t for t in txns if t["amount_cents"] > 0]
            deductions = [t for t in txns if t["amount_cents"] < 0]
            total_gross = sum(t["amount_cents"] for t in gross_row)
            total_ded = abs(sum(t["amount_cents"] for t in deductions))
            assert total_gross - total_ded == p["net_cents"], (
                f"{os.path.basename(path)}: sum of components != net"
            )

    def test_component_categories_correct(self):
        parser = PaystubPdfParser()
        rows = parser.parse(_find_paystubs()[0])
        txns = rows[1:]

        income_rows = [t for t in txns if t["amount_cents"] > 0]
        assert len(income_rows) == 1
        assert income_rows[0]["category_path"] == "Salary"
        assert income_rows[0]["entity_type"] == "income"

    def test_tax_rows_are_expense(self):
        parser = PaystubPdfParser()
        rows = parser.parse(_find_paystubs()[0])
        txns = rows[1:]
        tax_rows = [t for t in txns if t["entity_type"] == "expense" and "Tax" in t["description"]]
        for t in tax_rows:
            assert t["category_path"] == "Expense > Fees & Interest"

    def test_retirement_rows_are_transfer(self):
        parser = PaystubPdfParser()
        rows = parser.parse(_find_paystubs()[0])
        txns = rows[1:]
        ret_rows = [t for t in txns if t["entity_type"] == "transfer" and "401k" in t["description"]]
        for t in ret_rows:
            assert t["category_path"] == "Transfer"
            assert t["asset_class"] == "investment"

    def test_insurance_rows_are_healthcare_expense(self):
        parser = PaystubPdfParser()
        rows = parser.parse(_find_paystubs()[0])
        txns = rows[1:]
        ins_rows = [t for t in txns if "Insurance" in t["description"]]
        for t in ins_rows:
            assert t["category_path"] == "Health"
            assert t["entity_type"] == "expense"
            assert t["amount_cents"] < 0

    def test_hsa_rows_are_transfer(self):
        parser = PaystubPdfParser()
        rows = parser.parse(_find_paystubs()[0])
        txns = rows[1:]
        hsa_rows = [t for t in txns if "HSA" in t["description"]]
        for t in hsa_rows:
            assert t["category_path"] == "Transfer"
            assert t["asset_class"] == "investment"

    def test_later_paystubs_have_no_retirement(self):
        later_paystubs = _find_paystubs()[4:]
        for path in later_paystubs:
            parser = PaystubPdfParser()
            rows = parser.parse(path)
            assert rows[0]["retirement_cents"] == 0, f"{os.path.basename(path)} should have 0 retirement"

    def test_paystub_with_pto(self):
        last_path = _find_paystubs()[-1]
        parser = PaystubPdfParser()
        rows = parser.parse(last_path)
        p = rows[0]
        assert p["gross_cents"] == 769231
        assert p["net_cents"] == 524378
        # Verify PTO hours paystub still parses correctly
        assert p["period_start"] == "2026-04-26"
        assert p["period_end"] == "2026-05-09"

    def test_all_component_merchants_are_the_parsed_employer(self):
        """Every split row carries the same employer the statement named."""
        for path in _find_paystubs():
            parser = PaystubPdfParser()
            rows = parser.parse(path)
            employer = rows[0]["employer"]
            for t in rows[1:]:
                assert t["merchant"] == employer, (
                    f"{t['merchant']!r} does not match parsed employer {employer!r}")

    def test_all_amounts_are_nonzero(self):
        for path in _find_paystubs():
            parser = PaystubPdfParser()
            rows = parser.parse(path)
            for t in rows[1:]:
                assert t["amount_cents"] != 0, f"Zero amount in {os.path.basename(path)}"

    def test_detect_rejects_invalid(self):
        assert not PaystubPdfParser.detect("/nonexistent/file.pdf")
