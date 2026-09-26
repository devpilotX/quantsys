"""Operational CLI.

    python -m qsdash.cli init-db
    python -m qsdash.cli create-operator --username dipanshu [--password ...]
    python -m qsdash.cli seed-defaults
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from qsdash.db import SessionLocal
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
    elif args.cmd == "create-operator":
        create_operator(args.username, args.password)
    elif args.cmd == "reset-password":
        reset_password(args.username)


if __name__ == "__main__":
    main()
