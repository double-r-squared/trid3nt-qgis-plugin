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

``--coarse`` is the CANARY form of the same declaration: every param supplied
up front (path A - the gates are satisfied rather than skipped) on a short
reach, a short simulated window and a pinned discharge, so a library or shared-
step change can be proven end-to-end through the product path in minutes rather
than the half hour the showcase run takes. It writes its own evidence file and
never renders the canonical proof set.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_do_sag_cards.py [--timeout 1800] [--out evidence.json]
                             [--no-render-proof] [--coarse]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from render_all_layers_proof import add_render_proof_flag, render_proof  # noqa: E402
from trid3nt_server.testing import GateAnswers, LiveRun, run_live  # noqa: E402
from trid3nt_server.testing.proof_paths import proof_dir  # noqa: E402

#: The gate-CARDS walkthrough is an ADDENDUM proof, not the canary's coarse
#: baseline: it is a different case (a different reach, a drawn outfall) asked
#: for a different reason. Its folder says so.
EVIDENCE = os.path.join(proof_dir("telemac_do_sag", "addendum"),
                        "telemac_do_sag_cards_evidence.json")

#: A real NHDPlus reach WITH NHDArea polygon coverage - the domain is the cut.
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
        "input_mode": "user_gated",
    },
    case_title="showcase: telemac do sag (Eel River near Scotia, cards)",
    answers=GateAnswers(draw=OUTFALL_LONLAT, draw_geometry="point",
                        require_draw=True, confirm="proceed"),
)

#: The path-A canary: the same reach and outfall, every param supplied so no
#: card is left to answer, sized so the solve is a smoke test of the plumbing
#: rather than a physics study. The discharge is PINNED - a canary that also
#: depended on a live NWM cycle would report a source outage as a code failure.
COARSE = LiveRun(
    tool="telemac_do_sag",
    args={
        **RUN.args,
        "outfall_coords": OUTFALL_LONLAT,
        "reach_length_km": 0.5,
        "sim_duration_s": 600.0,
        # A 2D reach wants ~10 elements across the channel to route flow at all,
        # and the channel is ~150 m wide here. Coarser than this the clean chain
        # takes the ribbon apart: the boundary-face pass sees a one-element-wide
        # strip as slivers and removes every element.
        "mesh_resolution_m": 12.0,
        "discharge_m3s": 60.0,
        "input_mode": "auto",
    },
    case_title="canary: telemac do sag (Eel River near Scotia, coarse)",
    answers=GateAnswers(confirm="proceed"),
    cleanup_case=True,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default=EVIDENCE)
    # The storm/event moment to read the carrier discharge cycle at (ADR 0309).
    # Unset (the committed showcase run) reads the most recent published NWM
    # cycle; an explicit ISO date/datetime pins an older cycle for the A/B
    # comparison - pass --no-render-proof alongside it so the B run never
    # overwrites the canonical (latest-cycle) proof set.
    ap.add_argument("--event-time", default=None)
    ap.add_argument("--coarse", action="store_true",
                    help="the path-A canary declaration (short reach, pinned discharge)")
    add_render_proof_flag(ap)
    ns = ap.parse_args()

    run = RUN
    if ns.coarse:
        run = COARSE
        ns.render_proof = False
        if ns.out == EVIDENCE:
            ns.out = EVIDENCE.replace(".json", "_coarse.json")
    if ns.event_time:
        run = LiveRun(**{**RUN.__dict__, "args": {**RUN.args, "event_time": ns.event_time}})

    ev = run_live(LiveRun(**{**run.__dict__, "timeout_s": ns.timeout}))
    with open(ns.out, "w", encoding="utf-8") as fh:
        json.dump(ev.as_dict(), fh, indent=2, default=str)
    sheet = render_proof(ns.out) if ns.render_proof else None

    station_layers = [l for l in ev.layers
                      if "discharge station" in str(l.get("name", "")).lower()]

    print(json.dumps({
        "canvas_layers_sheet": sheet,
        "tool_status": ev.tool_status,
        "turn_complete": ev.turn_complete,
        "draw_card": ev.draw_card,
        "form_card_rows": len((ev.form_card or {}).get("rows", [])),
        "plain_warnings": ev.plain_warnings,
        "outfall_layers": [l for l in ev.layers
                           if "outfall" in str(l.get("name", "")).lower()],
        "station_layers": station_layers,
        "run_id": ev.run_id,
        "product_uris": ev.product_uris,
        "product_errors": ev.product_errors,
        "do_min_mgl": (ev.metrics or {}).get("do_min_mgl"),
        "do_min_distance_m": (ev.metrics or {}).get("do_min_distance_m"),
        "do_violates_standard": (ev.metrics or {}).get("do_violates_standard"),
        "discharge_m3s": (ev.metrics or {}).get("discharge_m3s"),
        "discharge_note": (ev.metrics or {}).get("discharge_note"),
        "detail": ev.detail,
        "evidence": ns.out,
    }, indent=2, default=str))

    ev.require_ok()
    ev.require_run_products()
    ev.require_layer(name_contains="outfall", role="context")
    return 0


if __name__ == "__main__":
    sys.exit(main())
