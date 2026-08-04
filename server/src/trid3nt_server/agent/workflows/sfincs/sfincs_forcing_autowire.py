"""SFINCS forcing synthesis + autowire library.

Engine-door conformance split: the ~1,200-line forcing synthesis/autowire library
(precip area-mean, surge-member construction, CO-OPS/GTSM/parametric surge autowire,
tide base, spiderweb resolve, river-discharge autowire, infiltration/building-obstacle
resolve, breach + tsunami synthesis) is factored out of ``flood/flood.py`` into this
engine-support module. ``model_flood_scenario`` re-imports these and calls them
exactly as before -- pure reorganization, no behavior change.

The module logs under the ``...sfincs.flood.flood`` logger name (unchanged) so the
observable log surface is byte-identical to the pre-split module.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.envelope import DataSource
from trid3nt_server.agent.workflows.sfincs.sfincs_builder import (
    DischargeForcing,
    PressureForcing,
    SpiderwebForcing,
    WaterlevelForcing,
    WindForcing,
    _to_vsigs,
)

logger = logging.getLogger("trid3nt_server.agent.workflows.sfincs.flood.flood")


# --------------------------------------------------------------------------- #
# v2 - real-precip forcing branch (area-mean netamt)
# --------------------------------------------------------------------------- #


class PrecipForcingError(RuntimeError):
    """Raised when the observed-precip-raster forcing path cannot be computed.

    Carries an A.6 open-set ``error_code`` so the workflow surface lifts it
    into a failed AssessmentEnvelope (same pattern as ``SFINCSSetupError``).
    Codes:
    - ``PRECIP_RASTER_READ_FAILED`` -- the raster bytes were unreadable.
    - ``PRECIP_RASTER_EMPTY`` -- the raster had no valid (non-nodata) cells in
      the domain → no area-mean is computable.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def compute_precip_area_mean_mm_per_hr(
    forcing_raster_uri: str,
    bbox: tuple[float, float, float, float],
    accumulation_hours: float,
    *,
    raster_units: str = "mm",
) -> tuple[float, float]:
    """Compute the AREA-MEAN accumulated precip over the model domain → mm/hr.

     v2 (netamt fallback). Reads the precipitation raster at
    ``forcing_raster_uri`` (an accumulated-precip COG -- MRMS QPE, ERA5,
    gridMET, …), computes the mean over all valid cells, and converts that
    single domain-mean accumulated depth into a uniform SFINCS ``netamt``
    rate in **mm/hr** by dividing by the ``accumulation_hours`` window.

    This collapses the raster's spatial structure to one number -- the v0.1
    netamt fallback locked by manifest. The spw spatially-varying-precip
    upgrade path (ingest the raster as a 2D time grid) is documented in
    ``sfincs_builder._generate_hydromt_yaml_config`` + this job's report.md.

    Domain handling (v0.1): we average over EVERY valid cell in the raster.
    The fetchers that produce the precip raster (e.g. ``fetch_mrms_qpe``) clip
    to roughly the requested bbox already, so the raster footprint ≈ the model
    domain. A future refinement would window-read the raster to the exact bbox
    before averaging; for v0.1 the
    whole-raster mean is the documented behavior.

    Args:
        forcing_raster_uri: ``gs://...`` (or local path / ``/vsigs/...``) URI
            of the accumulated-precip COG.
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` -- the model domain.
            Carried for provenance + future exact-window cropping; v0.1 uses
            the whole-raster mean.
        accumulation_hours: the precip accumulation window in hours (e.g. 24
            for a 24h QPE product). The area-mean accumulated depth is divided
            by this to yield mm/hr. Must be positive.
        raster_units: declared units of the raster values. Default ``"mm"``
            (the MRMS/ERA5/gridMET convention used by our fetchers). If
            ``"inches"`` the mean is multiplied by 25.4 to reach mm before the
            per-hour conversion.

    Returns:
        ``(magnitude_mm_per_hr, area_mean_mm)`` -- the uniform SFINCS netamt
        rate AND the area-mean accumulated depth in mm (echoed into forcing
        provenance for narration).

    Raises:
        PrecipForcingError("PRECIP_RASTER_READ_FAILED"): the read failed.
        PrecipForcingError("PRECIP_RASTER_EMPTY"): no valid cells.
        ValueError: ``accumulation_hours <= 0``.
    """
    if accumulation_hours <= 0:
        raise ValueError(
            f"accumulation_hours must be positive; got {accumulation_hours!r}"
        )
    try:
        import numpy as np  # type: ignore[import-not-found]
        import rasterio  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PrecipForcingError(
            "PRECIP_RASTER_READ_FAILED",
            f"rasterio/numpy not available for precip area-mean: {exc}",
        ) from exc

    # Scheme dispatch for the forcing-raster read:
    # s3:// - boto3 stage-then-open. GDAL's
    #            /vsis3/ credential chain does NOT resolve the EC2 instance role
    #            in this env (boto3 does) -- observed live: "does not exist" on an
    #            existing object. Stage the bytes via the shared boto3 reader and
    #            open in-memory (MemoryFile frees with the dataset; no temp-file
    #            leak -- mirrors extract_landcover_class._open_source). The MRMS
    #            COG is bbox-clipped/small, so a whole-file fetch is safe.
    #   gs:// / /vsigs/ / file:// / local - keep the GDAL /vsigs/ path (
    #            -- keeps the fragile gcsfs path out of the read; local pass-through).
    try:
        if forcing_raster_uri.startswith("s3://"):
            from rasterio.io import MemoryFile  # type: ignore[import-not-found]

            from trid3nt_server.agent.tools.cache import read_object_bytes_s3

            with MemoryFile(read_object_bytes_s3(forcing_raster_uri)) as mf:
                with mf.open() as src:
                    arr = src.read(1).astype("float64")
                    nodata = src.nodata
        else:
            read_path = _to_vsigs(forcing_raster_uri)
            with rasterio.open(read_path) as src:
                arr = src.read(1).astype("float64")
                nodata = src.nodata
    except Exception as exc:  # noqa: BLE001
        raise PrecipForcingError(
            "PRECIP_RASTER_READ_FAILED",
            f"rasterio.open({forcing_raster_uri}) failed: {exc}",
        ) from exc

    # Mask nodata + common sentinels + non-finite values. Negative precip is
    # physically invalid (some products use negatives as fill) -- mask those
    # too so they don't drag the mean.
    mask = np.isfinite(arr)
    if nodata is not None:
        mask &= arr != nodata
    mask &= arr != -9999.0
    mask &= arr >= 0.0
    valid = arr[mask]
    if valid.size == 0:
        raise PrecipForcingError(
            "PRECIP_RASTER_EMPTY",
            f"precip raster {forcing_raster_uri} has no valid cells over the "
            f"domain {bbox} — no area-mean computable",
        )

    area_mean = float(valid.mean())
    if raster_units == "inches":
        area_mean_mm = area_mean * 25.4
    else:
        area_mean_mm = area_mean
    magnitude_mm_per_hr = area_mean_mm / accumulation_hours
    logger.info(
        "precip area-mean: raster=%s valid_cells=%d mean=%.4f %s "
        "(%.4f mm) / %.2f hr → %.6f mm/hr",
        forcing_raster_uri,
        int(valid.size),
        area_mean,
        raster_units,
        area_mean_mm,
        accumulation_hours,
        magnitude_mm_per_hr,
    )
    return magnitude_mm_per_hr, area_mean_mm


