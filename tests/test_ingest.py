from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import ingest
from samples import make_samples as ms


@pytest.fixture(scope="module")
def tables(tmp_path_factory):
    folder = tmp_path_factory.mktemp("samples")
    paths = ms.build_all(folder)
    result, warnings = ingest.ingest_files([(p.name, p.read_bytes()) for p in paths])
    return {t.name: t for t in result}


def test_table_names(tables):
    assert set(tables) == {"sales_report__sales_data", "sales_report__targets", "monthly_sales", "expenses"}


def test_sales_report_cleaning(tables):
    t = tables["sales_report__sales_data"]
    df = t.df
    expected = ms.sales_rows(1, date(2026, 1, 1), 90, 120)
    assert list(df.columns) == ["order_id", "order_date", "region", "product", "sales_person",
                                "qty", "unit_price", "amount_rs"]
    assert len(df) == 120                                   # title rows, blank and Total row gone
    assert t.report["title_rows_skipped"] == 2
    assert t.report["total_rows_removed"] == 1
    assert t.column_types["order_date"] == "date"
    assert t.column_types["amount_rs"] == "integer"         # "1,200" and "Rs 500" parsed
    assert df["amount_rs"].sum() == sum(r["amount"] for r in expected)
    assert df["qty"].sum() == sum(r["qty"] for r in expected)
    assert df["order_date"].min() >= pd.Timestamp("2026-01-01")


def test_targets_grand_total_removed(tables):
    df = tables["sales_report__targets"].df
    assert len(df) == 4
    assert df["monthly_target"].sum() == 3_000_000


def test_monthly_sheets_combined_with_excel_serial_dates(tables):
    t = tables["monthly_sales"]
    df = t.df
    assert len(df) == 3 * 28 * 2                            # Subtotal rows removed
    assert set(df["source_sheet"]) == {"Jan", "Feb", "Mar"}
    assert t.column_types["date"] == "date"
    assert df["date"].min() == pd.Timestamp("2026-01-01")
    assert df["date"].max() == pd.Timestamp("2026-03-28")
    assert (df["revenue"] == df["units"] * 250).all()


def test_csv_dayfirst_dates_and_placeholders(tables):
    t = tables["expenses"]
    df = t.df
    assert len(df) == 40
    assert t.column_types["date"] == "date"
    assert df["date"].iloc[1] == pd.Timestamp("2026-01-03")  # 03/01/2026 is 3 Jan, not 1 Mar
    assert t.column_types["amount"] == "integer"
    assert df["paid_by"].isna().sum() == 5                   # "-" -> empty


@pytest.mark.parametrize("text,expected", [
    ("1,200", 1200), ("Rs 500", 500), ("PKR 1,000.50", 1000.5), ("(300)", -300),
    ("15%", 0.15), ("-42", -42), ("$ 9", 9), ("abc", None), ("12-01-2026", None),
])
def test_parse_number(text, expected):
    assert ingest.parse_number(text) == expected


def test_vectorized_parse_numbers_matches_scalar():
    values = ["1,200", "Rs 500", "PKR 1,000.50", "(300)", "15%", "-42", "$ 9", "abc", "12-01-2026", None,
              7, 3.5, True, "", "  88  ", "(1,234.5)", "-(5)", "Rs. 1,000", "1.2.3", ".5", "500 PKR"]
    got = ingest.parse_numbers(pd.Series(values, dtype=object)).tolist()
    for v, g in zip(values, got):
        expected = ingest.parse_number(v)
        assert (expected is None and pd.isna(g)) or g == expected, (v, g, expected)


def test_big_csv_is_fast():
    import time
    rows = "\n".join(f"{i},{(i % 28) + 1:02d}/0{(i % 9) + 1}/2026,Region {i % 5},\"{i * 10:,}\"" for i in range(30_000))
    data = f"Report\n\nID,Date,Region,Amount\n{rows}\nGrand Total,,,\n".encode()
    t0 = time.perf_counter()
    tables, _ = ingest.ingest_files([("big.csv", data)])
    assert time.perf_counter() - t0 < 15
    df = tables[0].df
    assert len(df) == 30_000 and tables[0].column_types["date"] == "date"
    assert df["amount"].sum() == sum(i * 10 for i in range(30_000))


def test_too_many_rows_gives_friendly_error(monkeypatch):
    monkeypatch.setattr(ingest, "MAX_ROWS", 100)
    data = ("a,b\n" + "\n".join(f"{i},{i}" for i in range(200))).encode()
    with pytest.raises(ingest.IngestError, match="limit on this server is 100 rows"):
        ingest.ingest_files([("big.csv", data)])


@pytest.mark.parametrize("text,expected", [
    ("Order Date", "order_date"), ("Amount (Rs)", "amount_rs"), ("  Qty# ", "qty_no"),
    ("2026 Sales", "c_2026_sales"), (None, "column"), ("Growth %", "growth_pct"),
])
def test_snake_case(text, expected):
    assert ingest.snake_case(text) == expected


def test_table_names_do_not_depend_on_other_files(tmp_path):
    paths = ms.build_all(tmp_path)
    alone, _ = ingest.ingest_files([(paths[0].name, paths[0].read_bytes())])
    assert {t.name for t in alone} == {"sales_report__sales_data", "sales_report__targets"}


def test_single_sheet_file_named_after_file_without_date():
    csv = b"Region,Amount\nLahore,10\nKarachi,20\n"
    tables, _ = ingest.ingest_files([("sales_2026-10-23.csv", csv)])
    assert tables[0].name == "sales"


def test_table_name_ignores_dates():
    assert ingest.table_name("sales_2026-10-23") == ingest.table_name("sales_2026-10-24") == "sales"


def test_header_row_detection_with_title_rows():
    raw = pd.DataFrame([
        ["Company report", None, None],
        ["Generated 2026", None, None],
        ["Name", "City", "Score"],
        ["A", "X", 1],
    ], dtype=object)
    assert ingest.detect_header_row(raw) == 2


def test_unsupported_file():
    with pytest.raises(ingest.IngestError):
        ingest.ingest_files([("notes.txt", b"hello")])
