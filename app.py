"""AKDash — Streamlit entry point.

Accounts with personas, projects, Excel/CSV/PDF upload and cleaning, AI dashboard generation,
chat with versions, and daily updates.
"""
import json
import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

import ai
import auth
import db
import engine
import ingest
import profiler
import render
import theme

DATA_DIR = Path(os.environ.get("AKDASH_DATA", "data"))   # uploaded files; app.db location: AKDASH_DB
TYPE_ICONS = {"date": "📅 date", "integer": "🔢 integer", "number": "🔢 number", "text": "🔤 text"}
ONBOARDING = [("Create a project", "One project = one dashboard."),
              ("Upload your data", "Excel, CSV or PDF reports. Messy data is cleaned for you."),
              ("Describe the analysis", "In English or Roman Urdu."),
              ("Explore & chat", "Filter, ask questions, refine, upload daily.")]
INSTRUCTION_IDEAS = {
    "📈 Monthly trend": "Show the monthly trend of the main amounts.",
    "🏆 Top 10": "Show the top 10 and bottom 10 by value.",
    "🗺️ By region": "Break down totals by region or branch.",
    "🚨 Unusual rows": "Flag unusual, very large or negative values.",
    "🎯 Target vs actual": "Compare actual results against targets if there are targets.",
}
CHAT_IDEAS = ["Which product sold the most?", "Add a chart of sales by month", "Remove the pie chart",
              "Sabse zyada sales kis region mein hui?"]

st.set_page_config(page_title="AKDash", page_icon=str(theme.ASSETS / "logo.svg"), layout="wide")
db.init_db()
theme.inject_css()


def secret(key: str, default=None):
    """Setting from .streamlit/secrets.toml, else from an environment variable (used on Render)."""
    try:
        value = st.secrets.get(key)
    except Exception:  # no secrets.toml
        value = None
    if value in (None, ""):
        value = os.environ.get(key) or os.environ.get(key.upper())
    return value if value not in (None, "") else default


# ---------- sidebar ----------

def user_box(user: dict) -> None:
    with st.sidebar.container(border=True, key="user_box"):
        p = user.get("persona") or {}
        role = " · ".join(x for x in [p.get("job_title"), p.get("department")] if x) or "Set your persona in My profile"
        st.html(f'<div class="ak-user"><div class="ak-avatar">{theme.esc((user.get("full_name") or user["username"])[:1].upper())}'
                f'</div><div><b>{theme.esc(user.get("full_name") or user["username"])}</b>'
                f'<small>{theme.esc(role)}</small></div></div>')
        if st.button("Log out", icon=":material/logout:", width="stretch", key="logout_btn"):
            auth.logout()


def sidebar(user: dict) -> dict | None:
    projects = db.list_projects(user["id"])

    with st.sidebar.expander("➕ New project", expanded=not projects):
        with st.form("new_project", clear_on_submit=True):
            name = st.text_input("Name")
            desc = st.text_area("Description", height=70)
            mode = st.radio("Daily upload mode", ["replace", "append"], horizontal=True,
                            help="Replace: each file is the full current data. "
                                 "Append: each file adds new rows (an upload_date column is added).")
            if st.form_submit_button("Create"):
                if not name.strip():
                    st.error("Please enter a project name.")
                else:
                    st.session_state.project_id = db.create_project(name, desc, mode, user["id"])
                    st.rerun()

    if not projects:
        return None

    ids = [p["id"] for p in projects]
    current = st.session_state.get("project_id")
    if current not in ids:
        current = ids[0]
    labels = {p["id"]: p["name"] for p in projects}
    pid = st.sidebar.selectbox("Select project", ids, index=ids.index(current),
                               format_func=lambda i: labels[i])
    st.session_state.project_id = pid
    project = db.get_project(pid)

    st.sidebar.caption(project["description"] or "")
    with st.sidebar.expander(f"⚙️ Upload mode: {project['mode'].title()}"):
        mode = st.radio("Daily upload mode", ["replace", "append"], index=["replace", "append"].index(project["mode"]),
                        format_func=str.title, key=f"mode_{pid}",
                        help="Replace: each file is the full current data. "
                             "Append: each file adds new rows (an upload_date column is added).")
        if mode != project["mode"] and st.button("Save mode"):
            db.update_project(pid, mode=mode)
            st.rerun()
        st.caption("Applies to future uploads.")

    uploads = db.list_uploads(pid)
    st.sidebar.subheader("Upload history")
    if uploads:
        hist = pd.DataFrame([{"Date": u["upload_date"], "Files": ", ".join(u["filenames"]),
                              "Rows": u["row_count"]} for u in uploads])
        st.sidebar.dataframe(hist, hide_index=True, width="stretch")
    else:
        st.sidebar.caption("No uploads yet.")

    with st.sidebar.expander("🗑️ Delete project"):
        confirm = st.checkbox(f"Yes, delete '{project['name']}' and its uploaded files")
        if st.button("Delete", type="primary", disabled=not confirm):
            db.delete_project(pid)
            _remove_project_files(pid)
            st.session_state.pop("project_id", None)
            st.rerun()
    return project


