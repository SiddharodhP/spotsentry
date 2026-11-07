"""Live SpotSentry dashboard. Reads only from the store, so it works the same for MinIO and S3.

    spotsentry dashboard --job jobs/synthetic_demo.yaml
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from spotsentry.config import load_config, load_dotenv  # noqa: E402
from spotsentry.live import JobView, list_jobs, load_job, parse_ts  # noqa: E402
from spotsentry.store import make_store  # noqa: E402

RESULTS_DIR = ROOT / "experiments" / "results"
REFRESH_SECONDS = 2

# One lane per kind of event. Colour follows the kind; the lane label also names it.
EVENT_KINDS = {
    "Checkpoint": "#2a78d6",
    "Rebalance signal": "#eda100",
    "Interruption notice": "#e34948",
    "Emergency decision": "#eb6834",
    "Emergency saved": "#1baf7a",
    "Emergency skipped": "#6250d6",
    "Restored": "#008300",
    "Worker started": "#e87ba4",
}


@st.cache_resource
def get_store_config():
    load_dotenv(ROOT / ".env")
    path = os.environ.get("SPOTSENTRY_DASHBOARD_JOB", str(ROOT / "jobs" / "synthetic_demo.yaml"))
    config = load_config(path)
    return config, make_store(config.store)


class JobPoller:
    """Reads the job in the background, so drawing the page never waits on the network.

    A refresh that takes longer than the refresh interval would otherwise be cut off by the next
    one, leaving the lower sections stale. Polling stops while nobody is viewing the page.
    """

    IDLE_AFTER_SEC = 30

    def __init__(self, store, job_id: str, prefix: str) -> None:
        self.store, self.job_id, self.prefix = store, job_id, prefix
        self.view: JobView | None = None
        self.error: Exception | None = None
        self.last_read = time.monotonic()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            if time.monotonic() - self.last_read < self.IDLE_AFTER_SEC:
                try:
                    self.view = load_job(self.store, self.job_id, self.prefix)
                    self.error = None
                except Exception as exc:  # noqa: BLE001
                    self.error = exc
            time.sleep(REFRESH_SECONDS)


@st.cache_resource
def get_poller(job_id: str) -> JobPoller:
    config, store = get_store_config()
    return JobPoller(store, job_id, config.store.prefix)


def human_size(num_bytes: float) -> str:
    return f"{num_bytes / 1e6:.1f} MB"


def human_age(iso_time: str) -> str:
    seconds = (datetime.now(UTC) - parse_ts(iso_time)).total_seconds()
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    return f"{seconds / 60:.0f} min ago"


def section_live(view: JobView) -> None:
    st.subheader("Live job")
    a, b, c, d = st.columns(4)
    a.metric("State", view.state)
    b.metric("Epoch / step", f"{view.epoch} / {view.step}")
    c.metric("Restarts", view.restarts)
    d.metric("Interruption notices", view.interruptions)
    if view.progress is not None:
        st.progress(view.progress, text=f"{view.progress:.1%} complete")
    a, b, c = st.columns(3)
    a.write(
        f"**Instance:** {view.instance_id or 'none'}  \n"
        f"**Type:** {view.instance_type or 'n/a'}  \n**Zone:** {view.az or 'n/a'}"
    )
    if view.manifest:
        m = view.manifest
        b.write(
            f"**Last checkpoint:** {m['tier']}, epoch {m['epoch']} step {m['step']}  \n"
            f"**Size:** {human_size(m['size_bytes'])}  \n"
            f"**Age:** {human_age(m['created_at'])}"
        )
    else:
        b.write("**Last checkpoint:** none yet")
    interval = f"{view.interval_sec:.0f} s" if view.interval_sec else "n/a"
    loss = f"{view.last_loss:.4f}" if view.last_loss is not None else "n/a"
    c.write(f"**Checkpoint interval:** {interval}  \n**Latest loss:** {loss}")
    if view.status and view.status.get("result"):
        with st.expander("Final result"):
            st.json(view.status["result"])


def section_timeline(view: JobView) -> None:
    st.subheader("Timeline")
    rows = view.timeline()
    if not rows:
        st.info("No events yet.")
        return
    frame = pd.DataFrame(rows)
    frame["time"] = pd.to_datetime(frame["time"], utc=True, format="ISO8601")
    chart_frame = frame[frame["event"].isin(EVENT_KINDS)]
    if not chart_frame.empty:
        chart = (
            alt.Chart(chart_frame)
            .mark_point(filled=True, size=110, opacity=1, stroke="white", strokeWidth=1)
            .encode(
                x=alt.X("time:T", title=None, axis=alt.Axis(format="%H:%M:%S", grid=False)),
                y=alt.Y(
                    "event:N",
                    title=None,
                    sort=list(EVENT_KINDS),
                    axis=alt.Axis(
                        grid=True, gridOpacity=0.25, tickSize=0, domain=False, labelLimit=200
                    ),
                ),
                color=alt.Color(
                    "event:N",
                    scale=alt.Scale(domain=list(EVENT_KINDS), range=list(EVENT_KINDS.values())),
                    legend=None,
                ),
                tooltip=[
                    alt.Tooltip("event:N", title="Event"),
                    alt.Tooltip("time:T", title="Time", format="%H:%M:%S"),
                    alt.Tooltip("instance:N", title="Instance"),
                    alt.Tooltip("detail:N", title="Detail"),
                ],
            )
            .properties(height=250)
        )
        st.altair_chart(chart, width="stretch")
    table = frame.sort_values("time", ascending=False).head(60).copy()
    # The chart shows the viewer's local time, so the table does too.
    local_zone = datetime.now().astimezone().tzinfo
    table["time"] = table["time"].dt.tz_convert(local_zone).dt.strftime("%H:%M:%S")
    with st.expander("Event table", expanded=True):
        st.dataframe(table, hide_index=True, width="stretch", height=260)


def section_cost(view: JobView) -> None:
    st.subheader("Cost")
    cost = view.cost()
    a, b, c = st.columns(3)
    a.metric("Spot cost so far", f"${cost['spot_cost']:.5f}")
    b.metric("Same time on demand", f"${cost['on_demand_cost']:.5f}")
    c.metric("Saved", f"{cost['saved_percent']:.0f}%")
    source = cost["source"]
    st.caption(
        f"{cost['instance_hours'] * 3600:.0f} instance-seconds at spot "
        f"${cost['spot_price']:.4f}/h vs on-demand ${cost['on_demand_price']:.4f}/h, {source}."
    )


def section_planner(view: JobView) -> None:
    st.subheader("Planner")
    plan = view.plan
    if not plan:
        st.info("No plan.json. Create one with: spotsentry plan --job <file> --write")
        return
    choice = "spot" if plan.get("use_spot") else "on-demand"
    predicted = plan.get("predicted_runtime_sec") or 0
    st.write(
        f"**Recommendation:** {plan.get('instance_type')} in {plan.get('az')}, use {choice} "
        f"({plan.get('reason')})  \n"
        f"**Interruption range:** {plan.get('interruption_range')}, "
        f"**placement score:** {plan.get('placement_score') or 'n/a'}"
    )
    a, b = st.columns(2)
    a.metric("Predicted run time", f"{predicted / 60:.1f} min")
    b.metric("Worker time so far", f"{view.instance_seconds / 60:.1f} min")
    if plan.get("candidates"):
        frame = pd.DataFrame(plan["candidates"])
        wanted = [
            "instance_type", "az", "interruption_range", "placement_score", "spot_price",
            "on_demand_price", "cost_spot", "cost_on_demand", "expected_interruptions",
        ]
        st.dataframe(
            frame[[c for c in wanted if c in frame.columns]],
            hide_index=True,
            width="stretch",
        )


def section_experiments() -> None:
    st.subheader("Experiments")
    pictures = sorted(RESULTS_DIR.glob("*.png")) if RESULTS_DIR.exists() else []
    tables = sorted(RESULTS_DIR.glob("*.csv")) if RESULTS_DIR.exists() else []
    if not pictures and not tables:
        st.info(
            "No results yet. Run: python experiments/run.py --config experiments/configs/e1.yaml"
        )
        return
    for start in range(0, len(pictures), 2):
        columns = st.columns(2)
        for column, picture in zip(columns, pictures[start : start + 2], strict=False):
            column.image(str(picture), caption=picture.stem)
    if tables:
        chosen = st.selectbox("Result table", tables, format_func=lambda p: p.name)
        st.dataframe(pd.read_csv(chosen), hide_index=True, width="stretch")


def main() -> None:
    st.set_page_config(page_title="SpotSentry", layout="wide")
    st.title("SpotSentry")
    try:
        config, store = get_store_config()
        jobs = list_jobs(store, config.store.prefix)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Cannot read the store: {exc}")
        # Experiment results are local files, so show them even when the store is down.
        section_experiments()
        st.stop()
    default = jobs.index(config.job_id) if config.job_id in jobs else 0
    job_id = st.sidebar.selectbox("Job", jobs or [config.job_id], index=default)
    st.sidebar.caption(f"Refreshes every {REFRESH_SECONDS} s. Store: {config.store.type}")

    @st.fragment(run_every=REFRESH_SECONDS)
    def live_sections() -> None:
        poller = get_poller(job_id)
        poller.last_read = time.monotonic()
        if poller.error is not None:
            st.warning(f"Cannot read the store right now: {poller.error}")
        view = poller.view
        if view is None:
            st.info("Reading the job from the store...")
            return
        section_live(view)
        section_timeline(view)
        section_cost(view)
        section_planner(view)

    live_sections()
    section_experiments()


main()
