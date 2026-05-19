"""
Ad-hoc Upload page.

User picks one or more sources, sees validation alerts (missing stores, dates,
files), and triggers an immediate upload with live progress.
"""
import sys
import time
from datetime import datetime
from pathlib import Path

# Make 'core' importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from core.adls import ADLSClient
from core.config import SOURCES, resolve_local_root
from core.state import append_history
from core.sync import sync_source
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
    page_title="Ad-hoc Upload — ADLS Sync Console",
    page_icon=":material/upload:",
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

# ─────────────────────────── shared client ───────────────────────────────
if "adls_client" not in st.session_state:
    st.session_state.adls_client = ADLSClient()
    st.session_state.connection_status = None

# ─────────────────────────── header ──────────────────────────────────────
st.title(":material/upload: Ad-hoc Upload")
st.caption("Push selected data sources to ADLS on demand")

# Check connection
if st.session_state.connection_status is not True:
    st.warning(
        ":material/warning: Connection not verified. "
        "Visit the **Connection** page first to test connectivity.",
        icon=":material/warning:",
    )
    if st.button("Test connection now", icon=":material/wifi:"):
        with st.spinner("Connecting..."):
            ok, _, _ = st.session_state.adls_client.test_connection()
            st.session_state.connection_status = ok
            if ok:
                st.rerun()

st.markdown("---")

# ─────────────────────────── source selection ────────────────────────────
st.subheader(":material/checklist: Step 1 · Select sources to upload")

# Multi-select with helpful info
selected_ids = []

for source in SOURCES:
    local_root = resolve_local_root(source)
    val = validate_source(source)

    with st.container(border=True):
        c1, c2, c3, c4 = st.columns([0.4, 3, 1.2, 1.4])

        with c1:
            checked = st.checkbox(
                "select",
                key=f"select_{source['id']}",
                label_visibility="collapsed",
                disabled=not val["exists"] or val["file_count"] == 0,
            )
            if checked:
                selected_ids.append(source["id"])

        with c2:
            st.markdown(f":material/{source['icon']}: **{source['name']}**")
            st.caption(
                f"`{source['local_root']}` → `{source['remote_container']}/{source['remote_prefix']}/`"
            )

        with c3:
            st.metric("Files", val["file_count"])

        with c4:
            st.metric("Size", _format_size(val["total_bytes"]))

        # Validation row — found dimensions
        if val["found"]:
            found_strs = []
            for key, items in val["found"].items():
                if items:
                    label = key.title()
                    if len(items) <= 6:
                        found_strs.append(f"**{label}:** {', '.join(map(str, items))}")
                    else:
                        found_strs.append(f"**{label}:** {len(items)} values")
            if found_strs:
                st.markdown(
                    "<small style='color: var(--text-color);'>"
                    + " &nbsp;·&nbsp; ".join(found_strs)
                    + "</small>",
                    unsafe_allow_html=True,
                )

        # Alerts
        for alert in val["alerts"]:
            st.warning(f":material/warning: {alert}", icon=":material/warning:")

# ─────────────────────────── summary + run ───────────────────────────────
st.markdown("---")
st.subheader(":material/play_arrow: Step 2 · Review and upload")

if not selected_ids:
    st.info(
        ":material/info: Select at least one source above to enable upload.",
        icon=":material/info:",
    )
else:
    # Summary
    total_files = 0
    total_bytes = 0
    for sid in selected_ids:
        source = next(s for s in SOURCES if s["id"] == sid)
        val = validate_source(source)
        total_files += val["file_count"]
        total_bytes += val["total_bytes"]

    c1, c2, c3 = st.columns(3)
    c1.metric("Sources selected", len(selected_ids))
    c2.metric("Total files", total_files)
    c3.metric("Total size", _format_size(total_bytes))

    overwrite = st.checkbox(
        ":material/refresh: Overwrite existing files in cloud",
        value=True,
        help=(
            "If unchecked, files that already exist at the target path in ADLS "
            "will fail with a conflict error. Usually leave on."
        ),
    )

    run = st.button(
        f":material/cloud_upload: Upload {total_files} files now",
        type="primary",
        disabled=(st.session_state.connection_status is not True),
        use_container_width=True,
    )

    if run:
        if st.session_state.connection_status is not True:
            st.error("Cannot run — connection not verified.")
        else:
            client = st.session_state.adls_client
            all_results = []

            for sid in selected_ids:
                source = next(s for s in SOURCES if s["id"] == sid)
                st.markdown(f"#### :material/{source['icon']}: {source['name']}")

                progress_bar = st.progress(0.0, text="Starting...")
                status_text = st.empty()

                def on_progress(done, total, current_file, success):
                    pct = done / total if total > 0 else 1.0
                    icon = "✓" if success else "✗"
                    progress_bar.progress(
                        pct, text=f"{done}/{total} files · {icon} {current_file}"
                    )

                start = time.time()
                result = sync_source(client, source, on_progress=on_progress)
                elapsed = time.time() - start

                progress_bar.empty()

                # Status summary for this source
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Total", result["total"])
                c2.metric("Uploaded", result["uploaded"])
                c3.metric(
                    "Failed",
                    result["failed"],
                    delta=None if result["failed"] == 0 else "Errors",
                    delta_color="inverse" if result["failed"] > 0 else "off",
                )
                c4.metric("Duration", f"{result['elapsed_seconds']:.1f}s")

                if result["failed"] > 0:
                    with st.expander(
                        f":material/error: {result['failed']} errors", expanded=False
                    ):
                        for err in result["errors"][:20]:
                            st.code(f"{err['file']}\n  → {err['error']}", language=None)
                        if len(result["errors"]) > 20:
                            st.caption(
                                f"... and {len(result['errors']) - 20} more errors"
                            )
                else:
                    st.success(
                        f":material/check_circle: All {result['uploaded']} files uploaded successfully",
                        icon=":material/check_circle:",
                    )

                # Record history
                append_history(
                    {
                        "timestamp": datetime.now().isoformat(),
                        "source_id": sid,
                        "source_name": source["name"],
                        "trigger": "adhoc",
                        "total": result["total"],
                        "uploaded": result["uploaded"],
                        "failed": result["failed"],
                        "elapsed_seconds": result["elapsed_seconds"],
                    }
                )

                all_results.append((source["name"], result))
                st.markdown("")

            # Overall summary
            st.markdown("---")
            total_up = sum(r["uploaded"] for _, r in all_results)
            total_fa = sum(r["failed"] for _, r in all_results)
            total_t = sum(r["total"] for _, r in all_results)

            if total_fa == 0:
                st.success(
                    f":material/celebration: **All done!** Uploaded {total_up}/{total_t} files across {len(all_results)} sources.",
                    icon=":material/celebration:",
                )
            else:
                st.warning(
                    f":material/warning: Completed with {total_fa} failures. "
                    f"Uploaded {total_up}/{total_t} files.",
                    icon=":material/warning:",
                )

            st.caption(
                ":material/info: Run history is recorded in the **Statistics** page."
            )
