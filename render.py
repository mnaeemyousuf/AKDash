"""Turn a Dashboard Spec plus computed results into Streamlit / Plotly widgets."""
from __future__ import annotations

import io

import pandas as pd
import plotly.express as px
import streamlit as st

import theme

DASHBOARD_TABS = ["Overview", "Trends", "Breakdown", "Exceptions", "Insights", "Data"]
KPI_PER_ROW = 4
TABLE_MAX_ROWS = 500


# ---------- formatting ----------

def fmt_value(v, fmt: str = "number", unit: str | None = None) -> str:
    if v is None or pd.isna(v):
        return "—"
    v = float(v)
    if fmt == "percent":
        text = f"{v:,.1f}%"
    elif fmt == "integer" or (fmt == "currency" and abs(v) >= 100) or (fmt == "number" and v.is_integer()):
        text = f"{v:,.0f}"
    else:
        text = f"{v:,.2f}"
    if unit and fmt != "percent":
        text = f"{unit} {text}" if fmt == "currency" else f"{text} {unit}"
    return text


def fmt_compact(v, fmt: str = "number", unit: str | None = None) -> str:
    """Short form for KPI cards (943.2K, 1.25M); falls back to fmt_value for small numbers."""
    if v is None or pd.isna(v) or fmt == "percent" or abs(float(v)) < 100_000:
        return fmt_value(v, fmt, unit)
    v = float(v)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            text = f"{v / div:,.2f}".rstrip("0").rstrip(".") + suffix
            break
    if unit:
        text = f"{unit} {text}" if fmt == "currency" else f"{text} {unit}"
    return text


def to_excel(df: pd.DataFrame, sheet: str = "data") -> bytes:
    buf = io.BytesIO()
    out = df.copy()
    for c in out.columns:  # Excel cannot store timezone-aware dates
        if isinstance(out[c].dtype, pd.DatetimeTZDtype):
            out[c] = out[c].dt.tz_localize(None)
    with pd.ExcelWriter(buf, engine="xlsxwriter") as xw:
        out.to_excel(xw, sheet_name=sheet[:31] or "data", index=False)
    return buf.getvalue()


def date_formats(df: pd.DataFrame) -> dict:
    """Show dates without 00:00:00 when a column has no time part."""
    config = {}
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]):
            s = df[c].dropna()
            fmt = "YYYY-MM-DD" if (s == s.dt.normalize()).all() else "YYYY-MM-DD HH:mm"
            config[c] = st.column_config.DatetimeColumn(format=fmt)
    return config


# ---------- widgets ----------

def _how_calculated(w: dict) -> None:
    with st.expander("How was this calculated?", icon=":material/code:"):
        if w.get("description"):
            st.caption(w["description"])
        st.code(w["sql"], language="sql")


def _card_header(w: dict) -> None:
    sub = f'<p class="ak-card-sub">{theme.esc(w["description"])}</p>' if w.get("description") else ""
    st.html(f'<p class="ak-card-title">{theme.esc(w["title"])}</p>{sub}')


def kpi_delta(value, prev_df) -> float | None:
    """% change of a KPI vs the previous upload, or None."""
    if prev_df is None or pd.isna(value):
        return None
    before = prev_df["value"].iloc[0]
    if pd.isna(before) or float(before) == 0:
        return None
    return (float(value) - float(before)) / abs(float(before)) * 100


def render_kpis(widgets: list[dict], results: dict, prev_results: dict) -> None:
    for start in range(0, len(widgets), KPI_PER_ROW):
        cols = st.columns(KPI_PER_ROW)
        for col, (i, w) in zip(cols, enumerate(widgets[start:start + KPI_PER_ROW], start=start)):
            with col:
                df, err = results.get(w["id"], (None, w.get("error")))
                if df is None:
                    st.html(theme.kpi_tile(i, w["title"], "—", error=err or "Not calculated"))
                    continue
                value = df["value"].iloc[0]
                fmt, unit = w.get("format", "number"), w.get("unit")
                prev_df, _ = prev_results.get(w["id"], (None, None))
                st.html(theme.kpi_tile(i, w["title"], fmt_compact(value, fmt, unit), fmt_value(value, fmt, unit),
                                       kpi_delta(value, prev_df), w.get("higher_is_better", True)))
                with st.popover("SQL", icon=":material/code:", width="content"):
                    if w.get("description"):
                        st.caption(w["description"])
                    st.code(w["sql"], language="sql")


