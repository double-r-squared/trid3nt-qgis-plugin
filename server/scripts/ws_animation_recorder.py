"""A1 animation-proof WS recorder (data-layer evidence; NO browser).

Drives a real flood scenario over the sanctioned WebSocket path and records the
DATA-layer evidence the A1 animation proof needs:

  * every ``map-command``/``load-layer`` frame (layer_id, wms_url, style_preset,
    temporal block) — the per-frame layers as emitted to the web map,
  * the final ``session-state`` ``loaded_layers`` snapshot (layer NAMES = the
    "Flood depth step N" grouping tokens + presets),
  * tool-call start/complete/failed, pipeline-state, solve-progress and error
    frames for run timing / failure surfacing.

The agent runs an AGENTIC loop: a preamble ``agent-message-chunk done=True`` is
NOT terminal; the real solver dispatch sits behind a ``tool-payload-warning``
(solver-confirm gate) which we auto-approve with ``tool-payload-confirmation``
``decision=proceed``. We treat the run as finished only once a solver tool ran,
a ``turn-complete`` arrived, and the socket then stays quiet for the idle grace.
The known ~10s ``ConnectionClosedError`` churn is survived by reconnecting and
re-sending ``session-resume`` (same session_id) to rebind to the in-flight turn.

Makes NO visual claim. Run to terminal with a generous timeout.

Usage::

    TRID3NT_AGENT_URL=ws://54.185.114.233:8765 \
      python services/agent/scripts/ws_animation_recorder.py \
      "model the flood from a 100-year storm in Fort Myers, FL" \
      --timeout 1800 --out /tmp/a1_ws_record.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import websockets

from trid3nt_contracts import new_ulid
from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload
from trid3nt_contracts.ws import (
    ClarificationResponsePayload,
    ConfirmResponsePayload,
    Envelope,
    SessionResumePayload,
    UserMessagePayload,
)


def _serialize(message_type: str, session_id: str, payload) -> str:
    env = Envelope(type=message_type, session_id=session_id, payload=payload)
    return env.model_dump_json()


class Recorder:
    def __init__(self, text: str, session_id: str, started: float):
        self.text = text
        self.session_id = session_id
        self.started = started
        self.load_layer_cmds: list[dict] = []
        self.final_session_state: dict | None = None
        self.tool_events: list[dict] = []
        self.errors: list[dict] = []
        self.pipeline_states: list[list[str]] = []
        self.agent_text: list[str] = []
        self.clarifications: list[dict] = []
        self.saw_solver_tool = False
        self.solver_started_at = 0.0
        self.turn_complete_count = 0
        self.last_turn_complete_at = 0.0
        self.last_frame_at = time.monotonic()
        self.terminal_seen = False

    async def handle(self, ws, parsed: dict) -> None:
        mt = parsed.get("type")
        pl = parsed.get("payload", {})
        el = time.monotonic() - self.started
        self.last_frame_at = time.monotonic()

        def _mark_solver():
            if not self.saw_solver_tool:
                self.saw_solver_tool = True
                self.solver_started_at = time.monotonic()

        if mt == "tool-payload-warning":
            wid = pl.get("warning_id")
            tname = pl.get("tool_name") or pl.get("tool") or ""
            print(
                f"< tool-payload-warning warning_id={wid} tool={tname} "
                f"options={pl.get('options')} -> replying proceed"
            )
            await ws.send(
                _serialize(
                    "tool-payload-confirmation",
                    self.session_id,
                    PayloadConfirmationEnvelopePayload(warning_id=wid, decision="proceed"),
                )
            )
            print("> tool-payload-confirmation(proceed) sent", file=sys.stderr)
        elif mt == "confirmation-request":
            rid = pl.get("request_id")
            print(f"< confirmation-request request_id={rid} -> approving")
            await ws.send(
                _serialize(
                    "confirm-response",
                    self.session_id,
                    ConfirmResponsePayload(request_id=rid, approved=True),
                )
            )
        elif mt == "clarification-request":
            # B3 urban-vs-coastal disambiguation: pick the URBAN / storm-drain
            # / PySWMM option. Match on label/description keywords; fall back to
            # the first option if no urban cue is found.
            rid = pl.get("request_id")
            opts = pl.get("options", []) or []
            urban_kw = ("urban", "storm", "drain", "street", "swmm", "pyswmm", "pipe", "sewer")
            chosen = None
            for o in opts:
                blob = f"{o.get('label','')} {o.get('description','')}".lower()
                if any(k in blob for k in urban_kw):
                    chosen = o.get("id")
                    break
            if chosen is None and opts:
                chosen = opts[0].get("id")
            self.clarifications.append({"question": pl.get("question"), "options": opts, "chose": chosen})
            print(
                f"< clarification-request q={str(pl.get('question'))[:90]!r} "
                f"options={[(o.get('id'), o.get('label')) for o in opts]} -> choosing {chosen!r} (URBAN)"
            )
            await ws.send(
                _serialize(
                    "clarification-response",
                    self.session_id,
                    ClarificationResponsePayload(request_id=rid, option_id=chosen),
                )
            )
            print("> clarification-response(URBAN) sent", file=sys.stderr)
        elif mt == "map-command":
            cmd = pl.get("command")
            a = pl.get("args", {})
            if cmd == "load-layer":
                self.load_layer_cmds.append(a)
                print(
                    f"< load-layer layer_id={a.get('layer_id')} "
                    f"style_preset={a.get('style_preset')} "
                    f"temporal={'Y' if a.get('temporal') else 'N'} "
                    f"wms={str(a.get('wms_url'))[:80]}"
                )
            else:
                print(f"< map-command {cmd} {json.dumps(a)[:100]}")
        elif mt == "session-state":
            self.final_session_state = pl
            print(f"< session-state loaded_layers={len(pl.get('loaded_layers', []))}")
        elif mt == "tool-call-start":
            self.tool_events.append({"ev": "start", **pl})
            tname = pl.get("tool_name") or pl.get("name") or ""
            if any(k in tname for k in ("flood", "solver", "run_model")):
                _mark_solver()
            print(f"< tool-call-start {tname}")
        elif mt == "tool-call-complete":
            self.tool_events.append({"ev": "complete", **pl})
            print(f"< tool-call-complete {pl.get('tool_name') or pl.get('name')}")
        elif mt == "tool-call-failed":
            self.tool_events.append({"ev": "failed", **pl})
            print(f"< tool-call-FAILED {pl.get('tool_name') or pl.get('name')} {str(pl)[:200]}")
        elif mt == "pipeline-state":
            self.pipeline_states.append([s.get("state") for s in pl.get("steps", [])])
        elif mt == "solve-progress":
            # The gated solver runs WITHOUT a separate tool-call-start frame; the
            # solve-progress stream IS the solver-activity signal.
            _mark_solver()
            print(f"< solve-progress run_id={pl.get('run_id')} elapsed={pl.get('elapsed_seconds')}")
        elif mt == "error":
            self.errors.append(pl)
            print(f"< ERROR code={pl.get('error_code')} msg={pl.get('message')}")
        elif mt == "agent-message-chunk":
            if pl.get("delta"):
                self.agent_text.append(pl["delta"])
            if pl.get("done"):
                print(f"< agent segment done (elapsed {el:.0f}s)")  # NOT terminal
        elif mt == "turn-complete":
            self.turn_complete_count += 1
            self.last_turn_complete_at = time.monotonic()
            print(
                f"< turn-complete #{self.turn_complete_count} "
                f"final_state={pl.get('final_state')} (elapsed {el:.0f}s)"
            )
        else:
            print(f"< {mt} {json.dumps(pl)[:100]}")

    def is_idle_terminal(self) -> bool:
        if self.turn_complete_count == 0:
            return False
        now = time.monotonic()
        quiet_frames = now - self.last_frame_at
        if self.saw_solver_tool:
            # The solver dispatched. Terminal only once a turn-complete arrived
            # AFTER the solve started (so the early resume turn-complete cannot
            # trip it) AND the socket has been quiet for the grace window. The
            # solve itself streams solve-progress, so a real quiet period only
            # happens once the run finished + frames published.
            tc_after_solve = self.last_turn_complete_at >= self.solver_started_at
            if tc_after_solve and quiet_frames >= 30.0:
                return True
            return False
        # No solver dispatch (pure-chat reply) -> longer idle terminates so we
        # don't burn the full timeout on a conversational answer.
        return quiet_frames >= 60.0


async def main(args) -> int:
    url = os.environ.get("TRID3NT_AGENT_URL", "ws://127.0.0.1:8765")
    session_id = new_ulid()
    started = time.monotonic()
    deadline = started + args.timeout
    print(f"# url={url}", file=sys.stderr)
    print(f"# session_id={session_id}", file=sys.stderr)

    rec = Recorder(args.text, session_id, started)
    sent_prompt = False

    while not rec.terminal_seen and time.monotonic() < deadline:
        try:
            async with websockets.connect(url, max_size=None, ping_interval=20) as ws:
                await ws.send(
                    _serialize("session-resume", session_id, SessionResumePayload())
                )
                if not sent_prompt:
                    await ws.send(
                        _serialize(
                            "user-message",
                            session_id,
                            UserMessagePayload(text=args.text, research_mode="research"),
                        )
                    )
                    sent_prompt = True
                    print(f"> sent prompt: {args.text!r}", file=sys.stderr)
                else:
                    print("> reconnected + session-resume (rebind to in-flight turn)", file=sys.stderr)

                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        print("# TIMEOUT waiting for terminal frame", file=sys.stderr)
                        rec.terminal_seen = False
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 10))
                    except asyncio.TimeoutError:
                        if rec.is_idle_terminal():
                            q = time.monotonic() - rec.last_turn_complete_at
                            print(
                                f"# idle {q:.0f}s after last turn-complete "
                                f"(solver_seen={rec.saw_solver_tool}) -> terminal",
                                file=sys.stderr,
                            )
                            rec.terminal_seen = True
                            break
                        print(
                            f"# ...still waiting ({time.monotonic()-started:.0f}s, "
                            f"turn_completes={rec.turn_complete_count}, "
                            f"solver_seen={rec.saw_solver_tool})",
                            file=sys.stderr,
                        )
                        continue
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    await rec.handle(ws, json.loads(raw))
                    if rec.terminal_seen:
                        break
        except websockets.exceptions.ConnectionClosed as exc:
            print(f"# connection closed ({exc}) -> reconnecting", file=sys.stderr)
            await asyncio.sleep(1.0)
        except (OSError, websockets.exceptions.InvalidMessage) as exc:
            print(f"# connect error ({exc}) -> retrying", file=sys.stderr)
            await asyncio.sleep(2.0)

    elapsed = time.monotonic() - started
    record = {
        "elapsed_seconds": round(elapsed, 1),
        "terminal_seen": rec.terminal_seen,
        "prompt": args.text,
        "saw_solver_tool": rec.saw_solver_tool,
        "turn_complete_count": rec.turn_complete_count,
        "load_layer_commands": rec.load_layer_cmds,
        "final_session_state_loaded_layers": (
            rec.final_session_state.get("loaded_layers", []) if rec.final_session_state else []
        ),
        "tool_events": rec.tool_events,
        "pipeline_states": rec.pipeline_states,
        "errors": rec.errors,
        "clarifications": rec.clarifications,
        "agent_text": "".join(rec.agent_text),
    }
    with open(args.out, "w") as fh:
        json.dump(record, fh, indent=2, default=str)
    print(f"\n# wrote record to {args.out} (elapsed {elapsed:.0f}s, terminal={rec.terminal_seen})", file=sys.stderr)
    print(f"# load-layer frames: {len(rec.load_layer_cmds)}", file=sys.stderr)
    print(f"# final loaded_layers: {len(record['final_session_state_loaded_layers'])}", file=sys.stderr)
    print(f"# errors: {len(rec.errors)}", file=sys.stderr)
    return 0 if rec.terminal_seen else 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("text")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--out", default="/tmp/a1_ws_record.json")
    args = parser.parse_args()
    rc = asyncio.run(main(args))
    sys.exit(rc)
