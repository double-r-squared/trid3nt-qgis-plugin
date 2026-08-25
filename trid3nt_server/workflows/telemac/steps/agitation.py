"""The ARTEMIS deck and deliverable: a swell at the mouth, an agitation field in.

One serialization hook and one publisher for ARTEMIS, the phase-RESOLVING elliptic
mild-slope (Berkhoff) solver: diffraction behind a breakwater, harbour resonance,
refraction over a shoal. Staging, dispatching and reading the run are the shared
open-water front (``steps/open_water.py``); what lives here is only what is
AGITATION about an agitation run.

THE REAL STRUCTURE. The sheltering question is meaningless without the thing that
shelters, so a real-harbour diffraction run fetches the ACTUAL surveyed breakwater
(OpenStreetMap ``man_made=breakwater``) and meshes it as a thin solid barrier.
``man_made=pier`` is deliberately excluded: a pier is the berthing dock being
sheltered, not a wave barrier, and meshing one solid would answer a different
question. When OSM has no structure the run says so and falls back to a LABELED
schematic - it never fabricates a breakwater and calls it surveyed.
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

from trid3nt_server.workflows.lib import Step
from trid3nt_server.workflows.shared.publish_product_layer import (
    publish_product_layer,
)

from .open_water import (
    OpenWaterError,
    download_open_water_result,
    mesh_sizing_provenance,
    solved_domain_bbox,
    surface_in_worker_bed_input,
)
from .wave import great_lake_for

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.agitation")

__all__ = ["Agitation", "fetch_osm_breakwaters", "publish_agitation_products",
           "write_agitation_deck"]

_STEPS = "trid3nt_server.workflows.telemac.steps"

_SECTION = "agitation"
_PREFIX = "artemis"
_RESULT = "agit_field.slf"
_OUTPUTS = [
    "agit_field.slf", "res_agitation.slf", "geo_agit.slf", "bc_agit.cli",
    "art_agit.cas", "full_listing.log", "artemis_agit.log", "bed_bathymetry.tif",
    "telemac_metrics.json",
]

#: Overpass mirrors, in order. The data-source fallback norm: primary, mirror,
#: then an HONEST give-up - never a fabricated structure.
_OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)

#: Labeled grid spacings when the caller names none.
_DEFAULT_REAL_RES_M = 40.0
_DEFAULT_IDEALIZED_RES_M = 8.0

#: Only the DIFFRACTION class has a real harbour to mesh. Resonance and shoal are
#: the ANALYTIC verification domains (a seiche ladder, the Berkhoff-Booij-Radder
#: elliptic shoal), so a real-bathymetry request for those falls back to the
#: idealized domain, labeled, rather than fabricating a harbour outline.
_REAL_BATHY_MODES = ("diffraction",)


def fetch_osm_breakwaters(aoi: tuple[float, float, float, float]) -> list[list[list[float]]]:
    """The REAL surveyed breakwater ways in the AOI, as ``[[lon, lat], ...]`` lines.

    BEST-EFFORT by contract: every failure returns ``[]`` and the run falls back to
    the LABELED schematic barrier. This stays a bare-Overpass call rather than a
    router fetch because it needs the WAY GEOMETRY - the router's general overpass
    source collapses features to centroids, and a centroid cannot be meshed as a
    barrier.
    """
    import urllib.parse
    import urllib.request

    west, south, east, north = (float(aoi[0]), float(aoi[1]),
                                float(aoi[2]), float(aoi[3]))
    query = ('[out:json][timeout:40];'
             f'(way["man_made"="breakwater"]({south},{west},{north},{east}););out geom;')
    body = b"data=" + urllib.parse.quote(query).encode()
    for url in _OVERPASS_MIRRORS:
        try:
            request = urllib.request.Request(
                url, data=body,
                headers={"User-Agent": "trid3nt/0.1 (agent@trid3nt.dev)"})
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            polylines = [
                [[float(p["lon"]), float(p["lat"])] for p in el.get("geometry", [])]
                for el in payload.get("elements", [])
                if len(el.get("geometry", [])) >= 2
            ]
            if polylines:
                logger.info("artemis: fetched %d OSM breakwater ways in %s",
                            len(polylines), aoi)
            return polylines
        except Exception as exc:  # noqa: BLE001 - best-effort, next mirror
            logger.warning("artemis: OSM breakwater fetch via %s failed: %s",
                           url.split("/")[2], exc)
    logger.warning("artemis: OSM breakwater fetch exhausted for %s - the run falls "
                   "back to the labeled schematic barrier", aoi)
    return []


def _stage_breakwater_layer(polylines: Any, run_tag: str, name: str) -> Any:
    """The surveyed structure as a context vector layer. ``None`` on any failure."""
    try:
        import geopandas as gpd
        from shapely.geometry import LineString

        from trid3nt_contracts.execution import LayerURI
        from trid3nt_server.workflows.solver.solver import _get_s3_client

        cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
        geometries = [LineString(line) for line in polylines if len(line) >= 2]
        if not cache_bucket or not geometries:
            return None
        frame = gpd.GeoDataFrame({"osm_kind": ["breakwater"] * len(geometries)},
                                 geometry=geometries, crs="EPSG:4326")
        local = os.path.join(tempfile.mkdtemp(prefix=f"bw-fgb-{run_tag}-"),
                             "breakwater.fgb")
        frame.to_file(local, driver="FlatGeobuf")
        key = f"{_PREFIX}/{run_tag}/breakwater_structure.fgb"
        with open(local, "rb") as fh:
            _get_s3_client().put_object(Bucket=cache_bucket, Key=key, Body=fh.read(),
                                        ContentType="application/octet-stream")
        return LayerURI(
            layer_id=f"artemis-breakwater-{run_tag}",
            name=f"Surveyed breakwater ({name})", layer_type="vector",
            uri=f"s3://{cache_bucket}/{key}", style_preset="affected_buildings",
            role="context")
    except Exception as exc:  # noqa: BLE001 - input surfacing is never fatal
        logger.warning("artemis: breakwater staging failed (non-fatal): %s", exc)
        return None


async def write_agitation_deck(
    *,
    aoi: dict[str, Any],
    wave_mode: str = "diffraction",
    wave_period_s: float = 8.0,
    wave_direction_deg: float = 90.0,
    wave_height_m: float = 1.0,
    reflection_coef: float = 1.0,
    breakwater: Any = None,
    mesh_resolution_m: float | None = None,
    bathy_source: str = "auto",
) -> dict[str, Any]:
    """Serialize the approved sheet into the worker's agitation config + run meta.

    A pinned ``breakwater`` segment SUPPRESSES the OSM fetch: the caller named the
    structure, and going and finding a different one would model something else.
    """
    from trid3nt_server.workflows.telemac.run_telemac import ARTEMIS_SOLVER_NAME

    asked = str(bathy_source or "auto").strip().lower()
    lake = great_lake_for(float(aoi["lon"]), float(aoi["lat"]))
    real = str(wave_mode) in _REAL_BATHY_MODES and (
        asked == "noaa_greatlakes" or (asked == "auto" and lake is not None))
    resolution = (float(mesh_resolution_m) if mesh_resolution_m is not None
                  else (_DEFAULT_REAL_RES_M if real else _DEFAULT_IDEALIZED_RES_M))
    pinned = _coerce_segment(breakwater)

    polylines: list | None = None
    if real and pinned is None:
        polylines = await asyncio.to_thread(
            fetch_osm_breakwaters, tuple(aoi["bbox"])) or None

    config: dict[str, Any] = {
        "name": aoi["slug"],
        "wave_mode": str(wave_mode),
        "bathy_source": "noaa_greatlakes" if real else "idealized",
        "wave_period_s": float(wave_period_s),
        "wave_dir_deg": float(wave_direction_deg),
        "wave_height_m": float(wave_height_m),
        "reflection_coef": float(reflection_coef),
        "target_resolution_m": float(resolution),
    }
    if real:
        config["bbox"] = [round(float(v), 4) for v in aoi["bbox"]]
        if polylines:
            config["breakwater_polylines"] = [
                [[round(lon, 6), round(lat, 6)] for lon, lat in line]
                for line in polylines]
        elif pinned is not None:
            config["breakwater"] = [round(v, 5) for v in pinned]
    return {
        "config": config,
        "run_tag": new_ulid(),
        "section": _SECTION,
        "prefix": _PREFIX,
        "solver": ARTEMIS_SOLVER_NAME,
        "result_basename": _RESULT,
        "outputs": list(_OUTPUTS),
        "run_failed_code": "ARTEMIS_RUN_FAILED",
        "output_missing_code": "ARTEMIS_OUTPUT_MISSING",
        # Only the REAL-bathymetry harbour is georeferenced. The analytic seiche
        # ladder and the Berkhoff shoal are geography-free by construction and
        # report no zone; their reader rasterizes the local metres directly.
        "requires_utm": real,
        "domain_name": aoi["name"],
        "domain_slug": aoi["slug"],
        "mesh_size_m": float(resolution),
        # What the CALLER asked for, kept beside what was built, so a
        # lever the worker moved leaves a row instead of a silence.
        "mesh_resolution_asked_m": (mesh_resolution_m if mesh_resolution_m is not None
                                    else None),
        "wave_mode": str(wave_mode),
        "wave_period_s": float(wave_period_s),
        "wave_height_m": float(wave_height_m),
        "real_bathymetry": real,
        "breakwater_polylines": polylines,
        "breakwater_pinned": pinned is not None,
        "bathy_label": _bathy_label(real, str(wave_mode), lake),
    }


def _coerce_segment(value: Any) -> tuple[float, float, float, float] | None:
    """A pinned breakwater segment, or ``None`` when nothing usable was supplied."""
    if value is None:
        return None
    try:
        segment = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None
    return segment if len(segment) == 4 else None  # type: ignore[return-value]


def _bathy_label(real: bool, wave_mode: str, lake: str | None) -> str:
    if real:
        return f"real NOAA Great Lakes lake-datum bathymetry ({lake or 'AOI'})"
    if wave_mode == "resonance":
        return ("idealized narrow-mouth harbour basin (analytic seiche ladder; no "
                "real harbour outline fetched)")
    if wave_mode == "shoal":
        return ("EXACT Berkhoff-Booij-Radder (1982) elliptic-shoal bathymetry "
                "(analytic refraction-focusing V&V)")
    return ("idealized flat bed (analytic Sommerfeld semi-infinite breakwater; no "
            "real bathymetry for this AOI)")


def _structure_row(deck: dict[str, Any]) -> SyntheticInput:
    """WHAT was meshed as the barrier - surveyed, pinned, or honestly schematic."""
    if deck.get("breakwater_polylines"):
        return SyntheticInput(
            param="breakwater",
            value=f"real_surveyed_{len(deck['breakwater_polylines'])}_ways",
            basis="fetched", consequence="scenario",
            real_source_if_any="OpenStreetMap man_made=breakwater ways",
            note="the REAL surveyed breakwater, meshed as a thin solid barrier "
                 "over real bathymetry")
    if deck.get("breakwater_pinned"):
        return SyntheticInput(
            param="breakwater", value="user-supplied", basis="user",
            consequence="scenario", note="user-supplied breakwater segment")
    return SyntheticInput(
        param="breakwater", value="schematic_demo", basis="default_demo",
        consequence="scenario",
        note="a LABELED schematic breakwater across the approach - no surveyed "
             "structure was fetched")


def _provenance(deck: dict[str, Any], metrics: dict[str, Any]) -> list[SyntheticInput]:
    """The physically dominant inputs, as rows the layer carries."""
    real = bool(deck["real_bathymetry"])
    rows = [
        SyntheticInput(
            param="wave_period_s", value=round(deck["wave_period_s"], 1), units="s",
            basis="default_demo", consequence="physics",
            note="prescribed monochromatic incident wave period"),
        SyntheticInput(
            param="wave_height_m", value=round(deck["wave_height_m"], 2), units="m",
            basis="default_demo", consequence="physics",
            note="prescribed incident wave height H0 at the open boundary"),
        SyntheticInput(
            param="bathy_source", value=deck["config"]["bathy_source"],
            basis="fetched" if real else "default_demo", consequence="physics",
            real_source_if_any=("NOAA NGDC Great Lakes lake-datum bathymetry"
                                if real else None),
            note=deck["bathy_label"]),
    ]
    if deck["wave_mode"] == "diffraction":
        rows.append(_structure_row(deck))
    return rows + mesh_sizing_provenance(deck.get("mesh_resolution_asked_m"), metrics)


def _honesty_note(deck: dict[str, Any]) -> str:
    return (
        "Phase-RESOLVING agitation SCREENING: ARTEMIS elliptic mild-slope "
        f"(Berkhoff) over a {deck['mesh_size_m']:g} m grid of {deck['bathy_label']}, "
        f"driven by a PRESCRIBED monochromatic {deck['wave_period_s']:g} s / "
        f"{deck['wave_height_m']:g} m incident wave - a labeled demo forcing, not an "
        "observed sea state. The raster is the steady-state agitation coefficient "
        "Kd = Hs/H0. Not a calibrated hindcast.")


async def publish_agitation_products(*, deck: dict[str, Any],
                                     solve: dict[str, Any]) -> ArtemisAgitationLayerURI:
    """Postprocess the solved harbour into its published layer + scalars."""
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.workflows.telemac.postprocess_telemac import postprocess_artemis

    emitter = current_emitter()
    run_id = solve["run_id"]
    metrics = dict(solve.get("metrics") or {})
    reach = deck["domain_slug"]
    utm_epsg = solve["utm_epsg"]

    slf_path = await asyncio.to_thread(
        download_open_water_result, run_id, deck["result_basename"],
        error_code=deck["output_missing_code"])
    try:
        layers, _pmetrics = await asyncio.to_thread(
            postprocess_artemis, slf_path, run_id=run_id, utm_epsg=utm_epsg,
            # The worker meshes in a LOCAL UTM frame, so the postprocess needs the
            # SW corner to add the origin back before UTM -> 4326. The idealized
            # analytic domain records no bbox and has none to add.
            request_bbox=solved_domain_bbox(deck, metrics),
            incident_hs_m=float(deck["wave_height_m"]), reach_name=reach,
            wave_mode=deck["wave_mode"])
    finally:
        Path(slf_path).unlink(missing_ok=True)
    if not layers:
        raise OpenWaterError("postprocess_artemis produced no agitation layer.",
                             error_code="ARTEMIS_NO_LAYERS")
    raw = layers[0]

    published = await publish_product_layer(
        raw, style_preset=TELEMAC_AGITATION_STYLE_PRESET,
        update={
            "kd_sheltered": metrics.get("kd_sheltered"),
            "kd_exposed": metrics.get("kd_exposed"),
            "resonant_period_s": metrics.get("resonant_period_s"),
            "response_at_resonance": metrics.get("response_at_resonance"),
            "response_off_resonance": metrics.get("response_off_resonance"),
            "wave_period_s": metrics.get("wave_period_s") or deck["wave_period_s"],
            "mesh_size_m": metrics.get("dx_m") or deck["mesh_size_m"],
            "mesh_resolution_label": (
                f"{'real NOAA lake bathy' if deck['real_bathymetry'] else 'idealized analytic'} "
                f"grid {metrics.get('dx_m', deck['mesh_size_m']):g} m"
                + (" (coarsened under node budget)" if metrics.get("coarsened") else "")),
            "fallback_note": _honesty_note(deck),
            "synthetic_inputs": _provenance(deck, metrics),
            "run_id": run_id,
            # The curve the WORKER measured across the field - a transect for a
            # diffraction run, a period sweep for a resonance one. The chart plots
            # this, so the chart and the narrated scalars are one measurement.
            "agitation_curve_m": list(metrics.get("chart_s_m") or []) or None,
            "agitation_curve_kd": list(metrics.get("chart_kd") or []) or None,
            "agitation_curve_kind": metrics.get("chart_kind"),
        })

    # INPUT PARITY: the surveyed structure on the map beside the field it shelters.
    # A bare-Overpass fetch the emit-on-fetch seam cannot cover (it needs the way
    # geometry, which the router's general source collapses to centroids), so it is
    # surfaced here explicitly and sweep-allowlisted.
    if deck.get("breakwater_polylines"):
        from trid3nt_server.emission.layer_uri_emit import publish_input_layer

        layer = await asyncio.to_thread(
            _stage_breakwater_layer, deck["breakwater_polylines"], deck["run_tag"],
            reach)
        if layer is not None:
            await publish_input_layer(emitter, layer, role="context")

    await surface_in_worker_bed_input(
        emitter, run_metrics=metrics, run_id=run_id,
        name=(f"Input: lake bed bathymetry ({reach}, NOAA Great Lakes lake-datum, "
              "in-worker)"),
        layer_id_prefix="input-lake-bed")

    logger.info("telemac artemis complete run_id=%s domain=%s mode=%s kd_max=%.3g "
                "sheltered=%s exposed=%s uri=%s", run_id, reach, deck["wave_mode"],
                published.kd_max, published.kd_sheltered, published.kd_exposed,
                published.uri)
    return published


class Agitation:
    """The ARTEMIS author + read steps, as the facade binds them."""

    @staticmethod
    def deck(**kwargs: Any) -> Step:
        return Step(runner=f"{_STEPS}.agitation.write_agitation_deck", stage="author",
                    kwargs=kwargs)

    @staticmethod
    def products(*, deck: Any, solve: Any) -> Step:
        return Step(runner=f"{_STEPS}.agitation.publish_agitation_products",
                    stage="publish", kwargs={"deck": deck, "solve": solve})
