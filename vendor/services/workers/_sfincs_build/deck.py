"""SFINCS regular-grid deck build — WORKER-side (heavy-compute offload).

SINGLE-PURPOSE offload module (reference implementation of the heavy-compute
offload effort, ``reports/design/heavy-compute-offload-2026-07-02.md``): the
hydromt-SFINCS model BUILD that used to run IN the always-on agent process (the
16 GB Chattanooga-OOM driver) now runs HERE, inside the tear-down ``grace2-sfincs``
Batch worker, alongside the solve + the raster postprocess.

The pure build functions below (dataclasses, Manning mapping loader + the OQ-4 §4
NLCD validation gate, the adaptive-grid autoscale, the HydroMT YAML config
generator + surge/physics emitters) are VENDORED VERBATIM from the agent's
``services/agent/src/grace2_agent/workflows/sfincs_builder.py`` — that module
remains the source of truth for the build contract (and the agent's legacy
local-docker build). ``services/workers/`` is NOT on the agent import path
(mirrors the ``_raster_postprocess`` split), so the worker keeps its own copy;
keep the two in sync when the build contract changes.

The worker DIFFERS from the agent build in exactly two ways, both because the
worker builds LOCALLY (no S3 deck round-trip):
  * inputs (DEM / landcover / river / every forcing file) are pre-downloaded to
    local paths by the orchestrator (``build_sfincs_deck``) via an injected
    ``download`` callable, so the vendored ``_stage_gcs_local`` only ever sees
    local paths (identity passthrough);
  * there is NO ModelSetup return + NO deck upload — the orchestrator writes the
    deck into ``<scratch>/deck`` and returns a plain provenance dict; the
    entrypoint then runs SFINCS in that dir and postprocesses in place.
"""

from __future__ import annotations

import csv
import logging
import math
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("grace2.worker.sfincs_build")


# --------------------------------------------------------------------------- #
# GDAL/rasterio env (vendored from sfincs_builder — stabilize remote reads).
# --------------------------------------------------------------------------- #
os.environ.setdefault("GDAL_NUM_THREADS", "1")
# Modest VSI cache + timeout for transient GCS hiccups (FR-DT-2 cache is
# external; this is the per-read pace inside GDAL).
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "60")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "3")
os.environ.setdefault("GDAL_HTTP_RETRY_DELAY", "1")
os.environ.setdefault("CPL_VSIL_CURL_CHUNK_SIZE", "1048576")  # 1 MiB


# --------------------------------------------------------------------------- #
# pandas>=2.0 guard for hydromt-sfincs 1.2.2 ``set_forcing_1d`` (COASTAL SFINCS)
# --------------------------------------------------------------------------- #
#
# hydromt-sfincs 1.2.2 ``SfincsModel.set_forcing_1d`` (sfincs.py:1858-1871) is
# the shared 1D-forcing sink for EVERY surge / tide / river-discharge boundary —
# ``setup_waterlevel_forcing`` (``bzs``), ``setup_river_inflow`` +
# ``setup_discharge_forcing`` (``dis``) all funnel through it. It calls three
# pandas ``Index`` methods that were DEPRECATED in pandas 2.0 and REMOVED in
# pandas 3.0::
#
#     gdf_locs.index.is_integer()      # sfincs.py:1858
#     df_ts.columns.is_integer()       # sfincs.py:1869
#     df_ts.index.is_numeric()         # sfincs.py:1871
#
# On the agent venv (pandas 2.2.3) these still exist but emit FutureWarning; on
# any pandas>=3.0 they raise ``AttributeError: 'RangeIndex' object has no
# attribute 'is_integer'`` deep inside the surge/river forcing path — exactly
# the forcing path the COASTAL SFINCS North Star needs. The kickoff forbids
# editing the installed package and a hard ``pandas<2.0`` pin would fight the
# rest of the stack, so we GUARD in OUR code: re-attach the removed methods to
# ``pandas.Index`` (idempotent; a no-op where they already exist) delegating to
# the supported ``pandas.api.types`` predicates the deprecation warnings name.
# Installed at import time so any importer (the build path, the worker, tests)
# inherits the safe shim. NEVER raises — a guard failure logs and proceeds (the
# methods exist on 2.x, so the only realistic failure is a future pandas API
# change we'd rather degrade through than crash the whole module import on).
def _install_pandas_set_forcing_1d_guard() -> bool:
    """Re-attach ``Index.is_integer`` / ``Index.is_numeric`` if pandas dropped them.

    Returns ``True`` if the guard ran cleanly (whether or not it had to patch),
    ``False`` if the guard itself errored (logged, never raised). Idempotent:
    safe to call repeatedly; only attaches a method that is genuinely absent.
    """
    try:
        import pandas as pd  # type: ignore[import-not-found]
        import pandas.api.types as pat  # type: ignore[import-not-found]

        index_cls = pd.Index
        patched: list[str] = []
        if not hasattr(index_cls, "is_integer"):
            def _is_integer(self):  # noqa: ANN001, ANN202
                return bool(pat.is_integer_dtype(self.dtype))

            index_cls.is_integer = _is_integer  # type: ignore[attr-defined]
            patched.append("is_integer")
        if not hasattr(index_cls, "is_numeric"):
            def _is_numeric(self):  # noqa: ANN001, ANN202
                return bool(pat.is_any_real_numeric_dtype(self.dtype))

            index_cls.is_numeric = _is_numeric  # type: ignore[attr-defined]
            patched.append("is_numeric")
        if patched:
            logger.info(
                "pandas guard: re-attached pandas.Index.%s for hydromt-sfincs "
                "set_forcing_1d (pandas %s removed them)",
                "/".join(patched),
                getattr(pd, "__version__", "?"),
            )
        return True
    except Exception as exc:  # noqa: BLE001 — never break module import on the guard
        logger.warning(
            "pandas guard for set_forcing_1d could not install (%s); surge/river "
            "forcing may raise on pandas>=3.0",
            exc,
        )
        return False


_PANDAS_GUARD_OK = _install_pandas_set_forcing_1d_guard()


# --- Manning's mapping CSV (co-located with this worker module) ---
MANNING_MAPPING_PATH: Path = Path(__file__).parent / "manning_mapping.csv"
MANNING_MAPPING_VERSION: str = "1.0.0"


class SFINCSSetupError(RuntimeError):
    """Raised by ``build_sfincs_model`` on any setup-time failure.

    The ``error_code`` is the A.6 open-set code surfaced to the WS error frame
    and threaded into the final ``AssessmentEnvelope`` when the workflow
    returns a failed envelope. ``details`` is a free-form dict with the gate
    specifics — for ``LULC_MAPPING_MISMATCH`` it carries::

        {
          "nlcd_vintage_year": int,
          "mapping_version": str,
          "unmapped_classes": list[int],
          "mapping_csv_path": str,
        }

    Open-set codes used by this module (per OQ-4 §5 OQ-4c):
    - ``LULC_MAPPING_MISMATCH`` — the **headline**; gate fired before HydroMT
      ran the roughness component.
    - ``DEM_COVERAGE_GAP`` — DEM bytes were not readable or had no spatial
      overlap with the bbox (defensive; HydroMT also catches this).
    - ``FORCING_OUT_OF_RANGE`` — forcing tuple was empty or carried no
      precipitation depth.
    - ``HYDROMT_UNAVAILABLE`` — ``import hydromt_sfincs`` failed in the
      runtime (container missing the dep — surfaces as schema-pushback to
      infra job-0040).
    - ``HYDROMT_BUILD_FAILED`` — HydroMT itself raised during the build (any
      uncaught underlying error is re-raised wrapped in this code).
    """

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
# Forcing + options surface (engine-internal)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WaterlevelForcing:
    """Surge / tide water-level boundary forcing (SFINCS ``bzs``).

    COASTAL SFINCS North Star: the surge / tide hydrograph the boundary cells
    are driven with. Two ways the deck can ingest it, mirroring
    ``SfincsModel.setup_waterlevel_forcing(timeseries=..., locations=...)`` /
    ``geodataset=...``:

    - ``timeseries_uri`` + ``locations_uri`` — a tabular CSV of water-level
      time series (first column the datetime index, remaining columns the
      integer station ids) PLUS a point geofile of those station locations.
      This is what the fetcher fan-out (``fetch_gtsm_tide_surge`` /
      ``fetch_noaa_coops_tides``) materialises: each station's
      ``times``/``values`` hydrograph → CSV, station coords → points.
    - ``geodataset_uri`` — a single geospatial point-timeseries (netCDF / zarr)
      carrying both the water-level series and the point geometry; HydroMT's
      ``geodataset`` path reads it directly.

    ``offset`` (optional, metres) is the vertical-datum offset added to the
    series (e.g. MSL→NAVD88) — passed verbatim to ``setup_waterlevel_forcing``.
    ``buffer_m`` bounds the gauge-selection buffer around the water-level
    boundary cells (HydroMT default 5 km).
    """

    timeseries_uri: str | None = None
    locations_uri: str | None = None
    geodataset_uri: str | None = None
    offset: float | None = None
    buffer_m: float | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DischargeForcing:
    """River-inflow discharge boundary forcing (SFINCS ``dis``).

    Fluvial / compound-flood coupling: the river-discharge hydrograph
    (``fetch_noaa_nwm_streamflow`` / ``fetch_cama_flood_discharge``) injected at
    the points where rivers enter the domain.

    ORDER MATTERS (hydromt-sfincs contract): ``setup_river_inflow`` must run
    BEFORE ``setup_discharge_forcing`` — ``setup_river_inflow`` establishes the
    ``src`` discharge points (and trims boundary cells the river crosses), then
    ``setup_discharge_forcing`` attaches the time series to those points. The
    YAML emitter encodes both, in that order, whenever this member is present.

    - ``rivers_uri`` / ``hydrography_uri`` feed ``setup_river_inflow`` (river
      centrelines or an upstream-area+flow-direction hydrography raster); at
      least one is needed for the inflow points.
    - ``timeseries_uri`` (+ optional ``locations_uri``) is the discharge series
      handed to ``setup_discharge_forcing`` (m3/s), same tabular shape as the
      water-level series.
    - ``river_upa_km2`` is the minimum upstream-area threshold for a river to
      count as an inflow (HydroMT default 10 km2).
    """

    timeseries_uri: str | None = None
    locations_uri: str | None = None
    rivers_uri: str | None = None
    hydrography_uri: str | None = None
    river_upa_km2: float | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WindForcing:
    """Wind forcing — uniform (``setup_wind_forcing``) or gridded.

    - Uniform: ``magnitude`` (m/s) + ``direction`` (deg, where the wind comes
      FROM; 0=N, 90=E) → ``setup_wind_forcing(magnitude=, direction=)``. This is
      the quick storm-wind paddle for the coastal demo.
    - Gridded: ``grid_uri`` (a netCDF with ``wind10_u`` / ``wind10_v`` over
      ``time,y,x``) → ``setup_wind_forcing_from_grid(wind=)`` for a real
      spatially-varying wind field (e.g. ERA5 / HRRR).
    """

    magnitude: float | None = None
    direction: float | None = None
    grid_uri: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PressureForcing:
    """Gridded mean-sea-level-pressure forcing (``setup_pressure_forcing_from_grid``).

    ``grid_uri`` is a netCDF with ``press_msl`` (Pa) over ``time,y,x`` (e.g.
    ERA5). The inverse-barometer setup that completes a surge deck alongside
    wind. ``fill_value`` (Pa) is the no-data fill (standard atmosphere 101325).
    """

    grid_uri: str
    fill_value: float | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InfiltrationForcing:
    """Soil-infiltration LOSS term (SFINCS ``scsfile`` / ``qinffile`` / ``qinf``).

    NATE 2026-06-26: the missing forcing archetype. Infiltration is a loss, not
    a driver, so it is emitted whenever set but is NOT counted by
    ``has_surge_forcing`` (a pure-infiltration deck still needs a precip/surge
    driver to flood). Three mutually-exclusive emission paths, in precedence:

    - ``cn_uri`` -> ``setup_cn_infiltration(cn=, antecedent_moisture=)`` writes
      ``scsfile`` (SCS curve-number method). For a SINGLE-BAND GCN250 raster
      ``antecedent_moisture`` MUST be ``None`` (emitted as YAML ``null``): the
      default ``'avg'`` looks for a ``cn_avg`` data_var inside a Dataset and
      ValueErrors on a bare DataArray band. CN WINS over a constant (HydroMT's
      setup_cn_infiltration pops the default ``qinf`` config).
    - ``lulc_uri`` + ``reclass_table_uri`` -> ``setup_constant_infiltration(
      lulc=, reclass_table=)`` writes ``qinffile`` (per-class constant mm/hr map).
    - ``constant_mm_per_hr`` -> a bare scalar ``qinf`` (mm/hr) via the
      setup_config passthrough (setup_constant_infiltration REQUIRES a raster /
      lulc, so a spatially-uniform constant routes through sfincs.inp:qinf).
    """

    cn_uri: str | None = None
    antecedent_moisture: str | None = None
    constant_mm_per_hr: float | None = None
    lulc_uri: str | None = None
    reclass_table_uri: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ForcingSpec:
    """Compact specification of the design-storm forcing for SFINCS.

    For the v0.1 substrate ``model_flood_scenario`` constructs this from the
    ``lookup_precip_return_period`` atomic-tool output — a single
    precipitation depth + duration + ARI metadata. The COASTAL SFINCS North
    Star extends it with the surge / tide / discharge / wind / pressure members
    below, populated from the forcing fetchers
    (``fetch_gtsm_tide_surge`` / ``fetch_noaa_coops_tides`` /
    ``fetch_noaa_nwm_streamflow`` / ``fetch_cama_flood_discharge`` / ERA5); the
    shape is intentionally open enough to grow.

    Surge / compound-flood members (all optional; ``None`` → that forcing block
    is not emitted, so a pure-pluvial deck is byte-identical to v0.1):

    - ``waterlevel`` — ``WaterlevelForcing`` (surge + tide ``bzs`` boundary).
    - ``discharge`` — ``DischargeForcing`` (river-inflow ``dis`` boundary;
      ``setup_river_inflow`` is emitted BEFORE ``setup_discharge_forcing``).
    - ``wind`` — ``WindForcing`` (uniform or gridded wind).
    - ``pressure`` — ``PressureForcing`` (gridded MSL pressure).

    Fields used by the v0.1 pluvial SFINCS deck:

    - ``forcing_type`` — drives the SFINCS forcing component(s) HydroMT
      configures (``"pluvial_synthetic"`` → uniform rainfall hyetograph from
      an Atlas 14 design storm; ``"pluvial_observed"`` → uniform rainfall
      hyetograph from an OBSERVED precip raster (job-0225 v2, area-mean
      netamt fallback); ``"storm_surge"`` → wind/pressure/water-level series;
      future).
    - ``precip_inches`` — total depth from Atlas 14 (design-storm path).
    - ``duration_hours`` — design-storm / accumulation duration (Atlas 14 row
      for ``pluvial_synthetic``; the precip-raster accumulation window for
      ``pluvial_observed``).
    - ``return_period_years`` — ARI (Atlas 14 column; ``None`` for observed
      forcing — observed precip has no ARI).
    - ``precip_magnitude_mm_per_hr`` — pre-computed uniform-rain rate in mm/hr
      (job-0225 v2 ``pluvial_observed`` netamt path). When set, the YAML
      emitter uses it VERBATIM as the SFINCS ``setup_precip_forcing``
      ``magnitude`` (mm/hr) — bypassing the Atlas 14
      ``precip_inches / duration_hours`` arithmetic. This is the seam where
      the area-mean of a real precip raster (MRMS QPE, ERA5, gridMET …)
      enters the deck. ``None`` for the design-storm path (where magnitude is
      derived from ``precip_inches``). See ``model_flood_scenario``'s
      ``forcing_raster_uri`` branch + OQ-6 (area-mean netamt v0.1; spw
      upgrade path documented there).
    - ``provenance`` — free-form dict echoed into ``ForcingSummary.parameters``
      so the AssessmentEnvelope carries the Atlas 14 volume / project_area /
      vintage strings (design storm) or the precip-raster URI + area-mean
      depth (observed) for narration.
    """

    forcing_type: str
    precip_inches: float | None = None
    duration_hours: float | None = None
    return_period_years: int | None = None
    precip_magnitude_mm_per_hr: float | None = None
    # COASTAL SFINCS surge / compound-flood members (None → block not emitted).
    waterlevel: WaterlevelForcing | None = None
    discharge: DischargeForcing | None = None
    # NATE 2026-06-26: ``breach`` is an INTERIOR levee-breach point source (reuses
    # DischargeForcing with timeseries_uri+locations_uri set at the breach point,
    # rivers_uri/hydrography_uri left None). Distinct from ``discharge`` (a
    # domain-EDGE river inflow) so a compound run can carry BOTH; emitted as a
    # SECOND setup_discharge_forcing(merge: true) with NO setup_river_inflow.
    breach: DischargeForcing | None = None
    wind: WindForcing | None = None
    pressure: PressureForcing | None = None
    # NATE 2026-06-26: infiltration is a LOSS term (scsfile/qinffile/qinf), not a
    # driver -> emitted when set but NOT counted by has_surge_forcing().
    infiltration: InfiltrationForcing | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    def has_surge_forcing(self) -> bool:
        """True iff any non-precip (surge/tide/discharge/breach/wind/pressure) member is set.

        NATE 2026-06-26: ``breach`` is a DRIVER (an interior discharge jet) so it
        joins the any(); ``infiltration`` is a loss term and is deliberately
        excluded (it never drives a flood on its own).
        """
        return any(
            m is not None
            for m in (
                self.waterlevel,
                self.discharge,
                self.breach,
                self.wind,
                self.pressure,
            )
        )


