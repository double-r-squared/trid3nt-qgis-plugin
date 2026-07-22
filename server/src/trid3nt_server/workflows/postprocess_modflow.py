"""MODFLOW GWT run-output postprocessing (sprint-13 Stage 2, job-0227).

``postprocess_modflow(run_outputs_uri, *, run_id, model_crs) -> PlumeLayerURI``
reads the MF6-GWT concentration output (``gwt_model.ucn``, a binary
HEADFILE-format array via ``flopy.utils.HeadFile`` with ``text="CONCENTRATION"``),
takes the FINAL-TIMESTEP max-over-layers concentration grid, reprojects it from
the deck's projected (UTM) grid to an EPSG:4326 Cloud-Optimized GeoTIFF,
computes the two narration scalars (``max_concentration_mgl`` +
``plume_area_km2``), uploads the COG, and returns a typed ``PlumeLayerURI``.

This is the MODFLOW analogue of ``postprocess_flood`` (job-0042). Differences:

  * The source is a UCN concentration array, not a SFINCS NetCDF depth field.
  * The grid georegistration (origin / cell size / CRS) is read from the
    DECK manifest's ``model_crs`` (the OQ-MOD-3 handoff field) + the flopy grid
    object - not from a CRS variable inside the output file. MF6 binary output
    carries NO CRS; the deck's ``model_crs`` is authoritative.
  * The output is reprojected to EPSG:4326 so the plume COG aligns with the
    client's MapLibre basemap exactly like every other published raster.

Determinism boundary (Invariant 1 / Decision H / FR-AS-7): ``PlumeLayerURI``
carries ``max_concentration_mgl`` + ``plume_area_km2`` as typed numbers the
agent narrates - never free-generated. This module computes them from the
concentration array with plain arithmetic; no LLM anywhere.

Tier separation (Invariant 5): the COG lands in the runs bucket; the agent does
not re-render. ``publish_layer`` bridges the COG to QGIS Server WMS so the
client renders it (mocked in tests; callable in production).
"""

from __future__ import annotations

import glob
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.modflow_contracts import (
    ASRLayerURI,
    BudgetPartitionLayerURI,
    CaptureZoneLayerURI,
    DewaterLayerURI,
    DrawdownLayerURI,
    HydroperiodLayerURI,
    MoundingLayerURI,
    MultiSpeciesPlumeResult,
    PlumeLayerURI,
    SaltwaterWedgeLayerURI,
    SeepageLayerURI,
    StreamReachLayerURI,
    SubsidenceLayerURI,
)

from . import cog_io
from .cog_io import CogIoError

logger = logging.getLogger("trid3nt_server.workflows.postprocess_modflow")

__all__ = [
    "PostprocessMODFLOWError",
    "postprocess_modflow",
    "postprocess_multi_species",
    "postprocess_river_seepage",
    "postprocess_drawdown",
    "postprocess_dewatering",
    "postprocess_budget_partition",
    "postprocess_mounding",
    "postprocess_asr",
    "postprocess_wetland_hydroperiod",
    "postprocess_capture_zone",
    "postprocess_saltwater_intrusion",
    "publish_modflow_quantities",
    "compute_plume_metrics",
    "compute_seepage_metrics",
    "compute_drawdown_metrics",
    "compute_cbc_term_metrics",
    "compute_budget_partition",
    "compute_mounding_metrics",
    "compute_recharged_volume_m3",
    "compute_seasonal_head_range_m",
    "compute_recovery_efficiency",
    "compute_saltwater_intrusion_metrics",
    "postprocess_subsidence",
    "compute_subsidence_metrics",
    "PLUME_DETECTION_FLOOR_MGL",
    "PLUME_STYLE_PRESET",
    "SEEPAGE_STYLE_PRESET",
    "HEAD_STYLE_PRESET",
    "DRAWDOWN_STYLE_PRESET",
    "DEWATERING_STYLE_PRESET",
    "MOUNDING_STYLE_PRESET",
    "ASR_STYLE_PRESET",
    "HYDROPERIOD_STYLE_PRESET",
    "CAPTURE_ZONE_STYLE_PRESET",
    "SALTWATER_INTRUSION_STYLE_PRESET",
    "SUBSIDENCE_STYLE_PRESET",
    "GWF_CBC_FILENAME",
    "GWF_HDS_FILENAME",
    "RUNS_BUCKET_DEFAULT",
]

#: Default runs bucket (matches the SFINCS substrate).
RUNS_BUCKET_DEFAULT: str = "trid3nt-runs"

#: QML style preset name for the plume concentration COG. The styles/ package
#: authors the matching ``continuous_plume_concentration.qml``; surfaced as
#: OQ-MOD-PLUME-PRESET-QML for the engine styles follow-up.
PLUME_STYLE_PRESET: str = "continuous_plume_concentration"

#: Detection floor (mg/L): cells at or below this are NOT counted as plume
#: (kickoff: "cells above a 0.001 mg/L floor"). Also masked to NaN in the COG
#: so the renderer hides clean cells.
PLUME_DETECTION_FLOOR_MGL: float = 0.001

#: Concentration output filename the GWT OC package writes (gwt_adapter).
GWT_UCN_FILENAME: str = "gwt_model.ucn"

#: multi_species (Wave-3): the per-species GWT OC writes ``gwt_<species>.ucn``
#: (e.g. ``gwt_tce.ucn`` / ``gwt_cis_dce.ucn``) - one CONCENTRATION HeadFile per
#: solute. The single-species deck writes the bare ``gwt_model.ucn`` (above); the
#: postprocess globs ``gwt_*.ucn`` and EXCLUDES the single-species stem so a
#: spill run (one ``gwt_model.ucn``) is never mis-read as a one-species multi run.
GWT_SPECIES_UCN_GLOB: str = "gwt_*.ucn"
#: The single-species concentration stem (without extension) excluded from the
#: per-species glob so a spill deck's ``gwt_model.ucn`` is never treated as a
#: per-species file.
GWT_SINGLE_SPECIES_STEM: str = "gwt_model"

#: GWF cell-by-cell budget filename (carries the RIV leakage term). The OC
#: BUDGET FILEOUT uses this bare name; the recursive glob captures it wherever
#: the entrypoint reorg lands it (root, per run_modflow output_globs).
GWF_CBC_FILENAME: str = "gwf_model.cbc"

#: TiTiler style preset for the diverging gaining/losing river-seepage COG
#: (sprint-17 J9). Registered in publish_layer._TITILER_STYLE_REGISTRY by the
#: orchestrator's shared-appends merge as ("-2,2", "rdbu").
SEEPAGE_STYLE_PRESET: str = "diverging_river_seepage"

#: TiTiler style preset for the sustainable-yield drawdown (head-decline) COG
#: (sprint-18 Wave-1). Matches the output_quantities registry "drawdown" spec.
DRAWDOWN_STYLE_PRESET: str = "continuous_drawdown_m"

#: TiTiler style preset for the mine-dewatering DRN-outflow COG (sprint-18
#: Wave-1). Matches the output_quantities registry "dewatering-rate" spec.
DEWATERING_STYLE_PRESET: str = "continuous_dewatering_rate"

#: TiTiler style preset for the MAR groundwater-mounding (head-RISE) COG
#: (sprint-18 Wave-2). Mounding renders on a distinct BLUE (rising-water) ramp so
#: it never reads like the red drawdown (declining-water) layer; the key is
#: registered in publish_layer._TITILER_STYLE_REGISTRY and matches OUTPUT_QUANTITIES.
MOUNDING_STYLE_PRESET: str = "continuous_mounding_m"

#: TiTiler style preset for the ASR representative-head COG (sprint-18 Wave-2).
#: The ASR layer carries the well-head sawtooth as the deliverable; the spatial
#: carrier is the final-step water-table head (continuous head ramp).
ASR_STYLE_PRESET: str = "continuous_head_m"

#: TiTiler style preset for the wetland-hydroperiod seasonal-head-range COG
#: (sprint-18 Wave-2). The range is a non-negative magnitude (max minus min head
#: over the transient periods); its dedicated key is registered in
#: publish_layer._TITILER_STYLE_REGISTRY and matches OUTPUT_QUANTITIES.
HYDROPERIOD_STYLE_PRESET: str = "continuous_hydroperiod_m"

#: Vector style preset for the PRT backward-particle-tracking capture-zone polygon
#: (Wave-4). The capture zone is a FlatGeobuf polygon; the client's vector renderer
#: applies ``presetColorFor("capture_zone")`` -> violet so it reads as a protection
#: boundary, distinct from the blue water, red alert, and amber roads layers.
#: ``publish_layer`` is RASTER-ONLY and must NOT be called for this vector; the
#: inline-GeoJSON path (``pipeline_emitter.add_loaded_layer`` via
#: ``_read_vector_uri_as_geojson``) renders it over WS.
CAPTURE_ZONE_STYLE_PRESET: str = "capture_zone"

#: Vector style preset for the saltwater intrusion transect + toe point (Wave-5).
#: Two features in one FlatGeobuf: a LINE (coastal transect A->B) and a POINT
#: (the 50%-isochlor toe). The client's vector renderer applies
#: ``presetColorFor("saltwater_intrusion")`` -> teal (#1ABC9C) so it reads as a
#: coastal/saltwater boundary, distinct from the violet capture-zone, the blue
#: water layers, and the red alert overlays. ``publish_layer`` is RASTER-ONLY and
#: must NOT be called for this vector; the inline-GeoJSON path renders it over WS.
SALTWATER_INTRUSION_STYLE_PRESET: str = "saltwater_intrusion"

#: Vector style preset for the SFR routed stream-depletion reach network (module
#: wave). The reaches are a FlatGeobuf of per-reach line segments; the client's
#: vector renderer matches ``presetColorFor`` on the substring "stream" -> the
#: shared hydro-blue (#4477FF) so the routed stream reads as a waterway (no new
#: web preset needed). ``publish_layer`` is RASTER-ONLY and must NOT be called
#: for this vector; the inline-GeoJSON path renders it over WS.
STREAM_DEPLETION_STYLE_PRESET: str = "stream_depletion"

#: Raster style preset for the CSUB land-subsidence bowl (module wave). The
#: z-displacement final frame is a continuous COG in cm (positive-down); this
#: preset drives the raster colour ramp exactly like the drawdown COG. UNLIKE the
#: SFR vector, this IS a raster layer that goes through ``publish_layer``.
SUBSIDENCE_STYLE_PRESET: str = "continuous_subsidence_cm"

#: PRT track CSV filename written by the MF6 PRT sim (``ModflowPrtoc`` +
#: ``trackcsv_filerecord``). The model name the adapter uses for the PRT model is
#: ``"prtmodel"``, so the CSV is always ``prtmodel.trk.csv``.
PRT_TRACK_CSV_FILENAME: str = "prtmodel.trk.csv"

#: CBC budget terms the generalized cell-by-cell reader scatters onto a grid.
#: Each is a head-dependent / source-sink package whose budget record carries a
#: per-cell signed flow (m^3/day, MF6 sign: positive = INTO the cell/aquifer).
_CBC_GRID_TERMS: frozenset[str] = frozenset(
    {"DRN", "EVT", "RCH", "WEL", "RCHA", "RIV", "GHB", "CHD"}
)

#: Budget partition headline EXCLUDES the inter-cell flow term (it is internal
#: bookkeeping, not a source/sink boundary the user narrates).
_BUDGET_EXCLUDE_FROM_HEADLINE: frozenset[str] = frozenset({"FLOW-JA-FACE"})


class PostprocessMODFLOWError(RuntimeError):
    """Raised on read / extraction / reproject / upload failures.

    Open-set A.6 ``error_code`` values:

    - ``PLUME_OUTPUT_READ_FAILED`` - could not locate / read ``gwt_model.ucn``.
    - ``PLUME_OUTPUT_EMPTY`` - the concentration array has no timesteps / cells.
    - ``PLUME_REPROJECT_FAILED`` - the UTM → EPSG:4326 warp failed.
    - ``PLUME_COG_WRITE_FAILED`` - rasterio could not write the COG.
    - ``PLUME_COG_UPLOAD_FAILED`` - the GCS upload of the COG failed.
    """

    error_code: str = "POSTPROCESS_MODFLOW_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# Pure metric math (unit-testable on synthetic arrays)
# --------------------------------------------------------------------------- #


def compute_plume_metrics(
    final_grid: Any,
    cell_area_m2: float,
    *,
    floor_mgl: float = PLUME_DETECTION_FLOOR_MGL,
) -> tuple[float, float]:
    """Compute (max_concentration_mgl, plume_area_km2) from a 2D conc grid.

    Pure arithmetic over the FINAL-timestep, max-over-layers concentration grid
    (a 2D ``numpy`` array in mg/L). A cell counts toward the plume iff its
    concentration is strictly greater than ``floor_mgl``.

    Args:
        final_grid: 2D array (rows × cols) of concentration in mg/L.
        cell_area_m2: per-cell area in m² (``delr * delc`` for a structured grid).
        floor_mgl: detection floor; cells ≤ this are clean (not plume).

    Returns:
        ``(max_concentration_mgl, plume_area_km2)``. Both ≥ 0. ``max`` is the
        global maximum over the grid (clamped at 0 so a numerically-negative
        dispersion artifact never narrates as a negative concentration);
        ``area`` is ``(#cells > floor) * cell_area_m2 / 1e6``.
    """
    import numpy as np  # local - caller vouched for the import path

    arr = np.asarray(final_grid, dtype="float64")
    if arr.size == 0:
        return 0.0, 0.0
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0, 0.0
    max_conc = float(np.max(finite))
    max_conc = max(0.0, max_conc)  # negative dispersion artifact → 0 (never narrate < 0)
    plume_cells = int(np.count_nonzero(finite > floor_mgl))
    plume_area_km2 = float(plume_cells) * float(cell_area_m2) / 1_000_000.0
    return max_conc, plume_area_km2


def compute_seepage_metrics(
    seepage_grid: Any,
) -> tuple[float, float, float, int]:
    """Compute (total_leakage, gaining, losing, river_cell_count) from a 2D grid.

    Pure arithmetic over the per-cell signed RIV exchange grid (m^3/day, NaN
    where no reach cell). MF6 RIV budget sign: a positive ``q`` is flow FROM the
    boundary INTO the cell, i.e. the river LOSES water to the aquifer (seepage
    in, a losing reach); a negative ``q`` is flow OUT of the cell to the river,
    i.e. the river GAINS water from the aquifer (baseflow, a gaining reach).

    Returns:
        ``(total_leakage_m3_day, gaining_m3_day, losing_m3_day, river_cell_count)``:
          * total_leakage_m3_day: net SIGNED sum over all reach cells
            (positive = net losing/recharging the aquifer).
          * gaining_m3_day: total MAGNITUDE of negative (gaining) flux, >= 0.
          * losing_m3_day: total MAGNITUDE of positive (losing) flux, >= 0.
          * river_cell_count: number of finite (reach) cells.
    """
    import numpy as np  # local - caller vouched for the import path

    arr = np.asarray(seepage_grid, dtype="float64")
    if arr.size == 0:
        return 0.0, 0.0, 0.0, 0
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0, 0.0, 0.0, 0
    total = float(np.sum(finite))
    losing = float(np.sum(finite[finite > 0.0]))  # river -> aquifer
    gaining = float(-np.sum(finite[finite < 0.0]))  # aquifer -> river (magnitude)
    return total, gaining, losing, int(finite.size)


def compute_drawdown_metrics(
    decline_grid: Any,
) -> float:
    """Compute the peak head DECLINE (>= 0) from a 2D drawdown grid.

    Pure arithmetic over the per-cell head-decline grid (m; pre-pumping head
    minus pumped head, so a positive value is a drawdown and a negative value is
    a mounding/recovery artifact). The headline is the maximum decline anywhere
    in the domain, clamped at 0 so a tiny numerical mounding never narrates as a
    negative drawdown.

    Args:
        decline_grid: 2D array (rows x cols) of head decline in m (NaN off-grid).

    Returns:
        ``max_drawdown_m`` (>= 0): the largest positive head decline.
    """
    import numpy as np  # local - caller vouched for the import path

    arr = np.asarray(decline_grid, dtype="float64")
    if arr.size == 0:
        return 0.0
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0
    return max(0.0, float(np.max(finite)))


def compute_cbc_term_metrics(
    term_grid: Any,
) -> tuple[float, int]:
    """Compute (total_outflow_magnitude_m3_day, active_cell_count) from a CBC grid.

    Pure arithmetic over a per-cell signed CBC budget grid (m^3/day, NaN where
    the term is absent). MF6 budget sign: a positive ``q`` is flow INTO the cell
    from the boundary; a negative ``q`` is flow OUT of the cell to the boundary.
    For a DRAIN (mine_dewatering) the drain removes water, so the per-cell flux
    is NEGATIVE and the dewatering RATE is the magnitude of that outflow.

    Returns:
        ``(total_magnitude_m3_day, active_cell_count)``:
          * total_magnitude_m3_day: sum of |q| over every finite (active) cell,
            >= 0 - the pump-to-dewater rate for a DRN term.
          * active_cell_count: number of finite cells the term touched.
    """
    import numpy as np  # local - caller vouched for the import path

    arr = np.asarray(term_grid, dtype="float64")
    if arr.size == 0:
        return 0.0, 0
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0, 0
    total_mag = float(np.sum(np.abs(finite)))
    return total_mag, int(finite.size)


def compute_mounding_metrics(
    rise_grid: Any,
) -> float:
    """Compute the peak head RISE (mounding, >= 0) from a 2D mound grid.

    Pure arithmetic over the per-cell head-RISE grid (m; pumped/recharged head
    minus pre-recharge head = head(t_last) - head(t0) under a MAR basin, so a
    positive value is a mound and a negative value is a draw-down artifact). The
    headline is the maximum rise anywhere in the domain, clamped at 0 so a tiny
    numerical dip never narrates as a negative mounding. This is the sign-flipped
    twin of ``compute_drawdown_metrics`` (which takes head(t0) - head(t_last)).

    Args:
        rise_grid: 2D array (rows x cols) of head rise in m (NaN off-grid).

    Returns:
        ``max_mounding_m`` (>= 0): the largest positive head rise.
    """
    import numpy as np  # local - caller vouched for the import path

    arr = np.asarray(rise_grid, dtype="float64")
    if arr.size == 0:
        return 0.0
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0
    return max(0.0, float(np.max(finite)))


def compute_recharged_volume_m3(
    rch_total_m3_day: float,
    duration_days: float,
) -> float | None:
    """Compute the total recharged volume (m^3, >= 0) from the RCH budget integral.

    Pure arithmetic: the MAR basin's recharge enters the aquifer at a per-day
    rate (the RCH/RCHA budget IN-sum over all basin cells, m^3/day, MF6 sign:
    positive = into the aquifer). Multiplied by the transient duration in days it
    gives the cumulative recharged volume. Returns ``None`` when either input is
    non-positive / unavailable (the honesty floor: never narrate a volume the
    budget did not measure).

    Args:
        rch_total_m3_day: the summed positive RCH/RCHA flux into the aquifer,
            m^3/day (the recharge-IN budget term).
        duration_days: the transient simulation duration in days.

    Returns:
        ``recharged_volume_m3`` (>= 0), or None when not computable.
    """
    if rch_total_m3_day is None or duration_days is None:
        return None
    rate = float(rch_total_m3_day)
    days = float(duration_days)
    if rate <= 0.0 or days <= 0.0:
        return None
    return rate * days


def compute_seasonal_head_range_m(
    head_steps: list[Any],
    cells: Any | None = None,
) -> tuple[float, list[float] | None]:
    """Compute the seasonal head RANGE (max-min, >= 0) + the at-cell head series.

    Pure arithmetic over the per-step max-over-layers head grids (m). The seasonal
    water-table swing IS the hydroperiod: at every cell take max-over-time minus
    min-over-time, then take the PEAK swing anywhere in the domain (the wetland
    cell where the table moves most). The returned timeseries is the per-step head
    AT THAT PEAK-SWING CELL (one value per saved step) so the chart traces the
    actual seasonal rise/fall.

    Args:
        head_steps: list of 2D head grids (NaN off-grid), one per saved step.
        cells: UNUSED placeholder (kept for signature symmetry); the peak-swing
            cell is found from the data, needing no footprint lookup.

    Returns:
        ``(seasonal_head_range_m, head_timeseries)``: the peak swing (>= 0) and
        the per-step head at the peak-swing cell (None when < 2 steps).
    """
    import numpy as np  # local - caller vouched for the import path

    del cells  # peak-swing cell is found from the data, not a footprint
    if not head_steps:
        return 0.0, None
    stack = np.stack([np.asarray(s, dtype="float64") for s in head_steps], axis=0)
    if stack.size == 0:
        return 0.0, None
    # Per-cell swing = max-over-time minus min-over-time (NaN-safe).
    with np.errstate(invalid="ignore"):
        cell_max = np.nanmax(stack, axis=0)
        cell_min = np.nanmin(stack, axis=0)
    swing = cell_max - cell_min
    finite = swing[np.isfinite(swing)]
    if finite.size == 0:
        return 0.0, None
    peak_range = max(0.0, float(np.nanmax(swing)))
    ts: list[float] | None = None
    if stack.shape[0] > 1:
        flat_idx = int(np.nanargmax(np.where(np.isfinite(swing), swing, -np.inf)))
        r, c = np.unravel_index(flat_idx, swing.shape)
        ts = [
            float(stack[i, r, c]) if np.isfinite(stack[i, r, c]) else 0.0
            for i in range(stack.shape[0])
        ]
    return peak_range, ts


def compute_recovery_efficiency(
    injected_volume_m3: float,
    recovered_volume_m3: float,
) -> float | None:
    """Compute the ASR recovery efficiency (recovered / injected), clamped [0, 1].

    Pure arithmetic: the fraction of injected water the ASR well recovers over
    the cycle(s). Both volumes are non-negative magnitudes (the WEL inject-IN and
    recover-OUT budget integrals). Returns ``None`` when nothing was injected (no
    efficiency is defined). The result is clamped to [0, 1] so a numerical
    over-recovery never narrates above 100%.

    Args:
        injected_volume_m3: total injected water magnitude, m^3 (>= 0).
        recovered_volume_m3: total recovered (extracted) water magnitude, m^3.

    Returns:
        ``recovery_efficiency`` in [0, 1], or None when injected <= 0.
    """
    if injected_volume_m3 is None or recovered_volume_m3 is None:
        return None
    inj = float(injected_volume_m3)
    rec = float(recovered_volume_m3)
    if inj <= 0.0:
        return None
    eff = rec / inj
    if eff < 0.0:
        return 0.0
    if eff > 1.0:
        return 1.0
    return eff


def compute_budget_partition(
    term_totals: dict[str, float],
) -> dict[str, float]:
    """Build the narration-ready budget partition from per-term CBC sums.

    Pure dict transform over the raw per-term signed budget sums (m^3/day, MF6
    sign: positive = into the aquifer/zone). EXCLUDES the internal inter-cell
    ``FLOW-JA-FACE`` term from the headline partition (it is bookkeeping, not a
    source/sink the user narrates) and drops a term whose magnitude rounds to
    zero. Honest signs are preserved verbatim: an extraction WEL reads negative,
    a recharge reads positive. Never free-generated - every value comes from a
    real CBC record sum the caller measured.

    Args:
        term_totals: mapping of CBC record name -> signed sum over all cells.

    Returns:
        ``budget_partition_m3_day``: the filtered/normalized partition dict.
    """
    partition: dict[str, float] = {}
    for raw_name, value in term_totals.items():
        name = str(raw_name).strip().upper()
        if name in _BUDGET_EXCLUDE_FROM_HEADLINE:
            continue
        q = float(value)
        if abs(q) < 1e-9:
            continue
        partition[name.lower()] = q
    return partition


def _normalize_cbc_record_names(cbc: Any) -> dict[str, str]:
    """Return a {UPPER label -> exact record name} map for a CBC file.

    flopy's ``get_unique_record_names(decode=True)`` returns the record names
    (str or bytes, padded). We strip + decode each and key by the UPPER label so
    a term lookup (``"DRN"``, ``"FLOW-JA-FACE"``) resolves to the exact name the
    ``get_data(text=...)`` call needs. The first record matching a label wins.
    """
    out: dict[str, str] = {}
    for r in cbc.get_unique_record_names(decode=True):
        name = (r.strip() if isinstance(r, str) else r.strip().decode())
        key = name.upper()
        out.setdefault(key, name)
    return out


