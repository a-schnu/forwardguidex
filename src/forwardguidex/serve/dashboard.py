"""Streamlit dashboard: run with `streamlit run src/forwardguidex/serve/dashboard.py`."""
from __future__ import annotations

import plotly.express as px
import streamlit as st

from forwardguidex.config import get_settings
from forwardguidex.db import connect
from forwardguidex.transform import marts

st.set_page_config(page_title="ForwardGuidex", page_icon=":satellite:", layout="wide")
st.title("ForwardGuidex")
st.caption("Forward guidance per tutto il tuo universo d'investimento.")

if not get_settings().db_path.exists():
    st.info("Database not found. Run `fwdx ingest all` then `fwdx marts` first.")
    st.stop()

con = connect(read_only=True)
sec, lat, rat, nws = (marts.sectors(con), marts.latest(con),
                      marts.rates(con), marts.news(con, 25))

left, right = st.columns(2)
with left:
    st.subheader("Sector performance (1d %)")
    if sec.empty:
        st.info("No sector data yet.")
    else:
        fig = px.bar(sec.sort_values("avg_ret_1d"), x="avg_ret_1d", y="sector_label",
                     orientation="h", color="avg_ret_1d", color_continuous_scale="RdYlGn")
        fig.update_layout(yaxis_title="", xaxis_title="avg 1d %", coloraxis_showscale=False)
        st.plotly_chart(fig, use_container_width=True)
with right:
    st.subheader("Rates & yields")
    if rat.empty:
        st.info("No rates yet (set FRED_API_KEY).")
    else:
        st.dataframe(rat[["name", "value", "chg"]], hide_index=True, use_container_width=True)

st.subheader("Universe")
if not lat.empty:
    st.dataframe(
        lat[["ticker", "name", "role", "sector_label", "last_close", "ret_1d", "ret_5d"]],
        hide_index=True, use_container_width=True,
    )

st.subheader("Latest headlines")
for r in nws.itertuples():
    st.markdown(f"**[{r.topic}]** [{r.title}]({r.url}) - _{r.domain}_")

st.subheader("Morning Brief")
try:
    row = con.execute(
        "SELECT content FROM brief_history ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    st.markdown(row[0] if row else "_No brief yet - run `fwdx brief`._")
except Exception:  # noqa: BLE001
    st.info("No brief yet - run `fwdx brief`.")
