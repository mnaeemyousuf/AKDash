"""Look and feel: design tokens (light + dark), CSS with animations, header, KPI tiles and Plotly styling.

Colors come from a validated data-viz palette. Light and dark are each their own set of steps, and
categorical colors are assigned in a fixed order and follow the entity (never its rank).
"""
from __future__ import annotations

import base64
import html
from pathlib import Path

import streamlit as st

FONT = "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif"

TOKENS = {
    "light": {
        "surface": "#fcfcfb", "page": "#f9f9f7", "raised": "#ffffff", "ink": "#0b0b0b", "ink2": "#52514e",
        "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7", "ring": "rgba(11,11,11,0.10)",
        "accent": "#34449f", "accent_soft": "rgba(52,68,159,0.09)", "gold": "#c79a3a", "good": "#006300",
        "bad": "#d03b3b",
        "shadow": "0 1px 2px rgba(11,11,11,.04), 0 4px 16px rgba(11,11,11,.05)",
        "shadow_hover": "0 2px 4px rgba(11,11,11,.06), 0 12px 32px rgba(11,11,11,.10)",
        "hero": "linear-gradient(120deg, #121838 0%, #26338a 55%, #3c4db5 100%)",
        "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
        "sequential": ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"],
    },
    "dark": {
        "surface": "#1a1a19", "page": "#0d0d0d", "raised": "#1f1f1e", "ink": "#f0efec", "ink2": "#c3c2b7",
        "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835", "ring": "rgba(255,255,255,0.10)",
        "accent": "#8e9bf2", "accent_soft": "rgba(142,155,242,0.14)", "gold": "#f0c96b", "good": "#0ca30c",
        "bad": "#e66767",
        "shadow": "0 1px 2px rgba(0,0,0,.4), 0 4px 16px rgba(0,0,0,.35)",
        "shadow_hover": "0 2px 4px rgba(0,0,0,.5), 0 12px 32px rgba(0,0,0,.55)",
        "hero": "linear-gradient(120deg, #0a0e24 0%, #1c2563 55%, #2b3a8f 100%)",
        "series": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
        "sequential": ["#0d366b", "#104281", "#184f95", "#1c5cab", "#2a78d6", "#5598e7", "#86b6ef"],
    },
}
OTHER_GRAY = "#898781"   # categories past the 8th fold into a neutral "other" color


def mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:
        return "light"


def tokens() -> dict:
    return TOKENS[mode()]


# ---------- CSS ----------

