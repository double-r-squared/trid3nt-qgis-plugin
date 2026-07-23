#!/usr/bin/env python3
"""One-off plotting script for the embedding-model-shootout experiment.

Reads the three candidates' results.json (+ metrics_addendum.json where
present) under data/ and renders three PNGs into graphs/. Not part of the
reusable bench engine -- experiment-local only.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
GRAPHS = HERE / "graphs"
GRAPHS.mkdir(exist_ok=True)

# Categorical palette (fixed order, per experiments/embedding-model-shootout
# dataviz pass): slot1 blue = incumbent, slot2 aqua = bge-small, slot3 yellow = gte-small.
COLORS = {
    "incumbent_minilm": "#2a78d6",
    "bge_small_en_v1_5": "#1baf7a",
    "gte_small": "#eda100",
}
LABELS = {
    "incumbent_minilm": "all-MiniLM-L6-v2\n(incumbent)",
    "bge_small_en_v1_5": "bge-small-en-v1.5",
    "gte_small": "gte-small",
}
ORDER = ["incumbent_minilm", "bge_small_en_v1_5", "gte_small"]


def load(model: str) -> dict:
    d = json.loads((DATA / model / "results.json").read_text())
    run1 = d["aggregate"]["per_run"][0]
    addendum_path = DATA / model / "metrics_addendum.json"
    if addendum_path.exists():
        addendum = json.loads(addendum_path.read_text())
        run1_ndcg_mrr = addendum["per_run"]["1"] if "1" in addendum["per_run"] else addendum["per_run"][1]
    else:
        run1_ndcg_mrr = None
    raw_lines = (DATA / model / "raw_rankings.jsonl").read_text().splitlines()
    times_by_run: dict[int, list[float]] = {}
    for line in raw_lines:
        row = json.loads(line)
        times_by_run.setdefault(row["run"], []).append(row["turnaround_ms"])
    steady = [t for run, ts in times_by_run.items() if run != 1 for t in ts]
    steady_mean = sum(steady) / len(steady) if steady else None
    return {
        "run1": run1,
        "ndcg_mrr": run1_ndcg_mrr,
        "steady_latency_mean": steady_mean,
    }


results = {m: load(m) for m in ORDER}

# ---------------------------------------------------------------------------
# Chart 1: hit@5 by register x model (grouped bars).
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(7.5, 4.5))
registers = ["specific", "vague"]
x = range(len(registers))
width = 0.25
for i, model in enumerate(ORDER):
    by_reg = results[model]["run1"]["by_register"]
    rates = [by_reg[r]["hit_at_k"] / by_reg[r]["n"] for r in registers]
    offs = [xi + (i - 1) * width for xi in x]
    bars = ax.bar(offs, rates, width, label=LABELS[model], color=COLORS[model])
    for b, r in zip(bars, registers):
        n = by_reg[r]["n"]
        hk = by_reg[r]["hit_at_k"]
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.015,
                f"{hk}/{n}", ha="center", va="bottom", fontsize=8, color="#52514e")
ax.set_xticks(list(x))
ax.set_xticklabels(["specific (n=28)", "vague (n=32)"])
ax.set_ylabel("hit@5 rate")
ax.set_ylim(0, 1.08)
ax.set_title("retrieval hit@5 by register x embedding model (extended probe, k=5)")
ax.legend(loc="lower right", fontsize=8)
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(GRAPHS / "hit5_by_register_model.png", dpi=150)
plt.close(fig)

# ---------------------------------------------------------------------------
# Chart 2: nDCG@5 and MRR@5 by model (overall).
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(7, 4.5))
metrics = ["ndcg_at_k_mean", "mrr_at_k_mean"]
metric_labels = ["nDCG@5", "MRR@5"]
xm = range(len(metrics))
width = 0.25
for i, model in enumerate(ORDER):
    nm = results[model]["ndcg_mrr"]
    if nm is None:
        continue
    vals = [nm["ndcg_at_k_mean"], nm["mrr_at_k_mean"]]
    offs = [xi + (i - 1) * width for xi in xm]
    bars = ax.bar(offs, vals, width, label=LABELS[model], color=COLORS[model])
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.015,
                f"{v:.3f}", ha="center", va="bottom", fontsize=8, color="#52514e")
ax.set_xticks(list(xm))
ax.set_xticklabels(metric_labels)
ax.set_ylabel("score")
ax.set_ylim(0, 1.0)
ax.set_title("ranked-quality metrics by embedding model (overall, n=60)")
ax.legend(loc="upper right", fontsize=8)
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(GRAPHS / "ndcg_mrr_by_model.png", dpi=150)
plt.close(fig)

# ---------------------------------------------------------------------------
# Chart 3: per-query latency, steady-state mean, with the 50ms ceiling.
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(7, 4.5))
lat = [results[m]["steady_latency_mean"] for m in ORDER]
bars = ax.bar([LABELS[m] for m in ORDER], lat, color=[COLORS[m] for m in ORDER], width=0.5)
for b, v in zip(bars, lat):
    ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 0.5,
            f"{v:.2f} ms", ha="center", va="bottom", fontsize=9, color="#52514e")
ax.axhline(50, color="#e34948", linestyle="--", linewidth=1.5)
ax.text(0.02, 50, "50 ms interactive ceiling", transform=ax.get_yaxis_transform(),
        color="#e34948", fontsize=8, va="bottom")
ax.set_ylabel("mean turnaround ms/query (steady state, runs 2-5)")
ax.set_ylim(0, 55)
ax.set_title("per-query retrieval latency by embedding model")
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
fig.savefig(GRAPHS / "latency_per_query.png", dpi=150)
plt.close(fig)

print("wrote:")
for p in sorted(GRAPHS.glob("*.png")):
    print(" ", p)
