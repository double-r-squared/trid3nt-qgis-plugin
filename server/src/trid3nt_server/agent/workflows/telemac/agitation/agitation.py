"""Engine template ``artemis_harbor_agitation`` - ARTEMIS phase-resolving
elliptic mild-slope (Berkhoff) wave-agitation engine (ADR 0237).

The LLM-facing exposure of TELEMAC's ARTEMIS harbour-agitation solver: the
phase-RESOLVING refinement-grade complement to TOMAWAC's phase-averaged spectral
tier (ADR 0236) on the fidelity ladder. ONE question-class tool with THREE modes
(the board's six ARTEMIS rows collapse to three distinct question classes):

  * ``diffraction`` - a breakwater / structure shelters a berthing area; the
                      diffracted wave in the lee is much smaller than the exposed
                      approach (Sommerfeld/Penny-Price). The proof-norm-#9 pair is
                      the sheltered zone behind the breakwater vs the exposed
                      approach in front of it.
  * ``resonance``   - incoming swell amplifies inside a narrow-mouth harbour at
                      the seiche periods; the response spikes AT a resonant period
                      and is quiet OFF resonance.
  * ``shoal``       - a nearshore reef/shoal refracts + focuses waves (the exact
                      Berkhoff-Booij-Radder 1982 elliptic shoal); a focus peak
                      Kd~2.2 forms down-wave.

Two bathymetry paths (see the worker ``artemis_build``):
  * ``noaa_greatlakes`` - a REAL US Great Lakes harbour AOI, bed sampled from the
    NOAA NGDC lake-datum bathymetry (Superior/Michigan/Huron/Erie/Ontario), with a
    schematic breakwater as a thin solid barrier (diffraction mode).
  * ``idealized``       - the geography-free analytic domains the sandbox proved
    (replicates the classic ARTEMIS validation set; clears the citations law like
    the GWE analytic V&V).

Structural sibling of ``tomawac_wave_field`` (same LOCAL-DOCKER solve seam, same
run_solver dispatch, same publish_layer render path): a registered engine TEMPLATE
tagged ``engine="telemac", tier="template"``. Determinism boundary (invariant 1):
every agitation number the agent narrates comes from the typed
``ArtemisAgitationLayerURI`` fields the postprocess/worker computed - never
free-generated. The ``fallback_note`` carries the honesty floor (phase-resolving
screening, not a calibrated hindcast).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.telemac_contracts import (
    TELEMAC_AGITATION_STYLE_PRESET,
    ArtemisAgitationLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata, ResolutionSpec

from trid3nt_server.agent.tools import TOOL_REGISTRY, register_tool
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.workflows.telemac._template_card import TemplateCard
from trid3nt_server.agent.workflows.telemac.postprocess_telemac import (
    PostprocessTelemacError,
    postprocess_artemis,
)
from trid3nt_server.agent.workflows.telemac.run_telemac import ARTEMIS_SOLVER_NAME

logger = logging.getLogger("trid3nt_server.agent.workflows.telemac.agitation")

__all__ = ["artemis_harbor_agitation", "model_artemis_harbor_agitation",
           "ArtemisAgitationError"]


class ArtemisAgitationError(RuntimeError):
    """Raised when the ARTEMIS agitation chain fails fatally before a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: The three question classes this tool covers (one tool, three modes).
_AGITATION_MODES = ("diffraction", "resonance", "shoal")

#: Rough lon/lat bboxes of the five Great Lakes open water (auto real-bathy gate).
_GREAT_LAKES: dict[str, tuple[float, float, float, float]] = {
    "superior": (-92.2, 46.4, -84.3, 49.1),
    "michigan": (-88.1, 41.6, -84.7, 46.1),
    "huron": (-84.8, 43.0, -79.7, 46.3),
    "erie": (-83.5, 41.3, -78.8, 42.9),
    "ontario": (-79.9, 43.2, -76.0, 44.3),
}

#: LOUD labeled demo defaults (no wave-forcing fetcher exists yet): a prescribed
#: monochromatic incident wave. These are narrated demo defaults, never observations.
DEFAULT_WAVE_PERIOD_S = 8.0
DEFAULT_WAVE_DIR_DEG = 90.0           # trig convention: 0=+X east, 90=+Y north
DEFAULT_WAVE_HEIGHT_M = 1.0
DEFAULT_REFLECTION_COEF = 1.0         # fully-reflecting quay wall / breakwater
DEFAULT_REAL_RES_M = 40.0
DEFAULT_IDEALIZED_RES_M = 8.0