def inject_css() -> None:
    t = tokens()
    st.html(f"""<style>
:root {{
  --surface:{t['surface']}; --raised:{t['raised']}; --ink:{t['ink']}; --ink2:{t['ink2']}; --muted:{t['muted']};
  --grid:{t['grid']}; --ring:{t['ring']}; --accent:{t['accent']}; --accent-soft:{t['accent_soft']};
  --good:{t['good']}; --bad:{t['bad']}; --shadow:{t['shadow']}; --shadow-hover:{t['shadow_hover']};
  --hero:{t['hero']}; --gold:{t['gold']}; --ease: cubic-bezier(.2,.8,.2,1);
}}

/* ---------- motion ---------- */
@keyframes fadeUp   {{ from {{ opacity:0; transform: translateY(10px); }} to {{ opacity:1; transform:none; }} }}
@keyframes fadeIn   {{ from {{ opacity:0; }} to {{ opacity:1; }} }}
@keyframes slideIn  {{ from {{ opacity:0; transform: translateX(-8px); }} to {{ opacity:1; transform:none; }} }}
@keyframes popIn    {{ from {{ opacity:0; transform: scale(.96); }} to {{ opacity:1; transform:none; }} }}
@keyframes drift    {{ 0% {{ background-position: 0% 50%; }} 50% {{ background-position: 100% 50%; }}
                       100% {{ background-position: 0% 50%; }} }}
@keyframes floaty   {{ 0%,100% {{ transform: translateY(0); }} 50% {{ transform: translateY(-6px); }} }}
@keyframes pulseDot {{ 0%,100% {{ opacity:.35; transform: scale(.85); }} 50% {{ opacity:1; transform: scale(1); }} }}

/* ---------- page ---------- */
.block-container {{ padding-top: 2.2rem; padding-bottom: 4rem; max-width: 1400px; }}
h1, h2, h3 {{ letter-spacing: -0.02em; }}
[data-testid="stMain"] [data-testid="stVerticalBlock"] {{ gap: 0.9rem; }}

/* ---------- header ---------- */
.ak-hero {{
  position: relative; overflow: hidden; border-radius: 18px; padding: 26px 28px 22px;
  background: var(--hero); background-size: 200% 200%; animation: drift 18s ease infinite, fadeIn .5s var(--ease);
  color: #fff; box-shadow: var(--shadow);
}}
.ak-hero::after {{
  content:""; position:absolute; inset:-40% -10% auto auto; width: 340px; height: 340px; border-radius: 50%;
  background: radial-gradient(circle, rgba(244,217,139,.22), transparent 65%); animation: floaty 9s ease-in-out infinite;
}}
.ak-hero::before {{   /* faint woven texture */
  content:""; position:absolute; inset:0; opacity:.07; pointer-events:none;
  background-image: repeating-linear-gradient(45deg, #fff 0 2px, transparent 2px 9px),
                    repeating-linear-gradient(-45deg, #fff 0 2px, transparent 2px 9px);
}}
.ak-hero h1, .ak-hero p {{ position: relative; z-index: 1; }}
.ak-hero .thread {{ height: 3px; width: 64px; border-radius: 3px; margin: 8px 0 10px; position: relative; z-index: 1;
  background: linear-gradient(90deg, #f4d98b, #c79a3a); animation: grow .8s var(--ease) both; }}
@keyframes grow {{ from {{ width: 0; }} to {{ width: 64px; }} }}

/* ---------- login ---------- */
.ak-login {{ text-align:center; padding: 34px 0 10px; animation: fadeUp .6s var(--ease) both; }}
.ak-login img {{ width: 76px; height: 76px; animation: popIn .6s var(--ease) both, floaty 6s ease-in-out 1s infinite;
  filter: drop-shadow(0 10px 24px rgba(52,68,159,.35)); }}
.ak-login h1 {{ margin: 14px 0 2px; font-size: 2.2rem; font-weight: 800; letter-spacing: -0.03em; }}
.ak-login h1 span {{ color: var(--gold); font-weight: 600; }}
.ak-login p {{ color: var(--ink2); margin: 0 0 18px; }}
.ak-hero h1 {{ color:#fff; margin:0; padding:0; font-size: 1.9rem; font-weight: 700; }}
.ak-hero p {{ color: rgba(255,255,255,.82); margin: 0 0 14px; font-size: .95rem; }}
.ak-chips {{ display:flex; flex-wrap: wrap; gap: 8px; position: relative; z-index: 1; }}
.ak-chip {{
  display:inline-flex; align-items:center; gap:6px; padding: 5px 11px; border-radius: 999px; font-size: .8rem;
  background: rgba(255,255,255,.14); border: 1px solid rgba(255,255,255,.22); color:#fff; backdrop-filter: blur(6px);
  animation: popIn .45s var(--ease) both;
}}
.ak-chip:nth-child(2) {{ animation-delay: .05s; }} .ak-chip:nth-child(3) {{ animation-delay: .1s; }}
.ak-chip:nth-child(4) {{ animation-delay: .15s; }} .ak-chip:nth-child(5) {{ animation-delay: .2s; }}

/* ---------- KPI tiles ---------- */
.ak-kpi {{
  position: relative; padding: 16px 18px 14px; border-radius: 14px; background: var(--raised);
  border: 1px solid var(--ring); box-shadow: var(--shadow); min-height: 118px;
  animation: fadeUp .5s var(--ease) both; animation-delay: calc(var(--i, 0) * 70ms);
  transition: transform .25s var(--ease), box-shadow .25s var(--ease);
}}
.ak-kpi:hover {{ transform: translateY(-3px); box-shadow: var(--shadow-hover); }}
.ak-kpi::before {{
  content:""; position:absolute; left: 0; top: 14px; bottom: 14px; width: 3px; border-radius: 0 3px 3px 0;
  background: linear-gradient(180deg, var(--gold), var(--accent));
}}
.ak-kpi .lbl {{ color: var(--ink2); font-size: .82rem; font-weight: 500; margin-bottom: 6px;
               white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
.ak-kpi .val {{ color: var(--ink); font-size: 1.85rem; font-weight: 650; letter-spacing: -0.02em; line-height: 1.15; }}
.ak-kpi .full {{ color: var(--muted); font-size: .75rem; margin-top: 2px; }}
.ak-kpi .delta {{ display:inline-flex; align-items:center; gap:4px; margin-top: 8px; font-size: .78rem; font-weight: 600;
                 padding: 2px 8px; border-radius: 999px; }}
.ak-kpi .delta.good {{ color: var(--good); background: color-mix(in srgb, var(--good) 12%, transparent); }}
.ak-kpi .delta.bad  {{ color: var(--bad);  background: color-mix(in srgb, var(--bad) 12%, transparent); }}
.ak-kpi .delta.flat {{ color: var(--muted); background: color-mix(in srgb, var(--muted) 12%, transparent); }}
.ak-kpi .delta span.vs {{ font-weight: 400; color: var(--muted); }}
.ak-kpi.err .val {{ color: var(--muted); }}
.ak-kpi .errmsg {{ color: var(--bad); font-size: .75rem; margin-top: 6px; }}

/* ---------- cards (bordered containers with key card_*) ---------- */
[class*="st-key-card_"] {{
  background: var(--raised); border-radius: 14px !important; box-shadow: var(--shadow);
  animation: fadeUp .55s var(--ease) both;
  transition: transform .25s var(--ease), box-shadow .25s var(--ease);
}}
[class*="st-key-card_"]:hover {{ transform: translateY(-2px); box-shadow: var(--shadow-hover); }}
[data-testid="stColumn"]:nth-child(2) [class*="st-key-card_"] {{ animation-delay: .08s; }}
.ak-card-title {{ font-weight: 600; font-size: 1rem; color: var(--ink); margin: 0; }}
.ak-card-sub {{ color: var(--muted); font-size: .8rem; margin: 2px 0 0; }}

/* ---------- tabs ---------- */
[data-testid="stTabs"] [data-baseweb="tab-list"] {{ gap: 4px; border-bottom: 1px solid var(--grid); }}
[data-testid="stTabs"] [data-baseweb="tab"] {{
  padding: 8px 14px; border-radius: 10px 10px 0 0; transition: background .2s var(--ease), color .2s var(--ease);
}}
[data-testid="stTabs"] [data-baseweb="tab"]:hover {{ background: var(--accent-soft); }}
[data-testid="stTabs"] [data-baseweb="tab"][aria-selected="true"] {{ color: var(--accent); font-weight: 600; }}
[data-testid="stTabs"] [data-baseweb="tab-panel"] {{ animation: fadeIn .35s var(--ease); padding-top: 14px; }}

/* ---------- buttons & inputs ---------- */
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {{
  background: linear-gradient(135deg, #26338a, #3c4db5) !important; border: 0 !important; color: #fff !important;
  box-shadow: 0 4px 14px color-mix(in srgb, var(--accent) 35%, transparent);
  transition: transform .2s var(--ease), box-shadow .2s var(--ease), filter .2s;
}}
[data-testid="stBaseButton-primary"]:hover, [data-testid="stBaseButton-primaryFormSubmit"]:hover {{
  transform: translateY(-1px); filter: brightness(1.12);
  box-shadow: 0 8px 22px color-mix(in srgb, var(--accent) 45%, transparent); }}
[data-testid="stBaseButton-secondary"] {{ transition: transform .2s var(--ease), border-color .2s, background .2s; }}
[data-testid="stBaseButton-secondary"]:hover {{ transform: translateY(-1px); }}
[data-testid="stFileUploaderDropzone"] {{ border-radius: 14px; border: 1.5px dashed var(--axis, var(--grid));
  transition: border-color .2s, background .2s; }}
[data-testid="stFileUploaderDropzone"]:hover {{ border-color: var(--accent); background: var(--accent-soft); }}

/* ---------- expanders ---------- */
[data-testid="stExpander"] details {{ border-radius: 14px; background: var(--raised); box-shadow: var(--shadow);
  animation: fadeUp .45s var(--ease) both; }}
[data-testid="stExpander"] summary:hover {{ color: var(--accent); }}

/* ---------- chat ---------- */
[data-testid="stChatMessage"] {{ border-radius: 14px; animation: slideIn .35s var(--ease) both; }}
[data-testid="stChatInput"] {{ border-radius: 14px; }}

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] .ak-brand {{ display:flex; align-items:center; gap:10px; margin: 0 0 6px; }}
[data-testid="stSidebar"] .ak-logo {{
  width: 34px; height: 34px; border-radius: 10px; display:grid; place-items:center; color:#fff; font-size: 18px;
  background: var(--hero); background-size: 200% 200%; animation: drift 10s ease infinite;
}}
[data-testid="stSidebar"] .ak-brand b {{ font-size: 1.05rem; }}
[data-testid="stSidebar"] .ak-brand small {{ display:block; color: var(--muted); font-size: .72rem; }}

/* ---------- signed-in user card ---------- */
.ak-user {{ display:flex; align-items:center; gap:10px; animation: fadeIn .4s var(--ease); }}
.ak-avatar {{ width: 36px; height: 36px; flex: none; border-radius: 50%; display:grid; place-items:center;
  color:#fff; font-weight:700; background: var(--hero); box-shadow: inset 0 0 0 2px var(--gold); }}
.ak-user b {{ display:block; font-size: .92rem; line-height: 1.2; }}
.ak-user small {{ display:block; color: var(--muted); font-size: .75rem; line-height: 1.3; }}

/* ---------- empty state / steps ---------- */
.ak-steps {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin: 8px 0 4px; }}
.ak-step {{
  padding: 18px; border-radius: 14px; background: var(--raised); border: 1px solid var(--ring); box-shadow: var(--shadow);
  animation: fadeUp .5s var(--ease) both; animation-delay: calc(var(--i, 0) * 90ms);
}}
.ak-step .n {{ width: 28px; height: 28px; border-radius: 50%; display:grid; place-items:center; font-weight: 700;
  font-size: .85rem; color: var(--accent); background: var(--accent-soft); margin-bottom: 10px; }}
.ak-step.on .n {{ color:#fff; background: var(--accent); }}
.ak-step.done .n {{ color:#fff; background: var(--good); }}
.ak-step b {{ display:block; color: var(--ink); margin-bottom: 4px; }}
.ak-step span {{ color: var(--ink2); font-size: .86rem; }}
.ak-empty {{ text-align:center; padding: 46px 20px 30px; animation: fadeIn .6s var(--ease); }}
.ak-empty .big {{ font-size: 54px; display:inline-block; animation: floaty 4s ease-in-out infinite; }}
.ak-empty h2 {{ margin: 10px 0 6px; }}
.ak-empty p {{ color: var(--ink2); max-width: 560px; margin: 0 auto; }}
.ak-dots span {{ display:inline-block; width: 7px; height: 7px; margin: 0 2px; border-radius: 50%; background: var(--accent);
  animation: pulseDot 1.2s ease-in-out infinite; }}
.ak-dots span:nth-child(2) {{ animation-delay: .15s; }} .ak-dots span:nth-child(3) {{ animation-delay: .3s; }}
.ak-section {{ display:flex; align-items:baseline; justify-content: space-between; gap: 12px; margin: 6px 0 -4px;
  animation: fadeIn .4s var(--ease); }}
.ak-section h3 {{ margin: 0; font-size: 1.25rem; }}
.ak-section small {{ color: var(--muted); }}

@media (prefers-reduced-motion: reduce) {{
  *, *::before, *::after {{ animation: none !important; transition: none !important; }}
}}
</style>""")