# --------------------------------------------------------------------------- #
# COASTAL SFINCS -- surge-forcing member construction (engine plumbing)
# --------------------------------------------------------------------------- #


def _build_surge_forcing_members(
    surge_forcing: dict[str, Any] | None,
) -> tuple[
    WaterlevelForcing | None,
    DischargeForcing | None,
    WindForcing | None,
    PressureForcing | None,
]:
    """Translate the workflow ``surge_forcing`` dict into typed ``ForcingSpec`` members.

    Coastal SFINCS couples surge / tide / discharge / wind /
    pressure forcing into the SFINCS deck. The workflow caller (or a future
    fetcher-plumbing step that materialises ``fetch_gtsm_tide_surge`` /
    ``fetch_noaa_coops_tides`` / ``fetch_noaa_nwm_streamflow``
    hydrographs to CSV + locations) supplies a
    nested dict::

        {
          "waterlevel": {"timeseries_uri": ..., "locations_uri": ...,
                          "geodataset_uri": ..., "offset": ..., "buffer_m": ...},
          "discharge":  {"timeseries_uri": ..., "locations_uri": ...,
                          "rivers_uri": ..., "hydrography_uri": ...,
                          "river_upa_km2": ...},
          "wind":       {"magnitude": ..., "direction": ...} | {"grid_uri": ...},
          "pressure":   {"grid_uri": ..., "fill_value": ...},
        }

    Any subset of keys may be present; an absent / empty sub-dict yields ``None``
    for that member (no block emitted). Unknown keys inside a sub-dict are
    ignored so a forward-compatible caller can't crash the build. Returns the
    four typed members ready to drop onto ``ForcingSpec``.
    """
    if not surge_forcing:
        return None, None, None, None

    def _sub(name: str) -> dict[str, Any]:
        v = surge_forcing.get(name)
        return dict(v) if isinstance(v, dict) else {}

    wl_raw = _sub("waterlevel")
    waterlevel = (
        WaterlevelForcing(
            timeseries_uri=wl_raw.get("timeseries_uri"),
            locations_uri=wl_raw.get("locations_uri"),
            geodataset_uri=wl_raw.get("geodataset_uri"),
            offset=wl_raw.get("offset"),
            buffer_m=wl_raw.get("buffer_m"),
            provenance={k: v for k, v in wl_raw.items() if k.startswith("_prov")},
        )
        if wl_raw and (
            wl_raw.get("timeseries_uri") or wl_raw.get("geodataset_uri")
        )
        else None
    )

    dq_raw = _sub("discharge")
    discharge = (
        DischargeForcing(
            timeseries_uri=dq_raw.get("timeseries_uri"),
            locations_uri=dq_raw.get("locations_uri"),
            rivers_uri=dq_raw.get("rivers_uri"),
            hydrography_uri=dq_raw.get("hydrography_uri"),
            river_upa_km2=dq_raw.get("river_upa_km2"),
        )
        if dq_raw and (
            dq_raw.get("timeseries_uri")
            or dq_raw.get("rivers_uri")
            or dq_raw.get("hydrography_uri")
        )
        else None
    )

    wd_raw = _sub("wind")
    wind = (
        WindForcing(
            magnitude=wd_raw.get("magnitude"),
            direction=wd_raw.get("direction"),
            grid_uri=wd_raw.get("grid_uri"),
        )
        if wd_raw
        and (
            wd_raw.get("grid_uri")
            or (wd_raw.get("magnitude") is not None and wd_raw.get("direction") is not None)
        )
        else None
    )

    pr_raw = _sub("pressure")
    pressure = (
        PressureForcing(
            grid_uri=pr_raw["grid_uri"],
            fill_value=pr_raw.get("fill_value"),
        )
        if pr_raw and pr_raw.get("grid_uri")
        else None
    )

    return waterlevel, discharge, wind, pressure


def _resolve_surge_forcing_from_fetchers(
    surge_forcing: dict[str, Any] | None,
    bbox: tuple[float, float, float, float],
    *,
    window_hours: float | None = None,
    data_sources: list[DataSource] | None = None,
) -> dict[str, Any] | None:
    """Materialise RAW fetcher outputs in ``surge_forcing`` into deck-ready URIs.

    This is the fetcher → ADAPTER bridge: it lets a caller hand
    ``model_flood_scenario`` the RAW surge/discharge fetcher outputs (a GTSM /
    CO-OPS / NWM ``LayerURI`` or FlatGeobuf URI, or a CaMa-Flood COG) instead of
    pre-materialised bzs/dis CSV + locations files. The adapter
    (``sfincs_forcing_adapter``) converts each hydrograph into the SFINCS
    ``timeseries_uri`` + ``locations_uri`` pair that
    ``_build_surge_forcing_members`` → the deck-emission seam expects.

    Recognised RAW keys inside a sub-dict (in addition to the already-materialised
    ``timeseries_uri`` / ``locations_uri`` / ``geodataset_uri`` the deck consumes
    verbatim):

    - ``waterlevel.fetch_uri`` (or ``fgb_uri``) -- a GTSM / CO-OPS FlatGeobuf to
      adapt into bzs files. Optional ``offset`` / ``buffer_m`` pass through.
    - ``discharge.fetch_uri`` (or ``fgb_uri``) -- an NWM FlatGeobuf to adapt into
      dis files. ``rivers_uri`` / ``hydrography_uri`` / ``river_upa_km2`` pass
      through.

    A sub-dict that ALREADY carries ``timeseries_uri`` / ``geodataset_uri`` is
    left untouched (the pre-materialised path -- backward compatible). Returns the
    surge_forcing dict with raw inputs replaced by materialised URIs, or the
    input unchanged when there is nothing to adapt. ``None`` → ``None``.

    Adapter failures are NOT swallowed here: a surge event the user explicitly
    requested that cannot be materialised must surface as a typed failed envelope
    (the workflow's Step-5 try/except catches ``SFINCSForcingAdapterError`` and
    threads its ``error_code``), NOT silently degrade to a pluvial-only deck
    (Invariant 7 -- never a silent wrong answer for an explicit surge request).
    """
    if not surge_forcing:
        return surge_forcing
    from trid3nt_server.agent.workflows.sfincs.sfincs_forcing_adapter import (
        discharge_forcing_from_fgb,
        waterlevel_forcing_from_fgb,
    )

    out = dict(surge_forcing)

    wl = surge_forcing.get("waterlevel")
    if isinstance(wl, dict):
        wl_fetch = wl.get("fetch_uri") or wl.get("fgb_uri")
        already = wl.get("timeseries_uri") or wl.get("geodataset_uri")
        if wl_fetch and not already:
            materialised = waterlevel_forcing_from_fgb(
                wl_fetch,
                window_hours=window_hours,
                offset=wl.get("offset"),
                buffer_m=wl.get("buffer_m"),
            )
            out["waterlevel"] = materialised
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="Water-level forcing (GTSM/CO-OPS → SFINCS bzs)",
                        uri=str(wl_fetch),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )

    dq = surge_forcing.get("discharge")
    if isinstance(dq, dict):
        dq_fetch = dq.get("fetch_uri") or dq.get("fgb_uri")
        already = dq.get("timeseries_uri") or dq.get("geodataset_uri")
        if dq_fetch and not already:
            # UNIT WIRING (Invariant-7 silent-wrong-physics guard): SFINCS dis is
            # m^3/s. A USGS NWIS hydrograph FGB carries discharge in ft^3/s (cfs);
            # NWM's streamflow_cms is already metric. discharge_forcing_from_fgb
            # defaults value_unit="cms", so a USGS hydrograph routed through here
            # WITHOUT a unit would be fed ~35.3x too large. Thread an explicit
            # value_unit, inferring cfs for USGS/NWIS sources when not supplied.
            dq_unit = dq.get("value_unit")
            if not dq_unit:
                _src = str(dq_fetch).lower()
                dq_unit = "cfs" if ("usgs" in _src or "nwis" in _src) else "cms"
            out["discharge"] = discharge_forcing_from_fgb(
                dq_fetch,
                window_hours=window_hours,
                rivers_uri=dq.get("rivers_uri"),
                hydrography_uri=dq.get("hydrography_uri"),
                river_upa_km2=dq.get("river_upa_km2"),
                value_unit=dq_unit,
            )
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="River-discharge forcing (USGS/NWM → SFINCS dis)",
                        uri=str(dq_fetch),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )

    return out


