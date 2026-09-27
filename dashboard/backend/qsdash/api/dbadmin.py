"""Read-only database browser under the same auth.

Full pgweb runs as a separate container behind nginx (/db/) on the VPS; this
endpoint set is the always-available, zero-extra-process fallback and the one
the SPA's Database page uses.

What makes the SQL console read-only, strongest first:

1. A dedicated read-only role, when ``console_database_url`` is set: console
   queries then run on a small engine of their own (pool_size=2,
   max_overflow=0) as that role, so the app's pool is never starved and the
   database itself refuses writes and credential reads. This is the real
   control; everything below is defence in depth. Create the role once:

       CREATE ROLE qs_console LOGIN PASSWORD '<generate>'
           NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
       GRANT CONNECT ON DATABASE quantsys TO qs_console;
       GRANT USAGE ON SCHEMA public TO qs_console;
       GRANT SELECT ON ALL TABLES IN SCHEMA public TO qs_console;
       REVOKE SELECT ON users, sessions FROM qs_console;
       ALTER ROLE qs_console SET default_transaction_read_only = on;
       ALTER ROLE qs_console SET statement_timeout = '5s';

   then CONSOLE_DATABASE_URL=postgresql+psycopg://qs_console:<pw>@postgres:5432/quantsys.
   Tables created by later migrations are not granted automatically, which is
   the safe default: grant them one by one. In prod without it, every console
   response carries a ``warning`` that the console runs on the application role.
2. Every query runs in a transaction set read-only for that statement:
   Postgres ``SET TRANSACTION READ ONLY`` with ``SET LOCAL statement_timeout``;
   SQLite ``PRAGMA query_only = ON``, restored when the query ends because
   the pragma outlives the transaction on a pooled connection.
3. Text checks: a single SELECT / WITH / EXPLAIN, no write keywords, no
   reference to the users or sessions tables (password hashes, TOTP seeds,
   CSRF tokens), and none of the functions that would run SQL built at run
   time, change the statement timeout, or read files on the database host.

The users and sessions tables are never listed or browsable either.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from qsdash.config import settings
from qsdash.db import Base, engine
from qsdash.deps import current_session, get_db, require_csrf

router = APIRouter(prefix="/db", tags=["dbadmin"], dependencies=[Depends(current_session)])

# credential tables: password hashes, TOTP seeds, session and CSRF tokens
_PRIVATE_TABLES = frozenset({"users", "sessions"})
ALLOWED_TABLES = sorted(t for t in Base.metadata.tables if t not in _PRIVATE_TABLES)

STATEMENT_TIMEOUT = "5s"

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|grant|revoke|truncate|vacuum|copy|do|call|set|listen|notify)\b",
    re.IGNORECASE,
)
_PRIVATE = re.compile(r"\b(users|sessions)\b", re.IGNORECASE)
# SQL assembled at run time would slip past _PRIVATE (dblink, the *_to_xml
# family, and ts_stat and ts_rewrite, which run a query given as a string);
# set_config would lift the statement timeout; the file functions read the
# database host; U&"..." spells an identifier the checks above cannot see.
_UNSAFE = re.compile(
    r"\b(?:set_config|dblink\w*|(?:query|table|cursor|schema|database)_to_xml\w*"
    r"|ts_stat|ts_rewrite"
    r"|pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file"
    r"|lo_import|lo_export|lo_get)\b"
    r"|\bu&['\"]",
    re.IGNORECASE,
)

# With every console response in prod while no read-only role is configured:
# the text checks cannot be made complete, and the app role can read the
# credential tables.
APP_ROLE_WARNING = (
    "the SQL console runs on the application role: CONSOLE_DATABASE_URL is not set, "
    "so only text checks stand between a query and the credential tables. Set it "
    "to the read-only role described in qsdash/api/dbadmin.py.")


@router.get("/tables")
def tables():
    insp = inspect(engine)
    out = []
    for t in ALLOWED_TABLES:
        try:
            cols = [c["name"] for c in insp.get_columns(t)]
        except Exception:
            cols = []
        out.append({"table": t, "columns": cols})
    return out


@router.get("/tables/{table}")
def table_rows(table: str, limit: int = 100, offset: int = 0,
               order: str = "desc", db: Session = Depends(get_db)):
    if table not in ALLOWED_TABLES:
        raise HTTPException(404, "unknown table")
    limit = min(limit, 500)
    tbl = Base.metadata.tables[table]
    pk = list(tbl.primary_key.columns)
    order_clause = ""
    if pk:
        direction = "DESC" if order == "desc" else "ASC"
        order_clause = f' ORDER BY "{pk[0].name}" {direction}'
    rows = db.execute(
        text(f'SELECT * FROM "{table}"{order_clause} LIMIT :lim OFFSET :off'),
        {"lim": limit, "off": offset},
    )
    cols = list(rows.keys())
    total = db.execute(text(f'SELECT count(*) FROM "{table}"')).scalar()
    return {
        "table": table, "columns": cols, "total": total,
        "rows": [[_jsonable(v) for v in r] for r in rows.fetchall()],
    }


class QueryBody(BaseModel):
    sql: str
    limit: int = 200


@lru_cache(maxsize=4)
def _engine_for(url: str) -> Engine:
    return create_engine(url, pool_size=2, max_overflow=0, pool_pre_ping=True)


def _console_engine() -> Engine:
    """The console's own engine when a read-only role is configured."""
    url = settings.console_database_url
    return _engine_for(url) if url else engine