@dataclass(frozen=True)
class BuildOptions:
    """Knobs ``build_sfincs_model`` exposes for engine-internal tuning.

    The workflow caller populates these from defaults — never user-input — per
    Decision K. Surfaces:

    - ``grid_resolution_m`` — SFINCS grid spacing. Defaults to 30 m to match
      NLCD native + NFR-P-4 (≤200 km² at 30 m).
    - ``simulation_hours`` — total simulation length (storm duration + spin-up).
    - ``crs`` — projected CRS the model grid is built in. SFINCS runs in a
      projected metric CRS; we use EPSG:3857 (Web Mercator) as a generic
      default for the v0.1 smoke. A production-grade default would route to
      the appropriate UTM zone per bbox center — captured as
      OQ-42-MODEL-CRS-AUTO-UTM (TENTATIVE: EPSG:3857 for v0.1 smoke).
    - ``output_setup_uri`` — explicit override for the staged deck's gs:// URI.
      When ``None`` we derive one inside the cache bucket.
    - ``compute_class`` — FR-CE-3 compute class the solve will run on; feeds the
      autoscale cap sizing (vCPU → cell cap via the perf model). The workflow
      passes the same class it hands ``run_solver``. Provenance only otherwise.
    - ``autoscale_grid`` — when ``True`` (default), ``build_sfincs_model`` snaps
      ``grid_resolution_m`` UP the resolution ladder so the estimated active-cell
      count fits the solve budget (sprint-16). Set ``False`` to pin
      ``grid_resolution_m`` verbatim (tests / explicit overrides).
    - ``enable_subgrid`` — emit a ``setup_subgrid`` block. Subgrid tables let
      SFINCS run on a COARSE computational grid while still resolving local
      topography + roughness at sub-pixel resolution — the standard way to get
      an urban-flood-around-buildings estimate cheaply (the COASTAL North Star's
      "rough urban flood" ask). Default ``False`` (v0.1 pluvial decks stay on
      the plain ``setup_dep`` + ``setup_manning_roughness`` path).
    - ``subgrid_nr_subgrid_pixels`` — sub-pixels per computational cell in the
      subgrid tables (HydroMT default 20). Higher = finer sub-cell topography,
      more build cost.
    - ``building_obstacle_uri`` — a vector geofile (FlatGeobuf / GeoJSON) of
      building footprints (``fetch_buildings`` OSM Overpass output). When set,
      the footprints are burned into the deck as a BUILDING-OBSTACLE mask so the
      flow routes AROUND buildings (a rough 2D urban-flood estimate). Burned via
      ``setup_subgrid`` ``datasets_riv`` raised-bank cells when subgrid is on,
      and/or as a ``setup_mask_active`` ``exclude_mask`` (footprint cells become
      INACTIVE / no-flow). Default ``None`` (no obstacles).
    - ``building_obstacle_mode`` — how footprints enter the deck:
      ``"exclude"`` (default) makes building cells INACTIVE (hard no-flow holes —
      the fast/rough approximation NATE asked for); ``"raise"`` keeps them active
      but raises their bed elevation via the subgrid so water is impeded but the
      domain stays connected. ``"raise"`` requires ``enable_subgrid=True``.
    """

    grid_resolution_m: float = 30.0
    simulation_hours: float = 24.0
    crs: str = "EPSG:3857"
    output_setup_uri: str | None = None
    compute_class: str = "medium"
    autoscale_grid: bool = True
    # COASTAL/WAVE animation cadence (coastal surge+SnapWave "looks like rain"
    # fix): the SFINCS map-output stride (``dtout``/``dtmaxout``) in MINUTES.
    # ``None`` (default) = the legacy hourly cadence (``max(600, total/24)``),
    # byte-identical to the pluvial deck. A coastal/wave run passes a FINE value
    # (e.g. 5) so SFINCS writes minute-scale snapshots and the animation reads as
    # water rolling in (waves move in seconds-to-minutes; hourly snapshots of a
    # rising surge look like a slowly-filling bathtub regardless of the wave
    # model). The physical floor is 60 s for a wave run (see the dtout wiring).
    output_interval_min: float | None = None
    # COASTAL SFINCS — subgrid + building-obstacle mask (urban-flood estimate).
    enable_subgrid: bool = False
    subgrid_nr_subgrid_pixels: int = 20
    building_obstacle_uri: str | None = None
    building_obstacle_mode: str = "exclude"
    # NATE 2026-06-26: advanced-physics overrides (advection/theta/alpha/huthresh
    # /coriolis_latitude/wind_drag) resolved via physics_registry.
    # validate_and_resolve_physics('sfincs', overrides). The composer passes the
    # RESOLVED dict (single resolve point); ``_emit_physics_config`` writes each
    # present key into the setup_config passthrough -> sfincs.inp. ``None`` (the
    # default) emits nothing, so a deck without overrides is byte-identical.
    advanced_physics: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Manning's mapping loader + NLCD vintage validation gate
# --------------------------------------------------------------------------- #