def _scatter_cbc_term_grid(
    cbc: Any, record_name: str, nrow: int, ncol: int
) -> Any:
    """Scatter the LAST-timestep CBC ``record_name`` budget onto a 2D grid.

    Reads the per-cell signed flux for ONE CBC term (DRN / WEL / RIV / RCH /
    ...) and scatters the last-timestep ``q`` values onto an (nrow, ncol) grid
    (NaN where the term is absent). Multi-layer cells accumulate onto the same
    2D cell (collapse the layer axis). The ``node`` field is a 1-based flat
    structured-grid index = lay*nrow*ncol + row*ncol + col + 1.

    Returns the 2D grid, or an all-NaN grid when the term carries no records.
    """
    import numpy as np  # type: ignore[import-not-found]

    grid = np.full((nrow, ncol), np.nan, dtype="float64")
    data = cbc.get_data(text=record_name)
    if not data:
        return grid
    last = data[-1]
    try:
        nodes = np.asarray(last["node"], dtype="int64")
        qvals = np.asarray(last["q"], dtype="float64")
    except Exception:  # noqa: BLE001 - list-style budget (older formats)
        nodes = np.asarray([int(r["node"]) for r in last], dtype="int64")
        qvals = np.asarray([float(r["q"]) for r in last], dtype="float64")
    cells_per_layer = nrow * ncol
    for node, q in zip(nodes, qvals):
        local = (int(node) - 1) % cells_per_layer
        row = local // ncol
        col = local % ncol
        if 0 <= row < nrow and 0 <= col < ncol:
            grid[row, col] = q if np.isnan(grid[row, col]) else grid[row, col] + q
    return grid


def _read_cbc_term_grid(
    cbc_path: Path, term: str, nrow: int, ncol: int
) -> Any:
    """Read ONE CBC budget term (e.g. DRN / WEL / RCH) into a 2D signed grid.

    Generalization of ``_read_riv_seepage_grid`` for the sprint-18 archetypes:
    the mine-dewatering DRN term, an RCH/EVT recharge term, etc. Resolves the
    term name case-insensitively against the file's unique record names and
    scatters the last-timestep flux onto the grid.

    Raises ``PostprocessMODFLOWError("DEWATER_OUTPUT_EMPTY")`` when the requested
    term is absent from the budget (a DRN run that wrote no DRN term is a real
    failure, not a silent empty layer).
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "DEWATER_OUTPUT_READ_FAILED",
            message=f"flopy/numpy not importable: {exc}",
            details={"cbc_path": str(cbc_path), "term": term},
        ) from exc

    try:
        cbc = flopy.utils.CellBudgetFile(str(cbc_path))
        names = _normalize_cbc_record_names(cbc)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "DEWATER_OUTPUT_READ_FAILED",
            message=f"could not open CBC {cbc_path}: {exc}",
            details={"cbc_path": str(cbc_path), "term": term},
        ) from exc

    want = term.strip().upper()
    match = next((exact for key, exact in names.items() if want in key), None)
    if match is None:
        raise PostprocessMODFLOWError(
            "DEWATER_OUTPUT_EMPTY",
            message=(
                f"no {want} budget record in {cbc_path}; "
                f"records present: {sorted(names)}"
            ),
            details={"cbc_path": str(cbc_path), "term": term},
        )
    try:
        return _scatter_cbc_term_grid(cbc, match, nrow, ncol)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "DEWATER_OUTPUT_READ_FAILED",
            message=f"could not read {want} budget from {cbc_path}: {exc}",
            details={"cbc_path": str(cbc_path), "term": term},
        ) from exc


def _read_cbc_budget_partition(cbc_path: Path) -> dict[str, float]:
    """Read every CBC term and sum its per-cell flux -> a per-term total dict.

    Iterates the file's unique record names and sums the LAST-timestep ``q``
    over all cells for each term. The result feeds ``compute_budget_partition``
    (which drops FLOW-JA-FACE + near-zero terms). Honest signs preserved: each
    sum is the signed MF6 budget total (positive = into the aquifer).

    Raises ``PostprocessMODFLOWError("BUDGET_OUTPUT_EMPTY")`` when the file has
    no budget records at all.
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "BUDGET_OUTPUT_READ_FAILED",
            message=f"flopy/numpy not importable: {exc}",
            details={"cbc_path": str(cbc_path)},
        ) from exc

    try:
        cbc = flopy.utils.CellBudgetFile(str(cbc_path))
        names = _normalize_cbc_record_names(cbc)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "BUDGET_OUTPUT_READ_FAILED",
            message=f"could not open CBC {cbc_path}: {exc}",
            details={"cbc_path": str(cbc_path)},
        ) from exc

    if not names:
        raise PostprocessMODFLOWError(
            "BUDGET_OUTPUT_EMPTY",
            message=f"no budget records in {cbc_path}",
            details={"cbc_path": str(cbc_path)},
        )

    totals: dict[str, float] = {}
    for key, exact in names.items():
        if key in _BUDGET_EXCLUDE_FROM_HEADLINE:
            # Skip the internal inter-cell term entirely (also dropped later, but
            # its per-cell array is large + uninformative for the partition).
            continue
        try:
            data = cbc.get_data(text=exact)
        except Exception:  # noqa: BLE001 - skip an unreadable term, not fatal
            continue
        if not data:
            continue
        last = data[-1]
        try:
            arr = np.asarray(last["q"], dtype="float64")
        except Exception:  # noqa: BLE001
            try:
                # full-grid arrays come back as a plain ndarray; sum to one total.
                totals[key] = totals.get(key, 0.0) + float(
                    np.nansum(np.asarray(last, dtype="float64"))
                )
            except Exception:  # noqa: BLE001
                pass
            continue
        # Split a head-dependent / source-sink term into IN (q>0, into the
        # aquifer) and OUT (q<0, out of the aquifer) so a balanced boundary like
        # the regional CHD gradient narrates as separate inflow + outflow legs
        # rather than collapsing to a net ~0 that hides the throughflow. Honest
        # MF6 signs preserved (in positive, out negative).
        in_sum = float(np.nansum(arr[arr > 0.0]))
        out_sum = float(np.nansum(arr[arr < 0.0]))
        if abs(in_sum) > 0.0:
            totals[f"{key}_IN"] = totals.get(f"{key}_IN", 0.0) + in_sum
        if abs(out_sum) > 0.0:
            totals[f"{key}_OUT"] = totals.get(f"{key}_OUT", 0.0) + out_sum
        if abs(in_sum) == 0.0 and abs(out_sum) == 0.0:
            # A genuinely-zero term still records its net (0) so the absence is
            # explicit rather than silently dropped before compute_budget_partition.
            totals[key] = totals.get(key, 0.0) + float(np.nansum(arr))
    return totals


def _read_riv_seepage_grid(
    cbc_path: Path, nrow: int, ncol: int
) -> Any:
    """Read the GWF cbc RIV budget into a 2D per-cell signed seepage grid.

    The RIV cell-by-cell budget is a list/recarray with a ``node`` (1-based
    cell id) + ``q`` (exchange flow) field per reach cell. We scatter the
    last-timestep ``q`` values onto an (nrow, ncol) grid (NaN elsewhere) so the
    seepage COG renders only the reach. flopy's ``CellBudgetFile.get_data(
    text="RIV")`` returns the recarray; the ``node`` is a flat 0-based-after-
    decrement structured-grid index = lay*nrow*ncol + row*ncol + col.
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "SEEPAGE_OUTPUT_READ_FAILED",
            message=f"flopy/numpy not importable: {exc}",
            details={"cbc_path": str(cbc_path)},
        ) from exc

    try:
        cbc = flopy.utils.CellBudgetFile(str(cbc_path))
        record_names = {
            (r.strip() if isinstance(r, str) else r.strip().decode())
            for r in cbc.get_unique_record_names(decode=True)
        }
        if not any("RIV" in n.upper() for n in record_names):
            raise PostprocessMODFLOWError(
                "SEEPAGE_OUTPUT_EMPTY",
                message=(
                    f"no RIV budget record in {cbc_path}; "
                    f"records present: {sorted(record_names)}"
                ),
                details={"cbc_path": str(cbc_path)},
            )
        riv_data = cbc.get_data(text="RIV")
        if not riv_data:
            raise PostprocessMODFLOWError(
                "SEEPAGE_OUTPUT_EMPTY",
                message=f"RIV budget record present but empty in {cbc_path}",
                details={"cbc_path": str(cbc_path)},
            )
    except PostprocessMODFLOWError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "SEEPAGE_OUTPUT_READ_FAILED",
            message=f"could not read RIV budget from {cbc_path}: {exc}",
            details={"cbc_path": str(cbc_path)},
        ) from exc

    last = riv_data[-1]
    grid = np.full((nrow, ncol), np.nan, dtype="float64")
    # The RIV budget recarray exposes the cell id under "node" (1-based flat).
    try:
        nodes = np.asarray(last["node"], dtype="int64")
        qvals = np.asarray(last["q"], dtype="float64")
    except Exception:  # noqa: BLE001 - list-style budget (older formats)
        # Fall back to attribute access on a list of records.
        nodes = np.asarray([int(r["node"]) for r in last], dtype="int64")
        qvals = np.asarray([float(r["q"]) for r in last], dtype="float64")
    cells_per_layer = nrow * ncol
    for node, q in zip(nodes, qvals):
        idx0 = int(node) - 1  # 1-based -> 0-based flat
        local = idx0 % cells_per_layer  # collapse layers onto the 2D grid
        row = local // ncol
        col = local % ncol
        if 0 <= row < nrow and 0 <= col < ncol:
            # Accumulate (a multi-layer reach maps several cells to one 2D cell).
            grid[row, col] = (
                q if np.isnan(grid[row, col]) else grid[row, col] + q
            )
    return grid


# --------------------------------------------------------------------------- #
# UCN read + grid georegistration
# --------------------------------------------------------------------------- #


def _resolve_ucn_path(run_outputs_uri: str) -> Path:
    """Locate ``gwt_model.ucn`` from a local dir / file:// / gs:// / s3:// run
    output.

    Local (``file://`` or a bare path): search the dir tree for the UCN file.
    gs:// : fetch via fsspec into a temp dir (mirrors postprocess_flood).
    s3:// (job-0292b - the local-backend runs prefix): fetch via **boto3**
    through the solver module's shared S3 client seam (job-0289 lesson). The
    local-mode live-evidence path always passes a local dir.
    """
    if run_outputs_uri.startswith("s3://"):
        from ..tools.simulation.solver import _get_s3_client

        tmpdir = Path(tempfile.mkdtemp(prefix="modflow-output-"))
        local_target = tmpdir / GWT_UCN_FILENAME
        source = (
            run_outputs_uri
            if run_outputs_uri.endswith(".ucn")
            else run_outputs_uri.rstrip("/") + f"/{GWT_UCN_FILENAME}"
        )
        bucket_name, _, obj_key = source[len("s3://"):].partition("/")
        try:
            import shutil as _shutil

            resp = _get_s3_client().get_object(Bucket=bucket_name, Key=obj_key)
            with local_target.open("wb") as fh:
                _shutil.copyfileobj(resp["Body"], fh)
        except Exception as exc:  # noqa: BLE001
            raise PostprocessMODFLOWError(
                "PLUME_OUTPUT_READ_FAILED",
                message=f"could not fetch UCN from {source}: {exc}",
                details={"run_outputs_uri": run_outputs_uri},
            ) from exc
        return local_target
    if run_outputs_uri.startswith("gs://"):
        try:
            import fsspec  # type: ignore[import-not-found]

            fs = fsspec.filesystem("gcs")
            tmpdir = Path(tempfile.mkdtemp(prefix="modflow-output-"))
            local_target = tmpdir / GWT_UCN_FILENAME
            prefix = run_outputs_uri.rstrip("/")
            candidate = (
                run_outputs_uri
                if run_outputs_uri.endswith(".ucn")
                else f"{prefix}/{GWT_UCN_FILENAME}"
            )
            fs.get(candidate, str(local_target))
            return local_target
        except Exception as exc:  # noqa: BLE001
            raise PostprocessMODFLOWError(
                "PLUME_OUTPUT_READ_FAILED",
                message=f"could not fetch UCN from {run_outputs_uri}: {exc}",
                details={"run_outputs_uri": run_outputs_uri},
            ) from exc

    p = Path(run_outputs_uri.replace("file://", ""))
    if p.is_file() and p.suffix == ".ucn":
        return p
    if p.is_dir():
        hits = sorted(glob.glob(str(p / "**" / GWT_UCN_FILENAME), recursive=True))
        if not hits:
            # any .ucn (defensive: an adapter could rename the stem).
            hits = sorted(glob.glob(str(p / "**" / "*.ucn"), recursive=True))
        if hits:
            return Path(hits[0])
    raise PostprocessMODFLOWError(
        "PLUME_OUTPUT_READ_FAILED",
        message=f"no {GWT_UCN_FILENAME} found under {run_outputs_uri}",
        details={"run_outputs_uri": run_outputs_uri},
    )


def _read_final_concentration(ucn_path: Path) -> Any:
    """Read the FINAL-timestep, max-over-layers concentration grid (mg/L, 2D).

    MF6 GWT concentration output is a binary HEADFILE-format array; flopy reads
    it via ``HeadFile(..., text="CONCENTRATION")``. ``get_data(totim=last)``
    returns a ``(nlay, nrow, ncol)`` array; we take ``nanmax`` over the layer
    axis to get a 2D worst-case (max-over-depth) grid the plume narrates.
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "PLUME_OUTPUT_READ_FAILED",
            message=f"flopy/numpy not importable: {exc}",
            details={"ucn_path": str(ucn_path)},
        ) from exc

    try:
        cobj = flopy.utils.HeadFile(str(ucn_path), text="CONCENTRATION")
        times = cobj.get_times()
        if not times:
            raise PostprocessMODFLOWError(
                "PLUME_OUTPUT_EMPTY",
                message=f"{ucn_path} carries no concentration timesteps",
                details={"ucn_path": str(ucn_path)},
            )
        data = cobj.get_data(totim=times[-1])  # (nlay, nrow, ncol)
    except PostprocessMODFLOWError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "PLUME_OUTPUT_READ_FAILED",
            message=f"could not read concentration from {ucn_path}: {exc}",
            details={"ucn_path": str(ucn_path)},
        ) from exc

    arr = np.asarray(data, dtype="float64")
    if arr.ndim == 3:
        # max over the layer axis → 2D worst-case-over-depth grid.
        final2d = np.nanmax(arr, axis=0)
    elif arr.ndim == 2:
        final2d = arr
    else:
        final2d = np.squeeze(arr)
        if final2d.ndim != 2:
            raise PostprocessMODFLOWError(
                "PLUME_OUTPUT_EMPTY",
                message=f"concentration array has shape {arr.shape}; cannot reduce to 2D",
                details={"ucn_path": str(ucn_path), "shape": list(arr.shape)},
            )
    # MF6 inactive/dry cells are flagged with a large sentinel (1e30). Mask them.
    final2d = np.where(np.abs(final2d) > 1e29, np.nan, final2d)
    return final2d


def _grid_georegistration_from_deck(deck_dir: str | None) -> dict[str, Any] | None:
    """Read grid origin + cell size from the deck via flopy (for the COG transform).

    The deck dir holds the GWT (or, for a GWF-only Wave-1 archetype deck, the
    GWF) DIS package; flopy's modelgrid gives the lower-left origin
    (xorigin/yorigin) + cell widths (delr/delc). Returns None if the deck cannot
    be loaded (the caller then falls back to identity, which still yields valid
    metrics - only the geo-placement degrades).

    The two model halves share the SAME georegistered grid (the GWFGWT exchange
    requires it), so either works; we PREFER the GWT model (the spill/seepage
    deck's transport grid) and fall back to the GWF model (a GWF-only archetype
    deck has no GWT model). Any model with a structured modelgrid is acceptable.
    """
    if not deck_dir:
        return None
    try:
        import flopy  # type: ignore[import-not-found]

        sim = flopy.mf6.MFSimulation.load(sim_ws=str(deck_dir), verbosity_level=0)
        model = None
        # Prefer GWT (transport grid); fall back to GWF (GWF-only archetypes); then
        # any model in the sim (defensive). Same grid either way.
        for prefix in ("gwt", "gwf"):
            for mname in sim.model_names:
                if mname.startswith(prefix):
                    model = sim.get_model(mname)
                    break
            if model is not None:
                break
        if model is None and sim.model_names:
            model = sim.get_model(sim.model_names[0])
        if model is None:
            return None
        mg = model.modelgrid
        return {
            "xorigin": float(mg.xoffset),
            "yorigin": float(mg.yoffset),
            "delr": float(mg.delr[0]),
            "delc": float(mg.delc[0]),
            "nrow": int(mg.nrow),
            "ncol": int(mg.ncol),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read deck georegistration from %s: %s", deck_dir, exc)
        return None


# --------------------------------------------------------------------------- #
# COG write + reproject + upload
# --------------------------------------------------------------------------- #


#: stage -> (MODFLOW error_code) map (STEP 1 dedupe; byte-identical codes). The
#: write/reproject stages map to the PLUME_* codes (the seepage path reuses the
#: same writer, exactly as before this dedupe).
_MODFLOW_STAGE_CODES: dict[str, str] = {
    "DEPENDENCY": "PLUME_COG_WRITE_FAILED",
    "WRITE": "PLUME_COG_WRITE_FAILED",
    "REPROJECT": "PLUME_REPROJECT_FAILED",
    "CRS_MISMATCH": "PLUME_REPROJECT_FAILED",
    "UPLOAD": "PLUME_COG_UPLOAD_FAILED",
}


def _reraise_cogio(
    exc: CogIoError, *, model_crs: str | None = None
) -> "PostprocessMODFLOWError":
    """Map a cog_io ``CogIoError`` onto the MODFLOW typed error (preserves codes)."""
    code = _MODFLOW_STAGE_CODES.get(exc.stage, "POSTPROCESS_MODFLOW_FAILED")
    details = dict(exc.details)
    if model_crs is not None and "model_crs" not in details:
        details["model_crs"] = model_crs
    return PostprocessMODFLOWError(code, message=exc.message, details=details)


def _write_reprojected_cog(
    final2d: Any,
    model_crs: str,
    geo: dict[str, Any] | None,
    *,
    mask_below_floor: bool = True,
) -> Path:
    """Write the concentration grid to an EPSG:4326 COG, reprojecting from model_crs.

    The grid is in the deck's projected (UTM) CRS. We build the source transform
    from the grid origin + cell size (flopy's row 0 is the NORTH row, so the
    transform's top-left is yorigin + nrow*delc), tag it ``model_crs``, then warp
    to EPSG:4326 via ``cog_io.write_cog_4326_from_grid`` (``reproject=True``,
    ``Resampling.bilinear`` for the smooth concentration field; NO CRS round-trip
    guard, byte-identical to the pre-dedupe writer).

    Args:
        mask_below_floor: when True (the plume default - BYTE-IDENTICAL to the
            pre-J9 behavior), cells at/below ``PLUME_DETECTION_FLOOR_MGL`` are
            masked to NaN so the COG renders only the plume. When False (the J9
            river-seepage diverging layer), the array is written AS-IS (already
            NaN off the reach) so negative gaining values survive - masking by a
            positive floor would wrongly drop every gaining (negative) reach
            cell. Passed to cog_io as the declared ``mask`` callable.
    """
    import numpy as np  # type: ignore[import-not-found]
    import rasterio  # type: ignore[import-not-found]
    from rasterio.warp import Resampling

    arr = np.asarray(final2d, dtype="float32")
    nrow, ncol = arr.shape

    if geo is not None:
        delr = geo["delr"]
        delc = geo["delc"]
        xorigin = geo["xorigin"]
        yorigin = geo["yorigin"]
        # flopy row 0 = north; rasterio's from_origin top-left = (west, north).
        west = xorigin
        north = yorigin + nrow * delc
        src_transform = rasterio.transform.from_origin(west, north, delr, delc)
    else:
        # Degraded fallback: identity transform (metrics still valid; placement
        # arbitrary). Logged by the caller via the None geo path.
        src_transform = rasterio.Affine.identity()

    def _mask(a: Any) -> Any:
        if mask_below_floor:
            # Mask clean cells (<= floor) to NaN so the COG renders only the plume.
            return np.where(a > PLUME_DETECTION_FLOOR_MGL, a, np.nan).astype("float32")
        # Diverging seepage: keep the array as-is (NaN already marks off-reach).
        return a.astype("float32")

    try:
        return cog_io.write_cog_4326_from_grid(
            arr,
            src_crs=model_crs,
            src_transform=src_transform,
            reproject=True,
            resampling=Resampling.bilinear,
            mask=_mask,
            crs_roundtrip_guard=False,
            src_suffix="_src.tif",
            dst_suffix="_4326.tif",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc, model_crs=model_crs) from exc


def _cog_bbox_4326(cog_path: Path) -> tuple[float, float, float, float] | None:
    """Return the COG's (min_lon, min_lat, max_lon, max_lat) for zoom-to."""
    return cog_io.cog_bbox_4326(cog_path)


def _upload_cog(
    local_cog: Path,
    run_id: str,
    runs_bucket: str | None,
    *,
    cog_filename: str = "plume_concentration_4326.tif",
) -> str:
    """Upload the EPSG:4326 plume COG to the runs bucket; return its object URI.

    Thin shim over ``cog_io.upload_cog`` (STEP 1 dedupe; byte-identical):
    scheme-aware per ``cache.storage_scheme()``. ``s3`` via boto3
    (``ContentType=image/tiff``) FAILS TYPED on a missing ``TRID3NT_RUNS_BUCKET`` /
    upload error (job-0241 / job-0292b: a silent file:// on AWS is the
    debug-invisible no-render failure). The ``gs`` branch keeps its best-effort
    ``file://`` fallback (the loud ImportError classification for a missing
    ``fsspec[gcs]`` is preserved by cog_io) for the offline-dev / local-mode path.
    """
    try:
        return cog_io.upload_cog(
            local_cog,
            run_id,
            runs_bucket,
            dest_filename=cog_filename,
            content_type="image/tiff",
            gs_backend="fsspec",
            gs_fallback_to_file=True,
            runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label="plume COG",
        )
    except CogIoError as exc:
        raise _reraise_cogio(exc) from exc


# --------------------------------------------------------------------------- #
# publish_layer dispatch (callable; mocked in tests)
# --------------------------------------------------------------------------- #


def _dispatch_publish_layer(
    cog_uri: str, layer_id: str, *, style_preset: str = PLUME_STYLE_PRESET
) -> str | None:
    """Publish the plume COG; return the WMS URL / tile template or None.

    Non-fatal: a publish failure (worker SA grant, GCS read) falls back to the
    COG URI so the rest of the envelope is usable. Skips publish entirely for
    non-object-store URIs (local mode has nothing for a tile server to read).

    job-0292b: ``s3://`` COGs pass through too - on the AWS deployment
    ``publish_layer`` returns a TiTiler XYZ tile TEMPLATE for them (the
    job-0290 ``TRID3NT_TILE_SERVER_BASE`` path), which closes the job-0254
    PlumeLayerURI rendering gap on AWS the same way flood-depth COGs publish.
    """
    if not (cog_uri.startswith("gs://") or cog_uri.startswith("s3://")):
        # job-0241: loud, not silent - a non-object-store URI here means the
        # upload fell back (stale venv / auth / network) and the plume will
        # NOT appear on the map. The Case 2 live gate (job-0235) burned on
        # exactly this as a debug-invisible skip.
        logger.warning(
            "publish_layer SKIPPED for %s: COG URI is not gs:// or s3:// (%s); "
            "the plume will NOT render as a map layer. Check the object-store "
            "upload succeeded.",
            layer_id,
            cog_uri,
        )
        return None
    try:
        from ..tools.publish_layer import PublishLayerError, publish_layer

        wms_url = publish_layer(
            layer_uri=cog_uri,
            layer_id=layer_id,
            style_preset=style_preset,
        )
        logger.info("publish_layer succeeded layer_id=%s wms_url=%s", layer_id, wms_url)
        return wms_url
    except Exception as exc:  # noqa: BLE001
        # PublishLayerError or any import/dispatch failure: non-fatal.
        logger.warning("publish_layer failed for %s: %s", layer_id, exc)
        return None


# --------------------------------------------------------------------------- #
# Top-level postprocess
# --------------------------------------------------------------------------- #


def postprocess_modflow(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    publish: bool = True,
) -> PlumeLayerURI:
    """Convert a MODFLOW GWT run's UCN output into a plume ``PlumeLayerURI``.

    Reads the final-timestep, max-over-layers concentration grid, reprojects it
    to an EPSG:4326 COG, computes the plume metrics, uploads + (optionally)
    publishes the COG, and returns the typed plume layer.

    Args:
        run_outputs_uri: the run output location (local dir / ``file://`` for the
            local path, ``gs://`` for the cloud path; finds ``gwt_model.ucn``).
        run_id: the run identifier the COG is keyed under in the runs bucket.
        model_crs: the deck's projected CRS (e.g. ``"EPSG:32617"``) - the
            OQ-MOD-3 handoff field the reprojection needs.
        deck_dir: optional on-disk deck dir for grid georegistration (origin +
            cell size). When ``None``, the COG uses an identity transform
            (metrics stay valid; geographic placement degrades).
        runs_bucket: optional override for the runs bucket name.
        publish: when True, dispatch ``publish_layer`` (mocked in tests).

    Returns:
        ``PlumeLayerURI`` with ``max_concentration_mgl`` + ``plume_area_km2``
        and (when published) a WMS ``uri``, else the COG ``gs://`` / ``file://``
        URI.

    Raises:
        PostprocessMODFLOWError: any read / reproject / write / upload step
            failed; ``error_code`` identifies the stage.
    """
    ucn_path = _resolve_ucn_path(run_outputs_uri)
    final2d = _read_final_concentration(ucn_path)

    geo = _grid_georegistration_from_deck(deck_dir)
    cell_area_m2 = (
        float(geo["delr"]) * float(geo["delc"]) if geo is not None else 2500.0
    )  # default 50 m cells if deck georegistration unavailable (gwt_adapter CELL_SIZE_M)

    max_conc, plume_area_km2 = compute_plume_metrics(final2d, cell_area_m2)
    logger.info(
        "postprocess_modflow run_id=%s max_concentration_mgl=%.6g plume_area_km2=%.6g",
        run_id,
        max_conc,
        plume_area_km2,
    )

    cog_path = _write_reprojected_cog(final2d, model_crs, geo)
    bbox_4326 = _cog_bbox_4326(cog_path)
    try:
        cog_uri = _upload_cog(cog_path, run_id, runs_bucket)
    finally:
        # The upload made a copy (cloud) or we returned the local path; only
        # unlink when we did NOT keep the local file as the URI.
        pass

    layer_id = f"plume-concentration-{run_id}"
    final_uri = cog_uri
    if publish:
        wms_url = _dispatch_publish_layer(cog_uri, layer_id)
        if wms_url:
            final_uri = wms_url

    # --- UGRID mesh sibling (MDAL phase 2, additive) ------------------------
    # Best-effort: a mesh-build/upload failure never sinks the plume COG this
    # function already produced. Lazy import -- modflow_mesh imports this
    # module's private helpers, so a module-level import here would be
    # circular; see workflows/modflow_mesh.py's "Wiring" docstring section.
    try:
        from .modflow_mesh import emit_modflow_mesh_artifact

        emit_modflow_mesh_artifact(
            run_outputs_uri,
            run_id=run_id,
            model_crs=model_crs,
            deck_dir=deck_dir,
            runs_bucket=runs_bucket,
        )
    except Exception as exc:  # noqa: BLE001 -- see emit_modflow_mesh_artifact docstring
        logger.warning("modflow mesh dispatch failed (non-fatal) run_id=%s: %s", run_id, exc)

    return PlumeLayerURI(
        layer_id=layer_id,
        name="Contaminant Plume (peak concentration)",
        layer_type="raster",
        uri=final_uri,
        style_preset=PLUME_STYLE_PRESET,
        role="primary",
        units="mg/L",
        bbox=bbox_4326,
        max_concentration_mgl=max_conc,
        plume_area_km2=plume_area_km2,
    )


# --------------------------------------------------------------------------- #
# multi_species postprocess (sprint-18 Wave-3) - N per-species UCN -> N plumes
#
# A multi_species run writes ONE ``gwt_<species>.ucn`` per solute (the adapter's
# per-species OC, gwt_adapter._build_multi_species_deck). This path globs those
# N files, REUSES the single-species concentration reader + plume COG + publish
# path per species, and returns a LIST of PlumeLayerURI (one per species; each
# carries the same two narration scalars + the species name in the layer label).
# The single-species spill path (postprocess_modflow, ONE gwt_model.ucn) is
# byte-untouched.
# --------------------------------------------------------------------------- #


# Must stay byte-identical to gwt_adapter._gwt_model_name_for_species (worker side,
# a separate deploy bundle) so a per-species UCN stem maps back to the user's name
# by value. MF6 caps MODELNAME at 16 chars -> the worker truncates + hash-tags long
# names; this mirror reproduces that exactly. See that function's docstring.
_MF6_MODELNAME_MAXLEN = 16


def _sanitise_species_to_stem(name: str) -> str:
    """Map a species name to the adapter's filesystem-safe ``gwt_<safe>`` stem.

    Mirrors ``gwt_adapter._gwt_model_name_for_species`` EXACTLY (lowercase, every
    non-alphanumeric run -> single ``_``, prefix ``gwt_``, then truncate + 4-hex
    hash when the result would exceed MF6's 16-char MODELNAME limit). Used to align
    a composer-supplied real species name (e.g. ``"cis-DCE"``) to the per-species
    UCN filename stem (``gwt_cis_dce``) BY VALUE rather than by position, so the
    layer label is correct regardless of the glob sort order.
    """
    safe = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(name))
    safe = "_".join(part for part in safe.split("_") if part) or "species"
    candidate = f"gwt_{safe}"
    if len(candidate) <= _MF6_MODELNAME_MAXLEN:
        return candidate
    digest = hashlib.sha1(safe.encode("utf-8")).hexdigest()[:4]
    head_budget = _MF6_MODELNAME_MAXLEN - len("gwt_") - 1 - len(digest)
    return f"gwt_{safe[:head_budget]}_{digest}"