def _remove_project_files(pid: int) -> None:
    import shutil
    folder = DATA_DIR / str(pid)
    if folder.exists():
        shutil.rmtree(folder, ignore_errors=True)


# ---------- upload ----------

def upload_section(project: dict, first: bool) -> None:
    pid = project["id"]
    key = f"uploader_{pid}_{st.session_state.get('uploader_nonce', 0)}"
    files = st.file_uploader("Excel, CSV or PDF reports (one or many; all sheets and PDF tables are read)",
                             type=["xlsx", "xlsm", "csv", "pdf"], accept_multiple_files=True, key=key)
    upload_day = st.date_input("Data date", value=date.today(), key=f"day_{pid}",
                               help="The date this data belongs to (shown in the upload history; "
                                    "in Append mode it fills the upload_date column).")
    if not first:
        st.caption("Mode: **Replace**, so this file replaces all current data." if project["mode"] == "replace"
                   else "Mode: **Append**, so these rows are added to the existing data.")
    if st.button("Process files" if first else "Upload and refresh dashboard", type="primary", disabled=not files):
        process_upload(project, files, upload_day)


def process_upload(project: dict, files, upload_day: date) -> None:
    pid = project["id"]
    payload = [(f.name, f.getvalue()) for f in files]
    extractor = None
    if any(name.lower().endswith(".pdf") for name, _ in payload):
        try:   # text-only PDF reports: the AI copies their figures into tables (marked "please verify")
            llm = get_llm()
            extractor = lambda name, text: ai.extract_tables_from_text(llm, name, text)  # noqa: E731
        except ai.AIError:
            extractor = None
    try:
        with st.spinner("Reading and cleaning files…"):
            result = ingest.ingest_upload(payload, text_extractor=extractor)
    except ingest.IngestError as e:
        st.error(str(e))
        return
    except ai.AIError as e:
        st.error(f"Reading the PDF text with AI failed: {e}")
        return
    except Exception as e:  # unexpected: show a friendly message, not a stack trace
        st.error(f"Something went wrong while cleaning the files: {e}")
        return

    cleaned, warnings = result.tables, result.warnings
    frames = {t.name: t.df for t in cleaned}
    info = {t.name: {"sources": t.sources, "report": t.report} for t in cleaned}
    frames, info = apply_saved_mappings(frames, info, db.get_mappings(pid))
    pending = {"payload": payload, "frames": frames, "info": info, "warnings": warnings,
               "documents": result.documents, "day": upload_day.isoformat(), "skipped": []}

    latest = db.latest_upload(pid)
    if latest:
        report = ingest.compare_schemas({t: m["columns"] for t, m in latest["tables"].items()}, frames,
                                        append=project["mode"] == "append")
        pending["report"] = report
        if ingest.has_breaking_changes(report):
            st.session_state[f"pending_{pid}"] = pending
            st.rerun()
    finalize_upload(project, pending)


def apply_saved_mappings(frames: dict, info: dict, mappings: dict) -> tuple[dict, dict]:
    """Apply table/column renames and keep the cleaning info under the renamed table names."""
    for old, new in mappings.items():
        if "." not in old and old not in frames and new in frames:
            info[old] = info.pop(new, {})
    return ingest.apply_mappings(frames, mappings), info


