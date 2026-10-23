"""Read Excel/CSV files and clean every sheet into a tidy pandas DataFrame.

Cleaning steps (per sheet):
  1. Drop fully empty rows and columns.
  2. Detect the header row (first row that is mostly non-empty text), skipping title rows.
  3. Clean column names to snake_case (unique, ASCII).
  4. Remove "Total" / "Grand Total" / "Subtotal" rows and repeated header rows.
  5. Convert columns: real dates, Excel serial dates, date text, numeric text
     ("1,200", "Rs 500", "(300)", "15%"), otherwise trimmed text.
Sheets with identical columns in the same upload are combined into one table
with a `source_sheet` column, so trends across monthly sheets work naturally.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

PLACEHOLDERS = {"-", "--", "—", "n/a", "na", "nil", "none", "null", "#n/a", "?"}
TOTAL_RE = re.compile(r"^\s*(grand\s*|sub\s*-?\s*)?totals?\b", re.IGNORECASE)
CURRENCY_RE = re.compile(r"(?i)^(rs\.?|pkr|usd|eur|gbp|aed|sar|inr|\$|€|£|₨)\s*|\s*(rs\.?|pkr|usd|eur|gbp|aed|sar|inr|\$|€|£|₨)$")
MONTH_TEXT_RE = re.compile(r"^\s*(\d{4}[-/]\d{1,2}|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-\s,']*\d{2,4})"
                           r"\s*$", re.IGNORECASE)
DATE_TEXT_RE = re.compile(
    r"^\s*(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}"
    r"|\d{1,2}[-\s](jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-\s,]*\d{2,4}"
    r"|(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-\s]\d{1,2},?[-\s]\d{2,4})"
    r"([ T]\d{1,2}:\d{2}(:\d{2})?)?\s*$",
    re.IGNORECASE,
)
DATE_NAME_RE = re.compile(r"date|day|month|period|_dt$|^dt_|time", re.IGNORECASE)
RESERVED = {"select", "from", "where", "group", "order", "by", "table", "limit", "join", "union",
            "user", "case", "when", "end", "date", "time", "year", "month", "values", "default"}
# ratio of parseable values needed to convert a column
CONVERT_THRESHOLD = 0.95
# rows per sheet: ~500K rows need ~700 MB of RAM to clean, more than a small (512 MB) server has
MAX_ROWS = int(os.environ.get("AKDASH_MAX_ROWS", "300000"))


class IngestError(Exception):
    """Raised with a user-friendly message when a file cannot be read."""


@dataclass
class CleanTable:
    name: str
    df: pd.DataFrame
    sources: list[str]                      # "file.xlsx / Sheet1"
    report: dict = field(default_factory=dict)

    @property
    def column_types(self) -> dict[str, str]:
        return {c: type_label(self.df[c]) for c in self.df.columns}


# ---------- naming ----------

def snake_case(text, fallback: str = "column") -> str:
    s = str(text).strip().lower() if text is not None and not _is_empty(text) else ""
    s = s.replace("%", " pct ").replace("#", " no ").replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if not s:
        s = fallback
    if s[0].isdigit():
        s = f"c_{s}"
    return s


def unique_names(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if n in seen:
            seen[n] += 1
            candidate = f"{n}_{seen[n]}"
            while candidate in seen:
                seen[n] += 1
                candidate = f"{n}_{seen[n]}"
            seen[candidate] = 1
            out.append(candidate)
        else:
            seen[n] = 1
            out.append(n)
    return out


def table_name(text: str) -> str:
    # strip dates like 2026-10-23 / 23-10-2026 / 20261023 so daily files keep the same table name
    stem = re.sub(r"\d{4}[-_.]?\d{2}[-_.]?\d{2}|\d{2}[-_.]\d{2}[-_.]\d{4}", " ", str(text))
    name = snake_case(stem, fallback="data")
    if name in RESERVED:
        name = f"{name}_data"
    return name


# ---------- value helpers ----------

def _is_empty(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip() == ""
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def parse_number(v):
    """Return a float for numbers and numeric text, else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s:
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    s = CURRENCY_RE.sub("", s).strip()
    pct = s.endswith("%")
    if pct:
        s = s[:-1].strip()
    s = s.replace(",", "").replace(" ", "")
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", s):
        return None
    num = float(s)
    if pct:
        num /= 100
    return -num if negative else num


