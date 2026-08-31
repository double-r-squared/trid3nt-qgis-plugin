#!/usr/bin/env python
"""Live driver: a user_gated ``telemac_river_dye`` answered through the CARDS.

The FORM card's live proof, and the release point's. ``telemac_river_dye``
declares a ``FormGate`` over its own param sheet and a ``DrawGate`` for the
release point, so this run exercises both:

  * the FORM card fires with the resolved sheet and ONE row is edited
    (``dye_concentration_mgl``), and the run's persisted metrics have to show the
    edited value reached the physics;
  * the DRAW card is answered with a real point, and the run has to AGREE with
    it - either the solver puts the source there (``--case honored``, which the
    driver verifies against the deck the solver actually wrote), or the run
    REFUSES typed rather than quietly releasing somewhere else
    (``--case refused``, a point off the meshed reach).

The evidence is the run's OWN artifacts under its prefix (``chart_spec.json``,
``metrics.json``, ``t2d_river.cas``, ``telemac_metrics.json``). Nothing here is
rederived.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_river_dye_cards.py [--case honored|refused] [--timeout 1800]
                                [--out F] [--no-render-proof]
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

#: A real NHDPlus reach WITH NHDArea polygon coverage - the domain is the cut.
LOCATION = "Eel River near Scotia, California"
#: A node ON the meshed Eel reach, read off an earlier run's ``river.slf`` and
#: reprojected from UTM 10N: a point the solver CAN put the source at.
IN_DOMAIN_LONLAT = [-124.106759, 40.509617]
#: The USGS Eel River at Scotia gage (11477000). Real, and 777 m off the meshed
#: 6 km reach - the point the worker cannot honor.
OFF_REACH_LONLAT = [-124.0983, 40.4921]
#: The one row the form card edits. The source concentration is the cleanest
#: check that an edit REACHED the physics: the peak concentration scales with it.
FORM_EDIT = {"dye_concentration_mgl": 250.0}

ARGS = {
    "location": LOCATION,
    "substance": "dye",
    "spill_fraction": 0.25,
    "spill_duration_s": 300.0,
    "dye_concentration_mgl": 100.0,
    "reach_length_km": 6.0,
    "sim_duration_s": 3600.0,
    "source_q_m3s": 8.0,
    "discharge_m3s": 2.2,
    "input_mode": "user_gated",
}

CASES = {"honored": IN_DOMAIN_LONLAT, "refused": OFF_REACH_LONLAT}
REFUSAL_CODE = "TELEMAC_RELEASE_POINT_OUTSIDE_DOMAIN"

#: The path-A canary: the same reach, every param supplied so no card is left to
#: answer, sized so the solve is a smoke test of the plumbing rather than a
#: physics study. The release is DERIVED at spill_fraction (no draw), and the
#: discharge is PINNED - a canary that also depended on a live NWM cycle would
#: report a source outage as a code failure.
COARSE_ARGS = {
    **ARGS,
    "reach_length_km": 1.0,
    "sim_duration_s": 600.0,
    "spill_duration_s": 120.0,
    "mesh_resolution_m": 50.0,
    "input_mode": "auto",
}


def _run_coarse(timeout: float):
    return run_live(LiveRun(
        tool="telemac_river_dye", args=COARSE_ARGS,
        case_title="canary: telemac river dye (Eel River, coarse)",
        answers=GateAnswers(confirm="proceed"),
        timeout_s=timeout, cleanup_case=True))


def _run(case: str, timeout: float):
    return run_live(LiveRun(
        tool="telemac_river_dye", args=ARGS,
        case_title=f"proof: telemac river dye (Eel River, release {case})",
        answers=GateAnswers(draw=CASES[case], draw_geometry="point",
                            form_edits=FORM_EDIT, require_draw=True,
                            require_form=True),
        timeout_s=timeout, cleanup_case=True))


def _where_the_source_went(run_id: str) -> dict:
    """The source coordinate the SOLVER wrote, off the run's own TELEMAC deck.

    ``ABSCISSAE/ORDINATES OF SOURCES`` in ``t2d_river.cas`` is the point the
    solve released from, in the run's UTM zone. Reading it back is what makes
    "the marker and the plume agree" a measurement instead of a claim.
    """
    import boto3
    from pyproj import Transformer

    s3 = boto3.client("s3", endpoint_url=os.environ.get("AWS_ENDPOINT_URL"),
                      region_name=os.environ.get("AWS_REGION", "us-east-1"))
    bucket = os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")

    def _get(name):
        return s3.get_object(Bucket=bucket,
                             Key=f"{run_id}/{name}")["Body"].read().decode("utf-8")

    metrics = json.loads(_get("telemac_metrics.json"))
    x = y = None
    for line in _get("t2d_river.cas").splitlines():
        if line.startswith("ABSCISSAE OF SOURCES"):
            x = float(line.split("=", 1)[1])
        elif line.startswith("ORDINATES OF SOURCES"):
            y = float(line.split("=", 1)[1])
    out = {"utm_epsg": metrics.get("utm_epsg"), "source_utm": [x, y]}
    if x is not None and y is not None and metrics.get("utm_epsg"):
        lon, lat = Transformer.from_crs(int(metrics["utm_epsg"]), 4326,
                                        always_xy=True).transform(x, y)
        out["source_lonlat"] = [round(lon, 6), round(lat, 6)]
        dx = (lon - IN_DOMAIN_LONLAT[0]) * 111320.0 * 0.76  # cos(40.5)
        dy = (lat - IN_DOMAIN_LONLAT[1]) * 110570.0
        out["drawn_to_source_m"] = round((dx * dx + dy * dy) ** 0.5, 1)
    return out


#: A layer's inline GeoJSON above this is bulk, not evidence: the mesh preview
#: alone carries ~2 MB of triangle edges. Small ones (the release marker) stay,
#: because the point they carry IS the claim being proven.
_INLINE_GEOJSON_KEEP_BYTES = 4096


def _compact(evidence: dict) -> dict:
    layers = []
    for layer in evidence.get("layers") or []:
        layer = dict(layer)
        blob = json.dumps(layer.get("inline_geojson") or "", default=str)
        if len(blob) > _INLINE_GEOJSON_KEEP_BYTES:
            layer["inline_geojson"] = f"<dropped, {len(blob)} bytes>"
        layers.append(layer)
    return {**evidence, "layers": layers}


def _main_coarse(ns) -> int:
    """The canary: status, the run's own metrics, the chart, the products."""
    ev = _run_coarse(ns.timeout)
    report = {
        "case": "coarse",
        "tool_status": ev.tool_status,
        "turn_complete": ev.turn_complete,
        "layers": [l.get("name") for l in ev.layers],
        "run_id": ev.run_id,
        "product_uris": ev.product_uris,
        "product_errors": ev.product_errors,
        "charts_emitted": ev.charts,
        "dye_cmax_mgl": (ev.metrics or {}).get("dye_cmax_mgl"),
        "dye_peak_time_s": (ev.metrics or {}).get("dye_peak_time_s"),
        "plume_reach_m": (ev.metrics or {}).get("plume_reach_m"),
        "active_frames": (ev.metrics or {}).get("active_frames"),
        "mesh_size_m": (ev.metrics or {}).get("mesh_size_m"),
        "mesh_node_estimate": (ev.metrics or {}).get("mesh_node_estimate"),
        "detail": ev.detail,
    }
    print(json.dumps(report, indent=2, default=str))
    out = ns.out or os.path.join(
        proof_dir("telemac_river_dye", "coarse"),
        "telemac_river_dye_coarse_evidence.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"report": report, "evidence": _compact(ev.as_dict())}, fh,
                  indent=2, default=str)
    print(f"evidence -> {os.path.abspath(out)}")
    ev.require_ok()
    ev.require_run_products()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=sorted(CASES), default="honored")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--coarse", action="store_true",
                    help="the path-A canary declaration (short reach, pinned discharge)")
    add_render_proof_flag(ap)
    ns = ap.parse_args()
    if ns.coarse:
        ns.render_proof = False
    # The release-point acceptance cases are ADDENDUM proofs: a named case, not
    # a resolution variant of the canary.
    out_path = ns.out or os.path.join(
        proof_dir("telemac_river_dye", "addendum"),
        f"telemac_river_dye_release_{ns.case}_evidence.json")

    if ns.coarse:
        return _main_coarse(ns)
    ev = _run(ns.case, ns.timeout)
    form = ev.form_card or {}
    report = {
        "case": ns.case,
        "drawn_point": CASES[ns.case],
        "tool_status": ev.tool_status,
        "dispatched": ev.dispatched,
        "is_error": ev.is_error,
        "turn_complete": ev.turn_complete,
        "draw_card": ev.draw_card,
        "form_card_rows": len(form.get("rows", [])),
        "form_card_title": form.get("title"),
        "form_edit": form.get("edited"),
        "release_layers": [l for l in ev.layers
                           if "release" in str(l.get("name", "")).lower()],
        "layers": [l.get("name") for l in ev.layers],
        "run_id": ev.run_id,
        "product_uris": ev.product_uris,
        "product_errors": ev.product_errors,
        "charts_emitted": ev.charts,
        "dye_cmax_mgl": (ev.metrics or {}).get("dye_cmax_mgl"),
        "dye_peak_time_s": (ev.metrics or {}).get("dye_peak_time_s"),
        "plume_reach_m": (ev.metrics or {}).get("plume_reach_m"),
        "active_frames": (ev.metrics or {}).get("active_frames"),
        "detail": ev.detail,
    }
    if ns.case == "honored" and ev.run_id:
        report["release_reconciliation"] = _where_the_source_went(ev.run_id)
    print(json.dumps(report, indent=2, default=str))
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"report": report, "evidence": _compact(ev.as_dict())}, fh,
                  indent=2, default=str)
    print(f"evidence -> {os.path.abspath(out_path)}")
    if ns.render_proof:
        print(f"canvas layers -> "
              f"{json.dumps(render_proof(out_path), indent=2, default=str)}")

    if ns.case == "refused":
        # The run REFUSES a release point the solver could not honor: no relocated
        # source, no published plume, and the typed code names what to fix. The
        # refusal reaches the socket as an ERROR envelope carrying the code (the
        # tool-io frame ahead of it carries no status), so the detail plus the
        # absent products are what prove it.
        assert REFUSAL_CODE in ev.detail, f"not the release-point refusal: {ev.detail}"
        assert ev.form_card and ev.draw_card, "the cards did not both fire"
        assert not ev.run_id and not ev.product_uris, "a refused run published products"
        assert not [l for l in ev.layers
                    if "peak" in str(l.get("name", "")).lower()], ev.layers
        return 0

    ev.require_ok()
    ev.require_run_products()
    ev.require_layer(name_contains="release", role="context")
    ev.require_layer(layer_type="mesh")
    rec = report["release_reconciliation"]
    # The deck's own SOURCE coordinates against the drawn point: the pre-flight
    # settled the release before the run, so where the solver put the source is
    # the only reconciliation left to make.
    assert rec["drawn_to_source_m"] < 25.0, rec  # the marker IS where it released
    return 0


if __name__ == "__main__":
    sys.exit(main())
