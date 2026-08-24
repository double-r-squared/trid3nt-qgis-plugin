#!/usr/bin/env python
"""Live driver: a user_gated ``telemac_do_sag`` answered through the CARDS.

A declaration, not a protocol implementation: the tool, its args, the answers
its gates get, and the assertions the run has to satisfy. The socket work lives
in ``trid3nt_server.testing``.

  * the DRAW card is answered with a real outfall point on the Eel River reach
    (the USGS Scotia gage), so the release is a USER value, not a derived seed;
  * ``telemac_do_sag`` declares NO FormGate - its reach step reviews its own
    inputs - so the card that fires is the composite's plain input review, and
    the driver answers it as a proceed.

The evidence is the run's OWN artifacts under its prefix (``chart_spec.json``,
``metrics.json``). Nothing here is rederived.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_do_sag_cards.py [--timeout 1800] [--out evidence.json]
                             [--no-render-proof]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from render_all_layers_proof import add_render_proof_flag, render_proof  # noqa: E402
from trid3nt_server.testing import GateAnswers, LiveRun, run_live  # noqa: E402

EVIDENCE = os.path.join(os.path.dirname(__file__), "..", "docs", "proof",
                        "templates", "telemac_do_sag_cards_evidence.json")

#: A real NHDPlus reach WITH NHDArea polygon coverage (the bank_source precondition).
LOCATION = "Eel River near Scotia, California"
#: The USGS Eel River at Scotia gage (11477000) - a real point on the reach.
OUTFALL_LONLAT = [-124.0983, 40.4921]

RUN = LiveRun(
    tool="telemac_do_sag",
    args={
        "location": LOCATION,
        "discharge_bod_mgl": 20.0,
        "water_temp_c": 20.0,
        "do_standard_mgl": 5.0,
        "k1_per_day": 0.3,
        "k2_per_day": 0.9,
        "reach_length_km": 12.0,
        "mesh_resolution": "auto",
        "input_mode": "user_gated",
    },
    case_title="showcase: telemac do sag (Eel River near Scotia, cards)",
    answers=GateAnswers(draw=OUTFALL_LONLAT, draw_geometry="point",
                        require_draw=True, confirm="proceed"),
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default=EVIDENCE)
    add_render_proof_flag(ap)
    ns = ap.parse_args()

    ev = run_live(LiveRun(**{**RUN.__dict__, "timeout_s": ns.timeout}))
    with open(ns.out, "w", encoding="utf-8") as fh:
        json.dump(ev.as_dict(), fh, indent=2, default=str)
    sheet = render_proof(ns.out) if ns.render_proof else None

    print(json.dumps({
        "canvas_layers_sheet": sheet,
        "tool_status": ev.tool_status,
        "turn_complete": ev.turn_complete,
        "draw_card": ev.draw_card,
        "form_card_rows": len((ev.form_card or {}).get("rows", [])),
        "plain_warnings": ev.plain_warnings,
        "outfall_layers": [l for l in ev.layers
                           if "outfall" in str(l.get("name", "")).lower()],
        "run_id": ev.run_id,
        "product_uris": ev.product_uris,
        "product_errors": ev.product_errors,
        "do_min_mgl": (ev.metrics or {}).get("do_min_mgl"),
        "do_min_distance_m": (ev.metrics or {}).get("do_min_distance_m"),
        "do_violates_standard": (ev.metrics or {}).get("do_violates_standard"),
        "detail": ev.detail,
        "evidence": ns.out,
    }, indent=2, default=str))

    ev.require_ok()
    ev.require_run_products()
    ev.require_layer(name_contains="outfall", role="context")
    return 0


if __name__ == "__main__":
    sys.exit(main())
