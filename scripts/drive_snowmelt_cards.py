#!/usr/bin/env python
"""Live driver: a user_gated ``swmm_snowmelt_degree_day`` through the CARDS.

A declaration, not a protocol implementation: the tool, its args, the answers its
gates get, and the assertions the run has to satisfy. The socket work lives in
``trid3nt_server.testing``.

  * the FORM card carries the whole resolved sheet - the declared rain-on-snow
    forcing shape, the degree-day coefficients, the pack surfaces and the
    plowing knobs - which at HEAD was a baked demo function plus a dozen
    literals inside an f-string;
  * the driver edits exactly one row, ``snowfall_intensity_in_hr``, doubling the
    precipitation that falls through the cold spell. A pack twice as deep is the
    unambiguous consequence, and the SWE chart the run itself emits narrates it.

This template publishes NO raster - it is the chart-first class - so there is no
run prefix to read products back from. The evidence is the charts the run itself
emitted, cited rather than rebuilt.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_snowmelt_cards.py [--timeout 900] [--out evidence.json]

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

#: The declared default is 0.05 in/hr through the cold spell; doubling it must
#: roughly double the peak snow water equivalent (1.20 in at the default).
REVISED_SNOWFALL_IN_HR = 0.10
#: Where the drive records what it saw.
EVIDENCE = os.path.join(os.path.dirname(__file__), "..", "docs", "proof",
                        "swmm_snowmelt_cards_evidence.json")

RUN = LiveRun(
    tool="swmm_snowmelt_degree_day",
    args={
        "area_ac": 50.0,
        "sim_days": 5.0,
        "input_mode": "user_gated",
    },
    case_title="showcase: swmm snowpack degree-day melt (rain-on-snow, cards)",
    answers=GateAnswers(
        form_edits={"snowfall_intensity_in_hr": REVISED_SNOWFALL_IN_HR},
        require_form=True, confirm="proceed"),
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
        "forcing_rows": {n: rows.get(n) for n in
                         ("cold_temp_f", "warm_temp_f",
                          "snowfall_intensity_in_hr", "rain_intensity_in_hr",
                          "dividing_temp_f")},
        "advanced_rows": sorted(n for n, r in rows.items() if r.get("advanced")),
        "edited": form.get("edited"),
        "plain_warnings": ev.plain_warnings,
        "charts": ev.charts,
        "chart_titles": [p.get("title") for p in ev.chart_payloads],
        "detail": ev.detail,
        "evidence": ns.out,
    }, indent=2, default=str))

    ev.require_ok()
    swe = ev.require_chart(title_contains="snow water equivalent")
    runoff = ev.require_chart(title_contains="runoff hydrograph")
    swe_caption = str(swe.get("caption", ""))
    print(f"\nSWE chart caption:    {swe_caption}")
    print(f"runoff chart caption: {runoff.get('caption')}")
    # The edit has to reach the physics: the reference run peaks at 1.20 in SWE,
    # so double snowfall must clear that by a wide margin.
    peak = _peak_swe_in(swe_caption)
    if peak is None or peak < 2.0:
        raise SystemExit(
            "the emitted SWE chart does not narrate a DOUBLED snowpack - the "
            f"form edit did not reach the run. caption: {swe_caption}")
    print(f"\npeak SWE at the revised snowfall: {peak} in "
          f"(reference run at the declared 0.05 in/hr: 1.20 in)")
    return 0


def _peak_swe_in(caption: str) -> float | None:
    """The peak SWE the run's own chart narrates, read back off the caption."""
    marker = "peak SWE "
    if marker not in caption:
        return None
    tail = caption.split(marker, 1)[1]
    try:
        return float(tail.split(" in", 1)[0])
    except ValueError:
        return None


if __name__ == "__main__":
    sys.exit(main())
