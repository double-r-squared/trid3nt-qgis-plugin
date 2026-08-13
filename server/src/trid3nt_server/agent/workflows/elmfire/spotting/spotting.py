"""Engine template ``elmfire_spot_fire_barrier_crossing`` - does wind-driven ember
spotting carry a wildfire ACROSS a barrier the contiguous front cannot cross?

A distinct question CLASS from surface spread and crown fire (per the capability-
naming rule): the headline is a barrier-JUMP - the cleanest spotting discriminant.
The template runs the SAME scenario TWICE (spotting OFF vs ON) and measures whether
the far (downwind) side of the barrier burns ONLY because of ember spotting. It has
TWO modes:

  - ``mode="real"`` (DEFAULT, the real-world demo): a REAL AOI in fire country
    where a REAL RIVER crosses the (E-W) wind axis. LANDFIRE fuels + a USGS 3DEP
    DEM (slope/aspect drive spread) are FETCHED over the caller's bbox - the river
    is the LANDFIRE water class (FBFM40 code 98), which renders NON-BURNABLE, so
    the contiguous surface front stops at the near bank. The river width is
    measured off the fetched grid; the head fire (upwind of the near bank) and the
    far-side burned area (downwind of the far bank) are split off the time-of-
    arrival raster within the river's cross-wind shadow. The honest physics is
    reported - the river may HOLD even with spotting at a realistic width, or the
    embers may cross; there is NO tuning to force a jump.

  - ``mode="verification"`` (the physics V&V tier): an
    ALL-CONSTANT flat grass deck carrying a single SYNTHETIC non-burnable strip
    (a fuel break, NB1/FBFM 91). It ASSERTS the clean discriminant - far-side
    burned area ~0 with spotting OFF and strictly > 0 with spotting ON - proving
    the ember-spotting mechanism in isolation. NOT a real-landscape event: a
    controlled bed for verifying the solver behaviour, kept alongside the real
    demo (delete-dont-disable does not apply to a still-valid V&V path).

ELMFIRE SPOTTING KNOB TRAP (verified against the baked binary,
``elmfire_spotting.f90::SET_SPOTTING_PARAMETERS``): whenever ``ENABLE_SPOTTING`` the
solver OVERWRITES the scalar spotting knobs from their ``_MIN/_MAX/_LO/_HI`` bounds,
and surface-fire spotting stays OFF unless ``GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT``
(default 0) is raised. So this template sets the BOUNDS (MIN==MAX for a deterministic
run), never the bare scalars. The folded spotting parameters - mean spotting distance
(the lognormal-distance model), critical spotting fireline intensity (the generation
gate), ember count and landing ignition probability - ride as this template's knobs.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from trid3nt_contracts.elmfire_contracts import (
    ELMFIRE_TOA_STYLE_PRESET,
    ElmfireRunArgs,
    ElmfireSensitivityLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.elmfire._template_card import TemplateCard
from trid3nt_server.agent.workflows.elmfire.fire_spread.fire_spread import (
    FireSpreadComposerError,
    _cleanup_dir,
    _download_elmfire_outputs,
    _publish_primary_layer,
)
from trid3nt_server.agent.workflows.elmfire.postprocess_elmfire import (
    PostprocessElmfireError,
    discover_elmfire_rasters,
    postprocess_elmfire,
    read_fire_raster,
)
from trid3nt_server.agent.workflows.elmfire.run_elmfire import (
    ElmfireWorkflowError,
    build_elmfire_deck,
    fetch_elmfire_inputs,
)
from trid3nt_server.agent.workflows.elmfire.sensitivity._sensitivity_common import (
    _dispatch_and_wait,
    cleanup_cases,
    publish_primary_from_out_dir,
    solve_constant_case,
)
from trid3nt_server.emission.pipeline_emitter import begin_substeps, current_emitter

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.elmfire.spotting.spotting"
)

__all__ = [
    "elmfire_spot_fire_barrier_crossing",
    "model_elmfire_spot_fire_barrier_crossing",
    "model_elmfire_river_barrier_crossing",
]

#: LANDFIRE FBFM40 code for open WATER - the non-burnable river barrier in the
#: real-data mode. Zero rate-of-spread; the contiguous surface front stops at it.
_FBFM_WATER_CODE: int = 98

#: Synthetic-deck centre - CA Sierra-foothill WUI fire country (geography
#: immaterial on a constant deck; a natural US fire-prone locale).
_CENTER_LON: float = -120.5
_CENTER_LAT: float = 38.8

#: A dry-grass surface fuel bed: GR2 (FBFM 102) - a fast, high-intensity head fire
#: that reliably launches embers on the verification deck.
_GRASS_FUEL_MODEL_GR2: int = 102

#: Fuel-break geometry (verification deck - LOUD labelled defaults, NOT user knobs):
#: a vertical NON-BURNABLE strip spanning the full N-S extent, placed ~1/3 across
#: the E-W domain so a strong head fire builds before hitting it. The ignition sits
#: ~10% in from the west so the head fire runs downwind into the break.
_BREAK_LO_FRAC: float = 0.33
_BREAK_HI_FRAC: float = 0.35
_IGN_FRAC_X: float = 0.10

#: The verification domain: long E-W (down-wind spotting runway) x short N-S.
_DOMAIN_KM_X: float = 12.0
_DOMAIN_KM_Y: float = 1.5

#: Lognormal spotting-distance shape defaults (baked-binary parametrization): the
#: normalized distance variance + the wind/intensity power-law exponents.
_NORMALIZED_SPOTTING_DIST_VARIANCE: float = 250.0
_SPOT_FLIN_EXP: float = 0.3
_SPOT_WS_EXP: float = 0.7

#: The 0:303 fuel-model index span of ELMFIRE's per-fuel spotting arrays (used to
#: broadcast a scalar critical-intensity gate across every fuel model via the
#: namelist ``N*value`` repeat form).
_FBFM_ARRAY_LEN: int = 304

#: Far-side burned area (km2) below which the barrier counts as NOT jumped - a small
#: floor absorbing at most a couple of edge cells, well under any real spot-fire.
_JUMP_FLOOR_KM2: float = 1.0e-3

#: Real-mode river-band tolerance: rows where the warped water class briefly breaks
#: (a warp-thinned bend) are bridged up to this many rows so one meandering river
#: reads as ONE cross-wind band rather than several.
_RIVER_BAND_ROW_GAP: int = 2

#: Minimum river run width (cells) counted as the river crossing in a row - skips a
#: lone warped water speck between the ignition and the true channel.
_RIVER_MIN_RUN_CELLS: int = 2


def _spotting_namelist(
    *,
    mean_spotting_distance_m: float,
    nembers: int,
    pign_pct: float,
    critical_spotting_intensity_kwm: float,
) -> dict[str, str]:
    """Build the deterministic ``&SPOTTING`` namelist dict (MIN==MAX bounds).

    The baked binary's ``SET_SPOTTING_PARAMETERS`` resolves each knob to
    ``_MIN + R1*(_MAX - _MIN)``, so MIN==MAX pins a deterministic value regardless of
    the internal draw; ``GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT`` must be raised or
    surface-fire spotting never launches. NEMBERS rides ``NEMBERS_MIN`` +
    ``NEMBERS_MAX_LO/HI``; PIGN the ``PIGN_MIN/MAX`` bounds."""
    extra: dict[str, str] = {
        "ENABLE_SPOTTING": ".TRUE.",
        "ENABLE_SURFACE_FIRE_SPOTTING": ".TRUE.",
        "USE_SUPERSEDED_SPOTTING": ".TRUE.",
        "SPOTTING_DISTRIBUTION_TYPE": "'LOGNORMAL'",
        "GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT_MIN": "100.0000",
        "GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT_MAX": "100.0000",
        "MEAN_SPOTTING_DIST_MIN": f"{float(mean_spotting_distance_m):.4f}",
        "MEAN_SPOTTING_DIST_MAX": f"{float(mean_spotting_distance_m):.4f}",
        "NORMALIZED_SPOTTING_DIST_VARIANCE_MIN": f"{_NORMALIZED_SPOTTING_DIST_VARIANCE:.4f}",
        "NORMALIZED_SPOTTING_DIST_VARIANCE_MAX": f"{_NORMALIZED_SPOTTING_DIST_VARIANCE:.4f}",
        "SPOT_FLIN_EXP_LO": f"{_SPOT_FLIN_EXP:.4f}",
        "SPOT_FLIN_EXP_HI": f"{_SPOT_FLIN_EXP:.4f}",
        "SPOT_WS_EXP_LO": f"{_SPOT_WS_EXP:.4f}",
        "SPOT_WS_EXP_HI": f"{_SPOT_WS_EXP:.4f}",
        "NEMBERS_MIN": f"{int(nembers):d}",
        "NEMBERS_MAX_LO": f"{int(nembers):d}",
        "NEMBERS_MAX_HI": f"{int(nembers):d}",
        "PIGN_MIN": f"{float(pign_pct):.4f}",
        "PIGN_MAX": f"{float(pign_pct):.4f}",
    }
    # CRITICAL_SPOTTING_FIRELINE_INTENSITY is a per-fuel (0:303) array; broadcast a
    # scalar gate across every fuel model with the namelist repeat form when > 0
    # (default 0 stays absent -> the binary default of 0, spotting always generates).
    if float(critical_spotting_intensity_kwm) > 0.0:
        extra["CRITICAL_SPOTTING_FIRELINE_INTENSITY"] = (
            f"{_FBFM_ARRAY_LEN}*{float(critical_spotting_intensity_kwm):.4f}"
        )
    return extra


TEMPLATE_CARD = TemplateCard(
    question=(
        "whether wind-driven ember spotting carries a wildfire across a river / road "
        "/ fuel break the contiguous front cannot cross (spotting OFF vs ON) - over "
        "REAL LANDFIRE fuels + terrain with a real river as the barrier (mode=real), "
        "or a controlled grass deck with a synthetic break (mode=verification)"
    ),
    required_inputs=["bbox", "ignition_lonlat (mode=real)"],
    knobs=(
        "mode (real/verification), mean_spotting_distance_m, "
        "critical_spotting_intensity_kwm, nembers, pign_pct, wind_speed_mph, "
        "wind_dir_deg, duration_hours, cellsize_m, fuel_moisture"
    ),
)

_METADATA = AtomicToolMetadata(
    name="elmfire_spot_fire_barrier_crossing",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="elmfire",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def elmfire_spot_fire_barrier_crossing(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    ignition_lonlat: tuple[float, float] | list[float] | None = None,
    mode: str = "real",
    mean_spotting_distance_m: float = 25.0,
    critical_spotting_intensity_kwm: float = 0.0,
    nembers: int = 20,
    pign_pct: float = 100.0,
    wind_speed_mph: float = 30.0,
    wind_dir_deg: float = 270.0,
    duration_hours: float = 5.0,
    cellsize_m: float = 30.0,
    fuel_model: int = _GRASS_FUEL_MODEL_GR2,
    fuel_moisture: str = "dry",
    compute_class: str = "standard",
    **_extra_ignored: Any,
) -> ElmfireSensitivityLayerURI | dict[str, Any]:
    """Does ember spotting make a wildfire JUMP a river / fuel break? (ELMFIRE)

    TWO modes. ``mode="real"`` (DEFAULT): a REAL wildfire over LANDFIRE 30 m fuels +
    a USGS 3DEP DEM (slope/aspect drive spread) on the caller's bbox, where a REAL
    RIVER (LANDFIRE water class, non-burnable) crosses the E-W wind axis; the far-
    side burned area is measured off the time-of-arrival raster within the river's
    cross-wind shadow (river width measured from the grid). Honest physics: the
    river may HOLD even with spotting - reported, never tuned to force a jump.
    ``mode="verification"``: an ALL-CONSTANT flat grass deck with ONE synthetic
    NON-BURNABLE strip, asserting the clean OFF~0 / ON>0 discriminant (physics V&V,
    no fetch, no bbox).

    Off-scope: plain wildfire spread over LANDFIRE fuels -> elmfire_fire_spread; the
    surface-to-crown transition -> elmfire_crown_fire_initiation_threshold_sweep.

    Use this when: the user asks whether a fire can jump a road / river / fuel break
    / firebreak via embers, how ember spotting spreads fire past a barrier, how far
    ahead of the front spot fires ignite, or how the spotting distance / ember count /
    ignition probability / generation-intensity threshold change spot-fire crossing.

    Params (mode=real):
        bbox: simulation AOI, EPSG:4326, CONUS-only, county-scale; a real river must
            cross the E-W wind axis inside it (a grass/shrub reach).
        ignition_lonlat: REQUIRED (lon, lat) UPWIND (west) of the river.
    Params (both):
        mode: "real" (default) or "verification".
        mean_spotting_distance_m: mean lognormal spotting distance knob (default 25;
            larger throws embers farther downwind).
        critical_spotting_intensity_kwm: fireline-intensity gate below which no embers
            generate (kW/m; default 0 = generate from every burning cell).
        nembers: embers cast per torching cell (default 20).
        pign_pct: probability (percent) a landed ember ignites (default 100).
        wind_speed_mph: constant wind, ELMFIRE 20 ft mph convention (default 30).
        wind_dir_deg: direction the wind blows FROM, met deg (default 270 = from the
            west, driving the head fire east into the river; must be ~E-W in real mode).
        duration_hours: burn duration (default 5).
        cellsize_m: computational cell size (default 30 = LANDFIRE native).
        fuel_model: the uniform burnable FBFM (verification mode only; default 102=GR2).
        fuel_moisture: "dry" (default), "moderate", or "moist".
        compute_class: solver compute class (default "standard").

    Returns:
        On success: ``ElmfireSensitivityLayerURI`` - the spotting-ON time-of-arrival
        COG, a two-point ``sweep`` (spotting OFF vs ON far-side burned area), and a
        ``summary`` carrying the barrier-jump scalars (in real mode: measured river
        width + the honest jumped/held verdict). An OFF-vs-ON chart is emitted. On
        failure: ``{"status": "error", "error_code", "error_message"}``. Not cached.
    """
    if int(nembers) < 1:
        return _err("FIRE_PARAMS_INVALID", "nembers must be >= 1")
    if not (0.0 < float(pign_pct) <= 100.0):
        return _err("FIRE_PARAMS_INVALID", "pign_pct must be in (0, 100]")
    if float(mean_spotting_distance_m) <= 0.0:
        return _err("FIRE_PARAMS_INVALID", "mean_spotting_distance_m must be > 0")
    if float(critical_spotting_intensity_kwm) < 0.0:
        return _err("FIRE_PARAMS_INVALID", "critical_spotting_intensity_kwm must be >= 0")

    mode = str(mode).lower().strip()
    if mode not in ("real", "verification"):
        return _err("FIRE_PARAMS_INVALID", f"mode must be 'real' or 'verification' (got {mode!r})")

    try:
        if mode == "verification":
            primary = await model_elmfire_spot_fire_barrier_crossing(
                mean_spotting_distance_m=float(mean_spotting_distance_m),
                critical_spotting_intensity_kwm=float(critical_spotting_intensity_kwm),
                nembers=int(nembers),
                pign_pct=float(pign_pct),
                wind_speed_mph=float(wind_speed_mph),
                wind_dir_deg=float(wind_dir_deg),
                duration_hours=float(duration_hours),
                cellsize_m=float(cellsize_m),
                fuel_model=int(fuel_model),
                fuel_moisture=str(fuel_moisture),
                compute_class=("small" if compute_class == "standard" else compute_class),
            )
        else:
            if ignition_lonlat is None:
                return _err(
                    "FIRE_IGNITION_REQUIRED",
                    "elmfire_spot_fire_barrier_crossing(mode='real') requires an "
                    "ignition point (ignition_lonlat=[lon, lat]) UPWIND of the river, "
                    "and it must come from the USER. Do NOT invent one: ask the user, "
                    "or call request_spatial_input(mode='point') so they click the "
                    "ignition on the map, then pass the returned coordinates.",
                )
            try:
                run_args = ElmfireRunArgs(
                    bbox=bbox,  # type: ignore[arg-type]
                    ignition_lonlat=ignition_lonlat,  # type: ignore[arg-type]
                    wind_speed_mph=float(wind_speed_mph),
                    wind_dir_deg=float(wind_dir_deg),
                    fuel_moisture=fuel_moisture,  # type: ignore[arg-type]
                    duration_hours=float(duration_hours),
                    cellsize_m=float(cellsize_m),
                )
            except Exception as exc:  # noqa: BLE001 - pydantic coercion
                return _err("FIRE_PARAMS_INVALID", f"invalid run arguments: {exc}")
            primary = await model_elmfire_river_barrier_crossing(
                run_args,
                mean_spotting_distance_m=float(mean_spotting_distance_m),
                critical_spotting_intensity_kwm=float(critical_spotting_intensity_kwm),
                nembers=int(nembers),
                pign_pct=float(pign_pct),
                compute_class=compute_class,
            )
        logger.info(
            "elmfire_spot_fire_barrier_crossing[%s] complete layer_id=%s summary=%s uri=%s",
            mode, primary.layer_id, primary.summary, primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (ElmfireWorkflowError, PostprocessElmfireError, FireSpreadComposerError) as exc:
        logger.warning(
            "elmfire_spot_fire_barrier_crossing[%s] failed: %s (%s)",
            mode, getattr(exc, "error_code", "?"), exc,
        )
        return _err(getattr(exc, "error_code", "FIRE_INTERNAL_ERROR"), str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("elmfire_spot_fire_barrier_crossing[%s] unexpected failure", mode)
        return _err("FIRE_INTERNAL_ERROR", str(exc))


def _err(code: str, msg: str) -> dict[str, Any]:
    return {"status": "error", "error_code": code, "error_message": msg}


# --------------------------------------------------------------------------- #
# Real-data river-barrier composer.
# --------------------------------------------------------------------------- #
def _river_runs(row_water: Any) -> list[tuple[int, int]]:
    """Contiguous ``(lo_col, hi_col)`` runs of water cells in one raster row. Pure."""
    import numpy as np

    cols = np.where(row_water)[0]
    if cols.size == 0:
        return []
    runs: list[tuple[int, int]] = []
    lo = prev = int(cols[0])
    for c in cols[1:]:
        c = int(c)
        if c == prev + 1:
            prev = c
        else:
            runs.append((lo, prev))
            lo = prev = c
    runs.append((lo, prev))
    return runs


def _contiguous_band(rows: list[int], seed_row: int, max_gap: int) -> list[int]:
    """The maximal run of river rows around ``seed_row`` bridging gaps <= max_gap.

    ``rows`` = the sorted rows that carry a river crossing. Returns the contiguous
    (allowing <=max_gap missing rows) block containing ``seed_row`` (or the block
    nearest to it when ``seed_row`` itself has no river). Pure."""
    if not rows:
        return []
    present = set(rows)
    if seed_row not in present:
        seed_row = min(rows, key=lambda r: abs(r - seed_row))
    lo = hi = seed_row
    while True:
        nxt = next((r for r in range(lo - 1, lo - max_gap - 2, -1) if r in present), None)
        if nxt is None:
            break
        lo = nxt
    while True:
        nxt = next((r for r in range(hi + 1, hi + max_gap + 2) if r in present), None)
        if nxt is None:
            break
        hi = nxt
    return [r for r in range(lo, hi + 1)]


def measure_river_split(
    toa_s: Any,
    fbfm_arr: Any,
    *,
    ign_rowcol: tuple[int, int],
    wind_dir_deg: float,
    cellsize_m: float,
    water_code: int = _FBFM_WATER_CODE,
) -> dict[str, float]:
    """Split burned cells into head (upwind) vs far-side (downwind of the river).

    The river is the WATER barrier (``water_code``) crossing the E-W wind axis.
    Downwind is +col (wind FROM the west) or -col (from the east). For each raster
    row in the river's contiguous cross-wind shadow around the ignition, the river
    crossing is the water run (>= ``_RIVER_MIN_RUN_CELLS`` wide) nearest DOWNWIND of
    the ignition column; head = burned cells upwind of its near bank, far-side =
    burned cells downwind of its far bank. Every scalar is measured off the ToA
    raster + the fetched fbfm grid (Invariant 1). Pure.

    Raises ``ValueError`` when the wind is not E-W-dominant, no river separates the
    ignition from downwind, or the ignition is not upwind of the river."""
    import numpy as np

    ny, nx = toa_s.shape
    ign_row, ign_col = int(ign_rowcol[0]), int(ign_rowcol[1])
    theta_to = math.radians((float(wind_dir_deg) + 180.0) % 360.0)
    east = math.sin(theta_to)   # +col component of the downwind flow
    north = math.cos(theta_to)  # +y (map north) component; +north => -row
    if abs(east) < abs(north):
        raise ValueError(
            "river-barrier real mode expects an E-W-dominant wind so a N-S river is "
            f"the cross-wind barrier (wind_from={wind_dir_deg} deg gives N-S flow); "
            "orient the scenario wind ~east/west."
        )
    downwind_sign = 1 if east > 0 else -1
    water = fbfm_arr == int(water_code)
    burned = np.isfinite(toa_s)
    cell_km2 = (float(cellsize_m) * float(cellsize_m)) / 1.0e6

    near: dict[int, int] = {}
    far: dict[int, int] = {}
    width: dict[int, int] = {}
    for r in range(ny):
        runs = [
            (lo, hi) for (lo, hi) in _river_runs(water[r])
            if (hi - lo + 1) >= _RIVER_MIN_RUN_CELLS
        ]
        if downwind_sign > 0:
            runs = [(lo, hi) for (lo, hi) in runs if lo > ign_col]
            runs.sort(key=lambda rn: rn[0])  # nearest downwind first
        else:
            runs = [(lo, hi) for (lo, hi) in runs if hi < ign_col]
            runs.sort(key=lambda rn: -rn[1])
        if not runs:
            continue
        lo, hi = runs[0]
        near[r] = lo if downwind_sign > 0 else hi
        far[r] = hi if downwind_sign > 0 else lo
        width[r] = hi - lo + 1
    if not near:
        raise ValueError(
            "no river (LANDFIRE water class) cells found downwind of the ignition - "
            "pick a bbox where a river crosses the wind axis downwind of the ignition."
        )

    band = _contiguous_band(sorted(near), ign_row, _RIVER_BAND_ROW_GAP)
    band = [r for r in band if r in near]
    if len(band) < 3:
        raise ValueError(
            "the river does not form a contiguous cross-wind band near the ignition "
            f"(only {len(band)} river rows around row {ign_row})."
        )

    head_cells = 0
    far_cells = 0
    for r in band:
        if downwind_sign > 0:
            head_cells += int(burned[r, :near[r]].sum())
            far_cells += int(burned[r, far[r] + 1:].sum())
        else:
            head_cells += int(burned[r, near[r] + 1:].sum())
            far_cells += int(burned[r, :far[r]].sum())

    widths = [width[r] for r in band]
    return {
        "head_cells": float(head_cells),
        "far_cells": float(far_cells),
        "head_area_km2": float(head_cells) * cell_km2,
        "far_area_km2": float(far_cells) * cell_km2,
        "river_width_m": float(np.median(widths)) * float(cellsize_m),
        "river_width_min_m": float(np.min(widths)) * float(cellsize_m),
        "river_band_rows": float(len(band)),
        "river_band_coverage": float((max(band) - min(band) + 1)) / float(ny),
        "downwind_sign": float(downwind_sign),
    }


def _read_fbfm_grid(deck_dir: str, epsg: int) -> Any:
    """Read the warped ``fbfm40`` grid from a built deck's inputs/ dir. Sync."""
    import numpy as np
    import rasterio

    path = f"{deck_dir}/inputs/fbfm40.tif"
    with rasterio.open(path) as ds:
        arr = ds.read(1)
    return np.asarray(arr)


