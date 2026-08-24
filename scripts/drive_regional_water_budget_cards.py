#!/usr/bin/env python
"""Live driver: a user_gated ``modflow_regional_water_budget`` through the CARDS.

A declaration, not a protocol implementation: the tool, its args, the answers its
gates get, and the assertions the run has to satisfy. The socket work lives in
``trid3nt_server.testing``.

  * the FORM card carries the whole resolved sheet - including ``aquifer_k_ms``
    and ``porosity`` with their REAL SoilGrids-derived values and source badges,
    which is what the DERIVED door buys over resolving them inside a step;
  * the driver edits two rows and each one is traced through a different
    surface: ``porosity`` into the run's own ``metrics.json`` (a steady GWF flow
    budget is not a function of porosity, so the sheet is the only place it can
    show), and ``aquifer_k_ms`` into the BUDGET itself, which is linear in K for
    a fixed-head gradient - so doubling K doubles the partition.

The evidence is the run's OWN artifacts under its prefix (``chart_spec.json``,
``metrics.json``). Nothing here is rederived.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_regional_water_budget_cards.py [--timeout 1800] [--out evidence.json]
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
#: The rows the driver revises, so the edits are traceable end to end.
REVISED_POROSITY = 0.25
#: Exactly twice the SoilGrids-derived K at this AOI.
DERIVED_K_MS = 9.298175630928423e-07
REVISED_K_MS = 2.0 * DERIVED_K_MS
#: The CHD inflow the un-edited reference run reports at the derived K.
REFERENCE_CHD_IN_M3_DAY = 9.887537099091208
#: Where the drive records what it saw - committed, so the run is citable.
EVIDENCE = os.path.join(os.path.dirname(__file__), "..", "docs", "proof",
                        "modflow_regional_water_budget_cards_evidence.json")

RUN = LiveRun(
    tool="modflow_regional_water_budget",
    args={
        "location": LOCATION,
        "zone_partition": "upgradient_downgradient",
        "compute_class": "standard",
        "input_mode": "user_gated",
    },
    case_title="showcase: modflow regional water budget (Ames, cards)",
    answers=GateAnswers(
        form_edits={"porosity": REVISED_POROSITY, "aquifer_k_ms": REVISED_K_MS},
        require_form=True, confirm="proceed"),
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=1800.0)
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
        "aquifer_k_row": rows.get("aquifer_k_ms"),
        "porosity_row": rows.get("porosity"),
        "edited": form.get("edited"),
        "plain_warnings": ev.plain_warnings,
        "layers": [l.get("name") for l in ev.layers],
        "run_id": ev.run_id,
        "product_uris": ev.product_uris,
        "product_errors": ev.product_errors,
        "metrics": ev.metrics,
        "charts": ev.charts,
        "detail": ev.detail,
        "evidence": ns.out,
    }, indent=2, default=str))

    ev.require_ok()
    ev.require_run_products()
    ev.require_layer(layer_type="raster")
    # The edit reached the physics: the run's own metrics record the value the
    # user typed, not the one the derivation produced.
    ev.require_metric_close("porosity", REVISED_POROSITY, rel=1e-9)
    ev.require_metric_close("aquifer_k_ms", REVISED_K_MS, rel=1e-9)
    # ... and the SOLVER used it: the budget is linear in K, so twice the
    # conductivity moves twice the water through the same gradient.
    chd_in = float(ev.metric("budget_partition_m3_day")["chd_in"])
    ratio = chd_in / REFERENCE_CHD_IN_M3_DAY
    print(f"\nchd_in {chd_in:.6f} m3/day vs reference "
          f"{REFERENCE_CHD_IN_M3_DAY:.6f} -> ratio {ratio:.4f} (K doubled)")
    if abs(ratio - 2.0) > 0.01:
        raise SystemExit(f"the edited K did not reach the solve (ratio {ratio:.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
