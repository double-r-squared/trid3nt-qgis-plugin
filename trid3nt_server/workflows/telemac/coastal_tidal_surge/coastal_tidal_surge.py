"""Engine template ``coastal_tidal_surge`` - TELEMAC-2D coastal tidal/surge
inundation.

The LLM-facing exposure of the coastal open-water TELEMAC-2D substrate: how far
does an OBSERVED or PREDICTED coastal water-level series flood a stretch of
coast. A regular UTM grid over a coastal bbox with real NOAA DEM_all topobathy at
the nodes, ONE seaward OPEN (liquid, free-surface-imposed) boundary edge driven
in TIME by a NOAA CO-OPS tide/surge series through the LIQUID BOUNDARIES FILE
(SL(1)); SAINT-VENANT + TIDAL FLATS wetting/drying floods the low coast as the
boundary stage rises. The discriminant (proof-norm #9): a storm-surge series
(``series_type="observed"``) floods far more land than the calm astronomical
tide (``series_type="prediction"``) over the SAME domain.

The tide series is fetched through the ROUTER (``fetch_noaa_coops_tides``) so the
emit-on-fetch seam surfaces the gauge as a role=context input; the
composer reads the station's inline ``time_series_csv``, re-bases it to t=0, and
authors the ``manifest['coastal']`` water-level series. The LOCAL-DOCKER solve
dispatches through the shared ``run_solver`` seam (solver=telemac_coastal, the
baked ``trid3nt-local/telemac:latest`` image); the postprocess rasterizes the
per-node peak WATER DEPTH to a 4326 COG and folds in the worker's flooded-LAND
km^2. The in-worker NOAA-sampled bed COG surfaces via the shared ``_bed_input``
helper.

Structural sibling of ``tomawac_wave_field`` / ``artemis_harbor_agitation`` (same
LOCAL-DOCKER solve seam, same run_solver dispatch, same publish_layer render
path): a registered engine TEMPLATE tagged ``engine="telemac", tier="template"``.
Determinism boundary (invariant 1): every number the agent narrates comes from
the typed ``TelemacCoastalLayerURI.peak_depth_m`` / ``.flooded_land_km2`` /
``.sl_peak_m`` fields the postprocess computed - never free-generated. The
``fallback_note`` carries the honesty floor (planning-grade inundation screening,
not a calibrated hindcast).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_COASTAL_DEPTH_STYLE_PRESET,
    TelemacCoastalLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.data import TOOL_REGISTRY, register_tool
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.workflows.telemac._template_card import TemplateCard
from trid3nt_server.workflows.telemac.postprocess_telemac import (
    PostprocessTelemacError,
    postprocess_coastal,
)
from trid3nt_server.workflows.telemac.run_telemac import TELEMAC_COASTAL_SOLVER_NAME

logger = logging.getLogger("trid3nt_server.workflows.telemac.coastal_tidal_surge")

__all__ = ["coastal_tidal_surge", "model_coastal_tidal_surge", "CoastalTidalSurgeError"]


class CoastalTidalSurgeError(RuntimeError):
    """Raised when the coastal tidal/surge chain fails fatally before a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: The two question classes this tool answers (one tool, two series types).
_SERIES_TYPES = ("observed", "prediction")

#: LOUD labeled demo defaults: the validated Apalachicola Bay / Hurricane Michael
#: case. Used only when the caller supplies neither an AOI nor a
#: window; narrated as demo defaults, never observations.
DEFAULT_STATION = "8728690"                      # Apalachicola, FL (CO-OPS)
DEFAULT_BBOX = (-85.02, 29.69, -84.90, 29.80)    # coastal strip spanning the shore
DEFAULT_START = "2018-10-09"                      # Hurricane Michael surge window
DEFAULT_END = "2018-10-11"
DEFAULT_RES_M = 180.0
DEFAULT_TIME_STEP_S = 20.0
#: MLLW tide datum -> DEM_all (~MSL) reconciliation; a LABELED knob, never invented.
DEFAULT_DATUM_OFFSET_M = 0.0


def _classify_series_type(text: str | None, explicit: str | None) -> str:
    """Pick observed-surge vs astronomical-prediction from an arg or prompt words."""
    if explicit and str(explicit).strip().lower() in _SERIES_TYPES:
        return str(explicit).strip().lower()
    t = (text or "").lower()
    if any(w in t for w in ("predict", "astronomical", "calm tide", "tide table",
                            "harmonic")):
        return "prediction"
    return "observed"


