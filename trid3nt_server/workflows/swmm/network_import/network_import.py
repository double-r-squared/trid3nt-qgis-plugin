"""Engine template ``swmm_network_import`` - build a SWMM model from a REAL
municipal storm-drain GIS network (the dual-drainage MINOR system).

Where ``swmm_urban_flood`` SYNTHESIZES a quasi-2D overland mesh from a DEM, this
template imports the REAL piped sewer/storm-drain network a city publishes on its
open-data portal (or a user uploads): nodes (manholes/inlets/outfalls) as point
features, conduits (pipes) as line features. It parses them into SWMM
JUNCTIONS + OUTFALLS + CONDUITS (``agent/mesh/swmm_network.py``), loads the
imported network with an Atlas-14 design storm, runs pyswmm headless, and returns
a ``SWMMNetworkLayerURI`` - a network VECTOR layer (nodes coloured by peak HGL /
flooding, conduits by surcharge) plus the typed hydraulic-response scalars.

This is the practice-verification's #1-ranked gap (real sewer-network import as
the START of an urban-drainage project, not the DEM-synthesized approximation).
It is the foundation the dual-drainage overland<->pipe coupling builds on.

Determinism (invariant 1): every number the agent narrates comes from the typed
``SWMMNetworkLayerURI`` scalars the postprocess computed - never free-generated.
The network geometry is REAL (user upload / public GIS); missing attributes are
gap-filled with LABELED demo defaults (the labeled-degrade doctrine).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.swmm_contracts import SWMMNetworkLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.data.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.data import register_tool
from trid3nt_server.mesh.swmm_network import (
    MAX_NETWORK_NODES,
    SWMMNetworkError,
    build_network_inp,
    network_to_geojson_4326,
    parse_network_features,
    run_network_deck,
)
from trid3nt_server.workflows.swmm._template_card import TemplateCard
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.network_import.network_import"
)

__all__ = ["swmm_network_import", "model_swmm_network_import"]


#: Inches -> mm (Atlas-14 PFDS returns inches; the hyetograph builder wants mm).
_INCH_TO_MM: float = 25.4


TEMPLATE_CARD = TemplateCard(
    question=(
        "import a REAL municipal storm-drain / sewer GIS network (nodes + "
        "conduits) into a runnable SWMM model, load it with a design storm, and "
        "report where the pipes surcharge / flood and how much reaches the "
        "outfall (the dual-drainage MINOR system)"
    ),
    required_inputs=["nodes_uri OR nodes_geojson (+ conduits, or one combined file)"],
    knobs=(
        "conduits_uri, nodes_geojson, conduits_geojson, bbox, return_period_yr, "
        "total_rain_depth_mm, storm_duration_hr, fill_missing_inverts_from_dem"
    ),
)


_METADATA = AtomicToolMetadata(
    name="swmm_network_import",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="swmm",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=True,  # may fetch a public ArcGIS FeatureServer network
    destructive_hint=False,
    idempotent_hint=False,
)
async def swmm_network_import(
    nodes_uri: str | None = None,
    conduits_uri: str | None = None,
    nodes_geojson: dict[str, Any] | None = None,
    conduits_geojson: dict[str, Any] | None = None,
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    return_period_yr: int = 10,
    total_rain_depth_mm: float | None = None,
    storm_duration_hr: float = 2.0,
    rain_interval_min: int = 5,
    fill_missing_inverts_from_dem: bool = True,
    compute_class: str = "standard",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SWMMNetworkLayerURI | dict[str, Any]:
    """Import a REAL municipal storm-drain / sewer GIS network into a runnable SWMM model.

    Fidelity: SWMM dynamic-wave routing on a REAL imported pipe network; a
    planning-grade dual-drainage MINOR-system model, not a calibrated master plan.
    Data: the network GEOMETRY is real (a user upload OR a public ArcGIS
    FeatureServer / GeoJSON / GeoPackage). Missing pipe attributes are gap-filled
    with LABELED demo defaults (inverts DEM-interpolated / slope-walked; diameters
    a default size; per-junction contributing sub-area a uniform demo value).
    Off-scope: DEM-synthesized overland pluvial mesh -> swmm_urban_flood;
    riverine/coastal inundation -> sfincs_flood.

    Use this when: the user wants to import / load / build a SWMM (or storm-sewer /
    storm-drain / sanitary-sewer) model FROM a GIS network (a shapefile /
    GeoPackage / GeoJSON of manholes + pipes, or a city's published storm-drain
    FeatureServer), and see where the network surcharges / floods under a design
    storm and how much flow reaches the outfall.

    Params:
        nodes_uri: URI of the NODE layer (manholes/inlets/outfalls as points) -
            an ``s3://`` object, ``file://`` / local path, an ``https://``
            ``.geojson``, or an ArcGIS ``FeatureServer/<n>`` / ``MapServer/<n>``
            layer URL (queried keylessly as GeoJSON). If this is a SINGLE combined
            file holding BOTH points and lines, leave ``conduits_uri`` unset and it
            is split by geometry.
        conduits_uri: URI of the CONDUIT layer (pipes as lines), same accepted
            forms. Optional when ``nodes_uri`` is a combined file.
        nodes_geojson / conduits_geojson: INLINE GeoJSON FeatureCollections, an
            alternative to the URIs (e.g. a network already in hand).
        bbox: OPTIONAL AOI ``(min_lon,min_lat,max_lon,max_lat)`` EPSG:4326 - used
            to fetch a DEM for filling missing node inverts and to frame the map.
            Inferred from the network extent when omitted.
        return_period_yr: design-storm return period, years (Atlas-14). Default 10.
        total_rain_depth_mm: OPTIONAL explicit total storm depth, mm (> 0);
            overrides the Atlas-14 lookup. When neither is available the run uses a
            labeled demo depth.
        storm_duration_hr: design-storm duration, hours (> 0). Default 2.
        rain_interval_min: hyetograph timestep, minutes (> 0). Default 5.
        fill_missing_inverts_from_dem: True (default) fetches a DEM to interpolate
            missing node inverts; False slope-walks them from known inverts only.
        compute_class: compute class. Default "standard".
        input_mode: run-mode lever. "user_gated" presents the resolved
            rainfall + the labeled gap-fills for review before the solve; "auto"
            (default) proceeds with them labeled.

    Returns:
        On success: ``SWMMNetworkLayerURI`` (a VECTOR network layer) carrying
        ``n_junctions`` / ``n_conduits`` / ``n_outfalls`` / ``peak_outfall_flow_cms``
        / ``total_outfall_volume_m3`` / ``n_flooded_nodes`` / ``n_surcharged_conduits``
        / ``max_node_hgl_m`` / ``continuity_error_pct`` / ``n_inverts_filled`` /
        ``n_topology_snapped`` / ``network_source`` (narrate these typed numbers only).
        On failure: ``{"status":"error","error_code","error_message"}``.
    """
    if not any([nodes_uri, nodes_geojson]):
        return {
            "status": "error",
            "error_code": "SWMM_NETWORK_PARAMS_INCOMPLETE",
            "error_message": (
                "swmm_network_import needs a network source: pass nodes_uri (+ "
                "conduits_uri), OR nodes_geojson (+ conduits_geojson), OR a single "
                "combined file as nodes_uri."
            ),
        }

    coerced_bbox = coerce_bbox_value(bbox) if bbox is not None else None

    try:
        result = await model_swmm_network_import(
            nodes_uri=nodes_uri,
            conduits_uri=conduits_uri,
            nodes_geojson=nodes_geojson,
            conduits_geojson=conduits_geojson,
            bbox=tuple(coerced_bbox) if coerced_bbox else None,
            return_period_yr=int(return_period_yr),
            total_rain_depth_mm=(float(total_rain_depth_mm) if total_rain_depth_mm else None),
            storm_duration_hr=float(storm_duration_hr),
            rain_interval_min=int(rain_interval_min),
            fill_missing_inverts_from_dem=bool(fill_missing_inverts_from_dem),
            input_mode=input_mode,
        )
        logger.info(
            "swmm_network_import complete layer_id=%s junctions=%d conduits=%d "
            "peak_outfall=%.4g CMS flooded=%d uri=%s",
            result.layer_id, result.n_junctions, result.n_conduits,
            result.peak_outfall_flow_cms, result.n_flooded_nodes, result.uri,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SWMMNetworkError as exc:
        logger.warning("swmm_network_import failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001 - defensive catch-all
        logger.exception("swmm_network_import unexpected failure")
        return {
            "status": "error",
            "error_code": "SWMM_NETWORK_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# Input loading: inline FC / s3 / file / https-geojson / ArcGIS FeatureServer.
# --------------------------------------------------------------------------- #
def _split_by_geometry(fc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a combined FeatureCollection into (points_fc, lines_fc)."""
    pts, lns = [], []
    for f in (fc or {}).get("features", []) or []:
        g = (f or {}).get("geometry") or {}
        t = g.get("type")
        if t == "Point":
            pts.append(f)
        elif t in ("LineString", "MultiLineString"):
            lns.append(f)
    return (
        {"type": "FeatureCollection", "features": pts},
        {"type": "FeatureCollection", "features": lns},
    )


