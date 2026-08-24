#!/usr/bin/env python
"""Live driver: a user_gated ``telemac_do_sag`` answered through the CARDS.

Drives the daemon exactly as the plugin does - ``dev-tool-invoke`` on the
registered tool, then answers the gates as they arrive on the wire:

  * ``spatial-input-request``  -> the DRAW card: replies with a real outfall
    point on the Eel River reach (the USGS Scotia gage location), so the run
    seeds its release from a drawn value with ``basis=user``.
  * ``tool-payload-warning``   -> the FORM card when the envelope carries a
    ``param_sheet`` (submit one edited row as ``narrow_scope`` +
    ``revised_args``), else the existing proceed-confirm.

Then reads back what the run persisted under its own prefix: ``chart_spec.json``
and ``metrics.json``. Nothing here is rederived - the evidence is the product's
own artifacts.

Reuses the product driver helpers from ``seed_showcase_cases`` (mk / _handshake /
_create_case / _parse_tool_status / WS_URL) - no new WS protocol code.

Env (MinIO): set -a; source .env.local; set +a
Usage: drive_do_sag_cards.py [--timeout 1800] [--out evidence.json]
"""
from __future__ import annotations

import argparse
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
    _BLOCKING,
    _auto_approve_request,
    _create_case,
    _handshake,
    _parse_tool_status,
    mk,
    new_ulid,
)

#: A real NHDPlus reach WITH NHDArea polygon coverage (the bank_source precondition).
LOCATION = "Eel River near Scotia, California"
#: The USGS Eel River at Scotia gage (11477000) - a real point on the reach, used
#: as the drawn outfall so the release is a USER value, not a derived seed.
OUTFALL_LONLAT = [-124.0983, 40.4921]
#: The one row the form card edits, so "the run used the edited value" is checkable
#: against the persisted metrics (the standard the verdict is judged against).
FORM_EDIT = ("do_standard_mgl", 6.0)

ARGS = {
    "location": LOCATION,
    "discharge_bod_mgl": 20.0,
    "water_temp_c": 20.0,
    "do_standard_mgl": 5.0,
    "k1_per_day": 0.3,
    "k2_per_day": 0.9,
    "reach_length_km": 12.0,
    "mesh_resolution": "auto",
    "input_mode": "user_gated",
}


def _s3():
    return boto3.client(
        "s3", endpoint_url=require_local_endpoint(),
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
    )


async def _answer_draw(ws, session_id: str, msg: dict, out: dict) -> None:
    payload = msg["payload"]
    out["draw_card"] = {
        "request_id": payload.get("request_id"),
        "mode": payload.get("mode"),
        "purpose": payload.get("purpose"),
        "title": payload.get("title"),
        "description": payload.get("description"),
        "answered_with": OUTFALL_LONLAT,
    }
    await ws.send(mk("spatial-input-response", session_id, {
        "request_id": payload["request_id"],
        "geometry_type": "point",
        "coordinates": OUTFALL_LONLAT,
        "features": None,
        "cancelled": False,
    }))


async def _answer_warning(ws, session_id: str, msg: dict, out: dict) -> None:
    payload = msg["payload"]
    sheet = payload.get("param_sheet")
    if isinstance(sheet, dict) and sheet.get("rows"):
        name, value = FORM_EDIT
        out["form_card"] = {
            "workflow": sheet.get("workflow"),
            "title": sheet.get("title"),
            "rows": [{k: r.get(k) for k in
                      ("name", "value", "units", "door", "basis",
                       "source_badge", "bounds", "advanced")}
                     for r in sheet["rows"]],
            "edited": {name: value},
        }
        await ws.send(mk("tool-payload-confirmation", session_id, {
            "warning_id": payload["warning_id"],
            "decision": "narrow_scope",
            "revised_args": {name: value},
        }))
        return
    out.setdefault("plain_warnings", []).append(payload.get("tool_name"))
    await ws.send(mk("tool-payload-confirmation", session_id, {
        "warning_id": payload["warning_id"],
        "decision": "proceed",
        "revised_args": None,
    }))


