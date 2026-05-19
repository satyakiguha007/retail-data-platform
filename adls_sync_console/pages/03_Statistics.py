"""
Statistics & alerts page.

Shows:
  - Per-source: local vs cloud file counts
  - Recent upload history (last 50)
  - Charts of files uploaded over time
  - Active validation alerts (missing stores, dates, marketplaces, files)
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import plotly.express as px
import streamlit as st

from core.adls import ADLSClient
from core.config import SOURCES
from core.state import load_history
from core.validate import validate_source


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n/(1024*1024):.1f} MB"
    return f"{n/(1024*1024*1024):.2f} GB"


# ─────────────────────────── page config ─────────────────────────────────
st.set_page_config(
    page_title="Statistics — ADLS Sync Console",
    page_icon=":material/insights:",
    layout="wide",
)

st.markdown(
    """
    <style>
      div[data-testid="stMetric"] {
        background: rgba(255,255,255,0.03);
        padding: 0.85rem 1rem;
        border-radius: 12px;
        border: 1px solid rgba(125,125,125,0.18);
      }
      div.stButton > button { border-radius: 8px; font-weight: 500; }
      h1, h2, h3 { font-weight: 500; }
    </style>
    """,
    unsafe_allow_html=True,
)

if "adls_client" not in st.session_state:
    st.session_state.adls_client = ADLSClient()
    st.session_state.connection_status = None

# ─────────────────────────── header ──────────────────────────────────────
st.title(":material/insights: Statistics & Alerts")
st.caption("Cloud sync state, upload history, and validation alerts")

# Refresh button
col_refresh, _ = st.columns([1, 5])
with col_refresh:
    if st.button(":material/refresh: Refresh from cloud", use_container_width=True):
        if "cloud_counts" in st.session_state:
            del st.session_state.cloud_counts
        st.rerun()

# ─────────────────────────── alerts ──────────────────────────────────────
st.markdown("---")
st.subheader(":material/notifications_active: Active alerts")

all_alerts = []
for source in SOURCES:
    val = validate_source(source)
    for alert in val["alerts"]:
        all_alerts.append({"source": source["name"], "alert": alert})

if not all_alerts:
    st.success(
        ":material/check_circle: No active alerts. All sources look healthy.",
        icon=":material/check_circle:",
    )
else:
    for alert in all_alerts:
        st.warning(
            f":material/warning: **{alert['source']}** — {alert['alert']}",
            icon=":material/warning:",
        )

# ─────────────────────────── source state ────────────────────────────────
st.markdown("---")
st.subheader(":material/dataset: Source state · Local vs Cloud")

# Cache cloud counts since they require network calls
if "cloud_counts" not in st.session_state:
    st.session_state.cloud_counts = None

if st.session_state.cloud_counts is None:
    if st.session_state.connection_status is True:
        with st.spinner("Listing remote files..."):
            counts = {}
            for source in SOURCES:
                counts[source["id"]] = st.session_state.adls_client.count_remote_files(
                    source["remote_container"], source["remote_prefix"]
                )
            st.session_state.cloud_counts = counts
    else:
        st.session_state.cloud_counts = {}
        st.info(
            ":material/info: Connect on the **Connection** page first to see cloud counts.",
            icon=":material/info:",
        )

table_rows = []
for source in SOURCES:
    val = validate_source(source)
    cloud_count = st.session_state.cloud_counts.get(source["id"], None)
    table_rows.append(
        {
            "Source": source["name"],
            "Category": source.get("category", "Other"),
            "Local files": val["file_count"],
            "Cloud files": cloud_count if cloud_count is not None else "—",
            "Local size": _format_size(val["total_bytes"]),
            "Status": "✓ in sync"
            if cloud_count is not None and cloud_count >= val["file_count"] and val["file_count"] > 0
            else ("⚠ behind" if cloud_count is not None and cloud_count < val["file_count"] else "—"),
        }
    )

df = pd.DataFrame(table_rows)
st.dataframe(df, use_container_width=True, hide_index=True)

# ─────────────────────────── history charts ──────────────────────────────
st.markdown("---")
st.subheader(":material/history: Upload history")

history = load_history()

if not history:
    st.info(
        ":material/info: No uploads recorded yet. Run an ad-hoc upload to see history here.",
        icon=":material/info:",
    )
else:
    history_df = pd.DataFrame(history)
    history_df["timestamp"] = pd.to_datetime(history_df["timestamp"])

    # Summary metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total runs", len(history_df))
    c2.metric("Files uploaded (lifetime)", int(history_df["uploaded"].sum()))
    c3.metric("Total failures", int(history_df["failed"].sum()))
    avg_dur = history_df["elapsed_seconds"].mean() if len(history_df) else 0
    c4.metric("Avg duration", f"{avg_dur:.1f}s")

    # Time series chart
    if len(history_df) >= 2:
        st.markdown("##### Uploads over time")
        chart_df = (
            history_df.sort_values("timestamp")
            .groupby(history_df["timestamp"].dt.date)
            .agg(uploaded=("uploaded", "sum"), failed=("failed", "sum"))
            .reset_index()
        )
        chart_df.columns = ["date", "uploaded", "failed"]
        fig = px.bar(
            chart_df,
            x="date",
            y=["uploaded", "failed"],
            barmode="stack",
            color_discrete_map={"uploaded": "#1D9E75", "failed": "#E24B4A"},
            labels={"value": "Files", "variable": "Outcome", "date": "Date"},
        )
        fig.update_layout(
            height=300,
            margin=dict(l=0, r=0, t=10, b=0),
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", yanchor="bottom", y=1, xanchor="right", x=1),
        )
        st.plotly_chart(fig, use_container_width=True)

    # Per-source breakdown
    st.markdown("##### By source")
    by_source = (
        history_df.groupby("source_name")
        .agg(
            runs=("uploaded", "size"),
            uploaded=("uploaded", "sum"),
            failed=("failed", "sum"),
            avg_seconds=("elapsed_seconds", "mean"),
        )
        .reset_index()
        .sort_values("uploaded", ascending=False)
    )
    by_source["avg_seconds"] = by_source["avg_seconds"].round(1)
    st.dataframe(by_source, use_container_width=True, hide_index=True)

    # Recent runs table
    st.markdown("##### Recent runs (last 20)")
    recent = history_df.head(20)[
        ["timestamp", "source_name", "trigger", "total", "uploaded", "failed", "elapsed_seconds"]
    ].copy()
    recent["timestamp"] = recent["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    recent["elapsed_seconds"] = recent["elapsed_seconds"].round(2)
    recent.columns = ["When", "Source", "Trigger", "Total", "Uploaded", "Failed", "Seconds"]
    st.dataframe(recent, use_container_width=True, hide_index=True)