def _blank_mask(s: pd.Series) -> pd.Series:
    """True where a cell is empty (None/NaN/whitespace). Vectorized: fast on big sheets."""
    return s.astype("string").str.strip().fillna("").eq("")


def parse_numbers(s: pd.Series) -> pd.Series:
    """Vectorized parse_number for a whole column: float Series, NaN where a value isn't a number."""
    obj = s.astype(object)
    types = obj.map(type)
    is_num = types.map(lambda t: issubclass(t, (int, float)) and t is not bool).astype(bool)
    is_str = types.eq(str)
    out = pd.Series(float("nan"), index=s.index, dtype="float64")
    if is_num.any():
        out[is_num] = pd.to_numeric(obj[is_num], errors="coerce").astype("float64")
    if is_str.any():
        t = obj[is_str].astype("string").str.strip()
        neg = t.str.match(r"^\(.*\)$").fillna(False).astype(bool)
        t = t.str.replace(r"^\((.*)\)$", r"\1", regex=True).str.strip()
        t = t.str.replace(CURRENCY_RE, "", regex=True).str.strip()
        pct = t.str.endswith("%").fillna(False).astype(bool)
        t = t.str.replace(r"%$", "", regex=True).str.strip()
        t = t.str.replace(",", "", regex=False).str.replace(" ", "", regex=False)
        minus = t.str.startswith("-").fillna(False).astype(bool)
        t = t.str.replace(r"^-", "", regex=True)
        valid = t.str.fullmatch(r"\d+(\.\d+)?|\.\d+").fillna(False).astype(bool)
        nums = pd.to_numeric(t.where(valid), errors="coerce").astype("float64")
        nums = nums.where(~pct, nums / 100)
        nums = nums.where(~(neg ^ minus), -nums)
        out[is_str] = nums
    return out


DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%b-%Y", "%d %b %Y", "%d-%b-%y", "%m/%d/%Y"]


def _parse_date_text(strs: pd.Series, needed: int) -> pd.Series:
    """ISO first, then common day-first formats (fast, exact), then a slower mixed-format parse."""
    out = pd.to_datetime(strs, format="ISO8601", errors="coerce")
    if out.notna().sum() >= needed:
        return out
    for fmt in DATE_FORMATS:
        out = pd.to_datetime(strs, format=fmt, errors="coerce")
        if out.notna().sum() >= needed:
            return out
    return pd.to_datetime(strs, format="mixed", dayfirst=True, errors="coerce")


def type_label(s: pd.Series) -> str:
    if pd.api.types.is_datetime64_any_dtype(s):
        return "date"
    if pd.api.types.is_integer_dtype(s):
        return "integer"
    if pd.api.types.is_numeric_dtype(s):
        return "number"
    return "text"


# ---------- reading ----------

def read_file(name: str, data: bytes) -> dict[str, pd.DataFrame]:
    """Return {sheet_name: raw DataFrame (no header, object dtype)}."""
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        return read_pdf(name, data)[0]
    try:
        if suffix in (".xlsx", ".xlsm"):
            try:   # calamine (Rust) reads big workbooks ~10x faster; openpyxl is the fallback
                return pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=object,
                                     engine="calamine")
            except ImportError:
                return pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=object,
                                     engine="openpyxl")
        if suffix == ".csv":
            return {"csv": _read_csv(name, data)}
    except IngestError:
        raise
    except Exception as e:  # corrupt/password-protected files etc.
        raise IngestError(f"Could not read '{name}'. Is it a valid, unprotected file? ({type(e).__name__})") from e
    raise IngestError(f"'{name}': only .xlsx, .csv and .pdf files are supported.")


def _header_like(row: list) -> bool:
    values = [v for v in row if not _is_empty(v)]
    return bool(values) and sum(isinstance(v, str) and parse_number(v) is None for v in values) >= 0.6 * len(values)


