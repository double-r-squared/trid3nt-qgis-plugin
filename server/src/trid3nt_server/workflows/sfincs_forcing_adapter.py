"""SFINCS forcing adapter — hydrographs → bzs/dis timeseries + bnd/src locations.

THE GAP THIS FILLS (COASTAL SFINCS North Star, Mexico Beach / Hurricane Michael):

The forcing FETCHERS produce per-station / per-reach hydrographs:

  * ``fetch_gtsm_tide_surge`` / ``fetch_noaa_coops_tides`` → a FlatGeobuf with
    one Point feature per gauge/station carrying an inline ``time_series_csv``
    attribute (``"iso,value"`` rows; water level in metres).
  * ``fetch_noaa_nwm_streamflow`` → a FlatGeobuf with one Point feature per
    NHDPlus reach carrying ``feature_id`` + ``streamflow_cms`` (a SINGLE
    instantaneous discharge value per reach, plus ``valid_time``).
  * ``fetch_cama_flood_discharge`` → a COG (time-mean discharge raster,
    m^3/s, EPSG:4326) — NOT a per-point hydrograph; sampled at points.

The DECK-EMISSION seam (``sfincs_builder._emit_surge_forcing_blocks``) consumes
FILE URIs:

  * ``WaterlevelForcing.timeseries_uri`` + ``locations_uri`` →
    ``setup_waterlevel_forcing(timeseries=..., locations=...)`` (``bzs``).
  * ``DischargeForcing.timeseries_uri`` + ``locations_uri`` (+ ``rivers_uri`` /
    ``hydrography_uri`` for ``setup_river_inflow``) →
    ``setup_discharge_forcing(timeseries=..., locations=...)`` (``dis``).

NOTHING materialised the hydrographs into those files. This module is that
adapter.

------------------------------------------------------------------------------
THE EXACT hydromt-sfincs 1.2.2 FORMAT CONTRACT (read from the installed source)
------------------------------------------------------------------------------

``setup_waterlevel_forcing`` / ``setup_discharge_forcing`` read the timeseries
CSV via::

    self.data_catalog.get_dataframe(timeseries, time_tuple=(tstart, tstop),
                                    parse_dates=True, index_col=0)
    df_ts.columns = df_ts.columns.map(int)   # column headers MUST cast to int

and the locations file via ``get_geodataframe(locations, ...)`` then, if it has
an ``index`` column, ``gdf_locs.set_index("index")``. The CSV columns and the
locations ``index`` must be the SAME integer set. ``set_forcing_1d`` then asserts
Point geometry, unique-integer columns, and >= 2 timesteps covering the model
window. Crucially, ``set_forcing_1d`` ALSO supports a NUMERIC index::

    if df_ts.index.is_numeric():
        df_ts.index = tref + pd.to_timedelta(df_ts.index, unit="sec")

i.e. **seconds-relative-to-tref** is a first-class index form. BUT the
``setup_*_forcing`` entry point reads the CSV with ``parse_dates=True``, which
would mangle bare integer seconds. So the FILE the deck consumes carries a
**datetime index** (ISO ``YYYY-MM-DD HH:MM:SS`` strings, anchored to the deck's
``tref``); this round-trips cleanly through ``parse_dates=True`` and is then
handed to ``set_forcing_1d`` as a real DatetimeIndex. We ALSO expose the
seconds-relative form (``timeseries_format="seconds"``) for callers who feed
``set_forcing_1d`` directly / build a custom catalog entry — see
``write_bzs_timeseries_csv``.

TIME RE-ANCHORING (load-bearing): the fetchers carry REAL event timestamps
(Hurricane Michael = Oct 2018). The deck's ``tref``/``tstart``/``tstop`` are a
synthetic ``20260101 000000`` window sized to ``simulation_hours``. If the CSV
timestamps don't overlap the deck window, ``get_dataframe(time_tuple=...)`` clips
to EMPTY and the forcing silently vanishes (Invariant 7). So the adapter
RE-ANCHORS every hydrograph: the first sample maps to ``tref`` and the relative
spacing is preserved, with the series clamped/extended to span the deck window.

------------------------------------------------------------------------------
PUBLIC API (all pure-ish: read fetcher bytes → write forcing files → URIs)
------------------------------------------------------------------------------

  * ``build_surge_forcing(...)`` — top-level convenience: takes the
    waterlevel / discharge fetcher ``LayerURI`` (or URI string) + the deck
    time window, materialises the bzs/dis files, and returns the nested
    ``surge_forcing`` dict shape ``model_flood_scenario(surge_forcing=...)``
    expects.
  * ``waterlevel_forcing_from_fgb(...)`` — GTSM/CO-OPS FGB → bzs CSV + bnd FGB
    → ``{"timeseries_uri", "locations_uri", "offset", "buffer_m"}``.
  * ``discharge_forcing_from_fgb(...)`` — NWM FGB → dis CSV + src FGB →
    ``{"timeseries_uri", "locations_uri", ...}``.
  * ``discharge_forcing_from_cama_cog(...)`` — CaMa-Flood COG → sampled src
    points → constant-discharge dis CSV + src FGB.

Lower-level (unit-tested directly):

  * ``parse_station_hydrographs_from_fgb`` — FGB bytes → list of
    ``StationHydrograph`` (point + times + values).
  * ``reanchor_to_tref`` — real timestamps → seconds-since-tref + datetime
    index anchored at the deck ``tref``.
  * ``write_bzs_timeseries_csv`` / ``write_locations_fgb`` — emit the files.

Invariants: deterministic (no LLM, no global state); typed errors
(``SFINCSForcingAdapterError`` with an A.6 open-set ``error_code``); never
silently emits an empty forcing (an empty station set / all-NaN series raises).
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("trid3nt_server.workflows.sfincs_forcing_adapter")

__all__ = [
    "SFINCSForcingAdapterError",
    "StationHydrograph",
    "ReanchoredSeries",
    "parse_station_hydrographs_from_fgb",
    "parse_discharge_points_from_fgb",
    "reanchor_to_tref",
    "write_bzs_timeseries_csv",
    "write_dis_timeseries_csv",
    "write_locations_fgb",
    "waterlevel_forcing_from_fgb",
    "discharge_forcing_from_fgb",
    "discharge_forcing_from_cama_cog",
    "synthesize_tsunami_bzs",
    "build_surge_forcing",
    "SFINCS_TREF",
    "SFINCS_TIME_FMT",
]


# --------------------------------------------------------------------------- #
# Constants — must match sfincs_builder's deck time format
# --------------------------------------------------------------------------- #

#: The deck reference time the YAML emitter pins (sfincs_builder
#: ``_generate_hydromt_yaml_config``: ``tref: "20260101 000000"``). All
#: re-anchored hydrographs map their first sample to this instant so the series
#: overlaps the deck's ``tstart``/``tstop`` window (else ``get_dataframe``'s
#: ``time_tuple`` clip empties the forcing — Invariant 7).
SFINCS_TREF: _dt.datetime = _dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)

#: Datetime-index format written into the bzs/dis CSV. ISO-ish
#: ``YYYY-MM-DD HH:MM:SS`` round-trips cleanly through pandas
#: ``read_csv(parse_dates=True)`` — the path ``setup_waterlevel_forcing`` /
#: ``setup_discharge_forcing`` take.
SFINCS_TIME_FMT: str = "%Y-%m-%d %H:%M:%S"

#: Minimum timesteps ``set_forcing_1d`` requires (it raises on < 2). When a
#: source carries a single instantaneous value (NWM streamflow, CaMa time-mean)
#: we synthesise a flat 2-point series spanning the deck window.
_MIN_TIMESTEPS: int = 2


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class SFINCSForcingAdapterError(RuntimeError):
    """Raised on any forcing-adapter failure.

    ``error_code`` is the A.6 open-set code the workflow surface lifts into a
    failed AssessmentEnvelope (same pattern as ``SFINCSSetupError``). Codes:

    - ``FORCING_FGB_READ_FAILED`` — the fetcher FlatGeobuf bytes/URI were
      unreadable.
    - ``FORCING_NO_STATIONS`` — the FGB carried no usable point features (empty
      or all-NaN hydrographs) → no boundary forcing is materialisable.
    - ``FORCING_SERIES_EMPTY`` — a station's hydrograph had < 1 finite sample.
    - ``FORCING_DEPS_UNAVAILABLE`` — geopandas / pandas / shapely / rasterio
      missing in the runtime.
    - ``FORCING_COG_READ_FAILED`` / ``FORCING_COG_EMPTY`` — CaMa-Flood COG read
      / sampling failed or had no valid cells.
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
# Typed intermediates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StationHydrograph:
    """One boundary point's hydrograph parsed from a fetcher FlatGeobuf.

    ``point_id`` is the integer SFINCS boundary index (assigned 1..N by the
    adapter — hydromt-sfincs wants unique integer ids matching the locations
    ``index`` column and the timeseries column headers). ``lon``/``lat`` are
    EPSG:4326. ``times`` are timezone-aware UTC datetimes; ``values`` are the
    metric quantity (water level m for bzs, discharge m^3/s for dis).
    """

    point_id: int
    lon: float
    lat: float
    times: list[_dt.datetime]
    values: list[float]
    source_id: str = ""  # the upstream gauge_id / station_id / feature_id
    provenance: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReanchoredSeries:
    """A hydrograph re-anchored to the deck ``tref`` window.

    ``seconds`` are seconds-since-tref (the SFINCS-native numeric index form);
    ``datetimes`` are the same instants as absolute UTC datetimes anchored at
    ``tref`` (the form written into the CSV for ``parse_dates=True``).
    """

    seconds: list[float]
    datetimes: list[_dt.datetime]
    values: list[float]


