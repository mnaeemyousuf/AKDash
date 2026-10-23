"""Daily uploads: column check, mappings, Replace/Append, refreshed insights; plus the Mistral client."""
import copy
import json

import pandas as pd
import pytest

import ai
import db
import ingest
from test_dashboard import SPEC


def sales(dates, amounts, region="Lahore"):
    return pd.DataFrame({"order_date": pd.to_datetime(dates), "region": pd.array([region] * len(dates),
                                                                                 dtype="string"),
                         "amount": amounts})


# ---------- column check ----------

def test_compare_schemas_finds_missing_new_and_type_changes():
    previous = {"sales": {"order_date": "date", "region": "text", "amount": "integer"},
                "targets": {"region": "text", "target": "integer"}}
    new = {"sales": pd.DataFrame({"order_date": pd.to_datetime(["2026-01-01"]), "area": ["x"],
                                  "amount": ["abc"]})}
    report = ingest.compare_schemas(previous, new, append=False)
    assert report["missing_tables"] == ["targets"]
    assert report["missing_columns"] == {"sales": ["region"]}
    assert report["new_columns"] == {"sales": ["area"]}
    assert report["type_changes"] == {"sales": {"amount": ("integer", "text")}}
    assert ingest.has_breaking_changes(report)


def test_same_columns_is_not_breaking_and_int_float_is_fine():
    previous = {"sales": {"order_date": "date", "region": "text", "amount": "integer"}}
    new = {"sales": sales(["2026-01-01"], [1.5])}
    report = ingest.compare_schemas(previous, new, append=False)
    assert not ingest.has_breaking_changes(report)
    assert report["type_changes"] == {}


def test_append_mode_does_not_flag_missing_tables():
    previous = {"sales": {"order_date": "date", "region": "text", "amount": "integer"}, "targets": {"x": "text"}}
    report = ingest.compare_schemas(previous, {"sales": sales(["2026-01-01"], [1])}, append=True)
    assert report["missing_tables"] == []


def test_apply_mappings_renames_tables_and_columns():
    new = {"sales_v2": pd.DataFrame({"area": ["x"], "amount": [1]})}
    out = ingest.apply_mappings(new, {"sales": "sales_v2", "sales.region": "area"})
    assert list(out) == ["sales"]
    assert list(out["sales"].columns) == ["region", "amount"]


def test_apply_mappings_ignored_when_old_name_still_present():
    new = {"sales": pd.DataFrame({"region": ["x"], "area": ["y"]})}
    out = ingest.apply_mappings(new, {"sales.region": "area"})
    assert list(out["sales"].columns) == ["region", "area"]


def test_affected_widgets():
    report = {"missing_tables": ["expenses"],
              "missing_columns": {"sales_report__sales_data": ["amount_rs"]}, "new_columns": {}, "type_changes": {}}
    affected = ai.affected_widgets(SPEC, report)
    assert affected["expenses"] == ["expenses"]
    assert set(affected["sales_report__sales_data.amount_rs"]) == {"total_sales", "avg_order", "Sales by region"}


# ---------- Replace / Append ----------

def test_append_adds_upload_date_and_stacks():
    day1 = ingest.append_tables(None, {"sales": sales(["2026-01-01", "2026-01-01"], [10, 20])}, "2026-01-01")
    assert (day1["sales"]["upload_date"] == pd.Timestamp("2026-01-01")).all()
    day2 = ingest.append_tables(day1, {"sales": sales(["2026-01-02"], [5])}, "2026-01-02")
    assert len(day2["sales"]) == 3
    assert day2["sales"]["amount"].sum() == 35


def test_append_same_day_again_replaces_that_day():
    day1 = ingest.append_tables(None, {"sales": sales(["2026-01-01"], [10])}, "2026-01-01")
    day2 = ingest.append_tables(day1, {"sales": sales(["2026-01-02"], [5])}, "2026-01-02")
    again = ingest.append_tables(day2, {"sales": sales(["2026-01-02", "2026-01-02"], [7, 8])}, "2026-01-02")
    assert again["sales"]["amount"].sum() == 10 + 7 + 8


def test_append_keeps_tables_not_in_new_upload_and_aligns_columns():
    day1 = ingest.append_tables(None, {"sales": sales(["2026-01-01"], [10]),
                                       "targets": pd.DataFrame({"t": [1]})}, "2026-01-01")
    extra = sales(["2026-01-02"], [5]).assign(channel=pd.array(["web"], dtype="string"))
    day2 = ingest.append_tables(day1, {"sales": extra}, "2026-01-02")
    assert "targets" in day2
    df = day2["sales"]
    assert pd.api.types.is_datetime64_any_dtype(df["order_date"])
    assert pd.api.types.is_datetime64_any_dtype(df["upload_date"])
    assert df["channel"].isna().sum() == 1


def test_replace_mode_is_just_the_new_tables():
    # Replace mode stores the new upload as-is; the previous snapshot is only used for comparisons.
    new = {"sales": sales(["2026-02-01"], [99])}
    report = ingest.compare_schemas({"sales": {"order_date": "date", "region": "text", "amount": "integer"}},
                                    new, append=False)
    assert not ingest.has_breaking_changes(report)


# ---------- refreshed insights ----------

class InsightsLLM:
    def __init__(self):
        self.user = None

    def json(self, system, user, schema, max_tokens=0, messages=None):
        self.user = user
        return {"executive_summary": "new", "key_findings": [], "anomalies": [], "recommended_actions": [],
                "what_changed": ["Orders went from 60 to 120."]}


