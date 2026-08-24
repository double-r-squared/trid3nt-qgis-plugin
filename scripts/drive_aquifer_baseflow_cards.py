#!/usr/bin/env python
"""Live driver: a user_gated ``swmm_aquifer_baseflow_to_node`` through the CARDS.

A declaration, not a protocol implementation: the tool, its args, the answers its
gates get, and the assertions the run has to satisfy. The socket work lives in
``trid3nt_server.testing``.

  * the FORM card carries the whole resolved sheet - including the four
    SoilGrids-derived two-zone column rows with their real values and source
    badges;
  * the driver edits exactly one row, ``a1`` (the groundwater-to-node flow
    coefficient), which is the term the answer is most directly proportional to.

This template publishes NO raster - it is the chart-first class - so there is no
run prefix to read products back from. The evidence is the chart the run itself
emitted, cited rather than rebuilt.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_aquifer_baseflow_cards.py [--timeout 900] [--out evidence.json]

The evidence lands in ``docs/proof/`` by default: this template publishes no
raster, so the JSON the drive writes IS the record that the run happened.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trid3nt_server.testing import GateAnswers, LiveRun, run_live  # noqa: E402

#: Deep agricultural soil with clear SoilGrids texture coverage.
LOCATION = "Ames, Iowa"
#: Double the declared default, so the baseflow the card's edit produces is
#: unmistakably the edited one.
REVISED_A1 = 0.004
#: Where the drive records what it saw - committed, so the run is citable.
EVIDENCE = os.path.join(os.path.dirname(__file__), "..", "docs", "proof",
                        "swmm_aquifer_baseflow_cards_evidence.json")

RUN = LiveRun(
    tool="swmm_aquifer_baseflow_to_node",
    args={
        "location": LOCATION,
        "sim_days": 24,
        "area_ac": 100.0,
        "input_mode": "user_gated",
    },
    case_title="showcase: swmm aquifer baseflow to node (Ames, cards)",
    answers=GateAnswers(form_edits={"a1": REVISED_A1}, require_form=True,
                        confirm="proceed"),
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--out", default=EVIDENCE)
    ns = ap.parse_args()

    ev = run_live(LiveRun(**{**RUN.__dict__, "timeout_s": ns.timeout}))
    with open(ns.out, "w", encoding="utf-8") as fh:
        json.dump(ev.as_dict(), fh, indent=2, default=str)

    form = ev.form_card or {}
    rows = {r["name"]: r for r in form.get("rows", [])}
    print(json.dumps({
        "tool_status": ev.tool_status,
        "turn_complete": ev.turn_complete,
        "form_card_title": form.get("title"),
        "form_card_rows": len(form.get("rows", [])),
        "column_rows": {n: rows.get(n) for n in
                        ("porosity", "wilting_point", "field_capacity",
                         "conductivity_in_hr")},
        "a1_row": rows.get("a1"),
        "edited": form.get("edited"),
        "plain_warnings": ev.plain_warnings,
        "charts": ev.charts,
        "chart_titles": [p.get("title") for p in ev.chart_payloads],
        "detail": ev.detail,
        "evidence": ns.out,
    }, indent=2, default=str))

    ev.require_ok()
    chart = ev.require_chart(title_contains="node hydrograph")
    caption = str(chart.get("caption", ""))
    print(f"\nchart caption: {caption}")
    if "baseflow" not in caption:
        raise SystemExit("the emitted chart does not narrate the baseflow answer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