def mapping_section(project: dict, spec: dict | None) -> None:
    """Shown when a new upload is missing tables/columns used before: let the user map old -> new names."""
    pid = project["id"]
    pending = st.session_state[f"pending_{pid}"]
    report, frames = pending["report"], pending["frames"]
    affected = ai.affected_widgets(spec, report)
    with st.container(border=True):
        st.warning("**The new upload's columns don't match the previous upload.** "
                   "Map each old name to its new name so the saved dashboard keeps working.")
        choices: dict[str, str] = {}
        prev_tables = set((db.latest_upload(pid) or {}).get("tables", {}))
        for table in report["missing_tables"]:
            options = ["(no mapping)"] + [t for t in frames if t not in prev_tables]
            used = affected.get(table) or []
            choices[table] = st.selectbox(
                f"Table **{table}** is missing" + (f" (used by: {', '.join(used)})" if used else " (not used by "
                                                   "any chart)"), options, key=f"map_t_{pid}_{table}")
        for table, cols in report["missing_columns"].items():
            new_cols = report["new_columns"].get(table, [])
            others = [c for c in frames[table].columns if c not in new_cols]
            for col in cols:
                used = affected.get(f"{table}.{col}") or []
                choices[f"{table}.{col}"] = st.selectbox(
                    f"Column **{col}** in **{table}** is missing"
                    + (f" (used by: {', '.join(used)})" if used else " (not used by any chart)"),
                    ["(no mapping)"] + new_cols + others, key=f"map_c_{pid}_{table}_{col}")
        for table, cols in report["new_columns"].items():
            st.caption(f"New columns in {table}: {', '.join(cols)}")
        for table, changes in report["type_changes"].items():
            for col, (old_t, new_t) in changes.items():
                st.caption(f"⚠️ {table}.{col} changed type: {old_t} → {new_t}")

        c1, c2, c3 = st.columns(3)
        if c1.button("Apply mapping and finish upload", type="primary"):
            chosen = {k: v for k, v in choices.items() if v != "(no mapping)"}
            if chosen:
                db.save_mappings(pid, chosen)
            frames, info = apply_saved_mappings(frames, pending["info"], chosen)
            pending.update(frames=frames, info=info,
                           skipped=pending["skipped"] + [k for k in choices if k not in chosen])
            latest = db.latest_upload(pid)
            report = ingest.compare_schemas({t: m["columns"] for t, m in latest["tables"].items()}, frames,
                                            append=project["mode"] == "append")
            remaining = set(report["missing_tables"]) | {f"{t}.{c}" for t, cs in report["missing_columns"].items()
                                                         for c in cs}
            if remaining - set(pending["skipped"]):   # mapping a table revealed column differences: ask again
                pending["report"] = report
                st.rerun()
            finalize_upload(project, pending)
        if c2.button("Continue without mapping"):
            finalize_upload(project, pending)
        if c3.button("Cancel upload"):
            st.session_state.pop(f"pending_{pid}", None)
            st.session_state.uploader_nonce = st.session_state.get("uploader_nonce", 0) + 1
            st.rerun()


