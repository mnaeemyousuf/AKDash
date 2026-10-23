"""Build a compact data profile for the AI. Never includes the full data."""
from __future__ import annotations

import json

import pandas as pd

from ingest import type_label

SAMPLE_ROWS = 10
TOP_VALUES = 5
MAX_TEXT = 60


def _plain(v):
    """JSON-friendly scalar."""
    if v is None or (not isinstance(v, (list, dict)) and pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d") if v == v.normalize() else v.isoformat(sep=" ")
    if hasattr(v, "item"):  # numpy scalar
        v = v.item()
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, str) and len(v) > MAX_TEXT:
        return v[:MAX_TEXT] + "…"
    return v


def profile_column(s: pd.Series) -> dict:
    t = type_label(s)
    info = {"type": t, "nulls": int(s.isna().sum()), "distinct": int(s.nunique(dropna=True))}
    present = s.dropna()
    if present.empty:
        return info
    if t in ("integer", "number", "date"):
        info["min"] = _plain(present.min())
        info["max"] = _plain(present.max())
        if t != "date":
            info["sum"] = _plain(present.sum())
            info["mean"] = _plain(present.mean())
    else:
        top = present.astype(str).value_counts().head(TOP_VALUES)
        info["top_values"] = {_plain(k): int(v) for k, v in top.items()}
    return info


def profile_tables(tables: dict[str, pd.DataFrame]) -> dict:
    out = {}
    for name, df in tables.items():
        sample = df.head(SAMPLE_ROWS)
        out[name] = {
            "rows": int(len(df)),
            "columns": {c: profile_column(df[c]) for c in df.columns},
            "sample_rows": [{c: _plain(v) for c, v in row.items()} for row in sample.to_dict("records")],
        }
    return out


DOC_EXCERPT_CHARS = 3000


def profile_json(tables: dict[str, pd.DataFrame], documents: dict[str, str] | None = None) -> str:
    """Profile of the tables; PDF reports also add a short text excerpt each (context for the insights)."""
    profile = profile_tables(tables)
    if documents:
        profile = {"tables": profile,
                   "pdf_report_text_excerpts": {name: text[:DOC_EXCERPT_CHARS] for name, text in documents.items()}}
    return json.dumps(profile, ensure_ascii=False, default=str)
