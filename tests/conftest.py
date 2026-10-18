import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STL_DIR", tempfile.mkdtemp(prefix="stl-mcp-tests-"))
# A throwaway Postgres, e.g. `docker compose --profile test up -d postgres-test`.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL",
                                   "postgresql://postgres:test@localhost:55432/stl_test")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import psycopg
import pytest

import server
from auth import AuthState
from db import Database
from store import Store

TABLES = "users, oauth_clients, models"  # everything else hangs off these (ON DELETE CASCADE)


@pytest.fixture(scope="session")
def test_db():
    """A private schema for this test run, migrated once and dropped at the end."""
    schema = f"test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    d = Database(TEST_DATABASE_URL, schema=schema, max_size=4)
    d.migrate()
    yield d
    d.close()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as c:
        c.execute(f"DROP SCHEMA {schema} CASCADE")


@pytest.fixture(autouse=True)
def db(test_db, monkeypatch):
    """Every test starts with empty tables; the server uses the test schema."""
    test_db.execute(f"TRUNCATE {TABLES} CASCADE")
    monkeypatch.setattr(server, "db", test_db)
    monkeypatch.setattr(server.oauth, "state", AuthState(test_db))
    return test_db


@pytest.fixture
def out_dir(tmp_path, monkeypatch, db):
    """Point the server (standalone files AND the model library) at a fresh, empty directory."""
    monkeypatch.setattr(server, "DATA", tmp_path)
    monkeypatch.setattr(server, "OUT", tmp_path)
    monkeypatch.setattr(server, "store", Store(tmp_path, db, allow_script=False))
    return tmp_path
