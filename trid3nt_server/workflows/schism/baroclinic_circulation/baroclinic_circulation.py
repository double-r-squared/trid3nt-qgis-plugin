"""Engine template ``schism_baroclinic_circulation`` -- density-driven 3D estuary
circulation + stratification (SCHISM engine #12/0126).

The LLM-facing exposure of SCHISM's 3D BAROCLINIC pathway (ibc=0): the semi-implicit
unstructured-grid hydro core run in three dimensions with the T/S density field
driving a gravitational (estuarine) circulation. A coarse georeferenced channel over
a real US estuary footprint is forced by an estuarine salinity GRADIENT (fresh
landward -> salty seaward), a sustained freshwater RIVER point source, and a tidal
ocean boundary; the water column stratifies (a fresher surface over a salty
bottom -- the salt wedge). The deliverable is a SURFACE SALINITY map + a BOTTOM
SALINITY map + a stratification metric (top-minus-bottom salinity).

Fidelity / honesty floor: the mesh + bathymetry are an IDEALIZED COARSE
demonstration channel (a graded lon/lat lattice, a linearly-deepening idealized
bathymetry), NOT a surveyed estuary -- it proves the 3D baroclinic pathway executes
end-to-end and emits a physically-sane stratified field, it is NOT a calibrated
site study. The published-case validation (SCHISM Test_CORIE, the Columbia River
estuary 28-day 3D baroclinic hindcast vs ADCP/CTD stations) is the NATE-gated heavy
live drive (sec 2), recorded as pending in.

Determinism boundary (invariant 1): every salinity / stratification number the agent
narrates comes from the typed ``SchismBaroclinicLayerURI`` fields the postprocess
computed from the scribed ``salinity`` netCDF -- never free-generated. SCHISM is
LOCAL-DOCKER ONLY, so the composer dispatches through the generic run_solver seam
(the ``hydro`` executable variant run in 3D baroclinic mode).
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
from trid3nt_contracts.schism_contracts import (
    SCHISM_INPUT_INVALID,
    SCHISM_SOLVE_FAILED,
    SchismBaroclinicLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.workflows.schism import deck_authoring
from trid3nt_server.workflows.schism import postprocess_schism as pp
from trid3nt_server.workflows.schism._template_card import TemplateCard
from trid3nt_server.workflows.schism.run_schism import SCHISM_BAROCLINIC_SOLVER_NAME

logger = logging.getLogger(
    "trid3nt_server.workflows.schism.baroclinic_circulation.baroclinic_circulation"
)

__all__ = [
    "schism_baroclinic_circulation",
    "model_schism_baroclinic_circulation",
    "SchismBaroclinicError",
    "TEMPLATE_CARD",
]

#: A default US estuary footprint (Galveston Bay, TX) when neither location_query
#: nor bbox is supplied -- a labeled default_demo AOI, not an invented site. A
#: broad, well-covered open-water bay (Trinity/San Jacinto river inflow, the
#: Bolivar Roads Gulf mouth at the SOUTH edge) so the shoreline-clipped mesh is a
#: genuine water body, not a mostly-land box.
_DEFAULT_ESTUARY_BBOX: tuple[float, float, float, float] = (-94.95, 29.35, -94.70, 29.75)
_DEFAULT_ESTUARY_NAME: str = "Galveston Bay (TX)"

#: The LOUD coarse-demonstration honesty floor stamped on every result.
_DEMO_NOTE: str = (
    "3D BAROCLINIC estuary circulation on a COARSE IDEALIZED demonstration channel "
    "(a graded lon/lat lattice with linearly-deepening idealized bathymetry over the "
    "AOI footprint) forced by an estuarine salinity gradient + a freshwater river "
    "source + a tidal ocean boundary. It proves the density-driven 3D pathway "
    "(SZ vertical grid, ibc=0, T/S transport, river inflow) executes end-to-end and "
    "produces a physically-sane stratified salt wedge -- it is NOT a surveyed, "
    "calibrated estuary. The calibrated validation is the SCHISM Test_CORIE Columbia "
    "River estuary 28-day 3D baroclinic hindcast (NATE-gated heavy live drive). For "
    "barotropic tides use schism_tidal_hydro; for coupled waves use schism_coupled_waves."
)


class SchismBaroclinicError(RuntimeError):
    """Raised when the baroclinic chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "density-driven 3D BAROCLINIC estuary circulation + stratification: given a "
        "river discharge + tidal forcing on a US estuary, what surface/bottom "
        "salinity and vertical stratification (salt wedge) result -- a surface "
        "salinity map + bottom salinity map + a top-minus-bottom stratification metric"
    ),
    required_inputs=[],  # a labeled default US estuary footprint is self-contained
    knobs=(
        "location_query/bbox, river_discharge_m3s, ocean_salinity_psu, sim_days, "
        "ocean_side, input_mode"
    ),
)

