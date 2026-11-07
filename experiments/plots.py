"""Draw the paper's figures from the result CSVs, as PNGs in experiments/results/.

    python experiments/plots.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.ticker  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS = REPO_ROOT / "experiments" / "results"

# Categorical colours in fixed order, checked with a colour-blindness validator.
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
INK = "#0b0b0b"
MUTED = "#52514e"
GRID = "#e6e5e0"
MARKERS = ["o", "s", "^", "D", "v"]

STRATEGY_LABELS = {
    "none": "None",
    "periodic_fixed_5min": "Periodic\n(fixed 5 min)",
    "periodic_young": "Periodic\n(Young)",
    "reactive": "Reactive",
    "hybrid": "Hybrid\n(SpotSentry)",
    "fixed_1min": "Fixed 1 min",
    "fixed_5min": "Fixed 5 min",
    "fixed_15min": "Fixed 15 min",
    "adaptive_young": "Adaptive\nYoung",
}
MTBI_LABELS = {"short": "M = 5 min", "medium": "M = 15 min", "long": "M = 60 min"}


def style() -> None:
    plt.rcParams.update(
        {
            "figure.dpi": 200,
            "font.size": 9,
            "axes.edgecolor": MUTED,
            "axes.labelcolor": MUTED,
            "axes.titlesize": 10,
            "axes.titleweight": "bold",
            "axes.titlecolor": INK,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "axes.axisbelow": True,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.major.size": 0,
            "ytick.major.size": 0,
            "legend.frameon": False,
            "lines.linewidth": 2,
            "lines.markersize": 6,
        }
    )


def load(name: str) -> pd.DataFrame | None:
    path = RESULTS / f"{name}.csv"
    return pd.read_csv(path) if path.exists() else None


def save(fig, name: str) -> None:
    fig.tight_layout()
    fig.savefig(RESULTS / name, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"Wrote {name}")


def grouped_bars(ax, frame: pd.DataFrame, x: str, series: str, value: str, x_order, labels) -> None:
    """Bars with a gap between them, error bars for one standard deviation."""
    series_values = [s for s in ["short", "medium", "long"] if s in set(frame[series])]
    width = 0.8 / max(len(series_values), 1)
    positions = np.arange(len(x_order))
    tallest = 0.0
    for index, name in enumerate(series_values):
        subset = frame[frame[series] == name].groupby(x)[value]
        means = subset.mean().reindex(x_order)
        stds = subset.std(ddof=1).reindex(x_order).fillna(0)
        tallest = max(tallest, float((means + stds).max()))
        offset = (index - (len(series_values) - 1) / 2) * width
        ax.bar(
            positions + offset, means, width * 0.92, yerr=stds, color=SERIES[index],
            label=MTBI_LABELS.get(name, name),
            error_kw={"elinewidth": 0.8, "ecolor": MUTED, "capsize": 1.5},
        )
    ax.set_xticks(positions, [labels.get(o, o) for o in x_order])
    # Headroom so the legend never sits on a bar.
    ax.set_ylim(0, tallest * 1.25)
    ax.legend(loc="upper right", ncols=len(series_values))


def plot_e1() -> None:
    data = load("e1")
    if data is None:
        return
    order = ["none", "periodic_fixed_5min", "periodic_young", "reactive", "hybrid"]
    fig, ax = plt.subplots(figsize=(6.5, 3.2))
    grouped_bars(ax, data, "strategy", "mtbi", "time_vs_ideal", order, STRATEGY_LABELS)
    ax.axhline(1, color=MUTED, linewidth=0.8, linestyle="--")
    ax.set_ylabel("Completion time / uninterrupted time")
    ax.set_title("E1: Completion time by checkpoint strategy")
    save(fig, "e1_completion_time.png")


def plot_e2() -> None:
    data = load("e2")
    if data is None:
        return
    fig, ax = plt.subplots(figsize=(6.5, 3.2))
    labels = {"aware": "Deadline-aware", "naive": "Always full (naive)"}
    for index, mode in enumerate(["aware", "naive"]):
        subset = data[data["mode"] == mode].groupby("state_mb")
        met = subset["deadline_met"].mean() * 100
        ax.plot(met.index, met.values, color=SERIES[index], marker=MARKERS[index],
                label=labels[mode])
    saved = data[data["mode"] == "aware"].groupby("state_mb")["saved"].mean() * 100
    ax.plot(saved.index, saved.values, color=SERIES[2], marker=MARKERS[2],
            label="Deadline-aware: progress saved")
    ax.set_xscale("log")
    ticks = sorted(data["state_mb"].unique())
    ax.set_xticks(ticks, [str(t) for t in ticks])
    ax.minorticks_off()
    ax.set_ylim(-4, 106)
    ax.set_xlabel("Model state (MB). The full checkpoint is 3 times this.")
    ax.set_ylabel("Trials (%)")
    ax.set_title("E2: Deadline met, by state size")
    ax.legend(loc="lower left")
    save(fig, "e2_deadline_met.png")


def plot_e3() -> None:
    data = load("e3")
    if data is None:
        return
    fig, axes = plt.subplots(1, 2, figsize=(6.5, 3.0), sharey=True)
    tiers = [t for t in ["full", "light"] if t in set(data["tier"])]
    for ax, tier in zip(axes, tiers, strict=False):
        subset = data[(data["tier"] == tier) & (data["rep"] == data["rep"].min())]
        for index, (count, row) in enumerate(subset.groupby("planned_interruptions")):
            history = yaml.safe_load(row["history"].iloc[0]) if isinstance(
                row["history"].iloc[0], str
            ) else None
            if not history:
                continue
            epochs = [h["epoch"] for h in history]
            accuracy = [h["test_accuracy"] * 100 for h in history]
            ax.plot(
                epochs, accuracy, color=SERIES[index], marker=MARKERS[index],
                label=f"{count} interruption{'' if count == 1 else 's'}", linewidth=2, alpha=0.9,
                markersize=7 - index, zorder=5 - index,
            )
        # Identical runs draw on top of each other, which looks like a missing series.
        note = " (all lines overlap)" if tier == "full" else ""
        ax.set_title(f"{tier.capitalize()} tier{note}")
        ax.set_xlabel("Epoch")
        ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True))
    axes[0].set_ylabel("Test accuracy (%)")
    axes[0].legend(loc="lower right")
    fig.suptitle(
        "E3: Accuracy after resuming (first seed)", fontsize=10, fontweight="bold", color=INK
    )
    save(fig, "e3_accuracy.png")


def plot_e4() -> None:
    data = load("e4")
    if data is None:
        return
    fig, ax = plt.subplots(figsize=(4.2, 4.0))
    limit = max(data["predicted_sec"].max(), data["actual_sec"].max()) * 1.4
    low = min(data["predicted_sec"].min(), data["actual_sec"].min()) * 0.7
    ax.plot([low, limit], [low, limit], color=MUTED, linewidth=0.8, linestyle="--",
            label="Perfect prediction")
    for index, (workload, rows) in enumerate(data.groupby("workload")):
        ax.scatter(rows["predicted_sec"], rows["actual_sec"], s=36, color=SERIES[index],
                   marker=MARKERS[index], edgecolors="white", linewidths=0.8, label=workload)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.grid(True, axis="both")
    ax.set_xlabel("Predicted run time (s)")
    ax.set_ylabel("Actual run time (s)")
    ax.set_title("E4: Predicted against actual")
    ax.legend(loc="upper left")
    save(fig, "e4_prediction.png")


def plot_e5() -> None:
    data = load("e5")
    if data is None:
        return
    fig, ax = plt.subplots(figsize=(6.5, 3.0))
    sizes = sorted(data["state_mb"].unique())
    positions = np.arange(len(sizes))
    width = 0.38
    labels = {"sync": "Synchronous", "async": "Asynchronous"}
    for index, mode in enumerate(["sync", "async"]):
        subset = data[data["mode"] == mode].groupby("state_mb")["blocking_per_checkpoint_s"]
        means = subset.mean().reindex(sizes)
        stds = subset.std(ddof=1).reindex(sizes).fillna(0)
        bars = ax.bar(
            positions + (index - 0.5) * width, means, width * 0.92, yerr=stds,
            color=SERIES[index], label=labels[mode],
            error_kw={"elinewidth": 0.8, "ecolor": MUTED, "capsize": 1.5},
        )
        ax.bar_label(
            bars, labels=[f"{v:.2f}" for v in means], padding=2, fontsize=7.5, color=INK
        )
    ax.set_xticks(positions, [f"{s} MB" for s in sizes])
    ax.set_ylabel("Job blocked per checkpoint (s)")
    ax.set_title("E5: Time the job is blocked per checkpoint")
    ax.legend(loc="upper left")
    save(fig, "e5_overhead.png")


def plot_e6() -> None:
    data = load("e6")
    if data is None:
        return
    order = ["fixed_1min", "fixed_5min", "fixed_15min", "adaptive_young"]
    fig, ax = plt.subplots(figsize=(6.5, 3.2))
    grouped_bars(ax, data, "strategy", "mtbi", "time_vs_ideal", order, STRATEGY_LABELS)
    ax.axhline(1, color=MUTED, linewidth=0.8, linestyle="--")
    ax.set_ylabel("Completion time / uninterrupted time")
    ax.set_title("E6: Completion time, fixed against adaptive interval")
    save(fig, "e6_interval.png")


def main() -> int:
    if not RESULTS.exists():
        print("No results yet")
        return 1
    style()
    for plot in (plot_e1, plot_e2, plot_e3, plot_e4, plot_e5, plot_e6):
        plot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