# --------------------------------------------------------------------------- #
# Dependency import (lazy + typed)
# --------------------------------------------------------------------------- #


def _import_geo():
    """Import geopandas/pandas/shapely or raise a typed adapter error."""
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
        import pandas as pd  # type: ignore[import-not-found]
        from shapely.geometry import Point  # type: ignore[import-not-found]

        return gpd, pd, Point
    except Exception as exc:  # noqa: BLE001
        raise SFINCSForcingAdapterError(
            "FORCING_DEPS_UNAVAILABLE",
            message=f"geopandas/pandas/shapely not available: {exc}",
        ) from exc


def _read_fetcher_bytes(uri_or_bytes: str | bytes) -> bytes:
    """Read a fetcher output as bytes from a URI (s3:///file:///local) or pass bytes.

    Mirrors sfincs_builder's scheme handling so the adapter consumes whatever
    the fetchers emit. Bytes are passed through (test path). GCP is
    decommissioned: ``s3://`` uses the shared boto3 reader (the job-0293c
    instance-role lesson — NOT s3fs/vsis3); local/``file://`` paths read
    directly.
    """
    if isinstance(uri_or_bytes, (bytes, bytearray)):
        return bytes(uri_or_bytes)
    uri = str(uri_or_bytes)
    try:
        if uri.startswith("s3://"):
            from ..tools.cache import read_object_bytes_s3

            return read_object_bytes_s3(uri)
        path = uri[len("file://"):] if uri.startswith("file://") else uri
        with open(path, "rb") as fh:
            return fh.read()
    except SFINCSForcingAdapterError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SFINCSForcingAdapterError(
            "FORCING_FGB_READ_FAILED",
            message=f"could not read fetcher output {uri!r}: {exc}",
            details={"uri": uri},
        ) from exc


def _parse_iso(ts: str) -> _dt.datetime | None:
    """Parse an ISO-8601 timestamp (the fetchers' ``time_series_csv`` format).

    Accepts the ``...Z`` and ``...T...`` forms the GTSM/CO-OPS fetchers emit, as
    well as a plain ``YYYY-MM-DD HH:MM:SS``. Returns a tz-aware UTC datetime, or
    ``None`` if unparseable (the caller drops the row).
    """
    s = ts.strip()
    if not s:
        return None
    s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
    s2 = s2.replace(" ", "T", 1) if ("T" not in s2 and " " in s2) else s2
    try:
        dt = _dt.datetime.fromisoformat(s2)
    except ValueError:
        # last-ditch: a few common explicit formats
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                dt = _dt.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def _parse_time_series_csv(csv_text: str) -> tuple[list[_dt.datetime], list[float]]:
    """Parse a fetcher ``time_series_csv`` attribute (``"iso,value"`` rows).

    Drops non-finite values + unparseable timestamps. Returns parallel
    ``(times, values)`` lists sorted ascending by time.
    """
    times: list[_dt.datetime] = []
    values: list[float] = []
    reader = csv.reader(io.StringIO(csv_text))
    for row in reader:
        if len(row) < 2:
            continue
        t = _parse_iso(row[0])
        if t is None:
            continue
        try:
            v = float(row[1])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(v):
            continue
        times.append(t)
        values.append(v)
    # Sort by time (CO-OPS / GTSM are already ascending, but be defensive).
    if times:
        order = sorted(range(len(times)), key=lambda i: times[i])
        times = [times[i] for i in order]
        values = [values[i] for i in order]
    return times, values


