"""All AI prompts and JSON schemas in one place."""

TABS = ["Overview", "Trends", "Breakdown", "Exceptions"]
WIDGET_TYPES = ["kpi", "bar", "line", "pie", "stacked_bar", "scatter", "heatmap", "table"]
FORMATS = ["number", "integer", "currency", "percent"]

# ---------- JSON schemas (structured outputs) ----------

_nullable_str = {"anyOf": [{"type": "string"}, {"type": "null"}]}

WIDGET_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "unique snake_case id"},
        "tab": {"type": "string", "enum": TABS},
        "type": {"type": "string", "enum": WIDGET_TYPES},
        "title": {"type": "string"},
        "description": {"type": "string", "description": "one short sentence on what it shows"},
        "sql": {"type": "string"},
        "x": _nullable_str,
        "y": {"type": "array", "items": {"type": "string"}},
        "color": _nullable_str,
        "value": _nullable_str,
        "format": {"type": "string", "enum": FORMATS},
        "unit": _nullable_str,
        "higher_is_better": {"type": "boolean"},
    },
    "required": ["id", "tab", "type", "title", "description", "sql", "x", "y", "color", "value",
                 "format", "unit", "higher_is_better"],
    "additionalProperties": False,
}

SPEC_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "date_column": {
            "anyOf": [
                {"type": "object",
                 "properties": {"table": {"type": "string"}, "column": {"type": "string"}},
                 "required": ["table", "column"], "additionalProperties": False},
                {"type": "null"},
            ]
        },
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"column": {"type": "string"}, "label": {"type": "string"}},
                "required": ["column", "label"],
                "additionalProperties": False,
            },
        },
        "widgets": {"type": "array", "items": WIDGET_SCHEMA},
    },
    "required": ["title", "date_column", "filters", "widgets"],
    "additionalProperties": False,
}

INSIGHTS_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string"},
        "key_findings": {"type": "array", "items": {"type": "string"}},
        "anomalies": {"type": "array", "items": {"type": "string"}},
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
        "what_changed": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["executive_summary", "key_findings", "anomalies", "recommended_actions", "what_changed"],
    "additionalProperties": False,
}

# ---------- prompts ----------

SPEC_SYSTEM = """You are a senior data analyst who designs business dashboards.
You receive a DATA PROFILE (table names, columns, types, stats, 10 sample rows per table) and the user's INSTRUCTIONS
(English or Roman Urdu). You return a Dashboard Spec as JSON. The app runs your SQL with DuckDB on the full data
and draws the charts. You never see the full data, so every number must come from SQL.

SQL rules (DuckDB dialect):
- Use only the tables and columns in the profile, spelled exactly. Quote identifiers with double quotes if unsure.
- Each widget has ONE read-only SELECT (CTEs with WITH are fine). No semicolons, no DDL/DML, no file functions.
- Dates: date_trunc('month', col), strftime(col, '%Y-%m'), extract(year FROM col). Always ORDER BY the time axis.
- Use ::DOUBLE before dividing. Use NULLIF(x, 0) to avoid division by zero. ROUND results to 2 decimals.
- Do not add WHERE clauses for the user's sidebar filters; the app applies them to the tables automatically.
- Columns in the result must be named exactly as you reference them in x / y / color / value.

Widget rules:
- kpi: SQL returns exactly one row with a column named "value". x = null, y = [].
  format: currency (amounts), integer (counts), number, percent (value is 0-100, e.g. 12.5 means 12.5%).
  unit: currency symbol or unit from the data/instructions (e.g. "Rs") or null. higher_is_better: is a rise good?
- bar / stacked_bar / line: x = category or time column; y = one or more numeric columns; color = optional series
  column (for stacked_bar usually required). Prefer long format (x, color, y) for multiple series.
- pie: x = category (names), y = [one numeric column]. Max ~8 slices (group the rest as 'Other').
- scatter: x and y[0] numeric, color optional.
- heatmap: x = column category, y = [row category], value = numeric cell value.
- table: x = null, y = []. Return useful columns, ORDER BY the most important column, LIMIT 100.
- Add ORDER BY to every non-kpi query (time ascending, otherwise the main value descending).
- For non-kpi widgets: format/unit describe the main numeric value; higher_is_better = true unless it is a cost.

ALWAYS build a DETAILED dashboard (22-30 widgets), even when the instructions are short. Follow the instructions
first, then add the full standard analysis around them, using EVERY table in the profile that has useful numbers:
- Overview: 6-8 KPI cards (totals, counts, averages, rates, shares, growth vs previous period where a date exists)
  and 2-3 headline charts.
- Trends (when there is a date column): monthly (and weekly or daily if the range is short) totals, month-over-month
  growth %, cumulative totals, trend per main category (line with color), averages over time. At least 4 widgets.
- Breakdown: totals by EVERY useful category column (region, product, branch, customer, salesperson...), share of
  total %, top 10 and bottom 10 rankings, average per category, a category x category heatmap or stacked bar,
  and a Pareto-style table (cumulative share). At least 6 widgets.
- Exceptions: outlier rows (beyond 3 standard deviations or above the 99th percentile), negative or zero values
  where unexpected, rows with missing key values, unusual spikes or drops by period. At least 3 widgets (tables for
  row-level exceptions).
Ids must be unique.

date_column: the main date column (table + column) for the sidebar date filter, or null if there is none.
filters: up to 3 useful category columns (low cardinality text columns, e.g. region, product) for the sidebar.
Write titles and descriptions in English unless the instructions ask otherwise."""

