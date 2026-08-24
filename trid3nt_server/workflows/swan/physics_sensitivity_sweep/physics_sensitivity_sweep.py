"""Engine template ``swan_physics_sensitivity_sweep`` - how sensitive is the
nearshore wave field to a chosen SWAN physics scheme?

Runs SEVERAL cheap stationary SWAN solves over ONE coastal AOI, varying a single
physics axis (depth-induced breaker index, JONSWAP bottom-friction coefficient,
wind-input growth formulation, whitecapping scheme, triad biphase, or the DIA
quadruplet integration method) across an A-vs-B(-vs-C) value set while holding the
grid + offshore boundary fixed. It returns the typed wave scalars per scheme
(peak Hs, wave footprint area, mean peak period) and overlays them in ONE
comparison chart so the effect of the knob is directly visible.

This is the SWAN analogue of the SFINCS numerical-knobs template and the
pelicun sensitivity sweeps: the deliverable is the comparison, not a single
field. Every plotted number is a SWAN postprocess scalar carried on
``WaveFieldLayerURI`` - never free-generated (Invariant 1).

Physics note: ``gen_formulation`` / ``whitecapping`` / ``quad_iquad`` shape
WIND-INPUT growth + deep-water processes, so on a boundary-forced-only run (no
wind grid) they barely move the field; the result flags a wind-dependent axis.
The dissipation axes (``breaking_gamma``, ``friction_cfjon``) act directly on
boundary-forced surf-zone waves and are the robustly-demonstrable defaults.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.swan._sweep_common import (
    PHYSICS_AXES,
    SwanSweepError,
    emit_chart_if_live,
    fetch_swan_dem_once,
    multi_series_line_spec,
    run_stationary_solve,
)
from trid3nt_server.workflows.swan._template_card import TemplateCard

logger = logging.getLogger(
    "trid3nt_server.workflows.swan.physics_sensitivity_sweep."
    "physics_sensitivity_sweep"
)

__all__ = [
    "swan_physics_sensitivity_sweep",
    "resolve_axis_values",
    "build_sweep_chart_spec",
    "TEMPLATE_CARD",
]


TEMPLATE_CARD = TemplateCard(
    question=(
        "how sensitive the nearshore wave field is to a SWAN physics scheme -- an "
        "A-vs-B comparison of significant wave height across breaker-index / "
        "bottom-friction / wind-growth / whitecapping / triad / quadruplet choices"
    ),
    required_inputs=["bbox"],
    knobs="axis, values, boundary_hs_m, boundary_tp_s, boundary_dir_deg, boundary_side",
)

_METADATA = AtomicToolMetadata(
    name="swan_physics_sensitivity_sweep",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="swan",
    tier="template",
)


def resolve_axis_values(axis: str, values: list[Any] | None) -> list[Any]:
    """Validate ``axis`` and coerce ``values`` (or the axis default) to its type.

    Raises ``SwanSweepError`` for an unknown axis, an out-of-set categorical value,
    or a non-numeric numeric value. Returns a de-duplicated, order-preserving list
    of at least two values (a sweep needs a comparison).
    """
    if axis not in PHYSICS_AXES:
        raise SwanSweepError(
            "SWAN_SWEEP_AXIS_INVALID",
            f"axis must be one of {sorted(PHYSICS_AXES)}, got {axis!r}",
        )
    spec = PHYSICS_AXES[axis]
    raw = list(values) if values else list(spec["defaults"])
    numeric = axis in ("breaking_gamma", "friction_cfjon", "quad_iquad")
    out: list[Any] = []
    for v in raw:
        if numeric:
            try:
                cv: Any = int(v) if axis == "quad_iquad" else float(v)
            except (TypeError, ValueError) as exc:
                raise SwanSweepError(
                    "SWAN_SWEEP_VALUE_INVALID",
                    f"axis {axis!r} needs numeric values, got {v!r}",
                ) from exc
        else:
            cv = str(v).strip().lower()
        if cv not in out:
            out.append(cv)
    if len(out) < 2:
        raise SwanSweepError(
            "SWAN_SWEEP_VALUE_INVALID",
            f"a sensitivity sweep needs >= 2 distinct {axis!r} values, got {out}",
        )
    return out


def build_sweep_chart_spec(
    axis: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Two overlaid series (peak Hs + wave footprint, each normalized to the first
    scheme -> baseline 1.0) across the swept values, in ONE figure. Pure.

    Normalizing both metrics to the baseline scheme puts the domain-peak Hs and the
    (dissipation-sensitive) wave footprint on ONE dimensionless axis so their
    differing response to the knob reads together -- a color-field-grouped line
    with a legend (never duplicated categories). A zero baseline falls back to the
    raw value for that metric (no divide-by-zero).
    """
    mean0 = results[0]["mean_hs_m"] or 0.0
    peak0 = results[0]["max_hs_m"] or 0.0
    rows: list[dict[str, Any]] = []
    for r in results:
        scheme = str(r["scheme"])
        # Mean Hs (whole-field) is dissipation-sensitive -- it is the series that
        # MOVES with the knob. Peak Hs is boundary-pinned and included as the
        # near-flat reference so the contrast reads.
        rows.append({
            "scheme": scheme,
            "metric": "mean Hs (rel.)",
            "value": (r["mean_hs_m"] / mean0) if mean0 else r["mean_hs_m"],
        })
        rows.append({
            "scheme": scheme,
            "metric": "peak Hs (rel.)",
            "value": (r["max_hs_m"] / peak0) if peak0 else r["max_hs_m"],
        })
    label = PHYSICS_AXES[axis]["label"]
    return multi_series_line_spec(
        rows,
        x_field="scheme",
        y_field="value",
        color_field="metric",
        x_title=label,
        y_title="value relative to baseline",
        title=f"SWAN wave-field sensitivity to {label}",
    )