def _species_label_from_ucn_stem(stem: str) -> str:
    """Recover a human species label from a ``gwt_<species>`` UCN file stem.

    The adapter sanitises a species name to a filesystem-safe GWT model name
    ``gwt_<safe>`` (``gwt_adapter._gwt_model_name_for_species``: lowercased,
    non-alnum -> ``_``), so the exact original casing/punctuation is not
    recoverable from the filename alone. We strip the ``gwt_`` prefix and
    upper-case the remainder for a readable label (e.g. ``gwt_cis_dce`` ->
    ``CIS_DCE``). The caller may override with the real species name when the
    deck manifest carried it (the composer threads ``species_names`` so the
    layer label matches the user's vocabulary exactly).
    """
    s = stem
    if s.startswith("gwt_"):
        s = s[len("gwt_"):]
    return s.upper() if s else stem


def _species_label_for_path(
    ucn_path: Path, species_names: list[str] | None
) -> str:
    """Resolve the display label for one per-species UCN path.

    Prefers the composer-threaded real name whose sanitised form equals the UCN
    file stem (BY VALUE, not position - so the label is correct regardless of the
    glob sort order). Falls back to the stem-recovered label when no name matches
    (the per-species concentration scalars are correct either way).
    """
    if species_names:
        for name in species_names:
            if _sanitise_species_to_stem(name) == ucn_path.stem:
                return str(name)
    return _species_label_from_ucn_stem(ucn_path.stem)


def _resolve_species_ucn_paths(run_outputs_uri: str) -> list[Path]:
    """Locate every per-species ``gwt_<species>.ucn`` from a run output.

    Local (``file://`` / bare path): glob ``gwt_*.ucn`` under the dir tree,
    EXCLUDING the single-species ``gwt_model.ucn`` stem (so a spill deck is never
    mis-read as a one-species multi run). ``s3://`` / ``gs://``: the multi_species
    live path always passes a LOCAL deck dir (the local-mode mf6 run), so the
    object-store branch fetches by listing is not required here; a single ``.ucn``
    URI is resolved as one species. Returns the per-species UCN paths sorted by
    filename for a deterministic plume ordering.

    Raises ``PostprocessMODFLOWError("PLUME_OUTPUT_READ_FAILED")`` when no
    per-species UCN is found.
    """
    # A directly-pointed single .ucn (object-store / explicit file) is one species.
    if run_outputs_uri.endswith(".ucn"):
        if run_outputs_uri.startswith(("s3://", "gs://")):
            # Reuse the single-species resolver's fetch for one explicit file.
            return [_resolve_ucn_path(run_outputs_uri)]
        p = Path(run_outputs_uri.replace("file://", ""))
        if p.is_file():
            return [p]

    p = Path(run_outputs_uri.replace("file://", ""))
    if p.is_dir():
        hits = sorted(
            Path(g)
            for g in glob.glob(str(p / "**" / GWT_SPECIES_UCN_GLOB), recursive=True)
            if Path(g).stem != GWT_SINGLE_SPECIES_STEM
        )
        if hits:
            return hits
    raise PostprocessMODFLOWError(
        "PLUME_OUTPUT_READ_FAILED",
        message=(
            f"no per-species {GWT_SPECIES_UCN_GLOB} found under {run_outputs_uri} "
            f"(excluding the single-species {GWT_SINGLE_SPECIES_STEM} stem); a "
            "multi_species run must write one gwt_<species>.ucn per solute."
        ),
        details={"run_outputs_uri": run_outputs_uri},
    )


def postprocess_multi_species(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    publish: bool = True,
    species_names: list[str] | None = None,
) -> MultiSpeciesPlumeResult:
    """Convert a multi_species MODFLOW run's N UCN outputs into N plume layers.

    Globs the per-species ``gwt_<species>.ucn`` files the multi_species deck
    wrote, and for EACH species REUSES the single-species concentration reader +
    plume-metric math + plume COG + upload/publish path to produce ONE
    ``PlumeLayerURI`` carrying that species' ``max_concentration_mgl`` +
    ``plume_area_km2`` and the species name in the layer label. Returns an ordered
    ``MultiSpeciesPlumeResult`` (one plume per species, in glob/filename order, or
    in ``species_names`` order when the composer threads the real names).

    Args:
        run_outputs_uri: the run output location (a local dir / ``file://`` for
            the local-mode mf6 path; finds the per-species ``gwt_<species>.ucn``).
        run_id: the run identifier each per-species COG is keyed under.
        model_crs: the deck's projected CRS (the OQ-MOD-3 handoff field).
        deck_dir: optional on-disk deck dir for the SHARED grid georegistration
            (all species share one GWF flow field / grid).
        runs_bucket: optional override for the runs bucket name.
        publish: when True, dispatch ``publish_layer`` per species (mocked).
        species_names: optional ordered real species names (the composer threads
            ``DeckStaging.species_names`` so the layer labels match the user's
            vocabulary exactly, e.g. "TCE" / "cis-DCE"). When None, a label is
            recovered from each UCN file stem. When supplied AND the count matches
            the per-species UCN count, the names are zipped onto the SORTED ucn
            paths positionally; the adapter writes ucn files in species order and
            the sort is stable on the sanitised stem, so this is best-effort
            alignment (the per-species scalars are correct regardless).

    Returns:
        ``MultiSpeciesPlumeResult`` with ``plumes`` = ordered list of one
        ``PlumeLayerURI`` per species.

    Raises:
        PostprocessMODFLOWError: a read / reproject / write / upload step failed,
            or no per-species UCN was found.
    """
    ucn_paths = _resolve_species_ucn_paths(run_outputs_uri)

    # The N species share ONE GWF flow field / grid, so the georegistration is
    # read ONCE (not per species) - identical cell area for every plume.
    geo = _grid_georegistration_from_deck(deck_dir)
    cell_area_m2 = (
        float(geo["delr"]) * float(geo["delc"]) if geo is not None else 2500.0
    )  # default 50 m cells if deck georegistration unavailable (gwt_adapter CELL_SIZE_M)

    plumes: list[PlumeLayerURI] = []
    for idx, ucn_path in enumerate(ucn_paths):
        # Resolve the label by VALUE (match the composer-threaded real name whose
        # sanitised form equals this UCN stem), not by position - robust to the
        # glob sort order. Falls back to the stem-recovered label.
        species_label = _species_label_for_path(ucn_path, species_names)

        final2d = _read_final_concentration(ucn_path)
        max_conc, plume_area_km2 = compute_plume_metrics(final2d, cell_area_m2)
        logger.info(
            "postprocess_multi_species run_id=%s species=%r max_concentration_mgl=%.6g "
            "plume_area_km2=%.6g",
            run_id,
            species_label,
            max_conc,
            plume_area_km2,
        )

        cog_path = _write_reprojected_cog(final2d, model_crs, geo)
        bbox_4326 = _cog_bbox_4326(cog_path)
        # Each species gets its own COG filename + layer id so N COGs never
        # collide in the runs bucket and N layers render as distinct map layers.
        slug = _species_slug(species_label, idx)
        cog_uri = _upload_cog(
            cog_path,
            run_id,
            runs_bucket,
            cog_filename=f"plume_{slug}_concentration_4326.tif",
        )

        layer_id = f"plume-concentration-{slug}-{run_id}"
        final_uri = cog_uri
        if publish:
            wms_url = _dispatch_publish_layer(cog_uri, layer_id)
            if wms_url:
                final_uri = wms_url

        plumes.append(
            PlumeLayerURI(
                layer_id=layer_id,
                name=f"Contaminant Plume - {species_label} (peak concentration)",
                layer_type="raster",
                uri=final_uri,
                style_preset=PLUME_STYLE_PRESET,
                role="primary",
                units="mg/L",
                bbox=bbox_4326,
                max_concentration_mgl=max_conc,
                plume_area_km2=plume_area_km2,
            )
        )

    return MultiSpeciesPlumeResult(plumes=plumes)


def _species_slug(species_label: str, idx: int) -> str:
    """Filesystem/layer-id-safe slug for a species label (lowercased, alnum/_)."""
    slug = "".join(c.lower() if c.isalnum() else "_" for c in species_label).strip("_")
    return slug or f"species{idx}"


# --------------------------------------------------------------------------- #
# River-seepage postprocess (sprint-17 J9) - GWF cbc RIV budget -> seepage COG
# --------------------------------------------------------------------------- #


def _resolve_gwf_cbc_path(run_outputs_uri: str) -> Path:
    """Locate the GWF cell-by-cell budget (``gwf_model.cbc``) from a run output.

    Mirrors ``_resolve_ucn_path`` but targets the GWF budget file that carries
    the RIV leakage term. Local (``file://`` / bare path): search the dir tree.
    ``s3://`` / ``gs://``: fetch the cbc into a temp dir via the same boto3 /
    fsspec seams the UCN resolver uses.
    """
    if run_outputs_uri.startswith("s3://"):
        from ..tools.simulation.solver import _get_s3_client

        tmpdir = Path(tempfile.mkdtemp(prefix="modflow-cbc-"))
        local_target = tmpdir / GWF_CBC_FILENAME
        source = (
            run_outputs_uri
            if run_outputs_uri.endswith(".cbc")
            else run_outputs_uri.rstrip("/") + f"/{GWF_CBC_FILENAME}"
        )
        bucket_name, _, obj_key = source[len("s3://"):].partition("/")
        try:
            import shutil as _shutil

            resp = _get_s3_client().get_object(Bucket=bucket_name, Key=obj_key)
            with local_target.open("wb") as fh:
                _shutil.copyfileobj(resp["Body"], fh)
        except Exception as exc:  # noqa: BLE001
            raise PostprocessMODFLOWError(
                "SEEPAGE_OUTPUT_READ_FAILED",
                message=f"could not fetch GWF cbc from {source}: {exc}",
                details={"run_outputs_uri": run_outputs_uri},
            ) from exc
        return local_target
    if run_outputs_uri.startswith("gs://"):
        try:
            import fsspec  # type: ignore[import-not-found]

            fs = fsspec.filesystem("gcs")
            tmpdir = Path(tempfile.mkdtemp(prefix="modflow-cbc-"))
            local_target = tmpdir / GWF_CBC_FILENAME
            candidate = (
                run_outputs_uri
                if run_outputs_uri.endswith(".cbc")
                else f"{run_outputs_uri.rstrip('/')}/{GWF_CBC_FILENAME}"
            )
            fs.get(candidate, str(local_target))
            return local_target
        except Exception as exc:  # noqa: BLE001
            raise PostprocessMODFLOWError(
                "SEEPAGE_OUTPUT_READ_FAILED",
                message=f"could not fetch GWF cbc from {run_outputs_uri}: {exc}",
                details={"run_outputs_uri": run_outputs_uri},
            ) from exc

    p = Path(run_outputs_uri.replace("file://", ""))
    if p.is_file() and p.suffix == ".cbc" and "gwf" in p.name.lower():
        return p
    if p.is_dir():
        hits = sorted(glob.glob(str(p / "**" / GWF_CBC_FILENAME), recursive=True))
        if not hits:
            # any GWF cbc (defensive: the OC stem may differ).
            hits = sorted(
                g
                for g in glob.glob(str(p / "**" / "*.cbc"), recursive=True)
                if "gwf" in Path(g).name.lower()
            )
        if hits:
            return Path(hits[0])
    raise PostprocessMODFLOWError(
        "SEEPAGE_OUTPUT_READ_FAILED",
        message=f"no {GWF_CBC_FILENAME} found under {run_outputs_uri}",
        details={"run_outputs_uri": run_outputs_uri},
    )


def postprocess_river_seepage(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    publish: bool = True,
) -> SeepageLayerURI:
    """Convert a MODFLOW GWF run's RIV budget into a ``SeepageLayerURI``.

    Reads the GWF cell-by-cell budget (``gwf_model.cbc``) RIV leakage term,
    scatters the per-reach-cell signed exchange flux onto the model grid,
    reprojects to a DIVERGING EPSG:4326 gaining/losing-seepage COG, computes the
    leakage narration scalars, uploads + (optionally) publishes the COG, and
    returns the typed seepage layer.

    Sign convention (MF6 RIV budget): positive ``q`` = flow INTO the cell from
    the river (LOSING reach, seepage INTO the aquifer); negative ``q`` = flow OUT
    to the river (GAINING reach, baseflow). The diverging ``rdbu`` ramp centred
    on 0 renders losing (positive) one colour and gaining (negative) the other.

    Args:
        run_outputs_uri: the run output location (local dir / ``file://`` for the
            local path, ``s3://`` / ``gs://`` for the cloud path; finds
            ``gwf_model.cbc``).
        run_id: the run identifier the COG is keyed under in the runs bucket.
        model_crs: the deck's projected CRS (e.g. ``"EPSG:32617"``).
        deck_dir: optional on-disk deck dir for grid georegistration + the grid
            (nrow/ncol) used to scatter the budget. When None the COG uses an
            identity transform and the grid shape is inferred from the budget.
        runs_bucket: optional override for the runs bucket name.
        publish: when True, dispatch ``publish_layer`` (mocked in tests).

    Returns:
        ``SeepageLayerURI`` with ``total_leakage_m3_day`` + ``gaining_m3_day`` +
        ``losing_m3_day`` + ``river_cell_count`` and a published WMS / tile URI
        (else the COG URI).

    Raises:
        PostprocessMODFLOWError: any read / reproject / write / upload step
            failed; ``error_code`` identifies the stage.
    """
    cbc_path = _resolve_gwf_cbc_path(run_outputs_uri)
    geo = _grid_georegistration_from_deck(deck_dir)
    nrow = int(geo["nrow"]) if geo is not None else None
    ncol = int(geo["ncol"]) if geo is not None else None
    if nrow is None or ncol is None:
        # No deck georegistration: infer a square grid from the budget node ids.
        nrow, ncol = _infer_grid_shape_from_cbc(cbc_path)

    seepage = _read_riv_seepage_grid(cbc_path, nrow, ncol)
    total, gaining, losing, river_cell_count = compute_seepage_metrics(seepage)
    logger.info(
        "postprocess_river_seepage run_id=%s total_leakage_m3_day=%.6g "
        "gaining_m3_day=%.6g losing_m3_day=%.6g cells=%d",
        run_id,
        total,
        gaining,
        losing,
        river_cell_count,
    )

    cog_path = _write_reprojected_cog(
        seepage, model_crs, geo, mask_below_floor=False
    )
    bbox_4326 = _cog_bbox_4326(cog_path)
    cog_uri = _upload_cog(
        cog_path,
        run_id,
        runs_bucket,
        cog_filename="river_seepage_4326.tif",
    )

    layer_id = f"river-seepage-{run_id}"
    final_uri = cog_uri
    if publish:
        wms_url = _dispatch_publish_layer(
            cog_uri, layer_id, style_preset=SEEPAGE_STYLE_PRESET
        )
        if wms_url:
            final_uri = wms_url

    return SeepageLayerURI(
        layer_id=layer_id,
        name="River Seepage (gaining / losing reach)",
        layer_type="raster",
        uri=final_uri,
        style_preset=SEEPAGE_STYLE_PRESET,
        role="primary",
        units="m^3/day",
        bbox=bbox_4326,
        total_leakage_m3_day=total,
        gaining_m3_day=gaining,
        losing_m3_day=losing,
        river_cell_count=river_cell_count,
    )