# --------------------------------------------------------------------------- #
# COASTAL SFINCS  -  auto-wire a time-varying sea-surge water-level boundary
# --------------------------------------------------------------------------- #

# Parametric design-storm surge scaling. The peak surge above the tidal datum
# (metres) is a smooth, monotone function of the design-storm return period so a
# "major hurricane / 100-yr" event shows a real, visually-meaningful multi-metre
# surge marching inland, while a frequent (2-yr) event shows only a modest rise.
# Anchored to published Gulf-coast storm-tide observations (Hurricane Michael at
# Mexico Beach peaked near 4 m NAVD88)  -  the 100-yr anchor sits at ~3.5 m, with a
# gentle log-scaling above/below so the curve never goes negative or runaway.
# Tunable via env for ops without a code change.
_SURGE_PEAK_M_AT_100YR = float(os.getenv("TRID3NT_SURGE_PEAK_M_AT_100YR", "3.5"))
_SURGE_PEAK_M_FLOOR = float(os.getenv("TRID3NT_SURGE_PEAK_M_FLOOR", "0.6"))
_SURGE_PEAK_M_CEIL = float(os.getenv("TRID3NT_SURGE_PEAK_M_CEIL", "7.5"))


def _parametric_surge_peak_m(return_period_yr: int | float | None) -> float:
    """Peak design-storm surge height (m above datum) for a return period.

    Monotone log-scaling anchored at the 100-yr peak (``_SURGE_PEAK_M_AT_100YR``):
    a larger ARI -> a higher peak, a smaller ARI -> a lower peak, clamped to a
    sane [floor, ceil] window so a degenerate / huge ARI can't drive a negative
    or runaway surge. ``log10(rp/100)`` gives 0 at 100-yr, +1 decade -> +scale,
    -1 decade -> -scale; a 0.9 m/decade slope puts 10-yr near ~2.6 m, 500-yr near
    ~4.1 m, 1000-yr near ~4.4 m  -  a realistic Gulf-coast spread.
    """
    import math

    rp = float(return_period_yr) if return_period_yr else 100.0
    rp = max(rp, 1.0)
    peak = _SURGE_PEAK_M_AT_100YR + 0.9 * math.log10(rp / 100.0)
    return max(_SURGE_PEAK_M_FLOOR, min(_SURGE_PEAK_M_CEIL, peak))