# ---------- HTML pieces ----------

def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


ASSETS = Path(__file__).parent / "assets"


def logo_data_uri(name: str = "logo.svg") -> str:
    data = (ASSETS / name).read_bytes()
    return "data:image/svg+xml;base64," + base64.b64encode(data).decode()


def sidebar_brand() -> None:
    """AKDash logo at the top of the sidebar (wordmark matches light / dark mode)."""
    full = ASSETS / ("logo_full_dark.svg" if mode() == "dark" else "logo_full.svg")
    st.logo(str(full), size="large", icon_image=str(ASSETS / "logo.svg"))


def login_header() -> str:
    return (f'<div class="ak-login"><img src="{logo_data_uri()}" alt="AKDash logo"/>'
            '<h1>AK<span>Dash</span></h1><p>AI dashboards from your Excel, CSV and PDF reports</p></div>')


def hero(title: str, subtitle: str = "", chips: list[str] | None = None) -> None:
    chip_html = "".join(f'<span class="ak-chip">{esc(c)}</span>' for c in (chips or []))
    st.html(f'<div class="ak-hero"><h1>{esc(title)}</h1><div class="thread"></div><p>{esc(subtitle) or "&nbsp;"}</p>'
            f'<div class="ak-chips">{chip_html}</div></div>')