def make_figure(w: dict, df: pd.DataFrame, domain: list | None = None):
    """Plotly figure for a widget. `domain` is the category order from the unfiltered data, so a category
    keeps its color when filters remove others."""
    t, x, y, color = w["type"], w.get("x"), list(w.get("y") or []), w.get("color")
    tk = theme.tokens()
    y_arg = y[0] if len(y) == 1 else y
    key_col = x if t == "pie" else color
    cmap = None
    if key_col and key_col in df.columns:
        extra = sorted((v for v in df[key_col].dropna().unique() if v not in (domain or [])), key=str)
        order = list(domain or []) + extra
        cmap = theme.color_map(order, tk)
    common = (dict(color_discrete_map=cmap, category_orders={key_col: order}) if cmap
              else dict(color_discrete_sequence=tk["series"]))
    if t in ("bar", "stacked_bar"):
        fig = px.bar(df, x=x, y=y_arg, color=color, barmode="stack" if t == "stacked_bar" else "group", **common)
    elif t == "line":
        fig = px.line(df, x=x, y=y_arg, color=color, markers=True, **common)
    elif t == "pie":
        fig = px.pie(df, names=x, values=y[0], hole=0.55, color=x if cmap else None, **common)
    elif t == "scatter":
        fig = px.scatter(df, x=x, y=y[0], color=color, **common)
    elif t == "heatmap":
        pivot = df.pivot_table(index=y[0], columns=x, values=w.get("value"), aggfunc="sum")
        # low values recede toward the surface, high values stand out (each mode's ramp is ordered that way)
        fig = px.imshow(pivot, text_auto=".3~s", aspect="auto", color_continuous_scale=tk["sequential"])
    else:
        raise ValueError(f"Unknown chart type {t}")
    if len(y) == 1 and not color and t not in ("pie", "heatmap"):
        fig.update_layout(showlegend=False)   # one series: the title names it, no legend box
    n_categories = df[x].nunique() if x in df.columns else 0
    return theme.style_figure(fig, t, n_categories)


def render_chart(w: dict, df: pd.DataFrame | None, err: str | None, domain: list | None = None) -> None:
    with st.container(border=True, key=f"card_{w['id']}"):
        _card_header(w)
        if df is None:
            st.warning(f"This chart could not be calculated: {err}", icon=":material/error:")
        elif df.empty:
            st.info("No data for the current filters.", icon=":material/filter_alt_off:")
        else:
            try:
                st.plotly_chart(make_figure(w, df, domain), width="stretch", key=f"chart_{w['id']}", theme=None,
                                config={"displayModeBar": False})
            except Exception as e:
                st.warning(f"This chart could not be drawn: {e}", icon=":material/error:")
        _how_calculated(w)


def render_table(w: dict, df: pd.DataFrame | None, err: str | None) -> None:
    with st.container(border=True, key=f"card_{w['id']}"):
        _card_header(w)
        if df is None:
            st.warning(f"This table could not be calculated: {err}", icon=":material/error:")
        elif df.empty:
            st.info("No rows for the current filters.", icon=":material/filter_alt_off:")
        else:
            shown = df.head(TABLE_MAX_ROWS)
            st.dataframe(shown, hide_index=True, width="stretch", column_config=date_formats(shown))
            c1, c2 = st.columns([1, 3], vertical_alignment="center")
            c1.download_button("Download Excel", to_excel(df, w["id"]), file_name=f"{w['id']}.xlsx",
                               key=f"dl_{w['id']}", icon=":material/download:")
            c2.caption(f"{len(df):,} rows" + (f" · showing first {TABLE_MAX_ROWS}" if len(df) > TABLE_MAX_ROWS else ""))
        _how_calculated(w)


