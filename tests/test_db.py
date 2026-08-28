"""Warehouse layer guards.

`upsert()` is the only place in the codebase that concatenates SQL (DuckDB
cannot bind table or column names as parameters), so the identifier validation
it performs is part of the security surface and is tested directly.
"""
from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from forwardguidex import db


def test_ident_accepts_plain_identifiers():
    assert db._ident("raw_news") == "raw_news"
    assert db._ident("_x1") == "_x1"


@pytest.mark.parametrize("bad", [
    'raw_news"; DROP TABLE raw_news; --',
    "raw news",
    "raw-news",
    "1raw",
    "",
    "x" * 64,
    None,
])
def test_ident_rejects_anything_else(bad):
    with pytest.raises(ValueError):
        db._ident(bad)


def test_upsert_rejects_injected_table_name():
    con = duckdb.connect(":memory:")
    df = pd.DataFrame([{"k": 1, "v": "a"}])
    with pytest.raises(ValueError, match="unsafe table name"):
        db.upsert(con, 'raw_news" AS SELECT 1; --', df, keys=["k"])


def test_upsert_rejects_injected_key_column():
    con = duckdb.connect(":memory:")
    df = pd.DataFrame([{"k": 1, "v": "a"}])
    with pytest.raises(ValueError, match="unsafe key column"):
        db.upsert(con, "t", df, keys=['k" = 1 OR "1'])


def test_upsert_is_idempotent_on_keys():
    con = duckdb.connect(":memory:")
    first = pd.DataFrame([{"k": 1, "v": "a"}, {"k": 2, "v": "b"}])
    assert db.upsert(con, "t", first, keys=["k"]) == 2
    # same key, new value -> replaced, not duplicated
    assert db.upsert(con, "t", pd.DataFrame([{"k": 1, "v": "z"}]), keys=["k"]) == 1
    rows = con.execute("SELECT k, v FROM t ORDER BY k").fetchall()
    assert rows == [(1, "z"), (2, "b")]