def test_refresh_insights_uses_saved_spec_and_real_changes(tmp_path_factory):
    from samples import make_samples as ms
    folder = tmp_path_factory.mktemp("s")
    paths = ms.build_all(folder)
    cleaned, _ = ingest.ingest_files([(p.name, p.read_bytes()) for p in paths])
    tables = {t.name: t.df for t in cleaned}
    previous = dict(tables)
    previous["sales_report__sales_data"] = tables["sales_report__sales_data"].head(60)

    spec = ai.normalize_spec(copy.deepcopy(SPEC), tables)
    spec["insights"] = {"executive_summary": "old"}
    llm = InsightsLLM()
    new_spec = ai.refresh_insights(llm, spec, "sales", tables, previous, "2026-10-24")

    assert new_spec["widgets"] == spec["widgets"]                    # dashboard not redesigned
    assert new_spec["insights"]["what_changed"] == ["Orders went from 60 to 120."]
    changes = json.loads(llm.user.split("is the first upload):\n", 1)[1].split("\n\nWrite the insights.")[0])
    kpis = {k["id"]: k for k in changes["kpis"]}
    assert kpis["orders"]["previous"] == 60 and kpis["orders"]["now"] == 120
    assert changes["row_counts"]["sales_report__sales_data"] == {"previous": 60, "now": 120}


# ---------- mappings in the database ----------

def test_mappings_saved_and_updated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    pid = db.create_project("P")
    db.save_mappings(pid, {"sales.region": "area"})
    db.save_mappings(pid, {"sales.region": "zone", "old": "new"})
    assert db.get_mappings(pid) == {"sales.region": "zone", "old": "new"}


# ---------- Mistral client (HTTP mocked) ----------

class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def ok(content, finish="stop"):
    return FakeResponse(200, {"choices": [{"message": {"content": content}, "finish_reason": finish}]})


@pytest.fixture
def mistral_calls(monkeypatch):
    import requests
    calls, queue = [], []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(json)
        return queue.pop(0)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return calls, queue


def test_mistral_json_schema_request(mistral_calls):
    calls, queue = mistral_calls
    queue.append(ok('{"answer": "42"}'))
    llm = ai.make_llm("mistral", "test-key")
    assert llm.json("sys", "question", {"type": "object"}) == {"answer": "42"}
    body = calls[0]
    assert body["model"] == "ministral-14b-latest"
    assert body["response_format"]["type"] == "json_schema"
    assert body["messages"][0]["role"] == "system"


def test_mistral_falls_back_to_json_mode_and_parses_fences(mistral_calls):
    calls, queue = mistral_calls
    queue += [FakeResponse(400, {"message": "unsupported schema"}), ok('```json\n{"sql": "SELECT 1"}\n```')]
    llm = ai.MistralLLM("k")
    assert llm.json("sys", "q", {"type": "object"}) == {"sql": "SELECT 1"}
    assert calls[1]["response_format"] == {"type": "json_object"}


def test_mistral_retries_on_rate_limit(mistral_calls):
    calls, queue = mistral_calls
    queue += [FakeResponse(429, {}), ok('{"a": 1}')]
    assert ai.MistralLLM("k").json("s", "u", {}) == {"a": 1}
    assert len(calls) == 2


def test_mistral_errors_are_friendly(mistral_calls):
    calls, queue = mistral_calls
    queue.append(FakeResponse(401, {}))
    with pytest.raises(ai.AIError, match="invalid"):
        ai.MistralLLM("bad").json("s", "u", {})
    with pytest.raises(ai.AIError, match="No Mistral API key"):
        ai.MistralLLM(None)


def test_mistral_model_not_on_plan_switches_to_free_model(mistral_calls):
    calls, queue = mistral_calls
    queue += [FakeResponse(403, {"message": "This model is not available in your subscription tier",
                                 "type": "tier_not_allowed"}), ok('{"ok": 1}')]
    assert ai.MistralLLM("k", "mistral-large-latest").json("s", "u", {}) == {"ok": 1}
    assert [c["model"] for c in calls] == ["mistral-large-latest", "ministral-14b-latest"]


def test_mistral_zero_rate_limit_fails_fast(mistral_calls):
    calls, queue = mistral_calls
    for _ in range(4):   # the requested model + the 3 free models, none allowed on this plan
        resp = FakeResponse(429, {"message": "Rate limit exceeded"})
        resp.headers = {"x-ratelimit-limit-req-minute": "0"}
        queue.append(resp)
    with pytest.raises(ai.AIError, match="not included in your Mistral plan"):
        ai.MistralLLM("k", "mistral-medium-latest").json("s", "u", {})
    assert len(calls) == 4   # one call per model, no pointless retries


def test_mistral_other_errors_include_mistral_message(mistral_calls):
    calls, queue = mistral_calls
    queue.append(FakeResponse(403, {"message": "Workspace disabled"}))
    with pytest.raises(ai.AIError, match="Workspace disabled"):
        ai.MistralLLM("k").json("s", "u", {})


def test_mistral_merges_consecutive_roles(mistral_calls):
    calls, queue = mistral_calls
    queue.append(ok('{"ok": true}'))
    msgs = [{"role": "user", "content": "context"}, {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}]
    ai.MistralLLM("k").json("s", None, {}, messages=msgs)
    roles = [m["role"] for m in calls[0]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert calls[0]["messages"][1]["content"] == "context\n\nq1"