def _synthesize_parametric_surge_forcing(
    bbox: tuple[float, float, float, float],
    *,
    duration_hr: float,
    return_period_yr: int | float | None,
) -> dict[str, Any]:
    """LAST-RESORT parametric design-storm surge -> materialised bzs files dict.

    With no CO-OPS station and no CDS key (the only fully offline / key-free
    deterministic path), synthesise a smooth surge hydrograph: a base ramp that
    rises to a single peak near mid-event then recedes (a raised-cosine bump on a
    small tidal-mean offset), driven onto a handful of offshore boundary points
    laid along the SEAWARD edge of the bbox. The peak scales with
    ``return_period_yr`` via ``_parametric_surge_peak_m`` so a major-hurricane ARI
    yields a real multi-metre surge.

    Returns the SAME materialised dict shape ``waterlevel_forcing_from_fgb``
    produces (``{"timeseries_uri": <bzs.csv>, "locations_uri": <bnd.fgb>}``), so it
    flows verbatim through ``_build_surge_forcing_members`` -> a NON-None
    ``WaterlevelForcing`` (``timeseries_uri`` is set, which is the gate). The files
    are written via the SAME ``write_bzs_timeseries_csv`` / ``write_locations_fgb``
    seam the fetcher adapter uses, so the deck consumes them unchanged.
    """
    import math

    from trid3nt_server.agent.workflows.sfincs.sfincs_forcing_adapter import (
        SFINCS_TREF,
        ReanchoredSeries,
        StationHydrograph,
        write_bzs_timeseries_csv,
        write_locations_fgb,
    )

    min_lon, min_lat, max_lon, max_lat = bbox
    peak_m = _parametric_surge_peak_m(return_period_yr)
    # Small tidal-mean offset the surge rides on (a modest high-tide baseline so
    # the boundary water level is never below the datum even off-peak).
    base_m = 0.3
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0

    # --- RISING-LIMB ramp-and-hold hydrograph -------------------------------
    # A SYMMETRIC raised-cosine bump (peak at mid-event, 0 at both ends) makes the
    # surge crest at win_hr/2 and then DRAIN back to base by the window end -- the
    # peak map captures a transient that has already pushed the front fully inland
    # at the FIRST output frame, so the wet-front-advance test reads ratio ~1.0
    # (no march). Instead drive a clear RISING LIMB: hold at the tidal base for a
    # short pre-storm lead, ramp smoothly (raised half-cosine S-curve) up to the
    # full peak over the first ~40% of the window, then HOLD near the peak for the
    # remainder (a gentle final taper avoids a hard boundary discontinuity at
    # tstop). The flood therefore MARCHES inland across frames as the boundary
    # climbs, and the sustained hold lets the inundation reach its full connected
    # extent + berm runup rather than a transient crest.
    lead_hr = min(0.5, 0.05 * win_hr)            # brief pre-storm tidal lead
    rise_hr = max(1.0, 0.40 * win_hr)            # ramp base -> peak over ~40%
    rise_end_hr = lead_hr + rise_hr
    taper_hr = min(0.5, 0.05 * win_hr)           # soft easing into tstop
    taper_start_hr = max(rise_end_hr, win_hr - taper_hr)
    taper_floor = 0.92                           # never drop below 92% of peak

    # FINE sampling (~6 min) so the rising limb resolves across the minute-scale
    # output cadence (>= 2 samples so set_forcing_1d accepts it).
    _sample_s = 360.0
    n_steps = max(int(round(win_hr * 3600.0 / _sample_s)), 2)
    secs = [float(i) * (win_hr * 3600.0) / float(n_steps) for i in range(n_steps + 1)]
    values: list[float] = []
    for s in secs:
        hr = s / 3600.0
        if hr <= lead_hr:
            frac = 0.0
        elif hr < rise_end_hr:
            # raised half-cosine S-curve from 0 -> 1 across the rise window.
            x = (hr - lead_hr) / rise_hr
            frac = 0.5 * (1.0 - math.cos(math.pi * x))
        elif hr < taper_start_hr:
            frac = 1.0
        else:
            # gentle ease-down to taper_floor over the last taper window.
            x = (hr - taper_start_hr) / max(taper_hr, 1e-6)
            x = max(0.0, min(1.0, x))
            frac = 1.0 - (1.0 - taper_floor) * 0.5 * (1.0 - math.cos(math.pi * x))
        values.append(round(base_m + peak_m * frac, 4))

    # Offshore boundary points along the SEAWARD edge of the bbox. Without a
    # coastline lookup we cannot know which edge faces the sea, so we seed points
    # along ALL FOUR edges (a thin ring just inside the bbox)  -  HydroMT selects
    # the boundary cells nearest the actual water-level boundary via ``buffer_m``,
    # and the deck ignores points that don't fall on a boundary cell. A few points
    # per edge is enough to drive a coherent surge boundary.
    inset_lon = 0.02 * (max_lon - min_lon)
    inset_lat = 0.02 * (max_lat - min_lat)
    mid_lon = 0.5 * (min_lon + max_lon)
    mid_lat = 0.5 * (min_lat + max_lat)
    edge_pts: list[tuple[float, float]] = [
        (min_lon + inset_lon, mid_lat),  # west edge
        (max_lon - inset_lon, mid_lat),  # east edge
        (mid_lon, min_lat + inset_lat),  # south edge
        (mid_lon, max_lat - inset_lat),  # north edge
    ]

    times = [SFINCS_TREF + _timedelta_s(s) for s in secs]
    stations: list[StationHydrograph] = []
    series_by_id: dict[int, ReanchoredSeries] = {}
    for i, (lon, lat) in enumerate(edge_pts, start=1):
        stations.append(
            StationHydrograph(
                point_id=i,
                lon=float(lon),
                lat=float(lat),
                times=times,
                values=list(values),
                source_id=f"parametric-surge-{i}",
                provenance={"_prov_synthetic": True},
            )
        )
        series_by_id[i] = ReanchoredSeries(
            seconds=list(secs),
            datetimes=list(times),
            values=list(values),
        )

    from trid3nt_server.agent.workflows.sfincs.sfincs_forcing_adapter import _staging_dir, _unique  # local: lean top

    stage = _staging_dir(None)
    csv_path = write_bzs_timeseries_csv(series_by_id, _unique(stage, "bzs", "csv"))
    loc_path = write_locations_fgb(stations, _unique(stage, "bnd", "fgb"))
    logger.info(
        "model_flood_scenario: synthesised PARAMETRIC RISING-LIMB surge hydrograph "
        "for bbox=%s (return_period_yr=%s -> peak=%.2f m on base=%.2f m, ramp "
        "%.1f->%.1f hr then hold, %d steps over %.0f hr, %d boundary points) "
        "-> bzs=%s bnd=%s",
        bbox,
        return_period_yr,
        peak_m,
        base_m,
        lead_hr,
        rise_end_hr,
        len(secs),
        win_hr,
        len(stations),
        csv_path,
        loc_path,
    )
    return {
        "timeseries_uri": csv_path,
        "locations_uri": loc_path,
        "_prov_synthetic_parametric": True,
        "_prov_peak_m": peak_m,
        "_prov_return_period_yr": return_period_yr,
    }


def _timedelta_s(seconds: float):
    """Local helper: a ``timedelta`` of ``seconds`` (avoids a top-level import)."""
    from datetime import timedelta

    return timedelta(seconds=float(seconds))