def _load_fc(source: Any) -> dict[str, Any]:
    """Resolve a network layer source to an EPSG:4326 GeoJSON FeatureCollection.

    Accepts an inline FC dict, or a URI string: ``s3://`` / ``file://`` / a local
    path / an ``https://`` GeoJSON / an ArcGIS ``FeatureServer|MapServer/<n>``
    layer (queried keylessly as GeoJSON in EPSG:4326). SYNC (network + GDAL);
    the composer wraps it in ``asyncio.to_thread``.
    """
    if isinstance(source, dict):
        if source.get("type") == "FeatureCollection":
            return source
        raise SWMMNetworkError("SWMM_NETWORK_EMPTY", message="inline network is not a FeatureCollection")
    if not isinstance(source, str) or not source.strip():
        raise SWMMNetworkError("SWMM_NETWORK_EMPTY", message="empty network source")
    uri = source.strip()
    low = uri.lower()

    # ArcGIS FeatureServer / MapServer layer -> keyless GeoJSON query.
    if ("featureserver" in low or "mapserver" in low) and "/query" not in low:
        return _fetch_arcgis_layer_geojson(uri)

    # HTTPS GeoJSON (or an already-formed ArcGIS query URL).
    if low.startswith("http://") or low.startswith("https://"):
        return _fetch_http_geojson(uri)

    # s3:// object -> bytes -> geopandas.
    if low.startswith("s3://"):
        from trid3nt_server.cases.ingest_user_layer import _get_object_bytes
        data = _get_object_bytes(uri)
        return _bytes_to_fc(data, Path(uri.split("?")[0]).suffix or ".geojson")

    # file:// or a local path.
    path = uri[len("file://"):] if low.startswith("file://") else uri
    return _local_path_to_fc(path)


