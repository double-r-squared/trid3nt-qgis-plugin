"""ADR 0198 DEFECT 1 live proof: a typed tool error on the dev-tool-invoke
(!run) no-awaiter path must surface as an ``error`` WS envelope, NOT vanish
silently. Reuses the seeder client machinery.
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, "scripts")
import websockets.asyncio.client as wsc  # noqa: E402
from seed_showcase_cases import WS_URL, mk, _handshake, _create_case, run_line  # noqa: E402
from trid3nt_contracts import new_ulid  # noqa: E402

ARGS = {
    "location": "Otto, North Carolina",
    "pour_point": (0.0, 0.0),  # mid-ocean (Gulf of Guinea) -> degenerate/typed error
    "antecedent_moisture": "normal",
    "design_storm_mm_per_hr": 25.0,
    "storm_duration_hr": 6.0,
}


async def main() -> int:
    session_id = new_ulid()
    async with wsc.connect(WS_URL, max_size=64 * 1024 * 1024) as ws:
        await _handshake(ws, session_id)
        case_id = await _create_case(ws, session_id, "ADR0198 defect1 mid-ocean typed error")
        rl = run_line("telemac_rain_on_grid", ARGS)
        print(f"case_id={case_id}  {rl}", flush=True)
        await ws.send(mk("dev-tool-invoke", session_id,
                         {"name": "telemac_rain_on_grid", "args": ARGS,
                          "case_id": case_id, "raw_text": rl},
                         case_id=case_id))
        deadline = time.monotonic() + 240
        error_env = None
        tool_io_error = None
        seen_types = []
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(deadline - time.monotonic(), 30))
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            mt = msg["type"]
            seen_types.append(mt)
            if mt == "tool-payload-warning":
                await ws.send(mk("tool-payload-confirmation", session_id,
                                 {"warning_id": msg["payload"].get("warning_id"),
                                  "decision": "proceed", "revised_args": None}))
            elif mt == "confirmation-request":
                await ws.send(mk("confirm-response", session_id,
                                 {"request_id": msg["payload"].get("request_id"),
                                  "approved": True}))
            elif mt == "error":
                error_env = msg["payload"]
                print("ERROR ENVELOPE:", json.dumps(error_env), flush=True)
                break
            elif mt == "tool-io" and msg["payload"].get("is_error"):
                tool_io_error = msg["payload"]
                print("TOOL-IO is_error:", json.dumps(msg["payload"])[:400], flush=True)
                break
            elif mt == "turn-complete":
                print("turn-complete (types seen:", seen_types, ")", flush=True)
                break
        print("SEEN_TYPES:", seen_types, flush=True)
        if error_env is not None:
            print("VERDICT: PASS -- error envelope reached client:",
                  error_env.get("error_code"), "|", (error_env.get("message") or "")[:200], flush=True)
            return 0
        if tool_io_error is not None:
            print("VERDICT: tool-io is_error (not the dispatch-swallow path)", flush=True)
            return 0
        print("VERDICT: FAIL -- no error envelope (silent no-result / timeout)", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