def read_pdf(name: str, data: bytes) -> tuple[dict[str, pd.DataFrame], str]:
    """Tables found in a PDF report ({"table_1": raw DataFrame, ...}) and its full text.
    A table that continues on the next page (same width, repeated header or no header) is joined."""
    try:
        import pdfplumber
    except ImportError as e:
        raise IngestError("PDF support needs the 'pdfplumber' package: pip install -r requirements.txt") from e
    tables: list[list[list]] = []
    texts: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                texts.append(page.extract_text() or "")
                for raw in page.extract_tables():
                    rows = [[(c.replace("\n", " ").strip() if isinstance(c, str) else c) for c in r]
                            for r in raw if r and any(not _is_empty(c) for c in r)]
                    if not rows:
                        continue
                    prev = tables[-1] if tables else None
                    if prev and len(prev[0]) == len(rows[0]) and (rows[0] == prev[0] or not _header_like(rows[0])):
                        prev.extend(rows)   # continuation of the previous page's table
                    else:
                        tables.append(rows)
    except Exception as e:
        raise IngestError(f"Could not read '{name}'. Is it a valid, unprotected PDF? ({type(e).__name__})") from e
    frames = {f"table_{i + 1}": pd.DataFrame(rows, dtype=object) for i, rows in enumerate(tables)}
    return frames, "\n".join(texts).strip()


@dataclass
class IngestResult:
    tables: list[CleanTable]
    warnings: list[str]
    documents: dict[str, str]   # file name -> extracted text (PDF reports), used as context for the AI


