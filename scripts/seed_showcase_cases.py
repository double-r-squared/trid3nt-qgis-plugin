#!/usr/bin/env python3
"""seed_showcase_cases.py -- seed inspectable showcase Cases through the PRODUCT path.

The engine-template proofs have always lived in ``docs/proof/`` renders and ADR
smoke logs -- they never landed in a QGIS profile a human can open. This driver
closes that gap. It is a HEADLESS WS client that drives the live daemon exactly
the way the QGIS plugin does:

  1. auth-token (anonymous) -> auth-ack
  2. session-resume -> session-state
  3. case-command create {title="showcase: <template>"} -> case-open (new Case)
  4. dev-tool-invoke {name, args, case_id, raw_text="!run <tool>(...)"} -- the
     ADR 0114 ``!run`` direct-invocation path: the SAME registry closure, gates,
     layer materialization + Case persistence a model-issued call rides.
  5. collect the turn (auto-confirm the tool-payload-warning / solver-confirm /
     granularity gate; auto-approve a confirmation-request) until turn-complete,
     recording the tool-io status + the emitted ``session-state`` loaded_layers.

After every entry is seeded a SECOND connection reopens each Case (``case-command
select``) and confirms the persisted ``loaded_layers`` survive the reconnect --
the per-Case layer-durability norm, proven end-to-end.

Nothing here fabricates physics: every arg set is a PROVEN demo mined from the
ADR 0141-0174 smoke reports and the ``scripts/run_*_direct.py`` drivers (the
source of each is recorded in the entry ``note``). The reconstructed ``!run``
line each Case records is a line a human can paste into the composer verbatim.

OFFLINE proof (no daemon): ``--dry-run`` prints the planned invocation table and
round-trips every reconstructed ``!run`` line back through the PRODUCT parser
(``trid3nt.net.run_invocation.parse_run_invocation``), asserting the line parses
to the SAME (name, args) -- a hermetic contract check that reuses product code.

This driver NEVER deletes or mutates an existing Case; it only CREATES new
``showcase:``-prefixed Cases. It NEVER touches a template file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
# Product parser lives in the plugin tree; reuse it for the offline round-trip.
sys.path.insert(0, str(REPO_ROOT / "qgis-plugin"))

WS_URL = "ws://127.0.0.1:8765/ws"
LOG_FILE = Path("/tmp/seed_showcase_cases.log")

# --------------------------------------------------------------------------- #
# Showcase entries -- ordered CHEAPEST-first so a slow tail never starves the
# fast, high-value Cases (quality over completeness). Every ``args`` set is a
# proven demo; ``note`` records its provenance; ``timeout_s`` time-boxes the
# solve.
# --------------------------------------------------------------------------- #
_BOULDER = [-105.37, 39.998, -105.33, 40.032]          # ADR 0141 landlab AOI
_WEST_BIJOU = [-104.33, 39.31, -104.28, 39.35]         # ADR 0184 chi-map escarpment
_SF_BAY = [-122.30, 37.70, -122.10, 37.90]             # ADR 0149 PSHA AOI
_EAST_BAY = [-122.30, 37.75, -122.10, 37.95]           # ADR 0164 Hayward AOI
_APALACHEE = [-85.55, 29.70, -85.40, 29.85]            # ADR 0147 SWAN shelf
_CHATTANOOGA = [-85.32, 35.03, -85.28, 35.07]          # ADR 0152 SFINCS pluvial
_MEXBEACH = [-85.5522, 29.6983, -85.3976, 29.8517]     # ADR 0176 SFINCS quadtree (Michael)
_CRESCENT = [-124.24, 41.73, -124.16, 41.78]           # ADR 0148 GeoClaw tsunami
_GALVESTON = [-95.2, 29.0, -94.2, 29.8]                # ADR 0168 surge shelf
_PLATTE = [40.905, -98.42]                              # ADR 0165/0166 well (lat, lon)


@dataclass
class Showcase:
    tool: str
    args: dict
    note: str
    timeout_s: float = 300.0

    @property
    def case_title(self) -> str:
        # Humanize the tool name into a Case label the left rail reads well.
        return "showcase: " + self.tool.replace("_", " ")


SHOWCASE: list[Showcase] = [
    # -- fast closed-form / fixture validators (seconds) ---------------------
    Showcase("pelicun_closed_form_validation", {"check": "damage_state_probability"},
             "ADR 0146 pelicun validation wave (analytic DS-probability identity)", 180),
    Showcase("pelicun_hazus_seismic_dl_run", {},
             "ADR 0160 HAZUS seismic DL harness defaults (C1 Low-Rise Pre-Code)", 240),
    Showcase("modflow_package_validation", {"case": "maw_crossaquifer"},
             "ADR 0153 MODFLOW package validation, MAW cross-aquifer fixture", 300),
    Showcase("modflow_package_validation", {"case": "sfr_stream_depletion"},
             "ADR 0167 MODFLOW SFR stream-depletion fixture", 300),
    Showcase("swmm_subcatchment_runoff_comparison", {"compare": "infiltration_method"},
             "ADR 0151 SWMM mechanism comparison (infiltration method A/B)", 240),
    Showcase("swmm_wetwell_pump_control_comparison", {},
             "ADR 0151 SWMM wet-well pump-control comparison defaults", 240),
    # -- landlab diagnostics on the Boulder AOI (DEM fetch + solve) ----------
    Showcase("landlab_flow_accumulation", {"bbox": _BOULDER},
             "ADR 0141 landlab diagnostic wave, Boulder CO AOI", 360),
    Showcase("landlab_hand_wetness", {"bbox": _BOULDER},
             "ADR 0141 landlab HAND wetness, Boulder CO AOI", 360),
    Showcase("landlab_lake_mapping", {"bbox": _BOULDER},
             "ADR 0141/0145 landlab lake mapping, Boulder CO AOI", 360),
    Showcase("landlab_overland_flow_timeseries", {"bbox": _BOULDER},
             "ADR 0141 landlab overland-flow timeseries, Boulder CO AOI", 420),
    Showcase("landlab_channel_incision_steady_state", {"bbox": _BOULDER},
             "ADR 0184 landlab detachment-limited incision to steady state + "
             "slope-area V&V, Boulder CO foothills (fitted concavity ~0.485 vs "
             "analytical 0.5, K recovered within ~25%)", 480),
    Showcase("landlab_channel_steepness_chi_map", {"bbox": _WEST_BIJOU},
             "ADR 0184 landlab chi / channel-steepness (ksn) knickpoint diagnostic, "
             "West Bijou Creek escarpment CO", 420),
    Showcase("landlab_storm_sequence_generator", {"bbox": _BOULDER},
             "ADR 0184 landlab stochastic storm-sequence generator "
             "(PrecipitationDistribution, in-process), Boulder CO AOI", 180),
    # -- MODFLOW georeferenced wellhead/capture-zone -------------------------
    Showcase("modflow_wellhead_protection",
             {"aoi_latlon": _PLATTE, "well_location_latlon": _PLATTE,
              "travel_time_years": [5.0, 10.0, 25.0], "n_particles": 48},
             "ADR 0165/0166 Platte valley nr Grand Island NE, well 40.905/-98.42", 480),
    # -- OpenQuake seismic ---------------------------------------------------
    Showcase("openquake_psha", {"bbox": _SF_BAY, "logic_tree": "gr_uncertainty"},
             "ADR 0149 PSHA logic-tree GR uncertainty, SF Bay AOI", 480),
    Showcase("openquake_scenario_gmf", {"bbox": _EAST_BAY, "magnitude": 6.9},
             "ADR 0164 scenario GMF, East Bay M6.9 (auto Hayward-fault trace)", 480),
    Showcase("openquake_secondary_perils", {"bbox": _EAST_BAY, "magnitude": 6.9},
             "ADR 0164 secondary perils (liquefaction/landslide), East Bay M6.9", 480),
    Showcase("openquake_disaggregation", {"bbox": _SF_BAY},
             "ADR 0182 hazard disaggregation, SF Bay AOI (dominant M-R-eps at 10%/50yr; "
             "local oq subprocess, ~30s)", 300),
    Showcase("openquake_event_based",
             {"bbox": _SF_BAY, "ses_per_logic_tree_path": 300},
             "ADR 0182 event-based/stochastic PSHA, SF Bay AOI (synthetic catalogue + "
             "classical convergence check; local oq subprocess)", 480),
    Showcase("openquake_psha", {"bbox": _SF_BAY, "vs30_compare": 260.0},
             "ADR 0182 Vs30 site-response A/B fold, SF Bay AOI (rock 760 vs soft 260 m/s "
             "hazard-curve overlay on the classical map path)", 480),
    # -- ELMFIRE wildfire sensitivity ----------------------------------------
    Showcase("elmfire_live_fuel_moisture_sensitivity", {},
             "ADR 0142 ELMFIRE live-fuel-moisture sensitivity defaults (GR2)", 420),
    Showcase("elmfire_transient_wind_schedule_spread", {},
             "ADR 0161 ELMFIRE transient wind-schedule (mid-run direction shift)", 420),
    # -- SWAN wave physics ---------------------------------------------------
    Showcase("swan_physics_sensitivity_sweep", {"bbox": _APALACHEE},
             "ADR 0147 SWAN friction sweep on the Apalachee Bay shelf", 480),
    # -- SFINCS pluvial ------------------------------------------------------
    Showcase("sfincs_flood",
             {"bbox": _CHATTANOOGA, "return_period_yr": 100, "duration_hr": 24,
              "compute_class": "small"},
             "ADR 0152 SFINCS pluvial flood, Chattanooga TN AOI", 600),
    Showcase("sfincs_flood",
             {"bbox": _MEXBEACH, "quadtree": True, "coastal": True,
              "return_period_yr": 100, "duration_hr": 12, "compute_class": "small",
              "quadtree_base_resolution_m": 400.0, "quadtree_coast_refine_level": 3,
              "quadtree_max_refine_level": 3},
             "ADR 0178 SFINCS COAST-FOLLOWING quadtree flood, Mexico Beach FL "
             "(Hurricane Michael lineage). Full PRODUCT path: sfincs_flood(quadtree="
             "True) stages the topobathy DEM + build_spec and dispatches the worker "
             "build+solve (solver=sfincs-quadtree); cht_sfincs authors the grid with "
             "the fine 50 m band hugging the z=0 shoreline (2:1-balanced 400->50 m, "
             "~12k cells), the native sfincs_map.nc mesh carries EPSG:32616.", 900),
    # -- GeoClaw coastal -----------------------------------------------------
    Showcase("geoclaw_inundation",
             {"bbox": _CRESCENT, "scenario": "tsunami", "sim_duration_s": 1800,
              "amr_levels": 2, "output_frames": 6, "fgout_frames": 12},
             "ADR 0187 GeoClaw tsunami inundation with fgout SMOOTH animation, "
             "Crescent City CA: fgout_frames=12 -> 12 evenly-spaced uniform-grid "
             "frames become the scrubber animation (fort.q peak retained)", 600),
    Showcase("geoclaw_thacker_validation",
             {"bowl_a_m": 1.0, "bowl_h0_m": 0.1, "bowl_eta_amp": 0.5,
              "n_periods": 2.5, "amr_levels": 3, "base_cells": 60},
             "ADR 0187 GeoClaw Thacker paraboloid-basin V&V: frictionless closed-wall "
             "bowl vs the 1981 closed form (period ~1.9%, amplitude ~0.1%, mass drift "
             "~5%). Synthetic non-geographic solver verification (charts/scalars).", 300),
    Showcase("geoclaw_storm_surge",
             {"bbox": _GALVESTON, "sim_duration_s": 54000, "output_frames": 12,
              "amr_levels": 2},
             "ADR 0168 GeoClaw storm surge, synthetic demo track on Galveston shelf", 600),
    # -- TELEMAC water quality / transport -----------------------------------
    Showcase("telemac_do_sag", {"location": "Sacramento River near Colusa, California"},
             "ADR 0169 TELEMAC-WAQTEL DO-sag, real NHDPlus reach nr Colusa CA", 600),
    Showcase("telemac_river_dye",
             {"location": "Eel River near Scotia, California",
              "wind_speed_mps": 18.0, "wind_direction_deg": 270.0},
             "ADR 0154 TELEMAC river dye + wind forcing, Eel River nr Scotia CA", 600),
    # -- HEC-RAS (bundled Muncie deck; cheap 1D/2D) --------------------------
    Showcase("hecras_flood_2d",
             {"bbox": [-98.115, 29.975, -98.083, 30.000], "target_peak_cfs": 15000,
              "resolution_m": 30, "equation_set": "full_swe", "computation_interval": "1MIN"},
             "ADR 0188 HEC-RAS 2D fresh-AOI flood on the Blanco River canyon nr "
             "Wimberley TX (329 ft relief), exercising the equation_set (full SWE-ELM) "
             "+ computation_interval (1MIN stability step) knobs. DW vs SWE agree on "
             "the peak footprint, separating only at momentum-dominated channel cells; "
             "the coarse-step overshoot converges as the step tightens.", 900),
    Showcase("hecras_riverine_flood", {},
             "ADR 0170/0172 HEC-RAS riverine flood, shipped Muncie deck", 480),
    Showcase("hecras_levee_breach", {"breach_enabled": True},
             "ADR 0171/0172 HEC-RAS levee breach, shipped Muncie deck", 480),
]


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(str(LOG_FILE)), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("seed_showcase")


# --------------------------------------------------------------------------- #
# !run line reconstruction (pythonic kwargs form the plugin parser accepts)
# --------------------------------------------------------------------------- #
def _py_literal(v: Any) -> str:
    """Render ``v`` as a Python literal the composer parser can ``literal_eval``."""
    if isinstance(v, bool):
        return "True" if v else "False"
    if v is None:
        return "None"
    if isinstance(v, str):
        return repr(v)  # single-quoted; ast.literal_eval-safe
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_py_literal(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_py_literal(k)}: {_py_literal(val)}" for k, val in v.items()) + "}"
    raise TypeError(f"unsupported literal: {v!r}")


def run_line(tool: str, args: dict) -> str:
    if not args:
        return f"!run {tool}()"
    inner = ", ".join(f"{k}={_py_literal(v)}" for k, v in args.items())
    return f"!run {tool}({inner})"


# --------------------------------------------------------------------------- #
# WS envelope helper
# --------------------------------------------------------------------------- #
from trid3nt_contracts import new_ulid  # noqa: E402


def mk(type_: str, session_id: str, payload: dict, case_id: str | None = None) -> str:
    return json.dumps({
        "type": type_,
        "id": new_ulid(),
        "ts": "2026-08-07T00:00:00Z",
        "session_id": session_id,
        "case_id": case_id,
        "payload": payload,
    })


# --------------------------------------------------------------------------- #
# Per-entry result record
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    tool: str
    title: str
    args: dict
    note: str
    run_line: str
    case_id: str | None = None
    status: str = "not_run"          # ok | error | blocked | timeout | no_result
    detail: str = ""
    layers: list[dict] = field(default_factory=list)
    tool_status: str | None = None   # status parsed from the function_response
    charts: int = 0                  # chart-dock emissions (validation/report output)
    persisted_layers: int | None = None  # from the reconnect verify

    def as_row(self) -> dict:
        return {
            "case": self.title,
            "case_id": self.case_id,
            "tool": self.tool,
            "args": self.args,
            "status": self.status,
            "detail": self.detail,
            "layers": [{"name": l.get("name"), "type": l.get("layer_type"),
                        "uri": l.get("uri")} for l in self.layers],
            "charts": self.charts,
            "persisted_layers": self.persisted_layers,
            "run_line": self.run_line,
        }


# --------------------------------------------------------------------------- #
# WS client core
# --------------------------------------------------------------------------- #
async def _handshake(ws, session_id: str) -> None:
    await ws.send(mk("auth-token", session_id, {"token": "", "anonymous_user_id": None}))
    ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
    assert ack["type"] == "auth-ack", f"expected auth-ack, got {ack['type']}"
    await ws.send(mk("session-resume", session_id, {"case_id": None}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
        if msg["type"] == "session-state":
            return


async def _create_case(ws, session_id: str, title: str) -> str:
    await ws.send(mk("case-command", session_id,
                     {"command": "create", "args": {"title": title}}))
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
        if msg["type"] == "case-open":
            ss = msg["payload"].get("session_state")
            if ss:
                return ss["case"]["case_id"]


async def _auto_confirm_warning(ws, session_id: str, msg: dict) -> None:
    wid = msg["payload"].get("warning_id")
    log.info("    auto-confirm tool-payload-warning warning_id=%s -> proceed", wid)
    await ws.send(mk("tool-payload-confirmation", session_id,
                     {"warning_id": wid, "decision": "proceed", "revised_args": None}))


async def _auto_approve_request(ws, session_id: str, msg: dict) -> None:
    rid = msg["payload"].get("request_id")
    log.info("    auto-approve confirmation-request request_id=%s -> approved", rid)
    await ws.send(mk("confirm-response", session_id,
                     {"request_id": rid, "approved": True}))


_BLOCKING = {
    "spatial-input-request", "disambiguation-request",
    "clarification-request", "recovery-choice",
}


async def _seed_one(ws, session_id: str, sc: Showcase) -> Result:
    res = Result(tool=sc.tool, title=sc.case_title, args=sc.args, note=sc.note,
                 run_line=run_line(sc.tool, sc.args))
    res.case_id = await _create_case(ws, session_id, sc.case_title)
    log.info("[%s] case_id=%s  %s", sc.tool, res.case_id, res.run_line)

    await ws.send(mk("dev-tool-invoke", session_id,
                     {"name": sc.tool, "args": sc.args,
                      "case_id": res.case_id, "raw_text": res.run_line},
                     case_id=res.case_id))

    deadline = time.monotonic() + sc.timeout_s
    activity = False
    tool_io_seen = False
    tool_io_error = False
    charts = 0
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
            await _auto_confirm_warning(ws, session_id, msg)
        elif mtype == "confirmation-request":
            activity = True
            await _auto_approve_request(ws, session_id, msg)
        elif mtype in _BLOCKING:
            res.status = "blocked"
            res.detail = f"gate needs interactive input ({mtype}); skipped headlessly"
            log.warning("    BLOCKED by %s", mtype)
            return res
        elif mtype in ("pipeline-state", "tool-call-start", "tool-call-progress"):
            activity = True
        elif mtype == "tool-io":
            activity = True
            tool_io_seen = True
            res.tool_status = _parse_tool_status(msg["payload"])
            if msg["payload"].get("is_error"):
                tool_io_error = True
                res.detail = _first_line(msg["payload"].get("function_response", ""))
        elif mtype in ("chart-emission", "chart"):
            activity = True
            charts += 1
        elif mtype in ("tool-call-failed",):
            activity = True
            res.detail = _first_line(json.dumps(msg["payload"]))
        elif mtype == "session-state":
            ll = msg["payload"].get("loaded_layers") or []
            if ll:
                latest_layers = ll
        elif mtype == "error":
            res.status = "error"
            res.detail = f"{msg['payload'].get('error_code')}: {msg['payload'].get('message')}"
            log.error("    ERROR %s", res.detail)
            return res
        elif mtype == "turn-complete":
            if activity:
                break
    else:
        res.status = "timeout"
        res.detail = f"no turn-complete within {sc.timeout_s:.0f}s"
        res.layers = latest_layers
        log.warning("    TIMEOUT")
        return res

    res.layers = latest_layers
    res.charts = charts
    # Honesty floor: is_error on the tool-io is authoritative. Success requires a
    # non-error dispatch that actually EMITTED something inspectable -- a map
    # layer (LayerPanel) or a chart-dock chart (validation/report output) or an
    # explicit status=ok in the function_response.
    if tool_io_error or res.tool_status == "error":
        res.status = "error"
        res.detail = res.detail or "tool-io reported is_error/status=error"
    elif latest_layers:
        res.status = "ok"
        res.detail = f"{len(latest_layers)} layer(s)" + (f" + {charts} chart(s)" if charts else "")
    elif charts or res.tool_status == "ok":
        res.status = "ok"
        res.detail = f"{charts} chart(s), no map layer (validation/report output)"
    elif tool_io_seen:
        res.status = "no_result"
        res.detail = res.detail or "dispatch not error but emitted no layer/chart"
    else:
        res.status = "no_result"
        res.detail = res.detail or "turn completed with no tool dispatch"
    log.info("    -> %s :: %s", res.status.upper(), res.detail)
    return res


def _parse_tool_status(payload: dict) -> str | None:
    raw = payload.get("function_response") or ""
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return "error" if payload.get("is_error") else None
    if isinstance(obj, dict):
        st = obj.get("status")
        if isinstance(st, str):
            return st
    return "error" if payload.get("is_error") else None


def _first_line(s: str, n: int = 240) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[:n]


async def _verify_persistence(session_id: str, results: list[Result]) -> None:
    """Reopen every seeded Case on a FRESH connection and confirm its layers
    survived the reconnect (per-Case layer-durability norm)."""
    import websockets.asyncio.client as wsc
    async with wsc.connect(WS_URL) as ws:
        await _handshake(ws, session_id)
        for res in results:
            if not res.case_id:
                continue
            await ws.send(mk("case-command", session_id,
                             {"command": "select", "case_id": res.case_id},
                             case_id=res.case_id))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                except asyncio.TimeoutError:
                    break
                if msg["type"] == "case-open":
                    ss = msg["payload"].get("session_state")
                    if ss and ss["case"]["case_id"] == res.case_id:
                        res.persisted_layers = len(ss.get("loaded_layers") or [])
                        log.info("    reconnect: case %s -> %d persisted layer(s)",
                                 res.case_id, res.persisted_layers)
                        break


async def run_all(only: str | None) -> list[Result]:
    import websockets.asyncio.client as wsc
    entries = [s for s in SHOWCASE if (only is None or only in s.tool)]
    session_id = new_ulid()
    log.info("=== showcase seeding: %d entries, session=%s ===", len(entries), session_id)
    results: list[Result] = []
    async with wsc.connect(WS_URL, max_size=64 * 1024 * 1024) as ws:
        await _handshake(ws, session_id)
        for sc in entries:
            try:
                results.append(await _seed_one(ws, session_id, sc))
            except Exception as exc:  # noqa: BLE001 -- one bad entry never stops the run
                log.exception("[%s] driver exception", sc.tool)
                r = Result(tool=sc.tool, title=sc.case_title, args=sc.args, note=sc.note,
                           run_line=run_line(sc.tool, sc.args))
                r.status = "error"
                r.detail = f"driver exception: {exc}"
                results.append(r)
    # Second connection: prove durability across a reconnect.
    log.info("=== reconnect: verifying per-Case layer durability ===")
    try:
        await _verify_persistence(session_id, results)
    except Exception:  # noqa: BLE001
        log.exception("persistence verify failed")
    return results


# --------------------------------------------------------------------------- #
# Offline dry-run: plan + product-parser round-trip
# --------------------------------------------------------------------------- #
def dry_run(only: str | None) -> int:
    from trid3nt.net.run_invocation import parse_run_invocation
    entries = [s for s in SHOWCASE if (only is None or only in s.tool)]
    print(f"planned showcase Cases: {len(entries)}\n")
    failures = 0
    for sc in entries:
        line = run_line(sc.tool, sc.args)
        parsed = parse_run_invocation(line)
        ok = (parsed is not None and parsed.name == sc.tool and parsed.args == sc.args)
        mark = "OK " if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{mark}] {sc.case_title}")
        print(f"        tool  = {sc.tool}")
        print(f"        args  = {json.dumps(sc.args)}")
        print(f"        note  = {sc.note}")
        print(f"        !run  = {line}")
        if not ok:
            print(f"        PARSE = {parsed}")
        print()
    print(f"round-trip: {len(entries) - failures}/{len(entries)} !run lines parse "
          f"back to the exact (name, args) via the product parser")
    return 1 if failures else 0


def _print_summary(results: list[Result]) -> None:
    rows = [r.as_row() for r in results]
    print("\n" + "=" * 78)
    print("SHOWCASE SEEDING SUMMARY")
    print("=" * 78)
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    print("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    print()
    for r in results:
        pl = "" if r.persisted_layers is None else f" persisted={r.persisted_layers}"
        print(f"[{r.status.upper():9}] {r.title}  ({len(r.layers)} layers, {r.charts} charts{pl})")
        print(f"            {r.run_line}")
        if r.detail:
            print(f"            {r.detail}")
    out = Path("/tmp/seed_showcase_results.json")
    out.write_text(json.dumps(rows, indent=2))
    print(f"\nfull JSON: {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="offline: print the plan + round-trip !run lines through the product parser")
    ap.add_argument("--only", default=None,
                    help="substring filter on the tool name (e.g. 'landlab')")
    args = ap.parse_args()
    if args.dry_run:
        return dry_run(args.only)
    results = asyncio.run(run_all(args.only))
    _print_summary(results)
    ok = sum(1 for r in results if r.status == "ok")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