#: DECLARED target_resolution_m range. The coastal grid floor is
#: GRID_H_FLOOR_M (20 m in the worker); a wide bbox is coarsened under the node
#: budget (self-labeled). A screening inundation field gains nothing finer.
_COASTAL_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=20.0,
    native_hint="NOAA DEM_all topobathy (~30-90 m coastal) / grid node spacing",
    constraint_source="solver",
    rationale=(
        "target grid node spacing; the coastal grid floor is 20 m, a wide bbox is "
        "coarsened under the node budget (self-labeled); a planning-grade "
        "inundation screening field gains nothing finer than the topobathy"
    ),
)

_COASTAL_METADATA = AtomicToolMetadata(
    name="coastal_tidal_surge",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_COASTAL_RES_SPEC,),
)

TEMPLATE_CARD = TemplateCard(
    question=(
        "how far a coastal water-level series FLOODS the coast - drive a stretch of "
        "shoreline with an OBSERVED storm-surge tide-gauge record (or the calm "
        "astronomical PREDICTION) and map the peak inundation depth + newly-flooded "
        "land area; TELEMAC-2D open-water domain with one seaward liquid boundary "
        "forced by a NOAA CO-OPS series through the LIQUID BOUNDARIES FILE "
        "(SAINT-VENANT + TIDAL FLATS wetting/drying)"
    ),
    required_inputs=["location OR bbox (a coastal AOI spanning the shoreline)"],
    knobs=(
        "series_type (observed / prediction), station (CO-OPS id), start_date, "
        "end_date, datum_offset_m, ocean_edge (auto/N/S/E/W), target_resolution_m, "
        "duration_hours, time_step_s, bathy_source (noaa_demall / synthetic)"
    ),
)


