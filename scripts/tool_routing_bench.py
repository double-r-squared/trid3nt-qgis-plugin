#!/usr/bin/env python3
"""tool_routing_bench.py -- TRID3NT Local tool-routing breadth benchmark.

15 prompts covering geocode, fetch-class, compute, solver, and no-tool cases.
For each prompt:
  - Creates a fresh WS session + case
  - Sends the prompt
  - Watches pipeline-state envelopes for tool_name values in steps
  - For SOLVER prompts: cancels once the composer tool appears in pipeline-state
  - For FETCH/COMPUTE prompts: runs to completion (capped at 4 minutes)
  - Records: expected tool, tools observed, args validity, wall time
  - Auto-confirms any tool-payload-warning to let sims START before cancel

Logs raw envelopes to logs/tool_routing_bench.log.
Writes results to docs/reports/tool-routing-bench-qwen3-8b.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import websockets.asyncio.client as ws_client

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parent.parent
LOG_FILE = REPO_ROOT / "logs" / "tool_routing_bench.log"
REPORT_FILE = REPO_ROOT / "docs" / "reports" / "tool-routing-bench-qwen3-8b.md"
WS_URL = "ws://127.0.0.1:8765/ws"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Logging: file (all raw) + stdout (progress)
# NOTE: handler attachment happens in _setup_logging(), called only under
# __main__ -- importing this module must NOT truncate the log file.
# ---------------------------------------------------------------------------
log = logging.getLogger("bench")


def _setup_logging() -> None:
    file_handler = logging.FileHandler(str(LOG_FILE), mode="w")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    logging.basicConfig(level=logging.DEBUG, handlers=[file_handler, stdout_handler])

# ---------------------------------------------------------------------------
# ULID / ID helper
# ---------------------------------------------------------------------------
try:
    from trid3nt_contracts import new_ulid
    def new_id() -> str:
        return new_ulid()
except ImportError:
    import uuid
    def new_id() -> str:
        return str(uuid.uuid4()).replace("-", "").upper()[:26]

# ---------------------------------------------------------------------------
# LLM step names to ignore when extracting tool names (bookkeeping)
# ---------------------------------------------------------------------------
LLM_STEP_NAMES = frozenset({
    "llm_generation", "gemini_generate", "thinking", "llm",
    "model_generate", "generate", "bedrock_generate", "ollama_generate",
})

# ---------------------------------------------------------------------------
# SOLVER tools -- cancel once one of these appears in pipeline steps
# ---------------------------------------------------------------------------
SOLVER_TOOLS = frozenset({
    "run_model_flood_scenario",
    "model_flood_scenario",
    "run_sfincs",
    "run_model_sustainable_yield_scenario",
    "run_modflow",
    "run_modflow_tool",
    "run_modflow_archetype",
    "run_swmm",
    "run_swmm_tool",
    "run_urban_flood_swmm",
    "model_urban_flood_swmm",
    "run_geoclaw",
    "run_geoclaw_tool",
    "model_dambreak_geoclaw_scenario",
    "run_openquake",
    "run_openquake_tool",
    "model_seismic_hazard_scenario",
    # Broad catch for any composer step with "solver" in name
})

def _is_solver_tool(name: str) -> bool:
    if name in SOLVER_TOOLS:
        return True
    low = name.lower()
    return any(k in low for k in ("run_model", "solver", "geoclaw", "openquake", "swmm", "modflow"))

# ---------------------------------------------------------------------------
# Benchmark prompts
# ---------------------------------------------------------------------------
PROMPTS = [
    {
        "id": 1,
        "short": "Boulder CO bbox",
        "prompt": "Where is Boulder, Colorado? Give me its bounding box.",
        "expected": "geocode_location",
        "expected_set": {"geocode_location"},
        "is_solver": False,
    },
    {
        "id": 2,
        "short": "DEM Asheville NC",
        "prompt": "Fetch a digital elevation model for a 5km box around Asheville, North Carolina.",
        "expected": "fetch_elevation / fetch_topobathy",
        "expected_set": {
            "fetch_elevation", "fetch_topobathy", "fetch_dem",
            "fetch_srtm", "fetch_3dep", "fetch_cop30",
            "fetch_elevation_data", "fetch_lidar",
        },
        "is_solver": False,
    },
    {
        "id": 3,
        "short": "Land cover Sacramento",
        "prompt": "Show me the land cover types around Sacramento, California.",
        "expected": "fetch_landcover",
        "expected_set": {"fetch_landcover", "fetch_land_cover", "fetch_nlcd"},
        "is_solver": False,
    },
    {
        "id": 4,
        "short": "Buildings Savannah GA",
        "prompt": "Get the building footprints for downtown Savannah, Georgia.",
        "expected": "fetch_buildings",
        "expected_set": {"fetch_buildings", "fetch_building_footprints", "fetch_osm_buildings"},
        "is_solver": False,
    },
    {
        "id": 5,
        "short": "River network Missoula",
        "prompt": "Show the river network near Missoula, Montana.",
        "expected": "fetch_rivers (or similar)",
        "expected_set": {
            "fetch_rivers", "fetch_river_network", "fetch_hydrology",
            "fetch_nhd", "fetch_streams", "fetch_waterways",
            "fetch_nhd_flowlines", "fetch_flowlines",
        },
        "is_solver": False,
    },
    {
        "id": 6,
        "short": "Earthquakes San Jose",
        "prompt": "Show recent earthquakes near San Jose, California from the last month.",
        "expected": "USGS earthquake fetcher",
        "expected_set": {
            "fetch_earthquakes", "fetch_usgs_earthquakes",
            "fetch_seismic_events", "fetch_earthquake_catalog",
        },
        "is_solver": False,
    },
    {
        "id": 7,
        "short": "Precip radar Kansas",
        "prompt": "Show current precipitation radar over Kansas.",
        "expected": "NEXRAD/radar fetcher",
        "expected_set": {
            "fetch_nexrad", "fetch_radar", "fetch_mrms",
            "fetch_mrms_qpe", "fetch_precipitation_radar",
            "fetch_nexrad_radar", "fetch_precip_radar",
        },
        "is_solver": False,
    },
    {
        "id": 8,
        "short": "Hillshade Boone NC",
        "prompt": "Make a hillshade from the terrain around Boone, North Carolina.",
        "expected": "compute_hillshade (+ DEM chain)",
        "expected_set": {
            "compute_hillshade", "hillshade",
            # chain starters also acceptable as first tool
            "fetch_elevation", "fetch_topobathy", "fetch_dem", "fetch_3dep",
        },
        "is_solver": False,
    },
    {
        "id": 9,
        "short": "Avg elev Provo UT",
        "prompt": "What is the average elevation inside the city limits of Provo, Utah?",
        "expected": "compute_zonal_statistics chain",
        "expected_set": {
            "compute_zonal_statistics", "zonal_statistics",
            "fetch_elevation", "fetch_dem", "fetch_3dep",
            "fetch_administrative_boundaries", "geocode_location",
        },
        "is_solver": False,
    },
    {
        "id": 10,
        "short": "Pluvial flood Peoria IL",
        "prompt": (
            "Run a small pluvial flood simulation for a 4km box in Peoria, Illinois "
            "with a 50-year storm, coarsest resolution."
        ),
        "expected": "run_model_flood_scenario",
        "expected_set": {
            "run_model_flood_scenario", "model_flood_scenario",
            "run_sfincs", "run_flood",
        },
        "is_solver": True,
    },
    {
        "id": 11,
        "short": "MODFLOW Bakersfield CA",
        "prompt": (
            "Run a MODFLOW sustainable yield analysis for a small aquifer near "
            "Bakersfield, California, aoi around lat 35.37 lon -119.02, "
            "one well pumping 500 m3/day at the center."
        ),
        "expected": "run_model_sustainable_yield_scenario",
        "expected_set": {
            "run_model_sustainable_yield_scenario",
            "run_modflow", "run_modflow_tool",
            "run_modflow_archetype", "model_sustainable_yield",
        },
        "is_solver": True,
    },
    {
        "id": 12,
        "short": "SWMM Alexandria VA",
        "prompt": (
            "Run an urban stormwater SWMM simulation for a few blocks of "
            "Alexandria, Virginia."
        ),
        "expected": "SWMM composer",
        "expected_set": {
            "run_swmm_urban_flood",  # registered SWMM composer name
            "run_swmm", "run_swmm_tool", "run_urban_flood_swmm",
            "model_urban_flood_swmm", "run_model_urban_flood",
        },
        "is_solver": True,
    },
    {
        "id": 13,
        "short": "Tsunami Crescent City",
        "prompt": "Simulate a tsunami hitting Crescent City, California.",
        "expected": "GeoClaw composer",
        "expected_set": {
            "run_geoclaw_inundation",  # registered GeoClaw composer name
            "run_geoclaw", "run_geoclaw_tool",
            "model_dambreak_geoclaw_scenario", "run_tsunami",
            "run_model_tsunami", "geoclaw",
        },
        "is_solver": True,
    },
    {
        "id": 14,
        "short": "Seismic hazard SF Bay",
        "prompt": "Run a probabilistic seismic hazard analysis for the San Francisco Bay Area.",
        "expected": "OpenQuake composer",
        "expected_set": {
            "run_seismic_hazard_psha",  # registered OpenQuake composer name
            "run_openquake", "run_openquake_tool",
            "model_seismic_hazard_scenario", "run_psha",
            "run_model_seismic_hazard",
        },
        "is_solver": True,
    },
    {
        "id": 15,
        "short": "Haiku (no tool)",
        "prompt": "Write me a haiku about rivers.",
        "expected": "NO_TOOL",
        "expected_set": set(),  # empty = no tool should fire
        "is_solver": False,
    },
]

# ---------------------------------------------------------------------------
# Envelope builder
# ---------------------------------------------------------------------------
def mk(type_: str, session_id: str, payload: dict, case_id: str | None = None) -> str:
    env = {
        "type": type_,
        "id": new_id(),
        "ts": "2026-07-05T00:00:00Z",
        "session_id": session_id,
        "case_id": case_id,
        "payload": payload,
    }
    raw = json.dumps(env)
    log.debug("SEND type=%-30s %s", type_, raw[:200])
    return raw

# ---------------------------------------------------------------------------
# Per-prompt driver
# ---------------------------------------------------------------------------
SOLVER_CANCEL_TIMEOUT = 240  # max seconds to wait for solver start before cancel
FETCH_TIMEOUT = 240          # max seconds for fetch/compute prompts
MAX_TURN_TIMEOUT = 240       # overall cap per prompt

async def run_one_prompt(ws, session_id: str, case_id: str, spec: dict) -> dict:
    """Send one prompt, collect results.

    Returns dict with:
      tools_fired: list of tool names in order of first appearance
      args_valid: bool (True if no USER_INPUT_REQUIRED or error on first call)
      wall_time: float seconds
      cancelled: bool
      error_text: str | None
    """
    prompt_id = spec["id"]
    label = f"P{prompt_id}"
    is_solver = spec["is_solver"]
    timeout = SOLVER_CANCEL_TIMEOUT if is_solver else FETCH_TIMEOUT

    print(f"\n[{label}] {spec['short']}")
    print(f"  Prompt: {spec['prompt'][:80]}...")
    print(f"  Expected: {spec['expected']}")

    # Send the prompt
    await ws.send(mk(
        "user-message", session_id,
        {"text": spec["prompt"], "case_id": case_id},
        case_id=case_id,
    ))

    tools_fired: list[str] = []
    args_valid = True
    cancelled = False
    error_text: str | None = None
    first_error_code: str | None = None

    t_start = time.monotonic()
    deadline = t_start + timeout
    llm_started = False
    cancel_sent = False
    turn_done = False

    while time.monotonic() < deadline and not turn_done:
        remaining = deadline - time.monotonic()
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 15))
        except asyncio.TimeoutError:
            log.warning("%s: recv timed out (%.0fs elapsed)", label, time.monotonic() - t_start)
            break

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("%s: non-JSON frame", label)
            continue

        mtype = msg.get("type", "")
        payload = msg.get("payload") or {}
        log.debug("RECV [%s] type=%-30s payload=%s", label, mtype, json.dumps(payload)[:300])

        if mtype == "pipeline-state":
            llm_started = True
            steps = payload.get("steps") or []
            for step in steps:
                raw_name = step.get("tool_name") or step.get("name") or ""
                if not raw_name:
                    continue
                # Skip LLM bookkeeping steps
                if raw_name.lower() in LLM_STEP_NAMES:
                    continue
                if raw_name not in tools_fired:
                    tools_fired.append(raw_name)
                    log.info("%s: tool appeared: %s (state=%s)", label, raw_name, step.get("state"))
                    print(f"  [tool] {raw_name} (state={step.get('state')})")

                # For solver prompts: cancel once a solver tool starts
                if is_solver and not cancel_sent and _is_solver_tool(raw_name):
                    step_state = step.get("state", "")
                    if step_state in ("running", "pending", "started", ""):
                        log.info("%s: solver tool %r started -- sending cancel", label, raw_name)
                        print(f"  [cancel] solver {raw_name} started -- cancelling")
                        await ws.send(mk("cancel", session_id, {"reason": "bench-cancel"}, case_id=case_id))
                        cancel_sent = True
                        cancelled = True
                        # Give a moment for the cancel to propagate
                        await asyncio.sleep(1)
                        turn_done = True
                        break

            # Check if ALL steps are terminal
            if steps and all(
                s.get("state") in ("complete", "failed", "cancelled") for s in steps
            ):
                if not is_solver:
                    log.info("%s: pipeline terminal (all steps done)", label)
                    # For non-solver, allow turn-complete to finalize
                    # but don't break yet - might have more text

        elif mtype == "tool-payload-warning":
            # Auto-confirm so the tool actually starts (especially solvera)
            warning_id = payload.get("warning_id")
            log.info("%s: tool-payload-warning warning_id=%s -- auto-confirming", label, warning_id)
            print(f"  [confirm] auto-confirming payload warning {warning_id}")
            await ws.send(mk(
                "tool-payload-confirmation", session_id,
                {"warning_id": warning_id, "decision": "proceed", "revised_args": None},
            ))

        elif mtype == "agent-message-chunk":
            delta = payload.get("delta", "") or payload.get("text", "")
            if delta:
                log.debug("%s: text chunk: %r", label, delta[:80])
            if payload.get("done"):
                log.info("%s: agent-message-chunk done=True", label)
                if not is_solver:
                    # For non-solver, after done chunk wait for turn-complete
                    pass

        elif mtype == "turn-complete":
            if llm_started:
                log.info("%s: turn-complete (llm_started=True)", label)
                turn_done = True
            else:
                log.debug("%s: stale turn-complete (skipping)", label)

        elif mtype == "error":
            ec = payload.get("error_code", "")
            msg_text = payload.get("message", "") or payload.get("detail", "")
            log.warning("%s: error envelope error_code=%s msg=%s", label, ec, msg_text)
            if first_error_code is None:
                first_error_code = ec
                error_text = msg_text
            # USER_INPUT_REQUIRED on first tool call = args invalid
            if "USER_INPUT_REQUIRED" in ec or "MISSING_ARG" in ec or "VALIDATION" in ec.upper():
                if not tools_fired:
                    args_valid = False
            # Don't break on error - model may recover

        elif mtype in ("case-update", "case-list", "layer-uri", "layer-update",
                       "heartbeat", "session-state", "case-open"):
            log.debug("%s: housekeeping type=%s", label, mtype)

    wall_time = time.monotonic() - t_start

    # If we cancelled a solver, drain any lingering frames briefly
    if cancelled:
        await _drain_after_cancel(ws, label, timeout=5)

    result = {
        "tools_fired": tools_fired,
        "args_valid": args_valid,
        "wall_time": round(wall_time, 1),
        "cancelled": cancelled,
        "error_text": error_text,
        "first_error_code": first_error_code,
    }
    log.info(
        "%s: done  tools=%s  args_valid=%s  wall=%.1fs  cancelled=%s",
        label, tools_fired, args_valid, wall_time, cancelled,
    )
    return result


async def _drain_after_cancel(ws, label: str, timeout: float = 5.0) -> None:
    """Drain residual frames for a few seconds after cancel."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 2))
            msg = json.loads(raw)
            log.debug("DRAIN [%s] type=%s", label, msg.get("type"))
            if msg.get("type") == "turn-complete":
                break
        except asyncio.TimeoutError:
            break
        except Exception:
            break


