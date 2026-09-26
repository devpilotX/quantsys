"""SQL console and table browser: read-only, credential tables out of reach,
and working on the SQLite this suite runs on."""

from __future__ import annotations

import pytest
from qsdash.config import settings
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError


def test_tables_never_lists_credential_tables(authed):
    r = authed.get("/api/db/tables")
    assert r.status_code == 200
    names = {t["table"] for t in r.json()}
    assert {"orders", "fills", "positions"} <= names
    assert names.isdisjoint({"users", "sessions"})


@pytest.mark.parametrize("table", ["users", "sessions"])
def test_table_browser_refuses_credential_tables(authed, table):
    assert authed.get(f"/api/db/tables/{table}").status_code == 404


def test_query_runs_on_sqlite(authed):
    """SET TRANSACTION READ ONLY ran outside the try and is not SQLite
    syntax, so every console query here returned 500."""
    r = authed.post("/api/db/query", json={"sql": "SELECT count(*) AS n FROM orders"})
    assert r.status_code == 200, r.text
    assert r.json()["columns"] == ["n"]


def test_query_requires_csrf(authed):
    authed.headers.pop("X-CSRF-Token")
    assert authed.post("/api/db/query", json={"sql": "SELECT 1"}).status_code == 403


@pytest.mark.parametrize("sql", [
    "SELECT password_hash, totp_secret FROM users",
    'SELECT csrf_token FROM "sessions"',
    "SELECT u.username FROM main.users u",
    "WITH s AS (SELECT * FROM Sessions) SELECT count(*) FROM s",
    "SELECT * FROM orders WHERE 1 = (SELECT count(*) FROM users)",
])
def test_query_refuses_credential_tables(authed, sql):
    r = authed.post("/api/db/query", json={"sql": sql})
    assert r.status_code == 400, r.text
    assert "users and sessions" in r.json()["detail"]


@pytest.mark.parametrize("sql", [
    "SELECT set_config('statement_timeout', '0', true)",
    "SELECT query_to_xml('select * from us' || 'ers', true, true, '')",
    'SELECT * FROM U&"\\0075sers"',
    "SELECT pg_read_file('/proc/self/environ')",
    "SELECT * FROM ts_stat('SELECT to_tsvector(totp_secret) FROM ' || 'us' || 'ers')",
    "SELECT ts_rewrite('a'::tsquery, 'SELECT to_tsquery(token), to_tsquery(csrf) FROM '"
    " || 'sess' || 'ions')",
    "SELECT * FROM pg_catalog.TS_STAT('SELECT 1')",
])
def test_query_refuses_calls_that_dodge_the_checks(authed, sql):
    """Runtime-built SQL hides a table name (ts_stat and ts_rewrite run a
    query given as a string), set_config lifts the timeout, and file reads
    reach the database host. On SQLite an unknown function would also be a
    400, so the reason is checked, not only the status."""
    r = authed.post("/api/db/query", json={"sql": sql})
    assert r.status_code == 400
    assert "function or escape the console refuses" in r.json()["detail"]


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
    "SELECT 1; DROP TABLE orders",
])
def test_query_still_refuses_writes(authed, sql):
    assert authed.post("/api/db/query", json={"sql": sql}).status_code == 400


def test_read_only_scope_blocks_writes_and_restores_the_connection():
    """PRAGMA query_only is per connection; a pooled connection left
    read-only would break every later write that happened to reuse it."""
    from qsdash.api.dbadmin import _read_only
    from qsdash.db import engine

    with engine.connect() as conn:
        with pytest.raises(OperationalError), _read_only(conn):
            conn.exec_driver_sql("CREATE TABLE _console_probe (x INTEGER)")
        assert conn.exec_driver_sql("PRAGMA query_only").scalar() == 0


def test_query_runs_on_the_console_engine_when_configured(authed, monkeypatch, tmp_path):
    from qsdash.api.dbadmin import _console_engine

    url = f"sqlite:///{(tmp_path / 'console.db').as_posix()}"
    seed = create_engine(url)
    with seed.begin() as c:
        c.exec_driver_sql("CREATE TABLE marker (v TEXT)")
        c.exec_driver_sql("INSERT INTO marker VALUES ('console-engine')")
    seed.dispose()
    monkeypatch.setattr(settings, "console_database_url", url)
    try:
        r = authed.post("/api/db/query", json={"sql": "SELECT v FROM marker"})
        assert r.status_code == 200, r.text
        assert r.json()["rows"] == [["console-engine"]]
        assert _console_engine().pool.size() == 2
    finally:
        _console_engine().dispose()


def test_prod_console_on_the_app_role_warns_on_every_query(authed, monkeypatch):
    """Without CONSOLE_DATABASE_URL the console runs as the app role, which
    can read the credential tables; the text checks are the only barrier."""
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "console_database_url", None)
    for sql in ("SELECT count(*) AS n FROM orders", "EXPLAIN SELECT 1"):
        r = authed.post("/api/db/query", json={"sql": sql})
        assert r.status_code == 200, r.text
        assert "application role" in r.json()["warning"]
    for sql, why in (("DELETE FROM orders", "SELECT/EXPLAIN only"),
                     ("SELECT * FROM no_such_table", "query error")):
        r = authed.post("/api/db/query", json={"sql": sql})
        assert r.status_code == 400
        assert why in r.json()["detail"] and "application role" in r.json()["warning"]


def test_dev_console_does_not_warn(authed, monkeypatch):
    monkeypatch.setattr(settings, "env", "dev")
    monkeypatch.setattr(settings, "console_database_url", None)
    r = authed.post("/api/db/query", json={"sql": "SELECT 1 AS one"})
    assert r.status_code == 200, r.text
    assert "warning" not in r.json()


def test_prod_console_on_its_own_role_does_not_warn(authed, monkeypatch, tmp_path):
    from qsdash.api.dbadmin import _console_engine

    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "console_database_url",
                        f"sqlite:///{(tmp_path / 'console.db').as_posix()}")
    try:
        r = authed.post("/api/db/query", json={"sql": "SELECT 1 AS one"})
        assert r.status_code == 200, r.text
        assert "warning" not in r.json()
    finally:
        _console_engine().dispose()
