#!/usr/bin/env python
"""Live driver: a user_gated ``telemac_river_dye`` answered through the CARDS.

The FORM card's first live proof. ``telemac_river_dye`` declares a ``FormGate``
over its own param sheet and a ``DrawGate`` for the release point, so this run
exercises both:

  * the FORM card fires with the resolved sheet and ONE row is edited
    (``dye_concentration_mgl``), and the run's persisted metrics have to show the
    edited value reached the physics;
  * the DRAW card is answered with a real point on the Eel River reach, so the
    release is a USER value and the plume starts where the user clicked.

The evidence is the run's OWN artifacts under its prefix (``chart_spec.json``,
``metrics.json``). Nothing here is rederived.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_river_dye_cards.py [--timeout 1800] [--out evidence.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trid3nt_server.testing import GateAnswers, LiveRun, run_live  # noqa: E402

#: A real NHDPlus reach WITH NHDArea polygon coverage (the bank_source precondition).
LOCATION = "Eel River near Scotia, California"
#: The USGS Eel River at Scotia gage (11477000) - a real point on the reach.
RELEASE_LONLAT = [-124.0983, 40.4921]
#: The one row the form card edits. The source concentration is the cleanest
#: check that an edit REACHED the physics: the peak concentration scales with it.
FORM_EDIT = {"dye_concentration_mgl": 250.0}

RUN = LiveRun(
    tool="telemac_river_dye",
    args={
        "location": LOCATION,
        "substance": "dye",
        "spill_fraction": 0.25,
        "spill_duration_s": 300.0,
        "dye_concentration_mgl": 100.0,
        "reach_length_km": 6.0,
        "sim_duration_s": 3600.0,
        "source_q_m3s": 8.0,
        "channel_width_m": 60.0,
        "mesh_resolution": "auto",
        "discharge_m3s": 2.2,
        "input_mode": "user_gated",
    },
    case_title="proof: telemac river dye (Eel River near Scotia, cards)",
    answers=GateAnswers(draw=RELEASE_LONLAT, draw_geometry="point",
                        form_edits=FORM_EDIT, require_draw=True,
                        require_form=True),
    cleanup_case=True,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default="/tmp/river_dye_cards_evidence.json")
    ns = ap.parse_args()

    ev = run_live(LiveRun(**{**RUN.__dict__, "timeout_s": ns.timeout}))
    with open(ns.out, "w", encoding="utf-8") as fh:
        json.dump(ev.as_dict(), fh, indent=2, default=str)

    form = ev.form_card or {}
    print(json.dumps({
        "tool_status": ev.tool_status,
        "turn_complete": ev.turn_complete,
        "draw_card": ev.draw_card,
        "form_card_rows": len(form.get("rows", [])),
        "form_card_title": form.get("title"),
        "form_edit": form.get("edited"),
        "form_rows": form.get("rows"),
        "release_layers": [l for l in ev.layers
                           if "release" in str(l.get("name", "")).lower()],
        "mesh_layers": [l for l in ev.layers if l.get("layer_type") == "mesh"],
        "run_id": ev.run_id,
        "product_uris": ev.product_uris,
        "product_errors": ev.product_errors,
        "charts_emitted": ev.charts,
        "dye_cmax_mgl": (ev.metrics or {}).get("dye_cmax_mgl"),
        "dye_peak_time_s": (ev.metrics or {}).get("dye_peak_time_s"),
        "plume_reach_m": (ev.metrics or {}).get("plume_reach_m"),
        "active_frames": (ev.metrics or {}).get("active_frames"),
        "detail": ev.detail,
        "evidence": ns.out,
    }, indent=2, default=str))

    ev.require_ok()
    ev.require_run_products()
    ev.require_layer(name_contains="release", role="context")
    ev.require_layer(layer_type="mesh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