# ---------------------------------------------------------------------------
# Handshake helpers
# ---------------------------------------------------------------------------
async def do_handshake(ws, session_id: str) -> None:
    """Auth + session-resume + drain until session-state."""
    await ws.send(mk("auth-token", session_id, {"token": "", "anonymous_user_id": None}))
    ack_raw = await asyncio.wait_for(ws.recv(), timeout=15)
    ack = json.loads(ack_raw)
    assert ack["type"] == "auth-ack", f"expected auth-ack got {ack['type']}"
    log.info("auth-ack OK user_id=%s", ack["payload"].get("user_id"))

    await ws.send(mk("session-resume", session_id, {"case_id": None}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        msg = json.loads(raw)
        log.debug("HANDSHAKE type=%s", msg["type"])
        if msg["type"] == "session-state":
            log.info("session-state OK")
            break


async def create_case(ws, session_id: str, title: str) -> str:
    """Create a new case and return its case_id."""
    await ws.send(mk("case-command", session_id, {"command": "create", "args": {"title": title}}))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=15)
        msg = json.loads(raw)
        log.debug("CASE_CREATE type=%s", msg["type"])
        if msg["type"] == "case-open":
            ss = msg["payload"].get("session_state")
            if ss:
                case_id = ss["case"]["case_id"]
                log.info("case-open OK case_id=%s", case_id)
                return case_id
        # Drain stray frames (case-list etc.)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_prompt(spec: dict, result: dict) -> dict:
    """Compute SELECTED_CORRECT / SELECTED_WRONG / NO_CALL for one prompt."""
    expected_set = spec["expected_set"]
    tools_fired = result["tools_fired"]
    is_no_tool = len(expected_set) == 0  # prompt 15

    first_tool = tools_fired[0] if tools_fired else None

    if is_no_tool:
        # Prompt 15: any tool = false positive
        if tools_fired:
            verdict = "FALSE_POSITIVE"
        else:
            verdict = "NO_CALL_CORRECT"
    elif not tools_fired:
        verdict = "NO_CALL"
    elif first_tool in expected_set:
        verdict = "SELECTED_CORRECT"
    else:
        # Check if correct tool appears anywhere in chain (chain match)
        if any(t in expected_set for t in tools_fired):
            verdict = "CHAIN_CORRECT"
        else:
            verdict = "SELECTED_WRONG"

    return {
        "id": spec["id"],
        "short": spec["short"],
        "expected": spec["expected"],
        "first_tool": first_tool,
        "all_tools": tools_fired,
        "verdict": verdict,
        "args_valid": result["args_valid"],
        "wall_time": result["wall_time"],
        "cancelled": result["cancelled"],
        "error_text": result.get("error_text"),
        "error_code": result.get("first_error_code"),
    }


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------
def write_report(scores: list[dict]) -> None:
    lines: list[str] = []

    lines.append("# Tool Routing Benchmark -- qwen3:8b-16k + /no_think")
    lines.append("")
    lines.append("**Date:** 2026-07-05  ")
    lines.append("**Model:** qwen3:8b-16k (Ollama, /no_think mode)  ")
    lines.append("**Tools registered:** 176  ")
    lines.append("**Agent:** ws://localhost:8765  ")
    lines.append("")

    # Summary table
    lines.append("## Results Table")
    lines.append("")
    lines.append("| # | Prompt (short) | Expected | Actual_First_Tool | Result | Args_Valid | Wall_Time_s |")
    lines.append("|---|---------------|----------|-------------------|--------|------------|-------------|")

    correct_count = 0
    chain_correct_count = 0
    no_call_count = 0
    false_pos_count = 0
    wrong_count = 0
    valid_args_count = 0
    total_tools_expected = 0
    times_to_first: list[float] = []

    for s in scores:
        v = s["verdict"]
        first = s["first_tool"] or "-"
        av = "YES" if s["args_valid"] else "NO"
        wt = f"{s['wall_time']:.1f}s"

        if v == "SELECTED_CORRECT":
            v_disp = "CORRECT"
            correct_count += 1
        elif v == "CHAIN_CORRECT":
            v_disp = "CHAIN_CORRECT"
            chain_correct_count += 1
        elif v == "SELECTED_WRONG":
            v_disp = "WRONG"
            wrong_count += 1
        elif v == "NO_CALL":
            v_disp = "NO_CALL"
            no_call_count += 1
        elif v == "NO_CALL_CORRECT":
            v_disp = "NO_CALL_CORRECT"
        elif v == "FALSE_POSITIVE":
            v_disp = "FALSE_POSITIVE"
            false_pos_count += 1
        else:
            v_disp = v

        lines.append(
            f"| {s['id']} | {s['short']} | {s['expected']} | `{first}` "
            f"| {v_disp} | {av} | {wt} |"
        )

        if s["id"] != 15 and s["all_tools"]:
            times_to_first.append(s["wall_time"])
        if s["id"] != 15:
            total_tools_expected += 1
            if v in ("SELECTED_CORRECT", "CHAIN_CORRECT"):
                valid_args_count += (1 if s["args_valid"] else 0)

    lines.append("")

    # Per-prompt notes
    lines.append("## Per-Prompt Notes")
    lines.append("")
    for s in scores:
        lines.append(f"### P{s['id']} -- {s['short']}")
        lines.append(f"- **Expected:** {s['expected']}")
        lines.append(f"- **Tools fired (in order):** {s['all_tools'] or ['(none)']}")
        lines.append(f"- **Verdict:** {s['verdict']}")
        lines.append(f"- **Args valid:** {s['args_valid']}")
        lines.append(f"- **Wall time:** {s['wall_time']:.1f}s")
        if s["cancelled"]:
            lines.append("- **Cancelled:** YES (solver cancel on start)")
        if s["error_text"]:
            lines.append(f"- **Error:** `{s['error_code']}` -- {s['error_text'][:120]}")
        lines.append("")

    # Stats
    total = len(scores)
    tool_prompts = total - 1  # exclude prompt 15 (no-tool)
    sel_correct = correct_count + chain_correct_count
    sel_accuracy = round(100.0 * sel_correct / tool_prompts, 1)
    false_pos_rate = f"{false_pos_count}/1"
    mean_ttft = (
        round(sum(times_to_first) / len(times_to_first), 1)
        if times_to_first else 0.0
    )

    lines.append("## Overall Stats")
    lines.append("")
    lines.append(f"- **Prompts run:** {total}")
    lines.append(f"- **Tool prompts (1-14):** {tool_prompts}")
    lines.append(f"- **SELECTED_CORRECT (first tool exact match):** {correct_count}/{tool_prompts}")
    lines.append(f"- **CHAIN_CORRECT (correct tool appeared, not first):** {chain_correct_count}/{tool_prompts}")
    lines.append(f"- **Total selection accuracy (correct + chain):** {sel_correct}/{tool_prompts} = {sel_accuracy}%")
    lines.append(f"- **SELECTED_WRONG:** {wrong_count}/{tool_prompts}")
    lines.append(f"- **NO_CALL (tool expected, none fired):** {no_call_count}/{tool_prompts}")
    lines.append(f"- **False-positive rate (P15 haiku):** {false_pos_rate}")
    lines.append(f"- **Mean time to completion (tool prompts with tool fired):** {mean_ttft}s")
    lines.append("")

    # Verdict
    lines.append("## VERDICT")
    lines.append("")
    if sel_accuracy >= 80:
        verdict_text = (
            f"**ADEQUATE -- RAG top-k retrieval NOT urgently needed.**  "
            f"Selection accuracy {sel_accuracy}% (>= 80% threshold). "
            f"qwen3:8b-16k with 176 tools in context is routing correctly for most task types. "
        )
    else:
        verdict_text = (
            f"**RAG TOP-K RETRIEVAL RECOMMENDED.**  "
            f"Selection accuracy {sel_accuracy}% (< 80% threshold) indicates the 8B model "
            f"is struggling to select the right tool from a 176-tool context. "
            f"RAG-based tool pre-selection (top-k relevant tools injected per query) "
            f"would reduce context load and likely improve routing accuracy significantly. "
        )

    if false_pos_count > 0:
        verdict_text += (
            f"The model called a tool for the no-tool haiku prompt (false positive), "
            f"suggesting over-triggering on tool use. "
        )
    else:
        verdict_text += (
            f"The model correctly abstained from tool use for the no-tool prompt (haiku). "
        )

    no_call_frac = round(100.0 * no_call_count / tool_prompts, 1)
    if no_call_count > 2:
        verdict_text += (
            f"High NO_CALL rate ({no_call_frac}%) suggests the model is answering "
            f"in prose rather than dispatching tools for some task categories -- "
            f"system prompt reinforcement or few-shot examples may help. "
        )

    lines.append(verdict_text)
    lines.append("")

    report_text = "\n".join(lines)
    REPORT_FILE.write_text(report_text)
    log.info("Report written to %s", REPORT_FILE)


# ---------------------------------------------------------------------------
# Main benchmark loop
# ---------------------------------------------------------------------------
async def run_bench() -> list[dict]:
    scores: list[dict] = []

    print(f"\n=== TRID3NT tool-routing benchmark: {len(PROMPTS)} prompts ===")
    print(f"WS: {WS_URL}")
    print(f"Log: {LOG_FILE}")
    print(f"Report: {REPORT_FILE}\n")

    for spec in PROMPTS:
        pid = spec["id"]
        print(f"\n{'='*60}")
        print(f"[P{pid}/15] {spec['short']}")

        try:
            # Fresh session + case per prompt
            session_id = new_id()
            async with ws_client.connect(WS_URL, open_timeout=15, close_timeout=10) as ws:
                await do_handshake(ws, session_id)
                case_id = await create_case(ws, session_id, f"bench-p{pid}")

                result = await run_one_prompt(ws, session_id, case_id, spec)

        except asyncio.TimeoutError as e:
            log.error("P%d: timeout: %s", pid, e)
            result = {
                "tools_fired": [],
                "args_valid": False,
                "wall_time": MAX_TURN_TIMEOUT,
                "cancelled": False,
                "error_text": f"Timeout: {e}",
                "first_error_code": "TIMEOUT",
            }
        except Exception as e:
            log.error("P%d: exception: %s", pid, e, exc_info=True)
            result = {
                "tools_fired": [],
                "args_valid": False,
                "wall_time": 0.0,
                "cancelled": False,
                "error_text": str(e),
                "first_error_code": "EXCEPTION",
            }

        sc = score_prompt(spec, result)
        scores.append(sc)
        print(f"  RESULT: {sc['verdict']}  first_tool={sc['first_tool']!r}  "
              f"args_valid={sc['args_valid']}  t={sc['wall_time']:.1f}s")

        # Brief pause between prompts to let the server settle
        await asyncio.sleep(2)

    return scores


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _setup_logging()
    scores = asyncio.run(run_bench())
    write_report(scores)

    # Print summary table to stdout
    print("\n" + "="*80)
    print("FINAL RESULTS")
    print("="*80)
    print(f"{'#':3} {'Prompt':22} {'Expected':28} {'Actual':28} {'Result':18} {'AV':4} {'Time':7}")
    print("-"*115)
    for s in scores:
        print(
            f"{s['id']:3} {s['short']:22} {s['expected'][:27]:28} "
            f"{str(s['first_tool'] or '-')[:27]:28} {s['verdict']:18} "
            f"{'Y' if s['args_valid'] else 'N':4} {s['wall_time']:>6.1f}s"
        )
    print("-"*115)

    tool_prompts = len(scores) - 1
    correct = sum(1 for s in scores if s["verdict"] in ("SELECTED_CORRECT", "CHAIN_CORRECT"))
    wrong = sum(1 for s in scores if s["verdict"] == "SELECTED_WRONG")
    no_call = sum(1 for s in scores if s["verdict"] == "NO_CALL")
    fp = sum(1 for s in scores if s["verdict"] == "FALSE_POSITIVE")
    acc = round(100.0 * correct / tool_prompts, 1)

    print(f"\nSelection accuracy (P1-P14): {correct}/{tool_prompts} = {acc}%")
    print(f"WRONG: {wrong}  NO_CALL: {no_call}  FALSE_POSITIVE(P15): {fp}/1")
    print(f"\nReport: {REPORT_FILE}")