@register_tool(
    _COASTAL_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def coastal_tidal_surge(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    series_type: str | None = None,
    station: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    datum_offset_m: float = DEFAULT_DATUM_OFFSET_M,
    ocean_edge: str = "auto",
    target_resolution_m: float | None = None,
    duration_hours: float | None = None,
    time_step_s: float = DEFAULT_TIME_STEP_S,
    bathy_source: str = "noaa_demall",
    compute_class: str = "medium",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> TelemacCoastalLayerURI | dict[str, Any]:
    """How far an OBSERVED or PREDICTED coastal water-level series FLOODS this coast.

    Fidelity: TELEMAC-2D shallow-water (SAINT-VENANT FE) with TIDAL FLATS
    wetting/drying over a real-topobathy coastal domain (NOAA DEM_all), ONE
    seaward liquid boundary driven in time by a NOAA CO-OPS tide/surge series
    through the LIQUID BOUNDARIES FILE (SL(1)). A planning-grade inundation
    SCREENING driven by observed gauge stage (the refinement complement to the
    SFINCS/SnapWave coastal screening on the fidelity ladder), not a calibrated
    hindcast.

    THE tool for "how far does the storm surge flood inland", "map the coastal
    inundation from this tide-gauge record", "which low land does the storm tide
    reach", "surge vs calm-tide flooded area at this coast". Answers TWO question
    classes via ``series_type``:

      - ``observed`` (default) - the OBSERVED water-level record (storm surge +
        tide) from the CO-OPS gauge floods the low coast.
      - ``prediction`` - the astronomical PREDICTION (calm tide, no surge) over the
        SAME domain; the A/B control that isolates the surge contribution.

    Do NOT use this for: a spectral WAVE-HEIGHT field (``tomawac_wave_field``);
    harbour agitation (``artemis_harbor_agitation``); a river dye/contaminant plume
    (``telemac_river_dye``); regional compound-flood screening (``sfincs_flood``).
    This tool returns a coastal INUNDATION-DEPTH field driven by a stage series.

    Params:
        location: a coastal place near the AOI (e.g. "Apalachicola, Florida").
            Supply this OR ``bbox`` - geocoded, never hand-typed coords.
        bbox: OPTIONAL explicit AOI ``(min_lon, min_lat, max_lon, max_lat)``
            EPSG:4326 spanning the shoreline (open water on one side, low land on
            the other). Wins over ``location`` when both are given for the domain.
        series_type: ``observed`` (storm-surge record, default) or ``prediction``
            (astronomical tide). Unset -> inferred from the prompt.
        station: OPTIONAL NOAA CO-OPS station id (e.g. "8728690"). Unset -> the
            in-bbox station nearest the AOI centre is used.
        start_date / end_date: ISO ``YYYY-MM-DD`` window for the gauge series.
            Unset -> a LOUD labeled demo window (the validated Hurricane Michael
            case) surfaced through the input-review gate.
        datum_offset_m: metres ADDED to every series value to reconcile the tide
            datum (MLLW) with the DEM datum (DEM_all ~ MSL). A labeled knob, never
            invented; default 0 (series used as-is).
        ocean_edge: seaward boundary edge - ``auto`` (deepest-mean bbox edge,
            default) or ``N`` / ``S`` / ``E`` / ``W``.
        target_resolution_m: OPTIONAL grid node spacing (m). Unset -> a labeled
            default (180 m). Floored at 20 m, coarsened under the node budget.
        duration_hours: OPTIONAL simulated window (h). Unset -> the series span.
        time_step_s: solver time step (s). Default 20.
        bathy_source: ``noaa_demall`` (real topobathy, default) or ``synthetic``
            (an analytic plane beach - deterministic offline path).
        compute_class: compute class. Default ``"medium"``.
        input_mode: ``"user_gated"`` reviews the resolved window/station/datum
            before the solve; ``"auto"`` (default) proceeds labeled.

    Returns:
        On success: ``TelemacCoastalLayerURI`` (``LayerURI`` subtype) - the emitter
        loads the peak-inundation-depth COG onto the map and the client animates
        the coastal SELAFIN mesh sibling. Carries ``peak_depth_m`` /
        ``flooded_land_km2`` / ``wet_area_km2`` / ``sl_peak_m`` / ``series_type``
        (narrate these typed numbers only - invariant 1) + a ``fallback_note``
        (screening honesty floor). On failure: dict with ``status="error"`` +
        ``error_code`` + ``error_message``.
    """
    coerced_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        cb = coerce_bbox_value(bbox)
        if cb is None:
            if isinstance(bbox, str) and any(c.isalpha() for c in bbox) \
                    and not (location and str(location).strip()):
                location, bbox = bbox, None
            else:
                return {
                    "status": "error",
                    "error_code": "COASTAL_PARAMS_INVALID",
                    "error_message": f"invalid bbox (need 4 numbers): {bbox!r}",
                }
        else:
            coerced_bbox = tuple(cb)  # type: ignore[assignment]

    has_loc = bool(location and str(location).strip())
    if not has_loc and coerced_bbox is None and not (station and str(station).strip()):
        # nothing anchors the AOI -> fall back to the LOUD labeled demo case.
        coerced_bbox = DEFAULT_BBOX
        station = station or DEFAULT_STATION
        start_date = start_date or DEFAULT_START
        end_date = end_date or DEFAULT_END
        logger.info("coastal_tidal_surge: no AOI/window given -> labeled demo case "
                    "(Apalachicola Bay / Hurricane Michael, CO-OPS %s)", DEFAULT_STATION)

    stype = _classify_series_type(location, series_type)
    try:
        time_step_s = max(1.0, float(time_step_s))
    except (TypeError, ValueError):
        time_step_s = DEFAULT_TIME_STEP_S
    if target_resolution_m is not None:
        try:
            target_resolution_m = max(20.0, float(target_resolution_m))
        except (TypeError, ValueError):
            target_resolution_m = None
    try:
        datum_offset_m = float(datum_offset_m)
    except (TypeError, ValueError):
        datum_offset_m = DEFAULT_DATUM_OFFSET_M

    logger.info(
        "coastal_tidal_surge location=%r bbox=%s series=%s station=%s window=%s..%s "
        "datum_off=%.2f ocean_edge=%s res=%s bathy=%s",
        location, coerced_bbox, stype, station, start_date, end_date,
        datum_offset_m, ocean_edge, target_resolution_m, bathy_source,
    )

    try:
        layer = await model_coastal_tidal_surge(
            location=location if has_loc else None,
            bbox=coerced_bbox,
            series_type=stype,
            station=str(station).strip() if station else None,
            start_date=start_date,
            end_date=end_date,
            datum_offset_m=datum_offset_m,
            ocean_edge=str(ocean_edge or "auto"),
            target_resolution_m=target_resolution_m,
            duration_hours=float(duration_hours) if duration_hours else None,
            time_step_s=time_step_s,
            bathy_source=str(bathy_source or "noaa_demall"),
            compute_class=compute_class,
            input_mode=input_mode,
        )
        logger.info(
            "coastal_tidal_surge complete layer_id=%s peak_depth=%.4g "
            "flooded_land=%.4g km^2 sl_peak=%s uri=%s",
            layer.layer_id, layer.peak_depth_m, layer.flooded_land_km2,
            layer.sl_peak_m, layer.uri,
        )
        return layer
    except asyncio.CancelledError:
        raise
    except (CoastalTidalSurgeError, PostprocessTelemacError) as exc:
        logger.warning("coastal_tidal_surge failed: %s (%s)",
                       getattr(exc, "error_code", "?"), exc)
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "COASTAL_RUN_FAILED"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("coastal_tidal_surge unexpected failure")
        return {
            "status": "error",
            "error_code": "COASTAL_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
def _bbox_center(bbox) -> tuple[float, float]:
    return (0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3]))


def _fetch_bbox(domain_bbox, station_hint: str | None) -> tuple[float, float, float, float]:
    """A gauge-fetch bbox = the domain bbox padded so a nearby station is captured
    (CO-OPS stations sit at the shoreline, sometimes just outside a tight strip)."""
    pad = 0.25
    return (round(domain_bbox[0] - pad, 4), round(domain_bbox[1] - pad, 4),
            round(domain_bbox[2] + pad, 4), round(domain_bbox[3] + pad, 4))


def _iso_to_epoch_s(iso: str) -> float | None:
    """Parse a CO-OPS ISO stamp ("YYYY-MM-DDTHH:MMZ" / "YYYY-MM-DD HH:MM") -> epoch s."""
    s = str(iso).strip()
    if not s:
        return None
    iso_norm = s.replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(iso_norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s.replace("Z", ""), fmt).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _read_station_series(
    fgb_uri: str, station_id: str | None, center: tuple[float, float]
) -> tuple[list[list[float]], dict[str, Any]]:
    """Download the CO-OPS FGB, pick the station (id else nearest to centre), and
    parse its inline ``time_series_csv`` -> ([[t_seconds_from_start, sl_m], ...],
    station_meta). Raises ``CoastalTidalSurgeError`` on any read/empty failure."""
    import geopandas as gpd
    from trid3nt_server.data.cache import read_object_bytes_s3

    tmp = tempfile.mkdtemp(prefix="coops-")
    local = os.path.join(tmp, "coops.fgb")
    try:
        data = read_object_bytes_s3(fgb_uri) if str(fgb_uri).startswith("s3://") else None
        if data is None:
            with open(str(fgb_uri), "rb") as fh:  # local path (test/offline)
                data = fh.read()
        with open(local, "wb") as fh:
            fh.write(data)
        gdf = gpd.read_file(local)
    except Exception as exc:  # noqa: BLE001
        raise CoastalTidalSurgeError(
            "COASTAL_TIDE_FETCH_FAILED",
            f"could not read the CO-OPS tide FGB {fgb_uri!r}: {exc}") from exc
    finally:
        try:
            Path(local).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    if len(gdf) == 0 or "time_series_csv" not in gdf.columns:
        raise CoastalTidalSurgeError(
            "COASTAL_TIDE_EMPTY",
            f"the CO-OPS fetch returned no station with a time series over the AOI "
            f"({fgb_uri!r}); widen the bbox or pick a documented station id.")

    def _col(row, *names):
        for n in names:
            if n in gdf.columns and row.get(n) is not None:
                return row.get(n)
        return None

    row = None
    if station_id:
        for _, r in gdf.iterrows():
            if str(_col(r, "station_id", "id") or "").strip() == str(station_id).strip():
                row = r
                break
        if row is None:
            avail = sorted({str(_col(r, "station_id", "id")) for _, r in gdf.iterrows()})
            raise CoastalTidalSurgeError(
                "COASTAL_STATION_NOT_FOUND",
                f"CO-OPS station {station_id!r} not among the in-bbox stations "
                f"{avail}; drop `station` to use the nearest, or widen the bbox.")
    else:
        # nearest station to the AOI centre.
        best_d = None
        for _, r in gdf.iterrows():
            try:
                geom = r.geometry
                d = (geom.x - center[0]) ** 2 + (geom.y - center[1]) ** 2
            except Exception:  # noqa: BLE001
                lon = _col(r, "lon"); lat = _col(r, "lat")
                if lon is None or lat is None:
                    continue
                d = (float(lon) - center[0]) ** 2 + (float(lat) - center[1]) ** 2
            if best_d is None or d < best_d:
                best_d, row = d, r

    raw_csv = row.get("time_series_csv")
    pairs: list[tuple[float, float]] = []
    for line in str(raw_csv or "").splitlines():
        line = line.strip()
        if not line or "," not in line:
            continue
        iso, _, val = line.partition(",")
        t = _iso_to_epoch_s(iso)
        try:
            v = float(val.strip())
        except (TypeError, ValueError):
            continue
        if t is not None and v == v:  # finite
            pairs.append((t, v))
    if len(pairs) < 2:
        raise CoastalTidalSurgeError(
            "COASTAL_TIDE_EMPTY",
            f"station {station_id or 'nearest'} carried < 2 finite time-series "
            f"points; pick a station/window with a real water-level record.")
    pairs.sort(key=lambda p: p[0])
    t0 = pairs[0][0]
    series = [[round(t - t0, 1), round(v, 4)] for t, v in pairs]
    meta = {
        "station_id": str(_col(row, "station_id", "id") or station_id or ""),
        "station_name": str(_col(row, "station_name", "name") or ""),
        "series_datum": str(_col(row, "datum") or "MLLW"),
        "n_points": len(series),
        "span_s": series[-1][0],
    }
    return series, meta


def _stage_coastal_manifest(coastal: dict[str, Any], run_tag: str) -> str:
    """Write the coastal ``coastal`` worker manifest to the cache bucket; return uri."""
    from trid3nt_server.data.simulation.solver.solver import _get_s3_client

    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise CoastalTidalSurgeError(
            "COASTAL_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage the coastal manifest.")
    manifest = {
        "coastal": coastal,
        "run_id": run_tag,
        "inputs": [],
        "telemac_args": [],
        "outputs": [
            "res_coastal.slf", "geo_coastal.slf", "bc_coastal.cli", "t2d_coastal.cas",
            "coastal_liquid_bnd.txt", "full_listing.log", "bed_bathymetry.tif",
            "telemac_metrics.json",
        ],
    }
    key = f"coastal/{run_tag}/manifest.json"
    _get_s3_client().put_object(
        Bucket=cache_bucket, Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


def _download_coastal_result(run_id: str) -> tuple[str, dict[str, Any]]:
    """Download ``res_coastal.slf`` + read telemac_metrics.json. Returns (path, metrics)."""
    from trid3nt_server.data.simulation.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()
    metrics: dict[str, Any] = {}
    try:
        obj = s3.get_object(Bucket=runs_bucket, Key=f"{run_id}/telemac_metrics.json")
        loaded = json.loads(obj["Body"].read().decode("utf-8"))
        if isinstance(loaded, dict):
            metrics = loaded
    except Exception as exc:  # noqa: BLE001
        logger.warning("coastal: metrics read failed for run %s: %s", run_id, exc)
    slf_key = f"{run_id}/res_coastal.slf"
    tmp_dir = tempfile.mkdtemp(prefix=f"coastal-{run_id}-")
    slf_path = str(Path(tmp_dir) / "res_coastal.slf")
    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=slf_key)
        with open(slf_path, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise CoastalTidalSurgeError(
            "COASTAL_OUTPUT_MISSING",
            f"coastal run {run_id} completed but s3://{runs_bucket}/{slf_key} "
            f"was not downloadable: {exc}") from exc
    if metrics.get("utm_epsg") is None:
        raise CoastalTidalSurgeError(
            "COASTAL_OUTPUT_MISSING",
            f"coastal run {run_id} produced no utm_epsg; cannot georeference.")
    return slf_path, metrics


async def model_coastal_tidal_surge(
    *,
    location: str | None,
    bbox: tuple[float, float, float, float] | None,
    series_type: str,
    station: str | None,
    start_date: str | None,
    end_date: str | None,
    datum_offset_m: float,
    ocean_edge: str,
    target_resolution_m: float | None,
    duration_hours: float | None,
    time_step_s: float,
    bathy_source: str,
    compute_class: str = "medium",
    input_mode: str | None = None,
    output_interval_min: float | None = None,
    pipeline_emitter: Any = None,
) -> TelemacCoastalLayerURI:
    """Compose place/AOI + a CO-OPS series -> coastal tidal/surge inundation layer.

    Resolves the coastal bbox, fetches the gauge series through the ROUTER
    (emit-on-fetch surfaces it as a context input), reads the station's
    ``time_series_csv`` -> the ``manifest['coastal']`` water-level series, stages
    the manifest, dispatches the generic run_solver seam (solver=telemac_coastal),
    downloads the result SELAFIN, and postprocesses the peak WATER DEPTH to a 4326
    COG. The in-worker NOAA bed COG surfaces via the shared ``_bed_input`` helper.
    """
    from trid3nt_server.emission.pipeline_emitter import (
        begin_substeps,
        current_emitter,
        mint_dispatch_and_sim_cards,
        route_sim_terminal,
        substep,
    )
    from trid3nt_server.gates.input_review import gate_input_review
    from trid3nt_server.data.publish_layer.publish_layer import publish_layer
    from trid3nt_server.data.simulation.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )
    from trid3nt_server.workflows.shared.solve_progress import (
        drive_live_solve_progress,
    )

    emitter = pipeline_emitter or current_emitter()
    is_synthetic = str(bathy_source).lower() == "synthetic"

    # --- Stage 1: resolve the coastal AOI ------------------------------------- #
    _planned = 4  # fetch_tides + run_solver + postprocess + publish
    if location:
        _planned += 1  # geocode
    begin_substeps(current_emitter(), _planned)

    location_name = location or "coast"
    if bbox is not None:
        aoi = tuple(float(v) for v in bbox)
    elif location:
        geocode_fn = TOOL_REGISTRY["geocode_location"].fn
        async with substep(current_emitter(), "geocode_location"):
            geo = await asyncio.to_thread(geocode_fn, location)
        clon = _geo_field(geo, ("lon", "longitude", "x"))
        clat = _geo_field(geo, ("lat", "latitude", "y"))
        if clon is None or clat is None:
            raise CoastalTidalSurgeError(
                "COASTAL_GEOCODE_FAILED",
                f"could not geocode {location!r} to a coastal AOI.")
        h = 0.06
        aoi = (round(clon - h, 4), round(clat - h, 4), round(clon + h, 4), round(clat + h, 4))
    else:
        raise CoastalTidalSurgeError(
            "COASTAL_PARAMS_INCOMPLETE",
            "coastal_tidal_surge needs a `location`, an explicit `bbox`, or a "
            "`station` (with the labeled demo window).")
    center = _bbox_center(aoi)

    win_start = start_date or DEFAULT_START
    win_end = end_date or DEFAULT_END
    window_default = not (start_date and end_date)

    # --- Stage 2: fetch the CO-OPS gauge series THROUGH the router ------------ #
    series: list[list[float]] = []
    station_meta: dict[str, Any] = {}
    if not is_synthetic:
        product = "predictions" if series_type == "prediction" else "water_level"
        fetch_bbox = _fetch_bbox(aoi, station)
        try:
            coops = TOOL_REGISTRY.get("fetch_noaa_coops_tides")
            if coops is None:
                raise CoastalTidalSurgeError(
                    "COASTAL_TIDE_FETCH_FAILED", "fetch_noaa_coops_tides is not registered.")
            async with substep(emitter, "fetch_noaa_coops_tides"):
                tide_layer = await asyncio.to_thread(
                    lambda: coops.fn(
                        bbox=list(fetch_bbox), start_date=win_start, end_date=win_end,
                        product=product, purpose="coastal tide/surge boundary"))
            tide_uri = getattr(tide_layer, "uri", None)
            if not tide_uri:
                raise CoastalTidalSurgeError(
                    "COASTAL_TIDE_EMPTY",
                    f"fetch_noaa_coops_tides returned no layer for bbox={fetch_bbox} "
                    f"window {win_start}..{win_end} product={product}.")
            series, station_meta = await asyncio.to_thread(
                _read_station_series, str(tide_uri), station, center)
        except CoastalTidalSurgeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CoastalTidalSurgeError(
                "COASTAL_TIDE_FETCH_FAILED",
                f"coastal tide-series fetch/parse failed: {exc}") from exc

    series_datum = station_meta.get("series_datum", "MLLW")
    duration_s = float(duration_hours) * 3600.0 if duration_hours else (
        float(series[-1][0]) if series else 108000.0)
    res_m = float(target_resolution_m) if target_resolution_m is not None else DEFAULT_RES_M

    # --- Input-review gate: window / station / datum are reviewable ----------- #
    _review_entries = [
        SyntheticInput(
            param="series_type", value=series_type, units=None,
            basis="user",
            note=("OBSERVED storm-surge record" if series_type == "observed"
                  else "astronomical PREDICTION (calm tide control)")),
        SyntheticInput(
            param="window", value=f"{win_start}..{win_end}", units=None,
            basis="default_demo" if window_default else "user", consequence="scenario",
            note="CO-OPS gauge series window (labeled Hurricane Michael demo default)"
            if window_default else "gauge series window"),
        SyntheticInput(
            param="station", value=str(station_meta.get("station_id") or station or "nearest"),
            units=None, basis="fetched" if station_meta else "user",
            note=str(station_meta.get("station_name") or "in-bbox CO-OPS gauge")),
        SyntheticInput(
            param="datum_offset_m", value=round(float(datum_offset_m), 3), units="m",
            basis="user" if datum_offset_m else "default_demo", consequence="physics",
            note=f"reconciles the {series_datum} tide datum to the DEM_all (~MSL) datum"),
        SyntheticInput(
            param="target_resolution_m", value=round(res_m, 0), units="m",
            basis="default_demo" if target_resolution_m is None else "user", consequence="numerical",
            note="coastal grid node spacing"),
    ]
    _review = await gate_input_review(
        tool_name="coastal_tidal_surge", mode=input_mode,
        entries=_review_entries,
        params={"datum_offset_m": float(datum_offset_m)})
    if _review.cancelled:
        raise CoastalTidalSurgeError("USER_INPUT_CANCELLED",
                                     f"coastal_tidal_surge {_review.cancel_reason}")
    datum_offset_m = float(_review.params.get("datum_offset_m", datum_offset_m))

    # --- Stage 3: stage the coastal manifest ---------------------------------- #
    coastal: dict[str, Any] = {
        "name": _slug(location_name),
        "bbox": [round(v, 4) for v in aoi],
        "bathy_source": "synthetic" if is_synthetic else "noaa_demall",
        "target_resolution_m": float(res_m),
        "ocean_edge": str(ocean_edge or "auto"),
        "series_datum": series_datum,
        "datum_offset_m": float(datum_offset_m),
        "duration_s": float(duration_s),
        "time_step_s": float(time_step_s),
    }
    # ADR 0283 cadence lever: threaded ONLY when set, so the manifest + solve stay
    # byte-identical (computed ~40-frame default) otherwise. INERT until the worker
    # image is rebuilt to parser coastal-tidal-2.
    if output_interval_min is not None:
        coastal["output_interval_min"] = float(output_interval_min)
    if series:
        coastal["water_level_series"] = series
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(_stage_coastal_manifest, coastal, run_tag)
    logger.info("model_coastal_tidal_surge staged manifest run_tag=%s series=%s "
                "station=%s pts=%s -> %s", run_tag, series_type,
                station_meta.get("station_id"), len(series), manifest_uri)

    # --- Stage 4: dispatch to the solver -------------------------------------- #
    handle = run_solver(
        solver=TELEMAC_COASTAL_SOLVER_NAME, model_setup_uri=manifest_uri,
        compute_class=compute_class)
    run_id = handle.run_id
    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=TELEMAC_COASTAL_SOLVER_NAME, handle=handle,
        compute_class=compute_class)
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))
    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(), run_id=run_id,
            solver=TELEMAC_COASTAL_SOLVER_NAME, grid_resolution_m=res_m,
            active_cell_count=None, vcpus=None, eta_seconds=None))
    run_result = None
    try:
        async with substep(emitter, "run_solver"):
            try:
                run_result = await wait_for_completion(handle, timeout_s=3600.0)
            finally:
                _progress_task.cancel()
                try:
                    await _progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                set_emitter_binding(None)
    finally:
        await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        raise CoastalTidalSurgeError(
            "COASTAL_RUN_FAILED",
            f"coastal tidal/surge solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}")

    # --- Stage 5: download + postprocess to the peak-depth COG ---------------- #
    batch_run_id = getattr(run_result, "run_id", None) or run_id
    slf_path, metrics = await asyncio.to_thread(_download_coastal_result, batch_run_id)
    utm_epsg = int(metrics["utm_epsg"])
    metrics["series_type"] = series_type
    metrics.setdefault("series_datum", series_datum)
    metrics.setdefault("datum_offset_m", float(datum_offset_m))
    if station_meta:
        metrics.setdefault("station_id", station_meta.get("station_id"))
        metrics.setdefault("station_name", station_meta.get("station_name"))
    reach = _slug(location_name)
    try:
        async with substep(emitter, "postprocess_coastal"):
            layers, _pmetrics = await asyncio.to_thread(
                postprocess_coastal, slf_path, run_id=batch_run_id,
                utm_epsg=utm_epsg, domain_bbox=aoi, reach_name=reach,
                worker_metrics=metrics)
    finally:
        try:
            Path(slf_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    if not layers:
        raise CoastalTidalSurgeError("COASTAL_NO_LAYERS",
                                     "postprocess_coastal produced no layer.")
    enriched = layers[0].model_copy(update={
        "mesh_resolution_label": (
            f"real NOAA DEM_all topobathy grid {metrics.get('dx_m', res_m):g} m"
            + (" (coarsened under node budget)" if metrics.get("coarsened") else "")),
    })

    from trid3nt_server.data.publish_layer.publish_layer import PublishLayerError

    async with substep(emitter, "publish_layer"):
        published = enriched
        if enriched.uri.startswith(("s3://", "gs://")):
            try:
                pub_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=enriched.uri,
                    layer_id=enriched.layer_id,
                    style_preset=enriched.style_preset or TELEMAC_COASTAL_DEPTH_STYLE_PRESET,
                )
                published = enriched.model_copy(update={"uri": pub_uri})
            except PublishLayerError as exc:
                logger.warning("coastal publish_layer failed (%s) - unpublished COG", exc)

    # EMIT-ON-SOLVE (ADR 0283): write outputs.json (the peak entry + the
    # res_coastal.slf mesh entry, crs_authid=EPSG:{utm}) and let the seam publish
    # the native rising-tide mesh animation. The typed peak stays composer-built
    # (frames_only). Best-effort -- peak-only on a miss. enriched carries the raw
    # s3 COG uri for the whole-run record.
    from trid3nt_server.workflows.telemac.results_mesh_seam import (
        publish_results_mesh_via_seam,
    )

    await publish_results_mesh_via_seam(
        emitter,
        run_id=batch_run_id,
        engine="telemac",
        peak_layer=enriched,
        peak_quantity="flood_depth",
        mesh_basename="res_coastal.slf",
        mesh_epsg=utm_epsg,
        reach_name=reach,
    )

    # in-worker bed input (S3): the NOAA DEM_all bed is sampled INSIDE the
    # solver container (no agent-side router fetch), so the composer rides the bed
    # COG the worker recorded through the shared helper. Best-effort; only the
    # real-bathy path writes one (metrics.bed_cog present).
    from trid3nt_server.workflows.telemac._bed_input import (
        surface_in_worker_bed_input,
    )
    await surface_in_worker_bed_input(
        emitter, run_metrics=metrics, run_id=batch_run_id,
        name=(f"Input: coastal bed bathymetry ({reach}, NOAA DEM_all topobathy, "
              f"in-worker)"),
        layer_id_prefix="input-coastal-bed")
    return published


def _geo_field(geo: Any, keys: tuple[str, ...]) -> float | None:
    if geo is None:
        return None
    for k in keys:
        v = getattr(geo, k, None)
        if v is None and isinstance(geo, dict):
            v = geo.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    for sub in ("center", "geometry", "location", "result"):
        nested = getattr(geo, sub, None) or (geo.get(sub) if isinstance(geo, dict) else None)
        if nested is not None:
            f = _geo_field(nested, keys)
            if f is not None:
                return f
    return None


def _slug(name: str) -> str:
    s = "".join(c if c.isalnum() else "_" for c in str(name or "coast").lower())
    return "_".join(p for p in s.split("_") if p)[:48] or "coast"