def _autowire_coastal_surge_forcing(
    bbox: tuple[float, float, float, float],
    *,
    duration_hr: float,
    return_period_yr: int | float | None,
    data_sources: list[DataSource] | None = None,
) -> dict[str, Any]:
    """Auto-wire a time-varying SEA surge water-level boundary for a coastal run.

    Builds a water-level boundary for a ``coastal=True`` run with NO explicit
    ``surge_forcing`` so the flood animation shows water rising from the sea
    and marching inland (instead of a pure-rainfall deck).

    Degrade ladder (data-source fallback norm: primary -> fallback -> honest
    last-resort, never a silent dead-end):

    1. PRIMARY  -  NOAA CO-OPS tides (``fetch_noaa_coops_tides``): KEY-FREE, CONUS.
       Pull the observed tide+surge timeseries over the event window for the
       AOI's stations. Returns a FlatGeobuf carrying per-station ``time_series_csv``
        -  handed back as ``{"waterlevel": {"fetch_uri": <uri>}}`` so the EXISTING
       ``_resolve_surge_forcing_from_fetchers`` adapter materialises the bzs files.
    2. FALLBACK  -  GTSM tide+surge (``fetch_gtsm_tide_surge``): global, needs a CDS
       key. Attempted only if CO-OPS yields no usable station; degrades on a
       missing key / no data.
    3. LAST-RESORT  -  a PARAMETRIC design-storm surge hydrograph (key-free, offline,
       deterministic) materialised directly to bzs files. The peak scales with
       ``return_period_yr`` (a major-hurricane ARI -> a real multi-metre surge).

    Returns a ``surge_forcing`` dict whose ``waterlevel`` sub-dict yields a
    NON-None ``WaterlevelForcing`` after the resolve+build seam  -  guaranteed,
    because the last-resort parametric path is always available. Never raises (a
    fetcher exception logs + falls through to the next rung).
    """
    # Event window: anchor on "today" and run forward over the deck window. The
    # exact calendar dates do NOT matter  -  the adapter re-anchors the series onto
    # the deck's synthetic ``tref`` window (reanchor_to_tref), so we just need a
    # window long enough to carry the surge shape.
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0
    end_dt = datetime.now(timezone.utc)
    span_days = max(int((win_hr + 23) // 24), 1)
    from datetime import timedelta as _td

    start_dt = end_dt - _td(days=span_days)
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = end_dt.strftime("%Y-%m-%d")

    # --- 1) PRIMARY: NOAA CO-OPS tides (key-free, CONUS) -------------------- #
    try:
        # data-router fold: fetch_noaa_coops_tides is now a promoted spec-driven
        # tool -- resolve the callable seam by registry name (same envelope).
        from trid3nt_server.agent.tools import TOOL_REGISTRY as _TR

        _coops = _TR.get("fetch_noaa_coops_tides")
        if _coops is None:
            raise RuntimeError("fetch_noaa_coops_tides is not registered")
        layer = _coops.fn(
            bbox=bbox, start_date=start_date, end_date=end_date, product="water_level"
        )
        uri = getattr(layer, "uri", None)
        if uri:
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="NOAA CO-OPS tides (auto-wired surge boundary)",
                        uri=str(uri),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )
            logger.info(
                "model_flood_scenario: auto-wired coastal surge via NOAA CO-OPS "
                "tides for bbox=%s -> %s",
                bbox,
                uri,
            )
            return {"waterlevel": {"fetch_uri": str(uri)}}
    except Exception as exc:  # noqa: BLE001  -  degrade to the next rung
        logger.warning(
            "model_flood_scenario: NOAA CO-OPS auto-wire failed for bbox=%s "
            "(%s)  -  trying GTSM fallback.",
            bbox,
            exc,
        )

    # --- 2) FALLBACK: GTSM tide+surge (global, needs a CDS key) ------------- #
    try:
        from trid3nt_server.agent.tools import TOOL_REGISTRY  # local: keep top imports lean

        fetch_gtsm_tide_surge = TOOL_REGISTRY["fetch_gtsm_tide_surge"].fn  # spec-driven (ADR 0085)
        layer = fetch_gtsm_tide_surge(
            bbox, start_date=start_date, end_date=end_date
        )
        uri = getattr(layer, "uri", None)
        if uri:
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="GTSM tide+surge (auto-wired surge boundary)",
                        uri=str(uri),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )
            logger.info(
                "model_flood_scenario: auto-wired coastal surge via GTSM for "
                "bbox=%s -> %s",
                bbox,
                uri,
            )
            return {"waterlevel": {"fetch_uri": str(uri)}}
    except Exception as exc:  # noqa: BLE001  -  degrade to the parametric path
        logger.warning(
            "model_flood_scenario: GTSM auto-wire failed for bbox=%s (%s)  -  "
            "falling back to the PARAMETRIC design-storm surge.",
            bbox,
            exc,
        )

    # --- 3) LAST-RESORT: parametric design-storm surge (always available) --- #
    wl = _synthesize_parametric_surge_forcing(
        bbox, duration_hr=win_hr, return_period_yr=return_period_yr
    )
    if data_sources is not None:
        data_sources.append(
            DataSource(
                name=(
                    "Parametric design-storm surge (auto-wired; "
                    f"{return_period_yr}-yr, peak {wl.get('_prov_peak_m')} m)"
                ),
                uri="synthetic:parametric-surge",
                accessed_at=datetime.now(timezone.utc),
            )
        )
    return {"waterlevel": wl}


def _synthesize_tide_base_forcing(
    bbox: tuple[float, float, float, float],
    *,
    duration_hr: float,
    base_m: float = 0.3,
) -> dict[str, Any]:
    """A FLAT constant tide-base bzs boundary (default 0.3 m) for the spiderweb path.

    SPIDERWEB (2026-07-19): when a parametric hurricane spiderweb drives the
    wind+pressure, the parametric surge synthesis is SUPPRESSED (it would
    double-count the surge). But the deck MUST still carry msk=2 water-level
    boundary cells or it cannot drain/feed (the setup_mask_bounds emit is gated
    on a waterlevel member). So we emit a low CONSTANT tide-base bzs: the
    offshore boundary sits at a modest high-tide level and the surge is then
    GENERATED by the spw wind+pressure over the shelf. Same materialised
    ``{timeseries_uri, locations_uri}`` shape the fetchers produce, so it flows
    through ``_build_surge_forcing_members`` -> a non-None WaterlevelForcing.
    """
    from trid3nt_server.agent.workflows.sfincs.sfincs_forcing_adapter import (
        SFINCS_TREF,
        ReanchoredSeries,
        StationHydrograph,
        _staging_dir,
        _unique,
        write_bzs_timeseries_csv,
        write_locations_fgb,
    )

    min_lon, min_lat, max_lon, max_lat = bbox
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0
    # 2 samples spanning the window (set_forcing_1d needs >= 2) at the flat base.
    secs = [0.0, win_hr * 3600.0]
    values = [round(base_m, 4), round(base_m, 4)]
    times = [SFINCS_TREF + _timedelta_s(s) for s in secs]
    inset_lon = 0.02 * (max_lon - min_lon)
    inset_lat = 0.02 * (max_lat - min_lat)
    mid_lon = 0.5 * (min_lon + max_lon)
    mid_lat = 0.5 * (min_lat + max_lat)
    edge_pts = [
        (min_lon + inset_lon, mid_lat),
        (max_lon - inset_lon, mid_lat),
        (mid_lon, min_lat + inset_lat),
        (mid_lon, max_lat - inset_lat),
    ]
    stations: list[StationHydrograph] = []
    series_by_id: dict[int, ReanchoredSeries] = {}
    for i, (lon, lat) in enumerate(edge_pts, start=1):
        stations.append(
            StationHydrograph(
                point_id=i, lon=float(lon), lat=float(lat), times=times,
                values=list(values), source_id=f"tide-base-{i}",
                provenance={"_prov_tide_base": True},
            )
        )
        series_by_id[i] = ReanchoredSeries(
            seconds=list(secs), datetimes=list(times), values=list(values)
        )
    stage = _staging_dir(None)
    csv_path = write_bzs_timeseries_csv(series_by_id, _unique(stage, "bzs", "csv"))
    loc_path = write_locations_fgb(stations, _unique(stage, "bnd", "fgb"))
    logger.info(
        "model_flood_scenario: synthesised FLAT %.2f m tide-base bzs boundary "
        "(spiderweb path; surge is generated by the spw wind+pressure) -> "
        "bzs=%s bnd=%s",
        base_m, csv_path, loc_path,
    )
    return {
        "timeseries_uri": csv_path,
        "locations_uri": loc_path,
        "_prov_tide_base_m": base_m,
    }


