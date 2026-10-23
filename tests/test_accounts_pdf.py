"""Accounts (hashing, register/login, ownership), persona in prompts, PDF parsing, and detailed dashboards."""
import copy

import pandas as pd
import pytest

import ai
import auth
import db
import ingest
import profiler
from samples import make_samples as ms
from test_dashboard import SPEC, kpi


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    return db


# ---------- accounts ----------

def test_password_hashing():
    h = auth.hash_password("secret123")
    assert h.startswith("pbkdf2_sha256$") and "secret123" not in h
    assert auth.verify_password("secret123", h)
    assert not auth.verify_password("Secret123", h)
    assert auth.hash_password("secret123") != h          # salted


def test_register_validation(temp_db):
    with pytest.raises(auth.AuthError, match="Username"):
        auth.register("a", "secret123", "secret123")
    with pytest.raises(auth.AuthError, match="at least"):
        auth.register("naeem", "short1", "short1")
    with pytest.raises(auth.AuthError, match="letters and numbers"):
        auth.register("naeem", "onlyletters", "onlyletters")
    with pytest.raises(auth.AuthError, match="match"):
        auth.register("naeem", "secret123", "secret124")
    with pytest.raises(auth.AuthError, match="sign-up code"):
        auth.register("naeem", "secret123", "secret123", signup_code="AK2026", entered_code="wrong")


def test_first_user_is_admin_and_adopts_old_projects(temp_db):
    old = db.create_project("Made before login existed")
    first = auth.register("admin", "secret123", "secret123", full_name="Admin")
    second = auth.register("Sara", "secret123", "secret123")
    assert db.get_user(first)["role"] == "admin"
    assert db.get_user(second)["role"] == "user"
    assert [p["id"] for p in db.list_projects(first)] == [old]
    assert db.list_projects(second) == []
    with pytest.raises(auth.AuthError, match="taken"):
        auth.register("sara", "secret123", "secret123")    # usernames are case-insensitive


def test_login_and_disabled_account(temp_db):
    uid = auth.register("naeem", "secret123", "secret123")
    assert auth.authenticate("naeem", "secret123")["id"] == uid
    assert db.get_user(uid)["last_login"]
    with pytest.raises(auth.AuthError, match="Wrong"):
        auth.authenticate("naeem", "nope12345")
    with pytest.raises(auth.AuthError, match="Wrong"):
        auth.authenticate("ghost", "secret123")
    db.update_user(uid, is_active=0)
    with pytest.raises(auth.AuthError, match="disabled"):
        auth.authenticate("naeem", "secret123")


def test_change_password(temp_db):
    uid = auth.register("naeem", "secret123", "secret123")
    user = db.get_user(uid)
    with pytest.raises(auth.AuthError, match="current password"):
        auth.change_password(user, "wrong", "newpass123", "newpass123")
    auth.change_password(user, "secret123", "newpass123", "newpass123")
    assert auth.authenticate("naeem", "newpass123")


def test_projects_are_private(temp_db):
    a = auth.register("aaa", "secret123", "secret123")
    b = auth.register("bbb", "secret123", "secret123")
    pa = db.create_project("A's", user_id=a)
    db.create_project("B's", user_id=b)
    assert [p["id"] for p in db.list_projects(a)] == [pa]


# ---------- persona ----------

def test_persona_text_and_prompt(temp_db):
    uid = auth.register("naeem", "secret123", "secret123", full_name="Muhammad Naeem")
    db.update_user(uid, persona={"job_title": "Regional Sales Manager", "department": "Sales",
                                 "organization": "Alkaram Textiles", "kpis": "sell-through %",
                                 "language": "Roman Urdu", "detail": "Deep-dive (most detail)"})
    text = auth.persona_text(db.get_user(uid))
    assert "Muhammad Naeem (Regional Sales Manager, Sales, Alkaram Textiles)" in text
    assert "sell-through %" in text and "Roman Urdu" in text

    class Recorder:
        system = None

        def json(self, system, user, schema, max_tokens=0, messages=None):
            Recorder.system = system
            return {}

    ai.PersonaLLM(Recorder(), text).json("BASE PROMPT", "u", {})
    assert Recorder.system.startswith("BASE PROMPT") and "Regional Sales Manager" in Recorder.system
    ai.PersonaLLM(Recorder(), "").json("BASE PROMPT", "u", {})
    assert Recorder.system == "BASE PROMPT"


