"""Compare the schema the migrations built with the one the models declare.

    DATABASE_URL=... python -m qsdash.cli init-db
    DATABASE_URL=... python scripts/check_migrations.py

The dashboard test suite builds its tables from the models, so a model change
that ships without a migration passes every test and then fails in production
at the first query that touches it. Missing or extra tables and columns, and
changed column types or nullability, exit 1. Other differences (indexes and
constraints, such as the time index TimescaleDB adds to a hypertable) are
printed but do not fail.
"""

from __future__ import annotations

import os
import sys

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine

FAILING = {"add_table", "remove_table", "add_column", "remove_column",
           "modify_type", "modify_nullable"}


def _kind(diff) -> str:
    # column modifications arrive as a list of tuples, everything else as one tuple
    return diff[0][0] if isinstance(diff, list) else diff[0]


def main() -> int:
    import qsdash.models  # noqa: F401  (registers every table)
    from qsdash.db import Base

    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        diffs = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    failing = [d for d in diffs if _kind(d) in FAILING]
    for d in diffs:
        print("FAIL" if d in failing else "info", d)
    if failing:
        print(f"{len(failing)} difference(s) between the migrations and the models")
        return 1
    print(f"migrations match the models ({engine.dialect.name}, "
          f"{len(diffs)} non-failing difference(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