def _resolve_spiderweb_forcing(
    bbox: tuple[float, float, float, float],
    *,
    duration_hr: float,
    storm_name: str | None,
    storm_season: int | None,
    storm_track_uri: str | None,
    data_sources: list[DataSource] | None = None,
) -> tuple["SpiderwebForcing", dict[str, Any]]:
    """Resolve the IBTrACS track -> build the Holland spiderweb -> SpiderwebForcing.

    SPIDERWEB (2026-07-19). Two track sources:
    - ``storm_track_uri`` verbatim (a prior fetch_storm_tracks POINTS-FGB), OR
    - resolve via ``fetch_storm_tracks(bbox, start_year=storm_season,
      end_year=storm_season, storm_name=..., geometry="points")``.

    The FGB is staged to a local path (s3://gs:// via the sfincs_builder cache
    stager; local / file:// used directly), read into fix dicts, and handed to
    ``sfincs_spiderweb.build_spiderweb_from_fixes`` which writes the .spw and
    returns the utm zone + provenance (incl. which values were fallback).

    Runs SYNC (fetch_storm_tracks network I/O + geopandas read + Holland build)
    -> the caller MUST invoke it via ``asyncio.to_thread`` (no-loop-block norm).
    """
    import os as _os

    # fetch_storm_tracks FOLDED to a spec-driven surface (ADR 0111): resolve the promoted
    # router closure from the registry (keyword-only), the standard fold re-point.
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    fetch_storm_tracks = TOOL_REGISTRY["fetch_storm_tracks"].fn
    from trid3nt_server.agent.workflows.sfincs.sfincs_builder import _stage_gcs_local
    from trid3nt_server.agent.workflows.sfincs import sfincs_spiderweb as _spw

    # --- 1. resolve the track FGB uri ----------------------------------------
    if storm_track_uri:
        track_uri = storm_track_uri
    else:
        layer = fetch_storm_tracks(
            bbox=bbox,
            start_year=storm_season,
            end_year=storm_season,
            storm_name=storm_name,
            geometry="points",
        )
        track_uri = layer.uri
        if data_sources is not None:
            data_sources.append(
                DataSource(
                    name=(
                        f"IBTrACS best track ({storm_name or 'storm'} "
                        f"{storm_season or ''})".strip()
                    ),
                    uri=track_uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )

    # --- 2. stage to a readable local path -----------------------------------
    local_fgb = track_uri
    if local_fgb.startswith("file://"):
        local_fgb = local_fgb[len("file://"):]
    if local_fgb.startswith(("gs://", "s3://")):
        local_fgb = _stage_gcs_local(local_fgb)

    # --- 3. build the spiderweb ----------------------------------------------
    out_dir = _os.path.join(
        _staging_dir_local(), f"spw_{new_ulid()}"
    )
    result = _spw.build_spiderweb_from_fixes(
        _spw.read_ibtracs_fixes_from_fgb(local_fgb),
        bbox,
        out_dir=out_dir,
        deck_sim_hours=float(duration_hr),
        storm_name=storm_name,
    )
    member = SpiderwebForcing(
        spw_path=result.spw_path,
        utmzone=result.utmzone,
        spw_filename=result.spw_filename,
        provenance=dict(result.provenance),
    )
    prov = dict(result.provenance)
    prov["track_uri"] = track_uri
    prov["utm_epsg"] = result.utm_epsg
    return member, prov


def _staging_dir_local() -> str:
    """A local scratch dir for generated spw files (temp, per-process)."""
    import tempfile as _tf

    d = _os_path_join(_tf.gettempdir(), "trid3nt_spw")
    import os as _os

    _os.makedirs(d, exist_ok=True)
    return d


def _os_path_join(*parts: str) -> str:
    import os as _os

    return _os.path.join(*parts)


def _resolve_building_obstacle_uri(
    building_obstacles: bool | str,
    bbox: tuple[float, float, float, float],
    data_sources: list[DataSource],
) -> str | None:
    """Resolve the building-obstacle geofile URI for the SFINCS deck (best-effort).

    COASTAL SFINCS -- burn building footprints into the deck so the rough 2D
    flood routes around buildings. Three forms of ``building_obstacles``:

    - ``False`` / falsy → no obstacles (``None``).
    - a ``str`` → used verbatim as the footprint geofile URI (caller already has
      a FlatGeobuf / GeoJSON; e.g. a prior ``fetch_buildings`` output).
    - ``True`` → fetch OSM building footprints for ``bbox`` via the
      ``fetch_buildings`` atomic tool (OSM Overpass primary). This is
      BEST-EFFORT: any fetch failure logs + returns ``None`` (the flood proceeds
      WITHOUT obstacles, never aborts) -- same degrade policy as river geometry
      A successful fetch is recorded as a ``DataSource``.

    Returns the obstacle geofile URI, or ``None`` when there is nothing to burn.
    """
    if not building_obstacles:
        return None
    if isinstance(building_obstacles, str):
        return building_obstacles
    # building_obstacles is True → fetch OSM footprints (best-effort).
    try:
        from trid3nt_server.agent.tools import TOOL_REGISTRY  # local: keep top imports lean

        fetch_buildings = TOOL_REGISTRY["fetch_buildings"].fn
        # Keyword-only: the post-fold registry closure takes ZERO positional
        # args, so a positional bbox raised TypeError this except swallowed ->
        # SFINCS silently ran with no building obstacles (same defect class as
        # the SWMM composer's fetch calls).
        layer = fetch_buildings(bbox=bbox, source="osm")
        uri = getattr(layer, "uri", None)
        if uri:
            data_sources.append(
                DataSource(
                    name="OSM building footprints (Overpass — SFINCS obstacles)",
                    uri=uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )
        return uri
    except Exception as exc:  # noqa: BLE001 -- obstacles are optional for the flood
        logger.warning(
            "model_flood_scenario: fetch_buildings failed for bbox=%s (%s) — "
            "proceeding WITHOUT building obstacles (the flood still runs, just "
            "without footprint masking).",
            bbox,
            exc,
        )
        return None


# --------------------------------------------------------------------------- #
# -- SFINCS scenario-coverage composer auto-wiring
# (fluvial / compound / wind / infiltration / levee-breach / tsunami).
# Each helper mirrors the existing _autowire_coastal_surge_forcing /
# _resolve_building_obstacle_uri patterns: best-effort fetch + honest degrade
# per the data-source fallback norm, EXCEPT the breach + tsunami magnitude
# gates, which HARD-FAIL (never fabricate model inputs).
# --------------------------------------------------------------------------- #


def _autowire_river_discharge_forcing(
    bbox: tuple[float, float, float, float],
    *,
    duration_hr: float,
    data_sources: list[DataSource] | None = None,
    river_layer_uri: str | None = None,
) -> dict[str, Any] | None:
    """Auto-wire a FLUVIAL river-discharge boundary for a fluvial / compound run.

    the fluvial archetype. A ``river=True`` (or ``compound``)
    run needs a domain-EDGE river-inflow hydrograph driving the SFINCS ``dis``
    boundary. Unlike the coastal surge there is NO parametric last-resort synth
    (a fabricated discharge would violate Invariant 7), so the ladder degrades to
    SKIP -- the run proceeds pluvial-only when no real discharge is available.

    Degrade ladder (data-source fallback norm: primary -> fallback -> honest skip):

    1. PRIMARY  -  NOAA National Water Model (``fetch_noaa_nwm_streamflow``):
       CONUS, KEY-FREE, the canonical operational streamflow. Returns a point
       FlatGeobuf carrying ``streamflow_cms`` (m^3/s) -> handed back as
       ``{"discharge": {"fetch_uri": <uri>, "value_unit": "cms"}}`` so the
       EXISTING ``_resolve_surge_forcing_from_fetchers`` adapter materialises the
       dis files. ``rivers_uri`` (the already-fetched NHDPlus river layer) is
       threaded so ``setup_river_inflow`` gets inflow points.
    2. FALLBACK  -  USGS NWIS gauges (``fetch_usgs_nwis_gauges``): observed
       instrument-record hydrograph. NWIS discharge is in cfs (ft^3/s), so
       ``value_unit`` is set to ``"cfs"`` (the resolve converts; without it the
       series would be ~35.3x too large -- silent-wrong-physics).
    3. LAST-RESORT  -  SKIP: return ``None`` (the run proceeds pluvial-only). The
       composer logs the honest degrade; NO fabricated hydrograph.

    Returns a partial ``{"discharge": {...}}`` dict to merge into ``surge_forcing``
    BEFORE ``_resolve_surge_forcing_from_fetchers``, or ``None`` when neither
    source yields a hydrograph. Never raises (a fetcher exception logs + falls
    through to the next rung).
    """
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0

    # --- 1) PRIMARY: NOAA NWM streamflow (key-free, CONUS) ------------------ #
    try:
        from trid3nt_server.agent.tools.fetchers.hydrology.fetch_noaa_nwm_streamflow.fetch_noaa_nwm_streamflow import fetch_noaa_nwm_streamflow

        layer = fetch_noaa_nwm_streamflow(bbox)
        uri = getattr(layer, "uri", None)
        if uri:
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="NOAA NWM streamflow (auto-wired fluvial boundary)",
                        uri=str(uri),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )
            logger.info(
                "model_flood_scenario: auto-wired fluvial discharge via NOAA NWM "
                "streamflow for bbox=%s -> %s",
                bbox,
                uri,
            )
            return {
                "discharge": {
                    "fetch_uri": str(uri),
                    "value_unit": "cms",  # NWM streamflow_cms is m^3/s
                    "rivers_uri": river_layer_uri,
                }
            }
    except Exception as exc:  # noqa: BLE001  -  degrade to the next rung
        logger.warning(
            "model_flood_scenario: NOAA NWM auto-wire failed for bbox=%s (%s)  -  "
            "trying USGS NWIS fallback.",
            bbox,
            exc,
        )

    # --- 2) FALLBACK: USGS NWIS gauges (observed hydrograph, cfs) ----------- #
    try:
        import math as _math

        from trid3nt_server.agent.tools import TOOL_REGISTRY  # local: keep top imports lean

        fetch_usgs_nwis_gauges = TOOL_REGISTRY["fetch_usgs_nwis_gauges"].fn  # spec-driven (ADR 0085)
        period_days = max(1, int(_math.ceil(win_hr / 24.0)))
        layer = fetch_usgs_nwis_gauges(bbox=bbox, period=f"P{period_days}D")
        uri = getattr(layer, "uri", None)
        if uri:
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="USGS NWIS gauges (auto-wired fluvial boundary)",
                        uri=str(uri),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )
            logger.info(
                "model_flood_scenario: auto-wired fluvial discharge via USGS NWIS "
                "gauges for bbox=%s -> %s",
                bbox,
                uri,
            )
            return {
                "discharge": {
                    "fetch_uri": str(uri),
                    "value_unit": "cfs",  # NWIS discharge is ft^3/s
                    "rivers_uri": river_layer_uri,
                }
            }
    except Exception as exc:  # noqa: BLE001  -  degrade to the honest skip
        logger.warning(
            "model_flood_scenario: USGS NWIS auto-wire failed for bbox=%s (%s)  -  "
            "no fluvial discharge available; the run proceeds PLUVIAL-only.",
            bbox,
            exc,
        )

    # --- 3) LAST-RESORT: honest skip (no fabricated discharge) -------------- #
    logger.info(
        "model_flood_scenario: no fluvial discharge source for bbox=%s (NWM + "
        "NWIS both unavailable)  -  skipping the discharge boundary (pluvial-only).",
        bbox,
    )
    return None


