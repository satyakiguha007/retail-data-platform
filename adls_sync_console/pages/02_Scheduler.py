"""
Scheduler page.

Add recurring sync jobs that run automatically in the background. Each job
binds a source to a frequency (every N minutes). View active jobs, pause,
resume, or remove them.
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import streamlit as st

from core.adls import ADLSClient
from core.config import SOURCES, get_source
from core.scheduler import JobScheduler


# Singleton scheduler (cached so it survives Streamlit reruns)
@st.cache_resource
def get_scheduler() -> JobScheduler:
    return JobScheduler()


# ─────────────────────────── page config ─────────────────────────────────
st.set_page_config(
    page_title="Scheduler — ADLS Sync Console",
    page_icon=":material/schedule:",
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

scheduler = get_scheduler()

# ─────────────────────────── header ──────────────────────────────────────
st.title(":material/schedule: Scheduler")
st.caption("Set up recurring background sync to ADLS")

if st.session_state.connection_status is not True:
    st.warning(
        ":material/warning: Connection not verified. The scheduler will attempt to "
        "connect when it runs, but you should verify on the **Connection** page first.",
        icon=":material/warning:",
    )

# ─────────────────────────── active jobs ─────────────────────────────────
st.markdown("---")
st.subheader(":material/list_alt: Active jobs")

jobs = scheduler.list_jobs()

if not jobs:
    st.info(
        ":material/info: No scheduled jobs yet. Add one below.",
        icon=":material/info:",
    )
else:
    for job in jobs:
        source = get_source(job["source_id"])
        source_name = source["name"] if source else f"(unknown: {job['source_id']})"
        icon = source["icon"] if source else "help"

        with st.container(border=True):
            c1, c2, c3, c4, c5 = st.columns([3, 1.5, 1.5, 1.2, 0.8])

            with c1:
                st.markdown(f":material/{icon}: **{source_name}**")
                st.caption(f"Job ID: `{job['job_id']}`")

            with c2:
                st.metric("Every", f"{job['frequency_minutes']} min")

            with c3:
                if job.get("next_run"):
                    next_run = datetime.fromisoformat(job["next_run"])
                    delta = next_run - datetime.now(next_run.tzinfo)
                    delta_secs = delta.total_seconds()
                    if delta_secs < 60:
                        next_str = f"in {int(delta_secs)}s"
                    elif delta_secs < 3600:
                        next_str = f"in {int(delta_secs/60)} min"
                    else:
                        next_str = next_run.strftime("%H:%M")
                else:
                    next_str = "paused"
                st.metric("Next run", next_str)

            with c4:
                active = st.toggle(
                    "Active",
                    value=job.get("active", True),
                    key=f"toggle_{job['job_id']}",
                )
                if active != job.get("active", True):
                    scheduler.toggle_job(job["job_id"], active)
                    st.rerun()

            with c5:
                if st.button(
                    ":material/delete:",
                    key=f"del_{job['job_id']}",
                    help="Remove this job",
                ):
                    scheduler.remove_job(job["job_id"])
                    st.rerun()

# ─────────────────────────── add new job ─────────────────────────────────
st.markdown("---")
st.subheader(":material/add_circle: Add a new scheduled job")

with st.container(border=True):
    c1, c2 = st.columns(2)

    with c1:
        source_options = {s["id"]: f"{s['name']}" for s in SOURCES}
        new_source_id = st.selectbox(
            "Source",
            options=list(source_options.keys()),
            format_func=lambda k: source_options[k],
            help="Which data source to sync.",
        )

    with c2:
        frequency_minutes = st.select_slider(
            "Run every",
            options=[5, 15, 30, 60, 120, 240, 360, 720, 1440],
            value=60,
            format_func=lambda m: (
                f"{m} min" if m < 60 else f"{m // 60}h" if m < 1440 else "24h (daily)"
            ),
            help="How often to run this sync. Shorter intervals = more cloud writes.",
        )

    st.caption(
        f":material/info: This will sync **{source_options[new_source_id]}** every "
        f"**{frequency_minutes} minutes**, starting immediately."
    )

    if st.button(
        ":material/add: Add job",
        type="primary",
        use_container_width=True,
    ):
        scheduler.add_job(new_source_id, frequency_minutes)
        st.success(f":material/check: Job added.")
        st.rerun()

# ─────────────────────────── tips ────────────────────────────────────────
st.markdown("---")
with st.expander(":material/lightbulb: Tips and notes"):
    st.markdown(
        """
- The scheduler runs in a background thread inside this Streamlit process.
  **If you close the Streamlit server (Ctrl+C), scheduled jobs stop.**
- For production, deploy this app behind a process manager (systemd / supervisord)
  or move the scheduler to a separate service (Airflow, Azure Functions, ADF).
- Jobs are persisted in `data/jobs.json` so they reload on restart.
- Run history is recorded on the **Statistics** page.
- The minimum useful interval is 5 minutes — anything faster will likely overlap.
        """
    )