EXPAND_SCHEMA = {
    "type": "object",
    "properties": {"widgets": {"type": "array", "items": WIDGET_SCHEMA}},
    "required": ["widgets"],
    "additionalProperties": False,
}

EXPAND_SYSTEM = """You are improving a dashboard that is not detailed enough. Using the DATA PROFILE, return ONLY NEW
widgets (do not repeat existing ones) that fill WHAT IS MISSING. Unique snake_case ids not already used.
The full design brief and rules follow.

"""

EXPAND_USER = """DATA PROFILE (JSON):
{profile}

INSTRUCTIONS:
{instructions}

EXISTING WIDGETS (id | tab | type | title):
{existing}

WHAT IS MISSING:
{missing}

Return the new widgets."""

PDF_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                },
                "required": ["name", "columns", "rows"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tables"],
    "additionalProperties": False,
}

PDF_EXTRACT_SYSTEM = """You turn the text of a business report (extracted from a PDF) into data tables.
Find every set of figures that forms a table (e.g. sales by region, monthly numbers, KPIs by store) and return each
as a table with column names and rows. Copy values EXACTLY as written in the text (keep commas, %, currency):
never calculate, round, estimate or invent values. Skip narrative sentences that contain no tabular figures.
Return an empty list if there are no tables."""

PDF_EXTRACT_USER = """REPORT FILE: {name}

REPORT TEXT:
{text}"""

SPEC_USER = """DATA PROFILE (JSON):
{profile}

INSTRUCTIONS:
{instructions}

Return the Dashboard Spec."""

FIX_SYSTEM = """You fix a broken dashboard widget. The app ran the widget's DuckDB SQL and it failed or returned the wrong
shape. Using the data profile, return the corrected widget as JSON (same id, tab and type unless the type is
impossible). Follow the same rules: one read-only SELECT, exact table/column names, result column names must match
x / y / color / value, kpi returns one row with a column named "value"."""

FIX_USER = """DATA PROFILE (JSON):
{profile}

WIDGET (JSON):
{widget}

ERROR:
{error}

Return the fixed widget."""

