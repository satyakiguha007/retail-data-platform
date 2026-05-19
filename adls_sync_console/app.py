"""
ADLS Sync Console — Main entry / Connection page.

Run with:  streamlit run app.py
"""
import sys
from pathlib import Path

# Make 'core' importable when run via 'streamlit run app.py'
sys.path.insert(0, str(Path(__file__).resolve().parent))

import streamlit as st

from core.adls import ADLSClient
from core.config import SOURCES, resolve_local_root


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n/(1024*1024):.1f} MB"
    return f"{n/(1024*1024*1024):.2f} GB"


# ─────────────────────────────── page config ──────────────────────────────────
st.set_page_config(
    page_title="ADLS Sync Console",
    page_icon=":material/cloud_sync:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Minimal custom CSS for cleaner look
st.markdown(
    """
    <style>
      div[data-testid="stMetric"] {
        background: rgba(255,255,255,0.03);
        padding: 0.85rem 1rem;
        border-radius: 12px;
        border: 1px solid rgba(125,125,125,0.18);
      }
      div[data-testid="stMetric"] label { font-weight: 500; }
      div.stButton > button {
        border-radius: 8px;
        font-weight: 500;
      }
      div[data-testid="stExpander"] {
        border-radius: 10px;
      }
      .source-card {
        border-radius: 12px;
        padding: 0.75rem;
      }
      h1, h2, h3 { font-weight: 500; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ─────────────────────────────── sidebar ──────────────────────────────────
with st.sidebar:
    st.markdown("### Retail Data Platform")
    st.caption("ADLS Sync Console")
    st.markdown("---")
    st.caption(f"**Project root**")
    st.code(str(Path(__file__).resolve().parent.parent), language=None)
    st.markdown("---")
    st.caption("Built with Streamlit · Azure SDK · APScheduler")

# ─────────────────────────────── header ───────────────────────────────────
left, right = st.columns([6, 1])
with left:
    st.title(":material/cloud_sync: ADLS Sync Console")
    st.caption("Local-to-cloud data sync for the Retail Data Platform")
with right:
    st.markdown("")

st.markdown("")

# ─────────────────────── connection status section ─────────────────────────
if "adls_client" not in st.session_state:
    st.session_state.adls_client = ADLSClient()
    st.session_state.connection_status = None
    st.session_state.connection_message = ""
    st.session_state.connection_details = {}

st.subheader(":material/lan: Connection")

env = st.session_state.adls_client.get_env_summary()

col1, col2 = st.columns([2, 1])

with col1:
    with st.container(border=True):
        if env["valid"]:
            st.markdown(":material/check_circle: **Environment configured** &nbsp; All required variables present in `.env`")
        else:
            st.error(
                f":material/error: Missing env vars: `{', '.join(env['missing'])}`. "
                "Check the `.env` file in your project root."
            )

        c1, c2 = st.columns(2)
        with c1:
            st.caption("Storage Account")
            st.code(env["storage_account"] or "(not set)", language=None)
            st.caption("Tenant ID")
            st.code(env["tenant_id_masked"] or "(not set)", language=None)
        with c2:
            st.caption("Client ID (Service Principal)")
            st.code(env["client_id_masked"] or "(not set)", language=None)
            st.caption("Client Secret")
            st.code(env["client_secret_masked"] or "(not set)", language=None)

with col2:
    with st.container(border=True):
        st.markdown("**Connection Test**")
        st.caption("Verify the SP can reach ADLS.")

        if st.button(
            "Test Connection",
            type="primary",
            use_container_width=True,
            icon=":material/wifi:",
        ):
            with st.spinner("Connecting to Azure..."):
                ok, msg, details = st.session_state.adls_client.test_connection()
                st.session_state.connection_status = ok
                st.session_state.connection_message = msg
                st.session_state.connection_details = details

        # Status display
        if st.session_state.connection_status is True:
            st.success(":material/check: Connected")
        elif st.session_state.connection_status is False:
            st.error(":material/error: Failed")
        else:
            st.info(":material/help: Not yet tested")

# Connection details (after test)
if st.session_state.connection_status is True:
    details = st.session_state.connection_details
    st.markdown("")
    c1, c2, c3 = st.columns(3)
    c1.metric("Containers Found", details["container_count"])
    c2.metric("Region", details.get("region", "—"))
    c3.metric("Auth Method", "Service Principal")

    with st.expander(":material/folder: View containers", expanded=False):
        for cont in details["containers"]:
            st.markdown(f":material/folder_open: `{cont}`")

elif st.session_state.connection_status is False:
    st.markdown("")
    st.error(st.session_state.connection_message)
    with st.expander(":material/help: Troubleshooting", expanded=True):
        st.markdown(
            """
**Common causes**

- `.env` file missing or values incorrect — verify at project root
- Service principal secret expired — run in Cloud Shell:
  ```
  az ad sp credential reset --id sp-retaildp-simulator-001 --years 1
  ```
- Wrong storage account name in `.env`
- Network or firewall blocking outbound HTTPS
- RBAC role hasn't propagated — wait 2 min after assignment
"""
        )

# ─────────────────────────── sources overview ──────────────────────────────
st.markdown("---")
st.subheader(":material/source: Configured Data Sources")
st.caption(
    f"{len(SOURCES)} sources configured. Use the **Ad-hoc Upload** page to "
    "push them to ADLS, or schedule recurring sync via the **Scheduler** page."
)

# Group sources by category
categories = {}
for s in SOURCES:
    cat = s.get("category", "Other")
    categories.setdefault(cat, []).append(s)

for category, sources_in_cat in categories.items():
    st.markdown(f"##### {category}")
    for source in sources_in_cat:
        local_root = resolve_local_root(source)
        with st.container(border=True):
            c1, c2, c3, c4 = st.columns([3, 1.2, 1.2, 1.6])

            with c1:
                st.markdown(
                    f":material/{source['icon']}: **{source['name']}**"
                )
                st.caption(source.get("description", ""))
                st.markdown(
                    f"<small><code>{source['local_root']}</code> → "
                    f"<code>{source['remote_container']}/{source['remote_prefix']}/</code></small>",
                    unsafe_allow_html=True,
                )

            if local_root.exists():
                files = [f for f in local_root.rglob("*") if f.is_file()]
                file_count = len(files)
                size = sum(f.stat().st_size for f in files)
                c2.metric("Files", file_count)
                c3.metric("Size", _format_size(size))
                if file_count > 0:
                    c4.markdown(":material/check_circle: **Ready**")
                else:
                    c4.markdown(":material/warning: **Empty**")
            else:
                c2.metric("Files", "—")
                c3.metric("Size", "—")
                c4.markdown(":material/folder_off: **Path missing**")

st.markdown("---")
st.caption(
    ":material/lightbulb: **Next step:** Open the **Ad-hoc Upload** page (left sidebar) "
    "to push any of these sources to ADLS on demand. Or use the **Scheduler** page "
    "to set up recurring sync."
)