def _read_csv(name: str, data: bytes) -> pd.DataFrame:
    """CSV with title rows has ragged lines, so read with the csv module rather than pandas."""
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise IngestError(f"'{name}' is not valid UTF-8 or Windows-1252 text.")
    try:
        dialect = csv.Sniffer().sniff(text[:20000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    width = max((len(r) for r in rows), default=0)
    return pd.DataFrame([r + [None] * (width - len(r)) for r in rows], dtype=object)


# ---------- cleaning ----------

def detect_header_row(raw: pd.DataFrame, max_scan: int = 30) -> int:
    """First row that is mostly filled with text values (title rows have only 1-2 cells)."""
    width = raw.shape[1]
    min_filled = max(min(2, width), int(0.5 * width + 0.5))
    fallback = None
    for i in range(min(max_scan, len(raw))):
        values = [v for v in raw.iloc[i].tolist() if not _is_empty(v)]
        if len(values) < min_filled:
            continue
        if fallback is None:
            fallback = i
        texts = [v for v in values if isinstance(v, str) and parse_number(v) is None]
        if len(texts) >= 0.6 * len(values):
            return i
    return fallback if fallback is not None else 0


def _drop_empty(raw: pd.DataFrame) -> pd.DataFrame:
    filled = ~raw.apply(_blank_mask)
    return raw.loc[filled.any(axis=1), filled.any(axis=0)].reset_index(drop=True)


SAMPLE_SIZE = 2000   # a column's type is guessed from its first values, then checked on the whole column


def _convert_column(s: pd.Series, name: str) -> tuple[pd.Series, str | None]:
    """Convert one object column to the best type. Returns (series, note). Vectorized for big sheets."""
    text_all = s.astype("string").str.strip()
    blank = (text_all.fillna("").eq("") | text_all.str.lower().isin(PLACEHOLDERS).fillna(False)).astype(bool)
    values = s.where(~blank)
    present = values[~blank]
    n = len(present)
    if n == 0:
        return pd.Series(pd.NA, index=s.index, dtype="string"), None
    sample = present.iloc[:SAMPLE_SIZE]
    needed = CONVERT_THRESHOLD * n

    # 1. real date cells from Excel
    is_date = lambda v: isinstance(v, (dt.datetime, dt.date, pd.Timestamp))  # noqa: E731
    if sample.map(is_date).mean() >= CONVERT_THRESHOLD:
        dates = values.map(is_date).astype(bool)
        if dates[~blank].mean() >= CONVERT_THRESHOLD:
            out = pd.to_datetime(values.where(dates), errors="coerce")
            return out, _lost_note(present, out)

    # 2. numbers / numeric text
    if parse_numbers(sample).notna().mean() >= CONVERT_THRESHOLD:
        out = parse_numbers(values)
        if out[~blank].notna().sum() >= needed:
            # 2a. Excel serial dates in a date-named column
            valid = out.dropna()
            if DATE_NAME_RE.search(name) and len(valid) and valid.between(20000, 60000).all():
                out = pd.to_datetime(out, unit="D", origin="1899-12-30").dt.round("s")
                return out, "converted from Excel date numbers"
            if len(valid) and (valid == valid.round()).all() and valid.abs().max() < 2**53:
                out = out.astype("Int64")
            return out, _lost_note(present, out)

    text = text_all[~blank]
    # 3. date text
    if (text.iloc[:SAMPLE_SIZE].str.match(DATE_TEXT_RE).mean() >= CONVERT_THRESHOLD
            and text.str.match(DATE_TEXT_RE).mean() >= CONVERT_THRESHOLD):
        out = _parse_date_text(text_all.where(~blank), needed)
        if out.notna().sum() >= needed:
            return out, _lost_note(present, out)

    # 3b. month text ("2026-07", "Jul 2026", "July-26") -> first day of the month
    if text.str.match(MONTH_TEXT_RE).mean() >= CONVERT_THRESHOLD:
        months = {v: _parse_month(v) for v in text.unique()}   # few distinct months: parse each once
        out = pd.to_datetime(text_all.where(~blank).map(months), errors="coerce")
        if out.notna().sum() >= needed:
            return out, "month values converted to the first day of each month"

    # 4. text
    return text_all.where(~blank), None


def _parse_month(s: str):
    s = s.strip().replace("'", " ")
    m = re.match(r"^(\d{4})[-/](\d{1,2})$", s)
    if m:
        return pd.Timestamp(int(m.group(1)), int(m.group(2)), 1) if 1 <= int(m.group(2)) <= 12 else pd.NaT
    for fmt in ("%b %Y", "%B %Y", "%b-%Y", "%B-%Y", "%b %y", "%B %y", "%b-%y", "%B-%y", "%b, %Y", "%B, %Y"):
        try:
            return pd.to_datetime(s, format=fmt)
        except (ValueError, TypeError):
            continue
    return pd.NaT


def _lost_note(present: pd.Series, out: pd.Series) -> str | None:
    lost = len(present) - int(out.notna().sum())
    return f"{lost} value(s) could not be converted and were left empty" if lost > 0 else None


def clean_sheet(raw: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    report: dict = {"raw_rows": int(raw.shape[0]), "raw_columns": int(raw.shape[1])}
    raw = _drop_empty(raw)
    if raw.empty:
        report["empty"] = True
        return pd.DataFrame(), report

    h = detect_header_row(raw)
    report["header_row"] = h + 1  # 1-based for display (after empty rows were removed)
    report["title_rows_skipped"] = h
    header = raw.iloc[h].tolist()
    body = raw.iloc[h + 1:].reset_index(drop=True)

    cols = unique_names([snake_case(v, fallback=f"column_{i + 1}") for i, v in enumerate(header)])
    body.columns = cols

    lowered = body.apply(lambda c: c.astype("string").str.strip().str.lower().fillna(""))

    # repeated header rows (e.g. exports with a header on each page)
    header_text = ["" if _is_empty(v) else str(v).strip().lower() for v in header]
    is_repeat = lowered.eq(header_text).all(axis=1) if len(body) else pd.Series(dtype=bool)

    # total rows: a label in one of the first 3 columns starting with "total"/"grand total"/"subtotal"
    is_total = pd.Series(False, index=body.index)
    for c in cols[:3]:
        is_total |= lowered[c].str.match(TOTAL_RE).fillna(False).astype(bool)

    keep = ~(is_repeat | is_total) & ~lowered.eq("").all(axis=1)
    report["total_rows_removed"] = int(is_total.sum())
    report["repeated_headers_removed"] = int(is_repeat.sum())
    body = body.loc[keep].reset_index(drop=True)

    notes = {}
    df = pd.DataFrame(index=body.index)
    for c in cols:
        df[c], note = _convert_column(body[c], c)
        if note:
            notes[c] = note
    report["notes"] = notes
    report["rows"] = int(len(df))
    return df, report


# ---------- upload-level ----------

def ingest_files(files: list[tuple[str, bytes]]) -> tuple[list[CleanTable], list[str]]:
    """Clean all sheets of all files. Returns (tables, warnings)."""
    result = ingest_upload(files)
    return result.tables, result.warnings


def ingest_upload(files: list[tuple[str, bytes]], text_extractor=None) -> IngestResult:
    """Clean every sheet / PDF table of every file.
    text_extractor(file_name, text) -> [(table_name, rows)] is used for PDFs that have text but no tables
    (the AI pulls the tables out of the text; those tables are marked for the user to verify)."""
    warnings: list[str] = []
    documents: dict[str, str] = {}
    cleaned: list[tuple[str, str, pd.DataFrame, dict]] = []  # (file, sheet, df, report)
    for fname, data in files:
        ai_extracted = False
        if Path(fname).suffix.lower() == ".pdf":
            sheets, text = read_pdf(fname, data)
            if text:
                documents[fname] = text
            if not sheets:
                if not text:
                    warnings.append(f"'{fname}' has no readable text (it may be a scanned image). "
                                    "Scanned PDFs are not supported yet.")
                elif text_extractor is None:
                    warnings.append(f"'{fname}' has no tables; its text is kept as context for the AI.")
                else:
                    extracted = text_extractor(fname, text) or []
                    sheets = {name: pd.DataFrame(rows, dtype=object) for name, rows in extracted if rows}
                    ai_extracted = bool(sheets)
                    if not sheets:
                        warnings.append(f"No tables could be found in '{fname}'; its text is kept as context.")
        else:
            sheets = read_file(fname, data)
        for sheet, raw in sheets.items():
            if len(raw) > MAX_ROWS:
                raise IngestError(f"'{fname} / {sheet}' has {len(raw):,} rows. The limit on this server is "
                                  f"{MAX_ROWS:,} rows per sheet. Please split the file (e.g. by month) or filter it.")
            df, report = clean_sheet(raw)
            if df.empty or len(df.columns) == 0:
                warnings.append(f"'{fname} / {sheet}' has no data rows and was skipped.")
                continue
            if ai_extracted:
                report.setdefault("notes", {})["⚠ source"] = ("extracted by AI from the PDF text, "
                                                             "please check the numbers against the report")
                report["ai_extracted"] = True
            cleaned.append((fname, str(sheet), df, report))
    if not cleaned:
        if documents:
            raise IngestError("No tables were found in the uploaded PDF(s). Upload a report that contains tables, "
                              "or an Excel/CSV file.")
        raise IngestError("No data found in the uploaded file(s).")

    # Table names depend only on the file itself (never on which other files are uploaded with it), so a daily
    # upload of one file keeps the names the dashboard uses. Sheets of one file with identical columns are combined.
    groups: dict[tuple, list] = {}
    for item in cleaned:
        groups.setdefault((item[0], tuple(sorted(item[2].columns))), []).append(item)
    groups_per_file: dict[str, int] = {}
    for fname, _ in groups:
        groups_per_file[fname] = groups_per_file.get(fname, 0) + 1

    tables: list[CleanTable] = []
    used: set[str] = set()
    for members in groups.values():
        fname, sheet = members[0][0], members[0][1]
        stem = Path(fname).stem
        if len(members) > 1:
            name = table_name(stem) if groups_per_file[fname] == 1 else f"{table_name(stem)}__{table_name(sheet)}"
            parts = []
            for f, sh, df, _ in members:
                part = df.copy()
                part["source_sheet"] = sh
                parts.append(part)
            df = _concat(parts, list(members[0][2].columns) + ["source_sheet"])
            report = {"combined_from": [f"{f} / {sh}" for f, sh, *_ in members],
                      "rows": int(len(df)),
                      "total_rows_removed": sum(m[3].get("total_rows_removed", 0) for m in members),
                      "title_rows_skipped": max(m[3].get("title_rows_skipped", 0) for m in members),
                      "notes": {k: v for m in members for k, v in m[3].get("notes", {}).items()},
                      "per_sheet": {f"{f} / {sh}": r for f, sh, _, r in members}}
        else:
            df, report = members[0][2], members[0][3]
            if groups_per_file[fname] == 1:
                name = table_name(stem)
            else:
                name = f"{table_name(stem)}__{table_name(sheet)}"
        base, i = name, 2
        while name in used:
            name, i = f"{base}_{i}", i + 1
        used.add(name)
        tables.append(CleanTable(name=name, df=df, sources=[f"{f} / {sh}" for f, sh, *_ in members],
                                 report=report))
    return IngestResult(tables, warnings, documents)


def _concat(parts: list[pd.DataFrame], columns: list[str]) -> pd.DataFrame:
    parts = [p[columns] for p in parts]
    df = pd.concat(parts, ignore_index=True)
    # a column may be int in one sheet and float in another; keep the most general type
    for c in columns:
        types = {type_label(p[c]) for p in parts}
        if types == {"integer", "number"}:
            df[c] = df[c].astype("float64")
        elif len(types) > 1 and "text" in types:
            df[c] = df[c].astype("string")
    return df


# ---------- daily updates ----------

def apply_mappings(tables: dict[str, pd.DataFrame], mappings: dict[str, str]) -> dict[str, pd.DataFrame]:
    """Rename new tables/columns back to the names the dashboard uses.
    mappings: {"old_table": "new_table"} for tables, {"table.old_column": "new_column"} for columns.
    A mapping is only applied when the old name is missing and the new name is present."""
    out = dict(tables)
    for old, new in mappings.items():
        if "." not in old and old not in out and new in out:
            out[old] = out.pop(new)
    for old, new in mappings.items():
        if "." in old:
            table, old_col = old.split(".", 1)
            df = out.get(table)
            if df is not None and old_col not in df.columns and new in df.columns:
                out[table] = df.rename(columns={new: old_col})
    return out


def compare_schemas(previous: dict[str, dict], new: dict[str, pd.DataFrame], append: bool) -> dict:
    """Compare the previous upload's columns ({table: {col: type}}) with the new tables.
    Returns {"missing_tables": [...], "missing_columns": {table: [...]}, "new_columns": {table: [...]},
             "type_changes": {table: {col: (old, new)}}}. In append mode missing tables are carried over."""
    ignore = {"upload_date"}
    report = {"missing_tables": [], "missing_columns": {}, "new_columns": {}, "type_changes": {}}
    for table, cols in previous.items():
        if table not in new:
            if not append:
                report["missing_tables"].append(table)
            continue
        df = new[table]
        missing = [c for c in cols if c not in df.columns and c not in ignore]
        added = [c for c in df.columns if c not in cols and c not in ignore]
        changed = {c: (t, type_label(df[c])) for c, t in cols.items()
                   if c in df.columns and c not in ignore and type_label(df[c]) != t
                   and {t, type_label(df[c])} != {"integer", "number"}}
        if missing:
            report["missing_columns"][table] = missing
        if added:
            report["new_columns"][table] = added
        if changed:
            report["type_changes"][table] = changed
    return report


def has_breaking_changes(report: dict) -> bool:
    return bool(report["missing_tables"] or report["missing_columns"])


def append_tables(previous: dict[str, pd.DataFrame] | None, new: dict[str, pd.DataFrame],
                  upload_date: str) -> dict[str, pd.DataFrame]:
    """Append mode: add an upload_date column to the new rows and stack them under the previous data.
    Re-uploading the same date replaces that date's rows. Tables not in the new upload are kept."""
    day = pd.Timestamp(upload_date)
    out = dict(previous or {})
    for name, df in new.items():
        df = df.copy()
        df["upload_date"] = day
        old = out.get(name)
        if old is not None:
            if "upload_date" in old.columns:
                old = old.loc[old["upload_date"] != day]
            else:
                old = old.assign(upload_date=pd.NaT)
            columns = list(dict.fromkeys(list(old.columns) + list(df.columns)))
            df = _concat_aligned([old, df], columns)
        out[name] = df.reset_index(drop=True)
    return out


def _concat_aligned(parts: list[pd.DataFrame], columns: list[str]) -> pd.DataFrame:
    """Stack frames whose columns differ; a column missing from one frame gets empty values of the right type."""
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame(columns=columns)
    dtypes = {}
    for p in parts:
        for c in p.columns:
            dtypes.setdefault(c, p[c].dtype)
    aligned = []
    for p in parts:
        p = p.copy()
        for c in columns:
            if c not in p.columns:
                p[c] = pd.Series(index=p.index, dtype=dtypes[c])
        aligned.append(p[columns])
    return _concat(aligned, columns) if len(aligned) > 1 else aligned[0]


def save_tables(tables: list[CleanTable], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for t in tables:
        t.df.to_parquet(folder / f"{t.name}.parquet", index=False)


def save_frames(tables: dict[str, pd.DataFrame], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_parquet(folder / f"{name}.parquet", index=False)


def save_documents(documents: dict[str, str], folder: Path) -> None:
    import json
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "documents.json").write_text(json.dumps(documents, ensure_ascii=False), encoding="utf-8")


def load_documents(folder: str | Path) -> dict[str, str]:
    import json
    path = Path(folder) / "documents.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_tables(folder: str | Path) -> dict[str, pd.DataFrame]:
    folder = Path(folder)
    return {p.stem: pd.read_parquet(p) for p in sorted(folder.glob("*.parquet"))}
