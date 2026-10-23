from datetime import date

import pandas as pd
import pytest

import engine


@pytest.fixture
def tables():
    return {"sales": pd.DataFrame({
        "order_date": pd.to_datetime(["2026-01-05", "2026-01-20", "2026-02-03", "2026-03-15"]),
        "region": pd.array(["Lahore", "Karachi", "Lahore", "Multan"], dtype="string"),
        "amount": [100, 250, 300, 50],
    })}


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select sum(amount) as value from sales;",
    "WITH t AS (SELECT * FROM sales) SELECT count(*) AS value FROM t",
    "SELECT 'drop table x; delete' AS note",            # keywords inside strings are fine
    "-- comment\nSELECT region FROM sales",
    "SELECT replace(region, 'a', 'b') AS r FROM sales",
])
def test_select_allowed(sql):
    engine.validate_select(sql)


@pytest.mark.parametrize("sql", [
    "DROP TABLE sales",
    "DELETE FROM sales",
    "SELECT 1; DROP TABLE sales",
    "INSERT INTO sales VALUES (1)",
    "CREATE TABLE x AS SELECT 1",
    "SELECT * FROM read_csv('secret.csv')",
    "COPY sales TO 'out.csv'",
    "ATTACH 'other.db'",
    "PRAGMA database_list",
    "SET enable_external_access = true",
    "",
])
def test_non_select_rejected(sql):
    with pytest.raises(engine.QueryError):
        engine.validate_select(sql)


def test_run_query(tables):
    df = engine.run_on_tables(tables, "SELECT SUM(amount) AS value FROM sales")
    assert df["value"].iloc[0] == 700


def test_bad_column_gives_clean_error(tables):
    with pytest.raises(engine.QueryError) as e:
        engine.run_on_tables(tables, "SELECT nope FROM sales")
    assert "nope" in str(e.value)


def test_file_access_blocked_even_if_check_bypassed(tables):
    con = engine.connect(tables)
    with pytest.raises(Exception):
        con.sql("SELECT * FROM read_csv('requirements.txt')").df()


def test_runaway_query_is_stopped(tables):
    import time
    t0 = time.perf_counter()
    with pytest.raises(engine.QueryError, match="took longer"):
        con = engine.connect(tables)
        engine.run_query(con, "SELECT COUNT(*) AS n FROM range(100000000) a, range(100000000) b", timeout=1)
    assert time.perf_counter() - t0 < 10


def test_semicolon_and_dashes_inside_strings_are_fine(tables):
    df = engine.run_on_tables(tables, "SELECT 'a''; b -- c' AS s")
    assert df["s"].iloc[0] == "a'; b -- c"


def test_filters(tables):
    f = engine.Filters(date_column="order_date", date_range=(date(2026, 1, 1), date(2026, 1, 31)))
    out = engine.apply_filters(tables, f)
    assert out["sales"]["amount"].sum() == 350

    f = engine.Filters(categories={"region": ["Lahore"]})
    assert engine.apply_filters(tables, f)["sales"]["amount"].sum() == 400

    # end date is inclusive
    f = engine.Filters(date_column="order_date", date_range=(date(2026, 3, 15), date(2026, 3, 15)))
    assert len(engine.apply_filters(tables, f)["sales"]) == 1


def test_filter_skips_tables_without_column(tables):
    tables["other"] = pd.DataFrame({"x": [1, 2]})
    out = engine.apply_filters(tables, engine.Filters(categories={"region": ["Lahore"]}))
    assert len(out["other"]) == 2