def _great_lake_for(lon: float, lat: float) -> str | None:
    for name, (x0, y0, x1, y1) in _GREAT_LAKES.items():
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            return name
    return None


def _classify_mode(text: str | None, explicit: str | None) -> str:
    """Pick the agitation question class from an explicit arg or prompt keywords."""
    if explicit and str(explicit).strip().lower() in _AGITATION_MODES:
        return str(explicit).strip().lower()
    t = (text or "").lower()
    if any(w in t for w in ("resonance", "resonan", "seiche", "standing wave",
                            "amplif", "basin oscillation")):
        return "resonance"
    if any(w in t for w in ("shoal", "reef", "focus", "refract")):
        return "shoal"
    return "diffraction"


#: DECLARED target_resolution_m range (ADR 0225). The elliptic solve is heavier
#: than TOMAWAC's spectral march, so a tighter node budget: the finest the real
#: grid authors is ~20 m (GRID_H_FLOOR_M in the worker); a large AOI coarsens.
_ARTEMIS_RES_SPEC = ResolutionSpec(
    param="target_resolution_m",
    unit="m",
    min_value=20.0,
    native_hint="NOAA Great Lakes lake-datum bathymetry (~90 m) / analytic grid",
    constraint_source="solver",
    rationale=(
        "target grid node spacing; the ARTEMIS elliptic mild-slope solve is "
        "heavier than a spectral march, so GRID_H_FLOOR_M=20 m is the finest the "
        "real grid authors and a large AOI is coarsened under GRID_NODE_CAP "
        "(self-labeled); a phase-resolving screening field gains little finer"
    ),
)

_ARTEMIS_METADATA = AtomicToolMetadata(
    name="artemis_harbor_agitation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="telemac",
    tier="template",
    resolution_specs=(_ARTEMIS_RES_SPEC,),
)

TEMPLATE_CARD = TemplateCard(
    question=(
        "the WAVE AGITATION (how much incident swell is amplified or sheltered, "
        "agitation coefficient Kd=Hs/H0) inside a harbour or around a coastal "
        "structure - breakwater DIFFRACTION sheltering a berthing area, harbour "
        "RESONANCE amplifying swell at the seiche periods, or reef/SHOAL "
        "refraction-focusing; ARTEMIS phase-resolving elliptic mild-slope "
        "(Berkhoff) solver (refinement-grade, the phase-resolving complement to "
        "the TOMAWAC spectral tier)"
    ),
    required_inputs=["location OR bbox (a harbour / coastal AOI)"],
    knobs=(
        "wave_mode (diffraction / resonance / shoal), wave_period_s, "
        "wave_direction_deg, wave_height_m, reflection_coef, breakwater "
        "endpoints, target_resolution_m, bathy_source (noaa_greatlakes / idealized)"
    ),
)


