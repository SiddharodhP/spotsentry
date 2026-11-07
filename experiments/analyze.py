"""Turn the result CSVs into summary tables and docs/RESULTS.md.

    python experiments/analyze.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS = REPO_ROOT / "experiments" / "results"
REPORT = REPO_ROOT / "docs" / "RESULTS.md"


def mean_std(frame: pd.DataFrame, by: list[str], column: str, digits: int = 1) -> pd.Series:
    """Format a column as "mean +/- std" for each group."""
    grouped = pd.to_numeric(frame[column], errors="coerce").groupby([frame[b] for b in by])
    return grouped.agg(
        lambda s: f"{s.mean():.{digits}f} +/- {s.std(ddof=1) if len(s) > 1 else 0:.{digits}f}"
    )


def to_markdown(frame: pd.DataFrame) -> str:
    """A plain Markdown table, with no extra dependency."""
    columns = [str(c) for c in frame.columns]
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for _, row in frame.iterrows():
        lines.append("| " + " | ".join(str(v) for v in row.tolist()) + " |")
    return "\n".join(lines)


def load(name: str) -> pd.DataFrame | None:
    path = RESULTS / f"{name}.csv"
    return pd.read_csv(path) if path.exists() else None


def e1_summary(data: pd.DataFrame) -> pd.DataFrame:
    by = ["mtbi", "strategy"]
    table = pd.DataFrame(
        {
            "completed": data.groupby(by)["completed"].apply(lambda s: f"{s.sum()}/{len(s)}"),
            "time / ideal": mean_std(data, by, "time_vs_ideal", 2),
            "lost work (s)": mean_std(data, by, "lost_work_sec", 2),
            "overhead %": mean_std(data, by, "overhead_percent", 2),
            "restarts": mean_std(data.assign(r=data["workers"] - 1), by, "r", 1),
            "cost / on-demand": mean_std(data, by, "cost_vs_on_demand", 2),
        }
    ).reset_index()
    order = {"short": 0, "medium": 1, "long": 2}
    return table.sort_values(
        by=["mtbi", "strategy"], key=lambda s: s.map(order) if s.name == "mtbi" else s
    )


def e2_summary(data: pd.DataFrame) -> pd.DataFrame:
    frame = data.copy()
    table = (
        frame.groupby(["mode", "state_mb"])
        .agg(
            trials=("deadline_met", "size"),
            deadline_met=("deadline_met", lambda s: f"{100 * s.mean():.0f}%"),
            saved=("saved", lambda s: f"{100 * s.mean():.0f}%"),
            full=("chosen", lambda s: (s == "full").sum()),
            light=("chosen", lambda s: (s == "light").sum()),
            tiny=("chosen", lambda s: (s == "tiny").sum()),
            skip=("chosen", lambda s: (s == "skip").sum()),
        )
        .reset_index()
    )
    return table.rename(columns={"state_mb": "model MB", "deadline_met": "deadline met"})


def e2_accuracy(data: pd.DataFrame) -> tuple[float, float, int]:
    """How predictions compare with actual save times, on saves that completed."""
    frame = data[(data["mode"] == "aware") & (data["saved"])].copy()
    frame["predicted_s"] = pd.to_numeric(frame["predicted_s"], errors="coerce")
    frame["actual_s"] = pd.to_numeric(frame["actual_s"], errors="coerce")
    frame = frame.dropna(subset=["predicted_s", "actual_s"])
    frame = frame[frame["actual_s"] > 0.2]
    if frame.empty:
        return float("nan"), float("nan"), 0
    ratio = frame["predicted_s"] / frame["actual_s"]
    covered = (frame["actual_s"] <= frame["predicted_s"]).mean()
    return float(ratio.mean()), float(covered), len(frame)


def e3_summary(data: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    table = (
        data.groupby(["tier", "planned_interruptions"])
        .agg(
            runs=("test_accuracy", "size"),
            completed=("completed", "sum"),
            accuracy_mean=("test_accuracy", "mean"),
            accuracy_std=("test_accuracy", "std"),
            loss_mean=("test_loss", "mean"),
        )
        .reset_index()
        .round(5)
    )
    notes = []
    for tier, group in data.groupby("tier"):
        for rep, by_rep in group.groupby("rep"):
            base = by_rep[by_rep["planned_interruptions"] == 0]
            if base.empty:
                continue
            base_weights = float(base["weights_abs_sum"].iloc[0])
            others = by_rep[by_rep["planned_interruptions"] > 0]
            same = (others["weights_abs_sum"].astype(float) == base_weights).sum()
            notes.append(
                f"{tier}, seed offset {rep}: {same}/{len(others)} interrupted runs have final "
                f"weights identical to the uninterrupted run"
            )
    return table, notes


def e4_summary(data: pd.DataFrame) -> pd.DataFrame:
    by = ["machine", "workload", "size"]
    return pd.DataFrame(
        {
            "predicted (s)": mean_std(data, by, "predicted_sec", 1),
            "actual (s)": mean_std(data, by, "actual_sec", 1),
            "abs error %": mean_std(data, by, "error_percent", 1),
        }
    ).reset_index()


def e5_summary(data: pd.DataFrame) -> pd.DataFrame:
    by = ["state_mb", "mode"]
    return pd.DataFrame(
        {
            "blocking per checkpoint (s)": mean_std(data, by, "blocking_per_checkpoint_s", 3),
            "overhead %": mean_std(data, by, "overhead_percent", 2),
            "job slowdown %": mean_std(data, by, "slowdown_percent", 1),
        }
    ).reset_index()


def e6_summary(data: pd.DataFrame) -> pd.DataFrame:
    by = ["mtbi", "strategy"]
    table = pd.DataFrame(
        {
            "completed": data.groupby(by)["completed"].apply(lambda s: f"{s.sum()}/{len(s)}"),
            "time / ideal": mean_std(data, by, "time_vs_ideal", 2),
            "lost work (s)": mean_std(data, by, "lost_work_sec", 2),
            "overhead %": mean_std(data, by, "overhead_percent", 1),
        }
    ).reset_index()
    order = {"short": 0, "medium": 1, "long": 2}
    return table.sort_values(
        by=["mtbi", "strategy"], key=lambda s: s.map(order) if s.name == "mtbi" else s
    )


HEADER = """# Experiment results