async def _solve_real_case(
    run_args: ElmfireRunArgs,
    inputs: dict[str, str],
    *,
    spotting_extra: dict[str, str] | None,
    compute_class: str,
    emitter: Any,
    step_label: str,
) -> tuple[str, str, str, int, bool]:
    """Build ONE real-data deck (spotting OFF or ON) from ALREADY-fetched inputs,
    dispatch + wait, download outputs. Returns
    ``(out_dir, run_id, deck_dir, epsg, out_is_temp)``.

    Reuses the fetched LANDFIRE/DEM warp identically for OFF vs ON - only the
    ``&SPOTTING`` namelist group differs, so the barrier and terrain are the SAME
    landscape across the pair. The out_dir + deck_dir are KEPT for the caller to
    read rasters / measure the river; the caller cleans the temp deck_dir and any
    TEMP out_dir (a LOCAL rundir - ``out_is_temp=False`` - is the run's artifact
    dir and is never deleted here)."""
    import tempfile

    from trid3nt_server.emission.pipeline_emitter import substep

    deck_dir = tempfile.mkdtemp(prefix="elmfire-river-deck-")
    async with substep(emitter, step_label):
        deck_manifest = await asyncio.to_thread(
            build_elmfire_deck, run_args, inputs, deck_dir, spotting_extra=spotting_extra
        )
    grid = deck_manifest.get("grid") or {}
    epsg = int(grid.get("epsg", 5070))

    run_id = await _dispatch_and_wait(
        deck_dir=deck_dir,
        deck_manifest=deck_manifest,
        run_args=run_args,
        compute_class=compute_class,
        emitter=emitter,
        run_id=None,
    )
    out_dir, out_is_temp = await asyncio.to_thread(_download_elmfire_outputs, run_id)
    return out_dir, run_id, deck_dir, epsg, out_is_temp


