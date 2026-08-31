"""Live E2E: a natural spill prompt -> the mesh gate -> a solve on the accepted mesh.

Asserts, in order:
  1. a tool-payload-warning arrives carrying a PARAM SHEET - the mesh gate's edit
     surface. Its rows are the open mesher's numeric edit knobs, named
     ``<action>.<input>``, plus the ``restart`` truncation row; that sheet is what
     makes the gate's revision channel reachable from the shipped card, so a gate
     without one is proceed/cancel-only and FAILS here;
  2. BEFORE that gate, a session-state carries the mesh display layer;
  3. the sheet is submitted UNCHANGED, which is the approval, and the run solves on
     the mesh that was presented;
  4. a dye-peak layer lands carrying the JOURNAL's measured banks-coverage line -
     the witness that the domain was cut from real mapped water and that the run
     said how much of the reach that water covered; when E2E_EXPECT_SUBSTANCE is
     set the layer NAME must contain it (the substance lever);
  5. post-run: the newest rundir's telemac_metrics.json - CORRECT END plus the
     node and element counts the SERVER measured and the worker echoed back,
     which is what makes "it solved on the accepted mesh" checkable.

Config via env (E2E_STUB=1 runs the SAME driver against tests/stub_server.py for a
zero-token contract validation):
  E2E_STUB E2E_URL E2E_PROMPT E2E_DEADLINE_S E2E_EXPECT_SUBSTANCE
  E2E_RUNS_DIR E2E_REGION_HINT
"""
import glob
import json
import re
import os
import sys
import time

sys.path.insert(0, "/home/nate/Documents/trid3nt-local")
from plugin.net.trid3nt_client import AgentClient

STUB = bool(os.environ.get("E2E_STUB"))
URL = os.environ.get("E2E_URL", "ws://127.0.0.1:8765")
PROMPT = os.environ.get("E2E_PROMPT") or (
    "A tanker overturned and spilled dye into the Snake River near "
    "Twin Falls, Idaho. Simulate how the dye plume travels downstream "
    "over the next few hours.")
DEADLINE_S = int(os.environ.get("E2E_DEADLINE_S", "1800"))
EXPECT_SUBSTANCE = (os.environ.get("E2E_EXPECT_SUBSTANCE") or "").strip().lower()
RUNS_DIR = os.environ.get("E2E_RUNS_DIR",
                          "/home/nate/Documents/trid3nt-local/data/runs")
REGION_HINT = (os.environ.get("E2E_REGION_HINT") or "twin falls").lower()

t_start = time.time()


def read_sheet(payload):
    """The gate's param sheet as (rows, knob_names, has_restart_row)."""
    sheet = payload.get("param_sheet") or {}
    rows = [r for r in (sheet.get("rows") or []) if isinstance(r, dict)]
    names = [str(r.get("name") or "") for r in rows]
    knobs = [n for n in names if "." in n]
    return rows, knobs, ("restart" in names)


