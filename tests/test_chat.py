"""Chat turns (with a fake AI), chat history trimming, spec versions and revert."""
import copy
import json

import pytest

import ai
import db
import ingest
import profiler
import prompts
from samples import make_samples as ms
from test_dashboard import SPEC, kpi


@pytest.fixture(scope="module")
def tables(tmp_path_factory):
    folder = tmp_path_factory.mktemp("samples")
    paths = ms.build_all(folder)
    result, _ = ingest.ingest_files([(p.name, p.read_bytes()) for p in paths])
    return {t.name: t.df for t in result}


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    return db


class ScriptedLLM:
    """Returns queued responses per schema and records what it was sent."""

    def __init__(self, **responses):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.sent = []

    def json(self, system, user, schema, max_tokens=0, messages=None):
        self.sent.append({"schema": schema, "user": user, "messages": messages})
        name = {id(prompts.CHAT_SCHEMA): "chat", id(prompts.ANSWER_SCHEMA): "answer",
                id(prompts.SQL_FIX_SCHEMA): "sql_fix", id(prompts.WIDGET_SCHEMA): "widget_fix"}[id(schema)]
        return self.responses[name].pop(0)


def base_spec(tables):
    spec = ai.normalize_spec(copy.deepcopy(SPEC), tables)
    spec["insights"] = {"executive_summary": "old insights"}
    return spec


def test_history_trimmed_to_last_10():
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(15)]
    msgs = ai.chat_messages("CONTEXT", history, "new question")
    assert msgs[0] == {"role": "user", "content": "CONTEXT"}
    assert [m["content"] for m in msgs[1:-1]] == [f"m{i}" for i in range(5, 15)]
    assert msgs[-1] == {"role": "user", "content": "new question"}


def test_chat_update_spec_removes_and_adds_widgets(tables):
    spec = base_spec(tables)
    updated = ai.spec_for_ai(spec)
    updated["widgets"] = [w for w in updated["widgets"] if w["id"] != "orders"]
    updated["widgets"].append(kpi("max_order", "SELECT MAX(amount_typo) AS value FROM sales_report__sales_data"))
    llm = ScriptedLLM(
        chat=[{"action": "update_spec", "reply": "Removed orders, added max order.", "spec": updated, "sql": None}],
        widget_fix=[kpi("max_order", "SELECT MAX(amount_rs) AS value FROM sales_report__sales_data")],
    )
    out = ai.chat_turn(llm, "remove orders kpi, add max order", spec, "sales", [], tables,
                       profiler.profile_json(tables))
    assert out["kind"] == "update_spec"
    ids = [w["id"] for w in out["spec"]["widgets"]]
    assert "orders" not in ids and "max_order" in ids
    fixed = next(w for w in out["spec"]["widgets"] if w["id"] == "max_order")
    assert "error" not in fixed
    assert out["spec"]["insights"] == {"executive_summary": "old insights"}   # kept
    # the spec sent to the AI has no insights
    assert "old insights" not in out_messages_text(llm)


def out_messages_text(llm):
    return json.dumps([s["messages"] for s in llm.sent if s["messages"]])


def test_chat_query_runs_sql_and_passes_real_numbers(tables):
    sql = ("SELECT product, SUM(amount_rs) AS sales FROM sales_report__sales_data "
           "WHERE month(order_date) = 3 GROUP BY 1 ORDER BY 2 DESC LIMIT 1")
    llm = ScriptedLLM(
        chat=[{"action": "query", "reply": "Looking it up", "spec": None, "sql": sql}],
        answer=[{"answer": "Rice sold the most."}],
    )
    out = ai.chat_turn(llm, "which product sold most in march?", base_spec(tables), "", [], tables,
                       profiler.profile_json(tables))
    assert out["kind"] == "query"
    assert out["reply"] == "Rice sold the most."
    sales = tables["sales_report__sales_data"]
    march = sales[sales["order_date"].dt.month == 3].groupby("product")["amount_rs"].sum()
    assert out["data"]["product"].iloc[0] == march.idxmax()
    assert out["data"]["sales"].iloc[0] == march.max()
    # the answer prompt contained the computed number
    assert str(int(march.max())) in llm.sent[-1]["user"]