Generated by `python experiments/analyze.py` on {today}. Every number here comes from the CSVs in
`experiments/results/`. Run the experiments again and regenerate this file; do not edit it by hand.

**Setup.** Workers ran as local processes on one laptop (12 logical CPUs, 15.5 GB RAM, Windows 11),
with a filesystem store whose uploads are limited by SpotSentry's own throttle, and a local
metadata service. These are not results from AWS. Interruptions are drawn from an exponential
distribution with mean M, using a fixed seed, so every strategy faces the same interruption times.

**Time scaling (E1, E6).** All times are divided by 30: a 30-minute job takes 60 s, a 2-minute
notice 4 s, a 3-minute replacement boot 6 s. Python's start-up time is not scaled, which penalises
every strategy that restarts more often.
"""


def main() -> int:
    if not RESULTS.exists():
        print("No results yet")
        return 1
    parts = [HEADER.format(today=date.today().isoformat())]
    data = load("e1")
    if data is not None:
        parts += ["## E1: which checkpoint strategy is best?\n", to_markdown(e1_summary(data)), ""]
        parts += ["![E1](../experiments/results/e1_completion_time.png)\n"]
    data = load("e2")
    if data is not None:
        parts += ["## E2: does deadline-aware saving beat always saving full?\n"]
        parts += [to_markdown(e2_summary(data)), ""]
        ratio, covered, count = e2_accuracy(data)
        if count:
            parts += [
                f"On {count} completed deadline-aware saves, predicted time was on average "
                f"{ratio:.2f} times the actual time, and the prediction covered the actual time "
                f"in {covered:.0%} of them. \"Deadline met\" counts a deliberate skip as met: the "
                "previous checkpoint stays valid and no time is overrun. A naive save that hits "
                "the limit is cut off and loses everything since the last checkpoint.\n"
            ]
        parts += ["![E2](../experiments/results/e2_deadline_met.png)\n"]
    data = load("e3")
    if data is not None:
        table, notes = e3_summary(data)
        parts += ["## E3: is resume exact?\n", to_markdown(table), ""]
        parts += [f"- {note}" for note in notes] + [""]
        parts += ["![E3](../experiments/results/e3_accuracy.png)\n"]
    data = load("e4")
    if data is not None:
        parts += ["## E4: how accurate is the run-time prediction?\n"]
        parts += [to_markdown(e4_summary(data)), ""]
        machines = data["machine"].nunique()
        if machines < 2:
            parts += [
                f"**Incomplete:** this covers {machines} machine; the spec asks for 2 or more.\n"
            ]
        parts += ["![E4](../experiments/results/e4_prediction.png)\n"]
    data = load("e5")
    if data is not None:
        parts += ["## E5: does async checkpointing reduce overhead?\n"]
        parts += [to_markdown(e5_summary(data)), ""]
        parts += ["![E5](../experiments/results/e5_overhead.png)\n"]
    data = load("e6")
    if data is not None:
        parts += ["## E6: does the adaptive interval help?\n", to_markdown(e6_summary(data)), ""]
        parts += ["![E6](../experiments/results/e6_interval.png)\n"]
    REPORT.write_text("\n".join(parts), encoding="utf-8")
    print(f"Wrote {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