async def model_elmfire_river_barrier_crossing(
    run_args: ElmfireRunArgs,
    *,
    mean_spotting_distance_m: float,
    critical_spotting_intensity_kwm: float,
    nembers: int,
    pign_pct: float,
    compute_class: str = "standard",
) -> ElmfireSensitivityLayerURI:
    """The REAL-DATA river-barrier spotting composer (OFF vs ON, honest verdict).

    Fetches LANDFIRE fuels + a 3DEP DEM ONCE over the AOI, surfaces the fuels + DEM
    inputs (ADR 0231) so the river corridor is visible, builds + solves the deck
    TWICE (spotting OFF then ON on the SAME warp), measures the river width + the
    far-side burned area off the ToA raster within the river's cross-wind shadow,
    publishes the spotting-ON ToA COG, and emits the OFF-vs-ON chart. Reports the
    honest physics - the river HOLDING even with spotting is a valid finding; there
    is NO assertion forcing a jump."""
    emitter = current_emitter()
    bbox = tuple(run_args.bbox)
    duration_s = float(run_args.duration_hours) * 3600.0

    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("river-barrier zoom-to failed: %s", exc)

    # fetch inputs (1) -> deck OFF (2) -> deck ON (3).
    begin_substeps(emitter, 3)

    from trid3nt_server.emission.pipeline_emitter import substep

    async with substep(emitter, "fetch_elmfire_inputs"):
        inputs = await asyncio.to_thread(fetch_elmfire_inputs, bbox)

    # ADR 0231: surface the fetched fuels (river corridor visible as the water class)
    # + the DEM (terrain that drives slope/aspect spread) as role=context inputs.
    from trid3nt_contracts import new_ulid
    from trid3nt_server.emission.layer_uri_emit import publish_raster_input_cog

    _fuels_uri = inputs.get("fbfm40")
    if _fuels_uri and str(_fuels_uri).startswith("s3://"):
        await publish_raster_input_cog(
            emitter, cog_uri=str(_fuels_uri),
            layer_id=f"input-landfire-fuels-{new_ulid()}",
            name="Input: LANDFIRE fuel model (FBFM40, LF2022 30 m) - river = water class",
            style_preset="categorical_landcover", role="context",
        )
    _dem_uri = inputs.get("dem")
    if _dem_uri and str(_dem_uri).startswith("s3://"):
        await publish_raster_input_cog(
            emitter, cog_uri=str(_dem_uri),
            layer_id=f"input-dem-{new_ulid()}",
            name="Input: DEM (USGS 3DEP) - terrain driving slope/aspect spread",
            style_preset="continuous_dem", role="context",
        )

    spotting_extra = _spotting_namelist(
        mean_spotting_distance_m=mean_spotting_distance_m,
        nembers=nembers,
        pign_pct=pign_pct,
        critical_spotting_intensity_kwm=critical_spotting_intensity_kwm,
    )

    scratch_dirs: list[str] = []
    try:
        off_out, off_run, off_deck, epsg, off_tmp = await _solve_real_case(
            run_args, inputs, spotting_extra=None,
            compute_class=compute_class, emitter=emitter,
            step_label="build_elmfire_deck_off",
        )
        scratch_dirs.append(off_deck)
        if off_tmp:
            scratch_dirs.append(off_out)
        on_out, on_run, on_deck, _epsg, on_tmp = await _solve_real_case(
            run_args, inputs, spotting_extra=spotting_extra,
            compute_class=compute_class, emitter=emitter,
            step_label="build_elmfire_deck_on",
        )
        scratch_dirs.append(on_deck)
        if on_tmp:
            scratch_dirs.append(on_out)

        # Read the ToA rasters + the (identical) warped fbfm grid; locate the ignition
        # in grid coordinates from the deck manifest's projected ignition xy.
        off_toa_path = discover_elmfire_rasters(off_out).get("time_of_arrival")
        on_toa_path = discover_elmfire_rasters(on_out).get("time_of_arrival")
        if off_toa_path is None or on_toa_path is None:
            raise FireSpreadComposerError(
                "ELMFIRE_NO_LAYERS", "OFF/ON run produced no time_of_arrival raster"
            )
        off_toa, transform, _crs, cellsize_m = await asyncio.to_thread(
            read_fire_raster, off_toa_path, epsg=epsg
        )
        on_toa, _t, _c, _cs = await asyncio.to_thread(read_fire_raster, on_toa_path, epsg=epsg)
        fbfm = await asyncio.to_thread(_read_fbfm_grid, on_deck, epsg)

        import json as _json
        from pathlib import Path as _Path

        manifest = _json.loads((_Path(on_deck) / "deck_manifest.json").read_text())
        ign_xy = (manifest.get("ignitions_domain_xy") or [{}])[0]
        inv = ~transform
        ign_col_f, ign_row_f = inv * (float(ign_xy.get("x", 0.0)), float(ign_xy.get("y", 0.0)))
        ign_rowcol = (int(round(ign_row_f)), int(round(ign_col_f)))

        off_split = measure_river_split(
            off_toa, fbfm, ign_rowcol=ign_rowcol,
            wind_dir_deg=float(run_args.wind_dir_deg), cellsize_m=float(cellsize_m),
        )
        on_split = measure_river_split(
            on_toa, fbfm, ign_rowcol=ign_rowcol,
            wind_dir_deg=float(run_args.wind_dir_deg), cellsize_m=float(cellsize_m),
        )
        off_far = off_split["far_area_km2"]
        on_far = on_split["far_area_km2"]
        river_width_m = on_split["river_width_m"]
        head_km2 = on_split["head_area_km2"]

        if head_km2 <= 0.0:
            raise FireSpreadComposerError(
                "ELMFIRE_NO_SPREAD",
                "the head fire did not reach the river (0 burned cells upwind of the "
                "near bank) - the ignition may be too far from the river, the wind too "
                "weak, or the fuel non-burnable; nothing to test for a barrier jump.",
            )

        # HONEST verdict - NO assertion forcing a jump (NATE ruling: the river holding
        # is a valid finding). break_jumped only when embers cleared a river the
        # contiguous front could not.
        jumped = bool(on_far > _JUMP_FLOOR_KM2 >= off_far)
        held = bool(on_far <= _JUMP_FLOOR_KM2)
        off_leaks = bool(off_far > _JUMP_FLOOR_KM2)
        logger.info(
            "river barrier-jump: width=%.0fm  OFF far=%.4f km2  ON far=%.4f km2  "
            "head=%.3f km2  jumped=%s held=%s off_leaks=%s band_rows=%d cov=%.2f",
            river_width_m, off_far, on_far, head_km2, jumped, held, off_leaks,
            int(on_split["river_band_rows"]), on_split["river_band_coverage"],
        )

        # Publish the spotting-ON ToA COG (the far-side spot fire, if any) as primary.
        on_case = _RealCase(out_dir=on_out, run_id=on_run, epsg=epsg)
        base = await asyncio.to_thread(
            publish_primary_from_out_dir,
            on_case, bbox=bbox, duration_s=duration_s,
            ignition_lonlat=tuple(run_args.ignition_lonlat),
        )
    finally:
        for d in scratch_dirs:
            _cleanup_dir(d)

    verdict = (
        "jumped" if jumped else ("held" if held else "inconclusive")
    )
    sweep = [
        {"x": 0.0, "y": off_far},  # spotting OFF
        {"x": 1.0, "y": on_far},   # spotting ON
    ]
    summary = {
        "far_side_area_spotting_off_km2": off_far,
        "far_side_area_spotting_on_km2": on_far,
        "head_fire_area_km2": head_km2,
        "far_side_spot_cells_on": on_split["far_cells"],
        "river_width_m": river_width_m,
        "river_width_min_m": on_split["river_width_min_m"],
        "river_band_rows": on_split["river_band_rows"],
        "river_band_coverage": on_split["river_band_coverage"],
        "mean_spotting_distance_m": float(mean_spotting_distance_m),
        "nembers": float(int(nembers)),
        "pign_pct": float(pign_pct),
        "critical_spotting_intensity_kwm": float(critical_spotting_intensity_kwm),
        "fixed_wind_mph": float(run_args.wind_speed_mph),
        "break_jumped": 1.0 if jumped else 0.0,
        "off_side_leaks": 1.0 if off_leaks else 0.0,
    }

    primary = ElmfireSensitivityLayerURI(
        layer_id=base.layer_id,
        name=f"Fire arrival time (ember spotting across a real river - {verdict})",
        layer_type=base.layer_type,
        uri=base.uri,
        style_preset=base.style_preset or ELMFIRE_TOA_STYLE_PRESET,
        role=base.role,
        bbox=base.bbox,
        burned_area_km2=base.burned_area_km2,
        fire_arrival_max_hr=base.fire_arrival_max_hr,
        max_flame_length_m=base.max_flame_length_m,
        max_spread_rate_m_min=base.max_spread_rate_m_min,
        duration_hours=base.duration_hours,
        ignition_lonlat=base.ignition_lonlat,
        swept_param="spotting_enabled",
        swept_units="off(0)/on(1)",
        response_metric="far_side_burned_area_km2",
        response_units="km2",
        sweep=sweep,
        summary=summary,
    )

    await _maybe_emit_chart(
        emitter, off_far, on_far, primary.uri, river_width_m=river_width_m, verdict=verdict
    )
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("river-barrier authoritative zoom-to failed: %s", exc)

    logger.info(
        "model_elmfire_river_barrier_crossing complete verdict=%s summary=%s uri=%s",
        verdict, summary, primary.uri,
    )
    return primary