def test_chat_query_fixes_failing_sql(tables):
    llm = ScriptedLLM(
        chat=[{"action": "query", "reply": "", "spec": None, "sql": "SELECT SUM(amt) AS total FROM expenses"}],
        sql_fix=[{"sql": "SELECT SUM(amount) AS total FROM expenses"}],
        answer=[{"answer": "ok"}],
    )
    out = ai.chat_turn(llm, "total expenses?", base_spec(tables), "", [], tables, profiler.profile_json(tables))
    assert out["data"]["total"].iloc[0] == tables["expenses"]["amount"].sum()


def test_chat_query_rejects_non_select(tables):
    llm = ScriptedLLM(
        chat=[{"action": "query", "reply": "", "spec": None, "sql": "DROP TABLE expenses"}],
        sql_fix=[{"sql": "DELETE FROM expenses"}, {"sql": "DROP TABLE expenses"}],
    )
    out = ai.chat_turn(llm, "drop it", base_spec(tables), "", [], tables, profiler.profile_json(tables))
    assert out["data"] is None
    assert "couldn't calculate" in out["reply"]
    assert len(tables["expenses"]) == 40


def test_chat_plain_answer(tables):
    llm = ScriptedLLM(chat=[{"action": "answer", "reply": "Hello!", "spec": None, "sql": None}])
    out = ai.chat_turn(llm, "hi", base_spec(tables), "", [], tables, profiler.profile_json(tables))
    assert out == {"kind": "answer", "reply": "Hello!", "spec": None, "sql": None, "data": None}


def test_versions_and_revert(temp_db):
    pid = db.create_project("P")
    v1 = db.save_spec_version(pid, {"title": "one", "widgets": []}, "generate", "first")
    db.save_spec_version(pid, {"title": "two", "widgets": []}, "chat", "add chart")
    assert db.latest_spec(pid)["spec"]["title"] == "two"

    new_id = db.revert_to_version(pid, v1)
    latest = db.latest_spec(pid)
    assert latest["id"] == new_id and latest["version"] == 3
    assert latest["spec"]["title"] == "one"
    assert [v["version"] for v in db.list_spec_versions(pid)] == [3, 2, 1]   # nothing deleted

    other = db.create_project("Other")
    assert db.revert_to_version(other, v1) is None                         # can't revert across projects


def test_chat_messages_persist(temp_db):
    pid = db.create_project("P")
    vid = db.save_spec_version(pid, {"widgets": []}, "chat", "x")
    db.add_chat_message(pid, "user", "add chart")
    db.add_chat_message(pid, "assistant", "done", spec_version_id=vid)
    db.add_chat_message(pid, "assistant", "answer", extra={"sql": "SELECT 1", "columns": ["a"], "rows": [[1]]})
    msgs = db.list_chat_messages(pid)
    assert [m["content"] for m in msgs] == ["add chart", "done", "answer"]
    assert msgs[1]["spec_version"] == 1
    assert msgs[2]["extra"]["rows"] == [[1]]


def test_old_database_is_migrated(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE spec_versions (id INTEGER PRIMARY KEY, project_id INTEGER, version INTEGER,
                                    spec_json TEXT, source TEXT, created_at TEXT);
        CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, project_id INTEGER, role TEXT, content TEXT,
                                    spec_version_id INTEGER, created_at TEXT);
    """)
    con.close()
    monkeypatch.setattr(db, "DB_PATH", str(path))
    db.init_db()
    pid = db.create_project("P")
    db.save_spec_version(pid, {"widgets": []}, "generate", "note works")
    assert db.list_spec_versions(pid)[0]["note"] == "note works"
