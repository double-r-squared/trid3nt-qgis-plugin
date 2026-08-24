#!/usr/bin/env python
"""Live driver: a user_gated ``swmm_rdii_rtk_unit_hydrograph`` through the CARDS.

A declaration, not a protocol implementation: the tool, its args, the answers its
gates get, and the assertions the run has to satisfy. The socket work lives in
``trid3nt_server.testing``.

  * the FORM card carries the whole resolved sheet - the three R/T/K unit
    hydrographs, the sewershed, the storm and the timestep;
  * the driver edits exactly one row, ``R1`` (the SHORT unit hydrograph's RDII
    volume fraction), which the RTK volume identity makes the answer
    proportional to. Doubling R1 raises sum(R) from 0.19 to 0.29, so the RDII
    volume must rise by that ratio and the peak with it.

This template publishes NO raster - it is the chart-first class - so there is no
run prefix to read products back from. The evidence is the chart the run itself
emitted, cited rather than rebuilt.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_rdii_rtk_cards.py [--timeout 900] [--out evidence.json]

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

#: The declared default for R1 is 0.10; doubling it is unmistakably the edit.
REVISED_R1 = 0.20
#: Where the drive records what it saw.
EVIDENCE = os.path.join(os.path.dirname(__file__), "..", "docs", "proof",
                        "swmm_rdii_rtk_cards_evidence.json")

RUN = LiveRun(
    tool="swmm_rdii_rtk_unit_hydrograph",
    args={
        "sewershed_area_ac": 100.0,
        "rainfall_depth_in": 1.0,
        "storm_duration_hr": 1.0,
        "input_mode": "user_gated",
    },
    case_title="showcase: swmm RTK unit-hydrograph RDII (cards)",
    answers=GateAnswers(form_edits={"R1": REVISED_R1}, require_form=True,
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
        "uh_rows": {n: rows.get(n) for n in ("R1", "T1", "K1", "R2", "R3")},
        "advanced_rows": [n for n, r in rows.items() if r.get("advanced")],
        "edited": form.get("edited"),
        "plain_warnings": ev.plain_warnings,
        "charts": ev.charts,
        "chart_titles": [p.get("title") for p in ev.chart_payloads],
        "detail": ev.detail,
        "evidence": ns.out,
    }, indent=2, default=str))

    ev.require_ok()
    chart = ev.require_chart(title_contains="RDII")
    caption = str(chart.get("caption", ""))
    print(f"\nchart caption: {caption}")
    # The edit has to reach the physics: sum R rises from 0.19 to 0.29.
    if "sum R=0.29" not in caption:
        raise SystemExit(
            "the emitted chart does not narrate the REVISED sum R - the form "
            f"edit did not reach the run. caption: {caption}")
    if "Volume identity ratio" not in caption:
        raise SystemExit("the emitted chart does not narrate the volume identity")
    return 0


if __name__ == "__main__":
    sys.exit(main())