def section(title: str, note: str = "") -> None:
    st.html(f'<div class="ak-section"><h3>{esc(title)}</h3><small>{esc(note)}</small></div>')


def steps(items: list[tuple[str, str]], current: int) -> None:
    """Numbered onboarding steps; steps before `current` are done."""
    cells = []
    for i, (title, text) in enumerate(items):
        state = "done" if i < current else ("on" if i == current else "")
        num = "✓" if i < current else str(i + 1)
        cells.append(f'<div class="ak-step {state}" style="--i:{i}"><div class="n">{num}</div>'
                     f'<b>{esc(title)}</b><span>{esc(text)}</span></div>')
    st.html(f'<div class="ak-steps">{"".join(cells)}</div>')


def empty_state(icon: str, title: str, text: str) -> None:
    st.html(f'<div class="ak-empty"><div class="big">{icon}</div><h2>{esc(title)}</h2><p>{esc(text)}</p></div>')


def kpi_tile(i: int, label: str, value: str, full: str | None = None, delta_pct: float | None = None,
             higher_is_better: bool = True, error: str | None = None) -> str:
    if error:
        return (f'<div class="ak-kpi err" style="--i:{i}"><div class="lbl" title="{esc(label)}">{esc(label)}</div>'
                f'<div class="val">—</div><div class="errmsg">⚠ {esc(error[:140])}</div></div>')
    delta = ""
    if delta_pct is not None:
        if abs(delta_pct) < 0.05:
            cls, arrow = "flat", "→"
        else:
            up = delta_pct > 0
            cls, arrow = ("good" if up == higher_is_better else "bad"), ("▲" if up else "▼")
        delta = (f'<div class="delta {cls}">{arrow} {abs(delta_pct):.1f}% '
                 f'<span class="vs">vs last upload</span></div>')
    sub = f'<div class="full">{esc(full)}</div>' if full and full != value else ""
    return (f'<div class="ak-kpi" style="--i:{i}"><div class="lbl" title="{esc(label)}">{esc(label)}</div>'
            f'<div class="val">{esc(value)}</div>{sub}{delta}</div>')