def finalize_upload(project: dict, pending: dict) -> None:
    """Save the upload (Replace or Append), then refresh the dashboard's insights from the saved spec."""
    pid = project["id"]
    day, payload, frames = pending["day"], pending["payload"], pending["frames"]
    new_rows = sum(len(df) for df in frames.values())
    previous = db.latest_upload(pid)
    previous_tables = load_upload_tables(previous["snapshot_dir"]) if previous else None

    if project["mode"] == "append":
        frames = ingest.append_tables(previous_tables, frames, day)

    project_dir = DATA_DIR / str(pid)
    project_dir.mkdir(parents=True, exist_ok=True)
    for name, data in payload:
        (project_dir / f"{day}_{Path(name).name}").write_bytes(data)

    # cleaned data is cached as one parquet file per table, in a folder per upload
    meta = {}
    for name, df in frames.items():
        base = pending["info"].get(name) or {
            "sources": (previous["tables"].get(name, {}) if previous else {}).get("sources", []),
            "report": {"notes": {}, "carried_over": True}}
        meta[name] = {"columns": {c: ingest.type_label(df[c]) for c in df.columns}, "rows": len(df), **base}
    upload_id = db.add_upload(pid, day, [n for n, _ in payload], new_rows, "", meta)
    snap_dir = project_dir / f"upload_{upload_id}"
    documents = dict(ingest.load_documents(previous["snapshot_dir"])) if previous and project["mode"] == "append" else {}
    documents.update(pending.get("documents") or {})
    try:
        ingest.save_frames(frames, snap_dir)
        if documents:
            ingest.save_documents(documents, snap_dir)
    except Exception as e:
        db.delete_upload(upload_id)
        st.error(f"Could not save the cleaned data: {e}")
        return
    db.set_upload_snapshot_dir(upload_id, str(snap_dir))
    st.session_state.pop(f"pending_{pid}", None)
    st.session_state.uploader_nonce = st.session_state.get("uploader_nonce", 0) + 1

    warnings = list(pending["warnings"])
    msg = f"Processed {len(payload)} file(s): {new_rows:,} rows."
    spec_row = db.latest_spec(pid)
    if spec_row and previous:
        try:
            llm = get_llm()
            with st.status("Refreshing insights with the new data…", expanded=True) as status:
                status.write("Running the saved dashboard queries and comparing with the last upload…")
                new_spec = ai.refresh_insights(llm, spec_row["spec"], project["instructions"] or "", frames,
                                               previous_tables, day)
                status.update(label="Insights refreshed", state="complete")
            vid = db.save_spec_version(pid, new_spec, "refresh", f"Data refreshed with upload of {day}")
            db.add_chat_message(pid, "assistant", f"🔄 Dashboard refreshed with the upload of {day}. "
                                "Insights now include what changed since the last upload.", spec_version_id=vid)
            msg += " Dashboard and insights refreshed."
        except Exception as e:  # AIError or anything unexpected: the data itself is already saved
            warnings.append(f"The dashboard shows the new data, but the insights could not be refreshed: {e}")
    st.session_state.flash = (msg, warnings)
    st.rerun()


# ---------- preview ----------

def preview_section(upload: dict) -> None:
    st.subheader(f"Data preview — upload of {upload['upload_date']}")
    try:
        frames = ingest.load_tables(upload["snapshot_dir"])
    except Exception as e:
        st.error(f"Could not load the saved data for this upload: {e}")
        return
    for name, meta in upload["tables"].items():
        df = frames.get(name)
        if df is None:
            st.warning(f"Table '{name}' is missing from the saved data.")
            continue
        rep = meta["report"]
        with st.expander(f"**{name}** — {len(df):,} rows × {df.shape[1]} columns "
                         f"(from {', '.join(meta['sources'])})", expanded=len(upload["tables"]) == 1):
            facts = []
            if rep.get("ai_extracted"):
                facts.append("⚠ extracted by AI from PDF text: please check the numbers against the report")
            if rep.get("carried_over"):
                facts.append("not in this upload, so kept from the previous upload")
            if "upload_date" in df.columns:
                facts.append(f"{df['upload_date'].nunique()} upload date(s) appended")
            if rep.get("title_rows_skipped"):
                facts.append(f"skipped {rep['title_rows_skipped']} title row(s)")
            if rep.get("total_rows_removed"):
                facts.append(f"removed {rep['total_rows_removed']} total row(s)")
            if rep.get("repeated_headers_removed"):
                facts.append(f"removed {rep['repeated_headers_removed']} repeated header row(s)")
            if rep.get("combined_from"):
                facts.append(f"combined {len(rep['combined_from'])} sheets with the same columns "
                             "(see `source_sheet`)")
            st.caption("Cleaning: " + ("; ".join(facts) if facts else "no fixes needed"))

            c1, c2 = st.columns([2, 5])
            with c1:
                types = pd.DataFrame({"Column": list(meta["columns"]),
                                      "Type": [TYPE_ICONS.get(t, t) for t in meta["columns"].values()],
                                      "Empty": [int(df[c].isna().sum()) for c in meta["columns"]]})
                st.dataframe(types, hide_index=True, width="stretch")
                for col, note in rep.get("notes", {}).items():
                    st.caption(f"⚠️ `{col}`: {note}")
            with c2:
                st.dataframe(df.head(20), hide_index=True, width="stretch",
                             column_config=render.date_formats(df))


# ---------- data + query caching ----------

