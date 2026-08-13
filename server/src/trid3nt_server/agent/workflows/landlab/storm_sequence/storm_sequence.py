"""Engine template ``landlab_storm_sequence_generator`` - a Landlab
PrecipitationDistribution stochastic storm-sequence forcing generator.

A distinct question CLASS (per the capability-naming rule): instead of one fixed
design storm, draw a realistic multi-year sequence of storm / interstorm events
for this location and report its climatology. A reusable FORCING utility surfaced
as a chart-led diagnostic (the 0141 storm-ensemble knob generalized); other chains
(overland flow, groundwater, landslide ensembles) consume the same generator.

The generator is spatially-uniform POINT rainfall (a Poisson storm/interstorm/depth
process), so it needs no DEM and no grid solve: the composer runs
PrecipitationDistribution IN-PROCESS (deterministic, sub-second, wrapped in a
thread) and emits an AOI marker + the storm-sequence time series + the
storm-statistics distribution. It is its OWN registered engine TEMPLATE
(engine="landlab", tier="template").

Determinism boundary (Invariant 1): every number the agent narrates comes from the
typed ``LandlabStormSequenceLayerURI`` fields the composer computed. The sequence
is a LABELED stochastic demo climatology (SyntheticInput), not a fetched record.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.execution import LegendClass, LegendKey
from trid3nt_contracts.landlab_contracts import LandlabStormSequenceLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.landlab._composer_common import (
    emit_landlab_chart,
    emit_zoom_to,
)
from trid3nt_server.agent.workflows.landlab._template_card import TemplateCard
from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
    PostprocessLandlabError,
    build_storm_sequence_chart_spec,
    build_storm_statistics_chart_spec,
)
from trid3nt_server.emission.pipeline_emitter import current_emitter, substep

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.landlab.storm_sequence.storm_sequence"
)

__all__ = [
    "landlab_storm_sequence_generator",
    "model_landlab_storm_sequence",
    "generate_storm_sequence",
    "StormSequenceWorkflowError",
]

#: Point-marker style preset (the AOI anchor for the spatially-uniform forcing).
STORM_MARKER_STYLE_PRESET: str = "mesh_grid"

#: Upper bound on drawn storms (guards a pathological param set from unbounded
#: draws); the default 5-year span at ~15 mm mean depth draws a few hundred.
_MAX_STORMS: int = 20_000


class StormSequenceWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "drive this analysis with a realistic multi-year sequence of "
        "storm/interstorm events instead of one fixed design storm - the "
        "stochastic storm-sequence generator + its climatology (Landlab "
        "PrecipitationDistribution; sequence time series + storm-depth statistics)"
    ),
    required_inputs=["bbox"],
    knobs="mean_storm_duration_hr, mean_interstorm_duration_hr, mean_storm_depth_mm, storm_total_years, random_seed",
)

_METADATA = AtomicToolMetadata(
    name="landlab_storm_sequence_generator",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="landlab",
    tier="template",
)


def generate_storm_sequence(
    *,
    mean_storm_duration_hr: float,
    mean_interstorm_duration_hr: float,
    mean_storm_depth_mm: float,
    total_years: float,
    random_seed: int,
) -> tuple[list[dict[str, float]], dict[str, float]]:
    """Draw a Poisson storm sequence via Landlab PrecipitationDistribution.

    Pure + deterministic (fixed seed). Returns ``(sequence, stats)`` where
    ``sequence`` is the per-storm list ({start_day, depth_mm, duration_hr,
    intensity_mm_hr, interstorm_hr}) and ``stats`` are the climatology scalars the
    layer narrates. Lazy-imports landlab (only when this runs).
    """
    from landlab.components import PrecipitationDistribution  # type: ignore

    total_hr = max(float(total_years), 0.0) * 365.0 * 24.0
    pd = PrecipitationDistribution(
        mean_storm_duration=float(mean_storm_duration_hr),
        mean_interstorm_duration=float(mean_interstorm_duration_hr),
        mean_storm_depth=float(mean_storm_depth_mm),
        total_t=max(total_hr, 1.0),
        delta_t=1.0,
        random_seed=int(random_seed),
    )
    sequence: list[dict[str, float]] = []
    elapsed_hr = 0.0
    guard = 0
    while elapsed_hr < total_hr and guard < _MAX_STORMS:
        pd.update()
        dur = float(pd.storm_duration)
        inter = float(pd.interstorm_duration)
        depth = float(pd.storm_depth)
        intensity = float(pd.intensity)
        start_day = elapsed_hr / 24.0
        if depth > 0.0:
            sequence.append(
                {
                    "start_day": round(start_day, 4),
                    "depth_mm": round(depth, 4),
                    "duration_hr": round(dur, 4),
                    "intensity_mm_hr": round(intensity, 5),
                    "interstorm_hr": round(inter, 4),
                }
            )
        elapsed_hr += dur + inter
        guard += 1

    n = len(sequence)
    if n == 0:
        stats = {
            "n_storms": 0,
            "total_rainfall_mm": 0.0,
            "mean_storm_depth_mm": 0.0,
            "mean_storm_intensity_mm_hr": 0.0,
            "mean_storm_duration_hr": 0.0,
            "mean_interstorm_duration_hr": 0.0,
            "max_storm_depth_mm": 0.0,
        }
        return sequence, stats

    depths = [s["depth_mm"] for s in sequence]
    stats = {
        "n_storms": n,
        "total_rainfall_mm": float(sum(depths)),
        "mean_storm_depth_mm": float(sum(depths) / n),
        "mean_storm_intensity_mm_hr": float(
            sum(s["intensity_mm_hr"] for s in sequence) / n
        ),
        "mean_storm_duration_hr": float(sum(s["duration_hr"] for s in sequence) / n),
        "mean_interstorm_duration_hr": float(
            sum(s["interstorm_hr"] for s in sequence) / n
        ),
        "max_storm_depth_mm": float(max(depths)),
    }
    return sequence, stats


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def landlab_storm_sequence_generator(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    mean_storm_duration_hr: float = 2.0,
    mean_interstorm_duration_hr: float = 48.0,
    mean_storm_depth_mm: float = 15.0,
    storm_total_years: float = 5.0,
    random_seed: int = 1234,
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> LandlabStormSequenceLayerURI | dict[str, Any]:
    """Draw a realistic multi-year stochastic storm sequence for a location.

    Fidelity: Landlab PrecipitationDistribution (Poisson storm/interstorm/depth) -
    a spatially-uniform stochastic forcing generator, not a fetched historical
    record.
    Data: the storm sequence is a LABELED stochastic demo climatology
    (SyntheticInput); the generator means are demo defaults, not a fitted local
    climate. Deterministic (fixed random_seed).
    Off-scope: a fixed single design storm from NOAA Atlas-14 (that is the
    overland-flow / green-ampt rainfall seam); spatial rainfall fields.

    Use this when: the user asks for a STOCHASTIC / RANDOM STORM SEQUENCE, a
    multi-year storm generator, a synthetic rainfall sequence, or to drive an
    analysis with drawn storms instead of one fixed design storm.

    Params:
        bbox: AOI the storm climatology applies to (the map anchor), EPSG:4326.
        mean_storm_duration_hr: mean storm duration, hours (default 2).
        mean_interstorm_duration_hr: mean dry interval, hours (default 48).
        mean_storm_depth_mm: mean per-storm depth, mm (default 15).
        storm_total_years: simulated span of the sequence, yr (default 5).
        random_seed: deterministic Poisson seed (default 1234).
        input_mode: run-mode lever. "user_gated" reviews the demo climatology
            before drawing; "auto" (default) proceeds labeled.

    Returns:
        On success: ``LandlabStormSequenceLayerURI`` - an AOI marker with
        ``n_storms``, ``total_rainfall_mm``, ``mean_storm_depth_mm``,
        ``mean_storm_intensity_mm_hr``, ``mean_interstorm_duration_hr``,
        ``max_storm_depth_mm``. The storm-sequence time series + storm-depth
        statistics charts are emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if bbox is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INCOMPLETE",
            "error_message": (
                "landlab_storm_sequence_generator requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "LANDLAB_PARAMS_INVALID",
            "error_message": f"invalid bbox: {bbox!r}",
        }

    provenance: list[SyntheticInput] = [
        SyntheticInput(
            param="storm_climatology",
            value=(
                f"mean depth {mean_storm_depth_mm} mm, storm {mean_storm_duration_hr} h, "
                f"interstorm {mean_interstorm_duration_hr} h, {storm_total_years} yr"
            ),
            basis="default_demo",
            real_source_if_any="landlab PrecipitationDistribution (Poisson)",
            note="storm-generator means are demo defaults, not a fitted local climate",
        ),
    ]
    source_note = (
        f"stochastic storm sequence: Poisson generator over {storm_total_years} yr "
        f"(mean depth {mean_storm_depth_mm} mm) - a demo climatology, not a fetched "
        "historical record."
    )

    _review = await gate_input_review(
        tool_name="landlab_storm_sequence_generator",
        mode=input_mode,
        entries=provenance,
        params={
            "mean_storm_depth_mm": mean_storm_depth_mm,
            "storm_total_years": storm_total_years,
        },
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"landlab_storm_sequence_generator {_review.cancel_reason}",
        }
    provenance = _review.entries
    mean_storm_depth_mm = float(
        _review.params.get("mean_storm_depth_mm", mean_storm_depth_mm)
    )
    storm_total_years = float(
        _review.params.get("storm_total_years", storm_total_years)
    )

    logger.info(
        "landlab_storm_sequence_generator bbox=%s depth=%.1fmm years=%.1f seed=%d",
        tuple(coerced), mean_storm_depth_mm, storm_total_years, random_seed,
    )

    try:
        layer = await model_landlab_storm_sequence(
            bbox=tuple(coerced),  # type: ignore[arg-type]
            mean_storm_duration_hr=float(mean_storm_duration_hr),
            mean_interstorm_duration_hr=float(mean_interstorm_duration_hr),
            mean_storm_depth_mm=float(mean_storm_depth_mm),
            total_years=float(storm_total_years),
            random_seed=int(random_seed),
            source_note=source_note,
            synthetic_inputs=provenance,
        )
        logger.info(
            "landlab_storm_sequence_generator complete layer_id=%s n_storms=%d "
            "total_rain=%.1fmm mean_depth=%.2fmm uri=%s",
            layer.layer_id, layer.n_storms, layer.total_rainfall_mm,
            layer.mean_storm_depth_mm, layer.uri,
        )
        return layer
    except asyncio.CancelledError:
        raise
    except (PostprocessLandlabError, StormSequenceWorkflowError) as exc:
        logger.warning(
            "landlab_storm_sequence_generator failed: %s (%s)",
            getattr(exc, "error_code", "?"), exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "LANDLAB_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("landlab_storm_sequence_generator unexpected failure")
        return {
            "status": "error",
            "error_code": "LANDLAB_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_landlab_storm_sequence(
    *,
    bbox: tuple[float, float, float, float],
    mean_storm_duration_hr: float,
    mean_interstorm_duration_hr: float,
    mean_storm_depth_mm: float,
    total_years: float,
    random_seed: int,
    run_id: str | None = None,
    source_note: str | None = None,
    synthetic_inputs: list[SyntheticInput] | None = None,
) -> LandlabStormSequenceLayerURI:
    """Compose the storm-sequence generator end-to-end (IN-PROCESS lane).

    Draws the sequence, uploads an AOI marker, and emits the sequence + statistics
    charts. Returns the ``LandlabStormSequenceLayerURI`` carrier.
    """
    from trid3nt_server.agent.tools.simulation.solver.solver import new_ulid
    from trid3nt_server.agent.workflows.landlab.postprocess_landlab import (
        _upload_geojson_to_runs_bucket,
    )

    emitter = current_emitter()
    rid = run_id or new_ulid()
    await emit_zoom_to(emitter, bbox)

    async with substep(current_emitter(), "generate_storm_sequence"):
        sequence, stats = await asyncio.to_thread(
            generate_storm_sequence,
            mean_storm_duration_hr=mean_storm_duration_hr,
            mean_interstorm_duration_hr=mean_interstorm_duration_hr,
            mean_storm_depth_mm=mean_storm_depth_mm,
            total_years=total_years,
            random_seed=random_seed,
        )

    # AOI centroid marker (the spatially-uniform forcing anchors to a point).
    cx = (float(bbox[0]) + float(bbox[2])) / 2.0
    cy = (float(bbox[1]) + float(bbox[3])) / 2.0
    marker = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "storm_generator": 1,
                    "n_storms": stats["n_storms"],
                    "total_rainfall_mm": stats["total_rainfall_mm"],
                },
                "geometry": {"type": "Point", "coordinates": [cx, cy]},
            }
        ],
    }
    async with substep(current_emitter(), "publish_layer"):
        marker_uri = await asyncio.to_thread(
            _upload_geojson_to_runs_bucket,
            marker,
            rid,
            None,
            dest_filename="landlab_storm_sequence_marker.geojson",
        )

    layer = LandlabStormSequenceLayerURI(
        layer_id=f"landlab-storm-sequence-{rid}",
        name=(
            f"Storm sequence ({stats['n_storms']} storms / {total_years:g} yr, "
            f"mean {stats['mean_storm_depth_mm']:.1f} mm)"
        ),
        layer_type="vector",
        uri=marker_uri,
        style_preset=STORM_MARKER_STYLE_PRESET,
        role="primary",
        bbox=tuple(bbox),
        legend=LegendKey(
            kind="categorical",
            classes=[
                LegendClass(
                    value="storm_generator",
                    color="#1f5fbf",
                    label="Stochastic storm generator (AOI)",
                )
            ],
            label="Storm sequence",
        ),
        n_storms=int(stats["n_storms"]),
        total_years=float(total_years),
        total_rainfall_mm=float(stats["total_rainfall_mm"]),
        mean_storm_depth_mm=float(stats["mean_storm_depth_mm"]),
        mean_storm_intensity_mm_hr=float(stats["mean_storm_intensity_mm_hr"]),
        mean_storm_duration_hr=float(stats["mean_storm_duration_hr"]),
        mean_interstorm_duration_hr=float(stats["mean_interstorm_duration_hr"]),
        max_storm_depth_mm=float(stats["max_storm_depth_mm"]),
    )
    if source_note is not None:
        layer = layer.model_copy(update={"source_note": source_note})
    if synthetic_inputs:
        layer = layer.model_copy(update={"synthetic_inputs": list(synthetic_inputs)})

    await emit_landlab_chart(
        emitter,
        build_storm_sequence_chart_spec(sequence),
        title="Stochastic storm sequence",
        caption=(
            f"{stats['n_storms']} drawn storms over {total_years:g} yr; per-storm "
            f"depth vs time (total {stats['total_rainfall_mm']:.0f} mm, max "
            f"{stats['max_storm_depth_mm']:.1f} mm)."
        ),
        source_uri=layer.uri,
    )
    await emit_landlab_chart(
        emitter,
        build_storm_statistics_chart_spec(sequence),
        title="Storm-depth distribution",
        caption=(
            f"Histogram of the {stats['n_storms']} per-storm depths - the Poisson "
            f"climatology (mean {stats['mean_storm_depth_mm']:.1f} mm, mean intensity "
            f"{stats['mean_storm_intensity_mm_hr']:.2f} mm/hr)."
        ),
        source_uri=layer.uri,
    )
    return layer
