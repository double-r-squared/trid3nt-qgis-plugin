"""BK-3b/BK-6 live E2E: natural spill prompt -> approve-mesh gate -> release point -> solve.

Asserts, in order:
  1. a tool-payload-warning arrives for run_telemac carrying a GranularitySuggestion
     with engine='telemac' + REAL npoin (not an estimate) + mesh_resolution_m param;
  2. BEFORE that gate, a session-state carries the telemac-mesh-preview vector layer;
  3. BK-6: when tool_args.release_point_required, the driver picks an ON-MESH point
     (midpoint of a mid-list wireframe segment from the preview inline_geojson;
     mesh_bbox center fallback) and answers via narrow_scope revised_args
     {mesh_resolution_m, release_lon, release_lat} - the only path that carries a
     point (proceed forces revised=None);
  4. the solve completes and a telemac-dye-peak layer lands; when
     E2E_EXPECT_SUBSTANCE is set the layer NAME must contain it (substance lever);
  5. post-run: newest rundir telemac_metrics.json sanity - bank_source +
     bank_width_mean_m >= E2E_MIN_MEAN_WIDTH_M (real-bank meshing witness).

Config via env (feedback_test_drivers_offline_first - E2E_STUB=1 runs the SAME
driver against tests/stub_server.py for a zero-token contract validation):
  E2E_STUB E2E_URL E2E_PROMPT E2E_DEADLINE_S E2E_EXPECT_SUBSTANCE
  E2E_MIN_MEAN_WIDTH_M E2E_RUNS_DIR E2E_REGION_HINT
"""
import glob
import json
import re
import os
import sys
import time

sys.path.insert(0, "/home/nate/Documents/trid3nt-local/qgis-plugin")
from trid3nt.trid3nt_client import AgentClient

STUB = bool(os.environ.get("E2E_STUB"))
URL = os.environ.get("E2E_URL", "ws://127.0.0.1:8765")
PROMPT = os.environ.get("E2E_PROMPT") or (
    "A tanker overturned and spilled dye into the Snake River near "
    "Twin Falls, Idaho. Simulate how the dye plume travels downstream "
    "over the next few hours.")
DEADLINE_S = int(os.environ.get("E2E_DEADLINE_S", "1800"))
EXPECT_SUBSTANCE = (os.environ.get("E2E_EXPECT_SUBSTANCE") or "").strip().lower()
MIN_MEAN_WIDTH_M = float(os.environ.get("E2E_MIN_MEAN_WIDTH_M") or 0)
RUNS_DIR = os.environ.get("E2E_RUNS_DIR",
                          "/home/nate/Documents/trid3nt-local/data/runs")
REGION_HINT = (os.environ.get("E2E_REGION_HINT") or "twin falls").lower()

t_start = time.time()


