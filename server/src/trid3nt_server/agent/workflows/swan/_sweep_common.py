"""Shared machinery for the SWAN multi-run templates (physics sensitivity sweep +
stationary snapshot batch).

Both templates run SEVERAL cheap stationary SWAN solves over ONE AOI and compare
the resulting typed wave scalars in a single overlay chart. What they share:

  - :func:`fetch_swan_dem_once` -- resolve the topo/bathy DEM ONCE (a real network
    fetch) so N solves reuse it instead of re-fetching per run.
  - :func:`run_stationary_solve` -- run one stationary ``model_swan_wave_field``
    solve with a set of run-arg overrides and return its typed
    ``WaveFieldLayerURI`` (max_hs_m / wave_area_km2 / mean_tp_s / mean_dir_deg).
  - :data:`PHYSICS_AXES` -- the physics-scheme axes the sweep can vary, each with a
    default value list, a human label, and a wind-dependence flag.
  - :func:`grouped_bar_spec` / :func:`multi_series_line_spec` -- pure Vega-Lite
    specs kept readable at the dock's 6.0x2.2in geometry (few nominal x
    categories, color-field series + legend, no rotated-collision labels).
  - :func:`emit_chart_if_live` -- side-emit a chart through the live pipeline
    emitter when one is bound (a no-op offline).

Determinism boundary (Invariant 1): every plotted number is a SWAN postprocess
scalar carried on ``WaveFieldLayerURI`` -- never free-generated.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("trid3nt_server.agent.workflows.swan._sweep_common")

__all__ = [
    "SwanSweepError",
    "PHYSICS_AXES",
    "fetch_swan_dem_once",
    "run_stationary_solve",
    "multi_series_line_spec",
    "emit_chart_if_live",
    "VEGA_LITE_V5_SCHEMA",
]

VEGA_LITE_V5_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


class SwanSweepError(RuntimeError):
    """A SWAN sweep/batch template failed before producing a comparison.

    Carries an open-set ``error_code`` for the WebSocket error frame. Raised for
    invalid knobs, a DEM without real bathymetry, or an empty solve.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# The physics axes the sweep can vary. Each maps ONE ``SwanRunArgs`` field to a
# default value list. ``wind_dependent`` flags axes (GEN formulation, quadruplet
# integration) whose effect is only meaningful WITH a wind field -- on a
# boundary-forced-only run they barely move the field, so the sweep labels them.
# --------------------------------------------------------------------------- #
PHYSICS_AXES: dict[str, dict[str, Any]] = {
    "breaking_gamma": {
        "field": "breaking_gamma",
        "label": "depth-induced breaker index (gamma)",
        "defaults": [0.55, 0.73, 0.9],
        "wind_dependent": False,
    },
    "friction_cfjon": {
        "field": "friction_cfjon",
        "label": "JONSWAP bottom-friction coefficient (cfjon)",
        "defaults": [0.019, 0.038, 0.067],
        "wind_dependent": False,
    },
    "gen_formulation": {
        "field": "gen_formulation",
        "label": "wind-input growth formulation",
        "defaults": ["westhuysen", "komen", "janssen"],
        "wind_dependent": True,
    },
    "whitecapping": {
        "field": "whitecapping",
        "label": "whitecapping dissipation scheme",
        "defaults": ["ab", "komen"],
        "wind_dependent": True,
    },
    "triad_biphase": {
        "field": "triad_biphase",
        "label": "triad biphase parametrization",
        "defaults": ["eldeberky", "dewit"],
        "wind_dependent": False,
    },
    "quad_iquad": {
        "field": "quad_iquad",
        "label": "DIA quadruplet integration method (iquad)",
        "defaults": [2, 3],
        "wind_dependent": True,
    },
}