class _RealCase:
    """Minimal ``publish_primary_from_out_dir`` case shim (out_dir/run_id/epsg)."""

    def __init__(self, *, out_dir: str, run_id: str, epsg: int) -> None:
        self.out_dir = out_dir
        self.run_id = run_id
        self.epsg = epsg


# --------------------------------------------------------------------------- #
# Verification (synthetic constant-deck) composer - the physics V&V path.
# --------------------------------------------------------------------------- #
async def model_elmfire_spot_fire_barrier_crossing(
    *,
    mean_spotting_distance_m: float,
    critical_spotting_intensity_kwm: float,
    nembers: int,
    pign_pct: float,
    wind_speed_mph: float,
    wind_dir_deg: float,
    duration_hours: float,
    cellsize_m: float,
    fuel_model: int,
    fuel_moisture: str,
    compute_class: str = "small",
) -> ElmfireSensitivityLayerURI:
    """Compose the VERIFICATION (synthetic) spotting barrier-jump OFF-vs-ON pair.

    An ALL-CONSTANT flat grass deck with ONE synthetic non-burnable strip; ASSERTS
    the clean discriminant (far-side ~0 OFF, > 0 ON). NOT a real-landscape event -
    the controlled bed that proves the ember-spotting mechanism in isolation."""
    emitter = current_emitter()
    duration_s = float(duration_hours) * 3600.0

    half_deg_lat = (_DOMAIN_KM_Y * 1000.0 / 2.0) / 111_320.0
    half_deg_lon = (_DOMAIN_KM_X * 1000.0 / 2.0) / (
        111_320.0 * max(math.cos(math.radians(_CENTER_LAT)), 1e-6)
    )
    bbox = (
        _CENTER_LON - half_deg_lon, _CENTER_LAT - half_deg_lat,
        _CENTER_LON + half_deg_lon, _CENTER_LAT + half_deg_lat,
    )
    ign_lon = _CENTER_LON - half_deg_lon + _IGN_FRAC_X * (2.0 * half_deg_lon)
    ignition = (ign_lon, _CENTER_LAT)
    fuel_break = {
        "axis": "x",
        "lo_frac": _BREAK_LO_FRAC,
        "hi_frac": _BREAK_HI_FRAC,
        "fuel_model": 91,
    }

    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("spotting zoom-to failed: %s", exc)

    begin_substeps(emitter, 3)

    def _mk_run_args() -> ElmfireRunArgs:
        return ElmfireRunArgs(
            bbox=bbox,  # type: ignore[arg-type]
            ignition_lonlat=ignition,  # type: ignore[arg-type]
            wind_speed_mph=float(wind_speed_mph),
            wind_dir_deg=float(wind_dir_deg),
            fuel_moisture=fuel_moisture,  # type: ignore[arg-type]
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
        )

    spotting_extra = _spotting_namelist(
        mean_spotting_distance_m=mean_spotting_distance_m,
        nembers=nembers,
        pign_pct=pign_pct,
        critical_spotting_intensity_kwm=critical_spotting_intensity_kwm,
    )

    cases = []
    try:
        off = await solve_constant_case(
            _mk_run_args(), knob_value=0.0, fuel_model=int(fuel_model),
            fuel_break=fuel_break, spotting_extra=None,
            compute_class=compute_class, emitter=emitter,
            step_label="build_elmfire_deck_off",
        )
        cases.append(off)
        on = await solve_constant_case(
            _mk_run_args(), knob_value=1.0, fuel_model=int(fuel_model),
            fuel_break=fuel_break, spotting_extra=spotting_extra,
            compute_class=compute_class, emitter=emitter,
            step_label="build_elmfire_deck_on",
        )
        cases.append(on)

        off_east = float(off.extras.get("east_of_break_km2", 0.0))
        on_east = float(on.extras.get("east_of_break_km2", 0.0))
        logger.info(
            "verification spotting barrier-jump: OFF east=%.4f km2  ON east=%.4f km2",
            off_east, on_east,
        )
        if not (off_east <= _JUMP_FLOOR_KM2 < on_east):
            raise FireSpreadComposerError(
                "ELMFIRE_SPOTTING_NO_DISCRIMINANT",
                "spotting barrier-jump not demonstrated: expected far-side burned "
                f"area ~0 with spotting OFF (got {off_east:.4f} km2) and > "
                f"{_JUMP_FLOOR_KM2} km2 with spotting ON (got {on_east:.4f} km2). "
                "The break may be leaking, or the embers not clearing it.",
            )

        base = await asyncio.to_thread(
            publish_primary_from_out_dir,
            on, bbox=bbox, duration_s=duration_s, ignition_lonlat=ignition,
        )
    finally:
        cleanup_cases(cases, keep_out_dir=None)

    sweep = [
        {"x": 0.0, "y": off_east},
        {"x": 1.0, "y": on_east},
    ]
    summary = {
        "far_side_area_spotting_off_km2": off_east,
        "far_side_area_spotting_on_km2": on_east,
        "head_fire_area_km2": float(on.extras.get("west_of_break_km2", 0.0)),
        "far_side_spot_cells_on": float(on.extras.get("east_of_break_cells", 0.0)),
        "mean_spotting_distance_m": float(mean_spotting_distance_m),
        "nembers": float(int(nembers)),
        "pign_pct": float(pign_pct),
        "critical_spotting_intensity_kwm": float(critical_spotting_intensity_kwm),
        "fixed_wind_mph": float(wind_speed_mph),
        "break_jumped": 1.0,  # asserted above
    }

    primary = ElmfireSensitivityLayerURI(
        layer_id=base.layer_id,
        name="Fire arrival time (ember spotting across a fuel break - verification)",
        layer_type=base.layer_type,
        uri=base.uri,
        style_preset=base.style_preset or ELMFIRE_TOA_STYLE_PRESET,
        role=base.role,
        bbox=base.bbox,
        burned_area_km2=base.burned_area_km2,
        fire_arrival_max_hr=base.fire_arrival_max_hr,
        max_flame_length_m=base.max_flame_length_m,
        max_spread_rate_m_min=base.max_spread_rate_m_min,
        duration_hours=base.duration_hours,
        ignition_lonlat=base.ignition_lonlat,
        swept_param="spotting_enabled",
        swept_units="off(0)/on(1)",
        response_metric="far_side_burned_area_km2",
        response_units="km2",
        sweep=sweep,
        summary=summary,
    )

    await _maybe_emit_chart(emitter, off_east, on_east, primary.uri)
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("spotting authoritative zoom-to failed: %s", exc)

    logger.info(
        "model_elmfire_spot_fire_barrier_crossing complete summary=%s uri=%s",
        summary, primary.uri,
    )
    return primary