# --------------------------------------------------------------------------- #
# FlatGeobuf → StationHydrograph parsers
# --------------------------------------------------------------------------- #


def parse_station_hydrographs_from_fgb(
    fgb: str | bytes,
    *,
    timeseries_column: str = "time_series_csv",
) -> list[StationHydrograph]:
    """Parse GTSM / CO-OPS FlatGeobuf bytes/URI into per-station hydrographs.

    Both ``fetch_gtsm_tide_surge`` and ``fetch_noaa_coops_tides`` emit one Point
    feature per gauge/station carrying an inline ``time_series_csv`` attribute
    (``"iso,value"`` rows, water level in metres). This reads them all, assigns
    sequential integer ``point_id`` (1..N — the SFINCS bnd index contract), and
    returns the hydrographs.

    Raises:
        SFINCSForcingAdapterError("FORCING_FGB_READ_FAILED"): unreadable bytes.
        SFINCSForcingAdapterError("FORCING_NO_STATIONS"): no usable features.
    """
    gpd, _pd, _Point = _import_geo()
    raw = _read_fetcher_bytes(fgb)
    try:
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as fh:
            fh.write(raw)
            tmp = fh.name
        try:
            gdf = gpd.read_file(tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except SFINCSForcingAdapterError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SFINCSForcingAdapterError(
            "FORCING_FGB_READ_FAILED",
            message=f"geopandas could not read the forcing FlatGeobuf: {exc}",
        ) from exc

    if timeseries_column not in gdf.columns:
        raise SFINCSForcingAdapterError(
            "FORCING_FGB_READ_FAILED",
            message=(
                f"FlatGeobuf lacks the {timeseries_column!r} hydrograph column; "
                f"columns={list(gdf.columns)}"
            ),
        )

    # Prefer EPSG:4326 lon/lat for the canonical point coords; fall back to
    # geometry coords if a lon/lat attribute is absent.
    if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "WGS84"):
        try:
            gdf = gdf.to_crs("EPSG:4326")
        except Exception:  # noqa: BLE001 — keep whatever coords we have
            pass

    id_col = next(
        (c for c in ("gauge_id", "station_id", "feature_id", "id") if c in gdf.columns),
        None,
    )

    out: list[StationHydrograph] = []
    next_id = 1
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        # lon/lat: explicit attrs win (the fetchers carry them); else geometry.
        try:
            lon = float(row["lon"]) if "lon" in gdf.columns and row["lon"] is not None else float(geom.x)
            lat = float(row["lat"]) if "lat" in gdf.columns and row["lat"] is not None else float(geom.y)
        except (TypeError, ValueError):
            lon, lat = float(geom.x), float(geom.y)
        csv_text = row[timeseries_column]
        if not isinstance(csv_text, str) or not csv_text.strip():
            continue
        times, values = _parse_time_series_csv(csv_text)
        if len(values) < 1:
            continue
        out.append(
            StationHydrograph(
                point_id=next_id,
                lon=lon,
                lat=lat,
                times=times,
                values=values,
                source_id=str(row[id_col]) if id_col else f"pt-{next_id}",
            )
        )
        next_id += 1

    if not out:
        raise SFINCSForcingAdapterError(
            "FORCING_NO_STATIONS",
            message=(
                "the forcing FlatGeobuf carried no usable point hydrographs "
                "(empty / all-NaN time_series_csv) — no boundary forcing is "
                "materialisable (Invariant 7: refusing to emit an empty deck)"
            ),
        )
    logger.info(
        "sfincs_forcing_adapter: parsed %d station hydrograph(s) from FGB", len(out)
    )
    return out