# ---------- PDF ----------

@pytest.fixture(scope="module")
def pdfs(tmp_path_factory):
    return ms.build_pdfs(tmp_path_factory.mktemp("pdf"))


def test_pdf_table_across_pages_is_joined_and_cleaned(pdfs):
    path = pdfs[0]
    result = ingest.ingest_upload([(path.name, path.read_bytes())])
    assert [t.name for t in result.tables] == ["store_sales_q3"]
    t = result.tables[0]
    rows = ms.store_sales_rows()
    assert len(t.df) == len(rows)                                  # 2 pages joined, total row removed
    assert t.df["net_sales_rs"].sum() == sum(r[5] for r in rows)
    assert t.column_types["month"] == "date"                       # "2026-07" -> 2026-07-01
    assert "Retail Store Sales Report" in result.documents[path.name]


def test_text_only_pdf_uses_ai_extraction(pdfs):
    path = pdfs[1]
    with pytest.raises(ingest.IngestError, match="No tables"):
        ingest.ingest_upload([(path.name, path.read_bytes())])

    seen = {}

    def extractor(name, text):
        seen["text"] = text
        return [("channel_sales", [["Channel", "Sales"], ["Retail stores", "Rs 48,500,000"],
                                   ["E-commerce", "Rs 12,300,000"]])]

    result = ingest.ingest_upload([(path.name, path.read_bytes())], text_extractor=extractor)
    assert "48,500,000" in seen["text"]
    t = result.tables[0]
    assert t.report["ai_extracted"] and t.df["sales"].sum() == 60_800_000


def test_extract_tables_from_text_pads_rows():
    class LLM:
        def json(self, system, user, schema, max_tokens=0, messages=None):
            return {"tables": [{"name": "Returns Rate", "columns": ["Channel", "Rate"],
                                "rows": [["Retail", "3.1%"], ["Export"]]}]}

    out = ai.extract_tables_from_text(LLM(), "r.pdf", "text")
    assert out == [("returns_rate", [["Channel", "Rate"], ["Retail", "3.1%"], ["Export", None]])]


def test_profile_includes_pdf_excerpt():
    tables = {"t": pd.DataFrame({"a": [1, 2]})}
    import json
    prof = json.loads(profiler.profile_json(tables, {"r.pdf": "x" * 5000}))
    assert prof["tables"]["t"]["rows"] == 2
    assert len(prof["pdf_report_text_excerpts"]["r.pdf"]) == profiler.DOC_EXCERPT_CHARS
    assert "t" in json.loads(profiler.profile_json(tables))      # no PDFs: same shape as before


# ---------- detailed dashboards ----------

def test_thin_dashboard_gets_expanded(tmp_path):
    paths = ms.build_all(tmp_path)
    cleaned, _ = ingest.ingest_files([(p.name, p.read_bytes()) for p in paths])
    tables = {t.name: t.df for t in cleaned}
    spec = ai.normalize_spec(copy.deepcopy(SPEC), tables)
    assert ai.detail_gaps(spec, tables)                               # 7 widgets: too thin

    extra = [kpi(f"k{i}", "SELECT COUNT(*) AS value FROM expenses", "integer") for i in range(3)]
    extra.append(dict(kpi("dup", "SELECT 1 AS value"), id="total_sales"))   # duplicate id gets renamed

    class LLM:
        def json(self, system, user, schema, max_tokens=0, messages=None):
            assert schema is ai.prompts.EXPAND_SCHEMA and "Overview needs" in user
            return {"widgets": extra}

    out = ai.expand_if_thin(LLM(), spec, "{}", "", tables)
    ids = [w["id"] for w in out["widgets"]]
    assert len(ids) == len(spec["widgets"]) + 4 and len(set(ids)) == len(ids)


def test_expansion_failure_keeps_dashboard(tmp_path):
    tables = {"t": pd.DataFrame({"a": [1]})}
    spec = {"title": "x", "date_column": None, "filters": [],
            "widgets": [kpi("a", "SELECT 1 AS value")]}

    class LLM:
        def json(self, *a, **k):
            raise ai.AIError("rate limited")

    assert ai.expand_if_thin(LLM(), spec, "{}", "", tables) == spec