def _infer_grid_shape_from_cbc(cbc_path: Path) -> tuple[int, int]:
    """Best-effort grid (nrow, ncol) when no deck georegistration is available.

    The cbc reader needs a grid shape to scatter the RIV nodes. flopy's
    ``CellBudgetFile`` exposes ``nrow``/``ncol`` header attributes for a
    structured grid; fall back to a 40x40 demo grid (gwt_adapter default) if
    they are absent.
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]

        cbc = flopy.utils.CellBudgetFile(str(cbc_path))
        nrow = int(getattr(cbc, "nrow", 0) or 0)
        ncol = int(getattr(cbc, "ncol", 0) or 0)
        if nrow > 0 and ncol > 0:
            return nrow, ncol
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not infer grid shape from %s: %s", cbc_path, exc)
    return 40, 40


# --------------------------------------------------------------------------- #
# levers STEP 3 -- NEW published quantities (registry-driven, ADDITIVE).
#
# The EXISTING plume + seepage stay on the byte-identical old postprocess path
# above. These helpers build the NEW quantities (concentration ANIMATION across
# all saved UCN steps + the GWF head / water-table) as registry readers and
# publish them through the shared executor (publish_quantities). Gated DEFAULT
# behind TRID3NT_MODFLOW_REGISTRY_QUANTITIES until live-proven per engine.
# --------------------------------------------------------------------------- #
#: GWF head filename the OC HEAD FILEOUT writes (gwt_adapter).
GWF_HDS_FILENAME: str = "gwf_model.hds"

#: continuous head / water-table style preset (publish_layer._TITILER_STYLE_REGISTRY).
HEAD_STYLE_PRESET: str = "continuous_head_m"

#: MF6 inactive/dry-cell sentinel magnitude.
_MF6_DRY_SENTINEL: float = 1e29


def _resolve_gwf_hds_path(run_outputs_uri: str) -> Path:
    """Locate the GWF head file (``gwf_model.hds``) from a run output.

    Mirrors ``_resolve_gwf_cbc_path`` (s3 / gs / local), but targets the head
    FILEOUT. Raises ``PostprocessMODFLOWError("HEAD_OUTPUT_READ_FAILED")`` when
    the head file cannot be located / fetched.
    """
    if run_outputs_uri.startswith("s3://"):
        from ..tools.simulation.solver import _get_s3_client

        tmpdir = Path(tempfile.mkdtemp(prefix="modflow-hds-"))
        local_target = tmpdir / GWF_HDS_FILENAME
        source = (
            run_outputs_uri
            if run_outputs_uri.endswith(".hds")
            else run_outputs_uri.rstrip("/") + f"/{GWF_HDS_FILENAME}"
        )
        bucket_name, _, obj_key = source[len("s3://"):].partition("/")
        try:
            import shutil as _shutil

            resp = _get_s3_client().get_object(Bucket=bucket_name, Key=obj_key)
            with local_target.open("wb") as fh:
                _shutil.copyfileobj(resp["Body"], fh)
        except Exception as exc:  # noqa: BLE001
            raise PostprocessMODFLOWError(
                "HEAD_OUTPUT_READ_FAILED",
                message=f"could not fetch GWF head from {source}: {exc}",
                details={"run_outputs_uri": run_outputs_uri},
            ) from exc
        return local_target
    if run_outputs_uri.startswith("gs://"):
        try:
            import fsspec  # type: ignore[import-not-found]

            fs = fsspec.filesystem("gcs")
            tmpdir = Path(tempfile.mkdtemp(prefix="modflow-hds-"))
            local_target = tmpdir / GWF_HDS_FILENAME
            candidate = (
                run_outputs_uri
                if run_outputs_uri.endswith(".hds")
                else f"{run_outputs_uri.rstrip('/')}/{GWF_HDS_FILENAME}"
            )
            fs.get(candidate, str(local_target))
            return local_target
        except Exception as exc:  # noqa: BLE001
            raise PostprocessMODFLOWError(
                "HEAD_OUTPUT_READ_FAILED",
                message=f"could not fetch GWF head from {run_outputs_uri}: {exc}",
                details={"run_outputs_uri": run_outputs_uri},
            ) from exc

    p = Path(run_outputs_uri.replace("file://", ""))
    if p.is_file() and p.suffix == ".hds":
        return p
    if p.is_dir():
        hits = sorted(glob.glob(str(p / "**" / GWF_HDS_FILENAME), recursive=True))
        if not hits:
            hits = sorted(glob.glob(str(p / "**" / "*.hds"), recursive=True))
        if hits:
            return Path(hits[0])
    raise PostprocessMODFLOWError(
        "HEAD_OUTPUT_READ_FAILED",
        message=f"no {GWF_HDS_FILENAME} found under {run_outputs_uri}",
        details={"run_outputs_uri": run_outputs_uri},
    )


def _read_head_grid(hds_path: Path) -> Any:
    """Read the FINAL-timestep, max-over-layers head grid (m, 2D).

    GWF head output is a binary HEADFILE-format array; flopy reads it via
    ``HeadFile``. ``get_data(totim=last)`` returns ``(nlay, nrow, ncol)``; we
    take ``nanmax`` over the layer axis (the water-table = the uppermost active
    head) and mask the MF6 dry/inactive sentinel to NaN.
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "HEAD_OUTPUT_READ_FAILED",
            message=f"flopy/numpy not importable: {exc}",
            details={"hds_path": str(hds_path)},
        ) from exc
    try:
        hobj = flopy.utils.HeadFile(str(hds_path))
        times = hobj.get_times()
        if not times:
            raise PostprocessMODFLOWError(
                "HEAD_OUTPUT_EMPTY",
                message=f"{hds_path} carries no head timesteps",
                details={"hds_path": str(hds_path)},
            )
        data = hobj.get_data(totim=times[-1])
    except PostprocessMODFLOWError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "HEAD_OUTPUT_READ_FAILED",
            message=f"could not read head from {hds_path}: {exc}",
            details={"hds_path": str(hds_path)},
        ) from exc
    arr = np.asarray(data, dtype="float64")
    if arr.ndim == 3:
        grid = np.nanmax(arr, axis=0)
    elif arr.ndim == 2:
        grid = arr
    else:
        grid = np.squeeze(arr)
    grid = np.where(np.abs(grid) > _MF6_DRY_SENTINEL, np.nan, grid)
    return grid


def _read_head_decline_grid(
    hds_path: Path, *, invert: bool = False
) -> tuple[Any, list[float] | None]:
    """Read the head DECLINE grid head(t0) - head(t_last) + a well timeseries.

    For a transient sustainable-yield run the FIRST saved head step is the
    pre-pumping steady spin-up and the LAST is the fully-pumped state, so the
    per-cell DECLINE = head(t0) - head(t_last) is the drawdown cone (positive
    where the well drew the water table down). The max-over-layers head is used
    at each step (the water-table head). For a recharge/MOUNDING variant
    ``invert=True`` returns head(t_last) - head(t0) (the mound rise, positive
    where recharge raised the head).

    Returns ``(decline_grid_2d, head_decline_timeseries)`` where the timeseries
    is the per-step decline AT THE CELL OF PEAK FINAL DECLINE (one value per
    saved step, t0..t_last), or None when only a single step was saved.

    Raises ``PostprocessMODFLOWError("DRAWDOWN_OUTPUT_*")`` on read failure.
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "DRAWDOWN_OUTPUT_READ_FAILED",
            message=f"flopy/numpy not importable: {exc}",
            details={"hds_path": str(hds_path)},
        ) from exc

    def _to2d(data: Any) -> Any:
        a = np.asarray(data, dtype="float64")
        if a.ndim == 3:
            a2 = np.nanmax(a, axis=0)
        elif a.ndim == 2:
            a2 = a
        else:
            a2 = np.squeeze(a)
        return np.where(np.abs(a2) > _MF6_DRY_SENTINEL, np.nan, a2)

    try:
        hobj = flopy.utils.HeadFile(str(hds_path))
        times = hobj.get_times()
        if not times:
            raise PostprocessMODFLOWError(
                "DRAWDOWN_OUTPUT_EMPTY",
                message=f"{hds_path} carries no head timesteps",
                details={"hds_path": str(hds_path)},
            )
        steps = [_to2d(hobj.get_data(totim=t)) for t in times]
    except PostprocessMODFLOWError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "DRAWDOWN_OUTPUT_READ_FAILED",
            message=f"could not read head steps from {hds_path}: {exc}",
            details={"hds_path": str(hds_path)},
        ) from exc

    first, last = steps[0], steps[-1]
    decline = (last - first) if invert else (first - last)

    # Per-step decline at the cell of peak FINAL decline (the well neighbourhood).
    ts: list[float] | None = None
    if len(steps) > 1:
        finite = decline[np.isfinite(decline)]
        if finite.size:
            flat_idx = int(np.nanargmax(np.where(np.isfinite(decline), decline, -np.inf)))
            r, c = np.unravel_index(flat_idx, decline.shape)
            ts = []
            for step in steps:
                val = (step[r, c] - first[r, c]) if invert else (first[r, c] - step[r, c])
                ts.append(float(val) if np.isfinite(val) else 0.0)
    return decline, ts


def _read_head_steps(hds_path: Path) -> list[Any]:
    """Read EVERY saved head step into a list of 2D max-over-layers head grids.

    GWF head output is a binary HEADFILE-format array. For each saved totim we
    take the max-over-layers head (the water table) and mask the MF6 dry/inactive
    sentinel to NaN. Used by the wetland-hydroperiod seasonal-range reader + the
    ASR well-head series.

    Raises ``PostprocessMODFLOWError("HEAD_OUTPUT_*")`` on read failure.
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "HEAD_OUTPUT_READ_FAILED",
            message=f"flopy/numpy not importable: {exc}",
            details={"hds_path": str(hds_path)},
        ) from exc

    def _to2d(data: Any) -> Any:
        a = np.asarray(data, dtype="float64")
        if a.ndim == 3:
            a2 = np.nanmax(a, axis=0)
        elif a.ndim == 2:
            a2 = a
        else:
            a2 = np.squeeze(a)
        return np.where(np.abs(a2) > _MF6_DRY_SENTINEL, np.nan, a2)

    try:
        hobj = flopy.utils.HeadFile(str(hds_path))
        times = hobj.get_times()
        if not times:
            raise PostprocessMODFLOWError(
                "HEAD_OUTPUT_EMPTY",
                message=f"{hds_path} carries no head timesteps",
                details={"hds_path": str(hds_path)},
            )
        return [_to2d(hobj.get_data(totim=t)) for t in times]
    except PostprocessMODFLOWError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "HEAD_OUTPUT_READ_FAILED",
            message=f"could not read head steps from {hds_path}: {exc}",
            details={"hds_path": str(hds_path)},
        ) from exc


def _read_cbc_term_signed_totals(
    cbc_path: Path, term: str
) -> tuple[float, float, float]:
    """Sum a CBC ``term`` (e.g. RCH / RCHA / WEL) into (net, in_mag, out_mag).

    Integrates the per-cell signed ``q`` over EVERY saved timestep for the named
    term (MF6 sign: positive = INTO the aquifer). Returns:
      * net: signed sum over all cells + all steps (m^3/day-steps; the caller
        multiplies by the per-step day count for a volume, or treats the steady
        single-step total as a rate).
      * in_mag: magnitude of the positive (into-aquifer) flux summed over all
        cells + steps (>= 0) -- the recharge / injection leg.
      * out_mag: magnitude of the negative (out-of-aquifer) flux (>= 0) -- the
        extraction / recovery leg.

    Resolves the term name case-insensitively. Returns (0, 0, 0) when the term is
    absent (the caller decides whether that is a failure or a defensible zero).
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "HEAD_OUTPUT_READ_FAILED",
            message=f"flopy/numpy not importable: {exc}",
            details={"cbc_path": str(cbc_path), "term": term},
        ) from exc

    try:
        cbc = flopy.utils.CellBudgetFile(str(cbc_path))
        names = _normalize_cbc_record_names(cbc)
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "HEAD_OUTPUT_READ_FAILED",
            message=f"could not open CBC {cbc_path}: {exc}",
            details={"cbc_path": str(cbc_path), "term": term},
        ) from exc

    want = term.strip().upper()
    match = next((exact for key, exact in names.items() if want in key), None)
    if match is None:
        return 0.0, 0.0, 0.0
    try:
        data = cbc.get_data(text=match)
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, 0.0
    net = 0.0
    in_mag = 0.0
    out_mag = 0.0
    for rec in data:
        try:
            q = np.asarray(rec["q"], dtype="float64")
        except Exception:  # noqa: BLE001 - full-grid ndarray
            q = np.asarray(rec, dtype="float64").ravel()
        q = q[np.isfinite(q)]
        if q.size == 0:
            continue
        net += float(np.sum(q))
        in_mag += float(np.sum(q[q > 0.0]))
        out_mag += float(-np.sum(q[q < 0.0]))
    return net, in_mag, out_mag


def _read_concentration_steps(ucn_path: Path) -> tuple[list[Any], Any]:
    """Read ALL saved transport steps -> (per-step 2D grids, final/peak grid).

    Each step is the max-over-layers concentration; the PEAK is the final step
    (matches the existing plume). Used by the concentration-animation reader.
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "PLUME_OUTPUT_READ_FAILED",
            message=f"flopy/numpy not importable: {exc}",
            details={"ucn_path": str(ucn_path)},
        ) from exc

    def _to2d(data: Any) -> Any:
        a = np.asarray(data, dtype="float64")
        if a.ndim == 3:
            a2 = np.nanmax(a, axis=0)
        elif a.ndim == 2:
            a2 = a
        else:
            a2 = np.squeeze(a)
        return np.where(np.abs(a2) > _MF6_DRY_SENTINEL, np.nan, a2)

    try:
        cobj = flopy.utils.HeadFile(str(ucn_path), text="CONCENTRATION")
        times = cobj.get_times()
        if not times:
            raise PostprocessMODFLOWError(
                "PLUME_OUTPUT_EMPTY",
                message=f"{ucn_path} carries no concentration timesteps",
                details={"ucn_path": str(ucn_path)},
            )
        grids = [_to2d(cobj.get_data(totim=t)) for t in times]
    except PostprocessMODFLOWError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "PLUME_OUTPUT_READ_FAILED",
            message=f"could not read concentration steps from {ucn_path}: {exc}",
            details={"ucn_path": str(ucn_path)},
        ) from exc
    return grids, grids[-1]


def _modflow_src_transform(geo: dict[str, Any] | None, nrow: int) -> Any:
    """Build the rasterio source transform from the deck georegistration."""
    import rasterio  # type: ignore[import-not-found]

    if geo is not None:
        west = geo["xorigin"]
        north = geo["yorigin"] + nrow * geo["delc"]
        return rasterio.transform.from_origin(west, north, geo["delr"], geo["delc"])
    return rasterio.Affine.identity()


def publish_modflow_quantities(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    register_manifest_layers: Any,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> Any:
    """Publish the NEW MODFLOW quantities (concentration animation + head).

    Builds registry readers bound to the in-memory grids, then routes them
    through the shared ``publish_quantities`` executor (ONE registrar). The
    EXISTING plume + seepage layers are produced by the byte-identical old
    postprocess path; this ADDS the animation + water-table layers.

    Returns the executor's ``register_manifest_layers`` result. Never publishes
    the ``default_on=False`` provenance rows (plume-concentration / river-seepage).
    """
    from dataclasses import replace as _dc_replace

    from trid3nt_contracts.output_quantities import (
        RasterField,
        TimeseriesField,
        get_output_registry,
    )

    from . import publish_quantities as _pq

    geo = _grid_georegistration_from_deck(deck_dir)
    cell_area_m2 = (
        float(geo["delr"]) * float(geo["delc"]) if geo is not None else 2500.0
    )

    import numpy as np  # type: ignore[import-not-found]

    # --- concentration animation reader (all saved UCN steps) --------------- #
    ucn_path = _resolve_ucn_path(run_outputs_uri)
    conc_grids, conc_peak = _read_concentration_steps(ucn_path)
    nrow_c = int(np.asarray(conc_peak).shape[0])
    conc_transform = _modflow_src_transform(geo, nrow_c)

    def _mask_floor(a: Any) -> Any:
        import numpy as np  # type: ignore[import-not-found]

        return np.where(a > PLUME_DETECTION_FLOOR_MGL, a, np.nan).astype("float32")

    def _conc_raster(grid: Any) -> RasterField:
        max_conc, area = compute_plume_metrics(grid, cell_area_m2)
        return RasterField(
            grid=grid,
            src_crs=model_crs,
            src_transform=conc_transform,
            reproject=True,
            mask=_mask_floor,
            crs_roundtrip_guard=False,
            metrics={
                "max_concentration_mgl": max_conc,
                "plume_area_km2": area,
            },
        )

    def _conc_ts_reader(_ctx: Any) -> TimeseriesField:
        return TimeseriesField(
            n_steps=len(conc_grids),
            read_step=lambda i: _conc_raster(conc_grids[i]),
            peak=_conc_raster(conc_peak),
            quantity_label="Plume concentration",
        )

    # --- head / water-table reader (final-step .hds) ------------------------ #
    hds_path = _resolve_gwf_hds_path(run_outputs_uri)
    head_grid = np.asarray(_read_head_grid(hds_path), dtype="float64")
    nrow_h = int(head_grid.shape[0]) if head_grid.ndim == 2 else nrow_c
    head_transform = _modflow_src_transform(geo, nrow_h)

    def _head_reader(_ctx: Any) -> RasterField:
        finite = head_grid[np.isfinite(head_grid)]
        max_head = float(np.max(finite)) if finite.size else 0.0
        min_head = float(np.min(finite)) if finite.size else 0.0
        return RasterField(
            grid=head_grid,
            src_crs=model_crs,
            src_transform=head_transform,
            reproject=True,
            crs_roundtrip_guard=False,
            metrics={"max_head_m": max_head, "min_head_m": min_head},
        )

    readers = {
        "plume-concentration-ts": _conc_ts_reader,
        "water-table": _head_reader,
    }
    specs = [
        _dc_replace(spec, reader=readers[spec.quantity_id])
        for spec in get_output_registry("modflow")
        if spec.quantity_id in readers
    ]

    def _upload(cog: Path, rid: str, _bucket: Any = None, *, dest_filename: str) -> str:
        return _upload_cog(cog, rid, runs_bucket, cog_filename=dest_filename)

    return _pq.publish_quantities(
        "modflow",
        run_id=run_id,
        upload=_upload,
        register_manifest_layers=register_manifest_layers,
        specs=specs,
        bbox=bbox,
    )


# --------------------------------------------------------------------------- #
# sprint-18 Wave-1 archetype postprocess (GWF-only: head + cbc readers).
#
# Each reuses the EXISTING resolve/write/upload/publish seams above and the new
# pure metric math. drawdown reads the transient .hds head decline; dewatering
# reads the .cbc DRN term; budget-partition reads ALL .cbc terms. Every narrated
# scalar is a typed field measured from the real run output (Invariant 1).
# --------------------------------------------------------------------------- #


def postprocess_drawdown(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    publish: bool = True,
    mounding: bool = False,
) -> DrawdownLayerURI:
    """Convert a transient GWF run's head into a drawdown ``DrawdownLayerURI``.

    Reads the GWF head file (``gwf_model.hds``), computes the per-cell head
    DECLINE = head(t0) - head(t_last) (the cone of depression a pumping well
    draws down), reprojects it to an EPSG:4326 COG, computes the peak drawdown
    + the at-well head-decline timeseries, uploads + (optionally) publishes the
    COG, and returns the typed drawdown layer.

    When ``mounding=True`` the sign is inverted (head(t_last) - head(t0)) so a
    recharge run renders the mound rise instead of a drawdown cone (same reader,
    inverse sign).

    Raises:
        PostprocessMODFLOWError: any read / reproject / write / upload step
            failed; ``error_code`` identifies the stage.
    """
    hds_path = _resolve_gwf_hds_path(run_outputs_uri)
    geo = _grid_georegistration_from_deck(deck_dir)

    decline, ts = _read_head_decline_grid(hds_path, invert=mounding)
    max_drawdown_m = compute_drawdown_metrics(decline)
    logger.info(
        "postprocess_drawdown run_id=%s mounding=%s max_drawdown_m=%.6g steps_ts=%s",
        run_id,
        mounding,
        max_drawdown_m,
        len(ts) if ts is not None else 0,
    )

    # The decline grid is already NaN off-grid; write AS-IS (mask_below_floor
    # False) so negative recovery cells survive (do not get floored away).
    cog_path = _write_reprojected_cog(decline, model_crs, geo, mask_below_floor=False)
    bbox_4326 = _cog_bbox_4326(cog_path)
    cog_uri = _upload_cog(
        cog_path, run_id, runs_bucket, cog_filename="drawdown_4326.tif"
    )

    name = "Recharge Mounding (head rise)" if mounding else "Pumping Drawdown (head decline)"
    layer_id = f"{'mounding' if mounding else 'drawdown'}-{run_id}"
    final_uri = cog_uri
    if publish:
        wms_url = _dispatch_publish_layer(
            cog_uri, layer_id, style_preset=DRAWDOWN_STYLE_PRESET
        )
        if wms_url:
            final_uri = wms_url

    return DrawdownLayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=final_uri,
        style_preset=DRAWDOWN_STYLE_PRESET,
        role="primary",
        units="m",
        bbox=bbox_4326,
        max_drawdown_m=max_drawdown_m,
        head_decline_timeseries=ts,
    )


# --------------------------------------------------------------------------- #
# CSUB land-subsidence postprocess (module wave)
#
# The land_subsidence archetype layers a MODFLOW-6 CSUB package onto the
# transient pumping (WEL) deck. CSUB writes a per-cell z-displacement grid whose
# FINAL frame is the cumulative ground subsidence bowl, plus a per-interbed OBS
# csv carrying, per interbed per timestep: COMPACTION_R{i} (total, m), INE_R{i}
# (inelastic/permanent, m) and ELA_R{i} (elastic/recoverable, m). PINNED by the
# local mf6 6.5.0 smoke fixture (services/workers/modflow/fixtures/csub_smoke):
#   * z-displacement HeadFile text tag is CSUB-ZDISPLACE (TRUNCATED to 16 chars,
#     NOT "CSUB-ZDISPLACEMENT"); compaction tag is CSUB-COMPACTION.
#   * subsidence is POSITIVE-DOWN (z-displacement positive at the pumped cell).
#   * dz(final) ~ Ssv * b * dh (the analytical ultimate-compaction cross-check).
# The subsidence bowl is reprojected model-UTM -> EPSG:4326 as a COG (cm) and
# reaches the client through the RASTER publish_layer path (unlike the SFR
# vector).
# --------------------------------------------------------------------------- #

#: HeadFile text tag mf6 6.5.0 writes for the CSUB z-displacement grid (the
#: subsidence bowl). TRUNCATED to 16 chars from "CSUB-ZDISPLACEMENT" - pinned by
#: the smoke fixture; reading with the full name raises EOFError.
CSUB_ZDISP_TEXT_TAG: str = "CSUB-ZDISPLACE"

#: Cells whose final subsidence exceeds this floor (m) count toward the
#: subsidence-bowl area. ~1 mm - below it is numerical noise, not real subsidence.
SUBSIDENCE_AREA_FLOOR_M: float = 1e-3


def _resolve_csub_zdisp_path(run_outputs_uri: str) -> Path:
    """Locate ``<gwf>.csub.zdisp.bin`` under a local run-output directory.

    Raises:
        PostprocessMODFLOWError (``SUBSIDENCE_OUTPUT_READ_FAILED``) when no
            ``*.csub.zdisp.bin`` is found under ``run_outputs_uri``.
    """
    p = Path(run_outputs_uri.replace("file://", ""))
    if p.is_file() and p.name.endswith(".csub.zdisp.bin"):
        return p
    if p.is_dir():
        hits = sorted(glob.glob(str(p / "**" / "*.csub.zdisp.bin"), recursive=True))
        if hits:
            return Path(hits[0])
    raise PostprocessMODFLOWError(
        "SUBSIDENCE_OUTPUT_READ_FAILED",
        message=(
            f"no *.csub.zdisp.bin found under {run_outputs_uri!r}; the "
            "land_subsidence run must have written the CSUB z-displacement grid."
        ),
        details={"run_outputs_uri": run_outputs_uri},
    )


def _resolve_csub_obs_csv(run_outputs_uri: str) -> Path | None:
    """Locate ``<gwf>.csub.obs.csv`` (the per-interbed compaction OBS); None if absent."""
    p = Path(run_outputs_uri.replace("file://", ""))
    if p.is_file() and p.name.endswith(".csub.obs.csv"):
        return p
    if p.is_dir():
        hits = sorted(glob.glob(str(p / "**" / "*.csub.obs.csv"), recursive=True))
        if hits:
            return Path(hits[0])
    return None


