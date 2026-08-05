"""Shared composer for the CITED-PUBLISHED-DECK SWMM templates (ADR 0128).

The three published-deck templates (``swmm_lid_raingarden_wq``,
``swmm_wwtp_detention_ponds``, ``swmm_pump_pid_rtc``) are THIN composers binding a
cited deck id; ALL the orchestration lives here so the machinery is shared and the
templates are the surface. One entry point, ``model_published_deck``, runs the
chain: fetch (runtime, pinned public URL) -> optional deterministic override ->
solve (headless ``swmm5_run`` + continuity gate) -> build the forcing-appropriate
CHARTS -> return a typed ``SWMMDeckRunResult``.

Demonstration-honesty (loud): the deck is the cited example's network, NOT a user
AOI; its coordinates are schematic, so there is NO georeferenced map layer - the
charts + typed scalars ARE the product. Every number comes from a real parsed
solver output (invariant 1).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.swmm_contracts import SWMMDeckRunResult

from trid3nt_server.agent.mesh.swmm_deck_runner import (
    PUBLISHED_DECKS,
    PublishedDeck,
    SWMMDeckError,
    apply_rain_scale,
    fetch_deck_text,
    link_flow_series,
    list_object_names,
    node_depth_series,
    solve_deck_text,
    subcatchment_runoff_series,
)
from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)

logger = logging.getLogger("trid3nt_server.agent.workflows.swmm.deck_runner")

__all__ = ["model_published_deck"]

_HDR_RE = re.compile(r"^\s*\[([A-Z_0-9]+)\]\s*$")


# --------------------------------------------------------------------------- #
# Small deck parsers (schematic-deck aware; all deterministic, no LLM).
# --------------------------------------------------------------------------- #
def _flow_units(inp_text: str) -> str:
    for ln in inp_text.splitlines():
        s = ln.strip()
        if s.upper().startswith("FLOW_UNITS"):
            parts = s.split()
            if len(parts) >= 2:
                return parts[1].upper()
    return ""


def _lid_subcatchments(inp_text: str) -> set[str]:
    """Subcatchment names that carry a LID control (from [LID_USAGE])."""
    with_lid: set[str] = set()
    cur: str | None = None
    for ln in inp_text.splitlines():
        m = _HDR_RE.match(ln)
        if m:
            cur = m.group(1)
            continue
        s = ln.strip()
        if cur == "LID_USAGE" and s and not s.startswith(";"):
            with_lid.add(s.split()[0])
    return with_lid


def _pid_target(inp_text: str) -> tuple[str | None, float | None, str | None]:
    """Parse (node, target_depth, pump) from a PID [CONTROLS] rule.

    Reads ``IF NODE <id> DEPTH <op> <target>`` + ``THEN PUMP <id> SETTING = PID``.
    Returns (None, None, None) when no PID rule is present.
    """
    node = target = pump = None
    cur: str | None = None
    for ln in inp_text.splitlines():
        m = _HDR_RE.match(ln)
        if m:
            cur = m.group(1)
            continue
        if cur != "CONTROLS":
            continue
        s = ln.strip()
        mn = re.search(r"NODE\s+(\S+)\s+DEPTH\s*[<>=]+\s*([-\d.]+)", s, re.I)
        if mn:
            node = mn.group(1)
            try:
                target = float(mn.group(2))
            except ValueError:
                target = None
        mp = re.search(r"PUMP\s+(\S+)\s+SETTING\s*=\s*PID", s, re.I)
        if mp:
            pump = mp.group(1)
    return node, target, pump


# --------------------------------------------------------------------------- #
# Chart builder (Vega-Lite multi-series line; honesty floor: None on < 2 pts).
# --------------------------------------------------------------------------- #
def _line_chart(
    *,
    title: str,
    caption: str,
    series: dict[str, list[tuple[float, float]]],
    x_title: str,
    y_title: str,
    ref_line: tuple[float, str] | None = None,
) -> dict[str, Any] | None:
    values: list[dict[str, Any]] = []
    for name, pts in series.items():
        for x, y in pts:
            values.append({"minute": round(x, 3), "value": round(y, 5), "series": name})
    if len(values) < 2 or not series:
        return None
    layers: list[dict[str, Any]] = [
        {
            "mark": {"type": "line", "point": False},
            "encoding": {
                "x": {"field": "minute", "type": "quantitative", "title": x_title},
                "y": {"field": "value", "type": "quantitative", "title": y_title},
                "color": {"field": "series", "type": "nominal", "title": ""},
            },
        }
    ]
    if ref_line is not None:
        rval, rlabel = ref_line
        layers.append({
            "data": {"values": [{"ref": rval, "label": rlabel}]},
            "mark": {"type": "rule", "strokeDash": [4, 4], "color": "#888"},
            "encoding": {"y": {"field": "ref", "type": "quantitative"}},
        })
    spec = {
        "data": {"values": values},
        "layer": layers,
        "title": title,
    }
    return build_chart_payload(vega_lite_spec=spec, title=title, caption=caption)


# --------------------------------------------------------------------------- #
# The shared composer.
# --------------------------------------------------------------------------- #
async def model_published_deck(
    *,
    deck_id: str,
    rain_scale: float = 1.0,
    input_mode: str | None = None,
) -> SWMMDeckRunResult:
    """Run one cited published deck: fetch -> override -> solve -> chart -> result.

    Args:
        deck_id: key into ``PUBLISHED_DECKS``.
        rain_scale: rainfall multiplier (only honored for a rain-forced deck;
            ignored with a labeled note otherwise).
        input_mode: ADR 0107 lever (reserved; the deck is a fixed published
            example, so there is no site input to gate - the labeled demonstration
            note is always surfaced).

    Raises ``SWMMDeckError`` (typed) on any fetch / parse / solve / mass-balance
    failure - the caller renders the honest error frame.
    """
    deck = PUBLISHED_DECKS.get(deck_id)
    if deck is None:
        raise SWMMDeckError(
            "SWMM_DECK_UNAVAILABLE", message=f"unknown cited deck id: {deck_id}"
        )

    emitter = current_emitter()
    begin_substeps(emitter, 4)

    # --- Step 1: fetch the cited deck at runtime from the pinned public URL ---
    async with substep(emitter, "fetch_published_deck"):
        inp_text = await asyncio.to_thread(fetch_deck_text, deck)

    # --- Step 2: optional deterministic override (rain scaling) ---
    applied_scale = 1.0
    override_labels: list[str] = []
    if deck.rain_scalable and abs(rain_scale - 1.0) > 1e-9:
        inp_text, lbl = apply_rain_scale(inp_text, float(rain_scale))
        applied_scale = float(rain_scale)
        override_labels.append(lbl)
    elif not deck.rain_scalable and abs(rain_scale - 1.0) > 1e-9:
        override_labels.append(
            f"rain_scale={rain_scale:g} IGNORED - this deck is {deck.forcing}-forced, "
            f"not rainfall-forced"
        )

    flow_units = _flow_units(inp_text)

    # --- Step 3: solve headless + continuity gate ---
    async with substep(emitter, "solve_deck"):
        res = await asyncio.to_thread(
            solve_deck_text,
            inp_text,
            mass_balance_tolerance_pct=deck.mass_balance_tol_pct,
            stem=deck.deck_id,
        )

    # --- Step 4: build the forcing-appropriate charts ---
    async with substep(emitter, "chart_and_publish"):
        headline, chart_titles = await asyncio.to_thread(
            _build_charts_sync, deck, inp_text, res, flow_units
        )

    # emit the charts (best-effort; each is None-safe)
    for payload in headline.pop("_charts", []) or []:
        try:
            await emit_chart_payloads(payload)
        except Exception as exc:  # noqa: BLE001 - never break the solve
            logger.warning("model_published_deck: chart emit failed (%s)", exc)

    demonstration_note = (
        f"Demonstration run of the CITED published example \"{deck.title}\" "
        f"({deck.author}, {deck.source}). This is the EXAMPLE's schematic network, "
        f"NOT a user area of interest - coordinates are local model units, so there "
        f"is no georeferenced map; the charts + numbers are the product."
    )

    provenance = [
        SyntheticInput(
            param="swmm_deck", value=deck.title, basis="fetched",
            real_source_if_any=f"{deck.source} ({deck.source_url})",
            note=f"published example deck by {deck.author}; run VERBATIM (schematic, "
                 f"not georeferenced)",
        ),
    ]
    if override_labels:
        provenance.append(SyntheticInput(
            param="rain_scale",
            value=(applied_scale if deck.rain_scalable else None),
            basis="default_demo", note="; ".join(override_labels),
        ))

    result = SWMMDeckRunResult(
        deck_id=deck.deck_id,
        deck_title=deck.title,
        deck_author=deck.author,
        deck_source=deck.source,
        deck_url=deck.source_url,
        capabilities=list(deck.capabilities),
        forcing=deck.forcing,
        flow_units=flow_units,
        continuity_error_pct=res.continuity_error_pct,
        n_nodes=res.n_nodes,
        n_links=res.n_links,
        n_subcatchments=res.n_subcatchments,
        peak_outfall_flow=res.peak_outfall_flow_cms,
        max_node_depth=res.max_node_depth_m,
        n_flooded_nodes=res.n_flooded_nodes,
        n_surcharged_conduits=res.n_surcharged_conduits,
        headline=headline,
        chart_titles=chart_titles,
        demonstration_note=demonstration_note,
        schematic_only=True,
        synthetic_inputs=provenance,
        rain_scale=applied_scale,
    )
    logger.info(
        "model_published_deck complete deck=%s continuity=%+.3f%% nodes=%d links=%d "
        "charts=%s headline_keys=%s",
        deck.deck_id, res.continuity_error_pct, res.n_nodes, res.n_links,
        chart_titles, sorted(headline.keys()),
    )
    return result


def _build_charts_sync(
    deck: PublishedDeck, inp_text: str, res: Any, flow_units: str
) -> tuple[dict[str, Any], list[str]]:
    """Build deck-specific charts + headline scalars (sync; off the event loop).

    Returns (headline, chart_titles). ``headline["_charts"]`` carries the chart
    payloads to emit (popped by the caller). Each branch is best-effort and skips a
    chart when its series is thin (honesty floor: no chart rather than an empty one).
    """
    headline: dict[str, Any] = {}
    charts: list[dict[str, Any]] = []
    titles: list[str] = []
    depth_unit = "ft" if flow_units == "CFS" else "m"
    flow_lbl = flow_units or "flow"

    if deck.forcing == "rainfall":
        # LID with/without: overlay the runoff hydrographs; report the reduction.
        with_lid = _lid_subcatchments(inp_text)
        subs = list_object_names(inp_text, "SUBCATCHMENTS")
        series: dict[str, list[tuple[float, float]]] = {}
        peaks: dict[str, float] = {}
        for sub in subs:
            ser = subcatchment_runoff_series(res.out_path, sub)
            if not ser:
                continue
            tag = "with rain garden" if sub in with_lid else "without rain garden"
            label = f"{sub} ({tag})"
            series[label] = ser
            peaks[label] = max((v for _, v in ser), default=0.0)
        chart = _line_chart(
            title="Runoff hydrograph - with vs without rain garden (LID)",
            caption=(
                f"Cited: {deck.title} ({deck.author}). Runoff in {flow_lbl}. The "
                f"rain-garden LID subcatchment shows lower runoff - the built-in "
                f"expected-outcome check."
            ),
            series=series, x_title="minutes from start", y_title=f"runoff ({flow_lbl})",
        )
        if chart is not None:
            charts.append(chart)
            titles.append("Runoff hydrograph - with vs without rain garden (LID)")
        headline["subcatchment_peak_runoff"] = {k: round(v, 4) for k, v in peaks.items()}
        # expected-outcome flag: any with-LID peak below any without-LID peak
        with_peaks = [v for k, v in peaks.items() if "with rain garden" in k]
        wo_peaks = [v for k, v in peaks.items() if "without rain garden" in k]
        if with_peaks and wo_peaks:
            headline["lid_reduces_runoff"] = max(with_peaks) < max(wo_peaks)

    elif deck.forcing == "initial_storage":
        # Detention ponds: stage recession for each STORAGE node.
        ponds = list_object_names(inp_text, "STORAGE")
        series = {}
        stages: dict[str, dict[str, float]] = {}
        for pond in ponds:
            ser = node_depth_series(res.out_path, pond)
            if not ser:
                continue
            series[pond] = ser
            stages[pond] = {
                "start": round(ser[0][1], 3),
                "end": round(ser[-1][1], 3),
                "peak": round(max((v for _, v in ser), default=0.0), 3),
            }
        chart = _line_chart(
            title="Detention pond stage recession",
            caption=(
                f"Cited: {deck.title} ({deck.author}). Pond water-surface depth "
                f"({depth_unit}) draining through the outlet weirs (storage routing; "
                f"no external storm - initial-storage drain-down)."
            ),
            series=series, x_title="minutes from start", y_title=f"pond stage ({depth_unit})",
        )
        if chart is not None:
            charts.append(chart)
            titles.append("Detention pond stage recession")
        headline["pond_stage"] = stages

    elif deck.forcing == "dry_weather_flow":
        # PID pump: wet-well depth tracking the target + the pump flow.
        node, target, pump = _pid_target(inp_text)
        series = {}
        if node:
            ser = node_depth_series(res.out_path, node)
            if ser:
                series[f"wet-well {node} depth"] = ser
                headline["wet_well_node"] = node
                headline["wet_well_depth"] = {
                    "min": round(min(v for _, v in ser), 3),
                    "max": round(max(v for _, v in ser), 3),
                }
        if pump:
            pf = link_flow_series(res.out_path, pump)
            if pf:
                headline["pump"] = pump
                headline["pump_peak_flow"] = round(max((v for _, v in pf), default=0.0), 3)
        if target is not None:
            headline["pid_target_depth"] = target
        chart = _line_chart(
            title="PID control - wet-well depth vs target",
            caption=(
                f"Cited: {deck.title} ({deck.author}). Wet-well depth ({depth_unit}); "
                f"the PID rule adjusts the pump to hold the target "
                f"{f'{target:g} {depth_unit}' if target is not None else 'setpoint'}."
            ),
            series=series, x_title="minutes from start", y_title=f"wet-well depth ({depth_unit})",
            ref_line=(target, f"PID target {target:g} {depth_unit}") if target is not None else None,
        )
        if chart is not None:
            charts.append(chart)
            titles.append("PID control - wet-well depth vs target")

    headline["_charts"] = charts
    return headline, titles