def load_manning_mapping(
    csv_path: Path | str | None = None,
) -> dict[int, float]:
    """Load the version-pinned NLCD class → Manning's n mapping.

    Reads ``manning_mapping.csv`` (default: the module-local file) and returns
    a dict keyed by NLCD class integer. Comments (``#``) and empty lines are
    ignored; the CSV header row is consumed; data rows must have exactly two
    numeric columns at indices 0 (nlcd_class) and 1 (manning_n). Optional
    columns (e.g. ``description``) are tolerated.

    Args:
        csv_path: optional explicit override (tests use this to inject a fixture
            CSV with only a subset of classes); ``None`` reads
            ``MANNING_MAPPING_PATH``.

    Returns:
        ``{nlcd_class_int: manning_n_float}`` — every row in the CSV becomes
        an entry; duplicates are last-wins with a logged warning.

    Raises:
        SFINCSSetupError("MANNING_MAPPING_LOAD_FAILED", …): the CSV is missing,
            empty, or unparseable.
    """
    path = Path(csv_path) if csv_path is not None else MANNING_MAPPING_PATH
    if not path.exists():
        raise SFINCSSetupError(
            "MANNING_MAPPING_LOAD_FAILED",
            message=f"Manning's mapping CSV not found at {path}",
            details={"path": str(path)},
        )

    mapping: dict[int, float] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            # Skip leading comments + blank lines.
            data_lines = [
                line
                for line in fh.readlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        if not data_lines:
            raise SFINCSSetupError(
                "MANNING_MAPPING_LOAD_FAILED",
                message=f"Manning's mapping CSV at {path} is empty after stripping comments",
                details={"path": str(path)},
            )
        reader = csv.reader(data_lines)
        header = next(reader, None)
        if header is None or not header:
            raise SFINCSSetupError(
                "MANNING_MAPPING_LOAD_FAILED",
                message=f"Manning's mapping CSV at {path} has no header row",
                details={"path": str(path)},
            )
        for row_idx, row in enumerate(reader, start=2):
            if not row or all(not c.strip() for c in row):
                continue
            if len(row) < 2:
                continue
            try:
                cls = int(row[0].strip())
                n_val = float(row[1].strip())
            except (ValueError, IndexError):
                logger.warning(
                    "manning_mapping row %d not parseable: %r (skipped)",
                    row_idx,
                    row,
                )
                continue
            if cls in mapping:
                logger.warning(
                    "manning_mapping duplicate nlcd_class=%d at row %d "
                    "(was %.4f, now %.4f) — last-wins",
                    cls,
                    row_idx,
                    mapping[cls],
                    n_val,
                )
            mapping[cls] = n_val
    except SFINCSSetupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SFINCSSetupError(
            "MANNING_MAPPING_LOAD_FAILED",
            message=f"Manning's mapping CSV at {path} could not be parsed: {exc}",
            details={"path": str(path)},
        ) from exc

    if not mapping:
        raise SFINCSSetupError(
            "MANNING_MAPPING_LOAD_FAILED",
            message=f"Manning's mapping CSV at {path} parsed to an empty mapping",
            details={"path": str(path)},
        )
    return mapping


def validate_nlcd_vintage_against_mapping(
    fetched_classes: set[int],
    nlcd_vintage_year: int,
    mapping: dict[int, float],
    mapping_version: str = MANNING_MAPPING_VERSION,
    mapping_csv_path: str | None = None,
) -> None:
    """The **OQ-4 §4 Invariant-7 mitigation gate**.

    Verifies that every NLCD class integer observed in the fetched landcover
    raster is present in the Manning's mapping. If any class is missing, raises
    ``SFINCSSetupError("LULC_MAPPING_MISMATCH")`` carrying the specifics so the
    workflow surface can render a failed AssessmentEnvelope rather than
    dispatching a model with HydroMT's silently-filled Manning's defaults to
    the solver.

    Args:
        fetched_classes: the set of integer class codes present in the fetched
            NLCD landcover raster. The workflow layer reads the raster
            (lazily) and extracts the unique class set; this gate is the
            verification step.
        nlcd_vintage_year: the NLCD vintage year (e.g. 2021) returned in the
            ``fetch_landcover`` sidecar.
        mapping: the loaded NLCD → Manning's mapping (from
            ``load_manning_mapping``).
        mapping_version: the CSV's pinned version string (echoed into the
            error details for provenance).
        mapping_csv_path: optional path string for diagnostics.

    Raises:
        SFINCSSetupError("LULC_MAPPING_MISMATCH"): one or more classes in
            ``fetched_classes`` is not covered by ``mapping``. The error
            carries the unmapped class list + vintage year + mapping version
            so the failure surface is fully actionable.
    """
    if not fetched_classes:
        # Defensive — an empty set shouldn't happen but isn't a mismatch per se.
        logger.warning(
            "NLCD validation gate received empty fetched_classes for vintage=%d",
            nlcd_vintage_year,
        )
        return
    # NLCD class 0 = nodata; ignore for the gate (HydroMT's mask component
    # handles nodata cells separately).
    candidate = {cls for cls in fetched_classes if cls != 0}
    unmapped = sorted(candidate - set(mapping.keys()))
    if unmapped:
        details: dict[str, Any] = {
            "nlcd_vintage_year": nlcd_vintage_year,
            "mapping_version": mapping_version,
            "unmapped_classes": unmapped,
            "fetched_classes": sorted(candidate),
            "mapped_classes": sorted(mapping.keys()),
        }
        if mapping_csv_path is not None:
            details["mapping_csv_path"] = mapping_csv_path
        raise SFINCSSetupError(
            "LULC_MAPPING_MISMATCH",
            message=(
                f"NLCD vintage {nlcd_vintage_year} contains classes "
                f"{unmapped} not covered by Manning's mapping "
                f"v{mapping_version}; HydroMT roughness would fill silently "
                "with defaults (Invariant 7 violation). Update "
                "manning_mapping.csv before SFINCS setup proceeds."
            ),
            details=details,
        )


# --------------------------------------------------------------------------- #
# Object-store staging helper (S3-only; GCP decommissioned)
# --------------------------------------------------------------------------- #


def _to_vsigs(uri: str) -> str:
    """Convert an ``s3://bucket/key`` URI to a GDAL ``/vsis3/`` path.

    Local paths (``file://`` or absolute) pass through unchanged; already-
    converted ``/vsis3/`` paths are idempotent. Anything else is treated as a
    local path (the caller's resolver layer is the gate).

    WARNING (job-0293c live observation): GDAL's ``/vsis3/`` credential chain
    does NOT resolve the EC2 instance role in this environment — it falls back
    to anonymous and reports "does not exist" / AccessDenied on an existing
    private object. boto3 DOES resolve the instance role. Therefore any caller
    that intends to ``rasterio.open`` an ``s3://`` object MUST NOT route it
    through this function; it must stage the bytes via
    ``cache.read_object_bytes_s3`` and open them from a
    ``rasterio.io.MemoryFile`` (see
    ``model_flood_scenario.compute_precip_area_mean_mm_per_hr`` and
    ``_extract_unique_nlcd_classes`` below, plus the clip / landcover tools).
    The ``s3://`` → ``/vsis3/`` mapping is retained only for non-rasterio string
    consumers; it is NOT a working read path on this instance.

    Args:
        uri: ``s3://...`` S3 URI, ``/vsis3/...`` GDAL virtual path, or local
            filesystem path (with or without ``file://`` prefix).

    Returns:
        The GDAL-readable string GDAL drivers (rasterio, HydroMT's
        rioxarray, the gdal CLI) can open without invoking ``s3fs``.
    """
    if uri.startswith("/vsigs/") or uri.startswith("/vsis3/"):
        return uri
    if uri.startswith("s3://"):
        return "/vsis3/" + uri[len("s3://"):]
    if uri.startswith("file://"):
        return uri[len("file://"):]
    return uri


def _rasterio_open_with_retry(read_path: str, *, max_attempts: int = 3):
    """Open a raster via rasterio with retry-and-backoff for transient GS hiccups.

    ``/vsigs/`` reads can fail with transient HTTP errors when the GCS
    endpoint rate-limits or returns a 5xx. Retry up to ``max_attempts``
    times with exponential backoff (1s, 2s, 4s); on final failure re-raise
    the underlying exception unwrapped so the caller's typed-error
    translation layer sees the real cause.

    The retry loop only catches ``rasterio.errors.RasterioIOError`` /
    generic ``RuntimeError`` / ``OSError`` — programming errors
    (TypeError, ValueError on the path string) escape immediately.

    NFR-R-1: external-API resilience — segfault root cause (gcsfs) is
    avoided structurally by the ``/vsigs/`` swap; this wrapper handles
    the remaining transient layer.
    """
    import time

    import rasterio  # local — caller already vouched for the import

    try:
        from rasterio.errors import RasterioIOError  # type: ignore[import-not-found]
        retryable_excs: tuple[type[BaseException], ...] = (
            RasterioIOError,
            RuntimeError,
            OSError,
        )
    except Exception:  # noqa: BLE001
        retryable_excs = (RuntimeError, OSError)

    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return rasterio.open(read_path)
        except retryable_excs as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt == max_attempts:
                break
            backoff_s = 2 ** (attempt - 1)
            logger.warning(
                "rasterio.open(%s) transient failure on attempt %d/%d (%s); "
                "retrying in %.1fs",
                read_path,
                attempt,
                max_attempts,
                exc,
                backoff_s,
            )
            time.sleep(backoff_s)
    assert last_exc is not None  # logic guarantee — the loop sets last_exc on each fail
    raise last_exc


def _write_hydromt_reclass_table_csv(
    mapping: dict[int, float],
    out_path: Path,
) -> Path:
    """Write a reclass-table CSV in the **hydromt-sfincs 1.2.x** expected format.

    OQ-52 hotfix (job-0053). ``_parse_datasets_rgh`` reads the reclass table
    via ``data_catalog.get_dataframe(reclass_table, index_col=0)`` then
    indexes ``df_map[["N"]]`` — i.e. the first column must be the LULC class
    integer (used as the index), and there must be a column literally named
    ``N`` carrying the Manning's roughness value. Our authored
    ``manning_mapping.csv`` uses ``nlcd_class,manning_n,description`` columns
    (load-bearing for ``load_manning_mapping`` + the OQ-4 §4 validation gate);
    here we rewrite the in-memory mapping into the v1.2.x-shaped CSV that
    HydroMT will actually consume during ``setup_manning_roughness``.

    Args:
        mapping: ``{nlcd_class_int: manning_n_float}`` as loaded by
            ``load_manning_mapping`` (the substrate-version-pinned set).
        out_path: destination path inside the per-build temp dir.

    Returns:
        ``out_path`` for convenience.
    """
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        # First column is the LULC class integer (index_col=0); the ``N``
        # column is what HydroMT's reclassify call picks up.
        writer.writerow(["nlcd_class", "N"])
        for cls in sorted(mapping.keys()):
            writer.writerow([cls, mapping[cls]])
    return out_path


_MASK_ELEV_BUFFER_M: float = 5.0

#: Seaward water-level-boundary elevation cap (NAVD88 m) for ``setup_mask_bounds``
#: ``btype="waterlevel"``. Active-domain EDGE cells at/below this bed elevation
#: become msk==2 water-level boundary cells where the bzs surge series is applied.
#: A low coastal cap (~+2 m) keeps the boundary on the Gulf-facing intertidal/
#: low-berm edge (where surge physically enters) and off the higher inland domain
#: edges, so the surge marches sea->land. Without these boundary cells the bzs
#: forcing is inert and the interior never floods (the surge-inundation root
#: cause fixed alongside the 10 h deck-window + rising-limb forcing).
SEAWARD_BOUNDARY_ZMAX_M: float = 2.0

#: Fallback active-cell mask bounds used when the DEM elevation range cannot be
#: read (missing file, unreadable bytes, all-nodata). These are deliberately
#: VERY wide so they NEVER exclude real land — a flood deck with an empty active
#: mask is the silent-wrong-answer this job exists to kill (Invariant 7). They
#: are NOT the old broken (-10, 10) window: -1000 m brackets the deepest
#: bathymetry / below-sea-level basins (Death Valley ~-86 m, Dead Sea ~-430 m),
#: and +9000 m brackets the highest terrain on Earth (Everest ~8849 m), so the
#: whole AOI stays active regardless of elevation.
_MASK_FALLBACK_ZMIN: float = -1000.0
_MASK_FALLBACK_ZMAX: float = 9000.0


def _compute_active_mask_bounds(dem_read_path: str) -> tuple[float, float, bool]:
    """Compute domain-adaptive ``setup_mask_active`` ``zmin``/``zmax`` (metres).

    job-0318 (CONFIRMED BUG): the active-cell mask was previously hardcoded to
    ``zmin: -10.0`` / ``zmax: 10.0`` — an *elevation window* in metres. Only
    DEM cells whose elevation fell inside ``[-10, 10]`` became ACTIVE. For any
    inland / elevated terrain (Asheville sits at ~650 m) every cell exceeds
    ``zmax=10``, so ``hydromt_sfincs.setup_mask_active`` logs "No active cells
    found", the SFINCS domain is empty, the solver returns zero inundation, and
    Pelicun then fails ``PELICUN_NO_ASSETS_IN_HAZARD``. The flood model has
    only ever worked for near-sea-level / coastal AOIs.

    The fix reads the staged DEM's ACTUAL elevation min/max (rasterio, masking
    nodata) and returns a window that BRACKETS the full terrain with a small
    buffer::

        zmin = floor(dem_min) - buffer
        zmax = ceil(dem_max)  + buffer

    so every land cell in the AOI is active regardless of absolute elevation.

    Coastal behaviour stays valid: a Fort-Myers-type DEM spanning roughly
    ``-5 .. +20`` m yields ``zmin ≈ -10`` / ``zmax ≈ +25`` — a non-empty active
    mask covering both the wet (below-zero topobathy) and dry cells.

    Failure policy (Invariant 7 — never silently re-introduce the broken
    window): if the DEM range cannot be read for ANY reason (file absent,
    rasterio/numpy missing, unreadable bytes, all-nodata), fall back to
    ``(_MASK_FALLBACK_ZMIN, _MASK_FALLBACK_ZMAX)`` — a window wide enough to
    never exclude land — and flag it so the caller can annotate the deck.

    Args:
        dem_read_path: a LOCAL filesystem path (or GDAL-readable string) to the
            DEM/topobathy raster. ``build_sfincs_model`` stages ``gs://`` /
            ``s3://`` DEMs to a real local file via ``_stage_gcs_local`` BEFORE
            this runs, so the common path is a plain local GeoTIFF.

    Returns:
        ``(zmin, zmax, adaptive)`` — ``adaptive`` is ``True`` when the bounds
        were derived from the DEM's real elevation range, ``False`` when the
        wide fallback was used.
    """
    import math

    try:
        import numpy as np  # type: ignore[import-not-found]
        import rasterio  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "setup_mask_active: numpy/rasterio unavailable for DEM range read "
            "(%s); using wide fallback bounds (%.1f, %.1f) to keep the active "
            "mask non-empty",
            exc,
            _MASK_FALLBACK_ZMIN,
            _MASK_FALLBACK_ZMAX,
        )
        return _MASK_FALLBACK_ZMIN, _MASK_FALLBACK_ZMAX, False

    try:
        with rasterio.open(dem_read_path) as src:
            arr = src.read(1, masked=True)
            nodata = src.nodata
        # ``masked=True`` already masks the dataset nodata; also defensively mask
        # the common GeoTIFF sentinels + non-finite values so a stray -9999 or
        # NaN can't pull ``zmin`` to absurd depths (which would still bracket
        # land, but pollutes provenance / SFINCS thinks the domain is far deeper
        # than it is).
        data = np.ma.masked_invalid(arr)
        sentinels = [-9999.0, -32768.0, 3.4028234663852886e38]
        if nodata is not None:
            sentinels.append(float(nodata))
        for s in sentinels:
            data = np.ma.masked_equal(data, s)
        if data.count() == 0:
            logger.warning(
                "setup_mask_active: DEM %s has no valid (non-nodata) cells; "
                "using wide fallback bounds (%.1f, %.1f)",
                dem_read_path,
                _MASK_FALLBACK_ZMIN,
                _MASK_FALLBACK_ZMAX,
            )
            return _MASK_FALLBACK_ZMIN, _MASK_FALLBACK_ZMAX, False
        dem_min = float(data.min())
        dem_max = float(data.max())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "setup_mask_active: could not read DEM elevation range from %s "
            "(%s); using wide fallback bounds (%.1f, %.1f) to keep the active "
            "mask non-empty (NOT the broken -10/10 window)",
            dem_read_path,
            exc,
            _MASK_FALLBACK_ZMIN,
            _MASK_FALLBACK_ZMAX,
        )
        return _MASK_FALLBACK_ZMIN, _MASK_FALLBACK_ZMAX, False

    if not (math.isfinite(dem_min) and math.isfinite(dem_max)):
        logger.warning(
            "setup_mask_active: DEM %s yielded non-finite range "
            "(min=%r max=%r); using wide fallback bounds (%.1f, %.1f)",
            dem_read_path,
            dem_min,
            dem_max,
            _MASK_FALLBACK_ZMIN,
            _MASK_FALLBACK_ZMAX,
        )
        return _MASK_FALLBACK_ZMIN, _MASK_FALLBACK_ZMAX, False

    zmin = math.floor(dem_min) - _MASK_ELEV_BUFFER_M
    zmax = math.ceil(dem_max) + _MASK_ELEV_BUFFER_M
    logger.info(
        "setup_mask_active: DEM elevation range [%.3f, %.3f] m -> adaptive "
        "active-cell window zmin=%.1f zmax=%.1f (buffer=%.1f m)",
        dem_min,
        dem_max,
        zmin,
        zmax,
        _MASK_ELEV_BUFFER_M,
    )
    return zmin, zmax, True


# --------------------------------------------------------------------------- #
# Adaptive grid-resolution autoscale (sprint-16 — SFINCS per-job autoscale)
#
# The immediate win (applies on the CURRENT local-docker / gcp-workflows path,
# NOT just AWS Batch): coarsen the SFINCS grid resolution for big AOIs so the
# solve fits a configurable wall-clock budget. SFINCS solve cost scales roughly
# super-linearly in the ACTIVE-cell count N (the cells inside the
# ``setup_mask_active`` elevation window — NOT the raw bbox area; see job-0318).
# We:
#
#   1. estimate the ACTIVE cell count at a candidate grid resolution from the
#      staged DEM (count DEM cells whose elevation falls in the active window,
#      scaled by (native_res / candidate_res)^2),
#   2. snap ``grid_resolution_m`` UP a ladder (30 → 50 → 100 → 200 m) until the
#      estimated active N is at or under a CELL CAP,
#   3. derive that cap from a configurable SOLVE_BUDGET_S (default 600 s = 10
#      min, with a configurable overhead reserve) and the chosen instance vCPU
#      via the perf model fitted from LIVE anchors (Chehalis ~45k @ 8 vCPU =
#      36 s; Chattanooga ~510k @ 8 vCPU censored >> 1800 s).
#
# Every coefficient is a module constant overridable by env so we re-tune from
# logged ``solve-telemetry`` records as real (cells, vCPU, time) data lands. We
# NEVER produce a degenerate/empty grid — resolution is clamped to the ladder's
# coarsest rung and the cap floored so a single absurd AOI cannot drive the cap
# to zero cells.
# --------------------------------------------------------------------------- #