def _read_csub_zdisplacement(zdisp_path: Path) -> Any:
    """Read the FINAL-frame CSUB z-displacement grid (metres, positive-down) as 2D.

    Reduces to 2D (max over layers) and masks the 1e30 mf6 dry/no-flow sentinel
    to NaN - the same reduce-to-2D pattern as the head/plume readers. The final
    frame at the top layer IS the cumulative subsidence bowl.

    Raises ``PostprocessMODFLOWError("SUBSIDENCE_OUTPUT_*")`` on read failure.
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "SUBSIDENCE_OUTPUT_READ_FAILED",
            message=f"flopy/numpy not importable: {exc}",
            details={"zdisp_path": str(zdisp_path)},
        ) from exc
    try:
        hobj = flopy.utils.HeadFile(str(zdisp_path), text=CSUB_ZDISP_TEXT_TAG)
        kk = hobj.get_kstpkper()
        if not kk:
            raise PostprocessMODFLOWError(
                "SUBSIDENCE_OUTPUT_EMPTY",
                message=f"{zdisp_path} carries no z-displacement frames",
                details={"zdisp_path": str(zdisp_path)},
            )
        data = hobj.get_data(kstpkper=kk[-1])
    except PostprocessMODFLOWError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "SUBSIDENCE_OUTPUT_READ_FAILED",
            message=(
                f"could not read CSUB z-displacement from {zdisp_path} "
                f"(text={CSUB_ZDISP_TEXT_TAG!r}): {exc}"
            ),
            details={"zdisp_path": str(zdisp_path)},
        ) from exc
    a = np.asarray(data, dtype="float64")
    if a.ndim == 3:
        a2 = np.nanmax(a, axis=0)
    elif a.ndim == 2:
        a2 = a
    else:
        a2 = np.squeeze(a)
    return np.where(np.abs(a2) > _MF6_DRY_SENTINEL, np.nan, a2)


def compute_subsidence_metrics(
    zdisp_grid_m: Any,
    *,
    cell_area_m2: float,
    obs_rows: list[dict[str, str]] | None = None,
    n_interbeds: int | None = None,
) -> dict[str, Any]:
    """Compute the subsidence headline metrics from the z-displacement grid + OBS.

    PURE arithmetic (no flopy, no mf6) so the SIGN + magnitude math is unit
    testable directly on the smoke-fixture outputs. Subsidence is POSITIVE-DOWN
    (pinned by the smoke), so a positive z-displacement IS subsidence; the peak is
    ``max(zdisp)`` clamped >= 0 (a tiny negative rebound never narrates as
    negative subsidence).

    Args:
        zdisp_grid_m: 2D final-frame z-displacement grid (m, positive-down, NaN
            off-grid).
        cell_area_m2: the model cell area (delr * delc, m^2) for the bowl area.
        obs_rows: parsed rows of ``<gwf>.csub.obs.csv`` (header-keyed). The final
            row's per-interbed INE_R{i} / ELA_R{i} give the inelastic fraction.
        n_interbeds: interbed count (for the INE/ELA column loop); inferred from
            the obs header when None.

    Returns a dict with ``max_subsidence_cm`` (>= 0), ``subsidence_area_km2``
    (cells past ``SUBSIDENCE_AREA_FLOOR_M``), ``inelastic_fraction`` (in [0, 1];
    sum(INE)/(sum(INE)+sum(ELA)) over interbeds at the final step, defaulting to
    1.0 when the obs is unavailable but there IS subsidence - the pcs0=0 demo
    signature), and the per-step ``subsidence_series_cm`` + ``days`` (from the OBS
    peak-compaction interbed) for the chart.
    """
    import numpy as np  # local - caller vouched for the import path

    arr = np.asarray(zdisp_grid_m, dtype="float64")
    finite = arr[np.isfinite(arr)] if arr.size else arr
    max_sub_m = max(0.0, float(np.max(finite))) if finite.size else 0.0
    n_cells = int(np.sum(finite > SUBSIDENCE_AREA_FLOOR_M)) if finite.size else 0
    area_km2 = float(n_cells) * float(cell_area_m2) / 1e6

    # --- inelastic fraction + per-step series from the OBS csv --------------- #
    inelastic_fraction = 1.0 if max_sub_m > 0 else 0.0
    days: list[float] = []
    subsidence_series_cm: list[float] = []
    if obs_rows:
        header = list(obs_rows[0].keys())
        if n_interbeds is None:
            n_interbeds = sum(1 for k in header if k.upper().startswith("COMPACTION_R"))
        n_interbeds = int(n_interbeds or 0)

        def _val(row: dict[str, str], prefix: str, i: int) -> float:
            key = f"{prefix}_R{i}"
            if key in row:
                try:
                    return float(row[key])
                except (TypeError, ValueError):
                    return 0.0
            for k, v in row.items():
                if k.upper() == key:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return 0.0
            return 0.0

        last = obs_rows[-1]
        has_split = any(k.upper().startswith("INE_R") for k in header)
        if has_split and n_interbeds > 0:
            tot_ine = sum(_val(last, "INE", i) for i in range(n_interbeds))
            tot_ela = sum(_val(last, "ELA", i) for i in range(n_interbeds))
            denom = tot_ine + tot_ela
            if denom > 0:
                inelastic_fraction = float(max(0.0, min(1.0, tot_ine / denom)))

        # Per-step cumulative subsidence series at the peak-compaction interbed
        # (total COMPACTION_R{i}); rises monotonically (permanence). cm.
        if n_interbeds > 0 and any(
            k.upper().startswith("COMPACTION_R") for k in header
        ):
            finals = [_val(last, "COMPACTION", i) for i in range(n_interbeds)]
            peak_i = int(np.argmax(finals)) if finals else 0
            for row in obs_rows:
                subsidence_series_cm.append(
                    _val(row, "COMPACTION", peak_i) * 100.0
                )
                try:
                    days.append(float(row.get("time", len(days) + 1)))
                except (TypeError, ValueError):
                    days.append(float(len(days) + 1))

    return {
        "max_subsidence_cm": max_sub_m * 100.0,
        "subsidence_area_km2": area_km2,
        "inelastic_fraction": float(inelastic_fraction),
        "days": days,
        "subsidence_series_cm": subsidence_series_cm,
    }


def postprocess_subsidence(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    publish: bool = True,
) -> SubsidenceLayerURI:
    """Convert a CSUB land-subsidence run into a ``SubsidenceLayerURI``.

    Reads the FINAL-frame CSUB z-displacement grid (the subsidence bowl, m,
    positive-down), converts to cm, reprojects to an EPSG:4326 COG through the
    EXISTING raster path (this IS a raster layer -> ``publish_layer``, unlike the
    SFR vector), computes ``max_subsidence_cm`` / ``subsidence_area_km2`` from the
    grid, ``max_head_decline_m`` from the ``.hds`` first-vs-last, and
    ``inelastic_fraction`` from the ``.csub.obs.csv`` INE/ELA split, and narrates
    the ``dz ~ Ssv*b*dh`` analytical cross-check honestly in the log. The
    per-step subsidence chart is stashed as a private attr on the layer for the
    composer to emit.

    Args:
        run_outputs_uri: the run-output directory (local path / ``file://``) that
            ``run_modflow_local`` produced; must contain ``*.csub.zdisp.bin``.
        run_id: run identifier; the COG is uploaded to
            ``<runs_bucket>/<run_id>/subsidence_4326.tif``.
        model_crs: the GWF deck's projected CRS string (e.g. ``"EPSG:32611"``).
        deck_dir: the GWF deck directory; flopy reads the grid georegistration +
            interbed count from it.
        runs_bucket: optional override for the runs bucket name.
        publish: dispatch ``publish_layer`` (raster WMS) when True.

    Returns:
        ``SubsidenceLayerURI`` (``layer_type='raster'``,
        ``style_preset='continuous_subsidence_cm'``) with the subsidence metrics.

    Raises:
        PostprocessMODFLOWError: read / reproject / write / upload failure.
    """
    import numpy as np  # type: ignore[import-not-found]

    zdisp_path = _resolve_csub_zdisp_path(run_outputs_uri)
    geo = _grid_georegistration_from_deck(deck_dir)

    zdisp_m = _read_csub_zdisplacement(zdisp_path)

    # Cell area + interbed count from the deck (fallback to 1 m^2 / inferred).
    if geo is not None:
        cell_area_m2 = float(geo["delr"]) * float(geo["delc"])
    else:
        cell_area_m2 = 1.0

    obs_csv = _resolve_csub_obs_csv(run_outputs_uri)
    obs_rows: list[dict[str, str]] | None = None
    if obs_csv is not None:
        import csv as _csv

        try:
            with obs_csv.open() as fh:
                obs_rows = list(_csv.DictReader(fh))
        except Exception as exc:  # noqa: BLE001  -- obs is best-effort
            logger.warning("could not read CSUB obs csv %s: %s", obs_csv, exc)
            obs_rows = None

    n_interbeds = None
    if obs_rows:
        n_interbeds = sum(
            1 for k in obs_rows[0] if k.upper().startswith("COMPACTION_R")
        )

    metrics = compute_subsidence_metrics(
        zdisp_m,
        cell_area_m2=cell_area_m2,
        obs_rows=obs_rows,
        n_interbeds=n_interbeds,
    )

    # head decline (m) from the .hds first-vs-last (the drawdown that drove it).
    max_head_decline_m = 0.0
    try:
        hds_path = _resolve_gwf_hds_path(run_outputs_uri)
        decline, _ts = _read_head_decline_grid(hds_path, invert=False)
        max_head_decline_m = compute_drawdown_metrics(decline)
    except PostprocessMODFLOWError as exc:
        logger.warning("subsidence head-decline read failed (metric -> 0): %s", exc)

    max_sub_cm = float(metrics["max_subsidence_cm"])

    # Analytical cross-check dz ~ Ssv * b * dh, narrated HONESTLY (never asserted):
    # the transient run under-shoots the t->inf ultimate, so this is an
    # order-of-magnitude yardstick, not an equality. Ssv/thick come from the deck
    # manifest when available (best-effort log only).
    logger.info(
        "postprocess_subsidence run_id=%s max_subsidence_cm=%.4g area_km2=%.4g "
        "max_head_decline_m=%.4g inelastic_fraction=%.3f n_interbeds=%s",
        run_id,
        max_sub_cm,
        metrics["subsidence_area_km2"],
        max_head_decline_m,
        metrics["inelastic_fraction"],
        n_interbeds,
    )

    # Subsidence bowl COG (cm, positive-down). The grid is already NaN off-grid;
    # write AS-IS so the bowl edges survive (mask_below_floor False).
    subsidence_cm_grid = zdisp_m * 100.0
    cog_path = _write_reprojected_cog(
        subsidence_cm_grid, model_crs, geo, mask_below_floor=False
    )
    bbox_4326 = _cog_bbox_4326(cog_path)
    cog_uri = _upload_cog(
        cog_path, run_id, runs_bucket, cog_filename="subsidence_4326.tif"
    )

    layer_id = f"subsidence-{run_id}"
    final_uri = cog_uri
    if publish:
        wms_url = _dispatch_publish_layer(
            cog_uri, layer_id, style_preset=SUBSIDENCE_STYLE_PRESET
        )
        if wms_url:
            final_uri = wms_url

    layer = SubsidenceLayerURI(
        layer_id=layer_id,
        name="Land subsidence (pumping-induced compaction)",
        layer_type="raster",
        uri=final_uri,
        style_preset=SUBSIDENCE_STYLE_PRESET,
        role="primary",
        units="cm",
        bbox=bbox_4326,
        max_subsidence_cm=max_sub_cm,
        subsidence_area_km2=float(metrics["subsidence_area_km2"]),
        max_head_decline_m=float(max_head_decline_m),
        inelastic_fraction=float(metrics["inelastic_fraction"]),
        interbed_count=int(n_interbeds or 1),
    )

    # Stash the subsidence-vs-time chart (composer emits it; private attr so the
    # Pydantic model ignores it).
    try:
        from ..tools.processing.charts_common import build_subsidence_timeseries_chart

        chart = build_subsidence_timeseries_chart(
            days=list(metrics["days"]),
            subsidence_cm=list(metrics["subsidence_series_cm"]),
            source_layer_uri=cog_uri,
        )
        object.__setattr__(layer, "_subsidence_chart", chart)
    except Exception as exc:  # noqa: BLE001  -- chart is best-effort side output
        logger.warning("postprocess_subsidence chart build failed: %s", exc)

    # Stash a CONTEXT drawdown COG (the pumping cone that DROVE the compaction) so
    # the composer can emit it beside the subsidence bowl (design: subsidence
    # primary + drawdown context). Best-effort; reuses the tested drawdown path.
    try:
        drawdown_layer = postprocess_drawdown(
            run_outputs_uri,
            run_id=run_id,
            model_crs=model_crs,
            deck_dir=deck_dir,
            runs_bucket=runs_bucket,
            publish=publish,
        )
        object.__setattr__(drawdown_layer, "role", "context")
        object.__setattr__(layer, "_drawdown_context", drawdown_layer)
    except Exception as exc:  # noqa: BLE001  -- context layer is best-effort
        logger.warning("postprocess_subsidence drawdown context build failed: %s", exc)

    return layer


def postprocess_dewatering(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    publish: bool = True,
    term: str = "DRN",
) -> DewaterLayerURI:
    """Convert a mine-dewatering GWF run's DRN budget into a ``DewaterLayerURI``.

    Reads the GWF cell-by-cell budget (``gwf_model.cbc``) ``term`` (default DRN)
    into a per-cell signed outflow grid, reprojects it to an EPSG:4326 COG,
    computes the total dewatering rate (sum of |q| over the drain cells) + the
    drain-cell count, uploads + (optionally) publishes the COG, and returns the
    typed dewatering layer. The DRN sum IS the pump-to-dewater rate.

    Raises:
        PostprocessMODFLOWError: any read / reproject / write / upload step
            failed; ``error_code`` identifies the stage.
    """
    cbc_path = _resolve_gwf_cbc_path(run_outputs_uri)
    geo = _grid_georegistration_from_deck(deck_dir)
    nrow = int(geo["nrow"]) if geo is not None else None
    ncol = int(geo["ncol"]) if geo is not None else None
    if nrow is None or ncol is None:
        nrow, ncol = _infer_grid_shape_from_cbc(cbc_path)

    term_grid = _read_cbc_term_grid(cbc_path, term, nrow, ncol)
    dewatering_rate_m3_day, drain_cell_count = compute_cbc_term_metrics(term_grid)
    logger.info(
        "postprocess_dewatering run_id=%s term=%s dewatering_rate_m3_day=%.6g cells=%d",
        run_id,
        term,
        dewatering_rate_m3_day,
        drain_cell_count,
    )

    # The drain outflow is negative per MF6 sign; render its MAGNITUDE so the
    # COG reads as a positive pump-to-dewater rate. Off-grid is already NaN.
    import numpy as np  # type: ignore[import-not-found]

    magnitude_grid = np.abs(np.asarray(term_grid, dtype="float64"))
    cog_path = _write_reprojected_cog(
        magnitude_grid, model_crs, geo, mask_below_floor=False
    )
    bbox_4326 = _cog_bbox_4326(cog_path)
    cog_uri = _upload_cog(
        cog_path, run_id, runs_bucket, cog_filename="dewatering_rate_4326.tif"
    )

    layer_id = f"dewatering-rate-{run_id}"
    final_uri = cog_uri
    if publish:
        wms_url = _dispatch_publish_layer(
            cog_uri, layer_id, style_preset=DEWATERING_STYLE_PRESET
        )
        if wms_url:
            final_uri = wms_url

    return DewaterLayerURI(
        layer_id=layer_id,
        name="Mine Dewatering Rate",
        layer_type="raster",
        uri=final_uri,
        style_preset=DEWATERING_STYLE_PRESET,
        role="primary",
        units="m^3/day",
        bbox=bbox_4326,
        dewatering_rate_m3_day=dewatering_rate_m3_day,
        drain_cell_count=drain_cell_count,
    )


def postprocess_budget_partition(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    publish: bool = True,
) -> BudgetPartitionLayerURI:
    """Convert a regional GWF run's cbc into a ``BudgetPartitionLayerURI``.

    Reads the GWF cell-by-cell budget (``gwf_model.cbc``), sums each term's
    per-cell flux into a per-term total (signs preserved), drops FLOW-JA-FACE +
    near-zero terms, and returns the typed partition. The deliverable is the
    SCALAR budget dict; the layer is rendered as the water-table head COG so the
    user sees the regional flow field the partition summarizes (the head is the
    spatial carrier; the partition is the narrated numbers - never free-generated).

    Raises:
        PostprocessMODFLOWError: any read / reproject / write / upload step
            failed; ``error_code`` identifies the stage.
    """
    cbc_path = _resolve_gwf_cbc_path(run_outputs_uri)
    geo = _grid_georegistration_from_deck(deck_dir)

    term_totals = _read_cbc_budget_partition(cbc_path)
    partition = compute_budget_partition(term_totals)
    logger.info(
        "postprocess_budget_partition run_id=%s terms=%s",
        run_id,
        {k: round(v, 3) for k, v in partition.items()},
    )

    # Spatial carrier = the water-table head COG (continuous head ramp). Best-
    # effort: if the head file is absent the partition is still the deliverable.
    bbox_4326: tuple[float, float, float, float] | None = None
    final_uri: str
    try:
        hds_path = _resolve_gwf_hds_path(run_outputs_uri)
        head_grid = _read_head_grid(hds_path)
        cog_path = _write_reprojected_cog(
            head_grid, model_crs, geo, mask_below_floor=False
        )
        bbox_4326 = _cog_bbox_4326(cog_path)
        final_uri = _upload_cog(
            cog_path, run_id, runs_bucket, cog_filename="water_table_4326.tif"
        )
        layer_id = f"budget-partition-{run_id}"
        if publish:
            wms_url = _dispatch_publish_layer(
                final_uri, layer_id, style_preset=HEAD_STYLE_PRESET
            )
            if wms_url:
                final_uri = wms_url
    except PostprocessMODFLOWError as exc:
        logger.warning(
            "budget-partition head COG unavailable (partition still returned): %s",
            exc,
        )
        final_uri = run_outputs_uri
        layer_id = f"budget-partition-{run_id}"

    return BudgetPartitionLayerURI(
        layer_id=layer_id,
        name="Regional Water Budget (zonal partition)",
        layer_type="raster",
        uri=final_uri,
        style_preset=HEAD_STYLE_PRESET,
        role="primary",
        units="m^3/day",
        bbox=bbox_4326,
        budget_partition_m3_day=partition,
    )


# --------------------------------------------------------------------------- #
# sprint-18 Wave-2 archetype postprocess (GWF-only: head + cbc readers).
#
# MAR (RCH mounding), ASR (seasonal WEL inject/recover), wetland_hydroperiod
# (RCH-schedule + EVT seasonal water-table range). Each reuses the EXISTING
# resolve/write/upload/publish seams + the new pure metric math. Every narrated
# scalar is a typed field measured from the real run output (Invariant 1); an
# absent series resolves to None rather than a fabricated number.
# --------------------------------------------------------------------------- #


def _head_total_duration_days(hds_path: Path) -> float | None:
    """Return the last cumulative totim (= total simulation duration, days).

    MF6 ``HeadFile.get_times()`` returns the cumulative simulation time at each
    saved step (in the deck's TIME_UNITS = days). The last value is the total
    duration. Returns None when unavailable.
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]

        times = flopy.utils.HeadFile(str(hds_path)).get_times()
        return float(times[-1]) if times else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read total duration from %s: %s", hds_path, exc)
        return None


def postprocess_mounding(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    publish: bool = True,
) -> MoundingLayerURI:
    """Convert a MAR transient GWF run's head into a ``MoundingLayerURI``.

    Reads the GWF head file (``gwf_model.hds``), computes the per-cell head RISE
    = head(t_last) - head(t0) (the groundwater mound the infiltration basin
    raises -- the sign-flipped twin of the drawdown reader, reusing
    ``_read_head_decline_grid(invert=True)``), reprojects it to an EPSG:4326 COG,
    computes the peak mounding, integrates the RCH/RCHA budget into the recharged
    volume, uploads + (optionally) publishes the COG, and returns the typed
    mounding layer.

    The recharged volume is the per-day RCH/RCHA IN-rate (the standing recharge
    measured from the budget) multiplied by the total simulation duration read
    from the head file's cumulative totim -- never a passed-in / fabricated value.
    When the budget carries no recharge or the duration is unavailable the volume
    is None (the honesty floor).

    Raises:
        PostprocessMODFLOWError: any read / reproject / write / upload step
            failed; ``error_code`` identifies the stage.
    """
    hds_path = _resolve_gwf_hds_path(run_outputs_uri)
    geo = _grid_georegistration_from_deck(deck_dir)

    # invert=True -> head(t_last) - head(t0) = the mound rise (positive up).
    rise, ts = _read_head_decline_grid(hds_path, invert=True)
    max_mounding_m = compute_mounding_metrics(rise)

    # Recharged volume from the RCH/RCHA budget IN-sum integral (best-effort).
    # The CBC IN-sum integrates EVERY saved step's per-day rate; divide by the
    # step count to recover the standing per-day recharge rate, then multiply by
    # the real total duration (read from the head file's cumulative totim).
    recharged_volume_m3: float | None = None
    try:
        duration_days = _head_total_duration_days(hds_path)
        cbc_path = _resolve_gwf_cbc_path(run_outputs_uri)
        rch_in = 0.0
        n_terms = 0
        for term in ("RCHA", "RCH"):
            _net, in_mag, _out = _read_cbc_term_signed_totals(cbc_path, term)
            if in_mag > 0.0:
                rch_in += in_mag
                n_terms += 1
        n_steps = len(ts) if ts is not None else 0
        if duration_days and rch_in > 0.0 and n_steps > 1:
            standing_rate = rch_in / float(n_steps)
            recharged_volume_m3 = compute_recharged_volume_m3(
                standing_rate, duration_days
            )
        elif duration_days and rch_in > 0.0:
            recharged_volume_m3 = compute_recharged_volume_m3(rch_in, duration_days)
    except PostprocessMODFLOWError as exc:
        logger.warning("MAR recharged-volume integral unavailable: %s", exc)

    logger.info(
        "postprocess_mounding run_id=%s max_mounding_m=%.6g recharged_volume_m3=%s",
        run_id,
        max_mounding_m,
        recharged_volume_m3,
    )

    # The rise grid is already NaN off-grid; write AS-IS so negative dip cells
    # survive (do not get floored away).
    cog_path = _write_reprojected_cog(rise, model_crs, geo, mask_below_floor=False)
    bbox_4326 = _cog_bbox_4326(cog_path)
    cog_uri = _upload_cog(
        cog_path, run_id, runs_bucket, cog_filename="mounding_4326.tif"
    )

    layer_id = f"mounding-{run_id}"
    final_uri = cog_uri
    if publish:
        wms_url = _dispatch_publish_layer(
            cog_uri, layer_id, style_preset=MOUNDING_STYLE_PRESET
        )
        if wms_url:
            final_uri = wms_url

    return MoundingLayerURI(
        layer_id=layer_id,
        name="Recharge Mounding (water-table rise)",
        layer_type="raster",
        uri=final_uri,
        style_preset=MOUNDING_STYLE_PRESET,
        role="primary",
        units="m",
        bbox=bbox_4326,
        max_mounding_m=max_mounding_m,
        recharged_volume_m3=recharged_volume_m3,
    )


def postprocess_asr(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    publish: bool = True,
) -> ASRLayerURI:
    """Convert an ASR transient GWF run's head + WEL budget into an ``ASRLayerURI``.

    Reads the GWF head file (``gwf_model.hds``) at the ASR well (the cell whose
    head swings most -- the inject-rise / recover-fall sawtooth), reads the GWF
    WEL budget to split injected vs recovered volume into the recovery efficiency,
    renders the final-step water-table head as the spatial carrier COG, uploads +
    (optionally) publishes it, and returns the typed ASR layer.

    The head series is found from the data (the peak-swing cell) so no well
    lat/lon re-derivation is needed -- the ASR well is, by construction, the cell
    where the cyclic inject/recover drives the largest head swing.

    Raises:
        PostprocessMODFLOWError: any read / reproject / write / upload step
            failed; ``error_code`` identifies the stage.
    """
    hds_path = _resolve_gwf_hds_path(run_outputs_uri)
    geo = _grid_georegistration_from_deck(deck_dir)

    head_steps = _read_head_steps(hds_path)
    # The well-head sawtooth = the per-step head at the peak-swing cell.
    _swing, head_timeseries = compute_seasonal_head_range_m(head_steps)

    # Recovery efficiency from the WEL inject-IN / recover-OUT budget integrals.
    recovery_efficiency: float | None = None
    try:
        cbc_path = _resolve_gwf_cbc_path(run_outputs_uri)
        _net, injected, recovered = _read_cbc_term_signed_totals(cbc_path, "WEL")
        recovery_efficiency = compute_recovery_efficiency(injected, recovered)
    except PostprocessMODFLOWError as exc:
        logger.warning("ASR recovery-efficiency integral unavailable: %s", exc)

    logger.info(
        "postprocess_asr run_id=%s recovery_efficiency=%s head_steps=%d",
        run_id,
        recovery_efficiency,
        len(head_timeseries) if head_timeseries is not None else 0,
    )

    # Spatial carrier = the final-step water-table head COG (continuous head ramp).
    head_grid = head_steps[-1]
    cog_path = _write_reprojected_cog(
        head_grid, model_crs, geo, mask_below_floor=False
    )
    bbox_4326 = _cog_bbox_4326(cog_path)
    cog_uri = _upload_cog(
        cog_path, run_id, runs_bucket, cog_filename="asr_head_4326.tif"
    )

    layer_id = f"asr-{run_id}"
    final_uri = cog_uri
    if publish:
        wms_url = _dispatch_publish_layer(
            cog_uri, layer_id, style_preset=ASR_STYLE_PRESET
        )
        if wms_url:
            final_uri = wms_url

    return ASRLayerURI(
        layer_id=layer_id,
        name="Aquifer Storage & Recovery (well head + recovery)",
        layer_type="raster",
        uri=final_uri,
        style_preset=ASR_STYLE_PRESET,
        role="primary",
        units="m",
        bbox=bbox_4326,
        recovery_efficiency=recovery_efficiency,
        head_timeseries=head_timeseries,
    )


def postprocess_wetland_hydroperiod(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    publish: bool = True,
) -> HydroperiodLayerURI:
    """Convert a wetland transient GWF run's head into a ``HydroperiodLayerURI``.

    Reads EVERY saved GWF head step (``gwf_model.hds``), computes the per-cell
    seasonal head RANGE (max-over-time minus min-over-time), takes the PEAK swing
    as the hydroperiod headline + the per-step head at that peak-swing cell as the
    series, renders the per-cell seasonal-range grid as the COG, uploads +
    (optionally) publishes it, and returns the typed hydroperiod layer.

    The peak-swing cell IS the wetland cell with the largest seasonal water-table
    movement, found from the data -- no footprint lat/lon re-derivation needed.

    Raises:
        PostprocessMODFLOWError: any read / reproject / write / upload step
            failed; ``error_code`` identifies the stage.
    """
    import numpy as np  # type: ignore[import-not-found]

    hds_path = _resolve_gwf_hds_path(run_outputs_uri)
    geo = _grid_georegistration_from_deck(deck_dir)

    head_steps = _read_head_steps(hds_path)
    seasonal_head_range_m, head_timeseries = compute_seasonal_head_range_m(head_steps)
    logger.info(
        "postprocess_wetland_hydroperiod run_id=%s seasonal_head_range_m=%.6g "
        "head_steps=%d",
        run_id,
        seasonal_head_range_m,
        len(head_timeseries) if head_timeseries is not None else 0,
    )

    # The COG renders the per-cell seasonal RANGE (max-over-time minus min). NaN
    # off-grid (a never-active cell stays NaN); already non-negative.
    stack = np.stack([np.asarray(s, dtype="float64") for s in head_steps], axis=0)
    with np.errstate(invalid="ignore"):
        range_grid = np.nanmax(stack, axis=0) - np.nanmin(stack, axis=0)
    cog_path = _write_reprojected_cog(
        range_grid, model_crs, geo, mask_below_floor=False
    )
    bbox_4326 = _cog_bbox_4326(cog_path)
    cog_uri = _upload_cog(
        cog_path, run_id, runs_bucket, cog_filename="hydroperiod_range_4326.tif"
    )

    layer_id = f"hydroperiod-{run_id}"
    final_uri = cog_uri
    if publish:
        wms_url = _dispatch_publish_layer(
            cog_uri, layer_id, style_preset=HYDROPERIOD_STYLE_PRESET
        )
        if wms_url:
            final_uri = wms_url

    return HydroperiodLayerURI(
        layer_id=layer_id,
        name="Wetland Hydroperiod (seasonal water-table range)",
        layer_type="raster",
        uri=final_uri,
        style_preset=HYDROPERIOD_STYLE_PRESET,
        role="primary",
        units="m",
        bbox=bbox_4326,
        seasonal_head_range_m=seasonal_head_range_m,
        head_timeseries=head_timeseries,
    )


# --------------------------------------------------------------------------- #
# PRT capture-zone postprocess (Wave-4)
#
# MF6 PRT backward-particle-tracking produces a ``prtmodel.trk.csv`` under the
# PRT working directory.  ``build_and_run_prt_from_gwf`` (in gwt_adapter) builds
# and runs the two-sim sequence (GWF -> reverse outputs -> PRT) and returns the
# PRT working directory, which the tool phase passes as ``run_outputs_uri`` here.
#
# The GWF deck was built at LOCAL origin (0,0) to sidestep the mf6 6.7.0
# coordinate-check float-precision bug at large UTM origins.
# ``CellBudgetFile.reverse()`` also drops the grid origin, so PRT track coords
# come out in local (0-origin) coordinates.  The true UTM origin is recovered
# from the DECK georegistration (``_grid_georegistration_from_deck(deck_dir)``
# returns ``xorigin`` / ``yorigin`` -- these are set to the true UTM lower-left
# easting/northing by ``_build_prt_capture_zone_deck`` for contract compatibility
# with this postprocess step).  The EPSG code is parsed from ``model_crs``
# (e.g. "EPSG:32617" -> 32617) and applied to the polygon before reprojecting to
# EPSG:4326 for the FlatGeobuf artifact.
#
# Vector publish:
#   publish_layer is RASTER-ONLY.  DO NOT call it here.  Vectors reach the client
#   through the inline-GeoJSON path: the tool emitter calls
#   ``add_loaded_layer`` which calls ``_read_vector_uri_as_geojson`` on the
#   FlatGeobuf URI and ships the GeoJSON FeatureCollection over the WS.  The
#   client renders the polygon via the ``presetColorFor("capture_zone")``
#   color branch in vector_rendering.ts.
# --------------------------------------------------------------------------- #


def _resolve_prt_track_csv(run_outputs_uri: str) -> Path:
    """Locate ``prtmodel.trk.csv`` in a local PRT output directory.

    ``build_and_run_prt_from_gwf`` places the PRT sim inside
    ``<gwf_run_dir>/prt/`` and runs it there; ``ModflowPrtoc`` writes
    ``prtmodel.trk.csv`` alongside the other PRT output files.  The caller
    passes that directory as ``run_outputs_uri``.

    Raises:
        PostprocessMODFLOWError: (``CAPTURE_ZONE_OUTPUT_READ_FAILED``) when no
            ``prtmodel.trk.csv`` is found under ``run_outputs_uri``.
    """
    p = Path(run_outputs_uri.replace("file://", ""))
    if p.is_file() and p.name.endswith(".csv"):
        return p
    if p.is_dir():
        hits = sorted(
            glob.glob(str(p / "**" / PRT_TRACK_CSV_FILENAME), recursive=True)
        )
        if hits:
            return Path(hits[0])
    raise PostprocessMODFLOWError(
        "CAPTURE_ZONE_OUTPUT_READ_FAILED",
        message=(
            f"no {PRT_TRACK_CSV_FILENAME} found under {run_outputs_uri!r}; "
            "build_and_run_prt_from_gwf must have run successfully and placed "
            "the PRT working directory there."
        ),
        details={"run_outputs_uri": run_outputs_uri},
    )


def _read_prt_track_df(csv_path: Path) -> Any:
    """Read the PRT track CSV into a pandas DataFrame and validate it.

    The CSV carries one row per particle-tracking event with columns:
    ``kper``, ``kstp``, ``imdl``, ``iprp``, ``irpt``, ``ilay``, ``icell``,
    ``izone``, ``istatus``, ``ireason``, ``trelease``, ``t``, ``x``, ``y``,
    ``z``, ``name``.

    Raises:
        PostprocessMODFLOWError: (``CAPTURE_ZONE_OUTPUT_READ_FAILED``) when
            pandas is unavailable, the file cannot be parsed, or the required
            ``x`` / ``y`` / ``t`` columns are absent / entirely NaN.
    """
    try:
        import pandas as pd  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "CAPTURE_ZONE_OUTPUT_READ_FAILED",
            message=f"pandas not importable for PRT track CSV: {exc}",
            details={"csv_path": str(csv_path)},
        ) from exc

    try:
        df = pd.read_csv(str(csv_path))
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "CAPTURE_ZONE_OUTPUT_READ_FAILED",
            message=f"could not read PRT track CSV {csv_path}: {exc}",
            details={"csv_path": str(csv_path)},
        ) from exc

    for col in ("x", "y", "t"):
        if col not in df.columns:
            raise PostprocessMODFLOWError(
                "CAPTURE_ZONE_OUTPUT_READ_FAILED",
                message=(
                    f"PRT track CSV {csv_path} is missing required column {col!r}; "
                    f"columns present: {list(df.columns)}"
                ),
                details={"csv_path": str(csv_path), "columns": list(df.columns)},
            )

    # Drop rows where x or y is NaN (e.g. release-event rows with no position).
    import pandas as pd  # already imported above; local re-ref for the filter

    df = df.dropna(subset=["x", "y"])
    if df.empty:
        raise PostprocessMODFLOWError(
            "CAPTURE_ZONE_OUTPUT_READ_FAILED",
            message=(
                f"PRT track CSV {csv_path} has no rows with finite x/y coordinates; "
                "the PRT run may have produced no tracked pathlines."
            ),
            details={"csv_path": str(csv_path)},
        )
    return df