@contextmanager
def _read_only(conn: Connection) -> Iterator[None]:
    """Run the enclosed statements read-only and time-boxed, then roll back.
    On SQLite the pragma is per connection and outlives the transaction, so
    it is restored before the connection can return to the pool."""
    dialect = conn.dialect.name
    if dialect == "postgresql":
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        conn.exec_driver_sql(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        try:
            yield
        finally:
            conn.rollback()
    elif dialect == "sqlite":
        was_on = bool(conn.exec_driver_sql("PRAGMA query_only").scalar())
        conn.exec_driver_sql("PRAGMA query_only = ON")
        try:
            yield
        finally:
            conn.rollback()
            conn.exec_driver_sql(f"PRAGMA query_only = {'ON' if was_on else 'OFF'}")
            conn.rollback()
    else:
        raise HTTPException(501, f"SQL console does not support {dialect}")


def _checked(raw: str) -> str:
    sql = raw.strip().rstrip(";")
    if ";" in sql:
        raise HTTPException(400, "single statement only")
    if not re.match(r"^\s*(select|with|explain)\b", sql, re.IGNORECASE):
        raise HTTPException(400, "SELECT/EXPLAIN only")
    if _FORBIDDEN.search(sql):
        raise HTTPException(400, "read-only: statement contains a write keyword")
    if _PRIVATE.search(sql):
        raise HTTPException(400, "the users and sessions tables are not available here")
    if _UNSAFE.search(sql):
        raise HTTPException(400, "statement uses a function or escape the console refuses")
    return sql


@router.post("/query")
def run_query(body: QueryBody, _sess=Depends(require_csrf)):
    """In prod without a console role, every response carries ``warning``,
    refusals and query errors included."""
    warning = (APP_ROLE_WARNING if settings.env == "prod" and not settings.console_database_url
               else None)
    try:
        out = _query(body)
    except HTTPException as e:
        if warning is None:
            raise
        return JSONResponse({"detail": e.detail, "warning": warning},
                            status_code=e.status_code)
    if warning is not None:
        out["warning"] = warning
    return out


def _query(body: QueryBody) -> dict:
    sql = _checked(body.sql)
    explain = sql.lower().startswith("explain")
    stmt = text(sql) if explain else text(f"SELECT * FROM ({sql}) _q LIMIT :lim")
    with _console_engine().connect() as conn:
        try:
            with _read_only(conn):
                rows = conn.execute(stmt, {} if explain else {"lim": min(body.limit, 1000)})
                cols = list(rows.keys())
                data = [[_jsonable(v) for v in r] for r in rows.fetchall()]
        except SQLAlchemyError as e:
            raise HTTPException(400, f"query error: {e}") from e
    return {"columns": cols, "rows": data}


def _jsonable(v):
    from datetime import date, datetime

    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, (dict, list, str, int, float, bool)) or v is None:
        return v
    return str(v)