# ---------- Plotly ----------

def color_map(categories: list, t: dict | None = None) -> dict:
    """Fixed-order categorical colors keyed by category (so filtering never repaints survivors)."""
    t = t or tokens()
    return {c: (t["series"][i] if i < len(t["series"]) else OTHER_GRAY) for i, c in enumerate(categories)}


def style_figure(fig, kind: str, n_categories: int = 0):
    t = tokens()
    fig.update_layout(
        font=dict(family=FONT, size=12.5, color=t["ink2"]),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        colorway=t["series"], margin=dict(l=8, r=8, t=8, b=8), height=360,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title_text="",
                    font=dict(color=t["ink2"])),
        hoverlabel=dict(bgcolor=t["raised"], bordercolor=t["grid"], font=dict(family=FONT, color=t["ink"], size=12.5)),
        hovermode="x unified" if kind == "line" else "closest",
        transition=dict(duration=500, easing="cubic-in-out"),
        bargap=0.35, bargroupgap=0.08,
    )
    axis = dict(gridcolor=t["grid"], gridwidth=1, zeroline=False, linecolor=t["axis"], tickcolor=t["axis"],
                tickfont=dict(color=t["muted"]), title_font=dict(color=t["muted"], size=12), automargin=True)
    fig.update_xaxes(**axis, showgrid=False)
    fig.update_yaxes(**axis, showgrid=True, tickformat="~s")
    if kind in ("bar", "stacked_bar"):
        # thin bars: few categories get a wide gap so bars don't fill the slot; a 2px card-colored gap
        # separates stacked segments; only the data end (top of the stack) is rounded
        fig.update_layout(barcornerradius=4,
                          bargap=0.62 if n_categories <= 4 else 0.5 if n_categories <= 8 else 0.3)
        fig.update_traces(marker_line_width=2 if kind == "stacked_bar" else 0, marker_line_color=t["raised"],
                          selector=dict(type="bar"))
    if kind == "line":
        fig.update_traces(line=dict(width=2),
                          marker=dict(size=8, line=dict(width=2, color=t["surface"])), selector=dict(type="scatter"))
    if kind == "scatter":
        fig.update_traces(marker=dict(size=9, opacity=0.85, line=dict(width=2, color=t["surface"])),
                          selector=dict(type="scatter"))
    if kind == "pie":
        fig.update_traces(marker=dict(line=dict(color=t["surface"], width=2)), textfont=dict(family=FONT),
                          textinfo="percent", insidetextfont=dict(color="#ffffff"), sort=False)
        fig.update_layout(legend=dict(orientation="v", y=0.5, yanchor="middle", x=1.02))
    if kind == "heatmap":
        fig.update_layout(coloraxis_colorbar=dict(thickness=10, outlinewidth=0, tickfont=dict(color=t["muted"])))
        fig.update_xaxes(showgrid=False)
        fig.update_yaxes(showgrid=False, tickformat="")
    return fig