def _resolve_infiltration_uri(
    infiltration: bool | str,
    bbox: tuple[float, float, float, float],
    data_sources: list[DataSource],
) -> str | None:
    """Resolve the GCN250 curve-number raster URI for the SFINCS infiltration loss.

    the infiltration archetype. Tri-state (mirrors
    ``building_obstacles``):

    - ``False`` / falsy -> no infiltration loss (``None``).
    - a ``str`` -> used verbatim as the CN raster URI (caller already has a
      single-band GCN250 GeoTIFF).
    - ``True`` -> BEST-EFFORT fetch of the GCN250 global SCS curve-number raster
      for ``bbox`` via ``fetch_gcn250_curve_numbers`` (key-free, global). A fetch
      failure logs + returns ``None`` (the flood proceeds WITHOUT an infiltration
      loss, never aborts -- same degrade policy as building obstacles).

    Returns the CN raster URI, or ``None`` when there is nothing to wire.
    """
    if not infiltration:
        return None
    if isinstance(infiltration, str):
        return infiltration
    # infiltration is True -> fetch the GCN250 CN raster (best-effort).
    try:
        from trid3nt_server.agent.tools import TOOL_REGISTRY

        layer = TOOL_REGISTRY["fetch_gcn250_curve_numbers"].fn(bbox, antecedent_moisture="average")
        uri = getattr(layer, "uri", None)
        if uri:
            data_sources.append(
                DataSource(
                    name="GCN250 SCS curve numbers (SFINCS infiltration loss)",
                    uri=str(uri),
                    accessed_at=datetime.now(timezone.utc),
                )
            )
        return uri
    except Exception as exc:  # noqa: BLE001 -- infiltration is optional for the flood
        logger.warning(
            "model_flood_scenario: fetch_gcn250_curve_numbers failed for bbox=%s "
            "(%s) — proceeding WITHOUT an infiltration loss (the flood still runs).",
            bbox,
            exc,
        )
        return None