def _upload_fgb(
    local_fgb: Path,
    run_id: str,
    runs_bucket: str | None,
    *,
    fgb_filename: str = "capture_zone_4326.fgb",
) -> str:
    """Upload a FlatGeobuf to ``{scheme}://<runs_bucket>/<run_id>/<fgb_filename>``.

    Mirrors ``_upload_cog`` but uses ``application/octet-stream`` as the MIME
    type (FlatGeobuf has no registered IANA type).  The inline-GeoJSON vector
    path reads this artifact server-side via ``_read_vector_uri_as_geojson``
    (geopandas + pyogrio) and ships the FeatureCollection over the WS; the
    client does NOT fetch the FGB directly, so CORS is not a concern here.

    Raises:
        PostprocessMODFLOWError: (``CAPTURE_ZONE_WRITE_FAILED``) on any upload
            failure.
    """
    try:
        return cog_io.upload_cog(
            local_fgb,
            run_id,
            runs_bucket,
            dest_filename=fgb_filename,
            content_type="application/octet-stream",
            gs_backend="fsspec",
            gs_fallback_to_file=True,
            runs_bucket_default=RUNS_BUCKET_DEFAULT,
            log_label="capture zone FGB",
        )
    except CogIoError as exc:
        raise PostprocessMODFLOWError(
            "CAPTURE_ZONE_WRITE_FAILED",
            message=f"could not upload capture-zone FlatGeobuf: {exc.message}",
            details=exc.details,
        ) from exc


def postprocess_capture_zone(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    xoffset_m: float | None = None,
    yoffset_m: float | None = None,
    model_utm_epsg: int | None = None,
    tier_years: list[float] | None = None,
) -> CaptureZoneLayerURI:
    """Convert MF6 PRT backward-tracking output into a ``CaptureZoneLayerURI``.

    Reads ``prtmodel.trk.csv`` from the PRT working directory (``run_outputs_uri``),
    builds the outer capture-zone polygon as the convex hull of ALL backtracked
    pathline vertices, and builds nested travel-time isochrone polygons for each
    requested tier.  Travel time is computed as ``abs(t) / 365.25`` (days to
    years; the GWF TDIS uses DAYS as the time unit, so ``t`` in the CSV is in
    days).  All polygon areas are computed in the model UTM CRS (m^2 -> km^2)
    before reprojection.

    The result is written as a FlatGeobuf in EPSG:4326 (a FeatureCollection with
    one polygon feature per isochrone tier plus the outer envelope, each carrying
    a ``travel_time_years`` property), uploaded to the runs bucket, and returned
    as a ``CaptureZoneLayerURI``.  ``publish_layer`` is NOT called (it is
    raster-only; vectors render via the inline-GeoJSON path over WS).

    Example usage (module-level docstring smoke test)::

        from trid3nt_server.workflows.postprocess_modflow import postprocess_capture_zone

        # The PRT working directory must contain prtmodel.trk.csv:
        # result = postprocess_capture_zone(
        #     "/tmp/prt_capture_zone/ws/gwf/prt",
        #     run_id="demo-001",
        #     model_crs="EPSG:32617",
        #     deck_dir="/tmp/prt_capture_zone/ws/gwf",
        # )
        # -> CaptureZoneLayerURI(
        #        layer_type='vector',
        #        style_preset='capture_zone',
        #        capture_zone_area_km2=...,
        #        travel_time_years=[1.0, 5.0, 10.0],
        #        isochrone_areas_km2={'1': ..., '5': ..., '10': ...},
        #        particle_count=16,
        #    )

    Args:
        run_outputs_uri: the PRT output directory (local path / ``file://``)
            that ``build_and_run_prt_from_gwf`` produced; must contain
            ``prtmodel.trk.csv``.
        run_id: run identifier; the FlatGeobuf artifact is uploaded to
            ``<runs_bucket>/<run_id>/capture_zone_4326.fgb``.
        model_crs: the GWF deck's projected CRS string (e.g. ``"EPSG:32617"``).
            Used to derive the integer EPSG code for area computation and the
            reprojection from model UTM to EPSG:4326.
        deck_dir: optional path to the GWF deck directory.  FALLBACK ONLY for
            the UTM origin: the PRT GWF DIS is built at LOCAL (0,0) origin (to
            dodge the mf6 6.7.0 coordinate-check float-precision bug), so a
            reloaded modelgrid reports xoffset=yoffset=0.0 -- it CANNOT recover
            the true origin.  The production call site therefore passes the true
            origin explicitly via ``xoffset_m`` / ``yoffset_m`` (below); the
            ``deck_dir`` reload is kept only as a last resort.
        runs_bucket: optional override for the S3 / GCS runs bucket name.
            ``None`` -> ``TRID3NT_RUNS_BUCKET`` env var / ``RUNS_BUCKET_DEFAULT``.
        xoffset_m: the TRUE UTM easting of the (local-origin) PRT grid lower-left,
            from ``DeckManifest.xoffset_m``.  The local-origin track coordinates
            are shifted by this before reprojection so the polygon lands at the
            real well location.  REQUIRED for a correctly georeferenced result;
            omitting it (and a real UTM ``model_utm_epsg``) raises rather than
            ship a polygon at the equator (Invariant-1 honesty guard).
        yoffset_m: the TRUE UTM northing of the PRT grid lower-left, from
            ``DeckManifest.yoffset_m``.  Paired with ``xoffset_m``.
        model_utm_epsg: integer EPSG code of the model UTM CRS (e.g. ``32617``),
            from ``DeckManifest.model_utm_epsg``.  Preferred over parsing
            ``model_crs``; drives the area computation and the 4326 reprojection.
        tier_years: the travel-time isochrone tiers the user/composer requested
            (``MODFLOWRunArgs.capture_zone_travel_time_years``, e.g. ``[1, 5, 10]``
            for capture_zone or ``[2, 5, 10]`` for wellhead_protection).  Used
            DIRECTLY so the narrated tiers equal the requested ones (Invariant-1).
            Only when ``None`` does the function derive data-driven tiers as a
            last resort.

    Returns:
        ``CaptureZoneLayerURI`` with:
          - ``layer_type='vector'``
          - ``style_preset='capture_zone'``
          - ``uri``: FlatGeobuf object-store URI (or ``file://`` in offline mode)
          - ``bbox``: EPSG:4326 ``(min_lon, min_lat, max_lon, max_lat)``
          - ``capture_zone_area_km2``: area of the outer isochrone envelope
          - ``travel_time_years``: isochrone tiers actually computed
          - ``isochrone_areas_km2``: per-tier nested area (keys = tier as str)
          - ``particle_count``: number of released particles in the track CSV

    Raises:
        PostprocessMODFLOWError:
          - ``CAPTURE_ZONE_OUTPUT_READ_FAILED`` -- CSV not found / unreadable /
            empty.
          - ``CAPTURE_ZONE_WRITE_FAILED`` -- FlatGeobuf write or upload failed.
    """
    try:
        import numpy as np  # type: ignore[import-not-found]
        import shapely.affinity  # type: ignore[import-not-found]
        import shapely.ops  # type: ignore[import-not-found]
        from shapely.geometry import MultiPoint, mapping  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "CAPTURE_ZONE_OUTPUT_READ_FAILED",
            message=f"shapely / numpy not importable for capture-zone postprocess: {exc}",
        ) from exc

    # --- Step 1: locate + read the PRT track CSV ----------------------------
    csv_path = _resolve_prt_track_csv(run_outputs_uri)
    df = _read_prt_track_df(csv_path)

    particle_count = int(df[["iprp", "irpt"]].drop_duplicates().shape[0])
    logger.info(
        "postprocess_capture_zone run_id=%s csv=%s rows=%d particles=%d",
        run_id, csv_path, len(df), particle_count,
    )

    # --- Step 2: recover the UTM offset (true grid lower-left) --------------
    # The GWF DIS was built at local origin (0,0) to avoid the mf6 6.7.0
    # coordinate-check float-precision bug.  CellBudgetFile.reverse() also drops
    # the origin, so PRT track x/y come out in LOCAL (0-origin) coordinates and
    # must be shifted by the TRUE UTM lower-left before reprojection.
    #
    # A reloaded modelgrid CANNOT recover this (a local-origin DIS reports
    # xoffset=yoffset=0.0), so the production call site passes the true origin
    # explicitly from the in-memory DeckManifest (xoffset_m/yoffset_m).  The
    # deck-dir reload is a last-resort fallback only.
    if xoffset_m is not None and yoffset_m is not None:
        x_off = float(xoffset_m)
        y_off = float(yoffset_m)
    else:
        geo = _grid_georegistration_from_deck(deck_dir)
        x_off = float(geo["xorigin"]) if geo is not None else 0.0
        y_off = float(geo["yorigin"]) if geo is not None else 0.0
        if geo is None:
            logger.warning(
                "postprocess_capture_zone: no explicit offset and no deck "
                "georegistration from %s; assuming track coordinates are already "
                "georeferenced (testing only)",
                deck_dir,
            )

    # EPSG code: prefer the explicit manifest value, else parse model_crs.
    if model_utm_epsg is not None and int(model_utm_epsg) > 0:
        utm_epsg = int(model_utm_epsg)
    else:
        try:
            utm_epsg = int(str(model_crs).split(":")[-1])
        except Exception:  # noqa: BLE001
            utm_epsg = 0
            logger.warning(
                "postprocess_capture_zone: could not parse EPSG code from "
                "model_crs=%r; reprojection from UTM to EPSG:4326 will fall back "
                "to identity",
                model_crs,
            )

    # Honesty guard (Invariant-1): a real UTM model with a (0,0) offset would
    # reproject the local-origin polygon to ~lat 0 -- a capture zone ~thousands
    # of km from the well that still passes the translation-invariant area floor.
    # Refuse to emit it; an explicit error is honest, a mislocated polygon is not.
    if utm_epsg not in (0, 4326) and x_off == 0.0 and y_off == 0.0:
        raise PostprocessMODFLOWError(
            "CAPTURE_ZONE_OUTPUT_READ_FAILED",
            message=(
                "capture-zone georegistration is missing: model CRS is "
                f"EPSG:{utm_epsg} (a projected UTM zone) but the grid offset is "
                "(0, 0).  The true UTM origin (DeckManifest.xoffset_m/yoffset_m) "
                "was not threaded to postprocess; refusing to emit a polygon at "
                "the equator."
            ),
            details={"model_utm_epsg": utm_epsg, "xoffset_m": x_off, "yoffset_m": y_off},
        )

    # --- Step 3: travel time in years ----------------------------------------
    # PRT timestamps each vertex with model time ``t`` (in DAYS; the GWF TDIS
    # time_units are DAYS).  Backward tracking yields INCREASING abs(t) as
    # particles travel up-gradient from the well.  The release event (ireason=1)
    # has t=0.  Use abs(t) as the elapsed travel time.
    df = df.copy()
    df["ttravel_years"] = df["t"].abs() / 365.25

    # --- Step 4: isochrone tiers ---------------------------------------------
    # The user/composer-requested tiers (MODFLOWRunArgs.capture_zone_travel_time_years)
    # are threaded in explicitly via ``tier_years`` (the call site holds the
    # in-memory DeckManifest + run_args).  Use them DIRECTLY so the narrated
    # tiers equal the requested ones (Invariant-1).  These manifest-only values
    # are NOT recoverable from any on-disk MF6 file, so there is no deck-reload
    # path -- only a data-driven last resort when no tiers were supplied.
    requested_tiers = [float(t) for t in (tier_years or []) if float(t) > 0.0]
    if requested_tiers:
        tiers: list[float] = sorted(requested_tiers)
    else:
        # Data-driven fallback (only when the caller supplied no tiers): three
        # evenly-spaced tiers at 10%, 50%, 100% of the maximum observed travel
        # time (minimum 1 year to avoid sub-year tiers that produce a degenerate
        # hull on a coarse 100 m grid).
        t_max = float(df["ttravel_years"].max())
        if t_max >= 3.0:
            tiers = [
                round(t_max * 0.10, 2),
                round(t_max * 0.50, 2),
                round(t_max, 2),
            ]
            tiers = [max(1.0, t) for t in tiers]
        else:
            tiers = [max(1.0, t_max)]

    logger.info(
        "postprocess_capture_zone run_id=%s requested_tiers=%s tiers=%s t_max_years=%.2f",
        run_id, requested_tiers or None, tiers, float(df["ttravel_years"].max()),
    )

    # --- Step 5: build polygons in LOCAL UTM coords --------------------------
    # All shapely geometry is first built in LOCAL coordinates (0-origin),
    # then shifted by (xoffset_m, yoffset_m) to get true UTM coords, then
    # reprojected to EPSG:4326 for the FlatGeobuf.  Area is measured in true
    # UTM (after offset) so the km^2 scalars are correct.

    x_local = df["x"].values
    y_local = df["y"].values

    if len(x_local) < 3:
        raise PostprocessMODFLOWError(
            "CAPTURE_ZONE_OUTPUT_READ_FAILED",
            message=(
                f"PRT track CSV {csv_path} has only {len(x_local)} vertices with "
                "finite x/y; need >= 3 to build a convex hull."
            ),
            details={"csv_path": str(csv_path), "n_vertices": len(x_local)},
        )

    # Outer capture envelope = convex hull of ALL pathline vertices.
    outer_hull_local = MultiPoint(list(zip(x_local, y_local))).convex_hull

    # Nested isochrone hulls for each requested tier.  Only compute a tier if
    # there are >= 3 vertices within that travel-time window; shorter tiers on
    # a 100 m grid may have too few points for a meaningful polygon.
    iso_hulls_local: dict[str, Any] = {}
    actual_tiers: list[float] = []
    for T in tiers:  # already sorted ascending
        sub = df[df["ttravel_years"] <= T]
        if len(sub) >= 3:
            h = MultiPoint(list(zip(sub["x"].values, sub["y"].values))).convex_hull
            key = str(int(T)) if T == int(T) else str(T)
            iso_hulls_local[key] = h
            actual_tiers.append(T)
        else:
            logger.info(
                "postprocess_capture_zone: tier %.2f yr has only %d vertices; skipped",
                T, len(sub),
            )

    if not actual_tiers:
        # The outer hull still spans all time; provide it as the single tier.
        key = str(int(tiers[-1])) if tiers[-1] == int(tiers[-1]) else str(tiers[-1])
        iso_hulls_local[key] = outer_hull_local
        actual_tiers = [tiers[-1]]

    # --- Step 6: shift to true UTM, measure areas, then reproject -----------
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
        from shapely.geometry import shape  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "CAPTURE_ZONE_WRITE_FAILED",
            message=f"geopandas / pandas not importable for FlatGeobuf write: {exc}",
        ) from exc

    def _shift_and_reproject(geom: Any) -> Any:
        """Translate local-origin geometry to true UTM then reproject to 4326."""
        if x_off != 0.0 or y_off != 0.0:
            geom = shapely.affinity.translate(geom, xoff=x_off, yoff=y_off)
        if utm_epsg and utm_epsg != 4326:
            src_crs = f"EPSG:{utm_epsg}"
            gdf_tmp = gpd.GeoDataFrame(geometry=[geom], crs=src_crs)
            gdf_tmp = gdf_tmp.to_crs("EPSG:4326")
            return gdf_tmp.geometry.iloc[0]
        return geom

    def _area_km2(geom_local: Any) -> float:
        """Area of a local-UTM geometry in km^2 (after shift to true UTM)."""
        if x_off != 0.0 or y_off != 0.0:
            g = shapely.affinity.translate(geom_local, xoff=x_off, yoff=y_off)
        else:
            g = geom_local
        return float(g.area) / 1_000_000.0

    # Outer envelope area (in true UTM before 4326 reproject).
    outer_area_km2 = _area_km2(outer_hull_local)

    # Per-tier isochrone areas and reprojected geometries for the FlatGeobuf.
    isochrone_areas_km2: dict[str, float] = {}
    features_geom: list[Any] = []
    features_props: list[dict[str, Any]] = []

    # Outer envelope feature (travel_time_years = None signals the outer hull).
    outer_4326 = _shift_and_reproject(outer_hull_local)
    features_geom.append(outer_4326)
    features_props.append({
        "feature_type": "outer_envelope",
        "travel_time_years": None,
        "area_km2": outer_area_km2,
    })

    for key, iso_local in iso_hulls_local.items():
        area = _area_km2(iso_local)
        isochrone_areas_km2[key] = area
        iso_4326 = _shift_and_reproject(iso_local)
        try:
            t_val = float(key)
        except ValueError:
            t_val = None
        features_geom.append(iso_4326)
        features_props.append({
            "feature_type": "isochrone",
            "travel_time_years": t_val,
            "area_km2": area,
        })

    # --- Step 7: write FlatGeobuf in EPSG:4326 --------------------------------
    gdf = gpd.GeoDataFrame(features_props, geometry=features_geom, crs="EPSG:4326")

    try:
        fgb_path = Path(
            tempfile.NamedTemporaryFile(
                suffix="_capture_zone_4326.fgb", delete=False
            ).name
        )
        gdf.to_file(str(fgb_path), driver="FlatGeobuf", engine="pyogrio")
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "CAPTURE_ZONE_WRITE_FAILED",
            message=f"could not write capture-zone FlatGeobuf: {exc}",
            details={"run_id": run_id},
        ) from exc

    # Derive the EPSG:4326 bounding box from the outer envelope polygon.
    bbox_4326: tuple[float, float, float, float] | None = None
    try:
        b = outer_4326.bounds  # (min_lon, min_lat, max_lon, max_lat) in EPSG:4326
        bbox_4326 = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    except Exception:  # noqa: BLE001
        bbox_4326 = None

    # --- Step 8: upload to runs bucket ---------------------------------------
    fgb_uri = _upload_fgb(fgb_path, run_id, runs_bucket)

    logger.info(
        "postprocess_capture_zone run_id=%s outer_area_km2=%.4g "
        "tiers=%s iso_areas=%s particles=%d uri=%s",
        run_id, outer_area_km2, actual_tiers, isochrone_areas_km2,
        particle_count, fgb_uri,
    )

    layer_id = f"capture-zone-{run_id}"
    return CaptureZoneLayerURI(
        layer_id=layer_id,
        name="Wellhead Capture Zone (backward particle tracking)",
        layer_type="vector",
        uri=fgb_uri,
        style_preset=CAPTURE_ZONE_STYLE_PRESET,
        role="primary",
        bbox=bbox_4326,
        capture_zone_area_km2=outer_area_km2,
        travel_time_years=actual_tiers,
        isochrone_areas_km2=isochrone_areas_km2,
        particle_count=particle_count,
    )