def parse_discharge_points_from_fgb(
    fgb: str | bytes,
    *,
    timeseries_column: str = "time_series_csv",
    value_unit: str = "cms",
) -> list[StationHydrograph]:
    """Parse a discharge FlatGeobuf into per-point discharge hydrographs.

    Two source shapes are supported, in priority order:

    1.  REAL HYDROGRAPH (the J4 compound-flood driver): a Point feature carrying
        an inline ``time_series_csv`` (``"iso,value"`` rows) — what the windowed
        ``fetch_usgs_nwis_gauges`` (USGS NWIS IV time-window) and a short_range
        ``fetch_noaa_nwm_streamflow`` hydrograph emit. Each station becomes a
        FULL multi-sample ``StationHydrograph`` (the re-anchor step preserves the
        real series — NO flat-2-point flatten). ``value_unit="cfs"`` converts
        ft^3/s → m^3/s (SFINCS discharge is m^3/s); ``"cms"`` passes through.

    2.  SINGLE INSTANTANEOUS VALUE (the v0.1 fallback): a Point per NHDPlus reach
        carrying ``feature_id`` + ``streamflow_cms`` (m^3/s) + ``valid_time`` —
        the unwindowed ``fetch_noaa_nwm_streamflow`` shape. Each reach becomes a
        1-sample hydrograph at its ``valid_time``; the re-anchor step expands it
        to a flat 2-point series over the deck window (discharge held constant).
        This single-value behaviour is UNCHANGED.

    A row that carries a non-empty ``time_series_csv`` uses path 1; a row without
    one falls back to path 2 if ``streamflow_cms`` is present. A FlatGeobuf with
    NEITHER an inline series column NOR ``streamflow_cms`` is unreadable.

    Raises:
        SFINCSForcingAdapterError("FORCING_FGB_READ_FAILED" / "FORCING_NO_STATIONS").
    """
    gpd, _pd, _Point = _import_geo()
    raw = _read_fetcher_bytes(fgb)
    try:
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as fh:
            fh.write(raw)
            tmp = fh.name
        try:
            gdf = gpd.read_file(tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except SFINCSForcingAdapterError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SFINCSForcingAdapterError(
            "FORCING_FGB_READ_FAILED",
            message=f"geopandas could not read the NWM FlatGeobuf: {exc}",
        ) from exc

    has_series = timeseries_column in gdf.columns
    has_single = "streamflow_cms" in gdf.columns
    if not has_series and not has_single:
        raise SFINCSForcingAdapterError(
            "FORCING_FGB_READ_FAILED",
            message=(
                f"discharge FlatGeobuf lacks BOTH the {timeseries_column!r} "
                "hydrograph column and the 'streamflow_cms' single-value column; "
                f"columns={list(gdf.columns)}"
            ),
        )
    if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "WGS84"):
        try:
            gdf = gdf.to_crs("EPSG:4326")
        except Exception:  # noqa: BLE001
            pass

    # ft^3/s → m^3/s conversion when the source carries imperial discharge.
    cfs_to_cms = 0.028316846592
    scale = cfs_to_cms if str(value_unit).lower() in ("cfs", "ft3/s", "ft^3/s") else 1.0

    id_col = next(
        (c for c in ("feature_id", "site_no", "gauge_id", "id") if c in gdf.columns),
        None,
    )

    out: list[StationHydrograph] = []
    next_id = 1
    for _, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        lon, lat = float(geom.x), float(geom.y)

        # ---- Path 1: a REAL inline hydrograph (preserve the multi-point series).
        csv_text = row[timeseries_column] if has_series else None
        if isinstance(csv_text, str) and csv_text.strip():
            times, values = _parse_time_series_csv(csv_text)
            values = [v * scale for v in values]
            # Drop physically-impossible negatives (gauge artefacts).
            keep = [(t, v) for t, v in zip(times, values) if v >= 0]
            if len(keep) >= 1:
                times = [t for t, _v in keep]
                values = [v for _t, v in keep]
                out.append(
                    StationHydrograph(
                        point_id=next_id,
                        lon=lon,
                        lat=lat,
                        times=times,
                        values=values,
                        source_id=str(row[id_col]) if id_col else f"reach-{next_id}",
                    )
                )
                next_id += 1
                continue  # series path won this row

        # ---- Path 2: a single instantaneous value (the unchanged fallback).
        if not has_single:
            continue
        try:
            flow = float(row["streamflow_cms"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(flow) or flow < 0:
            continue
        valid = None
        if "valid_time" in gdf.columns and row["valid_time"]:
            valid = _parse_iso(str(row["valid_time"]))
        if valid is None:
            valid = SFINCS_TREF
        out.append(
            StationHydrograph(
                point_id=next_id,
                lon=lon,
                lat=lat,
                times=[valid],
                values=[flow],  # streamflow_cms is already m^3/s — no scale
                source_id=str(row[id_col]) if id_col else f"reach-{next_id}",
            )
        )
        next_id += 1

    if not out:
        raise SFINCSForcingAdapterError(
            "FORCING_NO_STATIONS",
            message=(
                "the discharge FlatGeobuf carried no usable reaches/stations "
                "(no finite non-negative discharge series or streamflow_cms) — "
                "no fluvial forcing is materialisable"
            ),
        )
    n_multi = sum(1 for s in out if len(s.values) > 1)
    logger.info(
        "sfincs_forcing_adapter: parsed %d discharge point(s) from FGB "
        "(%d with a real multi-point hydrograph)",
        len(out),
        n_multi,
    )
    return out


# --------------------------------------------------------------------------- #
# Time re-anchoring (real event time → deck tref window)
# --------------------------------------------------------------------------- #


def reanchor_to_tref(
    times: list[_dt.datetime],
    values: list[float],
    *,
    tref: _dt.datetime = SFINCS_TREF,
    window_hours: float | None = None,
) -> ReanchoredSeries:
    """Re-anchor a real-event hydrograph onto the deck ``tref`` window.

    The deck's ``tref``/``tstart``/``tstop`` are a synthetic window (sized to
    ``simulation_hours``); the fetcher timestamps are real event time (e.g.
    Oct 2018). ``get_dataframe(time_tuple=(tstart,tstop))`` clips the series to
    the deck window, so a mismatched series clips to EMPTY (the forcing silently
    vanishes — Invariant 7). We map the FIRST sample to ``tref`` and preserve the
    relative spacing of subsequent samples (so a 6-day surge stays a 6-day surge,
    just shifted to start at tref).

    For a single-sample input (NWM instantaneous discharge / CaMa time-mean) we
    synthesise a FLAT 2-point series spanning ``[tref, tref+window]`` so
    ``set_forcing_1d`` (which requires >= 2 points covering the window) accepts it
    and the discharge is held constant across the sim.

    Args:
        times: ascending tz-aware UTC datetimes (>= 1).
        values: parallel values (>= 1).
        tref: the deck reference time (default ``SFINCS_TREF``).
        window_hours: when set, a single-sample series is expanded to span this
            many hours from tref (the deck simulation length); also used to
            EXTEND a too-short multi-sample series with a final flat sample so it
            covers the whole deck window (no clip-to-empty at the tail). When
            ``None`` and only one sample exists, a 24-hour window is assumed.

    Returns:
        ``ReanchoredSeries`` with both the seconds-since-tref index and the
        absolute-datetime index anchored at ``tref``.

    Raises:
        SFINCSForcingAdapterError("FORCING_SERIES_EMPTY"): no samples.
    """
    if not times or not values or len(times) != len(values):
        raise SFINCSForcingAdapterError(
            "FORCING_SERIES_EMPTY",
            message="reanchor_to_tref requires >= 1 parallel (time, value) sample(s)",
        )

    if tref.tzinfo is None:
        tref = tref.replace(tzinfo=_dt.timezone.utc)

    # Single-sample → flat 2-point series spanning the deck window.
    if len(times) == 1:
        win_s = float((window_hours if window_hours is not None else 24.0) * 3600.0)
        win_s = max(win_s, 3600.0)  # at least an hour so the two points differ
        secs = [0.0, win_s]
        vals = [float(values[0]), float(values[0])]
        return ReanchoredSeries(
            seconds=secs,
            datetimes=[tref + _dt.timedelta(seconds=s) for s in secs],
            values=vals,
        )

    t0 = times[0]
    secs = [max(0.0, (t - t0).total_seconds()) for t in times]
    vals = [float(v) for v in values]

    # Extend the tail with a flat sample so the series spans the full deck window
    # (otherwise the deck's tstop can fall past the last sample and the tail
    # clips, leaving the boundary undriven late in the sim).
    if window_hours is not None:
        win_s = float(window_hours * 3600.0)
        if secs[-1] < win_s:
            secs.append(win_s)
            vals.append(vals[-1])

    return ReanchoredSeries(
        seconds=secs,
        datetimes=[tref + _dt.timedelta(seconds=s) for s in secs],
        values=vals,
    )


# --------------------------------------------------------------------------- #
# File writers (the bzs/dis CSV + bnd/src locations FGB the deck consumes)
# --------------------------------------------------------------------------- #


def _write_timeseries_csv(
    series_by_id: dict[int, ReanchoredSeries],
    out_path: str,
    *,
    timeseries_format: str = "datetime",
) -> str:
    """Write the wide bzs/dis timeseries CSV.

    Layout (the ``get_dataframe(parse_dates=True, index_col=0)`` contract):

        time,<id1>,<id2>,...
        2026-01-01 00:00:00,<v1>,<v2>,...
        ...

    ``timeseries_format``:
      * ``"datetime"`` (default) — index column holds absolute datetimes anchored
        at tref (round-trips through ``parse_dates=True``; the
        ``setup_*_forcing`` path).
      * ``"seconds"`` — index column holds seconds-since-tref integers (the
        ``set_forcing_1d`` numeric-index path; for direct callers / custom
        catalog entries).

    All ids share a common time axis: we UNION the per-id index instants, sort
    them, and forward/linearly fill each column at the union grid so the CSV is
    rectangular (hydromt reads it as one DataFrame). For the typical case (all
    stations share the GTSM/CO-OPS hourly grid, or all reaches share the flat
    2-point grid) the union IS the shared grid and no interpolation happens.
    """
    _gpd, pd, _Point = _import_geo()
    if not series_by_id:
        raise SFINCSForcingAdapterError(
            "FORCING_SERIES_EMPTY",
            message="no series to write to the timeseries CSV",
        )

    # Build a DataFrame per id keyed on seconds-since-tref, then outer-join.
    cols: dict[int, "pd.Series"] = {}
    for pid, s in series_by_id.items():
        cols[pid] = pd.Series(data=s.values, index=s.seconds)
    df = pd.DataFrame(cols)
    df = df.sort_index()
    # Fill any gaps from the outer join (mismatched grids): linear in the
    # interior, edge-fill at the ends so no NaN reaches SFINCS.
    df = df.interpolate(method="index", limit_direction="both")
    df = df.ffill().bfill().fillna(0.0)
    # Ensure unique INTEGER column headers (the df_ts.columns.map(int) contract).
    df.columns = [int(c) for c in df.columns]

    if timeseries_format == "seconds":
        df.index = [int(round(s)) for s in df.index]
        index_label = "time"
    else:
        df.index = [
            (SFINCS_TREF + _dt.timedelta(seconds=float(s))).strftime(SFINCS_TIME_FMT)
            for s in df.index
        ]
        index_label = "time"

    df.index.name = index_label
    df.to_csv(out_path, index=True)
    logger.info(
        "sfincs_forcing_adapter: wrote timeseries CSV %s (%d rows x %d cols, fmt=%s)",
        out_path,
        df.shape[0],
        df.shape[1],
        timeseries_format,
    )
    return out_path


def write_bzs_timeseries_csv(
    series_by_id: dict[int, ReanchoredSeries],
    out_path: str,
    *,
    timeseries_format: str = "datetime",
) -> str:
    """Write the water-level (``bzs``) timeseries CSV (metres). See ``_write_timeseries_csv``."""
    return _write_timeseries_csv(series_by_id, out_path, timeseries_format=timeseries_format)


def write_dis_timeseries_csv(
    series_by_id: dict[int, ReanchoredSeries],
    out_path: str,
    *,
    timeseries_format: str = "datetime",
) -> str:
    """Write the discharge (``dis``) timeseries CSV (m^3/s). See ``_write_timeseries_csv``."""
    return _write_timeseries_csv(series_by_id, out_path, timeseries_format=timeseries_format)


def write_locations_fgb(
    points: list[StationHydrograph],
    out_path: str,
) -> str:
    """Write the bnd/src locations FlatGeobuf the deck's ``locations`` arg reads.

    Layout (the ``get_geodataframe`` + ``set_index("index")`` contract):

        Point geometry (EPSG:4326) per boundary point + an integer ``index``
        column whose values are the same ``point_id`` set used as the timeseries
        CSV column headers. hydromt sets this as the index and matches it against
        ``df_ts.columns``.

    Geometry MUST be Point (``set_forcing_1d`` asserts it). CRS is EPSG:4326;
    ``setup_*_forcing`` reprojects to the model CRS itself.
    """
    gpd, _pd, Point = _import_geo()
    if not points:
        raise SFINCSForcingAdapterError(
            "FORCING_NO_STATIONS",
            message="no boundary points to write to the locations FlatGeobuf",
        )
    data = {
        "index": [int(p.point_id) for p in points],
        "source_id": [p.source_id for p in points],
        "geometry": [Point(p.lon, p.lat) for p in points],
    }
    gdf = gpd.GeoDataFrame(data, geometry="geometry", crs="EPSG:4326")
    gdf.to_file(out_path, driver="FlatGeobuf", engine="pyogrio")
    logger.info(
        "sfincs_forcing_adapter: wrote locations FGB %s (%d point(s))",
        out_path,
        len(points),
    )
    return out_path


# --------------------------------------------------------------------------- #
# Staging directory
# --------------------------------------------------------------------------- #


def _staging_dir(stage_dir: str | None) -> str:
    """Resolve (and create) the directory the forcing files are written into.

    ``stage_dir`` explicit override wins (tests pass a tmp_path). Otherwise a
    per-process subdir under the system temp. The deck's ``_stage_gcs_local``
    passes local paths through unchanged, so the files the adapter writes here
    are consumed directly by the YAML emitter.
    """
    if stage_dir:
        os.makedirs(stage_dir, exist_ok=True)
        return stage_dir
    d = os.path.join(tempfile.gettempdir(), "trid3nt-sfincs-forcing")
    os.makedirs(d, exist_ok=True)
    return d


def _unique(stage: str, prefix: str, ext: str) -> str:
    import uuid

    return os.path.join(stage, f"{prefix}-{uuid.uuid4().hex[:12]}.{ext}")


# --------------------------------------------------------------------------- #
# High-level: fetcher output → WaterlevelForcing / DischargeForcing dict
# --------------------------------------------------------------------------- #


def waterlevel_forcing_from_fgb(
    fgb: str | bytes,
    *,
    window_hours: float | None = None,
    offset: float | None = None,
    buffer_m: float | None = None,
    stage_dir: str | None = None,
    timeseries_format: str = "datetime",
    timeseries_column: str = "time_series_csv",
) -> dict[str, Any]:
    """GTSM / CO-OPS FlatGeobuf → SFINCS ``bzs`` files → ``WaterlevelForcing`` dict.

    Materialises the per-station water-level hydrographs into a wide bzs
    timeseries CSV (metres) + a bnd-points locations FGB, both staged to
    ``stage_dir``, and returns the dict shape ``model_flood_scenario``'s
    ``surge_forcing["waterlevel"]`` (and ``_build_surge_forcing_members``)
    expects::

        {"timeseries_uri": <bzs.csv>, "locations_uri": <bnd.fgb>,
         "offset": <m or None>, "buffer_m": <m or None>}

    Args:
        fgb: the ``fetch_gtsm_tide_surge`` / ``fetch_noaa_coops_tides``
            FlatGeobuf URI or raw bytes.
        window_hours: deck simulation length (hours). Used to extend the tail of
            the re-anchored series so it spans the whole deck window.
        offset: optional vertical-datum offset (m) passed verbatim to
            ``setup_waterlevel_forcing`` (e.g. MLLW→model datum).
        buffer_m: optional gauge-selection buffer (m) around the boundary cells.
        stage_dir: directory the bzs CSV + bnd FGB are written to (local; the
            deck consumes them directly). ``None`` → per-process temp dir.
        timeseries_format: ``"datetime"`` (default; the setup_*_forcing path) or
            ``"seconds"`` (the set_forcing_1d numeric path).
    """
    stations = parse_station_hydrographs_from_fgb(
        fgb, timeseries_column=timeseries_column
    )
    series_by_id: dict[int, ReanchoredSeries] = {}
    for st in stations:
        series_by_id[st.point_id] = reanchor_to_tref(
            st.times, st.values, window_hours=window_hours
        )
    stage = _staging_dir(stage_dir)
    csv_path = write_bzs_timeseries_csv(
        series_by_id, _unique(stage, "bzs", "csv"), timeseries_format=timeseries_format
    )
    loc_path = write_locations_fgb(stations, _unique(stage, "bnd", "fgb"))
    out: dict[str, Any] = {
        "timeseries_uri": csv_path,
        "locations_uri": loc_path,
    }
    if offset is not None:
        out["offset"] = float(offset)
    if buffer_m is not None:
        out["buffer_m"] = float(buffer_m)
    out["_prov_n_stations"] = len(stations)
    return out


def discharge_forcing_from_fgb(
    fgb: str | bytes,
    *,
    window_hours: float | None = None,
    rivers_uri: str | None = None,
    hydrography_uri: str | None = None,
    river_upa_km2: float | None = None,
    stage_dir: str | None = None,
    timeseries_format: str = "datetime",
    value_unit: str = "cms",
) -> dict[str, Any]:
    """NWM / NWIS discharge FlatGeobuf → SFINCS ``dis`` files → ``DischargeForcing``.

    Materialises per-reach/per-station discharge into a wide dis timeseries CSV
    (m^3/s) + a src-points locations FGB, and returns the dict shape
    ``surge_forcing["discharge"]`` expects::

        {"timeseries_uri": <dis.csv>, "locations_uri": <src.fgb>,
         "rivers_uri": ..., "hydrography_uri": ..., "river_upa_km2": ...}

    The parser PRESERVES a real multi-point hydrograph when the FGB carries an
    inline ``time_series_csv`` (the windowed-NWIS / short_range-NWM driver) —
    only a single-instantaneous-value source is flattened to a flat 2-point
    series. ``value_unit="cfs"`` converts ft^3/s → m^3/s for the USGS NWIS
    hydrograph (whose discharge is ft^3/s); ``"cms"`` (default) passes NWM's
    already-metric ``streamflow_cms`` through unchanged.

    NOTE on river inflow: ``setup_discharge_forcing`` attaches the series to the
    ``src`` points established by ``setup_river_inflow`` — which needs
    ``rivers``/``hydrography``. If neither is provided here, the deck emits
    ``setup_discharge_forcing`` with the explicit ``locations`` (the src FGB) and
    NO ``setup_river_inflow`` (the locations carry their own geometry). Pass
    ``rivers_uri`` / ``hydrography_uri`` to additionally drive the inflow-trim.
    """
    points = parse_discharge_points_from_fgb(fgb, value_unit=value_unit)
    series_by_id: dict[int, ReanchoredSeries] = {}
    for pt in points:
        series_by_id[pt.point_id] = reanchor_to_tref(
            pt.times, pt.values, window_hours=window_hours
        )
    stage = _staging_dir(stage_dir)
    csv_path = write_dis_timeseries_csv(
        series_by_id, _unique(stage, "dis", "csv"), timeseries_format=timeseries_format
    )
    loc_path = write_locations_fgb(points, _unique(stage, "src", "fgb"))
    out: dict[str, Any] = {
        "timeseries_uri": csv_path,
        "locations_uri": loc_path,
    }
    if rivers_uri is not None:
        out["rivers_uri"] = rivers_uri
    if hydrography_uri is not None:
        out["hydrography_uri"] = hydrography_uri
    if river_upa_km2 is not None:
        out["river_upa_km2"] = float(river_upa_km2)
    out["_prov_n_reaches"] = len(points)
    return out


def discharge_forcing_from_cama_cog(
    cog: str | bytes,
    bbox: tuple[float, float, float, float],
    *,
    window_hours: float | None = None,
    n_points: int = 1,
    stage_dir: str | None = None,
    timeseries_format: str = "datetime",
) -> dict[str, Any]:
    """CaMa-Flood time-mean discharge COG → sampled ``src`` points → ``dis`` files.

    Unlike NWM (point FGB), ``fetch_cama_flood_discharge`` emits a single-band
    time-mean discharge RASTER (m^3/s, EPSG:4326). We sample the cell(s) of
    LARGEST discharge inside ``bbox`` (the main-stem river entering the domain),
    materialise each as a constant-discharge src point (flat 2-point series over
    the deck window), and return the ``DischargeForcing`` dict.

    Args:
        cog: the CaMa COG URI or bytes.
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` — the model domain to
            sample within.
        n_points: how many highest-discharge cells to use as src points
            (default 1 — the dominant inflow).
    """
    try:
        import numpy as np  # type: ignore[import-not-found]
        import rasterio  # type: ignore[import-not-found]
        from rasterio.io import MemoryFile  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise SFINCSForcingAdapterError(
            "FORCING_DEPS_UNAVAILABLE",
            message=f"rasterio/numpy not available for CaMa COG sampling: {exc}",
        ) from exc

    raw = _read_fetcher_bytes(cog)
    try:
        with MemoryFile(raw) as mf:
            with mf.open() as src:
                arr = src.read(1).astype("float64")
                nodata = src.nodata
                transform = src.transform
                # row/col → lon/lat helpers via the affine transform
                from rasterio.transform import xy as _xy
    except SFINCSForcingAdapterError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SFINCSForcingAdapterError(
            "FORCING_COG_READ_FAILED",
            message=f"could not read the CaMa discharge COG: {exc}",
        ) from exc

    mask = np.isfinite(arr)
    if nodata is not None:
        mask &= arr != nodata
    mask &= arr >= 0.0
    if not mask.any():
        raise SFINCSForcingAdapterError(
            "FORCING_COG_EMPTY",
            message="CaMa discharge COG has no valid (finite, non-negative) cells",
        )

    flat = np.where(mask, arr, -np.inf)
    n = max(1, int(n_points))
    # Indices of the n largest discharge cells.
    idx_flat = np.argpartition(flat.ravel(), -n)[-n:]
    points: list[StationHydrograph] = []
    next_id = 1
    for fi in idx_flat:
        r, c = np.unravel_index(int(fi), arr.shape)
        v = float(arr[r, c])
        if not math.isfinite(v) or v < 0:
            continue
        lon, lat = _xy(transform, int(r), int(c), offset="center")
        points.append(
            StationHydrograph(
                point_id=next_id,
                lon=float(lon),
                lat=float(lat),
                times=[SFINCS_TREF],
                values=[v],
                source_id=f"cama-cell-{next_id}",
            )
        )
        next_id += 1

    if not points:
        raise SFINCSForcingAdapterError(
            "FORCING_COG_EMPTY",
            message="CaMa COG sampling produced no valid src points",
        )

    series_by_id: dict[int, ReanchoredSeries] = {
        pt.point_id: reanchor_to_tref(pt.times, pt.values, window_hours=window_hours)
        for pt in points
    }
    stage = _staging_dir(stage_dir)
    csv_path = write_dis_timeseries_csv(
        series_by_id, _unique(stage, "dis", "csv"), timeseries_format=timeseries_format
    )
    loc_path = write_locations_fgb(points, _unique(stage, "src", "fgb"))
    return {
        "timeseries_uri": csv_path,
        "locations_uri": loc_path,
        "_prov_n_cells": len(points),
        "_prov_source": "cama_flood_time_mean",
    }


# --------------------------------------------------------------------------- #
# Tsunami waveform synthesizer (NATE 2026-06-26)
# --------------------------------------------------------------------------- #


def synthesize_tsunami_bzs(
    bbox: tuple[float, float, float, float],
    *,
    eta_max_m: float,
    period_s: float,
    wave_type: str = "ldn",
    lead_depression: bool = True,
    window_hours: float | None = None,
    stage_dir: str | None = None,
    timeseries_format: str = "datetime",
) -> dict[str, Any]:
    """Synthesize a TSUNAMI water-level (``bzs``) timeseries on the waterlevel seam.

    NATE 2026-06-26: a pure synthesizer PARALLEL to
    ``model_flood_scenario._synthesize_parametric_surge_forcing`` -- it does NOT
    reuse the storm raised-cosine. Two waveforms, both anchored at ``tref`` and
    driven onto the SAME 4-edge boundary points the surge synth uses (HydroMT
    selects the seaward boundary cells; setup_mask_bounds btype=waterlevel makes
    them msk==2 so the bzs is not inert):

      * ``"ldn"`` (LEADING-DEPRESSION N-WAVE) -- the canonical tsunami signature:
        a derivative-of-Gaussian (trough THEN crest) so the sea first WITHDRAWS
        then surges. ``lead_depression=True`` puts the trough first (the physical
        leading-depression case); ``False`` flips to a leading-elevation N-wave.
      * ``"solitary"`` -- a single ``sech^2`` crest of half-width ~ ``period_s``
        (the classic solitary-wave bump), no leading trough.

    Returns the SAME dict shape ``waterlevel_forcing_from_fgb`` produces
    (``{"timeseries_uri": <bzs.csv>, "locations_uri": <bnd.fgb>, ...}``) so it
    flows verbatim through ``_build_surge_forcing_members`` -> a non-None
    ``WaterlevelForcing``. Files are written via the SAME
    ``write_bzs_timeseries_csv`` / ``write_locations_fgb`` writers the fetcher
    adapter uses, so the deck consumes them unchanged.

    Args:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)``.
        eta_max_m: peak surface elevation amplitude (m); also the trough depth.
        period_s: characteristic wave period (s) -- sets the pulse half-width.
        wave_type: ``"ldn"`` (default) or ``"solitary"``.
        lead_depression: for ``"ldn"``, trough-first (True) vs crest-first.
        window_hours: deck simulation length (h); the series spans this window so
            ``get_dataframe`` does not clip it (defaults to ~ 6 periods, min 1 h).
        stage_dir: directory the bzs CSV + bnd FGB are written to (local).
        timeseries_format: ``"datetime"`` (default) or ``"seconds"``.

    Raises:
        SFINCSForcingAdapterError("FORCING_SERIES_EMPTY"): non-finite / non-positive
            ``eta_max_m`` or ``period_s``.
    """
    eta = float(eta_max_m)
    T = float(period_s)
    if not math.isfinite(eta) or eta <= 0.0 or not math.isfinite(T) or T <= 0.0:
        raise SFINCSForcingAdapterError(
            "FORCING_SERIES_EMPTY",
            message=(
                "synthesize_tsunami_bzs requires finite positive eta_max_m + "
                f"period_s (got eta={eta_max_m!r}, period_s={period_s!r})"
            ),
        )

    wt = str(wave_type).strip().lower()
    # Window: cover several periods so the full pulse + return-to-rest is in the
    # deck window (and the deck's tstop never clips the tail to empty).
    if window_hours is not None and window_hours > 0:
        win_s = float(window_hours) * 3600.0
    else:
        win_s = max(3600.0, 6.0 * T)

    # Centre the pulse: leave a lead-in so the leading trough is fully sampled,
    # then place the crest about one period later.
    t0 = max(T, 0.15 * win_s)  # time of the primary feature (trough or crest)

    # FINE sampling so the wave shape (period_s) resolves; >= 2 samples.
    _sample_s = max(5.0, T / 40.0)
    n_steps = max(int(round(win_s / _sample_s)), 2)
    secs = [float(i) * win_s / float(n_steps) for i in range(n_steps + 1)]

    values: list[float] = []
    for s in secs:
        if wt == "solitary":
            # Single sech^2 crest of half-width ~ period_s, centred at t0.
            # sech(x) = 1/cosh(x); width scaled so the bump spans ~ one period.
            x = (s - t0) / (T / 3.5)
            sech = 1.0 / math.cosh(max(-50.0, min(50.0, x)))
            eta_t = eta * sech * sech
        else:
            # Leading-depression N-wave: derivative-of-Gaussian (trough -> crest).
            # f(t) = A * (t-t0)/sigma * exp(-((t-t0)/sigma)^2): at u<0 (t<t0)
            # the term u*exp(-u^2) is NEGATIVE -> a TROUGH first, then a crest
            # after t0 (the leading-depression signature). The extremum amplitude
            # of u*exp(-u^2) is 1/sqrt(2e), so scale to hit eta.
            sigma = T / 2.0
            u = (s - t0) / sigma
            shape = u * math.exp(-(u * u))  # trough (neg, t<t0) then crest (pos)
            norm = 1.0 / math.sqrt(2.0 * math.e)  # |extremum| of u*exp(-u^2)
            eta_t = eta * (shape / norm)
            if not lead_depression:
                eta_t = -eta_t  # flip to leading-elevation (crest first)
        values.append(round(eta_t, 5))

    min_lon, min_lat, max_lon, max_lat = bbox
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

    times = [SFINCS_TREF + _dt.timedelta(seconds=s) for s in secs]
    stations: list[StationHydrograph] = []
    series_by_id: dict[int, ReanchoredSeries] = {}
    for i, (lon, lat) in enumerate(edge_pts, start=1):
        stations.append(
            StationHydrograph(
                point_id=i,
                lon=float(lon),
                lat=float(lat),
                times=list(times),
                values=list(values),
                source_id=f"tsunami-{wt}-{i}",
                provenance={"_prov_tsunami": True, "_prov_wave_type": wt},
            )
        )
        series_by_id[i] = ReanchoredSeries(
            seconds=list(secs),
            datetimes=list(times),
            values=list(values),
        )

    stage = _staging_dir(stage_dir)
    csv_path = write_bzs_timeseries_csv(
        series_by_id, _unique(stage, "bzs", "csv"), timeseries_format=timeseries_format
    )
    loc_path = write_locations_fgb(stations, _unique(stage, "bnd", "fgb"))
    return {
        "timeseries_uri": csv_path,
        "locations_uri": loc_path,
        "_prov_tsunami": True,
        "_prov_wave_type": wt,
        "_prov_eta_max_m": eta,
        "_prov_period_s": T,
        "_prov_lead_depression": bool(lead_depression),
    }


# --------------------------------------------------------------------------- #
# Top-level convenience — fetcher LayerURIs → surge_forcing dict
# --------------------------------------------------------------------------- #


def build_surge_forcing(
    *,
    waterlevel_fgb: str | bytes | None = None,
    discharge_fgb: str | bytes | None = None,
    cama_cog: str | bytes | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    window_hours: float | None = None,
    waterlevel_offset: float | None = None,
    waterlevel_buffer_m: float | None = None,
    rivers_uri: str | None = None,
    hydrography_uri: str | None = None,
    river_upa_km2: float | None = None,
    discharge_value_unit: str = "cms",
    wind: dict[str, Any] | None = None,
    pressure: dict[str, Any] | None = None,
    stage_dir: str | None = None,
    timeseries_format: str = "datetime",
) -> dict[str, Any]:
    """Assemble the ``surge_forcing`` dict ``model_flood_scenario`` consumes.

    Given any combination of a water-level fetcher FGB (GTSM / CO-OPS), a
    discharge fetcher FGB (NWM) and/or a CaMa COG, materialise the bzs/dis files
    and return::

        {"waterlevel": {...} | absent,
         "discharge":  {...} | absent,
         "wind":       {...} | absent,   # passed through verbatim
         "pressure":   {...} | absent}   # passed through verbatim

    ``model_flood_scenario(surge_forcing=<this>)`` →
    ``_build_surge_forcing_members`` → ``ForcingSpec`` → the deck. This is the
    single seam a caller invokes to turn fetched surge data into a deck-ready
    forcing spec. A ``LayerURI`` may be passed directly for any *_fgb / cog arg
    — its ``.uri`` is read automatically.

    At least one forcing source must be provided, else ``ValueError`` (an empty
    surge_forcing is a caller bug — use ``None`` for a pure-pluvial flood).
    """
    def _coerce_uri(x: Any) -> Any:
        # Accept a LayerURI (has .uri) or a raw URI/bytes.
        uri = getattr(x, "uri", None)
        return uri if uri is not None else x

    if waterlevel_fgb is None and discharge_fgb is None and cama_cog is None and not wind and not pressure:
        raise ValueError(
            "build_surge_forcing requires at least one forcing source "
            "(waterlevel_fgb / discharge_fgb / cama_cog / wind / pressure)"
        )

    surge: dict[str, Any] = {}

    if waterlevel_fgb is not None:
        surge["waterlevel"] = waterlevel_forcing_from_fgb(
            _coerce_uri(waterlevel_fgb),
            window_hours=window_hours,
            offset=waterlevel_offset,
            buffer_m=waterlevel_buffer_m,
            stage_dir=stage_dir,
            timeseries_format=timeseries_format,
        )

    if discharge_fgb is not None:
        surge["discharge"] = discharge_forcing_from_fgb(
            _coerce_uri(discharge_fgb),
            window_hours=window_hours,
            rivers_uri=rivers_uri,
            hydrography_uri=hydrography_uri,
            river_upa_km2=river_upa_km2,
            value_unit=discharge_value_unit,
            stage_dir=stage_dir,
            timeseries_format=timeseries_format,
        )
    elif cama_cog is not None:
        if bbox is None:
            raise ValueError("cama_cog requires bbox to sample the domain")
        surge["discharge"] = discharge_forcing_from_cama_cog(
            _coerce_uri(cama_cog),
            bbox,
            window_hours=window_hours,
            stage_dir=stage_dir,
            timeseries_format=timeseries_format,
        )

    if wind:
        surge["wind"] = dict(wind)
    if pressure:
        surge["pressure"] = dict(pressure)

    return surge