@st.cache_resource(show_spinner=False, max_entries=20)
def load_upload_tables(snapshot_dir: str) -> dict[str, pd.DataFrame]:
    """Cleaned tables of one upload (shared, read-only)."""
    return ingest.load_tables(snapshot_dir)


@st.cache_data(show_spinner=False, max_entries=100)
def cached_results(snapshot_dir: str, widgets_json: str, filters_key: tuple, _filters: engine.Filters):
    tables = engine.apply_filters(load_upload_tables(snapshot_dir), _filters)
    return ai.run_spec({"widgets": json.loads(widgets_json)}, tables)


def get_llm():
    """AI client from secrets.toml / environment (provider, model, base_url, API key), told who the user is."""
    provider = (secret("provider") or "mistral").lower()
    key_name = ai.provider_key_name(provider)
    llm = ai.make_llm(provider, secret(key_name) if key_name else None, secret("model"), secret("base_url"))
    # fallback_providers = "gemini,groq" -> used in this order when the main one is rate-limited or too small.
    # Not set: every other provider that has a key is used, in the default order below.
    chain = secret("fallback_providers") or ",".join(ai.DEFAULT_FALLBACK_ORDER)
    backups = []
    for name in [p.strip().lower() for p in chain.split(",") if p.strip()]:
        key_name = ai.provider_key_name(name)
        key = secret(key_name) if key_name else None
        if name == provider or (key_name and not key):
            continue   # skip duplicates and providers without a key
        try:
            backups.append(ai.make_llm(name, key, secret(f"{name}_model")))
        except ai.AIError:
            continue
    if backups:
        llm = ai.FallbackLLM([llm] + backups)
    return ai.PersonaLLM(llm, auth.persona_text(auth.current_user()))


# ---------- sidebar filters ----------

def sidebar_filters(spec: dict, tables: dict[str, pd.DataFrame], pid: int) -> engine.Filters:
    filters = engine.Filters()
    dc = spec.get("date_column")
    cats = spec.get("filters") or []
    if not dc and not cats:
        return filters
    st.sidebar.subheader("🎛️ Filters")
    if dc and dc["table"] in tables and dc["column"] in tables[dc["table"]].columns:
        s = tables[dc["table"]][dc["column"]].dropna()
        if not s.empty:
            lo, hi = s.min().date(), s.max().date()
            picked = st.sidebar.date_input("Date range", value=(lo, hi), min_value=lo, max_value=hi,
                                           key=f"f_date_{pid}_{lo}_{hi}")
            filters.date_column = dc["column"]
            if isinstance(picked, (tuple, list)) and len(picked) == 2 and tuple(picked) != (lo, hi):
                filters.date_range = (picked[0], picked[1])
    for f in cats:
        values = set()
        for df in tables.values():
            if f["column"] in df.columns:
                values.update(df[f["column"]].dropna().astype(str).unique().tolist())
        if not values:
            continue
        chosen = st.sidebar.multiselect(f["label"], sorted(values)[:500], placeholder="All",
                                        key=f"f_cat_{pid}_{f['column']}")
        if chosen:
            filters.categories[f["column"]] = chosen
    return filters


# ---------- dashboard ----------

def _add_idea(text_key: str, pills_key: str) -> None:
    """Quick-idea chip clicked: append its sentence to the instructions box."""
    idea = INSTRUCTION_IDEAS.get(st.session_state.get(pills_key))
    if idea:
        current = st.session_state.get(text_key, "").strip()
        if idea not in current:
            st.session_state[text_key] = f"{current} {idea}".strip()
    st.session_state[pills_key] = None