def _fetch_http_geojson(url: str, *, timeout: int = 60) -> dict[str, Any]:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "trid3nt-swmm-network/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - public GIS
        data = resp.read()
    try:
        fc = json.loads(data.decode("utf-8"))
    except Exception:
        # not JSON - maybe a downloadable vector file; hand to GDAL.
        return _bytes_to_fc(data, Path(url.split("?")[0]).suffix or ".geojson")
    if isinstance(fc, dict) and fc.get("type") == "FeatureCollection":
        return fc
    # An Esri JSON feature set (not GeoJSON) -> re-query with f=geojson.
    raise SWMMNetworkError(
        "SWMM_NETWORK_EMPTY",
        message=f"URL did not return a GeoJSON FeatureCollection: {url}",
    )


def _fetch_arcgis_layer_geojson(layer_url: str, *, page: int = 2000) -> dict[str, Any]:
    """Query an ArcGIS FeatureServer/MapServer layer as GeoJSON (EPSG:4326).

    Keyless, paginated (resultOffset) up to ``MAX_NETWORK_NODES`` features. No
    API key, no auth - only public open-data services.
    """
    import urllib.parse
    import urllib.request

    base = layer_url.split("?")[0].rstrip("/")
    feats: list[dict] = []
    offset = 0
    while True:
        params = {
            "where": "1=1", "outFields": "*", "outSR": "4326",
            "f": "geojson", "resultOffset": str(offset),
            "resultRecordCount": str(page),
        }
        url = f"{base}/query?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "trid3nt-swmm-network/1.0"})
        with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310 - public GIS
            chunk = json.loads(resp.read().decode("utf-8"))
        batch = chunk.get("features") or []
        feats.extend(batch)
        if len(batch) < page or len(feats) >= MAX_NETWORK_NODES * 2:
            break
        offset += page
    if not feats:
        raise SWMMNetworkError(
            "SWMM_NETWORK_EMPTY",
            message=f"ArcGIS layer returned no features: {layer_url}",
        )
    return {"type": "FeatureCollection", "features": feats}


