"""One-off live drive: schism_pahm_surge on a SMALL AOI at explicit resolution_m=30
(ADR 0219 explicit-override path). Reuses seed_showcase_cases.py's WS-protocol
helpers verbatim (product code, not reinvented) -- creates a fresh Case, dispatches
via dev-tool-invoke exactly like the QGIS plugin's !run path, auto-confirms the
tool-payload-warning gate if it fires, and prints the full turn trace + final
SchismElevationLayerURI-shaped result.

Run:
  cd /home/nate/Documents/trid3nt-local
  env $(grep -v '^#' .env.local | xargs) PYTHONPATH=server/src:contracts/src \
    venvs/agent/bin/python scripts/sandbox/schism/run_pahm_surge_fine_aoi.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_pahm_surge_fine_aoi")

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "qgis-plugin"))

spec = importlib.util.spec_from_file_location("seed_showcase_cases", REPO / "scripts" / "seed_showcase_cases.py")
seed_mod = importlib.util.module_from_spec(spec)
sys.modules["seed_showcase_cases"] = seed_mod
spec.loader.exec_module(seed_mod)  # type: ignore[union-attr]

WS_URL = seed_mod.WS_URL
mk = seed_mod.mk
_handshake = seed_mod._handshake
_create_case = seed_mod._create_case
_auto_confirm_warning = seed_mod._auto_confirm_warning
_auto_approve_request = seed_mod._auto_approve_request
_parse_tool_status = seed_mod._parse_tool_status
_first_line = seed_mod._first_line
run_line = seed_mod.run_line
_BLOCKING = seed_mod._BLOCKING

from trid3nt_contracts import new_ulid  # noqa: E402

TOOL = "schism_pahm_surge"
ARGS = {
    "bbox": [-95.05, 29.2, -94.6, 29.65],
    "resolution_m": 30,
    "sim_days": 1.5,
    "input_mode": "auto",
}
TIMEOUT_S = 2400
CASE_TITLE = "live-drive: schism_pahm_surge fine-AOI resolution_m=30 override"


async def main() -> int:
    import websockets.asyncio.client as wsc

    session_id = new_ulid()
    line = run_line(TOOL, ARGS)
    log.info("session=%s  %s", session_id, line)

    result: dict = {"status": "not_run", "layers": [], "tool_status": None,
                    "detail": "", "case_id": None, "payload_warnings": []}

    async with wsc.connect(WS_URL, max_size=64 * 1024 * 1024) as ws:
        await _handshake(ws, session_id)
        case_id = await _create_case(ws, session_id, CASE_TITLE)
        result["case_id"] = case_id
        log.info("case_id=%s", case_id)

        await ws.send(mk("dev-tool-invoke", session_id,
                         {"name": TOOL, "args": ARGS, "case_id": case_id, "raw_text": line},
                         case_id=case_id))

        deadline = time.monotonic() + TIMEOUT_S
        activity = False
        tool_io_error = False
        function_response = None
        latest_layers: list[dict] = []
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(deadline - time.monotonic(), 45))
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            mtype = msg["type"]
            if mtype == "tool-payload-warning":
                activity = True
                result["payload_warnings"].append(msg["payload"])
                log.info("PAYLOAD-WARNING FIRED: %s", json.dumps(msg["payload"])[:500])
                await _auto_confirm_warning(ws, session_id, msg)
            elif mtype == "confirmation-request":
                activity = True
                await _auto_approve_request(ws, session_id, msg)
            elif mtype in _BLOCKING:
                result["status"] = "blocked"
                result["detail"] = f"gate needs interactive input ({mtype})"
                log.warning("BLOCKED by %s: %s", mtype, json.dumps(msg["payload"])[:500])
                break
            elif mtype in ("pipeline-state", "tool-call-start", "tool-call-progress"):
                activity = True
                log.info("progress: %s", json.dumps(msg["payload"])[:300])
            elif mtype == "tool-io":
                activity = True
                result["tool_status"] = _parse_tool_status(msg["payload"])
                function_response = msg["payload"].get("function_response")
                if msg["payload"].get("is_error"):
                    tool_io_error = True
                    result["detail"] = _first_line(function_response or "", 2000)
                log.info("tool-io status=%s is_error=%s", result["tool_status"],
                         msg["payload"].get("is_error"))
            elif mtype in ("chart-emission", "chart"):
                activity = True
            elif mtype == "tool-call-failed":
                activity = True
                result["detail"] = _first_line(json.dumps(msg["payload"]), 2000)
                log.error("tool-call-failed: %s", result["detail"])
            elif mtype == "session-state":
                ll = msg["payload"].get("loaded_layers") or []
                if ll:
                    latest_layers = ll
            elif mtype == "error":
                result["status"] = "error"
                result["detail"] = f"{msg['payload'].get('error_code')}: {msg['payload'].get('message')}"
                log.error("ERROR %s", result["detail"])
                break
            elif mtype == "turn-complete":
                if activity:
                    break
        else:
            result["status"] = "timeout"
            result["detail"] = f"no turn-complete within {TIMEOUT_S}s"

        result["layers"] = latest_layers
        if result["status"] == "not_run":
            if tool_io_error or result["tool_status"] == "error":
                result["status"] = "error"
            elif latest_layers or result["tool_status"] == "ok":
                result["status"] = "ok"
            else:
                result["status"] = "no_result"

    result["function_response_raw"] = function_response
    out = Path("/tmp/pahm_surge_fine_aoi_result.json")
    out.write_text(json.dumps(result, indent=2, default=str))
    print("=== RESULT ===")
    print(json.dumps({k: v for k, v in result.items() if k != "function_response_raw"}, indent=2, default=str))
    print("full function_response:")
    print(function_response)
    print(f"\nsaved -> {out}")
    print(f"case_id -> {result['case_id']}")
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(asyncio.get_event_loop().run_until_complete(main()))
