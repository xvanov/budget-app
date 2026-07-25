import sys
import os
import tempfile
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

# Use a temp dir for test DB and context file
_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")
os.environ["BUDGET_CONTEXT_PATH"] = os.path.join(_tmp, "budget_context.md")
os.environ["SKIP_AUTO_IMPORT"] = "1"

from app.main import app

@pytest.fixture
def test_client():
    with TestClient(app) as client:
        yield client
