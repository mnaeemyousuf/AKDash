"""KPI values from the dashboard pipeline must match plain pandas calculations."""
import copy

import pandas as pd
import pytest

import ai
import ingest
import profiler
from samples import make_samples as ms


@pytest.fixture(scope="module")
def tables(tmp_path_factory):
    folder = tmp_path_factory.mktemp("samples")
    paths = ms.build_all(folder)
    result, _ = ingest.ingest_files([(p.name, p.read_bytes()) for p in paths])
    return {t.name: t.df for t in result}


def kpi(id_, sql, fmt="currency"):
    return {"id": id_, "tab": "Overview", "type": "kpi", "title": id_, "description": "", "sql": sql,
            "x": None, "y": [], "color": None, "value": None, "format": fmt, "unit": "Rs", "higher_is_better": True}


SPEC = {
    "title": "Sales",
    "date_column": {"table": "sales_report__sales_data", "column": "order_date"},
    "filters": [{"column": "region", "label": "Region"}, {"column": "not_a_column", "label": "X"}],
    "widgets": [
        kpi("total_sales", "SELECT SUM(amount_rs) AS value FROM sales_report__sales_data"),
        kpi("orders", "SELECT COUNT(*) AS value FROM sales_report__sales_data", "integer"),
        kpi("avg_order", "SELECT ROUND(AVG(amount_rs), 2) AS value FROM sales_report__sales_data"),
        kpi("units_mar", "SELECT SUM(units) AS value FROM monthly_sales WHERE source_sheet = 'Mar'", "integer"),
        kpi("expenses", "SELECT SUM(amount) AS value FROM expenses"),
        {"id": "by_region", "tab": "Breakdown", "type": "bar", "title": "Sales by region", "description": "",
         "sql": "SELECT region, SUM(amount_rs) AS sales FROM sales_report__sales_data GROUP BY 1 ORDER BY 2 DESC",
         "x": "region", "y": ["sales"], "color": None, "value": None, "format": "currency", "unit": "Rs",
         "higher_is_better": True},
        {"id": "monthly", "tab": "Trends", "type": "line", "title": "Monthly revenue", "description": "",
         "sql": "SELECT date_trunc('month', date) AS month, SUM(revenue) AS revenue FROM monthly_sales "
                "GROUP BY 1 ORDER BY 1",
         "x": "month", "y": ["revenue"], "color": None, "value": None, "format": "currency", "unit": "Rs",
         "higher_is_better": True},
    ],
}


def test_kpis_match_pandas(tables):
    spec = ai.normalize_spec(SPEC, tables)
    results = ai.run_spec(spec, tables)
    value = {wid: df["value"].iloc[0] for wid, (df, err) in results.items() if df is not None and "value" in df}
    sales = tables["sales_report__sales_data"]
    monthly = tables["monthly_sales"]

    assert value["total_sales"] == sales["amount_rs"].sum()
    assert value["orders"] == len(sales)
    assert value["avg_order"] == pytest.approx(round(sales["amount_rs"].mean(), 2))
    assert value["units_mar"] == monthly.loc[monthly["source_sheet"] == "Mar", "units"].sum()
    assert value["expenses"] == tables["expenses"]["amount"].sum()

    by_region, _ = results["by_region"]
    expected = sales.groupby("region")["amount_rs"].sum()
    assert dict(zip(by_region["region"], by_region["sales"])) == expected.to_dict()

    monthly_df, _ = results["monthly"]
    assert monthly_df["revenue"].sum() == monthly["revenue"].sum()
    assert len(monthly_df) == 3


def test_normalize_spec_drops_unknown_filter(tables):
    spec = ai.normalize_spec(SPEC, tables)
    assert [f["column"] for f in spec["filters"]] == ["region"]


def test_kpi_changes_vs_previous_upload(tables):
    spec = ai.normalize_spec(SPEC, tables)
    previous = dict(tables)
    previous["sales_report__sales_data"] = tables["sales_report__sales_data"].head(60)
    changes = {c["id"]: c for c in ai.kpi_changes(spec, ai.run_spec(spec, tables), previous)}
    assert changes["orders"]["now"] == 120
    assert changes["orders"]["previous"] == 60
    assert changes["orders"]["change_pct"] == 100.0


class FakeLLM:
    """Returns a spec with one broken widget, then a fixed widget, then insights."""

    def __init__(self, spec):
        self.spec = spec
        self.calls = []

    def json(self, system, user, schema, max_tokens=0):
        self.calls.append(system[:20])
        if schema is ai.prompts.SPEC_SCHEMA:
            return copy.deepcopy(self.spec)
        if schema is ai.prompts.WIDGET_SCHEMA:
            return kpi("broken", "SELECT SUM(amount_rs) AS value FROM sales_report__sales_data")
        if schema is ai.prompts.EXPAND_SCHEMA:
            return {"widgets": []}
        return {"executive_summary": "ok", "key_findings": [], "anomalies": [], "recommended_actions": [],
                "what_changed": []}


def test_pipeline_fixes_broken_query_and_writes_insights(tables):
    spec = copy.deepcopy(SPEC)
    spec["widgets"].append(kpi("broken", "SELECT SUM(amount_typo) AS value FROM sales_report__sales_data"))
    llm = FakeLLM(spec)
    out = ai.build_dashboard(llm, tables, "sales overview", profiler.profile_json(tables))
    broken = next(w for w in out["widgets"] if w["id"] == "broken")
    assert "error" not in broken
    assert "amount_rs" in broken["sql"]
    assert out["insights"]["executive_summary"] == "ok"
    assert len(llm.calls) == 4   # spec, detail top-up (the test spec is small), one fix, insights


def test_pipeline_gives_up_after_two_fixes(tables):
    class AlwaysBroken(FakeLLM):
        def json(self, system, user, schema, max_tokens=0):
            if schema is ai.prompts.WIDGET_SCHEMA:
                self.calls.append("fix")
                return kpi("broken", "SELECT nope AS value FROM sales_report__sales_data")
            return super().json(system, user, schema, max_tokens)

    spec = copy.deepcopy(SPEC)
    spec["widgets"].append(kpi("broken", "SELECT nope AS value FROM sales_report__sales_data"))
    llm = AlwaysBroken(spec)
    out = ai.build_dashboard(llm, tables, "", profiler.profile_json(tables))
    assert "broken" not in [w["id"] for w in out["widgets"]]      # left out of the new dashboard
    assert out["skipped_widgets"] == ["broken"]
    assert llm.calls.count("fix") == 2


def test_profile_has_no_full_data(tables):
    prof = profiler.profile_tables(tables)
    sales = prof["sales_report__sales_data"]
    assert sales["rows"] == 120
    assert len(sales["sample_rows"]) == 10
    assert sales["columns"]["amount_rs"]["sum"] == tables["sales_report__sales_data"]["amount_rs"].sum()
    assert len(sales["columns"]["region"]["top_values"]) <= 5
