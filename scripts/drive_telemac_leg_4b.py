#!/usr/bin/env python
"""wave 4b live driver -- drive ONE registered TELEMAC leg through the daemon
(dev-tool-invoke, the SAME direct tool-call path seed_showcase_cases uses) and
capture the 4b evidence for that leg:

  * tool_status (function_response status) + is_error,
  * the emitted layers, and specifically the results-MESH layer the seam
    publishes (layer_type=="mesh") with its crs_authid / style / role,
  * the peak layer (composer typed peak) fields,
  * the run's ``outputs.json`` in MinIO: the kind=="mesh" entry (crs_authid set)
    + the peak raster entry,
  * the result SELAFIN's time-record (frame) count via read_selafin.

Reuses the product driver helpers from seed_showcase_cases (mk / _handshake /
_create_case / _parse_tool_status / WS_URL) -- no new WS protocol code.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_telemac_leg_4b.py <tool> '<json-args>' <timeout_s> <out_json>
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import boto3
import websockets.asyncio.client as wsc

from _env_guard import require_local_endpoint

from scripts.seed_showcase_cases import (
    WS_URL,
    _auto_approve_request,
    _auto_confirm_warning,
    _BLOCKING,
    _create_case,
    _handshake,
    _parse_tool_status,
    delete_case,
    mk,
    new_ulid,
)
from trid3nt_server.workflows.telemac.result_reader import read_selafin


def _s3():
    return boto3.client(
        "s3", endpoint_url=require_local_endpoint(),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


async def _drive(tool: str, args: dict, timeout_s: float) -> dict:
    session_id = new_ulid()
    out: dict = {"tool": tool, "args": args, "session_id": session_id,
                 "tool_status": None, "is_error": False, "layers": [],
                 "turn_complete": False, "detail": ""}
    async with wsc.connect(WS_URL, max_size=64 * 1024 * 1024) as ws:
        await _handshake(ws, session_id)
        case_id = await _create_case(ws, session_id, f"4b live: {tool}")
        out["case_id"] = case_id
        try:
            await ws.send(mk("dev-tool-invoke", session_id,
                             {"name": tool, "args": args, "case_id": case_id,
                              "raw_text": f"!run {tool}(...)"}, case_id=case_id))
            deadline = time.monotonic() + timeout_s
            latest_layers: list[dict] = []
            activity = False
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=min(deadline - time.monotonic(), 45))
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                mt = msg["type"]
                if mt == "tool-payload-warning":
                    activity = True
                    await _auto_confirm_warning(ws, session_id, msg)
                elif mt == "confirmation-request":
                    activity = True
                    await _auto_approve_request(ws, session_id, msg)
                elif mt in _BLOCKING:
                    out["detail"] = f"BLOCKED by {mt}"
                    break
                elif mt == "tool-io":
                    activity = True
                    out["tool_status"] = _parse_tool_status(msg["payload"])
                    if msg["payload"].get("is_error"):
                        out["is_error"] = True
                        out["detail"] = (msg["payload"].get("function_response", "")
                                         or "")[:400]
                elif mt == "session-state":
                    ll = msg["payload"].get("loaded_layers") or []
                    if ll:
                        latest_layers = ll
                elif mt == "error":
                    out["detail"] = (f"{msg['payload'].get('error_code')}: "
                                     f"{msg['payload'].get('message')}")
                    break
                elif mt == "turn-complete":
                    if activity:
                        out["turn_complete"] = True
                        break
            out["layers"] = latest_layers
        finally:
            # throwaway proof Case ("4b live: <tool>") -- never a product
            # showcase entry, so it self-cleans regardless of outcome.
            await delete_case(ws, session_id, case_id)
    return out


def _inspect_run(out: dict) -> None:
    """Find the mesh layer, extract run_id, inspect outputs.json + SELAFIN."""
    mesh = [l for l in out["layers"] if l.get("layer_type") == "mesh"]
    out["mesh_layers"] = mesh
    out["peak_layers"] = [l for l in out["layers"]
                          if l.get("layer_type") == "raster"]
    if not mesh:
        out["inspect_error"] = "no mesh layer emitted"
        return
    uri = mesh[0].get("uri", "")
    # s3://bucket/<run_id>/<basename>.slf
    body = uri.split("s3://", 1)[-1]
    bucket, _, key = body.partition("/")
    run_id = key.split("/", 1)[0]
    mesh_basename = key.rsplit("/", 1)[-1]
    out["run_id"] = run_id
    out["mesh_uri"] = uri
    s3 = _s3()
    # outputs.json
    try:
        raw = s3.get_object(Bucket=bucket, Key=f"{run_id}/outputs.json")["Body"].read()
        manifest = json.loads(raw)
        out["outputs_manifest"] = manifest
        entries = manifest.get("entries", manifest if isinstance(manifest, list) else [])
        if isinstance(manifest, dict) and "entries" in manifest:
            entries = manifest["entries"]
        out["outputs_entries"] = entries
        out["mesh_entry"] = next(
            (e for e in entries if e.get("kind") == "mesh"), None)
        out["peak_entry"] = next(
            (e for e in entries if e.get("kind") == "raster"), None)
    except Exception as exc:  # noqa: BLE001
        out["outputs_error"] = f"{type(exc).__name__}: {exc}"
    # SELAFIN frame count
    try:
        tmp = f"/tmp/4b_{run_id}_{mesh_basename}"
        s3.download_file(bucket, key, tmp)
        m = read_selafin(tmp)
        vnames = [v.strip() for v in m["varnames"]]
        first = m["data"][next(iter(m["data"]))]
        out["selafin"] = {
            "varnames": vnames,
            "n_varnames": len(vnames),
            "n_time_records": int(getattr(first, "shape", [len(first)])[0]),
            "npoin": int(len(m["x"])),
        }
        os.remove(tmp)
    except Exception as exc:  # noqa: BLE001
        out["selafin_error"] = f"{type(exc).__name__}: {exc}"


def main() -> int:
    tool = sys.argv[1]
    args = json.loads(sys.argv[2])
    timeout_s = float(sys.argv[3])
    out_json = sys.argv[4]
    out = asyncio.run(_drive(tool, args, timeout_s))
    _inspect_run(out)
    with open(out_json, "w") as f:
        json.dump(out, f, indent=2, default=str)
    # terse stdout
    print(json.dumps({
        "tool": tool, "tool_status": out["tool_status"],
        "is_error": out["is_error"], "turn_complete": out["turn_complete"],
        "run_id": out.get("run_id"),
        "n_layers": len(out["layers"]),
        "n_mesh": len(out.get("mesh_layers", [])),
        "mesh_entry_crs": (out.get("mesh_entry") or {}).get("crs_authid"),
        "has_peak_entry": bool(out.get("peak_entry")),
        "selafin": out.get("selafin"),
        "detail": out.get("detail", "")[:200],
    }, indent=2))
    return 0 if (out["tool_status"] == "ok" and not out["is_error"]) else 1


if __name__ == "__main__":
    sys.exit(main())