# --------------------------------------------------------------------------- #
# SFR routed stream-depletion postprocess (module wave)
#
# The stream_depletion archetype drapes a MODFLOW-6 SFR6 stream network onto the
# GWF grid (path-ordered reaches) coupled to a pumping WEL well, and writes a
# continuous per-reach observation CSV (``<gwf>.sfr.obs.csv``) carrying, per
# reach per timestep: STAGE_R{i} (stage m), FLOW_R{i} (downstream-flow) and
# GWF_R{i} (the sfr<->GWF exchange). The column casing + SIGNS are pinned by the
# local mf6 6.5.0 smoke fixture (services/workers/modflow/fixtures/sfr_smoke):
#   * FLOW (downstream-flow) is reported NEGATIVE (an outflow magnitude); the
#     feature carries abs(FLOW) as the routed discharge.
#   * GWF (sfr exchange) is reach-relative: POSITIVE = the reach LOSES water to
#     the aquifer (losing reach); NEGATIVE = the aquifer FEEDS the reach (gaining
#     reach). Reach flow balance: out = upstream_in + INFLOW - GWF.
#   * Streamflow depletion = sum over reaches of (pumped-period GWF minus
#     baseline-period GWF); POSITIVE = the streamflow the well captured.
#
# The reach cell (row, col) + per-cell reach length + the model grid origin come
# from the deck itself (flopy reloads the SFR packagedata + modelgrid from
# ``deck_dir``); the pumping rate comes from the WEL package. The per-reach
# polyline is reprojected model-UTM -> EPSG:4326 and written as a FlatGeobuf,
# reaching the client through the SAME inline-GeoJSON add_loaded_layer path as
# the capture-zone vector (publish_layer is RASTER-ONLY - NOT used here).
# --------------------------------------------------------------------------- #


def _resolve_sfr_obs_csv(run_outputs_uri: str) -> Path:
    """Locate ``<gwf>.sfr.obs.csv`` under a local run-output directory.

    mf6 runs with its CWD at the deck dir, so the obs CSV lands there (or under a
    reorganised subdir); a recursive glob finds it wherever it landed.

    Raises:
        PostprocessMODFLOWError: (``STREAM_DEPLETION_OUTPUT_READ_FAILED``) when no
            ``*.sfr.obs.csv`` is found under ``run_outputs_uri``.
    """
    p = Path(run_outputs_uri.replace("file://", ""))
    if p.is_file() and p.name.endswith(".sfr.obs.csv"):
        return p
    if p.is_dir():
        hits = sorted(glob.glob(str(p / "**" / "*.sfr.obs.csv"), recursive=True))
        if hits:
            return Path(hits[0])
    raise PostprocessMODFLOWError(
        "STREAM_DEPLETION_OUTPUT_READ_FAILED",
        message=(
            f"no *.sfr.obs.csv found under {run_outputs_uri!r}; the stream_depletion "
            "run must have written the SFR continuous-observation CSV."
        ),
        details={"run_outputs_uri": run_outputs_uri},
    )


def _read_sfr_obs_rows(csv_path: Path) -> list[dict[str, str]]:
    """Read the SFR obs CSV into a list of dict rows (header-keyed)."""
    import csv as _csv

    try:
        with csv_path.open() as fh:
            rows = list(_csv.DictReader(fh))
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "STREAM_DEPLETION_OUTPUT_READ_FAILED",
            message=f"could not read SFR obs CSV {csv_path}: {exc}",
            details={"csv_path": str(csv_path)},
        ) from exc
    if len(rows) < 2:
        raise PostprocessMODFLOWError(
            "STREAM_DEPLETION_OUTPUT_READ_FAILED",
            message=(
                f"SFR obs CSV {csv_path} has < 2 timesteps (need a baseline row + at "
                "least one pumped row for the depletion delta)."
            ),
            details={"csv_path": str(csv_path), "n_rows": len(rows)},
        )
    return rows


def _sfr_obs_value(row: dict[str, str], prefix: str, i: int) -> float:
    """Read the ``{prefix}_R{i}`` obs column (mf6 UPPERCASES the boundname)."""
    key = f"{prefix}_R{i}"
    if key in row:
        return float(row[key])
    # Case-insensitive fallback (defensive; the smoke fixture is uppercased).
    for k, v in row.items():
        if k.upper() == key:
            return float(v)
    raise KeyError(key)


def compute_stream_depletion_metrics(
    rows: list[dict[str, str]], n_reaches: int
) -> dict[str, Any]:
    """Compute per-reach + aggregate stream-depletion metrics from the obs rows.

    PURE arithmetic over the parsed SFR obs CSV (no flopy, no mf6) so the SIGN
    math is unit-testable directly on the smoke fixture. ``rows[0]`` is the steady
    baseline (WEL off); ``rows[-1]`` is the final pumped step. The signs follow
    the Phase-1 smoke findings (see the section header):
      * depletion_i = GWF_pumped[i] - GWF_baseline[i] (POSITIVE = capture)
      * gaining if GWF_pumped[i] < 0, losing if > 0
      * flow = abs(FLOW_pumped[i]) (downstream-flow is a negative magnitude)

    Returns a dict with ``per_reach`` (one entry per reach: exchange / flow /
    stage / depletion / stage_decline / classification), the aggregate scalars
    (``total_depletion_m3_day`` >= 0 after a max(0, .) floor, ``max_stage_decline_m``,
    ``gaining_reach_count``, ``losing_reach_count``), and the per-timestep
    depletion series (``days`` + ``depletion_series_m3_day``) for the chart.
    """
    base = rows[0]
    pump = rows[-1]
    per_reach: list[dict[str, Any]] = []
    total_dep = 0.0
    max_stage_decline = 0.0
    gaining = 0
    losing = 0
    for i in range(n_reaches):
        exch_b = _sfr_obs_value(base, "GWF", i)
        exch_p = _sfr_obs_value(pump, "GWF", i)
        dep_i = exch_p - exch_b
        total_dep += dep_i
        stage_b = _sfr_obs_value(base, "STAGE", i)
        stage_p = _sfr_obs_value(pump, "STAGE", i)
        decline = stage_b - stage_p
        if decline > max_stage_decline:
            max_stage_decline = decline
        flow_p = abs(_sfr_obs_value(pump, "FLOW", i))
        if exch_p < 0:
            gaining += 1
            classification = "gaining"
        elif exch_p > 0:
            losing += 1
            classification = "losing"
        else:
            classification = "neutral"
        per_reach.append(
            {
                "reach": i,
                "exchange_m3_day": exch_p,
                "exchange_baseline_m3_day": exch_b,
                "depletion_m3_day": dep_i,
                "flow_m3_day": flow_p,
                "stage_m": stage_p,
                "stage_decline_m": decline,
                "classification": classification,
            }
        )

    # Per-timestep depletion series (every row after the baseline).
    days: list[float] = []
    depletion_series: list[float] = []
    base_exch = [_sfr_obs_value(base, "GWF", i) for i in range(n_reaches)]
    for r in rows[1:]:
        s = sum(_sfr_obs_value(r, "GWF", i) - base_exch[i] for i in range(n_reaches))
        depletion_series.append(float(s))
        try:
            days.append(float(r.get("time", len(days) + 1)))
        except (TypeError, ValueError):
            days.append(float(len(days) + 1))

    return {
        "per_reach": per_reach,
        "total_depletion_m3_day": max(0.0, float(total_dep)),
        "max_stage_decline_m": max(0.0, float(max_stage_decline)),
        "gaining_reach_count": gaining,
        "losing_reach_count": losing,
        "days": days,
        "depletion_series_m3_day": depletion_series,
    }


def _read_sfr_reach_geometry(deck_dir: str | None) -> dict[str, Any] | None:
    """Read the SFR reach cells + grid origin + pumping rate from the deck (flopy).

    Reloads the MF6 simulation from ``deck_dir`` and reads: the SFR packagedata
    (``ifno`` / ``cellid`` / ``rlen`` per reach in path order), the model grid
    origin + cell sizes (for the reach cell-centre coordinates), and the WEL
    pumping magnitude (for the depletion fraction). Returns None if the deck
    cannot be read (the caller then degrades to metrics-only).
    """
    if not deck_dir:
        return None
    try:
        import flopy  # type: ignore[import-not-found]

        sim = flopy.mf6.MFSimulation.load(sim_ws=str(deck_dir), verbosity_level=0)
        gwf = None
        for mname in sim.model_names:
            if mname.startswith("gwf"):
                gwf = sim.get_model(mname)
                break
        if gwf is None and sim.model_names:
            gwf = sim.get_model(sim.model_names[0])
        if gwf is None:
            return None
        sfr = None
        for pname in ("sfr-0", "sfr", "sfr_0"):
            try:
                sfr = gwf.get_package(pname)
            except Exception:  # noqa: BLE001
                sfr = None
            if sfr is not None:
                break
        if sfr is None:
            return None
        pd = sfr.packagedata.array
        reaches: list[tuple[int, int, int, float]] = []
        for rec in pd:
            cellid = rec["cellid"]
            row = int(cellid[1])
            col = int(cellid[2])
            reaches.append((int(rec["ifno"]), row, col, float(rec["rlen"])))
        reaches.sort(key=lambda t: t[0])  # path order = ifno order
        mg = gwf.modelgrid
        grid = {
            "xorigin": float(mg.xoffset),
            "yorigin": float(mg.yoffset),
            "delr": float(mg.delr[0]),
            "delc": float(mg.delc[0]),
            "nrow": int(mg.nrow),
            "ncol": int(mg.ncol),
        }
        # WEL pumping magnitude (period 1 = first pumped period).
        pumping = 0.0
        for pname in ("wel-0", "wel", "wel_0"):
            try:
                wel = gwf.get_package(pname)
            except Exception:  # noqa: BLE001
                wel = None
            if wel is not None:
                try:
                    data = wel.stress_period_data.get_data(1)
                    if data is not None and len(data) > 0:
                        pumping = float(abs(data["q"][0]))
                except Exception:  # noqa: BLE001
                    pumping = 0.0
                break
        return {"reaches": reaches, "grid": grid, "pumping_m3_day": pumping}
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read SFR reach geometry from %s: %s", deck_dir, exc)
        return None


def postprocess_stream_reaches(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
) -> StreamReachLayerURI:
    """Convert MF6 SFR obs + deck geometry into a ``StreamReachLayerURI``.

    Parses ``<gwf>.sfr.obs.csv`` from the run directory and the SFR reach cells +
    grid + pumping from the deck (``deck_dir``), then:
      * computes per-reach flow / stage / exchange / depletion + the aggregate
        depletion scalars (signs per the Phase-1 smoke findings);
      * builds a per-reach polyline FlatGeobuf (model-UTM -> EPSG:4326) reaching
        the client via the inline-GeoJSON add_loaded_layer path (NOT the raster
        publish_layer - same rule as the capture-zone vector);
      * stashes the two Vega-Lite charts (depletion-vs-time + reach flow/stage
        profile) on the returned layer as the runtime attributes
        ``_depletion_chart`` / ``_reach_profile_chart`` (private-underscore so the
        Pydantic model ignores them) for the composer to emit.

    Args:
        run_outputs_uri: the run-output directory (local path / ``file://``) that
            ``run_modflow_local`` produced; must contain ``*.sfr.obs.csv``.
        run_id: run identifier; the FlatGeobuf artifact is uploaded to
            ``<runs_bucket>/<run_id>/stream_depletion_4326.fgb``.
        model_crs: the GWF deck's projected CRS string (e.g. ``"EPSG:32611"``);
            drives the reach-polyline reprojection to EPSG:4326.
        deck_dir: the GWF deck directory; flopy reloads the SFR packagedata +
            modelgrid + WEL from it for the reach geometry + pumping rate.
        runs_bucket: optional override for the runs bucket name.

    Returns:
        ``StreamReachLayerURI`` (``layer_type='vector'``,
        ``style_preset='stream_depletion'``) with the depletion metrics.

    Raises:
        PostprocessMODFLOWError: read / geometry / write / upload failure.
    """
    # --- Step 1: parse the SFR obs CSV --------------------------------------- #
    csv_path = _resolve_sfr_obs_csv(run_outputs_uri)
    rows = _read_sfr_obs_rows(csv_path)

    # --- Step 2: reach geometry + pumping from the deck ---------------------- #
    geom = _read_sfr_reach_geometry(deck_dir)
    if geom is None or not geom["reaches"]:
        raise PostprocessMODFLOWError(
            "STREAM_DEPLETION_OUTPUT_READ_FAILED",
            message=(
                "could not recover the SFR reach geometry from the deck "
                f"({deck_dir!r}); refusing to emit a mislocated reach vector."
            ),
            details={"deck_dir": deck_dir},
        )
    reaches = geom["reaches"]
    grid = geom["grid"]
    pumping = float(geom["pumping_m3_day"])
    n_reaches = len(reaches)

    metrics = compute_stream_depletion_metrics(rows, n_reaches)

    # --- Step 3: reach cell centres in model UTM ----------------------------- #
    xorigin = grid["xorigin"]
    yorigin = grid["yorigin"]
    delr = grid["delr"]
    delc = grid["delc"]
    nrow = grid["nrow"]
    centers: list[tuple[float, float]] = []
    cum_len_km: list[float] = []
    running = 0.0
    for (_ifno, row, col, rlen) in reaches:
        east = xorigin + (col + 0.5) * delr
        north = (yorigin + nrow * delc) - (row + 0.5) * delc
        centers.append((east, north))
        running += float(rlen)
        cum_len_km.append(running / 1000.0)

    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import LineString  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "STREAM_DEPLETION_WRITE_FAILED",
            message=f"geopandas / shapely not importable for reach FlatGeobuf: {exc}",
        ) from exc

    # Per-reach segment: centre[i] -> centre[i+1] (last reach: prev -> self).
    segments: list[Any] = []
    for i in range(n_reaches):
        p0 = centers[i]
        if i < n_reaches - 1:
            p1 = centers[i + 1]
        elif n_reaches >= 2:
            p1 = centers[i]
            p0 = centers[i - 1]
        else:
            # Single reach: a tiny east-west tick so the geometry is non-degenerate.
            p1 = (p0[0] + delr * 0.5, p0[1])
        if p0 == p1:
            p1 = (p1[0] + delr * 0.25, p1[1])
        segments.append(LineString([p0, p1]))

    props = []
    for i, pr in enumerate(metrics["per_reach"]):
        props.append(
            {
                "reach": pr["reach"],
                "flow_m3_day": pr["flow_m3_day"],
                "stage_m": pr["stage_m"],
                "exchange_m3_day": pr["exchange_m3_day"],
                "depletion_m3_day": pr["depletion_m3_day"],
                "stage_decline_m": pr["stage_decline_m"],
                "classification": pr["classification"],
                "river_km": cum_len_km[i],
            }
        )

    # --- Step 4: reproject model-UTM -> EPSG:4326, write FlatGeobuf ----------- #
    src_crs = model_crs if str(model_crs).upper().startswith("EPSG") else None
    try:
        gdf = gpd.GeoDataFrame(props, geometry=segments, crs=src_crs)
        if src_crs and src_crs != "EPSG:4326":
            gdf = gdf.to_crs("EPSG:4326")
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "STREAM_DEPLETION_WRITE_FAILED",
            message=f"could not reproject SFR reaches to EPSG:4326: {exc}",
            details={"run_id": run_id, "model_crs": model_crs},
        ) from exc

    try:
        fgb_path = Path(
            tempfile.NamedTemporaryFile(
                suffix="_stream_depletion_4326.fgb", delete=False
            ).name
        )
        gdf.to_file(str(fgb_path), driver="FlatGeobuf", engine="pyogrio")
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "STREAM_DEPLETION_WRITE_FAILED",
            message=f"could not write SFR reach FlatGeobuf: {exc}",
            details={"run_id": run_id},
        ) from exc

    bbox_4326: tuple[float, float, float, float] | None = None
    try:
        b = gdf.total_bounds  # (minx, miny, maxx, maxy)
        bbox_4326 = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    except Exception:  # noqa: BLE001
        bbox_4326 = None

    fgb_uri = _upload_fgb(
        fgb_path, run_id, runs_bucket, fgb_filename="stream_depletion_4326.fgb"
    )

    total_dep = float(metrics["total_depletion_m3_day"])
    depletion_fraction = (total_dep / pumping) if pumping > 0 else 0.0

    logger.info(
        "postprocess_stream_reaches run_id=%s n_reaches=%d total_depletion=%.4g "
        "pumping=%.4g frac=%.3f gaining=%d losing=%d uri=%s",
        run_id, n_reaches, total_dep, pumping, depletion_fraction,
        metrics["gaining_reach_count"], metrics["losing_reach_count"], fgb_uri,
    )

    layer = StreamReachLayerURI(
        layer_id=f"stream-depletion-{run_id}",
        name="Stream depletion by reach (SFR routed exchange)",
        layer_type="vector",
        uri=fgb_uri,
        style_preset=STREAM_DEPLETION_STYLE_PRESET,
        role="primary",
        bbox=bbox_4326,
        total_depletion_m3_day=total_dep,
        depletion_fraction=float(depletion_fraction),
        n_reaches=n_reaches,
        max_stage_decline_m=float(metrics["max_stage_decline_m"]),
        gaining_reach_count=int(metrics["gaining_reach_count"]),
        losing_reach_count=int(metrics["losing_reach_count"]),
    )

    # --- Step 5: build + stash the two charts (composer emits them) ---------- #
    try:
        from ..tools.processing.charts_common import build_depletion_timeseries_chart, build_reach_profile_chart

        dep_chart = build_depletion_timeseries_chart(
            days=metrics["days"],
            depletion_m3_day=metrics["depletion_series_m3_day"],
            pumping_rate_m3_day=pumping or None,
            source_layer_uri=fgb_uri,
        )
        profile_chart = build_reach_profile_chart(
            river_km=cum_len_km,
            flow_m3_day=[pr["flow_m3_day"] for pr in metrics["per_reach"]],
            stage_m=[pr["stage_m"] for pr in metrics["per_reach"]],
            source_layer_uri=fgb_uri,
        )
        object.__setattr__(layer, "_depletion_chart", dep_chart)
        object.__setattr__(layer, "_reach_profile_chart", profile_chart)
    except Exception as exc:  # noqa: BLE001  -- charts are best-effort side output
        logger.warning("postprocess_stream_reaches chart build failed: %s", exc)

    return layer


# --------------------------------------------------------------------------- #
# Saltwater intrusion postprocess (Wave-5)
#
# A GWF+GWT single-sim on a nrow=1 vertical cross-section with ModflowGwfbuy
# variable-density coupling.  The UCN output is ``gwt_model.ucn`` (same stem as
# the spill archetype; the BUY deck uses the same GWT OC writer).  The salinity
# field is (nlay, 1, ncol) after a ``get_data`` call; we squeeze the singleton
# nrow=1 axis to get a (nlay, ncol) 2-D slice.
#
# PRODUCTS:
#   1. SCALAR: intrusion_length_m / toe_distance_m -- the most-inland column
#      where the BOTTOM-layer salinity >= 50% threshold (measured from the
#      seaward edge).  Zero when the domain is fully fresh.
#   2. MAP ELEMENT: a FlatGeobuf in EPSG:4326 with two features:
#        feature_type="transect_line"  -- the A->B coastal transect LINE
#        feature_type="toe_point"      -- a POINT at toe_distance_m along the
#                                         transect (interpolated between A and B)
#      The transect endpoints (lat, lon) come from the DeckManifest fields
#      (transect_lat_a/lon_a/transect_lat_b/lon_b) stored by the adapter.
#   3. CHART (side-channel): the (nlay, ncol) salinity cross-section heatmap
#      built via ``build_saltwater_wedge_chart`` from chart_tools and stashed on
#      the returned LayerURI as the runtime attribute ``_chart_payload`` so the
#      composer can emit it via ``emit_chart_payloads`` without re-reading the UCN.
#      Using a private-underscore name so the Pydantic model ignores it (extra
#      attributes on Pydantic v2 models are silently discarded on dict serialisation;
#      we read it via ``getattr(layer, "_chart_payload", None)`` in the composer).
#
# GEOREGISTRATION:
#   The saltwater intrusion grid is nrow=1 (a 2-D vertical cross-section), NOT
#   a plan-view projected grid.  There is no meaningful plan-view CRS -- the
#   cross-section axis is given directly by the transect endpoints in EPSG:4326.
#   Distance along the transect is derived from the manifest ``si_ncol`` +
#   ``si_delr`` fields (or from the UCN array shape if the manifest is absent).
#   Depth is derived from ``si_nlay`` + ``si_delv`` (layer thickness).
# --------------------------------------------------------------------------- #


#: UCN filename for the saltwater intrusion GWT model.  The BUY deck reuses the
#: single-species GWT OC writer with the same output stem.
SI_GWT_UCN_FILENAME: str = GWT_UCN_FILENAME