async def drive(timeout_s: float) -> dict:
    session_id = new_ulid()
    out: dict = {"tool": "telemac_do_sag", "args": ARGS, "session_id": session_id,
                 "tool_status": None, "is_error": False, "layers": [],
                 "turn_complete": False, "detail": "", "charts": 0}
    async with wsc.connect(WS_URL, max_size=64 * 1024 * 1024) as ws:
        await _handshake(ws, session_id)
        case_id = await _create_case(
            ws, session_id,
            "showcase: telemac do sag (Eel River near Scotia, cards)")
        out["case_id"] = case_id
        await ws.send(mk("dev-tool-invoke", session_id,
                         {"name": "telemac_do_sag", "args": ARGS,
                          "case_id": case_id,
                          "raw_text": "!run telemac_do_sag(...)"},
                         case_id=case_id))
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
            if mt == "spatial-input-request":
                activity = True
                await _answer_draw(ws, session_id, msg, out)
            elif mt == "tool-payload-warning":
                activity = True
                await _answer_warning(ws, session_id, msg, out)
            elif mt == "confirmation-request":
                activity = True
                await _auto_approve_request(ws, session_id, msg)
            elif mt == "chart-emission":
                out["charts"] += 1
            elif mt in _BLOCKING:
                out["detail"] = f"BLOCKED by {mt}"
                break
            elif mt == "tool-io":
                activity = True
                out["tool_status"] = _parse_tool_status(msg["payload"])
                if msg["payload"].get("is_error"):
                    out["is_error"] = True
                    out["detail"] = (msg["payload"].get("function_response", "")
                                     or "")[:600]
            elif mt == "session-state":
                loaded = msg["payload"].get("loaded_layers") or []
                if loaded:
                    latest_layers = loaded
            elif mt == "error":
                out["detail"] = (f"{msg['payload'].get('error_code')}: "
                                 f"{msg['payload'].get('message')}")
                break
            elif mt == "turn-complete" and activity:
                out["turn_complete"] = True
                break
        out["layers"] = latest_layers
    return out


def _inspect_run(out: dict) -> None:
    """Pull the run's OWN products off its prefix - never a rederivation."""
    out["outfall_layers"] = [
        {"name": l.get("name"), "layer_type": l.get("layer_type"),
         "role": l.get("role"), "uri": l.get("uri")}
        for l in out["layers"]
        if "outfall" in str(l.get("name", "")).lower()
    ]
    raster = next((l for l in out["layers"]
                   if l.get("layer_type") == "raster"
                   and str(l.get("uri", "")).startswith("s3://")), None)
    if raster is None:
        out["inspect_error"] = "no published raster to locate the run prefix from"
        return
    body = str(raster["uri"]).split("s3://", 1)[-1]
    bucket, _, key = body.partition("/")
    run_id = key.split("/", 1)[0]
    out["run_id"] = run_id
    s3 = _s3()
    for label, name in (("chart_spec", "chart_spec.json"),
                        ("metrics", "metrics.json")):
        try:
            blob = s3.get_object(Bucket=bucket, Key=f"{run_id}/{name}")["Body"].read()
            out[label] = json.loads(blob)
            out[f"{label}_uri"] = f"s3://{bucket}/{run_id}/{name}"
        except Exception as exc:  # noqa: BLE001 - absence is the finding, not a crash
            out[f"{label}_error"] = f"{type(exc).__name__}: {exc}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default="/tmp/do_sag_cards_evidence.json")
    ns = ap.parse_args()

    out = asyncio.run(drive(ns.timeout))
    _inspect_run(out)
    with open(ns.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)

    metrics = out.get("metrics") or {}
    print(json.dumps({
        "tool_status": out.get("tool_status"),
        "turn_complete": out.get("turn_complete"),
        "draw_card": out.get("draw_card"),
        "form_card_rows": len((out.get("form_card") or {}).get("rows", [])),
        "form_edit": (out.get("form_card") or {}).get("edited"),
        "outfall_layers": out.get("outfall_layers"),
        "run_id": out.get("run_id"),
        "chart_spec_uri": out.get("chart_spec_uri"),
        "chart_spec_error": out.get("chart_spec_error"),
        "metrics_uri": out.get("metrics_uri"),
        "metrics_error": out.get("metrics_error"),
        "do_min_mgl": metrics.get("do_min_mgl"),
        "do_min_distance_m": metrics.get("do_min_distance_m"),
        "do_standard_mgl": metrics.get("do_standard_mgl"),
        "do_violates_standard": metrics.get("do_violates_standard"),
        "detail": out.get("detail"),
        "evidence": ns.out,
    }, indent=2))
    return 0 if out.get("tool_status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
