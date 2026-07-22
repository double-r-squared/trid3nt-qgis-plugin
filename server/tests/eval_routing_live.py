"""Wave 4.10 — LIVE routing-correctness eval harness (anchor-set, Bayesian-adaptive).

Purpose
-------
Measure the *routing correctness* of the agent (Gemini-2.5-pro through
the FunctionTool registry) against a small, hand-picked anchor set of
prompts. Each anchor asserts (a) the first tool the model should dispatch and
(b) the set of tools that must appear in the observed chain.

Methodology constraints
-----------------------
- **Anchor-set, Bayesian-adaptive.** The anchor list is the *prior*. Future
  Wave 4.10 stages add probes adaptively; this Stage-0 baseline runs every
  anchor exactly once and records observations honestly.
- **NO ``__grace2Inject*`` seams.** Per memory
  ``feedback_playwright_must_drive_live_agent``, the driver must navigate to
  the live Vite dev server, accept the AuthGate, type into the real
  ``[data-testid="chat-input"]``, press Enter, and observe real ``pipeline-state``
  envelopes coming back over the WebSocket. The Gemini call is real — that
  is the point of the baseline.
- **Proactive screenshot capture.** A full-page screenshot is captured per
  anchor at the moment of (a) terminal pipeline state, (b) terminal agent
  message, or (c) the per-anchor watchdog firing — whichever happens first.

Pre-conditions for running
--------------------------
  - Agent backend on 127.0.0.1:8765 (``make run-agent``)
  - Web dev server on 127.0.0.1:5173 (``make run-web``)

Invoke
------
    .venv-agent/bin/python server/tests/eval_routing_live.py
    # Re-run a single anchor (Stage 4 Bayesian-adaptive selection):
    .venv-agent/bin/python server/tests/eval_routing_live.py --anchor A4_composite_workflow

Outputs
-------
  /tmp/wave4_10_baseline/<prompt_id>.png            — per-anchor screenshot
  /tmp/wave4_10_baseline/baseline_metrics.json      — aggregate + per-anchor JSON

Anchor selection rationale (5 prompts, 5 routing dimensions)
------------------------------------------------------------
Each anchor exercises a distinct routing dimension we expect Wave 4.10 to
move on. The pre-4.10 *expectation* column predicts the baseline outcome so
audit can compare against measured behaviour.

Harness version
---------------
wave-4-10-stage-1
  - Per-anchor watchdog_seconds field (default 120; A4 composite gets 900).
  - wait_for_agent_idle() helper replacing bare post-anchor sleep between anchors.
  - Error envelope classification: session-bookkeeping envelopes separated from
    routing envelopes so Stage 4 correctness metrics are not inflated by
    backend hygiene noise.
  - --anchor <id> CLI flag for single-anchor re-runs (Bayesian-adaptive selection).
  - harness_version field in baseline_metrics.json for drift detection.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "http://127.0.0.1:5173"
# Accept either host literal — the web client defaults to ``localhost:8765``
# but Playwright sometimes normalizes. Match on the port substring.
WS_HOST_HINT = ":8765"
OUT_DIR = Path("/tmp/wave4_10_baseline")
OUT_DIR.mkdir(parents=True, exist_ok=True)

ANON_KEY = "grace2_anonymous_accepted"

# Default per-anchor watchdog; individual anchors may override via
# ``watchdog_seconds``.  NFR-P-4 budgets 15 min for a small-domain flood;
# A4's composite workflow legitimately needs that full window.
DEFAULT_WATCHDOG_S = 120.0

# How long wait_for_agent_idle() polls for the chat-input to re-enable (or
# session-state idle envelope) before giving up and letting the next anchor
# proceed anyway.  This is a courtesy cooldown, not a hard failure.
INTER_ANCHOR_IDLE_TIMEOUT_S = 90.0

# Harness version token — included in baseline_metrics.json so Stage 4 can
# detect if the harness itself changed between measurement rounds.
HARNESS_VERSION = "wave-4-10-stage-1"

# ---------------------------------------------------------------------------
# Error envelope classification
# ---------------------------------------------------------------------------

# Envelope ``role`` values (or ``error_code`` prefixes) that represent normal
# backend bookkeeping activity rather than routing failures.  These are
# excluded from the ``n_routing_error_envelopes`` count that feeds correctness
# metrics.  The full raw list is still preserved in ``error_envelopes`` for
# forensic inspection.
_SESSION_BOOKKEEPING_ROLES = frozenset(
    {
        "session-bookkeeping",
        "session_bookkeeping",
        "session-init",
        "session_init",
        "heartbeat",
        "keepalive",
    }
)
_SESSION_BOOKKEEPING_CODE_PREFIXES = ("SESSION_", "KEEPALIVE_", "HEARTBEAT_")


def _classify_error_envelope(env: dict) -> str:
    """Return ``"routing"`` or ``"session-bookkeeping"`` for an error envelope.

    Classification rules (in priority order):
    1. Explicit ``role`` field present and in the bookkeeping set → bookkeeping.
    2. ``error_code`` starts with a bookkeeping prefix → bookkeeping.
    3. Anything else → routing (conservative; counts against correctness).
    """
    payload = env.get("payload") or {}
    role = (env.get("role") or payload.get("role") or "").lower()
    if role in _SESSION_BOOKKEEPING_ROLES:
        return "session-bookkeeping"
    error_code = (env.get("error_code") or payload.get("error_code") or "")
    for prefix in _SESSION_BOOKKEEPING_CODE_PREFIXES:
        if error_code.startswith(prefix):
            return "session-bookkeeping"
    return "routing"


# ---------------------------------------------------------------------------
# Anchor set (the prior)
# ---------------------------------------------------------------------------

ANCHOR_PROMPTS: list[dict] = [
    {
        "id": "A1_geocode_simple",
        "prompt": (
            "Where is Fort Myers, Florida? Just give me the coordinates — "
            "no follow-up questions, dispatch a tool if you need one."
        ),
        "expected_tool_first": ["geocode_location"],
        "expected_chain_contains": ["geocode_location"],
        "routing_dimension": "single-tool-trivial",
        "watchdog_seconds": 120,
        "pre_4_10_expected_note": (
            "Pre-4.10 baseline SHOULD pass — geocode is the canonical Wave 1 "
            "tool, registered + advertised in TOOL_REGISTRY, and the prompt "
            "directly names the city. Failure here implies a regression in "
            "the basic dispatch loop."
        ),
    },
    {
        "id": "A2_existing_endpoint_chain",
        "prompt": (
            "Show me protected areas in Big Cypress National Preserve, Florida. "
            "Use fetch_wdpa_protected_areas. Don't ask follow-up questions, "
            "just dispatch the tools."
        ),
        "expected_tool_first": ["geocode_location", "fetch_wdpa_protected_areas"],
        "expected_chain_contains": [
            "fetch_wdpa_protected_areas",
        ],
        "routing_dimension": "named-existing-endpoint",
        "watchdog_seconds": 240,
        "pre_4_10_expected_note": (
            "Pre-4.10 baseline SHOULD pass. The prompt names the tool by "
            "function name, and the chain is already proven by job-0175 "
            "live evidence. First-tool may be geocode_location (LLM may "
            "fan out to bbox first) — both are acceptable per the anchor."
        ),
    },
    {
        "id": "A3_new_endpoint_not_yet_registered",
        "prompt": (
            "Show me the latest HRRR weather forecast for Fort Myers, Florida. "
            "Use the HRRR fetch tool. Don't ask follow-up questions, just "
            "dispatch the tool."
        ),
        # Stage 4 update: fetch_hrrr_forecast requires a bbox argument, so
        # geocode_location for "Fort Myers" is a legitimate precursor (matches
        # the A2 anchor design which accepts geocode_location as valid first
        # tool when the named tool requires a bbox).  The full-chain check
        # below still requires fetch_hrrr_forecast to be dispatched.
        "expected_tool_first": ["fetch_hrrr_forecast", "fetch_hrrr", "geocode_location"],
        "expected_chain_contains": ["fetch_hrrr_forecast"],
        "routing_dimension": "A3-new-endpoint",
        # Stage 4 update: bumped to 300s so the precursor+named-tool chain has
        # room to complete (previous 120s truncated after geocode finished).
        "watchdog_seconds": 300,
        "pre_4_10_expected_note": (
            "Pre-4.10 baseline EXPECTED TO FAIL — HRRR is in the Wave 4.10 "
            "data-coverage gap list (project_grace1_endpoint_inventory) and "
            "is NOT yet a registered FunctionTool. Gemini should either (a) "
            "refuse with prose ('no tool exists'), (b) hallucinate a "
            "non-existent function name (rejected by registry, surfaces as "
            "TOOL_NOT_FOUND), or (c) fall back to a related fetcher (e.g. "
            "fetch_nws_alerts_conus, fetch_mrms_qpe). Any of these is a "
            "valid pre-4.10 baseline observation."
        ),
    },
    {
        "id": "A4_composite_workflow",
        "prompt": (
            "Model the flood from Hurricane Ian on Fort Myers using SFINCS. "
            "Use a 12-hour rainfall window. Don't ask follow-up questions, "
            "just dispatch the workflow."
        ),
        "expected_tool_first": [
            "geocode_location",
            "run_model_flood_scenario",
            "model_flood_scenario",
        ],
        "expected_chain_contains": [
            "geocode_location",
        ],
        "routing_dimension": "composite-workflow",
        # NFR-P-4: end-to-end small-domain flood ≤ 15 min (900 s).  The
        # 120 s default would always truncate before the model can finish, so
        # this anchor explicitly extends the watchdog to the full NFR budget.
        "watchdog_seconds": 900,
        "pre_4_10_expected_note": (
            "Pre-4.10 baseline MAY PASS partially. The SFINCS demo is the "
            "M5 acceptance path; geocode_location is a near-certainty as "
            "the first dispatch. Whether the full workflow runs (which "
            "takes 10–20 min wall-clock per SRS NFR-P-4) depends on the "
            "watchdog — we will likely see the first tool dispatched "
            "but the chain truncated at watchdog. Routing-correctness "
            "credit is awarded for first-tool match; full-sequence credit "
            "is NOT awarded if the watchdog truncates."
        ),
    },
    {
        "id": "A5_geographic_clip",
        "prompt": (
            "Show me roads within Lee County, Florida. Clip the OSM roads "
            "to the county polygon. Don't ask follow-up questions, just "
            "dispatch the tools."
        ),
        "expected_tool_first": [
            "fetch_administrative_boundaries",
            "geocode_location",
            "fetch_roads_osm",
        ],
        "expected_chain_contains": [
            "fetch_administrative_boundaries",
            "fetch_roads_osm",
        ],
        "routing_dimension": "geographic-clipping-pattern",
        "watchdog_seconds": 120,
        "pre_4_10_expected_note": (
            "Pre-4.10 baseline UNCERTAIN. Per memory "
            "feedback_geographic_clipping_pattern, the agent SHOULD route "
            "fetch_administrative_boundaries → clip_vector_to_polygon → "
            "fetch_roads_osm, but as of Wave 4.9 there is no system-prompt "
            "guidance enforcing the polygon-clip pattern. The model is "
            "likely to use bbox-mode (fetch_roads_osm with a radius) "
            "instead of the clip workflow. Captures the current 'clipping "
            "pattern not yet wired' state as a baseline data point."
        ),
    },
]


# ---------------------------------------------------------------------------
# Inter-anchor idle wait
# ---------------------------------------------------------------------------


async def wait_for_agent_idle(page, timeout: float = INTER_ANCHOR_IDLE_TIMEOUT_S) -> bool:
    """Poll until the chat-input re-enables OR a session-state-idle envelope arrives.

    This replaces the bare ``page.wait_for_timeout(sleep_ms)`` between anchors
    that caused the A5 driver race (previous anchor's long composite workflow
    left the backend session busy; the fresh browser context for the next anchor
    couldn't get ``chat-input`` enabled within 60 s).

    Strategy
    --------
    1. First check whether ``chat-input`` is already enabled — if so, return
       immediately (common fast path for short anchors).
    2. Otherwise enter a 2-second poll loop watching for either:
       - ``chat-input`` element without the ``disabled`` attribute, OR
       - a ``session-state`` WebSocket envelope carrying ``state == "idle"``.
    3. If ``timeout`` expires without either signal, log a warning and return
       ``False`` — the caller proceeds anyway (best-effort; the next anchor
       may see a slightly slower first response if the session is still warming
       up, but we should not block forever).

    Returns
    -------
    True  — idle signal received within ``timeout``.
    False — timed out; caller should proceed but note the condition.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # Check DOM state: is chat-input present and not disabled?
            is_enabled = await page.evaluate(
                "() => {"
                "  const el = document.querySelector('[data-testid=\"chat-input\"]');"
                "  return el !== null && !el.disabled;"
                "}"
            )
            if is_enabled:
                return True
        except Exception:
            pass  # page may be navigating; ignore and retry
        await page.wait_for_timeout(2000)
    return False


# ---------------------------------------------------------------------------
# Live driver
# ---------------------------------------------------------------------------


async def run_anchor(playwright, prompt_spec: dict) -> dict:
    """Drive one anchor through the live web client + agent.

    Returns a dict with the observed routing chain, the agent's final text
    (best-effort scrape), any error envelopes seen (raw), routing vs
    session-bookkeeping envelope counts, total wall-clock duration, and the
    path of the captured screenshot.

    Watchdog duration is taken from ``prompt_spec["watchdog_seconds"]``
    (default ``DEFAULT_WATCHDOG_S``).  A4's 900 s override lets the SFINCS
    composite workflow run to completion within NFR-P-4's 15-minute budget.
    """
    prompt_id = prompt_spec["id"]
    prompt_text = prompt_spec["prompt"]
    watchdog_s = float(prompt_spec.get("watchdog_seconds", DEFAULT_WATCHDOG_S))
    shot_path = OUT_DIR / f"{prompt_id}.png"

    # ``gemini_generate`` is the bookkeeping LLM-thinking step that always
    # appears in the pipeline regardless of routing — exclude it when
    # computing first_tool / full_chain so the routing-correctness measure
    # reflects ACTUAL tool dispatches, not Gemini's thinking phase.
    LLM_STEP_TOOL_NAMES = {"gemini_generate"}

    started = time.monotonic()
    full_chain: list[str] = []
    error_envelopes: list[dict] = []
    agent_final_text: str = ""
    pipeline_terminal: bool = False
    agent_terminal: bool = False
    console_msgs: list[str] = []
    ws_frame_count: int = 0

    browser = await playwright.chromium.launch(headless=True)
    ctx = await browser.new_context(viewport={"width": 1600, "height": 1000})
    page = await ctx.new_page()
    page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text}"))
    page.on("pageerror", lambda e: console_msgs.append(f"[pageerror] {e}"))

    # Track every JSON frame on every WebSocket the page opens. We pluck
    # ``tool_name`` strings out of ``pipeline-state`` envelopes to build
    # the observed chain.
    def _on_ws(ws):
        if WS_HOST_HINT not in ws.url:
            # ignore any HMR / vite WS
            return

        def _on_frame(payload):
            nonlocal ws_frame_count
            ws_frame_count += 1
            try:
                env = json.loads(payload)
            except Exception:
                return
            envelope_type = env.get("type") or env.get("envelope_type")
            payload_obj = env.get("payload") or {}
            if envelope_type == "pipeline-state":
                # Steps is a list of {step_id, name, tool_name, state, ...}.
                for step in payload_obj.get("steps") or []:
                    tn = step.get("tool_name")
                    if not tn or tn in LLM_STEP_TOOL_NAMES:
                        continue
                    # We want the order of *first appearance* of each tool;
                    # pipeline-state is emitted on every transition, so the
                    # same tool will be seen many times.
                    if tn not in full_chain:
                        full_chain.append(tn)
                # terminal pipeline = all steps complete/failed/cancelled
                steps = payload_obj.get("steps") or []
                if steps and all(
                    s.get("state") in ("complete", "failed", "cancelled")
                    for s in steps
                ):
                    nonlocal_terminal()
            elif envelope_type == "agent-message-chunk":
                delta = payload_obj.get("delta", "")
                # Accumulate text deltas; "done" closes the message.
                nonlocal agent_final_text  # noqa: PLW0127
                agent_final_text += delta
                if payload_obj.get("done"):
                    set_agent_terminal()
            elif envelope_type == "error":
                error_envelopes.append(env)

        ws.on("framereceived", _on_frame)

    # Helpers to flip the outer flags from the inner closure.
    def nonlocal_terminal():
        nonlocal pipeline_terminal
        pipeline_terminal = True

    def set_agent_terminal():
        nonlocal agent_terminal
        agent_terminal = True

    page.on("websocket", _on_ws)

    try:
        # 1) Navigate + accept anon gate.
        await page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
        await page.evaluate(f"() => localStorage.setItem('{ANON_KEY}', 'true')")
        await page.goto(BASE_URL, wait_until="load", timeout=60_000)
        await page.wait_for_timeout(1500)

        # If the anon gate is visible, click through.
        try:
            anon_btn = page.locator(
                '[data-testid="grace2-auth-gate-anonymous"]'
            ).first
            await anon_btn.wait_for(state="visible", timeout=5000)
            await anon_btn.click()
            await page.wait_for_timeout(1500)
        except Exception:
            pass  # already past

        # 2) Wait for app shell + chat input (must be enabled — WS-connected).
        await page.wait_for_selector(
            '[data-testid="grace2-app-shell"]', timeout=30_000
        )
        try:
            await page.wait_for_selector(
                '[data-testid="chat-input"]:not([disabled])', timeout=60_000
            )
        except Exception:
            # Diagnostic: capture what the page looks like + WS connection
            # status so we can debug honestly instead of silently retry.
            diag_path = OUT_DIR / f"{prompt_id}_disabled_diag.png"
            await page.screenshot(path=str(diag_path), full_page=False)
            try:
                shell_state = await page.evaluate(
                    "() => ({"
                    "  shell: document.querySelector('[data-testid=\\\"grace2-app-shell\\\"]')?.outerHTML?.slice(0, 300),"
                    "  input_disabled: document.querySelector('[data-testid=\\\"chat-input\\\"]')?.disabled,"
                    "  ws_state: window.__grace2_ws_state || 'unknown',"
                    "})"
                )
            except Exception as exc2:
                shell_state = {"diag_error": str(exc2)}
            console_msgs.append(f"[disabled-diag] {shell_state}")
            raise
        await page.wait_for_timeout(1500)

        # 3) Type + submit the prompt.
        chat_input = page.locator('[data-testid="chat-input"]').first
        await chat_input.click()
        await chat_input.fill("")
        await page.wait_for_timeout(150)
        await chat_input.fill(prompt_text)
        await page.wait_for_timeout(250)
        await page.keyboard.press("Enter")

        # 4) Watchdog poll: exit early when BOTH pipeline terminal AND agent
        #    terminal AND the expected chain is satisfied, OR watchdog fires.
        #    Previously this exited on `pipeline_terminal OR agent_terminal`,
        #    which fired between turns when an early tool completed but the
        #    follow-on tool hadn't started — truncating multi-turn chains.
        deadline = time.monotonic() + watchdog_s
        expected_chain_contains = set(prompt_spec.get("expected_chain_contains", []))
        while time.monotonic() < deadline:
            await page.wait_for_timeout(2000)
            chain_satisfied = (
                expected_chain_contains.issubset(set(full_chain))
                if expected_chain_contains
                else True
            )
            if pipeline_terminal and agent_terminal and chain_satisfied:
                # Give the map / UI a beat to settle visually.
                await page.wait_for_timeout(2000)
                break

        # 5) Capture the screenshot.
        await page.screenshot(path=str(shot_path), full_page=False)

    finally:
        await ctx.close()
        await browser.close()

    duration = time.monotonic() - started

    # Classify error envelopes: routing errors vs session-bookkeeping noise.
    routing_errors = [e for e in error_envelopes if _classify_error_envelope(e) == "routing"]
    bookkeeping_errors = [e for e in error_envelopes if _classify_error_envelope(e) == "session-bookkeeping"]

    return {
        "prompt_id": prompt_id,
        "first_tool": full_chain[0] if full_chain else None,
        "full_chain": full_chain,
        "agent_final_text": agent_final_text.strip(),
        "error_envelopes": error_envelopes,
        "n_routing_error_envelopes": len(routing_errors),
        "n_bookkeeping_error_envelopes": len(bookkeeping_errors),
        "duration_seconds": round(duration, 2),
        "screenshot_path": str(shot_path),
        "pipeline_terminal": pipeline_terminal,
        "agent_terminal": agent_terminal,
        "ws_frame_count": ws_frame_count,
        "console_tail": console_msgs[-30:],
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


async def score_run(run: dict, prompt_spec: dict) -> dict:
    """Score a single anchor run against its spec.

    - first_tool_correct: bool — the first dispatched tool is in
      ``expected_tool_first`` (acceptable set).
    - full_sequence_correct: bool — every member of ``expected_chain_contains``
      is observed somewhere in ``full_chain``.
    - had_routing_error: bool — at least one *routing* error envelope was seen
      (session-bookkeeping envelopes are excluded from this flag).
    """
    expected_first = set(prompt_spec["expected_tool_first"])
    expected_chain = set(prompt_spec["expected_chain_contains"])
    observed_chain = run["full_chain"]
    observed_set = set(observed_chain)

    first_tool_correct = (
        run["first_tool"] is not None and run["first_tool"] in expected_first
    )
    full_sequence_correct = bool(expected_chain) and expected_chain.issubset(
        observed_set
    )

    return {
        "prompt_id": prompt_spec["id"],
        "routing_dimension": prompt_spec["routing_dimension"],
        "first_tool_correct": first_tool_correct,
        "full_sequence_correct": full_sequence_correct,
        "observed_first_tool": run["first_tool"],
        "expected_first_tool_any_of": sorted(expected_first),
        "observed_full_chain": observed_chain,
        "expected_chain_contains": sorted(expected_chain),
        "missing_from_chain": sorted(expected_chain - observed_set),
        "extras_in_chain": sorted(observed_set - expected_chain),
        "duration_seconds": run["duration_seconds"],
        # Legacy field (total); kept for backward compatibility.
        "had_error_envelope": bool(run["error_envelopes"]),
        "n_error_envelopes": len(run["error_envelopes"]),
        # New classified fields — use these for correctness metrics.
        "had_routing_error": run["n_routing_error_envelopes"] > 0,
        "n_routing_error_envelopes": run["n_routing_error_envelopes"],
        "n_bookkeeping_error_envelopes": run["n_bookkeeping_error_envelopes"],
        "pipeline_terminal": run["pipeline_terminal"],
        "agent_terminal": run["agent_terminal"],
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate(scores: list[dict]) -> dict:
    """Roll per-anchor scores up into overall metrics."""
    if not scores:
        return {
            "first_tool_correctness_pct": 0.0,
            "full_sequence_correctness_pct": 0.0,
            "per_dimension_breakdown": {},
        }
    n = len(scores)
    first_n = sum(1 for s in scores if s["first_tool_correct"])
    full_n = sum(1 for s in scores if s["full_sequence_correct"])

    per_dim: dict[str, dict] = {}
    for s in scores:
        dim = s["routing_dimension"]
        bucket = per_dim.setdefault(
            dim,
            {
                "total": 0,
                "first_tool_correct": 0,
                "full_sequence_correct": 0,
                "anchor_ids": [],
            },
        )
        bucket["total"] += 1
        bucket["first_tool_correct"] += int(s["first_tool_correct"])
        bucket["full_sequence_correct"] += int(s["full_sequence_correct"])
        bucket["anchor_ids"].append(s["prompt_id"])

    return {
        "first_tool_correctness_pct": round(100.0 * first_n / n, 1),
        "full_sequence_correctness_pct": round(100.0 * full_n / n, 1),
        "per_dimension_breakdown": per_dim,
        "n_anchors": n,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _git_head() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(pathlib.Path(__file__).resolve().parents[2]),
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return "unknown"


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments.

    --anchor <id>
        Re-run only the named anchor.  Accepts either a bare anchor id
        (e.g. ``A4_composite_workflow``) or a comma-separated list
        (e.g. ``A1_geocode_simple,A4_composite_workflow``).  Used by the
        Stage 4 Bayesian-adaptive selection loop to re-probe high-uncertainty
        anchors without re-running the full set.
    """
    parser = argparse.ArgumentParser(
        description="Wave 4.10 routing-correctness eval harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--anchor",
        metavar="ID[,ID...]",
        default=None,
        help=(
            "Run only the specified anchor id(s). "
            "Accepts a single id or a comma-separated list. "
            "If omitted, all anchors in the prior are run."
        ),
    )
    return parser.parse_args()


def _select_anchors(anchor_arg: str | None) -> list[dict]:
    """Return the subset of ANCHOR_PROMPTS to run based on --anchor."""
    if anchor_arg is None:
        return ANCHOR_PROMPTS
    requested = {a.strip() for a in anchor_arg.split(",") if a.strip()}
    selected = [a for a in ANCHOR_PROMPTS if a["id"] in requested]
    missing = requested - {a["id"] for a in selected}
    if missing:
        raise SystemExit(
            f"Unknown anchor id(s): {', '.join(sorted(missing))}\n"
            f"Available: {', '.join(a['id'] for a in ANCHOR_PROMPTS)}"
        )
    return selected


async def main() -> int:
    from playwright.async_api import async_playwright

    args = _parse_args()
    anchors_to_run = _select_anchors(args.anchor)

    print(f"=== Wave 4.10 routing baseline — {len(anchors_to_run)} anchor(s) ===")
    print(f"BASE_URL={BASE_URL}  WS_HOST_HINT={WS_HOST_HINT}")
    print(f"HARNESS_VERSION={HARNESS_VERSION}")
    print(f"Output dir: {OUT_DIR}\n")

    runs: list[dict] = []
    scores: list[dict] = []

    async with async_playwright() as pw:
        for i, spec in enumerate(anchors_to_run):
            print(f"--- {spec['id']} ({spec['routing_dimension']}) ---")
            print(f"    prompt: {spec['prompt'][:90]}…")
            print(f"    watchdog: {spec.get('watchdog_seconds', DEFAULT_WATCHDOG_S)}s")
            try:
                run = await run_anchor(pw, spec)
            except Exception as exc:
                # Honest failure — record it; do not retry.
                print(f"    DRIVER EXCEPTION: {type(exc).__name__}: {exc}")
                run = {
                    "prompt_id": spec["id"],
                    "first_tool": None,
                    "full_chain": [],
                    "agent_final_text": "",
                    "error_envelopes": [
                        {"_driver_exception": f"{type(exc).__name__}: {exc}"}
                    ],
                    "n_routing_error_envelopes": 1,
                    "n_bookkeeping_error_envelopes": 0,
                    "duration_seconds": 0.0,
                    "screenshot_path": "",
                    "pipeline_terminal": False,
                    "agent_terminal": False,
                    "ws_frame_count": 0,
                    "console_tail": [],
                }
            print(
                f"    first_tool={run['first_tool']!r} "
                f"chain={run['full_chain']} "
                f"routing_err={run['n_routing_error_envelopes']} "
                f"bookkeeping_err={run['n_bookkeeping_error_envelopes']} "
                f"dur={run['duration_seconds']}s "
                f"ws_frames={run['ws_frame_count']}"
            )
            sc = await score_run(run, spec)
            runs.append(run)
            scores.append(sc)

            # Between anchors: wait for the agent session to become idle
            # before launching the next browser context.  This prevents the
            # A5 driver race where a fresh context races to enable chat-input
            # while the previous anchor's backend activity is still winding
            # down.  We open a fresh browser page here solely for the DOM
            # poll; the next anchor opens its own context as usual.
            is_last = i == len(anchors_to_run) - 1
            if not is_last:
                print(
                    f"    [inter-anchor] waiting for agent idle "
                    f"(timeout {INTER_ANCHOR_IDLE_TIMEOUT_S}s)…"
                )
                idle_browser = await pw.chromium.launch(headless=True)
                idle_ctx = await idle_browser.new_context()
                idle_page = await idle_ctx.new_page()
                try:
                    await idle_page.goto(
                        BASE_URL, wait_until="domcontentloaded", timeout=30_000
                    )
                    await idle_page.evaluate(
                        f"() => localStorage.setItem('{ANON_KEY}', 'true')"
                    )
                    await idle_page.goto(
                        BASE_URL, wait_until="load", timeout=30_000
                    )
                    got_idle = await wait_for_agent_idle(idle_page)
                    if got_idle:
                        print("    [inter-anchor] idle confirmed.")
                    else:
                        print(
                            "    [inter-anchor] WARNING: idle timeout; "
                            "proceeding to next anchor anyway."
                        )
                except Exception as idle_exc:
                    print(f"    [inter-anchor] idle poll error (ignored): {idle_exc}")
                finally:
                    await idle_ctx.close()
                    await idle_browser.close()

    agg = aggregate(scores)

    metrics = {
        "harness_version": HARNESS_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "anchors_run": [s["id"] for s in anchors_to_run],
        "scores": scores,
        "aggregate": agg,
        "agent_version": _git_head(),
        "pipeline_emitter_observations": [
            {
                "prompt_id": r["prompt_id"],
                "full_chain": r["full_chain"],
                "ws_frame_count": r["ws_frame_count"],
                "pipeline_terminal": r["pipeline_terminal"],
                "agent_terminal": r["agent_terminal"],
                "n_error_envelopes": len(r["error_envelopes"]),
                "n_routing_error_envelopes": r["n_routing_error_envelopes"],
                "n_bookkeeping_error_envelopes": r["n_bookkeeping_error_envelopes"],
                "agent_final_text_head": (r["agent_final_text"] or "")[:200],
            }
            for r in runs
        ],
    }
    metrics_path = OUT_DIR / "baseline_metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2, default=str))

    # ASCII summary table.
    print("\n=== BASELINE SUMMARY ===")
    print(f"{'anchor':24} {'dimension':28} {'first':6} {'chain':6} {'r_err':6} {'dur':7}")
    print("-" * 86)
    for sc in scores:
        print(
            f"{sc['prompt_id']:24} "
            f"{sc['routing_dimension']:28} "
            f"{('OK' if sc['first_tool_correct'] else 'X'):6} "
            f"{('OK' if sc['full_sequence_correct'] else 'X'):6} "
            f"{sc['n_routing_error_envelopes']:6} "
            f"{sc['duration_seconds']:>6.1f}s"
        )
    print("-" * 86)
    print(
        f"first_tool_correctness_pct = {agg['first_tool_correctness_pct']}%   "
        f"full_sequence_correctness_pct = {agg['full_sequence_correctness_pct']}%"
    )
    print("per_dimension_breakdown:")
    for dim, bucket in agg["per_dimension_breakdown"].items():
        print(
            f"  {dim:28} "
            f"first={bucket['first_tool_correct']}/{bucket['total']}  "
            f"chain={bucket['full_sequence_correct']}/{bucket['total']}"
        )
    print(f"\nMetrics written: {metrics_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