def _env_float(name: str, default: float) -> float:
    """Read an env override as a float; fall back (and warn) on a bad value."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "autoscale: env %s=%r not a float; using default %s", name, raw, default
        )
        return default


def _env_resolution_ladder(default: tuple[float, ...]) -> tuple[float, ...]:
    """Parse ``GRACE2_SFINCS_RES_LADDER`` (comma-separated metres) → sorted tuple.

    Falls back to ``default`` on any parse failure / empty value. The ladder is
    always returned sorted ascending + de-duplicated so the snap-up walk is
    monotone; a degenerate ladder (empty after parse) falls back to the default.
    """
    raw = os.environ.get("GRACE2_SFINCS_RES_LADDER")
    if raw is None or not raw.strip():
        return default
    try:
        vals = sorted(
            {float(p.strip()) for p in raw.split(",") if p.strip() and float(p.strip()) > 0}
        )
    except (TypeError, ValueError):
        logger.warning(
            "autoscale: env GRACE2_SFINCS_RES_LADDER=%r unparseable; using default %s",
            raw,
            default,
        )
        return default
    return tuple(vals) if vals else default


#: Wall-clock budget (seconds) we size the active-cell cap against. NATE's
#: target is ~10 min/solve, configurable + MEASURE-then-tune. Env override:
#: ``GRACE2_SFINCS_SOLVE_BUDGET_S``.
SFINCS_SOLVE_BUDGET_S: float = _env_float("GRACE2_SFINCS_SOLVE_BUDGET_S", 600.0)

#: Fraction of the budget reserved for non-solve overhead (container pull,
#: input staging, output upload, postprocess). The cap is sized against the
#: REMAINING ``(1 - overhead) * budget`` so a 10-min wall budget leaves the
#: solver ~6.5 min of CPU at the default 0.35. Env:
#: ``GRACE2_SFINCS_OVERHEAD_FRACTION``.
SFINCS_OVERHEAD_FRACTION: float = _env_float("GRACE2_SFINCS_OVERHEAD_FRACTION", 0.35)

#: Perf model T(N, vcpu) = PERF_A * N^PERF_P / (vcpu/8)^PERF_THREAD_EXP, fitted
#: from LIVE anchors at 8 vCPU. The single Chehalis anchor (45k cells, 36 s)
#: pins PERF_A given PERF_P; PERF_P >= 1.61 is a FLOOR (Chattanooga 510k was
#: censored at the 1800 s timeout, so the true exponent is only bounded below —
#: real p is likely 1.7-2.0). We adopt p=1.61 as the conservative-but-not-
#: reckless default (a HIGHER p would coarsen MORE aggressively; a lower p is
#: the dangerous direction, so the floor is the safe choice to ship). Re-tune
#: from logged solve-telemetry. Env: ``GRACE2_SFINCS_PERF_P`` etc.
#:
#:   PERF_A solved from the Chehalis anchor: 36 = A * 45000^1.61
#:     45000^1.61 = exp(1.61 * ln 45000) = exp(17.25) ≈ 3.10e7
#:       →  A ≈ 36 / 3.10e7 ≈ 1.16e-6
#:   (An earlier comment mis-stated 45000^1.61 ≈ 1.585e7, which yielded an
#:   inflated A=2.27e-6 — that over-predicted solve time ~2x and coarsened the
#:   grid more aggressively than the anchor warrants. 1.16e-6 is anchor-exact.)
SFINCS_PERF_P: float = _env_float("GRACE2_SFINCS_PERF_P", 1.61)
SFINCS_PERF_A: float = _env_float("GRACE2_SFINCS_PERF_A", 1.16e-6)

#: Thread speedup exponent: speedup ≈ (vcpu / 8) ** THREAD_EXP (sub-linear —
#: SFINCS does not scale perfectly with cores). Env: ``GRACE2_SFINCS_THREAD_EXP``.
SFINCS_THREAD_EXP: float = _env_float("GRACE2_SFINCS_THREAD_EXP", 0.85)

#: The reference vCPU count the anchors were measured at (the perf model
#: normalises against this). Env: ``GRACE2_SFINCS_PERF_REF_VCPU``.
SFINCS_PERF_REF_VCPU: float = _env_float("GRACE2_SFINCS_PERF_REF_VCPU", 8.0)

#: Resolution ladder (metres) the autoscaler snaps UP through. 30 m is the
#: NLCD-native NFR-P-4 baseline; 200 m is the coarsest rung we will ever solve
#: at (a 200 m flood grid is coarse but still meaningful for a regional AOI,
#: and is the floor against degenerate over-coarsening). Env:
#: ``GRACE2_SFINCS_RES_LADDER`` (comma-separated).
SFINCS_RES_LADDER: tuple[float, ...] = _env_resolution_ladder((30.0, 50.0, 100.0, 200.0))

#: Hard floor on the active-cell cap. A pathological budget/perf combination
#: could drive the computed cap toward zero; we never let it fall below this so
#: a tiny AOI is never needlessly coarsened past the 30 m rung. (At 8 vCPU /
#: 600 s / 0.35 overhead / p=1.61 the natural cap is ~250k cells, well above
#: this floor — the floor only bites under hostile env overrides.)
SFINCS_MIN_CELL_CAP: int = int(_env_float("GRACE2_SFINCS_MIN_CELL_CAP", 50_000))

#: compute_class → vCPU count used to size the cap on the CURRENT path. The
#: deployed agent EC2 box is 8 vCPU; ``run_solver(compute_class=...)`` is a
#: provenance tag today (the AWS Batch adapter below maps it to real
#: resourceRequirements). Env: ``GRACE2_SFINCS_SOLVE_VCPUS`` overrides the
#: effective vCPU outright (so a bigger always-on box re-sizes the cap without
#: a code change).
SFINCS_COMPUTE_CLASS_VCPUS: dict[str, int] = {
    "small": 4,
    "medium": 8,
    "standard": 8,
    "large": 16,
    "gpu": 32,
}

#: Default vCPU when the compute_class is unknown — the deployed 8-vCPU box.
SFINCS_DEFAULT_VCPUS: int = 8


def resolve_solve_vcpus(compute_class: str = "medium") -> int:
    """Effective vCPU count the cap is sized against.

    ``GRACE2_SFINCS_SOLVE_VCPUS`` (if set) wins outright — it lets a redeploy on
    a bigger always-on box re-size the cap without a code change. Otherwise map
    the FR-CE-3 ``compute_class`` through ``SFINCS_COMPUTE_CLASS_VCPUS`` (default
    8 — the current EC2 box).
    """
    env_v = os.environ.get("GRACE2_SFINCS_SOLVE_VCPUS")
    if env_v and env_v.strip():
        try:
            v = int(float(env_v))
            if v > 0:
                return v
        except (TypeError, ValueError):
            logger.warning(
                "autoscale: env GRACE2_SFINCS_SOLVE_VCPUS=%r invalid; ignoring", env_v
            )
    return SFINCS_COMPUTE_CLASS_VCPUS.get(
        (compute_class or "").strip().lower(), SFINCS_DEFAULT_VCPUS
    )


def estimate_solve_seconds(active_cells: int, vcpus: int) -> float:
    """Estimate SFINCS wall-clock solve seconds for ``active_cells`` at ``vcpus``.

    ``T(N, vcpu) = PERF_A * N^PERF_P / (vcpu / REF_VCPU) ** THREAD_EXP``. Fitted
    from the LIVE 8-vCPU anchors (see ``SFINCS_PERF_*``). Returns 0.0 for a
    non-positive cell count (a degenerate domain is caught elsewhere).
    """
    if active_cells <= 0:
        return 0.0
    speedup = (max(1, vcpus) / SFINCS_PERF_REF_VCPU) ** SFINCS_THREAD_EXP
    speedup = max(speedup, 1e-6)
    return (SFINCS_PERF_A * (float(active_cells) ** SFINCS_PERF_P)) / speedup


def compute_cell_cap(vcpus: int) -> int:
    """The max active-cell count that solves inside the budget at ``vcpus``.

    Invert the perf model at the budget net of overhead::

        N_cap = ( (1 - overhead) * budget * speedup / PERF_A ) ** (1 / PERF_P)

    Floored at ``SFINCS_MIN_CELL_CAP`` so a hostile env override can never drive
    the cap to a degenerate (near-zero) value. Logged so the chosen cap is
    auditable against logged solve-telemetry.
    """
    budget = max(0.0, SFINCS_SOLVE_BUDGET_S)
    overhead = min(max(SFINCS_OVERHEAD_FRACTION, 0.0), 0.95)
    solve_budget = budget * (1.0 - overhead)
    speedup = (max(1, vcpus) / SFINCS_PERF_REF_VCPU) ** SFINCS_THREAD_EXP
    if SFINCS_PERF_A <= 0 or solve_budget <= 0:
        cap = SFINCS_MIN_CELL_CAP
    else:
        cap = int((solve_budget * speedup / SFINCS_PERF_A) ** (1.0 / SFINCS_PERF_P))
    cap = max(cap, SFINCS_MIN_CELL_CAP)
    logger.info(
        "autoscale: cell cap=%d (budget=%.0fs overhead=%.2f vcpus=%d p=%.3f a=%.3e)",
        cap,
        budget,
        overhead,
        vcpus,
        SFINCS_PERF_P,
        SFINCS_PERF_A,
    )
    return cap


@dataclass(frozen=True)
class GridAutoscaleResult:
    """Outcome of ``autoscale_grid_resolution`` — the chosen resolution + why.

    Carried onto ``ModelSetup.parameters`` for provenance and into the
    solve-telemetry record so the cap can be re-tuned from logged data.
    """

    grid_resolution_m: float
    estimated_active_cells: int
    cell_cap: int
    vcpus: int
    base_resolution_m: float
    estimated_active_cells_at_base: int
    estimated_solve_seconds: float
    coarsened: bool
    reason: str


def _estimate_active_cells_at_native(
    dem_read_path: str,
    zmin: float,
    zmax: float,
) -> tuple[int, float] | None:
    """Count DEM cells inside the active elevation window + return native res (m).

    Reads the staged (LOCAL) DEM, masks nodata, counts cells whose elevation is
    within ``[zmin, zmax]`` (the ``setup_mask_active`` window — mirrors
    job-0318's active-domain definition), and returns ``(active_count,
    native_resolution_m)``. The native resolution is derived from the DEM
    transform; if the DEM is geographic (degrees) we convert the pixel size to
    metres at the DEM centre latitude (matching ``_bbox_area_km2``).

    Returns ``None`` when the DEM cannot be read (caller falls back to a
    bbox-area estimate so autoscale degrades gracefully — never crashes).
    """
    try:
        import numpy as np  # type: ignore[import-not-found]
        import rasterio  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        logger.warning("autoscale: numpy/rasterio unavailable for cell estimate (%s)", exc)
        return None

    try:
        with rasterio.open(dem_read_path) as src:
            arr = src.read(1, masked=True)
            nodata = src.nodata
            transform = src.transform
            crs = src.crs
            # Pixel size in the DEM's own units.
            px_w = abs(transform.a)
            px_h = abs(transform.e)
            # Approximate the DEM centre latitude for a degrees→metres convert.
            centre_lat = src.bounds.bottom + 0.5 * (src.bounds.top - src.bounds.bottom)
    except Exception as exc:  # noqa: BLE001
        logger.warning("autoscale: could not read DEM %s for cell estimate (%s)", dem_read_path, exc)
        return None

    try:
        import math

        data = np.ma.masked_invalid(arr)
        sentinels = [-9999.0, -32768.0, 3.4028234663852886e38]
        if nodata is not None:
            sentinels.append(float(nodata))
        for s in sentinels:
            data = np.ma.masked_equal(data, s)
        if data.count() == 0:
            logger.warning("autoscale: DEM %s has no valid cells for estimate", dem_read_path)
            return None
        in_window = (data >= zmin) & (data <= zmax)
        # ``in_window`` is masked where data is masked; count only unmasked True.
        active_native = int(np.ma.filled(in_window, False).sum())

        # Native resolution in METRES. A geographic CRS (EPSG:4326) reports the
        # pixel size in degrees; convert at the DEM centre latitude (km/deg ≈
        # 111.32 * cos(lat) for lon, 111.32 for lat — use the geometric mean so
        # a single scalar metre/pixel falls out, matching the square-cell model).
        is_geographic = bool(getattr(crs, "is_geographic", False)) if crs is not None else (px_w < 1.0)
        if is_geographic:
            m_per_deg_lat = 111_320.0
            m_per_deg_lon = 111_320.0 * max(0.01, math.cos(math.radians(centre_lat)))
            native_res_m = math.sqrt((px_w * m_per_deg_lon) * (px_h * m_per_deg_lat))
        else:
            native_res_m = math.sqrt(px_w * px_h)
        if not math.isfinite(native_res_m) or native_res_m <= 0:
            logger.warning("autoscale: DEM %s native res non-finite (%r)", dem_read_path, native_res_m)
            return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("autoscale: cell estimate failed for %s (%s)", dem_read_path, exc)
        return None

    return active_native, native_res_m


def estimate_active_cells_at_resolution(
    active_native: int,
    native_res_m: float,
    target_res_m: float,
) -> int:
    """Scale a native-resolution active-cell count to ``target_res_m``.

    The active AREA is invariant under regridding; the cell COUNT scales by
    ``(native_res / target_res) ** 2``. Coarsening (target > native) shrinks the
    count; refining grows it. Floored at 1 for a non-empty active domain so the
    estimate never returns 0 for a real domain (which would falsely advertise a
    free solve).
    """
    if active_native <= 0 or native_res_m <= 0 or target_res_m <= 0:
        return 0
    scaled = active_native * (native_res_m / target_res_m) ** 2
    return max(1, int(round(scaled)))


def autoscale_grid_resolution(
    dem_read_path: str,
    bbox: tuple[float, float, float, float],
    *,
    zmin: float,
    zmax: float,
    compute_class: str = "medium",
    base_resolution_m: float = 30.0,
) -> GridAutoscaleResult:
    """Choose ``grid_resolution_m`` so the estimated solve fits the budget.

    Walks ``SFINCS_RES_LADDER`` from the finest rung >= ``base_resolution_m``
    upward, snapping UP until the estimated ACTIVE cell count is at or under
    ``compute_cell_cap(vcpus)``. NEVER produces a degenerate grid: the walk
    stops at the coarsest ladder rung even if the cap is still exceeded (a
    huge AOI solves at 200 m, coarse but non-empty, rather than failing), and
    the cell estimate is floored at 1 for a real domain.

    DEM-read failure degrades gracefully to a bbox-area estimate (active ==
    whole bbox) so autoscale never crashes the build; the reason string records
    which path was taken.

    Args:
        dem_read_path: LOCAL staged DEM path (``_stage_gcs_local`` ran already).
        bbox: WGS84 bbox — the fallback area source when the DEM is unreadable.
        zmin/zmax: the ``setup_mask_active`` elevation window (job-0318) — the
            same window the solve will actually mask to, so the estimate counts
            the cells SFINCS will really solve, not the raw bbox.
        compute_class: FR-CE-3 class → vCPU via ``resolve_solve_vcpus``.
        base_resolution_m: the finest resolution to consider (NFR-P-4 30 m).

    Returns:
        ``GridAutoscaleResult`` with the chosen resolution + the estimate +
        the cap + why. Always log-emitted at INFO (chosen res, est cells, reason).
    """
    vcpus = resolve_solve_vcpus(compute_class)
    cap = compute_cell_cap(vcpus)

    # Ladder rungs at or coarser than the base; always include the base itself
    # so a base finer than every ladder rung still has a starting point.
    ladder = sorted({base_resolution_m, *SFINCS_RES_LADDER})
    ladder = [r for r in ladder if r >= base_resolution_m] or [base_resolution_m]

    native = _estimate_active_cells_at_native(dem_read_path, zmin, zmax)
    if native is not None:
        active_native, native_res_m = native
        estimate_source = "dem-active-window"
    else:
        # Fallback: assume the whole bbox is active at the base resolution.
        # ``_bbox_area_km2`` matches the workflow helper; cells = area / res^2.
        import math

        min_lon, min_lat, max_lon, max_lat = bbox
        mid_lat = 0.5 * (min_lat + max_lat)
        dlat_km = (max_lat - min_lat) * 111.320
        dlon_km = (max_lon - min_lon) * 111.320 * math.cos(math.radians(mid_lat))
        area_m2 = abs(dlat_km * dlon_km) * 1_000_000.0
        native_res_m = base_resolution_m
        active_native = max(1, int(area_m2 / (base_resolution_m * base_resolution_m)))
        estimate_source = "bbox-area-fallback"

    est_at_base = estimate_active_cells_at_resolution(
        active_native, native_res_m, base_resolution_m
    )

    chosen_res = ladder[0]
    chosen_est = est_at_base
    coarsened = False
    for res in ladder:
        est = estimate_active_cells_at_resolution(active_native, native_res_m, res)
        chosen_res = res
        chosen_est = est
        coarsened = res > base_resolution_m
        if est <= cap:
            break
    # ``chosen_res`` is now either the first rung under the cap, or (if none
    # fit) the coarsest rung — never degenerate.

    capped_out = chosen_est > cap  # coarsest rung still over cap (huge AOI)
    est_solve_s = estimate_solve_seconds(chosen_est, vcpus)

    if capped_out:
        reason = (
            f"AOI exceeds cap even at coarsest rung {chosen_res:.0f}m "
            f"(est {chosen_est} > cap {cap}); clamped to coarsest rung "
            f"(source={estimate_source})"
        )
    elif coarsened:
        reason = (
            f"coarsened {base_resolution_m:.0f}m→{chosen_res:.0f}m to fit cap "
            f"{cap} (est {est_at_base}@base → {chosen_est}@chosen; "
            f"source={estimate_source})"
        )
    else:
        reason = (
            f"base {base_resolution_m:.0f}m fits cap {cap} "
            f"(est {chosen_est}; source={estimate_source})"
        )

    logger.info(
        "autoscale: grid_resolution_m=%.0f estimated_active_cells=%d cap=%d "
        "vcpus=%d est_solve=%.0fs reason=%s",
        chosen_res,
        chosen_est,
        cap,
        vcpus,
        est_solve_s,
        reason,
    )

    return GridAutoscaleResult(
        grid_resolution_m=chosen_res,
        estimated_active_cells=chosen_est,
        cell_cap=cap,
        vcpus=vcpus,
        base_resolution_m=base_resolution_m,
        estimated_active_cells_at_base=est_at_base,
        estimated_solve_seconds=est_solve_s,
        coarsened=coarsened,
        reason=reason,
    )


def suggest_sfincs_resolution_from_bbox(
    bbox: tuple[float, float, float, float],
    *,
    base_resolution_m: float = 30.0,
    compute_class: str = "medium",
) -> GridAutoscaleResult:
    """Lightweight SFINCS resolution suggestion from the AOI bbox alone (no DEM).

    The combined run-settings gate (sprint-16) surfaces a SUGGESTED SFINCS grid
    resolution + active-cell count + estimated solve time BEFORE the run so the
    user can override the spatial resolution. The full :func:`autoscale_grid_resolution`
    reads a staged DEM to count cells inside the active elevation window — too
    heavy for a PRE-dispatch gate (it would block the WS loop and require a DEM
    fetch the gate has not done yet). This helper instead uses the SAME
    bbox-area fallback the autoscaler falls back to when the DEM is unreadable:
    treat the whole bbox as active at ``base_resolution_m`` and scale the count
    across the ladder. It is an ESTIMATE (labelled as such on the card); the real
    cell count comes from ``build_sfincs_model``'s DEM-active autoscale at run
    time. It NEVER reads a file and is safe to call on the event loop.

    Mirrors the ladder walk + cap logic of :func:`autoscale_grid_resolution` so
    the suggested rung the user SEES matches what the real autoscale would pick
    for an all-active AOI. Returns the same :class:`GridAutoscaleResult` shape so
    the gate builds the ``GranularitySuggestion`` block uniformly with the SWMM
    path.
    """
    import math

    base_resolution_m = float(base_resolution_m) if base_resolution_m > 0 else 30.0
    vcpus = resolve_solve_vcpus(compute_class)
    cap = compute_cell_cap(vcpus)

    # bbox-area active-cell model at the base resolution (matches the autoscaler's
    # ``bbox-area-fallback`` branch). ``_bbox_area_km2`` is the same WGS84 approx.
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    dlat_km = (max_lat - min_lat) * 111.320
    dlon_km = (max_lon - min_lon) * 111.320 * math.cos(math.radians(mid_lat))
    area_m2 = abs(dlat_km * dlon_km) * 1_000_000.0
    native_res_m = base_resolution_m
    active_native = max(1, int(area_m2 / (base_resolution_m * base_resolution_m)))

    est_at_base = estimate_active_cells_at_resolution(
        active_native, native_res_m, base_resolution_m
    )

    ladder = sorted({base_resolution_m, *SFINCS_RES_LADDER})
    chosen_res = ladder[0]
    chosen_est = est_at_base
    coarsened = False
    for res in ladder:
        est = estimate_active_cells_at_resolution(active_native, native_res_m, res)
        chosen_res = res
        chosen_est = est
        coarsened = res > base_resolution_m
        if est <= cap:
            break

    est_solve_s = estimate_solve_seconds(chosen_est, vcpus)

    if chosen_est > cap:
        reason = (
            f"AOI exceeds cap even at coarsest rung {chosen_res:.0f}m "
            f"(est {chosen_est} > cap {cap}); clamped to coarsest rung "
            f"(bbox-area estimate)"
        )
    elif coarsened:
        reason = (
            f"coarsened {base_resolution_m:.0f}m->{chosen_res:.0f}m to fit cap "
            f"{cap} (est {est_at_base}@base -> {chosen_est}@chosen; bbox-area estimate)"
        )
    else:
        reason = (
            f"base {base_resolution_m:.0f}m fits cap {cap} "
            f"(est {chosen_est}; bbox-area estimate)"
        )

    return GridAutoscaleResult(
        grid_resolution_m=chosen_res,
        estimated_active_cells=chosen_est,
        cell_cap=cap,
        vcpus=vcpus,
        base_resolution_m=base_resolution_m,
        estimated_active_cells_at_base=est_at_base,
        estimated_solve_seconds=est_solve_s,
        coarsened=coarsened,
        reason=reason,
    )




def _emit_surge_forcing_blocks(
    components: list[str],
    forcing: ForcingSpec,
) -> None:
    """Append the COASTAL SFINCS surge / discharge / wind / pressure YAML blocks.

    Mutates ``components`` in place, emitting (in this fixed order) the HydroMT
    setup steps for whichever ``ForcingSpec`` surge members are present:

    1. ``setup_waterlevel_forcing`` (``bzs``) — surge + tide boundary. Emitted
       from a ``geodataset`` (single point-timeseries netCDF) OR a tabular
       ``timeseries`` CSV + ``locations`` point file.
    2. ``setup_river_inflow`` THEN ``setup_discharge_forcing`` (``dis``) — river
       boundary. The inflow step (rivers / hydrography) ALWAYS precedes the
       discharge-series step (HydroMT contract: inflow makes the ``src`` points,
       discharge attaches the series). Order is load-bearing.
    3. ``setup_wind_forcing`` (uniform mag/dir) OR ``setup_wind_forcing_from_grid``
       (gridded netCDF).
    4. ``setup_pressure_forcing_from_grid`` (gridded MSL pressure netCDF).

    Every input URI is staged to a local path via ``_stage_gcs_local`` first
    (HydroMT's data adapter stats catalog paths with fsspec's LOCAL filesystem
    before GDAL opens them — a ``gs://``/``s3://`` URI would fail that stat;
    job-0248). All steps funnel surge/discharge series through
    ``set_forcing_1d``, which the module-level pandas guard keeps callable on
    pandas >= 3.0.
    """
    # --- 0. Water-level BOUNDARY CELLS (msk=2) along the seaward active edge ---
    # CRITICAL (surge inundation root cause): ``setup_mask_active`` only marks
    # ACTIVE cells (msk=1); it does NOT create water-level boundary cells. SFINCS
    # applies a bzs water-level boundary ONLY to msk==2 cells, so a deck with a
    # bnd/bzs surge series but NO msk==2 cells leaves the surge INERT -- the
    # interior zs stays pinned at zsini=0.0 for the whole run (proven live: run
    # 01KVVWGKK05FRQP0BNHANFBVDE had msk all ==1, zero msk==2/3 cells, and zs
    # output was 0.0 at every timestep; the only "wet" cells were below-datum
    # bathymetry, so the front never advanced and runup never climbed land).
    # ``setup_mask_bounds(btype="waterlevel", zmax=...)`` converts the active-
    # domain EDGE cells whose bed elevation is at/below ``zmax`` into msk==2
    # water-level boundary cells. Capping at a low coastal elevation keeps the
    # boundary on the SEAWARD (low) edge and off the high inland edges, so the
    # surge enters from the Gulf side and marches inland. Emitted ONLY when a
    # water-level forcing member is present (a pure-pluvial deck is unaffected).
    wl = forcing.waterlevel
    if wl is not None:
        components.append("setup_mask_bounds:")
        components.append('  btype: "waterlevel"')
        # Seaward-edge cap: any active-edge cell at/below this elevation (NAVD88 m)
        # becomes a water-level boundary cell. +2.0 m brackets the intertidal /
        # low-berm coastal edge (where the surge physically enters) while excluding
        # the higher inland domain edges.
        components.append(f"  zmax: {SEAWARD_BOUNDARY_ZMAX_M}")
        components.append("  reset_bounds: true")

    # --- 1. Water-level (surge + tide) boundary ---
    if wl is not None:
        components.append("setup_waterlevel_forcing:")
        if wl.geodataset_uri:
            components.append(
                f"  geodataset: '{_stage_gcs_local(wl.geodataset_uri)}'"
            )
        else:
            if wl.timeseries_uri:
                components.append(
                    f"  timeseries: '{_stage_gcs_local(wl.timeseries_uri)}'"
                )
            if wl.locations_uri:
                components.append(
                    f"  locations: '{_stage_gcs_local(wl.locations_uri)}'"
                )
        if wl.offset is not None:
            components.append(f"  offset: {wl.offset}")
        if wl.buffer_m is not None:
            components.append(f"  buffer: {wl.buffer_m}")

    # --- 2. River inflow (BEFORE discharge — order matters) ---
    dq = forcing.discharge
    if dq is not None:
        if dq.rivers_uri or dq.hydrography_uri:
            components.append("setup_river_inflow:")
            if dq.rivers_uri:
                components.append(
                    f"  rivers: '{_stage_gcs_local(dq.rivers_uri)}'"
                )
            if dq.hydrography_uri:
                components.append(
                    f"  hydrography: '{_stage_gcs_local(dq.hydrography_uri)}'"
                )
            if dq.river_upa_km2 is not None:
                components.append(f"  river_upa: {dq.river_upa_km2}")
        # Discharge series attaches to the src points established above.
        components.append("setup_discharge_forcing:")
        if dq.timeseries_uri:
            components.append(
                f"  timeseries: '{_stage_gcs_local(dq.timeseries_uri)}'"
            )
        if dq.locations_uri:
            components.append(
                f"  locations: '{_stage_gcs_local(dq.locations_uri)}'"
            )

    # --- 2b. Levee-breach INTERIOR point source (NATE 2026-06-26) ---
    # An interior breach hydrograph injected via setup_discharge_forcing with an
    # explicit ``locations`` Point at the breach cell (ARBITRARY interior cell --
    # NOT a domain-edge inflow, so NO setup_river_inflow). ``merge: true`` lets
    # hydromt COMPOSE this dis forcing with any river discharge emitted above, so
    # a compound run can carry BOTH an edge river inflow AND an interior breach.
    br = forcing.breach
    if br is not None and br.timeseries_uri and br.locations_uri:
        components.append("setup_discharge_forcing:")
        components.append(
            f"  timeseries: '{_stage_gcs_local(br.timeseries_uri)}'"
        )
        components.append(
            f"  locations: '{_stage_gcs_local(br.locations_uri)}'"
        )
        components.append("  merge: true  # breach point-source merges with river dis")

    # --- 3. Wind (uniform OR gridded) ---
    wind = forcing.wind
    if wind is not None:
        if wind.grid_uri:
            components.append("setup_wind_forcing_from_grid:")
            components.append(f"  wind: '{_stage_gcs_local(wind.grid_uri)}'")
        elif wind.magnitude is not None and wind.direction is not None:
            components.append("setup_wind_forcing:")
            components.append(f"  magnitude: {wind.magnitude}  # m/s")
            components.append(
                f"  direction: {wind.direction}  # deg (from; 0=N, 90=E)"
            )

    # --- 4. Pressure (gridded MSL) ---
    press = forcing.pressure
    if press is not None and press.grid_uri:
        components.append("setup_pressure_forcing_from_grid:")
        components.append(f"  press: '{_stage_gcs_local(press.grid_uri)}'")
        if press.fill_value is not None:
            components.append(f"  fill_value: {press.fill_value}")

    # --- 5. Infiltration LOSS term (NATE 2026-06-26) ---
    # CN (scsfile) WINS over a constant: hydromt's setup_cn_infiltration pops the
    # default ``qinf`` config, so emitting both is ambiguous. A bare scalar
    # constant has NO setup_* method (setup_constant_infiltration REQUIRES a
    # raster/lulc), so it routes through the setup_config ``qinf`` float instead
    # -- emitted in _emit_physics_config alongside the physics keys.
    inf = forcing.infiltration
    if inf is not None:
        if inf.cn_uri:
            components.append("setup_cn_infiltration:")
            components.append(f"  cn: '{_stage_gcs_local(inf.cn_uri)}'")
            # SINGLE-BAND GCN250 -> antecedent_moisture MUST be null, else the
            # cn_avg-VAR lookup ValueErrors on a bare DataArray band.
            am = inf.antecedent_moisture
            components.append(
                f"  antecedent_moisture: {('null' if am is None else repr(am))}"
            )
        elif inf.lulc_uri and inf.reclass_table_uri:
            components.append("setup_constant_infiltration:")
            components.append(f"  lulc: '{_stage_gcs_local(inf.lulc_uri)}'")
            components.append(
                f"  reclass_table: '{_stage_gcs_local(inf.reclass_table_uri)}'"
            )
        # A bare ``constant_mm_per_hr`` (no raster/lulc) is emitted as the scalar
        # sfincs.inp:qinf inside the setup_config block (_emit_physics_config).


def _emit_physics_config(
    components: list[str],
    physics: dict[str, Any] | None,
    *,
    infiltration: "InfiltrationForcing | None" = None,
) -> None:
    """Append advanced-physics + bare-constant-infiltration lines into setup_config.

    NATE 2026-06-26: ``setup_config`` is a HydroMT passthrough (any key -> a
    ``key = value`` line in sfincs.inp), so each resolved physics override lands
    directly in the deck. ``physics`` is the dict resolved by
    ``physics_registry.validate_and_resolve_physics('sfincs', overrides)`` (keys
    a subset of advection/theta/alpha/huthresh/coriolis_latitude/wind_drag). The
    caller appends these AFTER the existing crs/tref/tstart/tstop/dtout lines but
    still inside the same ``setup_config:`` block, so they merge into one dict.

    Key mapping (deck_target):
      - advection/theta/alpha/huthresh -> the same sfincs.inp key verbatim.
      - coriolis_latitude (float deg)  -> ``latitude`` (the constant-f plane;
        physics_registry FIX -- there is no sfincs.inp:coriolis key).
      - wind_drag (cd > 0)             -> a flat ``cdval: [cd,cd,cd]`` curve with
        ``cdnrb: 3`` (physics_registry FIX -- cdwnd is the speed-breakpoint axis,
        cdval is the coefficients). cd == 0 keeps the SFINCS default formula.

    ``infiltration.constant_mm_per_hr`` (a bare scalar with no raster/lulc) is
    ALSO emitted here as ``qinf: <v>`` -- the only setup_config-routed loss term
    (CN/lulc paths go through setup_*_infiltration steps in
    _emit_surge_forcing_blocks and take precedence; this fires only when neither
    a cn_uri nor a lulc_uri+reclass_table_uri is set).
    """
    if physics:
        for key in ("advection",):
            if key in physics:
                components.append(f"  {key}: {int(physics[key])}")
        for key in ("theta", "alpha", "huthresh"):
            if key in physics:
                components.append(f"  {key}: {float(physics[key])}")
        # Coriolis: a latitude float -> sfincs.inp:latitude (the constant-f plane).
        if "coriolis_latitude" in physics:
            components.append(f"  latitude: {float(physics['coriolis_latitude'])}")
        # Wind drag: a constant cd > 0 -> a flat cdval [cd,cd,cd] curve (cdnrb=3).
        cd = physics.get("wind_drag")
        if cd is not None and float(cd) > 0.0:
            cd_f = float(cd)
            components.append("  cdnrb: 3")
            components.append("  cdwnd: [0.0, 28.0, 50.0]")
            components.append(f"  cdval: [{cd_f}, {cd_f}, {cd_f}]")

    # Bare-constant infiltration (no raster) -> the scalar sfincs.inp:qinf (mm/hr).
    if (
        infiltration is not None
        and not infiltration.cn_uri
        and not (infiltration.lulc_uri and infiltration.reclass_table_uri)
        and infiltration.constant_mm_per_hr is not None
    ):
        components.append(f"  qinf: {float(infiltration.constant_mm_per_hr)}")


def _generate_hydromt_yaml_config(
    *,
    bbox: tuple[float, float, float, float],
    options: BuildOptions,
    dem_local_path: str,
    landcover_local_path: str,
    river_local_path: str | None,
    forcing: ForcingSpec,
    mapping_csv_path: str,
) -> str:
    """Compose a HydroMT-SFINCS YAML build config string.

    Per OQ-4 §3 + §4: the YAML drives ``hydromt build sfincs`` (or the
    equivalent Python API call). Generated programmatically from the typed
    inputs — never user-input.

    The component list is the v0.1 pluvial-flood capstone shape, with every
    step matched to a hydromt-sfincs 1.2.2 live ``inspect.signature`` cite
    (job-0054 comprehensive migration audit):

      * setup_config — config-file passthrough (``SfincsModel.setup_config``
        takes ``**cfdict`` per inheritance from ``hydromt.Model``). Time
        values (``tref``, ``tstart``, ``tstop``) MUST be in SFINCS format
        ``YYYYMMDD HHMMSS`` (e.g. ``"20260101 000000"``), NOT ISO 8601 —
        ``sfincs_input.py`` parses them via
        ``datetime.strptime(val, "%Y%m%d %H%M%S")``, and
        ``utils.parse_datetime`` uses the same format. ISO 8601 strings
        raise ``ValueError: time data '...' does not match format '%Y%m%d
        %H%M%S'`` inside ``setup_precip_forcing → get_model_time()``.
        (Discovered and fixed in job-0055.)
      * setup_grid_from_region — defines the SFINCS grid. Live sig:
        ``(region: dict, res: float = 100, crs: Union[str, int] = "utm", ...)``.
        We pass ``region: {bbox: [...]}`` + ``res``; ``crs`` left at the
        ``"utm"`` default so HydroMT picks the appropriate UTM zone for the
        bbox (Decision K: minimal parameter surface, derive inside).
      * setup_dep — DEM/topobathy ingest. Live sig:
        ``(datasets_dep: List[dict], buffer_cells: int = 0, interp_method:
        str = "linear")``. We pass ``datasets_dep: [{elevtn: <path>}]``.
      * setup_mask_active — active-cell mask. Live sig accepts ``zmin`` +
        ``zmax`` as keyword args; we pass both.
      * setup_manning_roughness — Manning's grid via NLCD + the reclass CSV.
        Live sig: ``(datasets_rgh: List[dict] = [], manning_land=0.04,
        manning_sea=0.02, rgh_lev_land=0)`` — NO top-level ``map_fn``
        (OQ-52). The reclass table lives INSIDE each ``datasets_rgh`` entry
        under key ``reclass_table`` (per ``_parse_datasets_rgh``: each dict
        supports ``manning`` (gridded n) OR ``lulc`` + ``reclass_table``);
        the CSV must be ``index_col=0`` + column literally ``N`` —
        ``_write_hydromt_reclass_table_csv`` materializes that view.
      * setup_subgrid — OPTIONAL (``options.enable_subgrid``). Subgrid tables
        let SFINCS run on a coarse computational grid while resolving sub-cell
        topography + roughness from the same dep + roughness datasets — the
        standard cheap urban-flood-around-buildings estimate. Live sig:
        ``(datasets_dep, datasets_rgh=[], datasets_riv=[], nr_subgrid_pixels=20,
        ...)``. The building-obstacle ``"raise"`` mode burns OSM footprints as a
        ``datasets_riv`` raised-bank entry here.
      * setup_waterlevel_forcing / setup_river_inflow + setup_discharge_forcing
        / setup_wind_forcing[_from_grid] / setup_pressure_forcing_from_grid —
        the COASTAL SFINCS surge / tide / discharge / wind / pressure forcing
        blocks, emitted by ``_emit_surge_forcing_blocks`` ONLY when the matching
        ``ForcingSpec`` member is present. ``setup_river_inflow`` is emitted
        BEFORE ``setup_discharge_forcing`` (order matters — inflow makes the src
        points, discharge attaches the series). These funnel through HydroMT's
        ``set_forcing_1d``, which the module-level pandas guard keeps callable on
        pandas >= 3.0 (the old job-0055 blocker that forced river inflow OFF).
        A pure-pluvial deck (no surge members) emits NONE of these, so it stays
        byte-identical to the v0.1 deck.
      * setup_precip_forcing — uniform precip forcing. Live sig:
        ``(timeseries=None, magnitude=None)`` — accepts EITHER a tabulated
        timeseries CSV OR a single ``magnitude`` float in ``mm/hr``
        (constant rate over the simulation window, then projected onto a
        10-minute time grid). OQ-54 fix: we previously emitted ``precip``
        + ``duration_hr`` (neither is a 1.2.x parameter); we now emit
        ``magnitude: <mm_per_hr>`` derived from Atlas 14 depth ÷ duration.
        The Atlas 14 depth + duration are still echoed via the inline YAML
        comment so the provenance trail survives.

    Returns the YAML as a string. Test code parses it back; the production
    runtime writes it to a temp file and points HydroMT at it.
    """
    crs = options.crs
    grid_res = options.grid_resolution_m
    components: list[str] = []
    components.append("setup_config:")
    components.append(f"  crs: {crs}")
    # Time values MUST be in SFINCS format "YYYYMMDD HHMMSS" — sfincs_input.py
    # parses them with strptime(val, "%Y%m%d %H%M%S"). ISO 8601 format raises
    # ValueError inside setup_precip_forcing -> get_model_time() (job-0055).
    #
    # ``tstop`` is ``tstart + simulation_hours`` at SUB-DAY precision (datetime
    # arithmetic), NOT a whole-day rounding. The old ``sim_days =
    # max(1, int(simulation_hours / 24))`` floored EVERY sub-24h request to a full
    # 24 h deck window: a requested 10 h surge ran 24 h, so the surge had already
    # peaked + receded + held drained for 14 h, the wet front was already fully
    # inland by frame 0, and the inundation read shallow. Anchoring tstop to the
    # real requested hours makes SFINCS run exactly the forcing window, so the
    # rising surge limb actually marches the front inland across frames.
    _SFINCS_TREF = datetime(2026, 1, 1, 0, 0, 0)
    _sim_hours = max(1.0, float(options.simulation_hours))
    _tstop_dt = _SFINCS_TREF + timedelta(hours=_sim_hours)
    components.append(f'  tref: "{_SFINCS_TREF.strftime("%Y%m%d %H%M%S")}"')
    components.append(f'  tstart: "{_SFINCS_TREF.strftime("%Y%m%d %H%M%S")}"')
    components.append(f'  tstop: "{_tstop_dt.strftime("%Y%m%d %H%M%S")}"')
    # --- Map-output cadence (flood-animation Phase 1, engine-agnostic) ---
    # ``dtout`` / ``dtmaxout`` are native sfincs.inp parameters (seconds) that
    # make SFINCS write TIME-VARYING map output — i.e. ``zs(time,n,m)`` snapshots
    # into sfincs_map.nc. WITHOUT them SFINCS writes only the max fields
    # (``zsmax`` / ``hmax``) and postprocess_flood can only build the single peak
    # COG (no animation). The cadence flows through HydroMT's setup_config
    # passthrough alongside tref/tstart/tstop.
    #
    # PLUVIAL (``options.output_interval_min is None``): the legacy HOURLY cadence
    #  -  ~MAX_FLOOD_FRAMES (24) raw snapshots over the whole sim window, floored at
    # 600 s (10 min) to match SFINCS's internal 10-minute precip grid. A rising
    # rain-driven sheet reads fine at hourly stride and finer buys nothing while
    # bloating sfincs_map.nc. Byte-identical to the pre-cadence deck.
    #
    # COASTAL/WAVE (``options.output_interval_min`` set): a FINE minute-scale
    # stride so the animation shows water rolling in. Waves move in
    # seconds-to-minutes; an hourly surge snapshot looks like a slowly-filling
    # bathtub regardless of SnapWave. The physical floor here is 60 s (the wave
    # cadence justifies sub-10-min output, unlike the precip grid) so a tiny
    # requested interval can't drive a pathological dtout=0.
    _total_seconds = int(max(1.0, options.simulation_hours) * 3600)
    if options.output_interval_min is not None:
        # FINE wave cadence: requested minutes -> seconds, floored at 60 s.
        _WAVE_DTOUT_FLOOR_S = 60
        dtout_seconds = max(
            _WAVE_DTOUT_FLOOR_S,
            int(round(float(options.output_interval_min) * 60.0)),
        )
    else:
        # Legacy HOURLY cadence (pluvial, unchanged): ~24 frames, 600 s floor.
        dtout_seconds = max(600, int(_total_seconds / 24))
    components.append(f"  dtout: {dtout_seconds}")
    components.append(f"  dtmaxout: {dtout_seconds}")
    # NATE 2026-06-26: advanced-physics overrides + a bare-constant infiltration
    # qinf land in THIS setup_config block (HydroMT passthrough -> sfincs.inp).
    # ``None`` advanced_physics + no bare-constant infiltration emit nothing, so a
    # plain pluvial deck stays byte-identical.
    _emit_physics_config(
        components, options.advanced_physics, infiltration=forcing.infiltration
    )
    components.append("setup_grid_from_region:")
    components.append(
        f"  region: {{ bbox: [{bbox[0]}, {bbox[1]}, {bbox[2]}, {bbox[3]}] }}"
    )
    components.append(f"  res: {grid_res}")
    # job-0248 (supersedes the job-0170 /vsigs/ rewrite FOR THE CATALOG PATH
    # ONLY): HydroMT's data adapter stats catalog paths with fsspec's LOCAL
    # filesystem before GDAL ever opens them, so a /vsigs/ GDAL-ism raises
    # "No such file found" even when the GCS object exists (proven live,
    # round-5 Stage 3). gs:// inputs are therefore STAGED to a local cache
    # via google-cloud-storage (ADC) and HydroMT receives a real local path
    # — which also keeps gcsfs out of the read path (job-0170's segfault
    # avoidance holds). Direct rasterio reads elsewhere still use /vsigs/.
    dem_read_path = _stage_gcs_local(dem_local_path)
    landcover_read_path = _stage_gcs_local(landcover_local_path)
    components.append("setup_dep:")
    components.append(f"  datasets_dep: [{{ elevtn: '{dem_read_path}' }}]")
    # job-0318: DOMAIN-ADAPTIVE active-cell mask. ``setup_mask_active``'s
    # zmin/zmax is an ELEVATION WINDOW (metres) — only cells whose DEM
    # elevation falls inside it become ACTIVE. The previous HARDCODED
    # ``zmin: -10 / zmax: 10`` only ever worked for near-sea-level / coastal
    # AOIs; any inland or elevated terrain (Asheville ~650 m) had every cell
    # above zmax=10 -> hydromt_sfincs "No active cells found" -> empty domain
    # -> zero inundation -> Pelicun PELICUN_NO_ASSETS_IN_HAZARD. We now read
    # the staged DEM's real elevation range and bracket the full terrain with
    # a small buffer so the whole AOI is active at any elevation. The DEM was
    # staged to a local file by ``_stage_gcs_local`` above, so the read is
    # local. If the range can't be read we fall back to a VERY wide window
    # (never the broken -10/10) — see ``_compute_active_mask_bounds``.
    mask_zmin, mask_zmax, _mask_adaptive = _compute_active_mask_bounds(dem_read_path)
    components.append("setup_mask_active:")
    components.append(
        f"  zmin: {mask_zmin}"
        + ("" if _mask_adaptive else "  # wide fallback (DEM range unreadable)")
    )
    components.append(f"  zmax: {mask_zmax}")
    # COASTAL SFINCS — BUILDING-OBSTACLE mask (rough urban-flood-around-buildings).
    # ``building_obstacle_mode == "exclude"`` burns the OSM footprint polygons as
    # an ``exclude_mask`` so those cells become INACTIVE (msk=0) — hard no-flow
    # holes the flood routes AROUND. This is the FAST/ROUGH approximation NATE
    # asked for (a 2D grid that respects building outlines without a true
    # building-resolving mesh). ``setup_mask_active``'s ``exclude_mask`` arg takes
    # a vector geofile path (FlatGeobuf / GeoJSON) of polygons; HydroMT clips it
    # to the domain and removes those cells from the active mask. The ``"raise"``
    # mode keeps the cells active and instead lifts their bed elevation through
    # the subgrid (``datasets_riv`` raised-bank cells) — see setup_subgrid below.
    building_uri = options.building_obstacle_uri
    obstacle_mode = (options.building_obstacle_mode or "exclude").strip().lower()
    if building_uri and obstacle_mode == "exclude":
        building_read_path = _stage_gcs_local(building_uri)
        components.append(f"  exclude_mask: '{building_read_path}'")
        components.append("  all_touched: true  # footprint-touching cells -> no-flow")
    components.append("setup_manning_roughness:")
    components.append(
        f"  datasets_rgh: [{{ lulc: '{landcover_read_path}', "
        f"reclass_table: '{mapping_csv_path}' }}]"
    )
    # COASTAL SFINCS — SUBGRID tables. ``setup_subgrid`` lets SFINCS run on a
    # COARSE computational grid while resolving sub-cell topography + roughness
    # (the standard way to get an urban-flood-around-buildings estimate cheaply).
    # We reuse the SAME dep + roughness datasets the plain path uses; the subgrid
    # derives water-level↔volume + representative-depth relations from them. When
    # a building-obstacle geofile is supplied in ``"raise"`` mode it is burned as
    # a ``datasets_riv`` raised-bank entry so the footprints impede flow without
    # disconnecting the domain (keeps active cells, unlike the exclude path).
    if options.enable_subgrid:
        components.append("setup_subgrid:")
        components.append(
            f"  datasets_dep: [{{ elevtn: '{dem_read_path}' }}]"
        )
        components.append(
            f"  datasets_rgh: [{{ lulc: '{landcover_read_path}', "
            f"reclass_table: '{mapping_csv_path}' }}]"
        )
        components.append(
            f"  nr_subgrid_pixels: {int(options.subgrid_nr_subgrid_pixels)}"
        )
        if building_uri and obstacle_mode == "raise":
            building_read_path = _stage_gcs_local(building_uri)
            # Burn footprints as raised-bank river-mask cells: the subgrid lifts
            # their bed level so water is impeded around buildings while the
            # domain stays connected (the higher-fidelity obstacle option).
            components.append(
                f"  datasets_riv: [{{ centerlines: '{building_read_path}', "
                "rivwth: 5, rivdph: -3 }]  # OSM footprints as raised obstacles"
            )
    # --- COASTAL SFINCS — surge / tide / discharge / wind / pressure forcing ---
    #
    # HISTORICAL NOTE (job-0055): for the v0.1 PLUVIAL deck ``setup_river_inflow``
    # was intentionally NOT emitted, partly because hydromt-sfincs 1.2.2's
    # ``set_forcing_1d`` (sfincs.py:1858) calls ``pd.Index.is_integer()`` which
    # pandas removed in 3.0. That blocker is now neutralised by the module-level
    # ``_install_pandas_set_forcing_1d_guard`` (re-attaches the removed Index
    # predicates), so the 1D-forcing sink is reachable again. These blocks are
    # emitted ONLY when the matching ``ForcingSpec`` member is present — a pure
    # pluvial deck (no surge members) is byte-identical to the v0.1 deck.
    #
    # ORDER MATTERS: ``setup_river_inflow`` MUST precede ``setup_discharge_forcing``
    # — the former establishes the ``src`` discharge points (and trims boundary
    # cells the river crosses); the latter attaches the time series to them.
    _emit_surge_forcing_blocks(components, forcing)
    # --- Precip forcing emission (uniform netamt magnitude) ---
    #
    # Two upstream paths converge on the same SFINCS ``setup_precip_forcing``
    # ``magnitude`` (mm/hr) — a single uniform precipitation hyetograph the
    # source projects onto a 10-minute time grid (``get_model_time()``):
    #
    #   1. ``pluvial_synthetic`` (Atlas 14 design storm, M5 v0.1): the
    #      magnitude is DERIVED here from ``precip_inches`` over
    #      ``duration_hours`` (depth → rate arithmetic).
    #   2. ``pluvial_observed`` (job-0225 v2, real precip raster): the
    #      magnitude is PRE-COMPUTED by ``model_flood_scenario``'s
    #      ``forcing_raster_uri`` branch (area-mean of the precip raster over
    #      the model domain, in mm, divided by the accumulation window) and
    #      carried on ``forcing.precip_magnitude_mm_per_hr``. We emit it
    #      verbatim — this is the netamt fallback locked by OQ-6 (see below).
    #
    # OQ-6 (manifest, TENTATIVE → LOCKED here): SFINCS accepts precipitation
    # as ``netamt`` (uniform mm/hr — what ``setup_precip_forcing``'s
    # ``magnitude`` produces) OR ``spw`` (spatially-variable precip via
    # NetCDF). v0.1 maps a precip raster to a SINGLE area-mean ``magnitude``
    # (netamt). This collapses spatial structure but demonstrates the
    # real-data forcing path end-to-end. SPW UPGRADE PATH: when the SFINCS
    # container is confirmed to support spw spatially-varying precip, replace
    # this single-magnitude emission for ``pluvial_observed`` with a
    # ``setup_precip_forcing_from_grid`` (hydromt-sfincs ≥ 1.1) step that
    # ingests the precip raster as a time-resolved 2D grid → SFINCS
    # ``precip_2d.nc`` (spw). That keeps the raster's spatial gradient (e.g.
    # an MRMS QPE band crossing the domain) instead of flattening to a mean.
    # The container-support finding for spw is recorded in this job's
    # report.md (job-0225).
    if (
        forcing.forcing_type == "pluvial_observed"
        and forcing.precip_magnitude_mm_per_hr is not None
    ):
        # job-0225 v2 — area-mean netamt path. The magnitude was computed
        # upstream from a real precip raster (MRMS QPE / ERA5 / gridMET); we
        # do NOT re-derive it from depth here. ``precip_inches`` may be None
        # on this path (observed forcing has no Atlas 14 depth).
        magnitude_mm_per_hr = forcing.precip_magnitude_mm_per_hr
        accum_hr = forcing.duration_hours or 24.0
        mean_mm = magnitude_mm_per_hr * accum_hr
        components.append("setup_precip_forcing:")
        components.append(
            f"  magnitude: {magnitude_mm_per_hr}  # mm/hr "
            f"(observed precip raster: area-mean {mean_mm:.4f} mm over "
            f"{accum_hr} hr → {magnitude_mm_per_hr:.4f} mm/hr; netamt fallback, "
            "OQ-6 — spw spatial path is the documented upgrade)"
        )
    elif forcing.forcing_type == "pluvial_synthetic" and forcing.precip_inches is not None:
        # OQ-54 fix (job-0054): the live 1.2.x signature is
        # ``setup_precip_forcing(timeseries=None, magnitude=None)``; ``precip``
        # / ``duration_hr`` (what we previously emitted) are NOT accepted
        # kwargs and would raise ``TypeError: got an unexpected keyword
        # argument``. We convert Atlas 14 (depth in inches over duration
        # hours) to a constant rate in mm/hr and pass ``magnitude``:
        #
        #     magnitude = precip_inches * 25.4 / duration_hours    [mm/hr]
        #
        # SFINCS receives this as a uniform precipitation hyetograph (the
        # source builds a 10-minute time grid from ``get_model_time()`` and
        # fills with ``magnitude``).
        duration_hr = forcing.duration_hours or 24.0
        magnitude_mm_per_hr = (forcing.precip_inches * 25.4) / duration_hr
        components.append("setup_precip_forcing:")
        components.append(
            f"  magnitude: {magnitude_mm_per_hr}  # mm/hr "
            f"(Atlas 14: {forcing.precip_inches} in over "
            f"{duration_hr} hr → {magnitude_mm_per_hr:.4f} mm/hr)"
        )
    return "\n".join(components)



# --------------------------------------------------------------------------- #
# Object staging — worker-local (decoupled from the agent's tools.solver/cache).
# --------------------------------------------------------------------------- #
# The orchestrator pre-downloads every input + forcing file to a LOCAL path and
# rewrites the spec, so ``_stage_gcs_local`` (the name the vendored YAML/forcing
# emitters call) only ever sees a local path here -> identity passthrough. A
# stray remote URI raises loudly rather than silently reaching HydroMT.


def _stage_gcs_local(uri: str) -> str:
    """Identity for a LOCAL path (strip ``file://``). Remote URIs are a bug on
    the worker build path (the orchestrator localizes everything first)."""
    if not uri:
        return uri
    if uri.startswith("file://"):
        return uri[len("file://"):]
    if uri.startswith("s3://") or uri.startswith("gs://"):
        raise SFINCSSetupError(
            "BUILD_INPUT_NOT_LOCALIZED",
            message=(
                f"worker build received a non-local forcing/input URI ({uri!r}); "
                "build_sfincs_deck must localize every input before YAML emission"
            ),
            details={"uri": uri},
        )
    return uri


def _extract_unique_nlcd_classes(landcover_local_path: str) -> set[int]:
    """Read a LOCAL landcover raster and return its unique NLCD class set.

    Worker variant of the agent gate read: inputs are already local (no S3
    scheme dispatch). Filters the dataset nodata + the common GeoTIFF sentinels.
    """
    try:
        import numpy as np  # type: ignore[import-not-found]
        import rasterio  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise SFINCSSetupError(
            "LANDCOVER_READ_FAILED",
            message=f"rasterio/numpy not available for landcover class extraction: {exc}",
            details={"landcover_uri": landcover_local_path},
        ) from exc
    try:
        with rasterio.open(_stage_gcs_local(landcover_local_path)) as src:
            arr = src.read(1)
            nodata = src.nodata
    except Exception as exc:  # noqa: BLE001
        raise SFINCSSetupError(
            "LANDCOVER_READ_FAILED",
            message=f"rasterio.open({landcover_local_path}) failed: {exc}",
            details={"landcover_uri": landcover_local_path},
        ) from exc
    classes: set[int] = set()
    for v in np.unique(arr).tolist():
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if nodata is not None and iv == int(nodata):
            continue
        if iv in (-9999, 255):
            continue
        classes.add(iv)
    return classes



# --------------------------------------------------------------------------- #
# job_spec deserialization (the agent-composed build spec, JSON dict).
# --------------------------------------------------------------------------- #
# Mirror of the agent's ForcingSpec/BuildOptions dataclasses. The agent
# serializes these to plain dicts in ``model_flood_scenario`` (see
# ``_forcing_spec_to_dict`` / ``_build_options_to_dict``); we reconstruct the
# typed objects here. Kept name-for-name with the dataclass fields so the
# contract is a flat dict, no shared class import needed.

_FORCING_FILE_URI_KEYS = (
    "geodataset_uri",
    "timeseries_uri",
    "locations_uri",
    "rivers_uri",
    "hydrography_uri",
    "grid_uri",
    "cn_uri",
    "lulc_uri",
    "reclass_table_uri",
)


def _waterlevel_from_dict(d: dict[str, Any] | None) -> "WaterlevelForcing | None":
    if not d:
        return None
    return WaterlevelForcing(
        timeseries_uri=d.get("timeseries_uri"),
        locations_uri=d.get("locations_uri"),
        geodataset_uri=d.get("geodataset_uri"),
        offset=d.get("offset"),
        buffer_m=d.get("buffer_m"),
        provenance=dict(d.get("provenance") or {}),
    )


def _discharge_from_dict(d: dict[str, Any] | None) -> "DischargeForcing | None":
    if not d:
        return None
    return DischargeForcing(
        timeseries_uri=d.get("timeseries_uri"),
        locations_uri=d.get("locations_uri"),
        rivers_uri=d.get("rivers_uri"),
        hydrography_uri=d.get("hydrography_uri"),
        river_upa_km2=d.get("river_upa_km2"),
        provenance=dict(d.get("provenance") or {}),
    )


def _wind_from_dict(d: dict[str, Any] | None) -> "WindForcing | None":
    if not d:
        return None
    return WindForcing(
        magnitude=d.get("magnitude"),
        direction=d.get("direction"),
        grid_uri=d.get("grid_uri"),
        provenance=dict(d.get("provenance") or {}),
    )


def _pressure_from_dict(d: dict[str, Any] | None) -> "PressureForcing | None":
    if not d or not d.get("grid_uri"):
        return None
    return PressureForcing(
        grid_uri=d["grid_uri"],
        fill_value=d.get("fill_value"),
        provenance=dict(d.get("provenance") or {}),
    )


def _infiltration_from_dict(d: dict[str, Any] | None) -> "InfiltrationForcing | None":
    if not d:
        return None
    return InfiltrationForcing(
        cn_uri=d.get("cn_uri"),
        antecedent_moisture=d.get("antecedent_moisture"),
        constant_mm_per_hr=d.get("constant_mm_per_hr"),
        lulc_uri=d.get("lulc_uri"),
        reclass_table_uri=d.get("reclass_table_uri"),
        provenance=dict(d.get("provenance") or {}),
    )


def forcing_spec_from_dict(d: dict[str, Any]) -> ForcingSpec:
    """Reconstruct a ``ForcingSpec`` from the job_spec ``forcing`` dict."""
    return ForcingSpec(
        forcing_type=d["forcing_type"],
        precip_inches=d.get("precip_inches"),
        duration_hours=d.get("duration_hours"),
        return_period_years=d.get("return_period_years"),
        precip_magnitude_mm_per_hr=d.get("precip_magnitude_mm_per_hr"),
        waterlevel=_waterlevel_from_dict(d.get("waterlevel")),
        discharge=_discharge_from_dict(d.get("discharge")),
        breach=_discharge_from_dict(d.get("breach")),
        wind=_wind_from_dict(d.get("wind")),
        pressure=_pressure_from_dict(d.get("pressure")),
        infiltration=_infiltration_from_dict(d.get("infiltration")),
        provenance=dict(d.get("provenance") or {}),
    )


def build_options_from_dict(d: dict[str, Any]) -> BuildOptions:
    """Reconstruct ``BuildOptions`` from the job_spec ``options`` dict."""
    base = BuildOptions()
    return BuildOptions(
        grid_resolution_m=float(d.get("grid_resolution_m", base.grid_resolution_m)),
        simulation_hours=float(d.get("simulation_hours", base.simulation_hours)),
        crs=str(d.get("crs", base.crs)),
        output_setup_uri=None,
        compute_class=str(d.get("compute_class", base.compute_class)),
        autoscale_grid=bool(d.get("autoscale_grid", base.autoscale_grid)),
        output_interval_min=d.get("output_interval_min"),
        enable_subgrid=bool(d.get("enable_subgrid", base.enable_subgrid)),
        subgrid_nr_subgrid_pixels=int(
            d.get("subgrid_nr_subgrid_pixels", base.subgrid_nr_subgrid_pixels)
        ),
        building_obstacle_uri=d.get("building_obstacle_uri"),
        building_obstacle_mode=str(
            d.get("building_obstacle_mode", base.building_obstacle_mode)
        ),
        advanced_physics=d.get("advanced_physics"),
    )


# --------------------------------------------------------------------------- #
# build_sfincs_deck — the WORKER-side orchestrator (build LOCALLY into a dir).
# --------------------------------------------------------------------------- #


def _localize_forcing_uris(
    forcing: dict[str, Any], download, dest_dir: Path
) -> dict[str, Any]:
    """Download every remote forcing FILE URI to ``dest_dir`` + rewrite in place.

    Returns a deep-ish copy of the forcing dict with each ``*_uri`` sub-field
    that names a remote object (s3://, gs://) replaced by its local path. Local
    paths pass through untouched. Non-file fields are copied verbatim.
    """
    out: dict[str, Any] = {}
    for member_name, member in forcing.items():
        if not isinstance(member, dict):
            out[member_name] = member
            continue
        new_member = dict(member)
        for key in _FORCING_FILE_URI_KEYS:
            uri = new_member.get(key)
            if not uri or not isinstance(uri, str):
                continue
            if uri.startswith("s3://") or uri.startswith("gs://"):
                fname = f"{member_name}__{key}__{Path(uri.split('?', 1)[0]).name}"
                dest = dest_dir / fname
                download(uri, dest)
                new_member[key] = str(dest)
            elif uri.startswith("file://"):
                new_member[key] = uri[len("file://"):]
        out[member_name] = new_member
    return out


def build_sfincs_deck(
    spec: dict[str, Any],
    scratch: Path,
    download,
) -> dict[str, Any]:
    """Build a regular-grid SFINCS deck from the agent's job_spec, LOCALLY.

    Args:
        spec: the validated job_spec dict (see ``spec.validate_job_spec``):
            ``{bbox, nlcd_vintage_year, inputs:{dem_uri, landcover_uri,
            river_uri?}, forcing:{...}, options:{...}}``. Input + forcing URIs
            may be s3:// / gs:// / local.
        scratch: the worker scratch dir. The deck is written to ``scratch/deck``;
            inputs are staged under ``scratch/inputs``.
        download: callable ``download(uri, dest_path)`` (the entrypoint's
            scheme-aware ``_download``) used to localize every remote input.

    Returns:
        A provenance dict ``{deck_dir, grid_resolution_m, autoscale, forcing_type,
        nlcd_vintage_year, fetched_classes, mask_adaptive}`` the entrypoint folds
        into completion.json. The deck itself lives at ``<deck_dir>``.

    Raises:
        SFINCSSetupError: any build-time failure (LULC_MAPPING_MISMATCH,
            LANDCOVER_READ_FAILED, HYDROMT_UNAVAILABLE, HYDROMT_BUILD_FAILED,
            FORCING_OUT_OF_RANGE, ...). The entrypoint maps ``error_code`` into
            completion.json so the agent surfaces the same failed envelope.
    """
    inputs_dir = scratch / "inputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    deck_dir = scratch / "deck"

    bbox = tuple(float(v) for v in spec["bbox"])  # type: ignore[assignment]
    nlcd_vintage_year = spec.get("nlcd_vintage_year")

    # --- Localize inputs (DEM / landcover / river) ---
    def _localize(uri: str | None, name: str) -> str | None:
        if not uri:
            return None
        if uri.startswith("file://"):
            return uri[len("file://"):]
        if not (uri.startswith("s3://") or uri.startswith("gs://")):
            return uri  # already a local path
        dest = inputs_dir / f"{name}{Path(uri.split('?', 1)[0]).suffix or '.tif'}"
        download(uri, dest)
        return str(dest)

    inp = spec["inputs"]
    dem_local = _localize(inp.get("dem_uri"), "dem")
    landcover_local = _localize(inp.get("landcover_uri"), "landcover")
    river_local = _localize(inp.get("river_uri"), "river")
    if not dem_local or not landcover_local:
        raise SFINCSSetupError(
            "BUILD_INPUT_MISSING",
            message="job_spec.inputs must carry dem_uri + landcover_uri",
            details={"inputs": inp},
        )

    # --- Localize forcing files + reconstruct the typed spec ---
    forcing_dict = _localize_forcing_uris(
        dict(spec.get("forcing") or {}), download, inputs_dir
    )
    forcing = forcing_spec_from_dict(forcing_dict)
    opts = build_options_from_dict(dict(spec.get("options") or {}))
    if opts.building_obstacle_uri:
        opts = replace(
            opts,
            building_obstacle_uri=_localize(opts.building_obstacle_uri, "buildings"),
        )

    # --- Forcing sanity (mirror build_sfincs_model) ---
    if forcing.forcing_type == "pluvial_synthetic" and (
        forcing.precip_inches is None or forcing.precip_inches <= 0
    ):
        raise SFINCSSetupError(
            "FORCING_OUT_OF_RANGE",
            message=f"pluvial forcing requires positive precip_inches; got {forcing.precip_inches!r}",
        )
    if forcing.forcing_type == "pluvial_observed" and (
        forcing.precip_magnitude_mm_per_hr is None
        or forcing.precip_magnitude_mm_per_hr <= 0
    ):
        raise SFINCSSetupError(
            "FORCING_OUT_OF_RANGE",
            message="pluvial_observed forcing requires positive precip_magnitude_mm_per_hr",
        )

    # --- Manning mapping + the OQ-4 §4 NLCD validation gate (defensive; the
    #     agent already ran it pre-submit, but a clean solve must never dispatch
    #     silently-wrong roughness) ---
    mapping = load_manning_mapping()
    fetched_classes = _extract_unique_nlcd_classes(landcover_local)
    logger.info(
        "worker build: landcover classes=%s (vintage=%s)",
        sorted(fetched_classes), nlcd_vintage_year,
    )
    if nlcd_vintage_year is not None:
        validate_nlcd_vintage_against_mapping(
            fetched_classes=fetched_classes,
            nlcd_vintage_year=int(nlcd_vintage_year),
            mapping=mapping,
            mapping_version=MANNING_MAPPING_VERSION,
            mapping_csv_path=str(MANNING_MAPPING_PATH),
        )

    # --- Adaptive grid autoscale (reads the LOCAL DEM) ---
    autoscale_result = None
    if opts.autoscale_grid:
        try:
            mask_zmin, mask_zmax, _adaptive = _compute_active_mask_bounds(dem_local)
            autoscale_result = autoscale_grid_resolution(
                dem_local, bbox, zmin=mask_zmin, zmax=mask_zmax,
                compute_class=opts.compute_class,
                base_resolution_m=opts.grid_resolution_m,
            )
            if autoscale_result.grid_resolution_m != opts.grid_resolution_m:
                opts = replace(opts, grid_resolution_m=autoscale_result.grid_resolution_m)
        except Exception as exc:  # noqa: BLE001 — autoscale must never break the build
            logger.warning(
                "worker build: grid autoscale failed (%s); proceeding at %.1f m",
                exc, opts.grid_resolution_m,
            )

    # --- HydroMT-SFINCS build (LOCAL deck; no S3 round-trip) ---
    try:
        from hydromt_sfincs import SfincsModel  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise SFINCSSetupError(
            "HYDROMT_UNAVAILABLE",
            message=f"hydromt_sfincs not importable in the worker image: {exc}",
            details={"import_error": str(exc)},
        ) from exc

    reclass_csv = _write_hydromt_reclass_table_csv(mapping, scratch / "manning_reclass.csv")
    yaml_text = _generate_hydromt_yaml_config(
        bbox=bbox,
        options=opts,
        dem_local_path=dem_local,
        landcover_local_path=landcover_local,
        river_local_path=river_local,
        forcing=forcing,
        mapping_csv_path=str(reclass_csv),
    )
    (scratch / "sfincs_build.yml").write_text(yaml_text, encoding="utf-8")
    try:
        opt_dict = yaml.safe_load(yaml_text)
        model = SfincsModel(root=str(deck_dir), mode="w")
        model.build(opt=opt_dict)
        model.write()
    except SFINCSSetupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SFINCSSetupError(
            "HYDROMT_BUILD_FAILED",
            message=f"HydroMT SfincsModel build failed: {exc}",
            details={"bbox": list(bbox), "underlying": str(exc)},
        ) from exc

    logger.info(
        "worker build: deck written to %s (grid_res=%.1f m, autoscale=%s)",
        deck_dir, opts.grid_resolution_m,
        (autoscale_result is not None and autoscale_result.coarsened),
    )
    return {
        "deck_dir": str(deck_dir),
        "grid_resolution_m": opts.grid_resolution_m,
        "forcing_type": forcing.forcing_type,
        "nlcd_vintage_year": nlcd_vintage_year,
        "fetched_classes": sorted(fetched_classes),
        "output_interval_min": opts.output_interval_min,
        "autoscale": (
            {
                "grid_resolution_m": autoscale_result.grid_resolution_m,
                "estimated_active_cells": autoscale_result.estimated_active_cells,
                "estimated_active_cells_at_base": autoscale_result.estimated_active_cells_at_base,
                "cell_cap": autoscale_result.cell_cap,
                "vcpus": autoscale_result.vcpus,
                "base_resolution_m": autoscale_result.base_resolution_m,
                "estimated_solve_seconds": autoscale_result.estimated_solve_seconds,
                "coarsened": autoscale_result.coarsened,
                "reason": autoscale_result.reason,
            }
            if autoscale_result is not None
            else None
        ),
    }
