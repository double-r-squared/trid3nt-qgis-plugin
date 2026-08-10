#!/usr/bin/env python3
"""Live proof for generate_mesh mode=hecras + the RoG consume gate (ADR 0211).

Drives the live daemon through the SAME headless WS path the QGIS plugin + the
seed_showcase driver use (reuses those proven client helpers -- no guessed event
shapes). Three paths, honest end-to-end:

  (a) generate_mesh(mesh_mode=hecras) on Coweeta Creek -> a channel-refined HEC-RAS
      cell mesh: an inspectable wireframe layer + a durable artifact in the case;
  (b) hecras_flood_2d rain-on-grid in the SAME case, input_mode=user_gated -> the
      mesh precondition gate FIRES ("use this mesh?"); we accept (proceed) -> the run
      CONSUMES the stored seeds (re-realized, no fresh delineation) + solves;
  (c) hecras_flood_2d rain-on-grid in a FRESH case (no mesh) -> the absent path:
      uniform mesh, solves green (proves decline/absent leaves 0209 unchanged).

Then a reconnect verifies case (a)'s mesh layer persisted. Prints a JSON summary.
ASCII only; never deletes/mutates an existing case.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "qgis-plugin"))

import websockets  # noqa: E402

from seed_showcase_cases import (  # noqa: E402
    WS_URL, mk, run_line, _handshake, _create_case, _parse_tool_status,
)
from trid3nt_contracts import new_ulid  # noqa: E402

COWEETA_BBOX = [-83.47, 35.02, -83.36, 35.10]
POUR_POINT = (-83.40402, 35.05746)


async def _invoke(ws, session_id, case_id, name, args, *, timeout_s, accept_warnings=True):
    """dev-tool-invoke + collect the turn; auto-proceed every payload warning (=accept
    the mesh + proceed input review). Returns a dict of the observed outcome."""
    rl = run_line(name, args)
    print(f"  -> {rl}  (case {case_id})", flush=True)
    await ws.send(mk("dev-tool-invoke", session_id,
                     {"name": name, "args": args, "case_id": case_id, "raw_text": rl},
                     case_id=case_id))
    out = {"tool": name, "run_line": rl, "warnings": [], "tool_status": None,
           "is_error": False, "layers": [], "detail": ""}
    deadline = time.monotonic() + timeout_s
    layers = []
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(deadline - time.monotonic(), 45))
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        mt = msg["type"]
        if mt == "tool-payload-warning":
            rec = (msg["payload"].get("recommendation") or "")[:90]
            out["warnings"].append(rec)
            wid = msg["payload"].get("warning_id")
            decision = "proceed" if accept_warnings else "cancel"
            print(f"     [gate] warning: {rec!r} -> {decision}", flush=True)
            await ws.send(mk("tool-payload-confirmation", session_id,
                             {"warning_id": wid, "decision": decision, "revised_args": None}))
        elif mt == "confirmation-request":
            await ws.send(mk("confirm-response", session_id,
                             {"request_id": msg["payload"].get("request_id"), "approved": True}))
        elif mt == "tool-io":
            out["tool_status"] = _parse_tool_status(msg["payload"])
            if msg["payload"].get("is_error"):
                out["is_error"] = True
                out["detail"] = (msg["payload"].get("function_response", "") or "")[:240]
        elif mt == "session-state":
            ll = msg["payload"].get("loaded_layers") or []
            if ll:
                layers = ll
        elif mt == "error":
            out["is_error"] = True
            out["detail"] = f"{msg['payload'].get('error_code')}: {msg['payload'].get('message')}"
            break
        elif mt == "turn-complete":
            break
    out["layers"] = [{"name": l.get("name"), "type": l.get("layer_type"),
                      "uri": (l.get("uri") or "")[:90]} for l in layers]
    return out


async def main():
    session_id = new_ulid()
    summary = {}
    async with websockets.connect(WS_URL, max_size=64 * 1024 * 1024) as ws:
        await _handshake(ws, session_id)

        # (a) build the HEC-RAS mesh into a fresh case.
        case_a = await _create_case(ws, session_id, "proof: generate_mesh hecras (Coweeta)")
        print(f"[a] generate_mesh hecras  case={case_a}", flush=True)
        summary["a_generate_mesh"] = await _invoke(
            ws, session_id, case_a, "generate_mesh",
            {"mesh_mode": "hecras", "bbox": COWEETA_BBOX,
             "pour_point": list(POUR_POINT), "min_edge_length_m": 22.0,
             "max_edge_length_m": 90.0},
            timeout_s=900)
        summary["a_generate_mesh"]["case_id"] = case_a

        # (b) consume it: RoG in the SAME case, user_gated so the mesh gate emits.
        print(f"[b] hecras_flood_2d RoG (consume) case={case_a}", flush=True)
        summary["b_consume"] = await _invoke(
            ws, session_id, case_a, "hecras_flood_2d",
            {"bbox": COWEETA_BBOX, "design_storm_mm_per_hr": 25.0,
             "storm_duration_hr": 6.0, "resolution_m": 90, "input_mode": "user_gated"},
            timeout_s=1500)
        summary["b_consume"]["case_id"] = case_a

        # (c) absent path: RoG in a fresh case, no mesh, auto.
        case_c = await _create_case(ws, session_id, "proof: hecras RoG absent-mesh (uniform)")
        print(f"[c] hecras_flood_2d RoG (absent) case={case_c}", flush=True)
        summary["c_absent"] = await _invoke(
            ws, session_id, case_c, "hecras_flood_2d",
            {"bbox": COWEETA_BBOX, "design_storm_mm_per_hr": 25.0,
             "storm_duration_hr": 6.0, "resolution_m": 90, "input_mode": "auto"},
            timeout_s=1200)
        summary["c_absent"]["case_id"] = case_c

    # reconnect: does case (a)'s mesh layer persist?
    async with websockets.connect(WS_URL, max_size=64 * 1024 * 1024) as ws:
        await _handshake(ws, session_id)
        # select shape mirrors the proven seed_showcase _verify_persistence: case_id is a
        # TOP-LEVEL payload field, not nested under args.
        await ws.send(mk("case-command", session_id,
                         {"command": "select", "case_id": case_a}, case_id=case_a))
        persisted = []
        t = time.monotonic()
        while time.monotonic() - t < 25:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if msg["type"] == "case-open":
                ss = msg["payload"].get("session_state")
                if ss and ss["case"]["case_id"] == case_a:
                    persisted = [{"name": l.get("name"), "type": l.get("layer_type")}
                                 for l in (ss.get("loaded_layers") or [])]
                    break
        summary["a_persisted_layers"] = persisted

    print("\n===SUMMARY===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