_METADATA = AtomicToolMetadata(
    name="schism_baroclinic_circulation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="schism",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def schism_baroclinic_circulation(
    location_query: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    river_discharge_m3s: float = 500.0,
    ocean_salinity_psu: float = 33.0,
    sim_days: float = 2.0,
    ocean_side: str = "south",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SchismBaroclinicLayerURI | dict[str, Any]:
    """A density-driven 3D BAROCLINIC estuary circulation + STRATIFICATION simulation (SCHISM).

    Fidelity: SCHISM (the semi-implicit cross-scale unstructured-grid hydrodynamic
    core behind NOAA STOFS-3D) run in 3D BAROCLINIC mode (ibc=0) with an SZ vertical
    grid, so the temperature/salinity density field drives a gravitational
    (estuarine) circulation. Forced by an estuarine salinity gradient + a sustained
    freshwater river source + a tidal ocean boundary; the water column stratifies
    into a fresher surface over a salty bottom (the salt wedge). Returns a SURFACE
    SALINITY map + a BOTTOM SALINITY map + a top-minus-bottom stratification metric.

    THE tool for "3D baroclinic estuary circulation", "estuarine salinity
    stratification / salt wedge", "density-driven circulation in a shelf-estuary",
    "surface vs bottom salinity in an estuary", "how far does salt intrude up the
    estuary", "river plume + tidal mixing stratification". A COARSE demonstration
    geometry proving the 3D baroclinic pathway -- the calibrated Columbia-River
    CORIE 28-day validation is NATE-gated.

    Do NOT use this for:
        - BAROTROPIC tides / max water-surface elevation (no density) -- use
          ``schism_tidal_hydro``.
        - COUPLED waves / nearshore wave transformation -- use
          ``schism_coupled_waves``.
        - FAST arbitrary-AOI flood screening -- use ``sfincs_flood``.
        - Transport-scheme numerical-mixing V&V on the idealized channel -- use
          ``schism_transport_validation``.

    Params:
        location_query: a US estuary place name (geocoded to a bbox footprint). If
            omitted with no bbox, a labeled default (Delaware Bay) is used.
        bbox: explicit EPSG:4326 (min_lon, min_lat, max_lon, max_lat) estuary
            footprint (wins over location_query).
        river_discharge_m3s: the freshwater river point-source discharge (m3/s,
            default 500). Clamped (0, 50000].
        ocean_salinity_psu: the seaward-end / ocean salinity (psu, default 33).
            Clamped (0, 40].
        sim_days: 3D baroclinic run length in days (default 2 -- a coarse spin-up
            smoke). Clamped [0.5, 5]. The calibrated CORIE case is 28 days
            (NATE-gated).
        ocean_side: which mesh edge is the seaward (tidal + salty) boundary
            ("south"|"north"|"east"|"west"; default "south"). The river source sits
            on the opposite (landward) edge.
        input_mode: run-mode lever. "user_gated" reviews the resolved
            forcing + coarse-geometry basis before solving; "auto" (default)
            proceeds labeled.

    Returns:
        On success: ``SchismBaroclinicLayerURI`` -- the surface-salinity COG
        (primary) beside the bottom-salinity COG + the 3D mesh preview. Carries
        ``surface_salinity_min_psu`` / ``surface_salinity_max_psu`` /
        ``bottom_salinity_max_psu`` / ``max_stratification_psu`` /
        ``mean_stratification_psu`` / ``river_discharge_m3s`` / ``n_layers`` /
        ``sim_days`` (narrate these typed numbers only -- invariant 1).
        On failure: dict with ``status="error"`` + ``error_code`` + ``error_message``.

    ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"``.
    """
    try:
        river_discharge_m3s = float(river_discharge_m3s)
        ocean_salinity_psu = float(ocean_salinity_psu)
        sim_days = float(sim_days)
    except (TypeError, ValueError):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "river_discharge_m3s / ocean_salinity_psu / sim_days must be numbers"}
    if not (0.0 < river_discharge_m3s <= 50000.0):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "river_discharge_m3s in (0, 50000]"}
    if not (0.0 < ocean_salinity_psu <= 40.0):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "ocean_salinity_psu in (0, 40]"}
    if not (0.5 <= sim_days <= 5.0):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "sim_days in [0.5, 5] (a coarse baroclinic smoke; 28-day CORIE is NATE-gated)"}
    if ocean_side not in ("south", "north", "east", "west"):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "ocean_side must be one of south/north/east/west"}

    logger.info(
        "schism_baroclinic_circulation loc=%s bbox=%s Q=%.0f Socean=%.1f days=%.2g side=%s",
        location_query, bbox, river_discharge_m3s, ocean_salinity_psu, sim_days, ocean_side,
    )
    try:
        result = await model_schism_baroclinic_circulation(
            location_query=location_query, bbox=bbox,
            river_discharge_m3s=river_discharge_m3s, ocean_salinity_psu=ocean_salinity_psu,
            sim_days=sim_days, ocean_side=ocean_side, input_mode=input_mode,
        )
        if isinstance(result, dict):
            return result
        logger.info(
            "schism_baroclinic_circulation complete layer_id=%s surf=[%.2f,%.2f] "
            "strat_max=%s strat_mean=%s uri=%s",
            result.layer_id, result.surface_salinity_min_psu, result.surface_salinity_max_psu,
            result.max_stratification_psu, result.mean_stratification_psu, result.uri,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SchismBaroclinicError as exc:
        logger.warning("schism_baroclinic_circulation failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("schism_baroclinic_circulation unexpected failure")
        return {"status": "error", "error_code": "SCHISM_INTERNAL_ERROR", "error_message": str(exc)}


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from trid3nt_server.data.publish_layer.publish_layer import (
    PublishLayerError,
    publish_layer,
)
from trid3nt_server.emission.layer_uri_emit import publish_input_layer


def _cache_bucket() -> str:
    b = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not b:
        raise SchismBaroclinicError(
            SCHISM_SOLVE_FAILED, "TRID3NT_CACHE_BUCKET must be set to stage the SCHISM manifest."
        )
    return b


def _resolve_bbox(
    location_query: str | None, bbox: Any,
) -> tuple[tuple[float, float, float, float], str, str]:
    """Return ``(bbox, aoi_label, basis)`` -- explicit bbox, geocoded place, or default."""
    if bbox is not None:
        bb = tuple(float(v) for v in bbox)
        if len(bb) != 4:
            raise SchismBaroclinicError(SCHISM_INPUT_INVALID, "bbox must be 4 floats (min_lon,min_lat,max_lon,max_lat)")
        return bb, f"bbox {bb}", "user"
    if location_query:
        from trid3nt_server.data.fetchers.socioeconomic.geocode_location.geocode_location import (
            geocode_location,
        )
        geo = geocode_location(location_query)
        bb = geo.get("bbox")
        if not bb or len(bb) != 4:
            raise SchismBaroclinicError(
                SCHISM_INPUT_INVALID, f"geocode_location({location_query!r}) returned no bbox")
        return tuple(float(v) for v in bb), location_query, "user"
    return _DEFAULT_ESTUARY_BBOX, _DEFAULT_ESTUARY_NAME, "default_demo"


def _stage_manifest(deck_files: list[Path], run_tag: str, *, ncompute: int, nscribe: int) -> str:
    """Upload the deck as manifest inputs[] (variant='hydro'); return its uri."""
    from trid3nt_server.data.simulation.solver.solver import _get_s3_client

    cache_bucket = _cache_bucket()
    s3 = _get_s3_client()
    inputs = []
    for f in deck_files:
        key = f"schism/{run_tag}/{f.name}"
        with open(f, "rb") as fh:
            s3.put_object(Bucket=cache_bucket, Key=key, Body=fh.read())
        inputs.append({"gs_uri": f"s3://{cache_bucket}/{key}", "dest": f.name})
    manifest = {
        "variant": "hydro",
        "ncompute": int(ncompute),
        "nscribe": int(nscribe),
        "run_id": run_tag,
        "inputs": inputs,
        "schism_args": [],
        "outputs": ["outputs/*.nc", "outputs/staout_*", "schism_metrics.json"],
    }
    key = f"schism/{run_tag}/manifest.json"
    s3.put_object(Bucket=cache_bucket, Key=key,
                  Body=json.dumps(manifest, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


#: USGS NHDPlus_HR MapServer NHDArea (water-body polygons) query endpoint -- the
#: same vector source the TELEMAC river-dye pipeline samples for real banks. A fast
#: (~2 s) polygon query, far cheaper than a full-resolution coastal DEM fetch.
_NHDAREA_URL = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/"
    "MapServer/8/query"
)


def _build_water_mask(bbox: tuple[float, float, float, float]) -> Any:
    """Return a vectorized ``water_mask_fn(lon_arr, lat_arr) -> bool_arr`` from the
    real USGS NHDArea WATER-body polygons over the AOI, or ``None`` when none cover
    it.

    Queries NHDPlus_HR NHDArea (bay / estuary / river polygons) for the bbox and
    unions them (holes = islands), so a lon/lat point tests WATER iff it lies inside
    the real water body. The estuary mesh is then clipped to the true shoreline and
    the salinity raster never paints land. A vector query (~2 s) -- much cheaper
    than a full-resolution CUDEM fetch, and it is exactly the shoreline. Best-effort:
    any failure / no coverage returns ``None`` and the caller meshes the full
    rectangle with a loud note (never a silent dead-end)."""
    import json
    import urllib.parse
    import urllib.request

    import numpy as np

    try:
        params = urllib.parse.urlencode({
            "geometry": ",".join(f"{v:.6f}" for v in bbox),
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "ftype", "f": "geojson",
            # server-side simplification + a record cap: the coarse demo mesh needs
            # the shoreline shape, not every vertex of a large estuary polygon.
            "maxAllowableOffset": "0.0008", "resultRecordCount": "200",
        })
        req = urllib.request.Request(f"{_NHDAREA_URL}?{params}",
                                     headers={"User-Agent": "trid3nt-local (agent@trid3nt.dev)"})
        with urllib.request.urlopen(req, timeout=45.0) as r:
            data = json.loads(r.read().decode("utf-8"))

        import shapely
        import shapely.geometry as sg
        from shapely.ops import unary_union

        polys = []
        for f in data.get("features") or []:
            g = f.get("geometry") or {}
            if g.get("type") == "Polygon":
                rings_list = [g.get("coordinates") or []]
            elif g.get("type") == "MultiPolygon":
                rings_list = g.get("coordinates") or []
            else:
                continue
            for rings in rings_list:
                if rings and len(rings[0]) >= 4:
                    holes = [h for h in rings[1:] if len(h) >= 4]
                    polys.append(sg.Polygon(rings[0], holes=holes))
        polys = [p.buffer(0) for p in polys if not p.is_empty]
        if not polys:
            logger.warning("baroclinic water mask: no NHDArea water polygon covers "
                           "%s -- meshing the full rectangle", bbox)
            return None
        union = unary_union(polys)

        def water_mask_fn(lons: Any, lats: Any) -> Any:
            lons = np.asarray(lons, dtype="float64")
            lats = np.asarray(lats, dtype="float64")
            return np.asarray(shapely.contains_xy(union, lons, lats), dtype=bool)

        return water_mask_fn
    except Exception as exc:  # noqa: BLE001 -- best-effort; fall back to rectangle
        logger.warning("baroclinic water mask build failed (%s) -- meshing the full "
                       "rectangle", exc)
        return None


def _download_run_output(run_id: str, rel_key: str) -> str | None:
    from trid3nt_server.data.simulation.solver.solver import (
        _get_runs_bucket, _get_s3_client,
    )
    try:
        s3 = _get_s3_client()
        obj = s3.get_object(Bucket=_get_runs_bucket(), Key=f"{run_id}/{rel_key}")
        tmp = tempfile.NamedTemporaryFile(suffix="_" + Path(rel_key).name, delete=False)
        tmp.write(obj["Body"].read())
        tmp.close()
        return tmp.name
    except Exception as exc:  # noqa: BLE001
        logger.info("schism baroclinic: run output miss %s/%s: %s", run_id, rel_key, exc)
        return None


def _runs_uri(run_id: str, rel_key: str) -> str:
    from trid3nt_server.data.simulation.solver.solver import _get_runs_bucket
    return f"s3://{_get_runs_bucket()}/{run_id}/{rel_key}"


def _parse_hgrid_nodes_cells(gr3_text: str) -> tuple[Any, Any, Any]:
    """Parse ``(points_lonlat (N,2), tris (M,3) 0-based, depths_down (N,))`` from
    an hgrid.gr3 string -- the supplied-mesh geometry the deck author consumes."""
    import numpy as np

    lines = gr3_text.splitlines()
    n_elem, n_node = (int(v) for v in lines[1].split()[:2])
    pts = np.empty((n_node, 2), dtype=float)
    depths = np.empty(n_node, dtype=float)
    for i in range(n_node):
        p = lines[2 + i].split()
        pts[i, 0], pts[i, 1], depths[i] = float(p[1]), float(p[2]), float(p[3])
    ebase = 2 + n_node
    tris = np.empty((n_elem, 3), dtype=np.int64)
    for e in range(n_elem):
        p = lines[ebase + e].split()
        # "id 3 n1 n2 n3" (1-based nodes) -> 0-based triangle
        tris[e] = (int(p[2]) - 1, int(p[3]) - 1, int(p[4]) - 1)
    return pts, tris, depths


async def _schism_mesh_precondition_gate(
    input_mode: str | None,
) -> tuple[tuple[Any, Any, Any] | None, str | None, str | None]:
    """Offer this case's SCHISM mesh to the baroclinic solve.

    Returns ``(supplied_mesh | None, ocean_side | None, note | None)``: a parsed
    ``(points_lonlat, tris, depths_down)`` tuple when a case mesh was discovered,
    SCHISM-compatible (a designated open boundary), and accepted; ``None`` when
    there is no usable mesh, an incompatible one was skipped, or the user declined
    -- the caller then authors the idealized channel. ``ocean_side`` is taken from
    the mesh's designated open boundary. NEVER raises into the solve path."""
    import tempfile

    from trid3nt_server.workflows.mesh.precondition_gate import (
        gate_supplied_mesh, materialize_supplied_mesh,
    )

    try:
        emitter = current_emitter()
        loaded_mesh_uris = (
            [ly.uri for ly in emitter.loaded_layers
             if getattr(ly, "layer_type", None) == "mesh"]
            if emitter is not None else [])
        s3 = None
        try:
            from trid3nt_server.data.simulation.solver.solver import (
                _get_s3_client,
            )
            s3 = _get_s3_client()
        except Exception:  # noqa: BLE001 -- sidecar fallback is optional
            s3 = None
        decision = await gate_supplied_mesh(
            tool_name="schism_baroclinic_circulation", engine="schism",
            input_mode=input_mode, loaded_mesh_uris=loaded_mesh_uris, s3_client=s3)
        if not decision.use or decision.artifact is None:
            return None, None, decision.note
        art = decision.artifact
        ocean_side = str((art.open_boundary_info or {}).get("open_boundary_side")
                         or "").strip().lower() or None

        def _materialize():
            rundir = tempfile.mkdtemp(prefix="schism-baroclinic-suppliedmesh-")
            gr3_local = materialize_supplied_mesh(art, rundir, s3, engine="schism")
            return _parse_hgrid_nodes_cells(Path(gr3_local).read_text(encoding="utf-8"))

        supplied_mesh = await asyncio.to_thread(_materialize)
        logger.info(
            "schism baroclinic: consuming case mesh %r (%d elements, open side=%s) "
            "instead of the idealized lattice", art.name, art.element_count, ocean_side)
        return supplied_mesh, ocean_side, decision.note
    except Exception as exc:  # noqa: BLE001 -- gate must never break the solve
        logger.warning(
            "schism baroclinic mesh precondition gate failed (%s); authoring the "
            "idealized channel", exc, exc_info=True)
        return None, None, None


async def model_schism_baroclinic_circulation(
    *, location_query, bbox, river_discharge_m3s, ocean_salinity_psu, sim_days,
    ocean_side, input_mode,
) -> SchismBaroclinicLayerURI | dict[str, Any]:
    """Author the estuary deck -> input gate -> 3D baroclinic solve -> salinity postprocess -> publish."""
    emitter = current_emitter()
    begin_substeps(emitter, 3)  # run_solver + postprocess + publish

    aoi_bbox, aoi_label, aoi_basis = _resolve_bbox(location_query, bbox)

    # Precondition gate: if this case holds a SCHISM-compatible mesh
    # (built explicitly by generate_mesh with an open boundary), offer to solve on
    # it -- real shoreline + real bathymetry replace the idealized lattice.
    # Accepted -> the supplied-mesh path; declined/absent/incompatible -> the
    # idealized channel below, unchanged.
    supplied_mesh, gate_ocean_side, mesh_gate_note = (
        await _schism_mesh_precondition_gate(input_mode))
    if supplied_mesh is not None:
        ocean_side = gate_ocean_side or ocean_side
        water_mask_fn = None  # the supplied mesh IS the (real) shoreline
    else:
        # Real coastline clip: mesh only the estuary WATER (no salinity over land).
        water_mask_fn = await asyncio.to_thread(_build_water_mask, aoi_bbox)

    workdir = Path(tempfile.mkdtemp(prefix="schism-baroclinic-deck-"))
    deck = deck_authoring.author_baroclinic_estuary_deck(
        workdir, bbox=aoi_bbox, constituents=["M2"], tidal_amplitude_m=0.6,
        sim_days=sim_days, ocean_side=ocean_side,
        river_discharge_m3s=river_discharge_m3s, ocean_salinity_psu=ocean_salinity_psu,
        water_mask_fn=water_mask_fn, supplied_mesh=supplied_mesh,
    )
    shoreline_clipped = bool(deck.get("shoreline_clipped"))
    used_supplied_mesh = supplied_mesh is not None
    logger.info("baroclinic mesh: %d nodes x %d layers, shoreline_clipped=%s, "
                "supplied_mesh=%s", deck["n_nodes"], deck["n_layers"],
                shoreline_clipped, used_supplied_mesh)

    review_entries = [
        SyntheticInput(param="estuary_aoi", value=aoi_label, basis=aoi_basis,
                       note="the georeferenced estuary footprint the coarse channel spans"),
        SyntheticInput(param="mesh_geometry",
                       value=(f"user-supplied mesh ({deck['n_nodes']} nodes x {deck['n_layers']} layers)"
                              if used_supplied_mesh else
                              f"coarse {'shoreline-clipped' if shoreline_clipped else 'rectangular'} "
                              f"channel ({deck['n_nodes']} nodes x {deck['n_layers']} layers)"),
                       basis="user" if used_supplied_mesh else "default_demo",
                       note=((mesh_gate_note or "consumed a user-supplied mesh (generate_mesh): real "
                              "shoreline + real sampled bathymetry; the salinity IC gradient + tidal/"
                              "river forcing remain idealized")
                             if used_supplied_mesh else
                             "a lon/lat lattice clipped to the real WATER body (coastline from "
                             "topo-bathymetry) with linearly-deepening idealized bathymetry -- the "
                             "SHORELINE is real, the bathymetry is NOT surveyed"
                             if shoreline_clipped else
                             "a graded lon/lat lattice + linearly-deepening idealized bathymetry -- "
                             "NOT surveyed (coastline clip unavailable for this AOI)")),
        SyntheticInput(param="river_discharge", value=round(river_discharge_m3s, 1), units="m3/s",
                       basis="user" if river_discharge_m3s != 500.0 else "default_demo",
                       note="freshwater point source at the landward edge (S=0)"),
        SyntheticInput(param="ocean_salinity", value=round(ocean_salinity_psu, 1), units="psu",
                       basis="user" if ocean_salinity_psu != 33.0 else "default_demo",
                       note="seaward-end salinity (the estuarine gradient endpoint + boundary)"),
        SyntheticInput(param="baroclinic_config", value="ibc=0 (3D baroclinic), SZ vgrid, TVD transport",
                       basis="default_demo",
                       note="the 3D density-driven pathway on the hydro-core binary"),
        SyntheticInput(param="sim_days", value=round(sim_days, 3), units="d",
                       basis="user" if sim_days != 2.0 else "default_demo",
                       note="coarse baroclinic spin-up smoke; the 28-day CORIE V&V is NATE-gated"),
    ]

    review = await gate_input_review(
        tool_name="schism_baroclinic_circulation", mode=input_mode, entries=review_entries,
        params={"river_discharge_m3s": river_discharge_m3s, "ocean_salinity_psu": ocean_salinity_psu,
                "sim_days": sim_days, "ocean_side": ocean_side},
    )
    if not review.proceed:
        return {"status": "error", "error_code": "SCHISM_INPUT_REVIEW_CANCELLED",
                "error_message": review.cancel_reason or "input review not approved; the solver did not run"}

    run_tag = new_ulid()
    # nscribe must cover the scribed 3D outputs (elevation + salinity + temperature
    # + depth-avg vel) -- BAROCLINIC_NSCRIBE compute ranks feed the scribes.
    manifest_uri = await asyncio.to_thread(
        _stage_manifest, deck["files"], run_tag, ncompute=3,
        nscribe=deck_authoring.BAROCLINIC_NSCRIBE,
    )
    logger.info("model_schism_baroclinic_circulation staged manifest run_tag=%s files=%d uri=%s",
                run_tag, len(deck["files"]), manifest_uri)

    from trid3nt_server.data.simulation.solver.solver import (
        run_solver, wait_for_completion,
    )
    handle = run_solver(solver=SCHISM_BAROCLINIC_SOLVER_NAME, model_setup_uri=manifest_uri,
                        compute_class="medium")
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=SCHISM_BAROCLINIC_SOLVER_NAME, handle=handle, compute_class="medium",
    )
    run_result = None
    try:
        async with substep(emitter, "run_solver"):
            run_result = await wait_for_completion(handle)
    except asyncio.CancelledError:
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        raise SchismBaroclinicError(
            SCHISM_SOLVE_FAILED,
            "SCHISM 3D baroclinic solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}",
        )
    batch_run_id = getattr(run_result, "run_id", None) or run_tag

    salt_local = await asyncio.to_thread(_download_run_output, batch_run_id, "outputs/salinity_1.nc")
    if salt_local is None:
        raise SchismBaroclinicError(
            SCHISM_SOLVE_FAILED, "SCHISM completed but outputs/salinity_1.nc was not downloadable")
    out2d_local = await asyncio.to_thread(_download_run_output, batch_run_id, "outputs/out2d_1.nc")
    mesh_uri = _runs_uri(batch_run_id, "outputs/out2d_1.nc")

    try:
        async with substep(emitter, "postprocess_schism_baroclinic"):
            layers, metrics = await asyncio.to_thread(
                pp.postprocess_schism_baroclinic, salt_local, mesh_uri,
                run_id=batch_run_id, sim_days=sim_days,
                river_discharge_m3s=river_discharge_m3s, out2d_path=out2d_local,
                n_nodes_grid=deck["n_nodes"], n_elements_grid=deck["n_elements"],
                n_layers=deck["n_layers"], fallback_note=_DEMO_NOTE,
            )
    except pp.PostprocessSchismError as exc:
        raise SchismBaroclinicError(exc.error_code, str(exc)) from exc
    finally:
        for p in (salt_local, out2d_local):
            try:
                if p:
                    Path(p).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    surf = layers[0]
    assert isinstance(surf, SchismBaroclinicLayerURI)
    bottom_layer = layers[1] if len(layers) > 1 else None
    mesh_layer = layers[2] if len(layers) > 2 else None

    async with substep(emitter, "publish_layer"):
        surf = await asyncio.to_thread(_publish_salinity_layer, surf, review.entries)

    if bottom_layer is not None:
        try:
            bottom_layer = await asyncio.to_thread(_publish_context_raster, bottom_layer)
            await publish_input_layer(emitter, bottom_layer, role="context")
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism bottom-salinity emit skipped: %s", exc)
    if mesh_layer is not None:
        try:
            await publish_input_layer(emitter, mesh_layer, role="context")
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism baroclinic mesh preview emit skipped: %s", exc)

    if emitter is not None and surf.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(surf.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("schism baroclinic zoom-to failed: %s", exc)

    return surf


def _publish_salinity_layer(
    surf: SchismBaroclinicLayerURI, synthetic_inputs: list[SyntheticInput]
) -> SchismBaroclinicLayerURI:
    """Publish the surface-salinity COG through publish_layer + stamp provenance."""
    out = surf
    if synthetic_inputs:
        try:
            out = out.model_copy(update={"synthetic_inputs": list(synthetic_inputs)})
        except Exception:  # noqa: BLE001
            pass
    try:
        published_uri = publish_layer(
            layer_uri=out.uri, layer_id=out.layer_id, style_preset=out.style_preset,
        )
        return out.model_copy(update={"uri": published_uri})
    except PublishLayerError as exc:
        logger.warning("schism surface-salinity publish_layer FAILED layer_id=%s (%s) - returning raw COG",
                       out.layer_id, exc)
        return out


def _publish_context_raster(layer: Any) -> Any:
    """Publish a context raster (bottom salinity) through publish_layer."""
    try:
        published_uri = publish_layer(
            layer_uri=layer.uri, layer_id=layer.layer_id, style_preset=layer.style_preset,
        )
        return layer.model_copy(update={"uri": published_uri})
    except PublishLayerError as exc:
        logger.warning("schism bottom-salinity publish_layer FAILED (%s) - returning raw COG", exc)
        return layer