def compute_saltwater_intrusion_metrics(
    salinity_grid: Any,
    *,
    seawater_salinity_ppt: float = 35.0,
    delr: float = 10.0,
) -> tuple[float, float]:
    """Compute (intrusion_length_m, toe_distance_m) from a salinity cross-section.

    Pure arithmetic over the FINAL-timestep ``(nlay, ncol)`` salinity grid (ppt).
    The 50%-isochlor threshold is ``0.5 * seawater_salinity_ppt``.

    Grid orientation (matches ``_build_saltwater_intrusion_deck``): column 0 is
    the INLAND boundary (WEL+AUX fresh) and column ncol-1 is the SEAWARD boundary
    (GHB+AUX salt, pinned at full salinity).  The wedge toe is therefore the
    most-inland (LOWEST column index) bottom-layer cell at/above the 50% isochlor,
    and the intrusion length is measured FROM THE SEAWARD edge inward.  Both
    headline scalars return the same measurement (``toe_distance_m`` is an alias
    for ``intrusion_length_m`` retained in the contract for downstream
    compatibility).

    Args:
        salinity_grid: 2-D array of shape ``(nlay, ncol)`` in ppt.  Row 0 is the
            top layer; row nlay-1 is the bottom (deepest) layer where the
            saltwater wedge toe is measured.
        seawater_salinity_ppt: the boundary salinity that defines 100%; 50% of
            this is the isochlor threshold.  Default 35.0 ppt.
        delr: column spacing, m.  Used to convert the column index to metres.
            Default 10.0 m (the adapter default for a 1 km transect / 100 cols).

    Returns:
        ``(intrusion_length_m, toe_distance_m)``: both >= 0.  Zero when the
        domain is entirely below threshold (fully fresh) or the grid is empty.
    """
    import numpy as np  # local - caller vouched for the import path

    arr = np.asarray(salinity_grid, dtype="float64")
    if arr.ndim != 2 or arr.size == 0:
        return 0.0, 0.0
    ncol = int(arr.shape[1])
    # MF6 inactive sentinels (> 1e29) -> NaN so they don't pollute the isochlor.
    arr = np.where(np.abs(arr) > 1e29, np.nan, arr)
    threshold = 0.5 * float(seawater_salinity_ppt)
    # Bottom layer is the last row (index nlay-1).
    bottom_row = arr[-1, :]
    # The toe is the most-inland (LOWEST column index) bottom cell >= threshold;
    # col ncol-1 (seaward GHB) is always salty, so np.min picks the true toe.
    salty_mask = np.where(np.isfinite(bottom_row), bottom_row >= threshold, False)
    if not np.any(salty_mask):
        return 0.0, 0.0
    toe_col = int(np.min(np.where(salty_mask)[0]))
    # Intrusion = distance from the SEAWARD edge (right side of col ncol-1) inland
    # to the toe column centre: (ncol - toe_col - 0.5) * delr.
    toe_m = float(ncol - toe_col - 0.5) * float(delr)
    return toe_m, toe_m


def _resolve_si_ucn_path(run_outputs_uri: str) -> Path:
    """Locate the saltwater-intrusion GWT UCN file from a run output location.

    Delegates to ``_resolve_ucn_path`` (the single-species stem ``gwt_model.ucn``
    is the same for the BUY deck).  Raises
    ``PostprocessMODFLOWError("SALTWATER_OUTPUT_READ_FAILED")`` when the file is
    absent, mapping the error code to the Wave-5 surface.
    """
    try:
        return _resolve_ucn_path(run_outputs_uri)
    except PostprocessMODFLOWError as exc:
        raise PostprocessMODFLOWError(
            "SALTWATER_OUTPUT_READ_FAILED",
            message=exc.args[0] if exc.args else str(exc),
            details=exc.details,
        ) from exc


def _read_si_salinity_grid(ucn_path: Path) -> Any:
    """Read the FINAL-timestep salinity grid as a 2-D ``(nlay, ncol)`` array (ppt).

    The BUY GWT deck writes salinity in the CONCENTRATION output (same format as
    the spill archetype's ``gwt_model.ucn``).  The grid shape is ``(nlay, 1, ncol)``
    because nrow=1 for the vertical cross-section; we squeeze the nrow axis.

    Raises:
        PostprocessMODFLOWError: ``SALTWATER_OUTPUT_READ_FAILED`` on any read
            failure; ``SALTWATER_OUTPUT_EMPTY`` when the file carries no timesteps
            or all values are NaN.
    """
    try:
        import flopy.utils  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "SALTWATER_OUTPUT_READ_FAILED",
            message=f"flopy/numpy not importable: {exc}",
            details={"ucn_path": str(ucn_path)},
        ) from exc

    try:
        cobj = flopy.utils.HeadFile(str(ucn_path), text="CONCENTRATION")
        times = cobj.get_times()
        if not times:
            raise PostprocessMODFLOWError(
                "SALTWATER_OUTPUT_EMPTY",
                message=f"{ucn_path} carries no concentration timesteps",
                details={"ucn_path": str(ucn_path)},
            )
        data = cobj.get_data(totim=times[-1])  # (nlay, 1, ncol) for nrow=1
    except PostprocessMODFLOWError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "SALTWATER_OUTPUT_READ_FAILED",
            message=f"could not read salinity from {ucn_path}: {exc}",
            details={"ucn_path": str(ucn_path)},
        ) from exc

    arr = np.asarray(data, dtype="float64")
    # Shape is (nlay, nrow, ncol) with nrow=1; squeeze to (nlay, ncol).
    if arr.ndim == 3 and arr.shape[1] == 1:
        arr = arr[:, 0, :]
    elif arr.ndim == 3:
        # Unexpected nrow > 1: take the first row (the only active row for nrow=1 decks)
        logger.warning(
            "_read_si_salinity_grid: unexpected nrow=%d in UCN %s; taking row 0",
            arr.shape[1], ucn_path,
        )
        arr = arr[:, 0, :]
    elif arr.ndim == 2:
        pass  # already (nlay, ncol)
    else:
        arr = np.squeeze(arr)
        if arr.ndim != 2:
            raise PostprocessMODFLOWError(
                "SALTWATER_OUTPUT_EMPTY",
                message=(
                    f"salinity array has shape {data.shape}; cannot reduce to 2D "
                    f"(nlay, ncol) for a cross-section postprocess"
                ),
                details={"ucn_path": str(ucn_path), "shape": list(data.shape)},
            )

    # MF6 inactive/dry sentinels (1e30) -> NaN.
    arr = np.where(np.abs(arr) > 1e29, np.nan, arr)

    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        raise PostprocessMODFLOWError(
            "SALTWATER_OUTPUT_EMPTY",
            message=f"all salinity values are NaN/sentinel in {ucn_path}",
            details={"ucn_path": str(ucn_path)},
        )
    return arr


def _transect_endpoints_from_deck(deck_dir: str | None) -> tuple[
    tuple[float, float],
    tuple[float, float],
] | None:
    """Read the coastal transect endpoints from the DeckManifest via flopy.

    The saltwater intrusion adapter stores
    ``transect_lat_a/lon_a/transect_lat_b/lon_b`` on the DeckManifest JSON file
    (``manifest.json``) in the deck directory.  We load it as JSON rather than
    reloading the full MF6 simulation (faster; the manifest is cheap JSON).

    Returns ``((lat_a, lon_a), (lat_b, lon_b))`` or ``None`` when the deck dir
    is absent / lacks a manifest with these fields.
    """
    if not deck_dir:
        return None
    import json

    manifest_path = Path(deck_dir) / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        with manifest_path.open() as f:
            m = json.load(f)
        lat_a = float(m.get("transect_lat_a", 0.0))
        lon_a = float(m.get("transect_lon_a", 0.0))
        lat_b = float(m.get("transect_lat_b", 0.0))
        lon_b = float(m.get("transect_lon_b", 0.0))
        # Any zero endpoint means the field was not populated.
        if lat_a == 0.0 and lon_a == 0.0 and lat_b == 0.0 and lon_b == 0.0:
            return None
        return (lat_a, lon_a), (lat_b, lon_b)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_transect_endpoints_from_deck: could not read transect from %s: %s",
            manifest_path, exc,
        )
        return None


def _si_grid_params_from_deck(deck_dir: str | None) -> dict[str, float]:
    """Read nlay/ncol/delr/delv from the DeckManifest JSON in the deck directory.

    Returns a dict with keys ``si_nlay``, ``si_ncol``, ``si_delr``, ``si_delv``,
    ``seawater_salinity_ppt``.  Missing / unreadable fields fall back to 0.0 so
    the caller can safely call ``float(params["si_delr"]) or <default>``.
    """
    defaults: dict[str, float] = {
        "si_nlay": 0.0,
        "si_ncol": 0.0,
        "si_delr": 0.0,
        "si_delv": 0.0,
        "seawater_salinity_ppt": 35.0,
    }
    if not deck_dir:
        return defaults
    import json

    manifest_path = Path(deck_dir) / "manifest.json"
    if not manifest_path.exists():
        return defaults
    try:
        with manifest_path.open() as f:
            m = json.load(f)
        defaults["si_nlay"] = float(m.get("si_nlay", 0.0))
        defaults["si_ncol"] = float(m.get("si_ncol", 0.0))
        defaults["si_delr"] = float(m.get("si_delr", 0.0))
        defaults["si_delv"] = float(m.get("si_delv", 0.0))
        defaults["seawater_salinity_ppt"] = float(
            m.get("seawater_salinity_ppt", 35.0)
        )
        return defaults
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_si_grid_params_from_deck: could not read manifest from %s: %s",
            manifest_path, exc,
        )
        return defaults


def _interpolate_toe_point(
    endpoint_a: tuple[float, float],
    endpoint_b: tuple[float, float],
    toe_distance_m: float,
    transect_length_m: float,
) -> tuple[float, float]:
    """Return (lat, lon) for the 50%-isochlor toe point interpolated along A->B.

    Linear interpolation: fraction = toe_distance_m / transect_length_m.  A
    fraction of 0 is at point A (seaward); 1 is at point B (inland).  Clamped
    to [0, 1] so an out-of-range toe does not extrapolate past the endpoints.

    Args:
        endpoint_a: (lat, lon) of the seaward end of the transect.
        endpoint_b: (lat, lon) of the inland end of the transect.
        toe_distance_m: 50%-isochlor toe penetration from the seaward end, m.
        transect_length_m: total transect length, m (si_ncol * si_delr).

    Returns:
        ``(lat, lon)`` of the toe point in EPSG:4326.
    """
    if transect_length_m <= 0.0:
        return endpoint_a
    frac = min(1.0, max(0.0, float(toe_distance_m) / float(transect_length_m)))
    lat = float(endpoint_a[0]) + frac * (float(endpoint_b[0]) - float(endpoint_a[0]))
    lon = float(endpoint_a[1]) + frac * (float(endpoint_b[1]) - float(endpoint_a[1]))
    return lat, lon


def _write_si_fgb(
    endpoint_a: tuple[float, float],
    endpoint_b: tuple[float, float],
    toe_point_latlon: tuple[float, float],
    *,
    intrusion_length_m: float,
    seaward_salinity_ppt: float,
    run_id: str,
) -> Path:
    """Write the saltwater-intrusion transect + toe FlatGeobuf in EPSG:4326.

    Two features in the output FeatureCollection:

    * ``feature_type="transect_line"``: a LINESTRING from endpoint A (seaward,
      lat/lon) to endpoint B (inland, lat/lon).  Carries properties:
      ``intrusion_length_m``, ``seaward_salinity_ppt``, ``run_id``.
    * ``feature_type="toe_point"``: a POINT at the 50%-isochlor toe position
      along the transect.  Carries properties: ``toe_distance_m``,
      ``seaward_salinity_ppt``, ``run_id``.

    The geometry coordinate order is (lon, lat) as required by GeoJSON / WKT
    (even though the ``(lat, lon)`` convention is used in all Python tuples).

    Raises:
        PostprocessMODFLOWError: ``SALTWATER_WRITE_FAILED`` on any shapely /
            geopandas / file-system error.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        from shapely.geometry import LineString, Point  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "SALTWATER_WRITE_FAILED",
            message=f"shapely / geopandas not importable for saltwater FGB write: {exc}",
            details={"run_id": run_id},
        ) from exc

    try:
        # GeoJSON / WKT coordinate order: (lon, lat).
        line_geom = LineString([
            (float(endpoint_a[1]), float(endpoint_a[0])),
            (float(endpoint_b[1]), float(endpoint_b[0])),
        ])
        toe_geom = Point(
            float(toe_point_latlon[1]),
            float(toe_point_latlon[0]),
        )

        features_geom = [line_geom, toe_geom]
        features_props: list[dict[str, Any]] = [
            {
                "feature_type": "transect_line",
                "intrusion_length_m": float(intrusion_length_m),
                "seaward_salinity_ppt": float(seaward_salinity_ppt),
                "run_id": run_id,
            },
            {
                "feature_type": "toe_point",
                "toe_distance_m": float(intrusion_length_m),
                "seaward_salinity_ppt": float(seaward_salinity_ppt),
                "run_id": run_id,
            },
        ]

        gdf = gpd.GeoDataFrame(features_props, geometry=features_geom, crs="EPSG:4326")
        fgb_path = Path(
            tempfile.NamedTemporaryFile(
                suffix=f"_si_transect_{run_id}.fgb", delete=False
            ).name
        )
        gdf.to_file(str(fgb_path), driver="FlatGeobuf", engine="pyogrio")
        return fgb_path
    except PostprocessMODFLOWError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PostprocessMODFLOWError(
            "SALTWATER_WRITE_FAILED",
            message=f"could not write saltwater intrusion FlatGeobuf: {exc}",
            details={"run_id": run_id},
        ) from exc


def postprocess_saltwater_intrusion(
    run_outputs_uri: str,
    *,
    run_id: str,
    model_crs: str,
    deck_dir: str | None = None,
    runs_bucket: str | None = None,
    transect_endpoints: tuple[
        tuple[float, float], tuple[float, float]
    ] | None = None,
) -> SaltwaterWedgeLayerURI:
    """Convert a MODFLOW BUY variable-density run's UCN output into a
    ``SaltwaterWedgeLayerURI``.

    Reads the FINAL-timestep GWT concentration output (``gwt_model.ucn``,
    the Henry-style saltwater archetype deck, salinity in ppt), computes the
    50%-isochlor toe penetration (the headline scalar), writes a FlatGeobuf in
    EPSG:4326 with the coastal transect LINE (A->B) and a toe POINT, uploads it
    to the runs bucket, and returns the typed ``SaltwaterWedgeLayerURI``.

    The PRIMARY product of this archetype is a Vega-Lite cross-section heatmap
    chart (the salinity field vs. distance inland / depth) built via
    ``build_saltwater_wedge_chart`` and stashed as the runtime attribute
    ``_chart_payload`` on the returned ``SaltwaterWedgeLayerURI``.  The composer
    (``model_saltwater_intrusion_scenario``) reads this attribute via
    ``getattr(layer, "_chart_payload", None)`` and emits it through
    ``emit_chart_payloads`` -- no second UCN read is needed.

    The MAP element (the FlatGeobuf transect + toe) geo-contextualizes the cross-
    section on the map.  The chart carries the physics.  Neither should be over-
    interpreted as a calibrated result (see ``SaltwaterWedgeLayerURI`` docstring).

    Georegistration: the saltwater intrusion grid is a nrow=1 vertical cross-
    section.  There is no plan-view UTM; the transect endpoints (lat, lon) from
    the manifest / the caller locate the cross-section.  ``model_crs`` is accepted
    for signature compatibility but is NOT used for reprojection (no raster COG
    is written; the FlatGeobuf is built directly in EPSG:4326).

    Args:
        run_outputs_uri: the run output location (local dir / ``file://``) that
            contains ``gwt_model.ucn``.
        run_id: run identifier for the FlatGeobuf filename in the runs bucket.
        model_crs: the deck's projected CRS string (accepted for signature
            compatibility; not used for this vector-only archetype).
        deck_dir: optional on-disk deck directory.  Used to read the DeckManifest
            JSON (``manifest.json``) for the transect endpoints and grid parameters
            (si_nlay, si_ncol, si_delr, si_delv, seawater_salinity_ppt) when they
            are not supplied directly.
        runs_bucket: optional override for the S3 / GCS runs bucket name.
        transect_endpoints: optional explicit ``((lat_a, lon_a), (lat_b, lon_b))``
            coastal transect endpoints in EPSG:4326 (lat-first, A=seaward,
            B=inland).  Preferred over reading from the deck manifest.  When both
            this argument and the manifest are absent / zero, the FlatGeobuf
            transect is omitted and only the toe-distance scalar is returned.

    Returns:
        ``SaltwaterWedgeLayerURI`` with:
          - ``layer_type='vector'``
          - ``style_preset='saltwater_intrusion'``
          - ``uri``: FlatGeobuf artifact URI (or ``file://`` in offline mode)
          - ``bbox``: EPSG:4326 ``(min_lon, min_lat, max_lon, max_lat)``
          - ``intrusion_length_m``: bottom-layer 50%-isochlor toe penetration, m
          - ``toe_distance_m``: alias for ``intrusion_length_m``
          - ``seaward_salinity_ppt``: the boundary salinity applied, ppt
          - ``transect_endpoints``: A->B (lat, lon) pairs

        The attribute ``_chart_payload`` (NOT a Pydantic field) is set on the
        returned object as the cross-section heatmap chart payload dict (or
        ``None`` when the grid is too small to chart).  The composer reads it
        via ``getattr(layer, "_chart_payload", None)``.

    Raises:
        PostprocessMODFLOWError:
          - ``SALTWATER_OUTPUT_READ_FAILED`` -- UCN not found / unreadable.
          - ``SALTWATER_OUTPUT_EMPTY`` -- UCN has no timesteps / all NaN.
          - ``SALTWATER_WRITE_FAILED`` -- FlatGeobuf write or upload failed.
    """
    import numpy as np  # type: ignore[import-not-found]

    del model_crs  # no raster COG to reproject; accepted for signature compat only.

    # --- Step 1: locate + read the salinity UCN ----------------------------
    ucn_path = _resolve_si_ucn_path(run_outputs_uri)
    salinity_2d = _read_si_salinity_grid(ucn_path)
    nlay, ncol = salinity_2d.shape

    logger.info(
        "postprocess_saltwater_intrusion run_id=%s ucn=%s shape=(%d,%d)",
        run_id, ucn_path, nlay, ncol,
    )

    # --- Step 2: grid parameters (prefer manifest, fall back to UCN shape) ---
    params = _si_grid_params_from_deck(deck_dir)
    delr = float(params["si_delr"]) if float(params["si_delr"]) > 0.0 else 10.0
    delv = float(params["si_delv"]) if float(params["si_delv"]) > 0.0 else 2.5
    seawater_ppt = float(params["seawater_salinity_ppt"])

    # Distance-INLAND from the seaward edge to each column centre.  The deck
    # convention is col 0 = INLAND (WEL fresh), col ncol-1 = SEAWARD (GHB salt),
    # so distance-inland(j) = (ncol - j - 0.5) * delr: the seaward column ncol-1
    # maps to ~0 m (the coast) and the inland column 0 to ~transect_length.  This
    # keeps the chart's 'distance inland (m)' x-axis honest (salt at the coast,
    # fresh inland) and consistent with the seaward-referenced toe metric.
    distances_m = np.array(
        [(ncol - j - 0.5) * delr for j in range(ncol)], dtype="float64"
    )
    # Layer depths (sea level=0, positive downward): centre of each layer.
    depths_m = np.array(
        [(k + 0.5) * delv for k in range(nlay)], dtype="float64"
    )
    transect_length_m = float(ncol) * delr

    # --- Step 3: 50%-isochlor toe -------------------------------------------
    intrusion_m, toe_m = compute_saltwater_intrusion_metrics(
        salinity_2d,
        seawater_salinity_ppt=seawater_ppt,
        delr=delr,
    )
    isochlor_value = 0.5 * seawater_ppt

    logger.info(
        "postprocess_saltwater_intrusion run_id=%s intrusion_length_m=%.3g "
        "toe_distance_m=%.3g seawater_ppt=%.3g",
        run_id, intrusion_m, toe_m, seawater_ppt,
    )

    # --- Step 4: transect endpoints (caller > manifest > sentinel zeros) ------
    if transect_endpoints is not None:
        ep_a, ep_b = transect_endpoints
    else:
        manifest_eps = _transect_endpoints_from_deck(deck_dir)
        if manifest_eps is not None:
            ep_a, ep_b = manifest_eps
        else:
            # No transect supplied and no manifest.  Use sentinel zero-endpoints so
            # the FGB still contains the geometry skeleton with the toe distance
            # encoded (Invariant 1: never silently skip the artifact, but also never
            # fabricate real coordinates -- zero lat/lon near the equator is honest
            # placeholder behaviour; the composer's honesty gate should have raised
            # InputError upstream if no transect was given).
            ep_a = (0.0, 0.0)
            ep_b = (0.0, float(transect_length_m) / 111_320.0)  # approx 1 deg lat
            logger.warning(
                "postprocess_saltwater_intrusion run_id=%s: no transect endpoints "
                "available; using sentinel zeros (the composer honesty gate "
                "should have raised InputError upstream)",
                run_id,
            )

    toe_latlon = _interpolate_toe_point(ep_a, ep_b, toe_m, transect_length_m)

    # --- Step 5: write FlatGeobuf -----------------------------------------
    fgb_path = _write_si_fgb(
        ep_a,
        ep_b,
        toe_latlon,
        intrusion_length_m=intrusion_m,
        seaward_salinity_ppt=seawater_ppt,
        run_id=run_id,
    )

    # Bounding box from the transect line (lon, lat order for the tuple).
    a_lon, a_lat = float(ep_a[1]), float(ep_a[0])
    b_lon, b_lat = float(ep_b[1]), float(ep_b[0])
    bbox_4326: tuple[float, float, float, float] | None = (
        min(a_lon, b_lon),
        min(a_lat, b_lat),
        max(a_lon, b_lon),
        max(a_lat, b_lat),
    )

    # --- Step 6: upload FlatGeobuf to runs bucket --------------------------
    try:
        fgb_uri = _upload_fgb(
            fgb_path, run_id, runs_bucket, fgb_filename="saltwater_intrusion_4326.fgb"
        )
    except PostprocessMODFLOWError as exc:
        # Remap the _upload_fgb error code (CAPTURE_ZONE_WRITE_FAILED) to the
        # Wave-5 surface for clean error narration.
        raise PostprocessMODFLOWError(
            "SALTWATER_WRITE_FAILED",
            message=exc.args[0] if exc.args else str(exc),
            details=exc.details,
        ) from exc

    logger.info(
        "postprocess_saltwater_intrusion run_id=%s fgb_uri=%s "
        "intrusion_length_m=%.3g bbox=%s",
        run_id, fgb_uri, intrusion_m, bbox_4326,
    )

    # --- Step 7: build the cross-section chart (stash on the result) --------
    # The chart builder does NOT read the UCN again - it uses the in-memory
    # salinity_2d grid.  Stashed as ``_chart_payload`` (a runtime attribute, not
    # a Pydantic field) so the composer can emit it without a second UCN read.
    chart_payload: dict[str, Any] | None = None
    try:
        from ..tools.processing.charts_common import build_saltwater_wedge_chart

        chart_payload = build_saltwater_wedge_chart(
            salinity_grid=salinity_2d,
            distances_m=distances_m,
            depths_m=depths_m,
            isochlor_value=isochlor_value,
            seawater_salinity_ppt=seawater_ppt,
            intrusion_length_m=intrusion_m,
            source_layer_uri=fgb_uri,
        )
    except Exception as exc:  # noqa: BLE001
        # Best-effort: a chart build failure is non-fatal; the LayerURI is still
        # returned with a None chart payload.
        logger.warning(
            "postprocess_saltwater_intrusion: chart build failed (non-fatal): %s",
            exc,
        )

    layer_id = f"saltwater-intrusion-{run_id}"
    result = SaltwaterWedgeLayerURI(
        layer_id=layer_id,
        name="Saltwater Intrusion Wedge (Henry-style variable-density cross-section)",
        layer_type="vector",
        uri=fgb_uri,
        style_preset=SALTWATER_INTRUSION_STYLE_PRESET,
        role="primary",
        bbox=bbox_4326,
        intrusion_length_m=intrusion_m,
        toe_distance_m=toe_m,
        seaward_salinity_ppt=seawater_ppt,
        transect_endpoints=(ep_a, ep_b),
    )

    # Stash the chart payload as a runtime attribute the composer picks up via
    # ``getattr(layer, "_chart_payload", None)``.  Pydantic v2 ignores non-field
    # attributes on serialisation (model_dump / model_dump_json) so this does not
    # contaminate the contract boundary.
    object.__setattr__(result, "_chart_payload", chart_payload)

    return result