def _synthesize_breach_discharge_forcing(
    breach_point: tuple[float, float],
    *,
    peak_m3s: float,
    arrival_hr: float | None,
    duration_hr: float,
) -> dict[str, Any]:
    """Synthesize an INTERIOR levee-breach discharge hydrograph -> dis files dict.

    the levee-breach archetype. The breach is an interior
    point-source ``dis`` jet (NOT a domain-edge river inflow), so it reuses the
    discharge seam with explicit ``locations`` at the drawn breach point and NO
    ``rivers_uri``/``hydrography_uri`` (the deck emits a SECOND
    ``setup_discharge_forcing(merge: true)`` with no ``setup_river_inflow``).

    HONESTY GATE (caller-enforced): the breach PEAK + LOCATION are USER inputs --
    the composer NEVER fabricates them. This synth only runs when the caller has
    already validated both are present (the magnitude gate fires upstream).

    Hydrograph: a triangular pulse rising from 0 to ``peak_m3s`` at
    ``arrival_hr`` (defaults to ~25% of the window) then receding linearly to a
    small residual by the window end. Materialised to a dis CSV + a 1-point
    locations FGB at ``breach_point`` via the SAME writers the fetcher adapter
    uses, so the deck consumes them unchanged.

    Returns ``{"timeseries_uri": <dis.csv>, "locations_uri": <src.fgb>, ...}`` --
    the pre-materialised discharge shape (carried onto ``ForcingSpec.breach``).
    """
    from trid3nt_server.agent.workflows.sfincs.sfincs_forcing_adapter import (
        SFINCS_TREF,
        ReanchoredSeries,
        StationHydrograph,
        _staging_dir,
        _unique,
        write_dis_timeseries_csv,
        write_locations_fgb,
    )

    lon, lat = float(breach_point[0]), float(breach_point[1])
    peak = float(peak_m3s)
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0
    # Time-to-peak (breach arrival). Default ~25% of the window so the jet builds
    # then drains within the run; clamp into (0, win_hr) so the triangle is valid.
    if arrival_hr is not None and arrival_hr > 0:
        t_peak_hr = min(float(arrival_hr), 0.95 * win_hr)
    else:
        t_peak_hr = 0.25 * win_hr
    t_peak_hr = max(t_peak_hr, 0.05 * win_hr)
    residual = max(0.0, 0.02 * peak)  # small non-zero tail (avoids a hard zero)

    # FINE sampling (~6 min) so the triangular rise/recession resolves on the
    # minute-scale output cadence (>= 2 samples for set_forcing_1d).
    _sample_s = 360.0
    n_steps = max(int(round(win_hr * 3600.0 / _sample_s)), 2)
    secs = [float(i) * (win_hr * 3600.0) / float(n_steps) for i in range(n_steps + 1)]
    values: list[float] = []
    for s in secs:
        hr = s / 3600.0
        if hr <= t_peak_hr:
            frac = hr / t_peak_hr if t_peak_hr > 0 else 1.0
            q = peak * frac
        else:
            # Linear recession from the peak to the residual by the window end.
            denom = max(win_hr - t_peak_hr, 1e-6)
            frac = (win_hr - hr) / denom
            frac = max(0.0, min(1.0, frac))
            q = residual + (peak - residual) * frac
        values.append(round(q, 4))

    times = [SFINCS_TREF + _timedelta_s(s) for s in secs]
    stations = [
        StationHydrograph(
            point_id=1,
            lon=lon,
            lat=lat,
            times=times,
            values=list(values),
            source_id="levee-breach-1",
            provenance={"_prov_breach": True},
        )
    ]
    series_by_id = {
        1: ReanchoredSeries(
            seconds=list(secs),
            datetimes=list(times),
            values=list(values),
        )
    }

    stage = _staging_dir(None)
    csv_path = write_dis_timeseries_csv(series_by_id, _unique(stage, "breach_dis", "csv"))
    loc_path = write_locations_fgb(stations, _unique(stage, "breach_src", "fgb"))
    logger.info(
        "model_flood_scenario: synthesised LEVEE-BREACH discharge hydrograph at "
        "(%.5f, %.5f): peak=%.1f m^3/s at %.1f hr, %d steps over %.0f hr "
        "-> dis=%s src=%s",
        lon,
        lat,
        peak,
        t_peak_hr,
        len(secs),
        win_hr,
        csv_path,
        loc_path,
    )
    return {
        "timeseries_uri": csv_path,
        "locations_uri": loc_path,
        "_prov_breach": True,
        "_prov_peak_m3s": peak,
        "_prov_arrival_hr": t_peak_hr,
    }


def _synthesize_tsunami_waterlevel_forcing(
    bbox: tuple[float, float, float, float],
    *,
    wave_height_m: float,
    period_min: float | None,
    duration_hr: float,
) -> dict[str, Any]:
    """Synthesize a TSUNAMI water-level boundary -> materialised bzs files dict.

    the tsunami archetype. Delegates the waveform GENERATION to
    the forcing adapter's ``synthesize_tsunami_bzs`` (a leading-depression N-wave
    -- trough THEN crest -- NOT the storm raised-cosine), driven onto the SAME
    seaward boundary points the surge synth uses. Reuses the ENTIRE existing
    waterlevel ``bzs`` deck seam (``setup_mask_bounds`` + ``setup_waterlevel_forcing``)
    with zero new deck code.

    HONESTY GATE (caller-enforced): the wave HEIGHT is a USER input -- the
    composer NEVER fabricates it. This synth only runs when the caller has already
    validated ``wave_height_m`` is present (the magnitude gate fires upstream).

    ``period_min`` defaults to ~15 min (a representative tsunami period) when the
    user did not supply it -- a SHAPE default, not a magnitude fabrication.

    Returns the materialised ``{"timeseries_uri": <bzs.csv>, "locations_uri":
    <bnd.fgb>, ...}`` dict (carried onto ``ForcingSpec.waterlevel``).
    """
    from trid3nt_server.agent.workflows.sfincs.sfincs_forcing_adapter import synthesize_tsunami_bzs

    period_s = float(period_min) * 60.0 if period_min and period_min > 0 else 15.0 * 60.0
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0
    out = synthesize_tsunami_bzs(
        bbox,
        eta_max_m=float(wave_height_m),
        period_s=period_s,
        wave_type="ldn",
        lead_depression=True,
        window_hours=win_hr,
    )
    logger.info(
        "model_flood_scenario: synthesised TSUNAMI N-wave bzs boundary for "
        "bbox=%s (height=%.2f m, period=%.0f s over %.0f hr) -> bzs=%s bnd=%s",
        bbox,
        float(wave_height_m),
        period_s,
        win_hr,
        out.get("timeseries_uri"),
        out.get("locations_uri"),
    )
    return out
