"""DuckDB connection + idempotent upsert helpers (the warehouse layer)."""
from __future__ import annotations

import re

import duckdb
import pandas as pd

from .config import get_settings, load_universe, ticker_dimension

# DuckDB cannot bind table/column names as parameters, so every identifier we
# interpolate is validated against this instead. Today all call sites pass
# literals, but `upsert()` is the one function in the codebase that concatenates
# SQL, so it validates rather than assuming its callers stay well-behaved.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def _ident(name: str, *, kind: str = "identifier") -> str:
    """Return ``name`` if it is a plain SQL identifier, else raise ``ValueError``."""
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"unsafe {kind}: {name!r}")
    return name


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    s = get_settings()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(s.db_path), read_only=read_only)


def table_exists(con, table: str) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()[0] > 0


def upsert(con, table: str, df: pd.DataFrame, keys: list[str]) -> int:
    """Insert `df` into `table`, replacing rows that match on `keys`."""
    if df is None or len(df) == 0:
        return 0
    table = _ident(table, kind="table name")
    safe_keys = [_ident(k, kind="key column") for k in keys]
    for c in df.columns:
        _ident(str(c), kind="column name")

    con.register("_incoming", df)
    cols = ", ".join(f'"{c}"' for c in df.columns)
    # The S608 suppressions below are safe: every interpolated identifier passed
    # `_ident()` above, and DuckDB offers no parameter binding for identifiers.
    con.execute(f"CREATE TABLE IF NOT EXISTS {table} AS SELECT * FROM _incoming WHERE 1=0")  # noqa: S608
    cond = " AND ".join(f'{table}."{k}" = _incoming."{k}"' for k in safe_keys)
    con.execute(f"DELETE FROM {table} WHERE EXISTS (SELECT 1 FROM _incoming WHERE {cond})")  # noqa: S608
    con.execute(f"INSERT INTO {table} ({cols}) SELECT {cols} FROM _incoming")  # noqa: S608
    con.unregister("_incoming")
    return len(df)


def build_dim_ticker(con) -> None:
    """(Re)build the ticker dimension from the universe config."""
    df = pd.DataFrame(ticker_dimension(load_universe()))
    con.register("_dim", df)
    con.execute("CREATE OR REPLACE TABLE dim_ticker AS SELECT * FROM _dim")
    con.unregister("_dim")