def render_widgets(widgets: list[dict], results: dict, prev_results: dict, domains: dict) -> None:
    kpis = [w for w in widgets if w["type"] == "kpi"]
    charts = [w for w in widgets if w["type"] not in ("kpi", "table")]
    tables = [w for w in widgets if w["type"] == "table"]
    if kpis:
        render_kpis(kpis, results, prev_results)
    for start in range(0, len(charts), 2):
        cols = st.columns(2)
        for col, w in zip(cols, charts[start:start + 2]):
            with col:
                df, err = results.get(w["id"], (None, w.get("error")))
                render_chart(w, df, err, domains.get(w["id"]))
    for w in tables:
        df, err = results.get(w["id"], (None, w.get("error")))
        render_table(w, df, err)
    if not widgets:
        theme.empty_state("🗂️", "Nothing here yet", 'Ask in the chat, e.g. "add a chart of sales by month".')


INSIGHT_SECTIONS = [("🔄", "What changed since the last upload", "what_changed"), ("💡", "Key findings", "key_findings"),
                    ("⚠️", "Anomalies", "anomalies"), ("✅", "Recommended actions", "recommended_actions")]


def render_insights(insights: dict | None) -> None:
    if not insights:
        theme.empty_state("💡", "No insights yet", "Insights are written after the dashboard is generated.")
        return
    with st.container(border=True, key="card_ins_summary"):
        st.html('<p class="ak-card-title">📋 Executive summary</p>'
                '<p class="ak-card-sub">Written by AI from the computed results only</p>')
        st.markdown(insights.get("executive_summary", ""))
    present = [(icon, title, insights.get(key) or []) for icon, title, key in INSIGHT_SECTIONS
               if insights.get(key)]
    for start in range(0, len(present), 2):
        cols = st.columns(2)
        for col, (icon, title, items) in zip(cols, present[start:start + 2]):
            with col, st.container(border=True, key=f"card_ins_{title[:12].replace(' ', '_')}"):
                st.html(f'<p class="ak-card-title">{icon} {theme.esc(title)}</p>')
                st.markdown("\n".join(f"- {i}" for i in items))


def render_data_tab(tables: dict[str, pd.DataFrame]) -> None:
    if not tables:
        return
    with st.container(border=True, key="card_data_tab"):
        c1, c2 = st.columns([2, 3], vertical_alignment="bottom")
        name = c1.selectbox("Table", list(tables), key="data_tab_table")
        df = tables[name]
        c2.caption(f"{len(df):,} rows × {df.shape[1]} columns after filters")
        st.dataframe(df.head(TABLE_MAX_ROWS * 4), hide_index=True, width="stretch", column_config=date_formats(df))
        st.download_button("Download Excel", to_excel(df, name), file_name=f"{name}.xlsx", key="dl_data_tab",
                           icon=":material/download:")


def render_dashboard(spec: dict, results: dict, prev_results: dict, tables: dict[str, pd.DataFrame],
                     domains: dict | None = None) -> None:
    labels = {"Overview": "🏠 Overview", "Trends": "📈 Trends", "Breakdown": "🧩 Breakdown",
              "Exceptions": "🚨 Exceptions", "Insights": "💡 Insights", "Data": "🗃️ Data"}
    tabs = st.tabs([labels[n] for n in DASHBOARD_TABS])
    for tab, name in zip(tabs, DASHBOARD_TABS):
        with tab:
            if name == "Insights":
                render_insights(spec.get("insights"))
            elif name == "Data":
                render_data_tab(tables)
            else:
                render_widgets([w for w in spec["widgets"] if w["tab"] == name], results, prev_results,
                               domains or {})


def color_domains(spec: dict, base_results: dict) -> dict:
    """Category order per widget from the unfiltered results (keeps colors stable under filters)."""
    out = {}
    for w in spec.get("widgets", []):
        col = w.get("x") if w["type"] == "pie" else w.get("color")
        df, _ = base_results.get(w["id"], (None, None))
        if col and df is not None and col in df.columns:
            # sorted, so a category gets the same color on every run (SQL row order is not guaranteed)
            out[w["id"]] = sorted(df[col].dropna().unique().tolist(), key=str)
    return out
