"""Shared machinery for the ELMFIRE one-knob sensitivity-sweep templates.

Each sensitivity template sweeps ONE namelist knob across a small ladder of
values on an otherwise ALL-CONSTANT flat deck (no LANDFIRE/DEM fetch), so the
knob's effect is isolated. Every point in the sweep is one real container solve
via the generic ``run_solver('elmfire')`` seam -- the sweeps are deliberately
SMALL (a handful of points) and run on tiny short-duration domains so the cost
stays bounded.

This module holds what the individual templates share:

  - :func:`solve_constant_case` -- build one constant flat deck (with the case's
    knob overrides), dispatch + wait on the solver, read the time-of-arrival +
    spread-rate rasters, and return the measured scalars (burned area, max
    spread rate, and -- for the shape templates -- the length:width ratio from
    the Richards-ellipse fit). The solver output dir is kept (not cleaned) so
    the caller can postprocess + publish the representative run.
  - :func:`build_sweep_chart_spec` -- the generic response-vs-knob line chart.
  - :func:`publish_primary_from_out_dir` -- postprocess + publish the primary
    time-of-arrival COG of the representative run into a base layer the
    template decorates into an ``ElmfireSensitivityLayerURI``.

Determinism boundary (Invariant 1): every returned scalar is measured off the
solver rasters -- never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trid3nt_contracts.elmfire_contracts import ElmfireRunArgs

from trid3nt_server.agent.workflows.elmfire.fire_spread.fire_spread import (
    FireSpreadComposerError,
    _cleanup_dir,
    _download_elmfire_outputs,
    _publish_primary_layer,
)
from trid3nt_server.agent.workflows.elmfire.postprocess_elmfire import (
    FTMIN_TO_MMIN,
    discover_elmfire_rasters,
    postprocess_elmfire,
    read_fire_raster,
    verify_elliptical_replication,
)
from trid3nt_server.agent.workflows.elmfire.run_elmfire import (
    ELMFIRE_SOLVER_NAME,
    ElmfireWorkflowError,
    build_constant_flat_deck,
    stage_elmfire_manifest,
)
from trid3nt_server.emission.pipeline_emitter import (
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.elmfire.sensitivity._sensitivity_common"
)

__all__ = [
    "SweepCaseResult",
    "solve_constant_case",
    "build_sweep_chart_spec",
    "publish_primary_from_out_dir",
]


@dataclass
class SweepCaseResult:
    """One solved constant-deck sweep point: the measured response scalars."""

    knob_value: float
    run_id: str
    out_dir: str
    out_is_temp: bool
    deck_dir: str
    epsg: int
    cellsize_m: float
    burned_area_km2: float
    fire_arrival_max_hr: float
    max_spread_rate_m_min: float | None = None
    length_to_width_ratio: float | None = None
    err_fraction: float | None = None
    corr_class: str | None = None
    crown_active_area_km2: float | None = None
    crown_any_area_km2: float | None = None
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    extras: dict[str, float] = field(default_factory=dict)


async def _dispatch_and_wait(
    *,
    deck_dir: str,
    deck_manifest: dict[str, Any],
    run_args: ElmfireRunArgs,
    compute_class: str,
    emitter: Any,
    run_id: str | None,
) -> str:
    """Stage + dispatch one built deck via ``run_solver`` and await completion.

    Returns the solve ``run_id``. Raises ``ElmfireWorkflowError`` when the solve
    does not complete (no silent dead-ends).
    """
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    staging = await asyncio.to_thread(
        stage_elmfire_manifest, deck_dir, deck_manifest, run_args, run_id=run_id
    )
    handle = run_solver(
        solver=ELMFIRE_SOLVER_NAME,
        model_setup_uri=staging.manifest_uri,
        compute_class=compute_class,
    )
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter,
        solver=ELMFIRE_SOLVER_NAME,
        handle=handle,
        compute_class=compute_class,
    )
    if emitter is not None and sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=sim_step_id))
    run_result = None
    try:
        async with substep(emitter, "run_solver"):
            run_result = await wait_for_completion(handle)
    except asyncio.CancelledError:
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    finally:
        set_emitter_binding(None)
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)

    if run_result.status != "complete":
        raise ElmfireWorkflowError(
            "ELMFIRE_RUN_FAILED",
            "ELMFIRE sensitivity solve did not complete "
            f"(status={run_result.status}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}",
            details={"run_id": staging.run_id},
        )
    return getattr(run_result, "run_id", None) or staging.run_id


async def solve_constant_case(
    run_args: ElmfireRunArgs,
    *,
    knob_value: float,
    fuel_model: int,
    canopy: dict[str, int] | None = None,
    moisture_override: dict[str, float] | None = None,
    weather_schedule: list[dict[str, float]] | None = None,
    dt_meteorology_s: float = 3600.0,
    target_cfl: float | None = None,
    simulator_extra: dict[str, str] | None = None,
    outputs_extra: dict[str, str] | None = None,
    inputs_extra: dict[str, str] | None = None,
    time_control_extra: dict[str, str] | None = None,
    spotting_extra: dict[str, str] | None = None,
    fuel_break: dict[str, Any] | None = None,
    dt_s: float = 30.0,
    dtdump_s: float = 3600.0,
    compute_class: str = "small",
    emitter: Any = None,
    measure_ltw: bool = False,
    measure_crown: bool = False,
    step_label: str = "build_elmfire_deck",
) -> SweepCaseResult:
    """Solve ONE constant flat-deck sweep point; return the measured scalars.

    Builds the deck (with the case's knob overrides), dispatches the solve, and
    reads the time-of-arrival (+ spread-rate) rasters into the response scalars.
    The output dir is KEPT (``out_dir``) so the caller can postprocess + publish
    the representative run; the caller cleans every case's ``deck_dir`` and any
    temp ``out_dir`` it does not publish.
    """
    import tempfile

    deck_dir = tempfile.mkdtemp(prefix="elmfire-sweep-deck-")
    async with substep(emitter, step_label):
        deck_manifest = await asyncio.to_thread(
            build_constant_flat_deck,
            run_args,
            deck_dir,
            fuel_model=int(fuel_model),
            canopy=canopy,
            moisture_override=moisture_override,
            weather_schedule=weather_schedule,
            dt_meteorology_s=dt_meteorology_s,
            target_cfl=target_cfl,
            simulator_extra=simulator_extra,
            outputs_extra=outputs_extra,
            inputs_extra=inputs_extra,
            time_control_extra=time_control_extra,
            spotting_extra=spotting_extra,
            fuel_break=fuel_break,
            dt_s=dt_s,
            dtdump_s=dtdump_s,
        )
    grid = deck_manifest.get("grid") or {}
    epsg = int(grid.get("epsg", 5070))

    solve_run_id = await _dispatch_and_wait(
        deck_dir=deck_dir,
        deck_manifest=deck_manifest,
        run_args=run_args,
        compute_class=compute_class,
        emitter=emitter,
        run_id=None,
    )

    out_dir, out_is_temp = await asyncio.to_thread(
        _download_elmfire_outputs, solve_run_id
    )
    rasters = discover_elmfire_rasters(out_dir)
    toa_path = rasters.get("time_of_arrival")
    if toa_path is None:
        raise FireSpreadComposerError(
            "ELMFIRE_NO_LAYERS",
            f"sweep point knob={knob_value} produced no time_of_arrival raster",
        )
    toa_s, transform, _crs, cellsize_m = await asyncio.to_thread(
        read_fire_raster, toa_path, epsg=epsg
    )

    import numpy as np

    burned = np.isfinite(toa_s)
    n_burned = int(burned.sum())
    if n_burned == 0:
        raise FireSpreadComposerError(
            "ELMFIRE_NO_SPREAD",
            f"sweep point knob={knob_value}: no cell burned (constant deck "
            "produced zero spread -- check the fuel/moisture/wind knobs)",
        )
    burned_area_km2 = float(n_burned) * (cellsize_m * cellsize_m) / 1.0e6
    fire_arrival_max_hr = float(np.nanmax(toa_s)) / 3600.0

    # Barrier-jump measurement: with a vertical (axis=x) fuel break, split the
    # burned cells into WEST-of-break (the contiguous head fire) and EAST-of-break
    # (reachable ONLY by lofted embers). Measured off the ToA raster (Invariant 1).
    break_extras: dict[str, float] = {}
    if fuel_break and str(fuel_break.get("axis", "x")).lower() == "x":
        nxr = int(burned.shape[1])
        c_lo = int(float(fuel_break["lo_frac"]) * nxr)
        c_hi = int(float(fuel_break["hi_frac"]) * nxr)
        cell_km2 = (float(cellsize_m) * float(cellsize_m)) / 1.0e6
        east_cells = int(np.isfinite(toa_s[:, c_hi:]).sum())
        west_cells = int(np.isfinite(toa_s[:, :c_lo]).sum())
        break_extras = {
            "east_of_break_cells": float(east_cells),
            "east_of_break_km2": float(east_cells) * cell_km2,
            "west_of_break_km2": float(west_cells) * cell_km2,
        }

    max_spread_rate_m_min: float | None = None
    vs_path = rasters.get("spread_rate")
    if vs_path is not None:
        vs_arr, _t, _c, _cs = await asyncio.to_thread(
            read_fire_raster, vs_path, epsg=epsg
        )
        if np.isfinite(vs_arr).any():
            max_spread_rate_m_min = float(np.nanmax(vs_arr)) * FTMIN_TO_MMIN

    crown_active_area_km2: float | None = None
    crown_any_area_km2: float | None = None
    if measure_crown:
        crown_path = rasters.get("crown_fire")
        if crown_path is not None:
            crown_arr, _t, _c, crown_cs = await asyncio.to_thread(
                read_fire_raster, crown_path, epsg=epsg
            )
            # Per-cell crown-fire type: 2 = active crown, 1 = passive/torching.
            cell_km2 = (float(crown_cs) * float(crown_cs)) / 1.0e6
            active = np.isfinite(crown_arr) & (crown_arr >= 1.5)
            anyc = np.isfinite(crown_arr) & (crown_arr >= 0.5)
            crown_active_area_km2 = float(int(active.sum())) * cell_km2
            crown_any_area_km2 = float(int(anyc.sum())) * cell_km2

    ltw: float | None = None
    err_fraction: float | None = None
    corr_class: str | None = None
    if measure_ltw:
        ign_xy = (deck_manifest.get("ignitions_domain_xy") or [{}])[0]
        ign_x = float(ign_xy.get("x", 0.0))
        ign_y = float(ign_xy.get("y", 0.0))
        inv = ~transform
        ign_col, ign_row = inv * (ign_x, ign_y)
        verification, _overlay = verify_elliptical_replication(
            toa_s,
            cellsize_m=float(cellsize_m),
            ignition_rowcol=(int(round(ign_row)), int(round(ign_col))),
            wind_from_deg=float(run_args.wind_dir_deg),
        )
        ltw = float(verification.get("length_to_width_ratio", 0.0)) or None
        err_fraction = float(verification.get("err_fraction", 0.0))
        corr_class = str(verification.get("corr_class", "poor"))

    return SweepCaseResult(
        knob_value=float(knob_value),
        run_id=solve_run_id,
        out_dir=out_dir,
        out_is_temp=out_is_temp,
        deck_dir=deck_dir,
        epsg=epsg,
        cellsize_m=float(cellsize_m),
        burned_area_km2=burned_area_km2,
        fire_arrival_max_hr=fire_arrival_max_hr,
        max_spread_rate_m_min=max_spread_rate_m_min,
        length_to_width_ratio=ltw,
        err_fraction=err_fraction,
        corr_class=corr_class,
        crown_active_area_km2=crown_active_area_km2,
        crown_any_area_km2=crown_any_area_km2,
        bbox=tuple(run_args.bbox),  # type: ignore[arg-type]
        extras=break_extras,
    )


def publish_primary_from_out_dir(
    case: SweepCaseResult,
    *,
    bbox: tuple[float, float, float, float],
    duration_s: float,
    ignition_lonlat: tuple[float, float],
) -> Any:
    """Postprocess + publish the representative case's time-of-arrival COG.

    Returns the published PRIMARY ``FireSpreadLayerURI`` (the base each template
    decorates into an ``ElmfireSensitivityLayerURI``). Raises
    ``FireSpreadComposerError`` when postprocess yields no layer (honesty floor).
    """
    layers, _metrics = postprocess_elmfire(
        case.out_dir,
        bbox,
        run_id=case.run_id,
        duration_s=duration_s,
        epsg=case.epsg,
        ignition_lonlat=ignition_lonlat,
    )
    if not layers:
        raise FireSpreadComposerError(
            "ELMFIRE_NO_LAYERS",
            "sensitivity postprocess produced no primary layer (honesty floor)",
        )
    return _publish_primary_layer(layers[0], case.run_id)


def cleanup_cases(cases: list[SweepCaseResult], keep_out_dir: str | None) -> None:
    """Remove every case's deck dir + any temp output dir except ``keep_out_dir``."""
    for c in cases:
        _cleanup_dir(c.deck_dir)
        if c.out_is_temp and c.out_dir != keep_out_dir:
            _cleanup_dir(c.out_dir)


def build_sweep_chart_spec(
    sweep: list[dict[str, float]],
    *,
    x_title: str,
    y_title: str,
    reference_y: float | None = None,
    reference_label: str | None = None,
    identity_diagonal: bool = False,
) -> dict[str, Any] | None:
    """Build the generic response-vs-knob Vega-Lite line+point sensitivity chart.

    ``sweep`` is the ascending ``[{"x": knob, "y": response}, ...]`` list. When
    ``reference_y`` is given a horizontal rule is overlaid (e.g. the natural
    length:width plateau, or the deterministic baseline) with ``reference_label``.
    When ``identity_diagonal`` is set a dashed ``y = x`` line is overlaid across
    the swept range (the cap-identity line: where the response tracks the diagonal
    the swept cap is binding). Returns ``None`` for an empty sweep. Pure."""
    if not sweep:
        return None
    line_layer: dict[str, Any] = {
        "mark": {"type": "line", "point": True, "color": "#d1495b"},
        "encoding": {
            "x": {"field": "x", "type": "quantitative", "title": x_title},
            "y": {"field": "y", "type": "quantitative", "title": y_title},
            "tooltip": [
                {"field": "x", "type": "quantitative", "format": ".3g"},
                {"field": "y", "type": "quantitative", "format": ".3g"},
            ],
        },
    }
    layers: list[dict[str, Any]] = [line_layer]
    if identity_diagonal:
        xs = [float(p["x"]) for p in sweep]
        x0, x1 = min(xs), max(xs)
        layers.append(
            {
                "data": {"values": [{"x": x0, "y": x0}, {"x": x1, "y": x1}]},
                "mark": {"type": "line", "color": "#1f5fbf", "strokeDash": [4, 4]},
                "encoding": {
                    "x": {"field": "x", "type": "quantitative"},
                    "y": {"field": "y", "type": "quantitative"},
                },
            }
        )
    if reference_y is not None:
        layers.append(
            {
                "data": {"values": [{"ref": float(reference_y)}]},
                "mark": {"type": "rule", "color": "#3a3a3a", "strokeDash": [2, 3]},
                "encoding": {"y": {"field": "ref", "type": "quantitative"}},
            }
        )
    spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": list(sweep)},
        "layer": layers,
    }
    if reference_label:
        spec["title"] = reference_label
    return spec
