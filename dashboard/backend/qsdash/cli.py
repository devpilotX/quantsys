"""Operational CLI.

    python -m qsdash.cli init-db
    python -m qsdash.cli create-operator --username NAME [--password ...]
    python -m qsdash.cli reset-password --username NAME
    python -m qsdash.cli seed-defaults
    python -m qsdash.cli console-role
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from qsdash.config import settings
from qsdash.db import SessionLocal, engine
from qsdash.models import RuntimeConfig, User
from qsdash.security import hash_password, new_totp_secret, totp_uri

BACKEND_DIR = Path(__file__).resolve().parents[1]


def migrations_dir() -> Path:
    """The directory holding alembic.ini and alembic/. A source checkout has
    them next to the package. An installed package does not ship them, and
    BACKEND_DIR is then site-packages, where alembic/ is the Alembic library
    itself, so the Docker image names the copy it keeps in
    QSDASH_MIGRATIONS_DIR."""
    for d in (os.environ.get("QSDASH_MIGRATIONS_DIR"), BACKEND_DIR):
        if d and (Path(d) / "alembic.ini").is_file() and (Path(d) / "alembic" / "env.py").is_file():
            return Path(d)
    raise SystemExit("migrations not found: set QSDASH_MIGRATIONS_DIR to the directory "
                     "holding alembic.ini (dashboard/backend in a checkout)")


def init_db() -> None:
    from alembic import command
    from alembic.config import Config

    where = migrations_dir()
    cfg = Config(str(where / "alembic.ini"))
    cfg.set_main_option("script_location", str(where / "alembic"))
    command.upgrade(cfg, "head")
    print("migrations applied")
    seed_defaults()
    if settings.console_database_url:
        console_role()


def console_role() -> None:
    """Create or update the SQL console's read-only role from
    CONSOLE_DATABASE_URL, and grant it every table the console may show.

    Idempotent, and run by init-db whenever the URL is set, so tables added
    by a later migration are granted on the next deploy. The credential
    tables are never granted.
    """
    from psycopg import sql
    from sqlalchemy.engine import make_url

    from qsdash.api.dbadmin import _PRIVATE_TABLES, ALLOWED_TABLES, STATEMENT_TIMEOUT

    if not settings.console_database_url:
        print("CONSOLE_DATABASE_URL is not set; nothing to do", file=sys.stderr)
        sys.exit(1)
    url = make_url(settings.console_database_url)
    if url.get_backend_name() != "postgresql" or not url.username or not url.password:
        print("CONSOLE_DATABASE_URL must be a postgresql URL with a user and password",
              file=sys.stderr)
        sys.exit(1)
    main_url = make_url(settings.database_url)
    if url.username == main_url.username:
        print("CONSOLE_DATABASE_URL must use its own role, not the application's",
              file=sys.stderr)
        sys.exit(1)

    role, db_name = sql.Identifier(url.username), sql.Identifier(main_url.database or "")
    stmts = [
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(db_name, role),
        sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role),
        *(sql.SQL("GRANT SELECT ON {} TO {}").format(sql.Identifier(t), role)
          for t in ALLOWED_TABLES),
        *(sql.SQL("REVOKE ALL ON {} FROM {}").format(sql.Identifier(t), role)
          for t in sorted(_PRIVATE_TABLES)),
        sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role),
        sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(
            role, sql.Literal(STATEMENT_TIMEOUT)),
    ]
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (url.username,))
        verb = "ALTER" if cur.fetchone() else "CREATE"
        cur.execute(sql.SQL(
            "{} ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOREPLICATION").format(sql.SQL(verb), role, sql.Literal(url.password)))
        for stmt in stmts:
            cur.execute(stmt)
        raw.commit()
    finally:
        raw.close()
    print(f"console role {url.username} ready: SELECT on {len(ALLOWED_TABLES)} tables")


def seed_defaults() -> None:
    defaults = {
        "mode": "paper",
        "paper_capital": 1_000_000.0,
    }
    db = SessionLocal()
    try:
        for key, value in defaults.items():
            if db.query(RuntimeConfig).filter(RuntimeConfig.key == key).first() is None:
                db.add(RuntimeConfig(key=key, value={"v": value}, updated_by="seed"))
        db.commit()
        print(f"runtime_config seeded (defaults: {defaults})")
    finally:
        db.close()


def create_operator(username: str, password: str | None) -> None:
    if not password:
        password = getpass.getpass("operator password: ")
        if getpass.getpass("repeat: ") != password:
            print("passwords do not match", file=sys.stderr)
            sys.exit(1)
    if len(password) < 12:
        print("password must be >= 12 characters", file=sys.stderr)
        sys.exit(1)
    secret = new_totp_secret()
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == username).first()
        if existing is not None:
            print(f"user {username} already exists — use reset-password", file=sys.stderr)
            sys.exit(1)
        db.add(User(username=username, password_hash=hash_password(password),
                    totp_secret=secret))
        db.commit()
    finally:
        db.close()
    print(f"operator '{username}' created.")
    print(f"TOTP secret: {secret}")
    print("otpauth URI (scan in Google Authenticator / Aegis):")
    print(f"  {totp_uri(secret, username)}")


def reset_password(username: str) -> None:
    password = getpass.getpass("new password: ")
    if len(password) < 12:
        print("password must be >= 12 characters", file=sys.stderr)
        sys.exit(1)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            print("no such user", file=sys.stderr)
            sys.exit(1)
        user.password_hash = hash_password(password)
        user.failed_attempts = 0
        user.locked_until = None
        db.commit()
        print("password updated")
    finally:
        db.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="qsdash")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db")
    sub.add_parser("seed-defaults")
    sub.add_parser("console-role")
    p_co = sub.add_parser("create-operator")
    p_co.add_argument("--username", required=True)
    p_co.add_argument("--password", default=None)
    p_rp = sub.add_parser("reset-password")
    p_rp.add_argument("--username", required=True)
    args = ap.parse_args()

    if args.cmd == "init-db":
        init_db()
    elif args.cmd == "seed-defaults":
        seed_defaults()
    elif args.cmd == "console-role":
        console_role()
    elif args.cmd == "create-operator":
        create_operator(args.username, args.password)
    elif args.cmd == "reset-password":
        reset_password(args.username)


if __name__ == "__main__":
    main()