def generate_section(project: dict, tables: dict, previous_tables: dict | None, has_spec: bool,
                     snapshot_dir: str) -> None:
    text_key, pills_key = f"instr_{project['id']}", f"ideas_{project['id']}"
    if text_key not in st.session_state:
        st.session_state[text_key] = project["instructions"] or ""
    with st.expander("📝 Instructions" if has_spec else "📝 Describe the analysis you want", expanded=not has_spec):
        instructions = st.text_area(
            "What analysis do you want? (English or Roman Urdu)", height=110, key=text_key,
            placeholder="e.g. Show monthly sales trend, top 10 products, sales by region, and flag unusual orders.",
        )
        st.pills("Quick ideas", list(INSTRUCTION_IDEAS), key=pills_key, on_change=_add_idea,
                 args=(text_key, pills_key), label_visibility="collapsed")
        c1, c2 = st.columns([1, 3], vertical_alignment="center")
        label = "Regenerate dashboard" if has_spec else "Generate dashboard"
        clicked = c1.button(label, type="primary", icon=":material/auto_awesome:", width="stretch")
        c2.caption("Regenerating builds a new dashboard version (you can revert)." if has_spec
                   else "Short instructions are fine: totals, trends, top/bottom 10 and outliers are always included.")
        if clicked:
            run_generation(project, tables, previous_tables, instructions, snapshot_dir)


def run_generation(project: dict, tables: dict, previous_tables: dict | None, instructions: str,
                   snapshot_dir: str) -> None:
    try:
        llm = get_llm()
        with st.status("Building your detailed dashboard… (this can take a minute)", expanded=True) as status:
            profile = cached_profile(snapshot_dir)
            spec = ai.build_dashboard(llm, tables, instructions, profile, previous_tables, progress=status.write)
            status.update(label="Dashboard ready", state="complete")
    except ai.AIError as e:
        st.error(str(e))
        return
    except Exception as e:
        st.error(f"Something went wrong while building the dashboard: {e}")
        return
    db.update_project(project["id"], instructions=instructions)
    vid = db.save_spec_version(project["id"], spec, "generate", "Generated from instructions")
    ai_note = answered_by(llm)
    db.add_chat_message(project["id"], "assistant", "📊 Built a new dashboard from your instructions.",
                        spec_version_id=vid, extra={"ai": ai_note} if ai_note else None)
    skipped = spec.pop("skipped_widgets", [])
    st.session_state.flash = (f"Dashboard generated with {len(spec['widgets'])} widgets"
                              + (f" · AI: {ai_note}" if ai_note else "") + ".",
                              [f"Left out because the data can't support them: {', '.join(skipped)}"] if skipped else [])
    st.rerun()


def answered_by(llm) -> str:
    """Which AI provider(s) answered, in order, e.g. "Mistral → Google AI Studio" (after a fallback)."""
    used = getattr(llm, "used", None)
    if used:
        return " → ".join(dict.fromkeys(used))
    return getattr(llm, "label", "")


@st.cache_data(show_spinner=False, max_entries=20)
def cached_profile(snapshot_dir: str) -> str:
    return profiler.profile_json(load_upload_tables(snapshot_dir), ingest.load_documents(snapshot_dir))


# ---------- versions ----------

def versions_bar(project: dict, spec_row: dict) -> None:
    pid = project["id"]
    versions = db.list_spec_versions(pid)
    c1, c2, _ = st.columns([2, 2, 3])
    with c1:
        if len(versions) > 1 and st.button("↩️ Revert to previous version", width="stretch",
                                           help=f"Go back to version {versions[1]['version']}"):
            do_revert(pid, versions[1]["id"])
    with c2, st.popover("🕘 Version history", width="stretch"):
        labels = {v["id"]: f"v{v['version']} · {v['source']} · {v['note'] or '—'} · "
                           f"{v['created_at'][:16].replace('T', ' ')}" for v in versions}
        chosen = st.radio("Versions (newest first)", list(labels), format_func=labels.get,
                          key=f"ver_{pid}", label_visibility="collapsed")
        st.caption("Reverting adds a new version, so no history is lost.")
        if st.button("Revert to this version", disabled=chosen == spec_row["id"]):
            do_revert(pid, chosen)


def do_revert(pid: int, version_id: int) -> None:
    old = db.get_spec_version(version_id)
    new_id = db.revert_to_version(pid, version_id)
    if new_id is None:
        st.error("That version could not be found.")
        return
    db.add_chat_message(pid, "assistant", f"↩️ Reverted the dashboard to version {old['version']}.",
                        spec_version_id=new_id)
    st.rerun()


# ---------- chat ----------

def _chat_idea(pid: int) -> None:
    st.session_state[f"chat_pending_{pid}"] = st.session_state.get(f"chat_ideas_{pid}")
    st.session_state[f"chat_ideas_{pid}"] = None


