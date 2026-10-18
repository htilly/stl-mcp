"""Postgres access: a lazily opened connection pool, transactions and a small migration runner.

Migrations are plain SQL files in migrations/ named NNN_description.sql. They are applied in
name order, each in its own transaction, and recorded in schema_migrations. An advisory lock keeps
two processes from migrating at the same time.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

MIGRATIONS = Path(__file__).parent / "migrations"
_LOCK_ID = 0x5714C0DE  # arbitrary, constant advisory lock id for migrations


class Database:
    def __init__(self, url: str, schema: str | None = None, max_size: int = 10):
        if not url:
            raise RuntimeError("DATABASE_URL is not set (e.g. postgresql://stl:stl@localhost/stl)")
        kwargs: dict = {"row_factory": dict_row}
        if schema:
            kwargs["options"] = f"-c search_path={schema}"
        self.schema = schema
        self.pool = ConnectionPool(url, min_size=1, max_size=max_size, open=False, kwargs=kwargs)
        self._opened = False
        self._lock = threading.Lock()

    def open(self):
        with self._lock:
            if not self._opened:
                self.pool.open(wait=True, timeout=10)
                self._opened = True

    def close(self):
        with self._lock:
            if self._opened:
                self.pool.close()
                self._opened = False

    @contextmanager
    def tx(self):
        """A connection in a transaction: committed on success, rolled back on an exception."""
        self.open()
        with self.pool.connection() as conn:
            yield conn

    def all(self, sql: str, params=None) -> list[dict]:
        with self.tx() as c:
            return c.execute(sql, params).fetchall()

    def one(self, sql: str, params=None) -> dict | None:
        with self.tx() as c:
            return c.execute(sql, params).fetchone()

    def execute(self, sql: str, params=None) -> int:
        """Runs a statement and returns the number of affected rows."""
        with self.tx() as c:
            return c.execute(sql, params).rowcount

    def migrate(self) -> list[str]:
        """Applies pending migrations; returns the names that were applied."""
        applied: list[str] = []
        with self.tx() as c:
            c.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ID,))
            c.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                      "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
            done = {r["name"] for r in c.execute("SELECT name FROM schema_migrations")}
            for f in sorted(MIGRATIONS.glob("*.sql")):
                if f.name in done:
                    continue
                c.execute(f.read_text())
                c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
                applied.append(f.name)
        return applied
