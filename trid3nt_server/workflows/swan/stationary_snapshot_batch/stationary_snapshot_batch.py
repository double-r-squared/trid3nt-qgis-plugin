"""Engine template ``swan_stationary_snapshot_batch`` - wave conditions at several
discrete times through an event, without a full nonstationary run.

Runs a SEQUENCE of independent STATIONARY SWAN solves over ONE coastal AOI, each
with its own offshore boundary sea state (a rising-then-falling storm, or an
explicit list of {Hs, Tp, dir} snapshots). Each solve is a self-contained
storm-peak field; together they sample the event at the requested instants at a
fraction of the cost of marching the action-balance equation forward in time
(MODE NONSTATIONARY). Every solve emits its own wave-height layer, and the peak-Hs
+ wave-footprint trajectory across the snapshots is overlaid in ONE chart.

Pure orchestration of the existing stationary solve (no new physics): the SWAN
analogue of a batch of ``swan_wave_field`` calls with a shared DEM fetch. Every
plotted number is a SWAN postprocess scalar carried on ``WaveFieldLayerURI`` -
never free-generated (Invariant 1).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.swan._sweep_common import (
    SwanSweepError,
    emit_chart_if_live,
    fetch_swan_dem_once,
    multi_series_line_spec,
    run_stationary_solve,
)
from trid3nt_server.workflows.swan._template_card import TemplateCard

logger = logging.getLogger(
    "trid3nt_server.workflows.swan.stationary_snapshot_batch."
    "stationary_snapshot_batch"
)

__all__ = [
    "swan_stationary_snapshot_batch",
    "resolve_snapshots",
    "build_snapshot_chart_spec",
    "TEMPLATE_CARD",
]

#: The default event: a symmetric storm build-up + decay in offshore Hs (m), each
#: entry a discrete stationary snapshot. Tp scales mildly with Hs (steeper seas at
#: peak); direction/side come from the caller or the AOI-derived demo boundary.
_DEFAULT_HS_SEQUENCE: tuple[float, ...] = (2.0, 3.5, 5.0, 3.5, 2.0)


TEMPLATE_CARD = TemplateCard(
    question=(
        "nearshore wave conditions at several discrete times through a storm event "
        "-- a batch of stationary SWAN snapshots instead of a full nonstationary run"
    ),
    required_inputs=["bbox"],
    knobs="hs_sequence, snapshots, boundary_tp_s, boundary_dir_deg, boundary_side",
)

_METADATA = AtomicToolMetadata(
    name="swan_stationary_snapshot_batch",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="swan",
    tier="template",
)


def resolve_snapshots(
    hs_sequence: list[float] | None,
    snapshots: list[dict[str, Any]] | None,
    *,
    default_tp_s: float | None,
    default_dir_deg: float | None,
    default_spread_deg: float | None,
    default_side: str | None,
) -> list[dict[str, Any]]:
    """Resolve the per-snapshot boundary sea-state list (>= 2 snapshots).

    ``snapshots`` (explicit per-instant {hs_m, tp_s, dir_deg, spread_deg, side,
    label} dicts) wins; else ``hs_sequence`` (or the default storm build-up/decay)
    is expanded, filling tp/dir/spread/side from the shared defaults. Raises
    ``SwanSweepError`` for fewer than 2 snapshots or a non-positive Hs.
    """
    resolved: list[dict[str, Any]] = []
    if snapshots:
        for i, snap in enumerate(snapshots):
            if not isinstance(snap, dict):
                raise SwanSweepError(
                    "SWAN_BATCH_SNAPSHOT_INVALID",
                    f"snapshot {i} must be an object, got {snap!r}",
                )
            hs = snap.get("hs_m")
            if hs is None or float(hs) <= 0.0:
                raise SwanSweepError(
                    "SWAN_BATCH_SNAPSHOT_INVALID",
                    f"snapshot {i} needs hs_m > 0, got {hs!r}",
                )
            entry: dict[str, Any] = {"hs_m": float(hs)}
            for k in ("tp_s", "dir_deg", "spread_deg", "side"):
                if snap.get(k) is not None:
                    entry[k] = snap[k]
            entry["label"] = str(snap.get("label") or f"t{i}")
            resolved.append(entry)
    else:
        seq = list(hs_sequence) if hs_sequence else list(_DEFAULT_HS_SEQUENCE)
        for i, hs in enumerate(seq):
            if float(hs) <= 0.0:
                raise SwanSweepError(
                    "SWAN_BATCH_SNAPSHOT_INVALID",
                    f"hs_sequence[{i}] must be > 0, got {hs!r}",
                )
            resolved.append({"hs_m": float(hs), "label": f"t{i}"})
    if len(resolved) < 2:
        raise SwanSweepError(
            "SWAN_BATCH_SNAPSHOT_INVALID",
            f"a snapshot batch needs >= 2 snapshots, got {len(resolved)}",
        )
    # Fill shared boundary defaults where a snapshot did not specify them.
    for entry in resolved:
        if default_tp_s is not None:
            entry.setdefault("tp_s", default_tp_s)
        if default_dir_deg is not None:
            entry.setdefault("dir_deg", default_dir_deg)
        if default_spread_deg is not None:
            entry.setdefault("spread_deg", default_spread_deg)
        if default_side is not None:
            entry.setdefault("side", default_side)
    return resolved


def build_snapshot_chart_spec(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Peak-Hs + wave-footprint trajectory across the snapshot sequence. Pure.

    A two-series line (color = metric) over the ordered snapshot labels shows the
    event's evolution as sampled by the discrete stationary solves.
    """
    rows: list[dict[str, Any]] = []
    for r in results:
        rows.append({"snapshot": r["label"], "metric": "peak Hs (m)", "value": r["max_hs_m"]})
        rows.append({
            "snapshot": r["label"],
            "metric": "wave footprint (km2)",
            "value": r["wave_area_km2"],
        })
    return multi_series_line_spec(
        rows,
        x_field="snapshot",
        y_field="value",
        color_field="metric",
        x_title="event snapshot",
        y_title="value",
        title="SWAN stationary snapshot batch - wave-field evolution",
    )


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def swan_stationary_snapshot_batch(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    hs_sequence: list[float] | None = None,
    snapshots: list[dict[str, Any]] | None = None,
    boundary_tp_s: float | None = None,
    boundary_dir_deg: float | None = None,
    boundary_spread_deg: float | None = None,
    boundary_side: str | None = None,
    n_dir: int = 24,
    n_freq: int = 24,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Sample a storm event with a BATCH of stationary SWAN snapshots.

    Fidelity: runs one STATIONARY SWAN solve per requested instant over the SAME
    coastal AOI, each with its own offshore Hs/Tp/direction; each is a
    self-contained storm-peak field. Cheaper than a full nonstationary
    time-marching run; samples discrete times, does NOT resolve the continuous
    action-balance evolution between them. Requires real below-datum bathymetry.
    Off-scope: minute-by-minute wave evolution -> swan_wave_field(mode=
    "nonstationary"); a single field -> swan_wave_field.

    Use this when: the user wants wave conditions at several times through an event
    (storm build-up and decay) without paying for a nonstationary run, or a set of
    discrete design sea states over one coast.

    Params:
        bbox: coastal computational-domain AOI, EPSG:4326 (required).
        hs_sequence: offshore Hs (m) per snapshot (default storm build-up/decay
            [2.0, 3.5, 5.0, 3.5, 2.0]); tp/direction/side shared across snapshots.
        snapshots: explicit per-instant boundary dicts ({hs_m, tp_s, dir_deg,
            spread_deg, side, label}); wins over hs_sequence when supplied.
        boundary_tp_s/boundary_dir_deg/boundary_spread_deg/boundary_side: shared
            offshore boundary fields filled where a snapshot omits them.
        n_dir/n_freq: spectral discretization (coarse 24/24 to keep the batch cheap).

    Returns:
        On success: ``{"status": "ok", "snapshots": [{"label", "hs_boundary_m",
        "max_hs_m", "wave_area_km2", "mean_tp_s", "layer_id"}...], "chart_emitted"}``.
        Each snapshot also emits its own wave-height layer; a trajectory chart is
        emitted when a live emitter is bound.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "SWAN_BATCH_PARAMS_INCOMPLETE",
            "error_message": "swan_stationary_snapshot_batch requires a coastal bbox.",
        }
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "SWAN_BATCH_PARAMS_INVALID",
            "error_message": f"invalid bbox: {bbox!r}",
        }
    try:
        resolved = resolve_snapshots(
            hs_sequence,
            snapshots,
            default_tp_s=boundary_tp_s,
            default_dir_deg=boundary_dir_deg,
            default_spread_deg=boundary_spread_deg,
            default_side=boundary_side,
        )
    except SwanSweepError as exc:
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}

    bbox_t = tuple(float(v) for v in coerced)  # type: ignore[arg-type]
    try:
        dem_uri = await asyncio.to_thread(fetch_swan_dem_once, bbox_t)
        results: list[dict[str, Any]] = []
        for snap in resolved:
            boundary_kwargs = {k: v for k, v in snap.items() if k != "label"}
            layer = await run_stationary_solve(
                bbox=bbox_t,
                dem_uri=dem_uri,
                boundary_kwargs=boundary_kwargs,
                overrides={},
                n_dir=n_dir,
                n_freq=n_freq,
            )
            results.append({
                "label": snap["label"],
                "hs_boundary_m": float(snap["hs_m"]),
                "max_hs_m": float(layer.max_hs_m),
                "wave_area_km2": float(layer.wave_area_km2),
                "mean_tp_s": float(layer.mean_tp_s),
                "layer_id": layer.layer_id,
                "uri": layer.uri,
            })
        spec = build_snapshot_chart_spec(results)
        emitted = await emit_chart_if_live(
            spec,
            title="SWAN stationary snapshot batch - wave-field evolution",
            caption=(
                f"Peak Hs + wave footprint across {len(results)} stationary "
                "snapshots sampling the event (shared AOI + bathymetry)."
            ),
        )
        logger.info(
            "swan_stationary_snapshot_batch n=%d peak_hs=%s",
            len(results), [round(r["max_hs_m"], 3) for r in results],
        )
        return {"status": "ok", "snapshots": results, "chart_emitted": emitted}
    except asyncio.CancelledError:
        raise
    except SwanSweepError as exc:
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("swan_stationary_snapshot_batch failed: %s", exc)
        return {
            "status": "error",
            "error_code": "SWAN_BATCH_ERROR",
            "error_message": f"stationary snapshot batch failed: {exc}",
        }