def chat_section(project: dict, spec: dict, tables: dict, snapshot_dir: str) -> None:
    pid = project["id"]
    messages = db.list_chat_messages(pid)
    with st.expander("💬 Chat with your data: ask questions or change the dashboard", expanded=True):
        box = st.container(height=min(380, 110 + 70 * len(messages)) if messages else 70, border=False)
        with box:
            if not messages:
                st.caption("Ask anything about your data, or tell me how to change the dashboard.")
            for m in messages:
                render_chat_message(m)
        st.pills("Try", CHAT_IDEAS, key=f"chat_ideas_{pid}", on_change=_chat_idea, args=(pid,),
                 label_visibility="collapsed")
        typed = st.chat_input("Ask a question or describe a change…", key=f"chat_{pid}")
    prompt = typed or st.session_state.pop(f"chat_pending_{pid}", None)
    if prompt:
        with box:
            with st.chat_message("user"):
                st.markdown(prompt)
            with st.chat_message("assistant"):
                handle_chat(project, spec, tables, snapshot_dir, prompt, messages)


def render_chat_message(m: dict) -> None:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("spec_version"):
            st.caption(f"📊 Dashboard version {m['spec_version']}")
        extra = m.get("extra") or {}
        if extra.get("rows"):
            st.dataframe(pd.DataFrame(extra["rows"], columns=extra.get("columns")), hide_index=True,
                         width="stretch")
            if extra.get("truncated"):
                st.caption(f"Showing the first {len(extra['rows'])} of {extra['truncated']} rows.")
        if extra.get("sql"):
            with st.expander("SQL"):
                st.code(extra["sql"], language="sql")
        if extra.get("ai"):
            st.caption(f"🤖 Answered by {extra['ai']}")


def handle_chat(project: dict, spec: dict, tables: dict, snapshot_dir: str, prompt: str,
                history: list[dict]) -> None:
    pid = project["id"]
    try:
        llm = get_llm()
        with st.status("Working on it…", expanded=False) as status:
            result = ai.chat_turn(llm, prompt, spec, project["instructions"] or "", history, tables,
                                  cached_profile(snapshot_dir), progress=status.write)
            status.update(label="Done", state="complete")
    except ai.AIError as e:
        st.error(str(e))
        return
    except Exception as e:
        st.error(f"Something went wrong: {e}")
        return

    db.add_chat_message(pid, "user", prompt)
    ai_note = answered_by(llm)
    if result["kind"] == "update_spec":
        vid = db.save_spec_version(pid, result["spec"], "chat", prompt)
        db.add_chat_message(pid, "assistant", result["reply"], spec_version_id=vid,
                            extra={"ai": ai_note} if ai_note else None)
    else:
        extra = {"ai": ai_note} if ai_note else {}
        if result.get("sql"):
            extra["sql"] = result["sql"]
        df = result.get("data")
        if df is not None and not df.empty:
            shown = df.head(50)
            extra["columns"] = list(shown.columns)
            extra["rows"] = json.loads(shown.to_json(orient="values", date_format="iso"))
            if len(df) > len(shown):
                extra["truncated"] = len(df)
        db.add_chat_message(pid, "assistant", result["reply"], extra=extra)
    st.rerun()


def dashboard_section(project: dict, uploads: list[dict]) -> None:
    latest = uploads[0]
    tables = load_upload_tables(latest["snapshot_dir"])
    previous = uploads[1] if len(uploads) > 1 else None
    previous_tables = load_upload_tables(previous["snapshot_dir"]) if previous else None

    spec_row = db.latest_spec(project["id"])
    generate_section(project, tables, previous_tables, spec_row is not None, latest["snapshot_dir"])
    if spec_row is None:
        return

    spec = spec_row["spec"]
    filters = sidebar_filters(spec, tables, project["id"])
    widgets_json = json.dumps(spec["widgets"], sort_keys=True)
    results = cached_results(latest["snapshot_dir"], widgets_json, filters.key(), filters)
    base = engine.Filters()
    unfiltered = cached_results(latest["snapshot_dir"], widgets_json, base.key(), base)
    prev_results = {}
    if previous:
        kpis = json.dumps([w for w in spec["widgets"] if w["type"] == "kpi"], sort_keys=True)
        prev_results = cached_results(previous["snapshot_dir"], kpis, filters.key(), filters)

    chat_section(project, spec, tables, latest["snapshot_dir"])

    active = (1 if filters.date_range else 0) + len(filters.categories)
    theme.section(spec.get("title") or "Dashboard",
                  f"version {spec_row['version']} · " + (f"{active} filter(s) active" if active else "no filters"))
    versions_bar(project, spec_row)
    render.render_dashboard(spec, results, prev_results, engine.apply_filters(tables, filters),
                            render.color_domains(spec, unfiltered))