@register_tool(
    _ARTEMIS_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def artemis_harbor_agitation(
    location: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    wave_mode: str | None = None,
    wave_period_s: float = DEFAULT_WAVE_PERIOD_S,
    wave_direction_deg: float = DEFAULT_WAVE_DIR_DEG,
    wave_height_m: float = DEFAULT_WAVE_HEIGHT_M,
    reflection_coef: float = DEFAULT_REFLECTION_COEF,
    breakwater: tuple[float, float, float, float] | list[float] | None = None,
    target_resolution_m: float | None = None,
    bathy_source: str = "auto",
    compute_class: str = "medium",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> ArtemisAgitationLayerURI | dict[str, Any]:
    """The WAVE AGITATION (Kd = Hs/H0) inside a harbour or around a coastal structure.

    Fidelity: ARTEMIS phase-RESOLVING elliptic mild-slope (Berkhoff) wave solver -
    steady-state diffraction, refraction, and partial reflection where PHASE
    (resonance, standing waves, diffraction fringes) is the answer. Refinement-grade
    harbour-agitation physics (the phase-resolving complement to the TOMAWAC
    phase-averaged spectral tier / SFINCS-SnapWave screening). A planning-grade demo
    driven by a PRESCRIBED monochromatic incident wave (no wave-forcing fetcher yet),
    not a calibrated hindcast.

    THE tool for "how much does swell amplify inside this harbour", "wave agitation
    / tranquility in the basin", "does this breakwater shelter the berths", "harbour
    resonance / seiche", "diffraction behind a breakwater", "reef/shoal wave
    sheltering / focusing". Answers THREE question classes via ``wave_mode``:

      - ``diffraction`` (default) - a breakwater shelters a berthing area; the lee
        agitation is far below the exposed approach. On a REAL Great Lakes harbour
        the ACTUAL surveyed breakwater is auto-fetched from OpenStreetMap
        (man_made=breakwater) and meshed as a thin solid barrier over real
        bathymetry; only if OSM has no structure does a LABELED schematic apply.
      - ``resonance`` - a narrow-mouth harbour amplifies swell at its seiche
        periods (response spikes AT resonance, quiet OFF).
      - ``shoal`` - a nearshore reef/shoal refracts + focuses waves (a focus peak
        down-wave).

    Do NOT use this for: the regional/offshore SEA STATE or fetch-limited wind-wave
    growth (``tomawac_wave_field`` - the phase-averaged spectral tier); inundation
    DEPTH (``sfincs_flood``); a river dye/contaminant plume (``telemac_river_dye``).
    This tool returns a dimensionless AGITATION field (Kd), not a water level.

    Params:
        location: a harbour / coastal place near the AOI (e.g. "Marquette,
            Michigan", "Duluth harbor"). Supply this OR ``bbox`` - geocoded, never
            hand-typed coords.
        bbox: OPTIONAL explicit AOI ``(min_lon, min_lat, max_lon, max_lat)``
            EPSG:4326 (open-water harbour approach for the real-bathy path).
        wave_mode: OPTIONAL question class - ``diffraction`` / ``resonance`` /
            ``shoal``. Unset -> inferred from the prompt (defaults to diffraction).
        wave_period_s: incident wave period (s). Default 8 (a LOUD demo default -
            no wave-forcing fetcher exists). Clamped [1, 300].
        wave_direction_deg: incident wave direction, trig convention (0=+X east /
            90=+Y north). Default 90.
        wave_height_m: incident wave height H0 (m) at the open boundary. Default 1.
            Clamped [0.01, 10].
        reflection_coef: structure/quay reflection coefficient (1=fully reflecting,
            0=absorbing). Default 1. Clamped [0, 1].
        breakwater: OPTIONAL diffraction breakwater segment
            ``(lon0, lat0, lon1, lat1)`` EPSG:4326 that PINS the structure and
            suppresses the OSM auto-fetch. Unset (real-bathy) -> the ACTUAL
            surveyed breakwater is fetched from OpenStreetMap and meshed; if OSM
            has none, a LABELED schematic segment applies.
        target_resolution_m: OPTIONAL grid node spacing (m). Unset -> a labeled
            default (real 40 m, idealized 8 m). Floored at 20 m and coarsened
            under the node budget (self-labeled).
        bathy_source: ``"auto"`` (default - a Great Lakes AOI uses real NOAA
            lake-datum bathymetry, else an idealized analytic domain labeled as
            such) | ``"noaa_greatlakes"`` | ``"idealized"``.
        compute_class: FR-CE-3 compute class. Default ``"medium"``.
        input_mode: ``"user_gated"`` reviews the resolved forcing before the solve;
            ``"auto"`` (default) proceeds labeled.

    Returns:
        On success: ``ArtemisAgitationLayerURI`` (``LayerURI`` subtype) - the
        emitter loads the Kd COG onto the map and the client animates the ARTEMIS
        SELAFIN mesh sibling. Carries ``kd_max`` / ``kd_sheltered`` / ``kd_exposed``
        / ``resonant_period_s`` / ``response_at_resonance`` / ``wave_mode`` (narrate
        these typed numbers only - invariant 1) + a ``fallback_note`` (phase-resolving
        screening honesty floor). On failure: dict with ``status="error"`` +
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
                    "error_code": "ARTEMIS_PARAMS_INVALID",
                    "error_message": f"invalid bbox (need 4 numbers): {bbox!r}",
                }
        else:
            coerced_bbox = tuple(cb)  # type: ignore[assignment]

    has_loc = bool(location and str(location).strip())
    if not has_loc and coerced_bbox is None:
        return {
            "status": "error",
            "error_code": "ARTEMIS_PARAMS_INCOMPLETE",
            "error_message": (
                "artemis_harbor_agitation needs a `location` (geocoded harbour/"
                "coast) or an explicit `bbox` AOI."
            ),
        }
    if has_loc and coerced_bbox is not None:
        coerced_bbox = None  # location wins (an LLM-invented bbox is not truth)

    mode = _classify_mode(location, wave_mode)
    try:
        wave_period_s = max(1.0, min(300.0, float(wave_period_s)))
    except (TypeError, ValueError):
        wave_period_s = DEFAULT_WAVE_PERIOD_S
    try:
        wave_direction_deg = float(wave_direction_deg) % 360.0
    except (TypeError, ValueError):
        wave_direction_deg = DEFAULT_WAVE_DIR_DEG
    try:
        wave_height_m = max(0.01, min(10.0, float(wave_height_m)))
    except (TypeError, ValueError):
        wave_height_m = DEFAULT_WAVE_HEIGHT_M
    try:
        reflection_coef = max(0.0, min(1.0, float(reflection_coef)))
    except (TypeError, ValueError):
        reflection_coef = DEFAULT_REFLECTION_COEF
    if target_resolution_m is not None:
        try:
            target_resolution_m = max(20.0, float(target_resolution_m))
        except (TypeError, ValueError):
            target_resolution_m = None
    bw = None
    if breakwater is not None:
        try:
            bw = tuple(float(v) for v in breakwater)
            if len(bw) != 4:
                bw = None
        except (TypeError, ValueError):
            bw = None

    logger.info(
        "artemis_harbor_agitation location=%r bbox=%s mode=%s T=%.1fs dir=%.0f "
        "H0=%.2f rp=%.2f bathy=%s res=%s",
        location, coerced_bbox, mode, wave_period_s, wave_direction_deg,
        wave_height_m, reflection_coef, bathy_source, target_resolution_m,
    )

    try:
        layer = await model_artemis_harbor_agitation(
            location=location if has_loc else None,
            bbox=coerced_bbox,
            wave_mode=mode,
            wave_period_s=wave_period_s,
            wave_direction_deg=wave_direction_deg,
            wave_height_m=wave_height_m,
            reflection_coef=reflection_coef,
            breakwater=bw,
            target_resolution_m=target_resolution_m,
            bathy_source=str(bathy_source or "auto"),
            compute_class=compute_class,
            input_mode=input_mode,
        )
        logger.info(
            "artemis_harbor_agitation complete layer_id=%s kd_max=%.3g "
            "sheltered=%s exposed=%s uri=%s",
            layer.layer_id, layer.kd_max, layer.kd_sheltered, layer.kd_exposed,
            layer.uri,
        )
        return layer
    except asyncio.CancelledError:
        raise
    except (ArtemisAgitationError, PostprocessTelemacError) as exc:
        logger.warning("artemis_harbor_agitation failed: %s (%s)",
                       getattr(exc, "error_code", "?"), exc)
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "ARTEMIS_RUN_FAILED"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("artemis_harbor_agitation unexpected failure")
        return {
            "status": "error",
            "error_code": "ARTEMIS_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
def _bbox_center(bbox) -> tuple[float, float]:
    return (0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3]))


def _stage_agitation_manifest(agitation: dict[str, Any], run_tag: str) -> str:
    """Write the artemis ``agitation`` worker manifest to the cache bucket; return uri."""
    from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise ArtemisAgitationError(
            "ARTEMIS_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage the ARTEMIS manifest.")
    manifest = {
        "agitation": agitation,
        "run_id": run_tag,
        "inputs": [],
        "telemac_args": [],
        "outputs": [
            "agit_field.slf", "res_agitation.slf", "geo_agit.slf", "bc_agit.cli",
            "art_agit.cas", "full_listing.log", "artemis_agit.log",
            "bed_bathymetry.tif", "telemac_metrics.json",
        ],
    }
    key = f"artemis/{run_tag}/manifest.json"
    _get_s3_client().put_object(
        Bucket=cache_bucket, Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


#: Overpass mirrors (data-source fallback norm: primary -> mirror -> honest give-up).
_OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)


def _fetch_osm_breakwaters(aoi: tuple[float, float, float, float]) -> list[list[list[float]]]:
    """Fetch REAL surveyed breakwater geometry (OSM man_made=breakwater ways) in the
    AOI as polylines [[lon,lat], ...]. BEST-EFFORT: any failure returns [] (the run
    falls back to the LABELED schematic barrier, never fabricates a structure).

    man_made=pier is deliberately EXCLUDED: piers are marina berthing docks (the
    thing being sheltered), not wave barriers -- meshing them as solid would be
    physically wrong for the sheltering question."""
    import urllib.parse
    import urllib.request

    w, s, e, n = (float(aoi[0]), float(aoi[1]), float(aoi[2]), float(aoi[3]))
    ql = (f'[out:json][timeout:40];'
          f'(way["man_made"="breakwater"]({s},{w},{n},{e}););out geom;')
    body = b"data=" + urllib.parse.quote(ql).encode()
    for url in _OVERPASS_MIRRORS:
        try:
            req = urllib.request.Request(
                url, data=body, headers={"User-Agent": "trid3nt/0.1 (agent@trid3nt.dev)"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            polylines = [
                [[float(p["lon"]), float(p["lat"])] for p in el.get("geometry", [])]
                for el in payload.get("elements", [])
                if len(el.get("geometry", [])) >= 2
            ]
            if polylines:
                logger.info("artemis: fetched %d OSM breakwater ways in %s",
                            len(polylines), aoi)
            return polylines
        except Exception as exc:  # noqa: BLE001 - best-effort, mirror fallback
            logger.warning("artemis: OSM breakwater fetch via %s failed: %s",
                           url.split("/")[2], exc)
    logger.warning("artemis: OSM breakwater fetch exhausted for %s -- schematic fallback", aoi)
    return []


def _stage_breakwater_fgb(polylines, run_tag: str, name: str):
    """Write the surveyed breakwater polylines to a FlatGeobuf in the cache bucket
    and return a context ``LayerURI`` (role=context) for input-parity (ADR 0231).
    BEST-EFFORT: returns None on any failure (input surfacing is never fatal)."""
    try:
        import geopandas as gpd
        from shapely.geometry import LineString

        from trid3nt_contracts.execution import LayerURI
        from trid3nt_server.agent.tools.simulation.solver.solver import _get_s3_client

        cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
        if not cache_bucket:
            return None
        geoms = [LineString(pl) for pl in polylines if len(pl) >= 2]
        if not geoms:
            return None
        gdf = gpd.GeoDataFrame({"osm_kind": ["breakwater"] * len(geoms)},
                               geometry=geoms, crs="EPSG:4326")
        tmp = tempfile.mkdtemp(prefix=f"bw-fgb-{run_tag}-")
        fgb = os.path.join(tmp, "breakwater.fgb")
        gdf.to_file(fgb, driver="FlatGeobuf")
        key = f"artemis/{run_tag}/breakwater_structure.fgb"
        with open(fgb, "rb") as fh:
            _get_s3_client().put_object(Bucket=cache_bucket, Key=key, Body=fh.read(),
                                        ContentType="application/octet-stream")
        return LayerURI(
            layer_id=f"artemis-breakwater-{run_tag}", name=f"Surveyed breakwater ({name})",
            layer_type="vector", uri=f"s3://{cache_bucket}/{key}",
            style_preset="affected_buildings", role="context")
    except Exception as exc:  # noqa: BLE001 - input surfacing is never fatal
        logger.warning("artemis: breakwater FGB staging failed (non-fatal): %s", exc)
        return None


def _download_agitation_result(run_id: str) -> tuple[str, dict[str, Any]]:
    """Download ``agit_field.slf`` + read telemac_metrics.json. Returns (path, metrics)."""
    from trid3nt_server.agent.tools.simulation.solver.solver import (
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
        logger.warning("artemis: metrics read failed for run %s: %s", run_id, exc)
    slf_key = f"{run_id}/agit_field.slf"
    tmp_dir = tempfile.mkdtemp(prefix=f"artemis-{run_id}-")
    slf_path = str(Path(tmp_dir) / "agit_field.slf")
    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=slf_key)
        with open(slf_path, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise ArtemisAgitationError(
            "ARTEMIS_OUTPUT_MISSING",
            f"ARTEMIS run {run_id} completed but s3://{runs_bucket}/{slf_key} "
            f"was not downloadable: {exc}") from exc
    return slf_path, metrics


async def model_artemis_harbor_agitation(
    *,
    location: str | None,
    bbox: tuple[float, float, float, float] | None,
    wave_mode: str,
    wave_period_s: float,
    wave_direction_deg: float,
    wave_height_m: float,
    reflection_coef: float,
    breakwater: tuple[float, float, float, float] | None,
    target_resolution_m: float | None,
    bathy_source: str,
    compute_class: str = "medium",
    input_mode: str | None = None,
    breakwater_polylines: list | None = None,
    pipeline_emitter: Any = None,
) -> ArtemisAgitationLayerURI:
    """Compose place/AOI -> ARTEMIS agitation field -> published Kd layer.

    A real Great Lakes AOI (diffraction) -> NOAA lake-datum bathymetry + the REAL
    surveyed breakwater auto-fetched from OSM (or a driver-supplied
    ``breakwater_polylines``; a schematic only if OSM has none); otherwise an
    idealized analytic domain (labeled). Stages the ``agitation`` manifest, the
    surveyed structure surfaces as a context layer (ADR 0231), dispatches run_solver
    (solver=artemis_agitation), downloads the single-frame agitation field, and
    postprocesses it to a Kd 4326 COG.
    """
    from trid3nt_server.emission.pipeline_emitter import (
        begin_substeps,
        current_emitter,
        mint_dispatch_and_sim_cards,
        route_sim_terminal,
        substep,
    )
    from trid3nt_server.agent.gates.input_review import gate_input_review
    from trid3nt_server.agent.tools.publish_layer.publish_layer import publish_layer
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )
    from trid3nt_server.agent.workflows.shared.solve_progress import (
        drive_live_solve_progress,
    )

    emitter = pipeline_emitter or current_emitter()

    _planned = 3  # run_solver + postprocess + publish
    if location:
        _planned += 1  # geocode
    begin_substeps(current_emitter(), _planned)

    center_lon = center_lat = None
    location_name = location or "AOI"
    if location:
        geocode_fn = TOOL_REGISTRY["geocode_location"].fn
        async with substep(current_emitter(), "geocode_location"):
            geo = await asyncio.to_thread(geocode_fn, location)
        center_lon = _geo_field(geo, ("lon", "longitude", "x"))
        center_lat = _geo_field(geo, ("lat", "latitude", "y"))
        if center_lon is None or center_lat is None:
            raise ArtemisAgitationError(
                "ARTEMIS_GEOCODE_FAILED",
                f"could not geocode {location!r} to a harbour/coastal AOI.")
    else:
        center_lon, center_lat = _bbox_center(bbox)

    src = str(bathy_source or "auto").lower()
    lake = _great_lake_for(float(center_lon), float(center_lat))
    # the real-bathy path only supports diffraction (resonance/shoal are the
    # analytic idealized V&V domains); a real-bathy request for those falls back
    # to idealized (labeled) rather than fabricating a harbour outline.
    real = (wave_mode == "diffraction") and (
        src == "noaa_greatlakes" or (src == "auto" and lake is not None))

    real_polylines: list | None = None
    if real:
        if bbox is not None:
            aoi = tuple(float(v) for v in bbox)
        else:
            # a small open-water harbour-approach box around the centroid.
            h = 0.06
            aoi = (round(center_lon - h, 4), round(center_lat - h, 4),
                   round(center_lon + h, 4), round(center_lat + h, 4))
        bathy_label = f"real NOAA Great Lakes lake-datum bathymetry ({lake or 'AOI'})"
        # REAL surveyed structure (norm #10 "a real marina with a real breaker"):
        # a driver-supplied geometry wins; else, when no single segment is pinned,
        # auto-fetch the ACTUAL breakwater from OSM and mesh it. Empty fetch ->
        # the labeled schematic barrier (never a fabricated structure).
        if breakwater_polylines:
            real_polylines = [pl for pl in breakwater_polylines if len(pl) >= 2]
        elif not breakwater:
            real_polylines = await asyncio.to_thread(_fetch_osm_breakwaters, aoi) or None
    else:
        aoi = None
        if wave_mode == "resonance":
            bathy_label = ("idealized narrow-mouth harbour basin (analytic seiche "
                           "ladder; no real harbour outline fetched)")
        elif wave_mode == "shoal":
            bathy_label = ("EXACT Berkhoff-Booij-Radder (1982) elliptic-shoal "
                           "bathymetry (analytic refraction-focusing V&V)")
        else:
            bathy_label = ("idealized flat bed (analytic Sommerfeld semi-infinite "
                           "breakwater; no real bathymetry for this AOI)")

    res_m = float(target_resolution_m) if target_resolution_m is not None else (
        DEFAULT_REAL_RES_M if real else DEFAULT_IDEALIZED_RES_M)
    res_default = target_resolution_m is None

    # --- LOUD labeled defaults (no wave-forcing fetcher): forcing is reviewable - #
    _review_entries = [
        SyntheticInput(
            param="wave_period_s", value=round(wave_period_s, 1), units="s",
            basis="default_demo", note="prescribed monochromatic incident wave period"),
        SyntheticInput(
            param="wave_height_m", value=round(wave_height_m, 2), units="m",
            basis="default_demo", note="prescribed incident wave height H0"),
        SyntheticInput(
            param="bathy_source", value="noaa_greatlakes" if real else "idealized",
            basis="fetched" if real else "default_demo", note=bathy_label),
    ]
    if wave_mode == "diffraction":
        if real_polylines:
            _bw_val, _bw_basis, _bw_note = (
                f"real_surveyed_{len(real_polylines)}_ways", "fetched",
                "the REAL surveyed breakwater (OSM man_made=breakwater) meshed as a "
                "thin solid barrier over real bathymetry")
        elif breakwater:
            _bw_val, _bw_basis, _bw_note = (
                "user-supplied", "user", "user-supplied breakwater segment")
        else:
            _bw_val, _bw_basis, _bw_note = (
                "schematic_demo", "default_demo",
                "a LABELED schematic breakwater across the approach (no surveyed "
                "structure fetched)")
        _review_entries.append(SyntheticInput(
            param="breakwater", value=_bw_val, units=None,
            basis=_bw_basis, note=_bw_note))
    _review = await gate_input_review(
        tool_name="artemis_harbor_agitation", mode=input_mode,
        entries=_review_entries,
        params={"wave_period_s": float(wave_period_s),
                "wave_height_m": float(wave_height_m)})
    if _review.cancelled:
        raise ArtemisAgitationError("USER_INPUT_CANCELLED",
                                    f"artemis_harbor_agitation {_review.cancel_reason}")
    wave_period_s = float(_review.params.get("wave_period_s", wave_period_s))
    wave_height_m = float(_review.params.get("wave_height_m", wave_height_m))

    # --- Stage the agitation manifest ----------------------------------------- #
    agitation: dict[str, Any] = {
        "name": _slug(location_name),
        "wave_mode": wave_mode,
        "bathy_source": "noaa_greatlakes" if real else "idealized",
        "wave_period_s": float(wave_period_s),
        "wave_dir_deg": float(wave_direction_deg),
        "wave_height_m": float(wave_height_m),
        "reflection_coef": float(reflection_coef),
        "target_resolution_m": float(res_m),
    }
    if real:
        agitation["bbox"] = [round(v, 4) for v in aoi]
        if real_polylines:
            agitation["breakwater_polylines"] = [
                [[round(lon, 6), round(lat, 6)] for lon, lat in pl]
                for pl in real_polylines]
        elif breakwater:
            agitation["breakwater"] = [round(v, 5) for v in breakwater]
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(_stage_agitation_manifest, agitation, run_tag)
    logger.info("model_artemis_harbor_agitation staged manifest run_tag=%s mode=%s "
                "bathy=%s -> %s", run_tag, wave_mode, agitation["bathy_source"],
                manifest_uri)

    # --- Dispatch to the solver ----------------------------------------------- #
    handle = run_solver(
        solver=ARTEMIS_SOLVER_NAME, model_setup_uri=manifest_uri,
        compute_class=compute_class)
    run_id = handle.run_id
    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=ARTEMIS_SOLVER_NAME, handle=handle,
        compute_class=compute_class)
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))
    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(), run_id=run_id, solver=ARTEMIS_SOLVER_NAME,
            grid_resolution_m=res_m, active_cell_count=None, vcpus=None,
            eta_seconds=None))
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
        raise ArtemisAgitationError(
            "ARTEMIS_RUN_FAILED",
            f"ARTEMIS agitation solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}")

    # --- Download + postprocess to the Kd COG --------------------------------- #
    batch_run_id = getattr(run_result, "run_id", None) or run_id
    slf_path, metrics = await asyncio.to_thread(_download_agitation_result, batch_run_id)
    utm_epsg = metrics.get("utm_epsg")   # None on the idealized analytic path
    # the AOI bbox the worker meshed in a LOCAL UTM frame: postprocess needs the SW
    # corner to add the origin offset back before UTM->4326 (else the field lands at
    # the zone origin, not the harbour). None on the idealized analytic path.
    request_bbox = metrics.get("bbox")
    reach_name = _slug(location_name)
    try:
        async with substep(emitter, "postprocess_artemis"):
            layers, pmetrics = await asyncio.to_thread(
                postprocess_artemis, slf_path, run_id=batch_run_id,
                utm_epsg=int(utm_epsg) if utm_epsg is not None else None,
                request_bbox=request_bbox,
                incident_hs_m=float(wave_height_m), reach_name=reach_name,
                wave_mode=wave_mode)
    finally:
        try:
            Path(slf_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    if not layers:
        raise ArtemisAgitationError("ARTEMIS_NO_LAYERS",
                                    "postprocess_artemis produced no agitation layer.")
    raw = layers[0]

    # fold the worker's discriminating pair + forcing onto the layer so the agent
    # narrates typed numbers (invariant 1).
    enriched = raw.model_copy(update={
        "kd_sheltered": metrics.get("kd_sheltered"),
        "kd_exposed": metrics.get("kd_exposed"),
        "resonant_period_s": metrics.get("resonant_period_s"),
        "response_at_resonance": metrics.get("response_at_resonance"),
        "response_off_resonance": metrics.get("response_off_resonance"),
        "wave_period_s": metrics.get("wave_period_s") or float(wave_period_s),
        "mesh_size_m": metrics.get("dx_m") or res_m,
        "mesh_resolution_label": (
            f"{'real NOAA lake bathy' if real else 'idealized analytic'} grid "
            f"{metrics.get('dx_m', res_m):g} m"
            + (" (coarsened under node budget)" if metrics.get("coarsened") else "")),
    })

    from trid3nt_server.agent.tools.publish_layer.publish_layer import PublishLayerError

    async with substep(emitter, "publish_layer"):
        published = enriched
        if enriched.uri.startswith(("s3://", "gs://")):
            try:
                pub_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=enriched.uri,
                    layer_id=enriched.layer_id,
                    style_preset=enriched.style_preset or TELEMAC_AGITATION_STYLE_PRESET,
                )
                published = enriched.model_copy(update={"uri": pub_uri})
            except PublishLayerError as exc:
                logger.warning("artemis publish_layer failed (%s) - unpublished COG", exc)

    # input-parity: surface the REAL surveyed breakwater geometry as a visible
    # context layer (best-effort, never fatal). This fetch needs the OSM way
    # POLYLINE geometry (meshed as a thin barrier), which the router's only
    # general overpass source collapses to centroids -- so it stays a bare-OSM
    # router-bypass, surfaced here explicitly + sweep-allowlisted (ADR 0244 S3).
    if real_polylines:
        from trid3nt_server.emission.layer_uri_emit import publish_input_layer
        bw_layer = await asyncio.to_thread(
            _stage_breakwater_fgb, real_polylines, run_tag, _slug(location_name))
        if bw_layer is not None:
            await publish_input_layer(emitter, bw_layer, role="context")

    # in-worker bed input (ADR 0244 S3): the NOAA lake-datum bed is sampled inside
    # the solver container (no agent-side router fetch), so the composer rides the
    # bed COG the worker recorded through publish_raster_input_cog. Best-effort;
    # only the real-bathy diffraction path writes one (metrics.bed_cog present).
    from trid3nt_server.agent.workflows.telemac._bed_input import (
        surface_in_worker_bed_input,
    )
    await surface_in_worker_bed_input(
        emitter, run_metrics=metrics, run_id=batch_run_id,
        name=(f"Input: lake bed bathymetry ({reach_name}, "
              f"NOAA Great Lakes lake-datum, in-worker)"))
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
    s = "".join(c if c.isalnum() else "_" for c in str(name or "harbor").lower())
    return "_".join(p for p in s.split("_") if p)[:48] or "harbor_agitation"