@register_tool(
    _METADATA,
    # readOnlyHint=False (runs solvers writing output COG artifacts),
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def swan_physics_sensitivity_sweep(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    axis: str = "friction_cfjon",
    values: list[Any] | None = None,
    boundary_hs_m: float | None = None,
    boundary_tp_s: float | None = None,
    boundary_dir_deg: float | None = None,
    boundary_spread_deg: float | None = None,
    boundary_side: str | None = None,
    n_dir: int = 24,
    n_freq: int = 24,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Compare the SWAN nearshore wave field across one physics-scheme axis.

    Fidelity: runs N cheap STATIONARY SWAN solves over the SAME coastal AOI +
    offshore boundary, varying ONE physics knob; compares the typed wave scalars.
    A sensitivity comparison, not a calibrated single field. Requires real
    below-datum bathymetry (a coastal AOI). Off-scope: a single defensible wave
    field -> swan_wave_field; compound-flood inundation -> sfincs_flood.

    Use this when: the user asks how sensitive significant wave height is to the
    breaker index / bottom friction / wind-growth formulation / whitecapping /
    triad / quadruplet choice, or wants an A-vs-B (uncertainty/QA) physics
    comparison on a coastal case.

    Params:
        bbox: coastal computational-domain AOI, EPSG:4326 (required).
        axis: the physics axis to vary -- one of "friction_cfjon" (default;
            whole-path bottom dissipation, demonstrable on any shelf),
            "breaking_gamma" (surf-zone breaker index, strongest on a shallow
            beach/reef), "gen_formulation", "whitecapping", "triad_biphase",
            "quad_iquad". The dissipation axes act without wind; the GEN/whitecap/
            quadruplet axes are wind-dependent (flagged in the result).
        values: the value set to sweep (>= 2); defaults per axis (e.g.
            breaking_gamma [0.55, 0.73, 0.9], friction_cfjon [0.019, 0.038, 0.067]).
        boundary_hs_m/boundary_tp_s/boundary_dir_deg/boundary_spread_deg/
            boundary_side: shared offshore boundary sea state; unset synthesizes a
            demo storm from the AOI.
        n_dir/n_freq: spectral discretization (coarse 24/24 to keep the sweep cheap).

    Returns:
        On success: ``{"status": "ok", "axis", "axis_label", "wind_dependent",
        "schemes": [{"scheme", "max_hs_m", "wave_area_km2", "mean_tp_s",
        "mean_dir_deg", "layer_id"}...], "chart_emitted"}``. A comparison chart is
        emitted when a live emitter is bound.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "SWAN_SWEEP_PARAMS_INCOMPLETE",
            "error_message": "swan_physics_sensitivity_sweep requires a coastal bbox.",
        }
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "SWAN_SWEEP_PARAMS_INVALID",
            "error_message": f"invalid bbox: {bbox!r}",
        }
    try:
        swept = resolve_axis_values(str(axis), values)
    except SwanSweepError as exc:
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}

    field = PHYSICS_AXES[str(axis)]["field"]
    boundary_kwargs: dict[str, Any] = {}
    for key, val in (
        ("hs_m", boundary_hs_m),
        ("tp_s", boundary_tp_s),
        ("dir_deg", boundary_dir_deg),
        ("spread_deg", boundary_spread_deg),
        ("side", boundary_side),
    ):
        if val is not None:
            boundary_kwargs[key] = val

    bbox_t = tuple(float(v) for v in coerced)  # type: ignore[arg-type]
    try:
        dem_uri = await asyncio.to_thread(fetch_swan_dem_once, bbox_t)
        results: list[dict[str, Any]] = []
        for value in swept:
            layer = await run_stationary_solve(
                bbox=bbox_t,
                dem_uri=dem_uri,
                boundary_kwargs=boundary_kwargs,
                overrides={field: value},
                n_dir=n_dir,
                n_freq=n_freq,
            )
            results.append({
                "scheme": str(value),
                "max_hs_m": float(layer.max_hs_m),
                "mean_hs_m": float(getattr(layer, "mean_hs_m", 0.0)),
                "wave_area_km2": float(layer.wave_area_km2),
                "mean_tp_s": float(layer.mean_tp_s),
                "mean_dir_deg": float(layer.mean_dir_deg),
                "layer_id": layer.layer_id,
                "uri": layer.uri,
            })
        spec = build_sweep_chart_spec(str(axis), results)
        label = PHYSICS_AXES[str(axis)]["label"]
        emitted = await emit_chart_if_live(
            spec,
            title=f"SWAN wave-field sensitivity to {label}",
            caption=(
                f"Peak Hs + wave footprint relative to baseline across "
                f"{len(results)} {axis} values (stationary solves, shared boundary)."
            ),
        )
        logger.info(
            "swan_physics_sensitivity_sweep axis=%s values=%s mean_hs=%s max_hs=%s",
            axis, swept,
            [round(r["mean_hs_m"], 4) for r in results],
            [round(r["max_hs_m"], 4) for r in results],
        )
        return {
            "status": "ok",
            "axis": str(axis),
            "axis_label": label,
            "wind_dependent": bool(PHYSICS_AXES[str(axis)]["wind_dependent"]),
            "schemes": results,
            "chart_emitted": emitted,
        }
    except asyncio.CancelledError:
        raise
    except SwanSweepError as exc:
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("swan_physics_sensitivity_sweep failed: %s", exc)
        return {
            "status": "error",
            "error_code": "SWAN_SWEEP_ERROR",
            "error_message": f"physics sensitivity sweep failed: {exc}",
        }