# ---------- main ----------

def dashboard_page() -> None:
    user = auth.current_user()
    project = sidebar(user)
    if project is None:
        first = (user.get("full_name") or user["username"]).split()[0]
        theme.hero(f"Welcome, {first} 👋", "Turn Excel, CSV and PDF reports into a live, explainable dashboard.",
                   ["🧹 Auto-cleaning", "📑 PDF reports", "🤖 AI-designed", "🧮 Exact numbers via SQL",
                    "💬 Chat to refine"])
        if "flash" in st.session_state:
            st.toast(st.session_state.pop("flash")[0], icon="👋")
        theme.steps(ONBOARDING, 0)
        theme.empty_state("👈", "Start by creating a project",
                          "Open “New project” in the sidebar, give it a name, and you're ready to upload.")
        return

    uploads = db.list_uploads(project["id"])
    spec_row = db.latest_spec(project["id"])
    chips = [f"🔁 {project['mode'].title()} mode"]
    if uploads:
        latest = uploads[0]
        n_rows = sum(t.get("rows", 0) for t in latest["tables"].values())
        chips = [f"📅 Updated {latest['upload_date']}", f"🧾 {n_rows:,} rows",
                 f"🗂️ {len(latest['tables'])} table(s)", f"📤 {len(uploads)} upload(s)"] + chips
    if spec_row:
        chips.append(f"🔖 Version {spec_row['version']}")
    theme.hero(project["name"], project["description"] or "Your AI-built dashboard", chips)

    if "flash" in st.session_state:
        msg, warnings = st.session_state.pop("flash")
        st.toast(msg, icon="✅")
        for w in warnings:
            st.warning(w, icon=":material/warning:")

    if not uploads:
        theme.steps(ONBOARDING, 1)
    elif spec_row is None:
        theme.steps(ONBOARDING, 2)

    tab_dash, tab_data = st.tabs(["📊 Dashboard", "📄 Data preview"])
    with tab_dash:
        if f"pending_{project['id']}" in st.session_state:
            mapping_section(project, spec_row["spec"] if spec_row else None)
        elif not uploads:
            with st.container(border=True, key="card_first_upload"):
                st.html('<p class="ak-card-title">📤 Upload your data</p>'
                        '<p class="ak-card-sub">Excel (.xlsx), CSV or PDF reports · all sheets and PDF tables are '
                        'read · title rows, totals and messy numbers are cleaned automatically</p>')
                upload_section(project, first=True)
        else:
            with st.expander("📤 Upload today's file"):
                upload_section(project, first=False)
        if uploads:
            dashboard_section(project, uploads)
    with tab_data:
        if uploads:
            preview_section(uploads[0])
        else:
            theme.empty_state("📄", "No data yet", "Upload files in the Dashboard tab to see the cleaned tables here.")


def profile_page() -> None:
    auth.profile_page(auth.current_user())


def admin_page() -> None:
    user = auth.current_user()
    if user["role"] != "admin":
        st.error("Only administrators can manage users.")
        return
    auth.admin_page(user)


def main() -> None:
    theme.sidebar_brand()
    auth.write_cookie()
    user = auth.current_user()
    if user is None:
        st.navigation([st.Page(lambda: auth.login_page(secret("signup_code")), title="Log in", icon="🔐",
                               url_path="login")], position="hidden").run()
        return
    pages = [st.Page(dashboard_page, title="Dashboard", icon=":material/dashboard:", default=True),
             st.Page(profile_page, title="My profile", icon=":material/badge:", url_path="profile")]
    if user["role"] == "admin":
        pages.append(st.Page(admin_page, title="Users", icon=":material/group:", url_path="users"))
    nav = st.navigation(pages)
    user_box(user)
    nav.run()


main()
