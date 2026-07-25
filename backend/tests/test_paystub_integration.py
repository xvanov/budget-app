import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers.import_routes import _date_add_days


class TestDateAdd:
    def test_add_days(self):
        assert _date_add_days("2026-01-01", 3) == "2026-01-04"

    def test_subtract_days(self):
        assert _date_add_days("2026-01-01", -3) == "2025-12-29"

    def test_month_boundary(self):
        assert _date_add_days("2026-01-31", 1) == "2026-02-01"

    def test_leap_year(self):
        assert _date_add_days("2024-02-28", 1) == "2024-02-29"