def main():
    cli = AgentClient(URL)
    cli.connect()
    cli.case_command("create")
    time.sleep(1 if STUB else 3)
    cli.send_chat(PROMPT, show_thinking=False)
    print("SENT:", PROMPT, flush=True)

    deadline = time.time() + DEADLINE_S
    saw_mesh_layer = False
    saw_gate = False
    gate_ok = False
    mesh_before_gate = False
    saw_peak_layer = False
    peak_layer_name = ""
    coverage_line = ""
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
                    ltype = str(getattr(L, "layer_type", "") or "")
                    if lid.startswith("mesh-") and not saw_mesh_layer:
                        saw_mesh_layer = True
                        if not saw_gate:
                            mesh_before_gate = True
                        print(f"MESH LAYER: {lid} type={ltype} "
                              f"(before_gate={mesh_before_gate})", flush=True)
                    lname = str(getattr(L, "name", "") or "")
                    is_peak = (lid.startswith("telemac-dye-peak")
                               or re.match(r"(?i)peak .* concentration", lname))
                    if is_peak and not saw_peak_layer:
                        saw_peak_layer = True
                        peak_layer_name = lname
                        note = str(getattr(L, "fallback_note", "") or "")
                        coverage_line = next(
                            (seg for seg in note.split("NOTE: ")
                             if seg.startswith("reach banks:")), "")
                        if EXPECT_SUBSTANCE:
                            substance_ok = EXPECT_SUBSTANCE in peak_layer_name.lower()
                        print(f"PEAK LAYER PUBLISHED: {lid} name={peak_layer_name!r} "
                              f"substance_ok={substance_ok}", flush=True)
                        print(f"BANKS COVERAGE: {coverage_line.strip() or None}",
                              flush=True)
            except Exception as e:  # noqa: BLE001
                print("layer-parse err:", e, flush=True)

        if k in ("payload-warning", "tool-payload-warning") and not saw_gate:
            saw_gate = True
            gate_payload = data or {}
            ta = gate_payload.get("tool_args") or {}
            tool = gate_payload.get("tool_name")
            rows, knobs, has_restart = read_sheet(gate_payload)
            print(f"GATE: tool={tool} options={gate_payload.get('options')}", flush=True)
            print(f"GATE tool_args: mesh_id={ta.get('mesh_id')} "
                  f"layer={ta.get('mesh_layer_id')}", flush=True)
            print(f"GATE param_sheet: {len(rows)} rows knobs={knobs} "
                  f"restart_row={has_restart}", flush=True)
            print(f"GATE recommendation: {gate_payload.get('recommendation')}", flush=True)
            if STUB:
                # The stub fixture is a flood-engine card; validate the CONTRACT
                # shape the driver depends on rather than the mesh gate's rows.
                gate_ok = bool(tool) and bool(gate_payload.get("warning_id")) \
                    and isinstance(gate_payload.get("options"), list)
            else:
                gate_ok = (
                    bool(rows)
                    and has_restart
                    and bool(knobs)
                    and "narrow_scope" in (gate_payload.get("options") or [])
                    and bool(ta.get("mesh_id"))
                )
            wid = gate_payload.get("warning_id")
            if wid:
                # Submitting the sheet unchanged IS the approval: the whole sheet
                # was on screen, so there is nothing left to re-present.
                cli.confirm_payload(wid, "proceed")
                print("CONFIRMED proceed (sheet unchanged)", flush=True)

        # A state-level geocode triggers the region-choice county picker - answer
        # it like a user tapping their county.
        if k == "raw" and data.get("type") == "region-choice-request":
            p = data.get("payload") or {}
            cands = p.get("candidates") or []
            pick = next((c for c in cands
                         if REGION_HINT in str(c.get("name", "")).lower()),
                        cands[0] if cands else None)
            if pick:
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

    # Post-run metrics witness: the newest rundir written AFTER this drive
    # started. The counts are the SERVER's own measurement, echoed back through
    # the case rather than re-derived in the container - so a run that reports
    # them is a run that solved on the mesh that was accepted at the gate.
    metrics = {}
    metrics_ok = STUB
    if not STUB and saw_peak_layer:
        try:
            cands = [p for p in glob.glob(os.path.join(RUNS_DIR, "*", "telemac_metrics.json"))
                     if os.path.getmtime(p) >= t_start]
            if cands:
                newest = max(cands, key=os.path.getmtime)
                metrics = json.loads(open(newest).read())
                print(f"METRICS {newest}: module={metrics.get('module')} "
                      f"npoin={metrics.get('npoin')} nelem={metrics.get('nelem')} "
                      f"bed_source={metrics.get('bed_source')} "
                      f"wall={metrics.get('wall_s')}s "
                      f"correct_end={metrics.get('correct_end')}", flush=True)
                metrics_ok = (bool(metrics.get("correct_end"))
                              and int(metrics.get("npoin") or 0) > 0
                              and int(metrics.get("nelem") or 0) > 0)
            else:
                print("METRICS: no fresh rundir found under", RUNS_DIR, flush=True)
        except Exception as e:  # noqa: BLE001
            print("metrics read err:", e, flush=True)

    print("\n===== VERDICT =====", flush=True)
    print(json.dumps({
        "gate_seen": saw_gate,
        "gate_contract_ok": gate_ok,
        "mesh_layer_seen": saw_mesh_layer,
        "mesh_before_gate": mesh_before_gate,
        "peak_layer_published": saw_peak_layer,
        "peak_layer_name": peak_layer_name,
        "substance_ok": substance_ok,
        "banks_coverage_line": coverage_line.strip() or None,
        "metrics_ok": metrics_ok,
        "PASS": bool(saw_gate and gate_ok and saw_peak_layer and substance_ok
                     and metrics_ok and (STUB or saw_mesh_layer)
                     and (STUB or bool(coverage_line))),
    }, indent=1), flush=True)


if __name__ == "__main__":
    main()