async def _maybe_emit_chart(
    emitter: Any,
    off_east: float,
    on_east: float,
    source_uri: str,
    *,
    river_width_m: float | None = None,
    verdict: str | None = None,
) -> None:
    """Emit the OFF-vs-ON far-side-burned-area comparison bar chart."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    if river_width_m is not None:
        title = (
            f"Does the fire jump the river? (~{river_width_m:.0f} m wide) - "
            f"far-side burned area, spotting OFF vs ON [{verdict}]"
        )
        caption = (
            f"Burned area on the FAR (downwind) side of a real river ~{river_width_m:.0f} m "
            "wide (LANDFIRE water class, non-burnable), within its cross-wind shadow. "
            "With spotting OFF the contiguous head fire stops at the near bank; with "
            "spotting ON lofted embers may clear the river and ignite the far bank. "
            "The honest physics is reported - the river can HOLD even with spotting."
        )
    else:
        title = "Does the fire jump the break? Far-side burned area, spotting OFF vs ON"
        caption = (
            "Burned area on the FAR (downwind) side of a non-burnable fuel break "
            "(verification deck). With spotting OFF the contiguous head fire stops at "
            "the break (~0 far-side area); with spotting ON lofted embers clear the "
            "break and ignite spot fires beyond it - the fire jumps ONLY via spotting."
        )
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": [
            {"case": "spotting OFF", "area": off_east},
            {"case": "spotting ON", "area": on_east},
        ]},
        "mark": {"type": "bar", "color": "#d1495b"},
        "encoding": {
            "x": {"field": "case", "type": "nominal", "title": None,
                  "sort": ["spotting OFF", "spotting ON"]},
            "y": {"field": "area", "type": "quantitative",
                  "title": "burned area beyond the barrier (km2)"},
            "tooltip": [
                {"field": "case", "type": "nominal"},
                {"field": "area", "type": "quantitative", "format": ".3g"},
            ],
        },
        "title": title,
    }
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Ember spotting across a barrier",
        caption=caption,
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("spotting chart emit failed: %s", exc)