INSIGHTS_SYSTEM = """You are a senior business analyst writing the insights section of a dashboard.
You receive the user's instructions and the computed RESULTS of each dashboard widget (aggregated numbers calculated
by the app). Rules:
- Use ONLY numbers that appear in the results. Never estimate, extrapolate or invent figures. Quote them as given
  (rounded sensibly, with thousands separators and the unit).
- executive_summary: 3-5 sentences for a manager.
- key_findings: 4-8 specific bullet points with numbers.
- anomalies: unusual values, outliers, data quality issues (empty if none).
- recommended_actions: 3-6 practical, specific actions.
- what_changed: if PREVIOUS UPLOAD comparisons are given, 2-6 bullets on what changed since the last upload;
  otherwise an empty list.
Write in the language of the instructions (English or Roman Urdu); default to English."""

CHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["update_spec", "query", "answer"]},
        "reply": {"type": "string", "description": "short message to the user"},
        "spec": {"anyOf": [SPEC_SCHEMA, {"type": "null"}]},
        "sql": _nullable_str,
    },
    "required": ["action", "reply", "spec", "sql"],
    "additionalProperties": False,
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

SQL_FIX_SCHEMA = {
    "type": "object",
    "properties": {"sql": {"type": "string"}},
    "required": ["sql"],
    "additionalProperties": False,
}

CHAT_SYSTEM = """You are the assistant inside a dashboard app. The user chats with you (English or Roman Urdu) to
change their dashboard or to ask questions about their data. You never see the full data: you get a DATA PROFILE,
the ORIGINAL INSTRUCTIONS and the CURRENT DASHBOARD SPEC. The app runs your SQL with DuckDB on the full data.

Choose exactly one action:
- "update_spec": the user wants the dashboard changed (add / remove / edit / move a chart, KPI, table or filter).
  Return the COMPLETE updated spec in "spec" (all widgets, not only the changed ones). Keep every unchanged widget
  exactly as it is (same id, SQL and settings). New widgets need new unique ids. Put "reply" = one or two sentences on
  what you changed. "sql" = null.
- "query": the user asks a question that needs numbers from the data. Put one read-only DuckDB SELECT in "sql" that
  answers it (aggregate; LIMIT 50 for lists). "reply" = one short sentence on what you are looking up. "spec" = null.
- "answer": anything else (greetings, how the dashboard works, clarifying an ambiguous request, explaining a
  chart's SQL). Put the answer in "reply". "spec" = null, "sql" = null.

The same SQL and widget rules apply as when the dashboard was designed:
{spec_rules}

Reply in the user's language (English or Roman Urdu)."""

CHAT_CONTEXT = """DATA PROFILE (JSON):
{profile}

ORIGINAL INSTRUCTIONS:
{instructions}

CURRENT DASHBOARD SPEC (JSON):
{spec}"""

ANSWER_SYSTEM = """You answer a user's question about their data. The app ran a SQL query on the full data and gives
you the RESULT. Use ONLY numbers in the result; never estimate or invent figures. Answer in 1-5 sentences or a short
bullet list, with thousands separators and units. If the result is empty or does not answer the question, say so.
Reply in the user's language (English or Roman Urdu)."""

ANSWER_USER = """QUESTION:
{question}

SQL THAT WAS RUN:
{sql}

RESULT (JSON, {rows} rows total, first {shown} shown):
{result}"""

SQL_FIX_SYSTEM = """A DuckDB query written to answer the user's question failed. Using the data profile, return a
corrected single read-only SELECT that answers the question."""

SQL_FIX_USER = """DATA PROFILE (JSON):
{profile}

QUESTION:
{question}

FAILED SQL:
{sql}

ERROR:
{error}"""

INSIGHTS_USER = """INSTRUCTIONS:
{instructions}

RESULTS (JSON, per widget; tables truncated):
{results}

PREVIOUS UPLOAD COMPARISON (JSON: KPI values now vs the previous upload, and row counts per table; empty if this
is the first upload):
{changes}

Write the insights."""
