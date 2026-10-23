"""DuckDB query runner: SELECT-only check, filters, and safe execution on in-memory DataFrames."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

import duckdb
import pandas as pd

MAX_RESULT_ROWS = 5000
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|export|import|install|load|pragma|"
    r"set|reset|call|truncate|merge|grant|revoke|vacuum|checkpoint|use|begin|commit|rollback|"
    r"read_csv\w*|read_parquet|read_json\w*|read_text|read_blob|read_xlsx|parquet_scan|glob)\b",
    re.IGNORECASE,
)


class QueryError(Exception):
    """A query was rejected or failed. The message is safe to show and to send to the AI."""


# ---------- SELECT-only check ----------

def _strip_comments_and_strings(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def validate_select(sql: str) -> str:
    """Return the cleaned query, or raise QueryError if it is not a single read-only SELECT."""
    if not sql or not sql.strip():
        raise QueryError("Empty query.")
    cleaned = sql.strip().rstrip(";").strip()
    bare = _strip_comments_and_strings(cleaned)
    if ";" in bare:
        raise QueryError("Only one statement is allowed.")
    if not re.match(r"^\s*\(*\s*(select|with)\b", bare, re.IGNORECASE):
        raise QueryError("Only SELECT queries are allowed.")
    bad = FORBIDDEN.search(bare)
    if bad:
        raise QueryError(f"Keyword '{bad.group(0)}' is not allowed. Only SELECT queries are allowed.")
    # double-check with DuckDB's own parser
    try:
        statements = duckdb.extract_statements(cleaned)
    except duckdb.Error as e:
        raise QueryError(f"SQL syntax error: {e}") from e
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
        raise QueryError("Only a single SELECT query is allowed.")
    return cleaned


# ---------- filters ----------

@dataclass
class Filters:
    date_column: str | None = None
    date_range: tuple[date, date] | None = None
    categories: dict[str, list] = field(default_factory=dict)   # column -> selected values

    def key(self) -> tuple:
        return (self.date_column, self.date_range,
                tuple(sorted((k, tuple(map(str, v))) for k, v in self.categories.items())))


def apply_filters(tables: dict[str, pd.DataFrame], filters: Filters | None) -> dict[str, pd.DataFrame]:
    """Filter every table that has the filter column. Tables without it are left unchanged."""
    if filters is None:
        return tables
    out = {}
    for name, df in tables.items():
        mask = pd.Series(True, index=df.index)
        dc = filters.date_column
        if dc and filters.date_range and dc in df.columns and pd.api.types.is_datetime64_any_dtype(df[dc]):
            start, end = filters.date_range
            mask &= (df[dc] >= pd.Timestamp(start)) & (df[dc] < pd.Timestamp(end + timedelta(days=1)))
        for col, values in filters.categories.items():
            if values and col in df.columns:
                mask &= df[col].astype("string").isin([str(v) for v in values])
        out[name] = df if mask.all() else df.loc[mask].reset_index(drop=True)
    return out


# ---------- execution ----------

def connect(tables: dict[str, pd.DataFrame]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:", config={"enable_external_access": False})
    for name, df in tables.items():
        con.register(name, df)
    con.execute("SET lock_configuration = true")
    return con


def run_query(con: duckdb.DuckDBPyConnection, sql: str, max_rows: int = MAX_RESULT_ROWS) -> pd.DataFrame:
    sql = validate_select(sql)
    try:
        rel = con.sql(sql)
        df = rel.limit(max_rows).df() if rel is not None else pd.DataFrame()
    except duckdb.Error as e:
        msg = str(e).split("\n\nLINE")[0].strip()
        missing = re.search(r"Table with name (\S+) does not exist", msg)
        if missing:
            msg = f"Table '{missing.group(1)}' is not in the current data (it was not in the latest upload)."
        raise QueryError(msg) from e
    return df


def run_on_tables(tables: dict[str, pd.DataFrame], sql: str, max_rows: int = MAX_RESULT_ROWS) -> pd.DataFrame:
    con = connect(tables)
    try:
        return run_query(con, sql, max_rows)
    finally:
        con.close()