def _bytes_to_fc(data: bytes, suffix: str) -> dict[str, Any]:
    import tempfile
    import geopandas as gpd

    with tempfile.NamedTemporaryFile(suffix=suffix or ".geojson", delete=True) as tf:
        tf.write(data)
        tf.flush()
        return _gdf_path_to_fc(tf.name)


def _local_path_to_fc(path: str) -> dict[str, Any]:
    return _gdf_path_to_fc(path)


def _gdf_path_to_fc(path: str) -> dict[str, Any]:
    import geopandas as gpd

    gdf = gpd.read_file(path)
    if gdf.crs is not None and str(gdf.crs).upper() not in ("EPSG:4326", "OGC:CRS84", "WGS 84"):
        gdf = gdf.to_crs("EPSG:4326")
    return gdf.__geo_interface__


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
async def model_swmm_network_import(
    *,
    nodes_uri: str | None,
    conduits_uri: str | None,
    nodes_geojson: dict[str, Any] | None,
    conduits_geojson: dict[str, Any] | None,
    bbox: tuple[float, float, float, float] | None,
    return_period_yr: int,
    total_rain_depth_mm: float | None,
    storm_duration_hr: float,
    rain_interval_min: int,
    fill_missing_inverts_from_dem: bool,
    input_mode: str | None = None,
    run_id: str | None = None,
) -> SWMMNetworkLayerURI:
    """Compose the network-import chain: load -> parse -> build -> solve -> publish."""
    from trid3nt_server.data.simulation.solver.solver import new_ulid

    emitter = current_emitter()
    rid = run_id or new_ulid()

    # --- Step 1: load node + conduit layers ---
    begin_substeps(emitter, 5 if not fill_missing_inverts_from_dem else 6)
    async with substep(emitter, "load_network"):
        nodes_fc, conduits_fc, network_source = await asyncio.to_thread(
            _resolve_network_layers,
            nodes_uri, conduits_uri, nodes_geojson, conduits_geojson,
        )

    n_node_feats = len(nodes_fc.get("features", []))
    if n_node_feats > MAX_NETWORK_NODES:
        raise SWMMNetworkError(
            "SWMM_NETWORK_TOO_LARGE",
            message=(
                f"the imported network has {n_node_feats} nodes (> cap "
                f"{MAX_NETWORK_NODES}); retry over a SMALLER AOI or a single "
                f"neighbourhood's storm-drain layer."
            ),
        )

    # --- Step 2 (optional): DEM for missing-invert filling ---
    dem_path: str | None = None
    inferred_bbox = bbox or _bbox_from_fc(nodes_fc)
    if fill_missing_inverts_from_dem and inferred_bbox is not None:
        async with substep(emitter, "fetch_dem"):
            try:
                from trid3nt_server.workflows.swmm.urban_flood.urban_flood import (
                    _fetch_dem_for_urban,
                )
                dem_path, _dem_src = await asyncio.to_thread(_fetch_dem_for_urban, inferred_bbox)
            except Exception as exc:  # noqa: BLE001 - DEM is an enhancement
                logger.warning("swmm_network_import: DEM fetch failed (%s); slope-walk inverts", exc)
                dem_path = None

    if inferred_bbox is not None and emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(inferred_bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("swmm_network_import: zoom-to failed: %s", exc)

    # --- Step 3: resolve the design-storm depth (Atlas-14 or explicit/demo) ---
    depth_mm, depth_basis = await _resolve_storm_depth(
        emitter, inferred_bbox, return_period_yr, storm_duration_hr, total_rain_depth_mm
    )

    # --- input review: rainfall + the labeled gap-fills ---
    _review = await gate_input_review(
        tool_name="swmm_network_import", mode=input_mode,
        entries=[
            SyntheticInput(
                param="total_rain_depth_mm", value=round(depth_mm, 1), units="mm",
                basis=depth_basis, consequence="scenario",
                real_source_if_any=("lookup_precip_return_period (NOAA Atlas-14)"
                                    if depth_basis == "fetched" else None),
                note=f"{return_period_yr}-yr/{storm_duration_hr:.0f}-hr design storm",
            ),
            SyntheticInput(
                param="junction_subarea", value="uniform demo", basis="default_demo", consequence="physics",
                note="imported network has no sub-catchment delineation; each junction "
                     "drains one uniform demo sub-area of the storm",
            ),
        ],
        params={"total_rain_depth_mm": float(depth_mm)},
    )
    if _review.cancelled:
        raise SWMMNetworkError("USER_INPUT_CANCELLED", message=f"swmm_network_import {_review.cancel_reason}")
    _rv = _review.params.get("total_rain_depth_mm")
    if _rv is not None:
        depth_mm = float(_rv)

    # --- Step 4: parse + build + solve (all sync -> off the event loop) ---
    async with substep(emitter, "build_network_deck"):
        build, run = await asyncio.to_thread(
            _parse_build_solve, nodes_fc, conduits_fc, dem_path, depth_mm,
            storm_duration_hr, rain_interval_min, rid,
        )

    async with substep(emitter, "solve_network"):
        pass  # solve happened inside _parse_build_solve; substep frames the timeline

    # --- Step 5: publish the network vector layer ---
    async with substep(emitter, "publish_network"):
        layer = await asyncio.to_thread(
            _publish_network_layer, build, run, rid, network_source, depth_basis,
            return_period_yr, storm_duration_hr, inferred_bbox,
        )

    if emitter is not None:
        try:
            safe = layer.model_copy(update={"bbox": None})  # avoid a competing zoom
            await emitter.add_loaded_layer(safe)
        except Exception as exc:  # noqa: BLE001
            logger.warning("swmm_network_import: add_loaded_layer failed: %s", exc)
        if inferred_bbox is not None:
            try:
                await emitter.emit_map_command("zoom-to", {"bbox": list(inferred_bbox)})
            except Exception:  # noqa: BLE001
                pass

    return layer


def _resolve_network_layers(
    nodes_uri, conduits_uri, nodes_geojson, conduits_geojson
) -> tuple[dict, dict, str]:
    """Load the node + conduit FCs, handling the combined-single-file case."""
    if nodes_geojson is not None:
        nodes_fc = nodes_geojson
        source = "inline GeoJSON"
    else:
        nodes_fc = _load_fc(nodes_uri)
        source = _source_label(nodes_uri)
    if conduits_geojson is not None:
        conduits_fc = conduits_geojson
    elif conduits_uri:
        conduits_fc = _load_fc(conduits_uri)
    else:
        # combined single file: split the node FC by geometry.
        pts, lns = _split_by_geometry(nodes_fc)
        if lns.get("features"):
            nodes_fc, conduits_fc = pts, lns
        else:
            raise SWMMNetworkError(
                "SWMM_NETWORK_EMPTY",
                message="no conduit layer given and the node file has no line features",
            )
    return nodes_fc, conduits_fc, source


def _source_label(uri: str | None) -> str:
    if not uri:
        return "network"
    low = uri.lower()
    if "featureserver" in low or "mapserver" in low:
        return "ArcGIS FeatureServer (public GIS)"
    if low.startswith("s3://"):
        return "user upload"
    return "user-provided GIS file"


def _parse_build_solve(
    nodes_fc, conduits_fc, dem_path, depth_mm, storm_duration_hr, rain_interval_min, rid
):
    import tempfile

    parsed = parse_network_features(nodes_fc, conduits_fc, dem_path=dem_path)
    workdir = Path(tempfile.mkdtemp(prefix=f"swmm-net-{rid}-"))
    inp = str(workdir / "network.inp")
    build = build_network_inp(
        parsed, out_inp_path=inp, total_rain_depth_mm=depth_mm,
        storm_duration_hr=storm_duration_hr, rain_interval_min=rain_interval_min,
    )
    run = run_network_deck(build)
    return build, run


async def _resolve_storm_depth(
    emitter, bbox, return_period_yr, storm_duration_hr, total_rain_depth_mm
) -> tuple[float, str]:
    if total_rain_depth_mm is not None:
        return float(total_rain_depth_mm), "user"
    if bbox is not None:
        async with substep(emitter, "lookup_precip_return_period"):
            try:
                from trid3nt_server.workflows.swmm.urban_flood.urban_flood import (
                    _atlas14_total_depth_mm,
                )
                depth = await asyncio.to_thread(
                    _atlas14_total_depth_mm, bbox, return_period_yr, storm_duration_hr
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("swmm_network_import: Atlas-14 lookup failed (%s)", exc)
                depth = None
        if depth is not None:
            return float(depth), "fetched"
    # labeled demo depth (a network with no AOI to look up against).
    return 90.0, "default_demo"


def _publish_network_layer(
    build, run, rid, network_source, depth_basis, return_period_yr,
    storm_duration_hr, bbox,
) -> SWMMNetworkLayerURI:
    """Serialize the network to GeoJSON, upload to the runs bucket, return the layer."""
    fc = network_to_geojson_4326(build, run)
    uri = _upload_network_geojson(fc, rid)

    labels: list[str] = []
    if build.n_inverts_filled:
        labels.append(f"{build.n_inverts_filled} inverts gap-filled")
    if build.n_topology_snapped:
        labels.append(f"{build.n_topology_snapped} endpoints snapped")
    if build.n_diameters_defaulted:
        labels.append(f"{build.n_diameters_defaulted} diameters defaulted")
    label_suffix = f" ({'; '.join(labels)})" if labels else ""

    provenance = [
        SyntheticInput(
            param="network_geometry", value="real", basis="fetched",
            real_source_if_any=network_source,
            note="imported municipal storm-drain nodes + conduits",
        ),
        SyntheticInput(
            param="total_rain_depth_mm",
            value=round(build.hyetograph.total_depth_mm, 1)
            if getattr(build, "hyetograph", None) is not None
            and hasattr(build.hyetograph, "total_depth_mm") else None,
            units="mm", basis=depth_basis, consequence="scenario",
            note=f"{return_period_yr}-yr/{storm_duration_hr:.0f}-hr design storm loading",
        ),
    ]
    if build.n_inverts_filled:
        provenance.append(SyntheticInput(
            param="node_inverts", value=f"{build.n_inverts_filled} filled",
            basis="default_demo", consequence="physics",
            note="nodes with no GIS invert were DEM-interpolated / slope-walked",
        ))

    return SWMMNetworkLayerURI(
        layer_id=f"swmm-network-{rid}",
        name=f"Imported storm-drain network{label_suffix}",
        layer_type="vector",
        uri=uri,
        style_preset="swmm_network",
        role="primary",
        bbox=tuple(bbox) if bbox else None,
        fallback_note=(
            f"Imported {network_source}: {build.n_junctions} junctions, "
            f"{build.n_conduits} conduits, {build.n_outfalls} outfall(s), "
            f"{build.total_pipe_length_m:.0f} m of pipe. Design storm loading."
        ),
        synthetic_inputs=provenance,
        n_junctions=build.n_junctions,
        n_conduits=build.n_conduits,
        n_outfalls=build.n_outfalls,
        total_pipe_length_m=build.total_pipe_length_m,
        peak_outfall_flow_cms=run.peak_outfall_flow_cms,
        total_outfall_volume_m3=run.total_outfall_volume_m3,
        n_flooded_nodes=run.n_flooded_nodes,
        n_surcharged_conduits=run.n_surcharged_conduits,
        max_node_hgl_m=run.max_node_hgl_m,
        continuity_error_pct=run.continuity_error_pct,
        n_inverts_filled=build.n_inverts_filled,
        n_topology_snapped=build.n_topology_snapped,
        network_source=network_source,
    )


def _upload_network_geojson(fc: dict[str, Any], rid: str) -> str:
    from trid3nt_server.data.simulation.solver.solver import _get_runs_bucket, _get_s3_client

    bucket = _get_runs_bucket()
    key = f"{rid}/network.geojson"
    _get_s3_client().put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps(fc).encode("utf-8"),
        ContentType="application/geo+json",
    )
    return f"s3://{bucket}/{key}"


def _bbox_from_fc(fc: dict[str, Any]) -> tuple[float, float, float, float] | None:
    xs, ys = [], []
    for f in (fc or {}).get("features", []) or []:
        g = (f or {}).get("geometry") or {}
        c = g.get("coordinates")
        if g.get("type") == "Point" and isinstance(c, (list, tuple)) and len(c) >= 2:
            xs.append(c[0]); ys.append(c[1])
    if not xs:
        return None
    pad = 0.001
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)