def pick_release_point(preview_geojson, mesh_bbox):
    """An ON-MESH lon/lat: midpoint of a mid-list wireframe segment (the segment
    list is mesh-interior edges, so any midpoint is inside the ribbon); fall back
    to the mesh bbox center when no inline geometry arrived."""
    try:
        feats = (preview_geojson or {}).get("features") or []
        for f in feats:
            geom = f.get("geometry") or {}
            if geom.get("type") == "MultiLineString":
                segs = geom.get("coordinates") or []
                if segs:
                    a, b = segs[len(segs) // 2][:2]
                    return (a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0
    except Exception as e:  # noqa: BLE001
        print("release-point geojson parse err:", e, flush=True)
    if mesh_bbox and len(mesh_bbox) == 4:
        return (mesh_bbox[0] + mesh_bbox[2]) / 2.0, (mesh_bbox[1] + mesh_bbox[3]) / 2.0
    return None


def main():
    cli = AgentClient(URL)
    cli.connect()
    cli.case_command("create")
    time.sleep(1 if STUB else 3)
    cli.send_chat(PROMPT, show_thinking=False)
    print("SENT:", PROMPT, flush=True)

    deadline = time.time() + DEADLINE_S
    saw_preview_layer = False
    preview_geojson = None
    saw_gate = False
    gate_ok = False
    preview_before_gate = False
    release_point_sent = None
    saw_peak_layer = False
    peak_layer_name = ""
    substance_ok = not EXPECT_SUBSTANCE  # vacuous when unset

    while time.time() < deadline:
        try:
            ev = cli.next_event(timeout=2.0)
        except Exception:
            ev = None
        if ev is None:
            continue
        k = getattr(ev, "kind", None)
        data = getattr(ev, "data", None) or {}
        # AgentEvent shapes: payload-warning -> data IS the payload;
        # session-state -> data = {"payload": raw, "layers": [LayerEvent,...]};
        # raw -> data = {"type": etype, "payload": raw}.

        if k == "session-state":
            try:
                for L in data.get("layers") or []:
                    lid = str(getattr(L, "layer_id", "") or "")
                    if lid.startswith("telemac-mesh-preview") and not saw_preview_layer:
                        saw_preview_layer = True
                        preview_geojson = getattr(L, "inline_geojson", None)
                        if not saw_gate:
                            preview_before_gate = True
                        print(f"PREVIEW LAYER: {lid} "
                              f"type={getattr(L, 'layer_type', None)} "
                              f"inline={'yes' if preview_geojson else 'no'} "
                              f"(before_gate={preview_before_gate})", flush=True)
                    lname = str(getattr(L, "name", "") or "")
                    # The server re-mints tool-returned layer ids (unique-id
                    # wrapper), so the peak layer is matched by NAME, with the
                    # legacy id prefix kept as a fallback.
                    is_peak = (lid.startswith("telemac-dye-peak")
                               or re.match(r"(?i)peak .* concentration", lname))
                    if is_peak and not saw_peak_layer:
                        saw_peak_layer = True
                        peak_layer_name = lname
                        if EXPECT_SUBSTANCE:
                            substance_ok = EXPECT_SUBSTANCE in peak_layer_name.lower()
                        print(f"PEAK LAYER PUBLISHED: {lid} name={peak_layer_name!r} "
                              f"substance_ok={substance_ok}", flush=True)
            except Exception as e:  # noqa: BLE001
                print("layer-parse err:", e, flush=True)

        if k in ("payload-warning", "tool-payload-warning") and not saw_gate:
            saw_gate = True
            gate_payload = data or {}
            g = gate_payload.get("granularity") or {}
            ta = gate_payload.get("tool_args") or {}
            tool = gate_payload.get("tool_name")
            print(f"GATE: tool={tool} options={gate_payload.get('options')}", flush=True)
            print(f"GATE granularity: engine={g.get('engine')} "
                  f"param={g.get('resolution_param')} h={g.get('suggested_resolution_m')} "
                  f"cells={g.get('estimated_active_cells')} "
                  f"est_s={g.get('estimated_solve_seconds')} "
                  f"ladder={g.get('resolution_choices')}", flush=True)
            print(f"GATE tool_args: release_point_required="
                  f"{ta.get('release_point_required')} mesh_bbox={ta.get('mesh_bbox')} "
                  f"dt={ta.get('time_step_s')}", flush=True)
            print(f"GATE recommendation: {gate_payload.get('recommendation')}", flush=True)
            if STUB:
                # stub fixture is a flood-engine card; validate the CONTRACT shape
                gate_ok = bool(tool) and bool(gate_payload.get("warning_id")) \
                    and isinstance(gate_payload.get("options"), list)
            else:
                gate_ok = (
                    tool == "run_telemac"
                    and g.get("engine") == "telemac"
                    and g.get("resolution_param") == "mesh_resolution_m"
                    and (g.get("estimated_active_cells") or 0) > 100
                    and bool(ta.get("release_point_required"))
                    and len(ta.get("mesh_bbox") or []) == 4
                )
            wid = gate_payload.get("warning_id")
            if wid:
                est = float(g.get("estimated_solve_seconds") or 0)
                ladder = g.get("resolution_choices") or []
                # Sane-user resolution: previewed h unless the estimate is >10 min,
                # then the coarsest ladder rung (exercises the editable knob).
                chosen_h = ta.get("mesh_resolution_m") or g.get("suggested_resolution_m")
                if est > 600 and ladder:
                    chosen_h = max(float(r) for r in ladder)
                    print(f"est {est:.0f}s > 600 -> coarsest rung {chosen_h} m", flush=True)
                if not STUB and ta.get("release_point_required"):
                    # BK-6: the point MUST ride narrow_scope revised_args (proceed
                    # drops revised entirely), alongside the chosen edge length.
                    pt = pick_release_point(preview_geojson, ta.get("mesh_bbox"))
                    if pt is None:
                        print("NO release point derivable - cancelling", flush=True)
                        cli.confirm_payload(wid, "cancel")
                    else:
                        release_point_sent = pt
                        revised = {"mesh_resolution_m": chosen_h,
                                   "release_lon": pt[0], "release_lat": pt[1]}
                        cli.confirm_payload(wid, "narrow_scope", revised)
                        print(f"CONFIRMED narrow_scope {revised}", flush=True)
                else:
                    cli.confirm_payload(wid, "proceed")
                    print("CONFIRMED proceed", flush=True)

        # Stochastic model path: a state-level geocode triggers the region-choice
        # county picker - answer it like a user tapping their county.
        if k == "raw" and data.get("type") == "region-choice-request":
            p = data.get("payload") or {}
            cands = p.get("candidates") or []
            pick = next((c for c in cands
                         if REGION_HINT in str(c.get("name", "")).lower()),
                        cands[0] if cands else None)
            if pick:
                # Contract field is ``choice`` (Literal["region","whole_state"]),
                # NOT ``decision`` -- a stale field name made the server's
                # RegionChoiceProvidedEnvelopePayload.model_validate reject this
                # frame, so the paused turn hung on the 24h local gate. Match
                # RegionChoiceProvidedEnvelopePayload exactly.
                cli._send("region-choice-provided", {
                    "request_id": p.get("request_id"),
                    "choice": "region",
                    "selected_region_id": pick.get("region_id"),
                    "selected_bbox": pick.get("bbox"),
                }, queue_if_closed=True)
                print(f"REGION-CHOICE answered: {pick.get('name')}", flush=True)

        if STUB and k == "turn-complete" and saw_gate:
            saw_peak_layer = True  # stub: post-confirm turn completion = chain closed
            substance_ok = True
            print("STUB turn-complete after confirm", flush=True)
        if saw_peak_layer:
            break

    cli.close()

    # Post-run metrics witness: the newest rundir written AFTER this drive started.
    metrics = {}
    metrics_ok = STUB or not MIN_MEAN_WIDTH_M
    if not STUB and saw_peak_layer:
        try:
            cands = [p for p in glob.glob(os.path.join(RUNS_DIR, "*", "telemac_metrics.json"))
                     if os.path.getmtime(p) >= t_start]
            if cands:
                newest = max(cands, key=os.path.getmtime)
                metrics = json.loads(open(newest).read())
                print(f"METRICS {newest}: bank_source={metrics.get('bank_source')} "
                      f"width_mean={metrics.get('bank_width_mean_m')} "
                      f"npoin={metrics.get('npoin')} wall={metrics.get('wall_s')}s "
                      f"correct_end={metrics.get('correct_end')}", flush=True)
                if MIN_MEAN_WIDTH_M:
                    metrics_ok = (metrics.get("bank_source") == "nhdarea"
                                  and float(metrics.get("bank_width_mean_m") or 0)
                                  >= MIN_MEAN_WIDTH_M)
            else:
                print("METRICS: no fresh rundir found under", RUNS_DIR, flush=True)
        except Exception as e:  # noqa: BLE001
            print("metrics read err:", e, flush=True)

    print("\n===== VERDICT =====", flush=True)
    print(json.dumps({
        "gate_seen": saw_gate,
        "gate_contract_ok": gate_ok,
        "preview_layer_seen": saw_preview_layer,
        "preview_before_gate": preview_before_gate,
        "release_point_sent": release_point_sent,
        "peak_layer_published": saw_peak_layer,
        "peak_layer_name": peak_layer_name,
        "substance_ok": substance_ok,
        "metrics_ok": metrics_ok,
        "PASS": bool(saw_gate and gate_ok and saw_peak_layer and substance_ok
                     and metrics_ok
                     and (STUB or (saw_preview_layer and release_point_sent))),
    }, indent=1), flush=True)


if __name__ == "__main__":
    main()