def fetch_swan_dem_once(bbox: tuple[float, float, float, float]) -> str:
    """Resolve the topo/bathy DEM for the AOI once (reused across N solves).

    Delegates to the wave-field composer's ``_fetch_bathy_for_swan`` (the SEAMLESS
    land+bathy fetch that REJECTS a land-only fallback), so a coastal AOI with no
    real below-datum bathymetry fails loudly here -- before N guaranteed no-op
    solves -- rather than silently returning all-calm fields.
    """
    from trid3nt_server.agent.workflows.swan.wave_field.wave_field import (
        SwanComposerError,
        _fetch_bathy_for_swan,
    )

    try:
        return _fetch_bathy_for_swan(bbox)
    except SwanComposerError as exc:
        raise SwanSweepError(exc.error_code, str(exc)) from exc


async def run_stationary_solve(
    *,
    bbox: tuple[float, float, float, float],
    dem_uri: str,
    boundary_kwargs: dict[str, Any],
    overrides: dict[str, Any],
    n_dir: int = 24,
    n_freq: int = 24,
) -> Any:
    """Run ONE stationary ``model_swan_wave_field`` solve; return its layer.

    ``boundary_kwargs`` (hs_m/tp_s/dir_deg/spread_deg/side) build the parametric
    offshore boundary; ``overrides`` set the physics-scheme fields for this run.
    A coarse default spectral grid (24 dir x 24 freq) keeps the sweep cheap. The
    returned ``WaveFieldLayerURI`` carries the typed scalars the chart plots.
    """
    from trid3nt_contracts.swan_contracts import SwanRunArgs, SwanWaveBoundary
    from trid3nt_server.agent.workflows.swan.wave_field.wave_field import (
        model_swan_wave_field,
    )

    boundary = SwanWaveBoundary(**boundary_kwargs) if boundary_kwargs else None
    kwargs: dict[str, Any] = dict(
        bbox=tuple(bbox),
        mode="stationary",
        n_dir=int(n_dir),
        n_freq=int(n_freq),
    )
    if boundary is not None:
        kwargs["boundary"] = boundary
    kwargs.update(overrides)
    run_args = SwanRunArgs(**kwargs)
    return await model_swan_wave_field(run_args, dem_uri=dem_uri)


# --------------------------------------------------------------------------- #
# Pure Vega-Lite chart specs (readable at the dock's 6.0x2.2in geometry).
# --------------------------------------------------------------------------- #
def multi_series_line_spec(
    rows: list[dict[str, Any]],
    *,
    x_field: str,
    y_field: str,
    color_field: str,
    x_title: str,
    y_title: str,
    title: str,
) -> dict[str, Any]:
    """A multi-series (color-grouped) line+point chart. Pure."""
    return {
        "$schema": VEGA_LITE_V5_SCHEMA,
        "title": title,
        "data": {"values": list(rows)},
        "mark": {"type": "line", "point": True},
        "encoding": {
            "x": {"field": x_field, "type": "ordinal", "title": x_title},
            "y": {"field": y_field, "type": "quantitative", "title": y_title},
            "color": {"field": color_field, "type": "nominal", "title": "metric"},
            "tooltip": [
                {"field": color_field, "type": "nominal"},
                {"field": x_field, "type": "ordinal"},
                {"field": y_field, "type": "quantitative", "format": ".4g"},
            ],
        },
    }


async def emit_chart_if_live(
    vega_lite_spec: dict[str, Any], *, title: str, caption: str
) -> bool:
    """Side-emit ``vega_lite_spec`` through the live pipeline emitter, if bound.

    Returns True when a chart was emitted. Offline (no bound emitter) it is a
    no-op returning False -- the numeric summary the template returns is the
    determinism-boundary source of truth regardless.
    """
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    emitter = current_emitter()
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return False
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

    try:
        payload = build_chart_payload(
            vega_lite_spec=vega_lite_spec, title=title, caption=caption
        )
        await emitter.emit_chart(payload)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("swan sweep chart emit failed: %s", exc)
        return False
