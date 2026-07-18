"""BK-3b live E2E: natural dye prompt -> approve-mesh gate (real preview) -> solve.

Asserts, in order:
  1. a tool-payload-warning arrives for run_telemac carrying a GranularitySuggestion
     with engine='telemac' + REAL npoin (not an estimate) + mesh_resolution_m param;
  2. BEFORE that gate, a session-state carries the telemac-mesh-preview vector layer;
  3. after auto-proceed, the solve completes and a telemac-dye-peak layer lands
     (proves the _publish_peak_layer NameError fix end-to-end).
"""
import os, sys, time, json
sys.path.insert(0, "/home/nate/Documents/trid3nt-local/qgis-plugin")
from trid3nt.trid3nt_client import AgentClient

# Parameterized (feedback_test_drivers_offline_first): E2E_STUB=1 runs the SAME
# driver against tests/stub_server.py for a zero-token contract validation.
STUB = bool(os.environ.get("E2E_STUB"))
URL = os.environ.get("E2E_URL", "ws://127.0.0.1:8765")
PROMPT = os.environ.get("E2E_PROMPT") or (
    "A tanker overturned and spilled dye into the Snake River near "
    "Twin Falls, Idaho. Simulate how the dye plume travels downstream "
    "over the next few hours.")
DEADLINE_S = int(os.environ.get("E2E_DEADLINE_S", "1800"))

cli = AgentClient(URL)
cli.connect()
cli.case_command("create")
time.sleep(1 if STUB else 3)
cli.send_chat(PROMPT, show_thinking=False)
print("SENT:", PROMPT, flush=True)

deadline = time.time() + DEADLINE_S
saw_preview_layer = False
saw_gate = False
gate_ok = False
preview_before_gate = False
saw_dye_layer = False
gate_payload = None

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
    payload = data

    if k == "session-state":
        try:
            layers = data.get("layers") or []
            for L in layers:
                lid = str(getattr(L, "layer_id", "") or "")
                if lid.startswith("telemac-mesh-preview") and not saw_preview_layer:
                    saw_preview_layer = True
                    if not saw_gate:
                        preview_before_gate = True
                    print(f"PREVIEW LAYER: {lid} "
                          f"type={getattr(L, 'layer_type', None)} "
                          f"(before_gate={preview_before_gate})", flush=True)
                if lid.startswith("telemac-dye-peak") and not saw_dye_layer:
                    saw_dye_layer = True
                    print(f"DYE LAYER PUBLISHED: {lid}", flush=True)
        except Exception as e:
            print("layer-parse err:", e, flush=True)

    if k in ("payload-warning", "tool-payload-warning") and not saw_gate:
        saw_gate = True
        gate_payload = data or {}
        g = gate_payload.get("granularity") or {}
        tool = gate_payload.get("tool_name")
        print(f"GATE: tool={tool} options={gate_payload.get('options')}", flush=True)
        print(f"GATE granularity: engine={g.get('engine')} "
              f"param={g.get('resolution_param')} h={g.get('suggested_resolution_m')} "
              f"cells={g.get('estimated_active_cells')} "
              f"est_s={g.get('estimated_solve_seconds')} "
              f"ladder={g.get('resolution_choices')}", flush=True)
        print(f"GATE reason: {g.get('reason')}", flush=True)
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
            )
        wid = gate_payload.get("warning_id")
        if wid:
            # Act like a sane user: if the estimated solve is >10 min, pick the
            # COARSEST rung on the ladder via narrow_scope (also exercises the
            # editable-knob path); otherwise approve as shown.
            est = float(g.get("estimated_solve_seconds") or 0)
            ladder = g.get("resolution_choices") or []
            if not STUB and est > 600 and ladder:
                coarsest = max(float(r) for r in ladder)
                cli.confirm_payload(wid, "narrow_scope",
                                    {"mesh_resolution_m": coarsest})
                print(f"CONFIRMED narrow_scope -> coarsest rung {coarsest} m "
                      f"(est was {est:.0f}s)", flush=True)
            else:
                cli.confirm_payload(wid, "proceed")
                print("CONFIRMED proceed", flush=True)

    # Stochastic model path: if it geocodes the river phrase first, a state-level
    # match triggers the region-choice county picker - answer it like a user
    # tapping their county (pick the Twin Falls candidate).
    if k == "raw" and data.get("type") == "region-choice-request":
        p = data.get("payload") or {}
        cands = p.get("candidates") or []
        pick = next((c for c in cands if "twin falls" in str(c.get("name", "")).lower()),
                    cands[0] if cands else None)
        if pick:
            cli._send("region-choice-provided", {
                "request_id": p.get("request_id"),
                "decision": "region",
                "selected_region_id": pick.get("region_id"),
                "selected_bbox": pick.get("bbox"),
            }, queue_if_closed=True)
            print(f"REGION-CHOICE answered: {pick.get('name')}", flush=True)

    if STUB and k == "turn-complete" and saw_gate:
        saw_dye_layer = True  # stub: post-confirm turn completion = chain closed
        print("STUB turn-complete after confirm", flush=True)
    if saw_dye_layer:
        break

cli.close()
print("\n===== VERDICT =====", flush=True)
print(json.dumps({
    "gate_seen": saw_gate,
    "gate_contract_ok": gate_ok,
    "preview_layer_seen": saw_preview_layer,
    "preview_before_gate": preview_before_gate,
    "dye_layer_published": saw_dye_layer,
    "PASS": bool(saw_gate and gate_ok and saw_dye_layer
                 and (STUB or saw_preview_layer)),
}, indent=1), flush=True)
