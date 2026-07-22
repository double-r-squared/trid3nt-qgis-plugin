"""Atomic tool ``export_case_to_qgis`` -- local-first QGIS bridge (v1).

Given a ``case_id`` (or an explicit ``layers`` list), export the case's layers
into a self-contained folder a user can open directly in desktop QGIS 3.x:

- ``export.gpkg`` -- ONE GeoPackage holding every vector layer (EPSG:4326),
  layer name = sanitized case-layer name.
- ``<name>.tif``  -- a downloaded local copy of every raster (COG) layer.
- ``project.qgz`` -- a QGIS 3.x project (a zip holding ``project.qgs`` XML,
  hand-built with ``xml.etree`` -- NO PyQGIS import) whose layer tree mirrors
  the case's layer order, whose project CRS + initial map extent come from the
  case AOI (or the union of layer bounds), and whose rasters carry a
  singleband-pseudocolor renderer translated from our TiTiler style params.

**Styling choice (documented per spec):** raster styling is embedded INLINE in
the ``.qgs`` ``<maplayer><pipe><rasterrenderer>`` node AND written as a
sidecar ``<name>.qml`` next to every exported GeoTIFF. Inside a QGIS project
the project XML is the authoritative style source, so inline embedding is the
reliable single-file path for "open the .qgz and it looks right"; the sidecar
``.qml`` exists for the OTHER consumer -- the TRID3NT QGIS plugin's
"open case in QGIS" flow adds the exported GeoTIFFs STANDALONE to the user's
current project (it never opens the .qgz, which would replace their open
project), so without a per-raster style file those layers rendered default
grayscale (near-black flood frames). Both forms are built from the SAME
``_raster_pipe_element`` translation, so the .qgz and the sidecars can never
disagree. Sidecar paths are returned as ``qml_paths`` in the result JSON. The
translation samples 5 interpolated stops from the matplotlib colormap named by
``colormap_name`` over the ``rescale=<vmin>,<vmax>`` range (falling back to a
viridis 0-1 ramp when params are absent or the colormap is unknown; colormap
lookup is CASE-INSENSITIVE because TiTiler style params carry lowercase names
like ``ylgnbu`` while matplotlib registers ``YlGnBu``). Ramps whose minimum is
exactly 0 (flood/depth-style) additionally get a raster-transparency entry
making 0-value cells fully transparent -- 0 depth means dry land, and nodata
stays transparent via the default empty ``nodataColor``.

**Local-first URI handling:** a layer ``uri`` may be a plain local path, an
``s3://`` object (boto3; honors ``AWS_ENDPOINT_URL_S3`` / ``AWS_ENDPOINT_URL``
so MinIO works), or ``http(s)://``. A TiTiler tile TEMPLATE
(``.../cog/tiles/...?url=<percent-encoded COG>``) is unwrapped to its
underlying COG ``url=`` query param first (mirrors
``compute_layer_bounds._resolve_layer_to_local_path``). Vector layers may also
carry ``inline_geojson`` instead of a readable uri.

**Honesty floor:** a single unreadable layer is a per-layer SKIP with a note
in the returned ``skipped`` list, not a hard fail -- but a result with ZERO
exported layers never reads ``status="ok"``; it raises
``NoExportableLayersError`` (render-honesty invariant: an empty envelope must
not claim success).

**Cross-cutting invariants:**

- **FR-DC-6:** ``cacheable=False`` / ``ttl_class="live-no-cache"`` -- the tool
  writes files to the local export dir (a side effect), so caching is wrong.
- **FR-AS-11 (typed errors):** every failure raises an ``ExportCaseError``
  subclass with a SCREAMING_SNAKE_CASE ``error_code``.

**Mesh artifacts (MDAL phase 1, additive):** every SFINCS flood-depth layer
(``style_preset == "continuous_flood_depth"``) whose ``uri`` lives under a
runs-bucket ``s3://<bucket>/<run_id>/...`` prefix is checked for a sibling
``<run_id>/sfincs_map.nc`` (the native SFINCS quadtree/regular mesh NetCDF --
QGIS's MDAL provider opens it directly, no conversion). When found, one entry
per DISTINCT ``run_id`` (a case's peak + per-frame flood layers all share the
same run) is appended to the result's ``mesh`` list -- NEVER a locally-copied
file (unlike the GeoTIFF/GeoPackage entries): ``s3_uri`` points straight at
the runs-bucket object; the caller (the QGIS plugin) downloads it itself. CRS
resolution reuses ``postprocess_flood._read_crs_from_dataset`` -- the SAME
reader that CRS-tags the flood-depth COG -- read off the mesh file's own
``crs`` data variable; there is no separate per-run manifest carrying a
resolved UTM EPSG for SFINCS (unlike MODFLOW's ``model_crs`` handoff). A
CRS-read failure still lists the mesh with ``crs_authid=None`` (honest
degrade -- the plugin falls back to "set CRS manually"); a missing
``sfincs_map.nc`` sibling (HeadObject miss) yields no entry at all.

**MDAL phase 2 (MODFLOW, additive).** The groundwater engine's plume layer
(``style_preset == "continuous_plume_concentration"``, ``run_modflow_job`` /
``postprocess_modflow``) gets the SAME treatment against a sibling
``<run_id>/modflow_mesh.nc`` -- but unlike SFINCS's native quadtree output,
this file does not come from the solver; ``workflows/modflow_mesh.py`` BUILDS
it (a CF-1.8/UGRID-1.0 2D mesh over the DIS grid, carrying time-varying head +
concentration datasets) and uploads it as a run-level sibling of the plume
COG. ``_MESH_SIBLING_BY_STYLE_PRESET`` maps a raster layer's ``style_preset``
to its engine's mesh filename/format/display-name-prefix; discovery,
dedup-by-run_id, and CRS resolution are format-agnostic and shared verbatim
between engines -- adding a THIRD engine's mesh only needs a new map entry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import uuid
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

__all__ = [
    "export_case_to_qgis",
    "ExportCaseError",
    "ExportInputError",
    "CaseNotFoundError",
    "NoExportableLayersError",
]

logger = logging.getLogger("grace2_agent.tools.export_case_to_qgis")


# --------------------------------------------------------------------------- #
# Typed errors (FR-AS-11)
# --------------------------------------------------------------------------- #


class ExportCaseError(RuntimeError):
    """Base typed error for the QGIS export. ``error_code`` is
    SCREAMING_SNAKE_CASE and surfaced in the function_response."""

    error_code: str = "EXPORT_FAILED"

    def __init__(self, message: str, error_code: str | None = None) -> None:
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code


class ExportInputError(ExportCaseError):
    """Exactly one of ``case_id`` / ``layers`` must be provided."""

    error_code = "INVALID_INPUT"


class CaseNotFoundError(ExportCaseError):
    """The case does not exist (or persistence is unreachable from here)."""

    error_code = "CASE_NOT_FOUND"


class NoExportableLayersError(ExportCaseError):
    """The case/list has no layers, or every layer was skipped -- an empty
    export never reads status=ok (honesty floor)."""

    error_code = "NO_EXPORTABLE_LAYERS"


# --------------------------------------------------------------------------- #
# Metadata -- writes local files (side effect) => not cacheable (FR-DC-6).
# --------------------------------------------------------------------------- #

_EXPORT_CASE_TO_QGIS_METADATA = AtomicToolMetadata(
    name="export_case_to_qgis",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
)

_RASTER_EXTENSIONS = (".tif", ".tiff", ".img", ".vrt", ".nc")
_VECTOR_EXTENSIONS = (".fgb", ".geojson", ".json", ".gpkg", ".shp", ".gml", ".kml", ".parquet", ".geoparquet")

_WGS84_WKT = (
    'GEOGCRS["WGS 84",DATUM["World Geodetic System 1984",'
    'ELLIPSOID["WGS 84",6378137,298.257223563,LENGTHUNIT["metre",1]]],'
    'PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433]],'
    'CS[ellipsoidal,2],AXIS["geodetic latitude (Lat)",north],'
    'AXIS["geodetic longitude (Lon)",east],'
    'ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",4326]]'
)


# --------------------------------------------------------------------------- #
# Small helpers: names, hashes, uri classification
# --------------------------------------------------------------------------- #


def _sanitize_name(name: str) -> str:
    """Layer name -> filesystem/GPKG-safe token (alnum, dash, underscore)."""
    token = re.sub(r"[^A-Za-z0-9_-]+", "_", (name or "").strip()).strip("_")
    return token or "layer"


def _short_hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


def _strip_query(uri: str) -> str:
    return uri.split("?", 1)[0].rstrip("/")


def _infer_layer_type(uri: str) -> str | None:
    """``"raster"`` / ``"vector"`` from the uri extension, else ``None``."""
    lower = _strip_query(uri).lower()
    if lower.endswith(_RASTER_EXTENSIONS):
        return "raster"
    if lower.endswith(_VECTOR_EXTENSIONS):
        return "vector"
    return None


def _unwrap_tile_template(uri: str) -> str:
    """If ``uri`` is a TiTiler tile TEMPLATE (``/cog/tiles/`` display URL),
    return the underlying COG from its percent-encoded ``url=`` query param;
    otherwise return ``uri`` unchanged."""
    if "/cog/tiles/" not in uri:
        return uri
    cog = (parse_qs(urlparse(uri).query).get("url") or [None])[0]
    if cog:
        return unquote(cog)
    return uri


def _read_uri_bytes(uri: str) -> bytes:
    """Read raw bytes for a local path / ``s3://`` / ``http(s)://`` uri.

    Local-first: ``s3://`` goes through boto3 with an explicit
    ``endpoint_url`` from ``AWS_ENDPOINT_URL_S3`` / ``AWS_ENDPOINT_URL`` when
    set (MinIO), falling back to the default AWS endpoint. Raises on failure
    (callers convert to a per-layer skip)."""
    if uri.startswith("s3://"):
        import boto3

        rest = uri[len("s3://"):]
        slash = rest.find("/")
        if slash <= 0 or slash == len(rest) - 1:
            raise ValueError(f"unparseable s3 uri {uri!r}")
        bucket, key = rest[:slash], rest[slash + 1:]
        endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get(
            "AWS_ENDPOINT_URL"
        )
        s3 = boto3.client(
            "s3",
            region_name=os.environ.get("AWS_REGION", "us-west-2"),
            endpoint_url=endpoint or None,
        )
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    if uri.startswith(("http://", "https://")):
        import urllib.request

        with urllib.request.urlopen(uri, timeout=120) as resp:  # noqa: S310
            return resp.read()

    # Local path (dev / MinIO-mounted / test convenience). A local uri may
    # still carry TiTiler-style query params (?rescale=..&colormap_name=..) --
    # strip them for the filesystem probe.
    for candidate in (Path(uri), Path(_strip_query(uri))):
        if candidate.is_file():
            return candidate.read_bytes()
    raise FileNotFoundError(
        f"layer uri {uri!r} is not an s3:// / http(s):// uri or a readable "
        f"local file"
    )


# --------------------------------------------------------------------------- #
# Mesh sibling discovery (MDAL phase 1) -- SFINCS sfincs_map.nc, additive
# --------------------------------------------------------------------------- #


def _s3_client() -> Any:
    """A boto3 S3 client honoring ``AWS_ENDPOINT_URL_S3`` / ``AWS_ENDPOINT_URL``
    (MinIO local-dev) -- same endpoint resolution as ``_read_uri_bytes``, kept
    as its own client here so a HeadObject probe or a mesh download never
    needs to round-trip a full raster/vector read first."""
    import boto3

    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
    return boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
        endpoint_url=endpoint or None,
    )


def _parse_s3_uri(uri: str) -> tuple[str, str] | None:
    """``s3://bucket/key`` -> ``(bucket, key)``, or ``None`` for anything else
    (non-s3 uri, or a bucket/key that fails to parse)."""
    if not uri.startswith("s3://"):
        return None
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        return None
    return bucket, key


def _resolve_mesh_crs(bucket: str, mesh_key: str) -> str | None:
    """Best-effort CRS for a runs-bucket ``sfincs_map.nc``: download it and
    read its ``crs`` data variable via ``postprocess_flood``'s SAME parser --
    the reader that CRS-tags the peak flood-depth COG, so the mesh and the
    raster can never disagree. Returns ``None`` on ANY failure (unreachable
    bucket, unreadable NetCDF, no recognizable crs encoding) -- the mesh
    entry is still listed by the caller (honest degrade, never a hard fail)."""
    try:
        import xarray as xr  # type: ignore[import-not-found]

        from ..workflows.postprocess_flood import _read_crs_from_dataset

        tmp = tempfile.NamedTemporaryFile(suffix=".nc", delete=False, prefix="grace2_meshcrs_")
        tmp.close()
        try:
            _s3_client().download_file(bucket, mesh_key, tmp.name)
            ds = xr.open_dataset(tmp.name)
            try:
                crs = _read_crs_from_dataset(ds)
            finally:
                ds.close()
            return str(crs) if crs else None
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001 -- CRS is a nicety, never a gate
        logger.warning(
            "export_case_to_qgis: could not resolve mesh CRS for s3://%s/%s (%s)",
            bucket,
            mesh_key,
            exc,
        )
        return None


def _resolve_telemac_mesh_crs(bucket: str, run_id: str) -> str | None:
    """CRS for a TELEMAC SELAFIN mesh: read ``utm_epsg`` from the run's
    ``telemac_metrics.json`` sibling and format it as ``EPSG:<code>``.

    SELAFIN (.slf) files embed NO coordinate system, so MDAL reports an empty
    CRS and the plugin cannot place the mesh on the basemap. The worker's
    ``telemac_metrics.json`` (uploaded alongside the result .slf) carries the
    reach UTM zone the mesh was built in, which is the mesh's true CRS. Returns
    ``None`` on ANY failure (missing/unreadable metrics, no ``utm_epsg``) -- the
    mesh entry is still listed (honest degrade: the plugin notes "set CRS
    manually")."""
    try:
        obj = _s3_client().get_object(Bucket=bucket, Key=f"{run_id}/telemac_metrics.json")
        metrics = json.loads(obj["Body"].read().decode("utf-8"))
        epsg = metrics.get("utm_epsg")
        if epsg is None:
            return None
        return f"EPSG:{int(epsg)}"
    except Exception as exc:  # noqa: BLE001 -- CRS is a nicety, never a gate
        logger.warning(
            "export_case_to_qgis: could not resolve TELEMAC mesh CRS for "
            "s3://%s/%s (%s)",
            bucket,
            run_id,
            exc,
        )
        return None


#: raster ``style_preset`` -> ``(mesh sibling filename, format id, display
#: name prefix)``. One entry per hazard-model engine that publishes a run-
#: level mesh sibling; SFINCS's is the engine's own native quadtree solve
#: output, MODFLOW's is BUILT by ``workflows/modflow_mesh.py`` (see the module
#: docstring's "MDAL phase 2" section). Only style_presets an engine's
#: postprocess ACTUALLY wires a mesh emitter for belong here -- an unwired
#: preset must stay absent (a HeadObject miss reads identically to "no mesh
#: support" either way, but an absent map entry is honest about scope: today
#: only the MODFLOW spill/plume path, run_modflow_job, emits a mesh; the
#: GWF-only archetype postprocess functions -- drawdown/dewatering/mounding/
#: ASR/hydroperiod/river-seepage -- do not call the emitter yet).
_MESH_SIBLING_BY_STYLE_PRESET: dict[str, tuple[str, str, str]] = {
    "continuous_flood_depth": ("sfincs_map.nc", "sfincs_map_netcdf", "SFINCS mesh"),
    "continuous_plume_concentration": (
        "modflow_mesh.nc",
        "modflow_ugrid_netcdf",
        "MODFLOW mesh",
    ),
    # TELEMAC-2D river-dye: the solver's NATIVE result SELAFIN (MDAL opens .slf
    # directly + animates its time-stepped DYE dataset group). Unlike SFINCS/
    # MODFLOW whose mesh siblings are NetCDF carrying a ``crs`` variable, SELAFIN
    # embeds NO CRS, so its CRS is resolved from the run's telemac_metrics.json
    # (utm_epsg) instead -- see ``_resolve_telemac_mesh_crs`` / the format branch
    # in ``_mesh_entry_for_layer``.
    "continuous_dye_concentration": (
        "r2d_river.slf",
        "telemac_selafin",
        "TELEMAC dye mesh",
    ),
    # GAIA v1 sediment: the deposition (CUMUL BED EVOL) COG maps to the GAIA
    # result SELAFIN so QGIS Temporal Controller animates the bed evolution +
    # per-frame suspended concentration. Same CRS-from-metrics path as the dye
    # mesh (SELAFIN embeds no CRS -> utm_epsg from telemac_metrics.json). Adding a
    # THIRD engine output needs only this one map entry.
    "diverging_bed_evolution": (
        "gaia_river.slf",
        "telemac_selafin",
        "GAIA sediment mesh",
    ),
}


def _mesh_entry_for_layer(layer: dict[str, Any]) -> dict[str, Any] | None:
    """For a raster layer whose ``style_preset`` names an engine with a mesh
    sibling (``_MESH_SIBLING_BY_STYLE_PRESET``) and whose ``uri`` is (or
    wraps, as a TiTiler display TEMPLATE) an ``s3://<bucket>/<run_id>/..``
    object, probe the SAME bucket for that engine's mesh sibling and build the
    additive mesh export entry (``None`` when the style_preset has no mapped
    mesh sibling, its uri does not resolve to s3://, or no mesh sibling
    exists).

    A PERSISTED layer's ``uri`` is normally the TiTiler
    ``/cog/tiles/...?url=<percent-encoded s3://...>`` display template, not a
    raw ``s3://`` uri (confirmed against real dev-persistence case data) --
    ``_unwrap_tile_template`` (the SAME helper the raster export path uses)
    resolves the underlying COG uri first."""
    sibling = _MESH_SIBLING_BY_STYLE_PRESET.get(str(layer.get("style_preset") or ""))
    if sibling is None:
        return None
    mesh_filename, mesh_format, name_prefix = sibling
    resolved_uri = _unwrap_tile_template(str(layer.get("uri") or ""))
    parsed = _parse_s3_uri(resolved_uri)
    if parsed is None:
        return None
    bucket, key = parsed
    run_id = key.split("/", 1)[0]
    if not run_id:
        return None
    mesh_key = f"{run_id}/{mesh_filename}"
    try:
        _s3_client().head_object(Bucket=bucket, Key=mesh_key)
    except Exception:  # noqa: BLE001 -- no mesh sibling (or bucket unreachable) -- no entry
        return None
    # CRS resolution is format-specific: NetCDF meshes (SFINCS/MODFLOW) carry a
    # ``crs`` variable; a SELAFIN (.slf) does not, so TELEMAC reads utm_epsg from
    # the run's telemac_metrics.json sibling instead.
    if mesh_format == "telemac_selafin":
        crs_authid = _resolve_telemac_mesh_crs(bucket, run_id)
    else:
        crs_authid = _resolve_mesh_crs(bucket, mesh_key)
    return {
        "kind": "mesh",
        "format": mesh_format,
        "s3_uri": f"s3://{bucket}/{mesh_key}",
        "crs_authid": crs_authid,
        "name": f"{name_prefix} ({run_id[:8]})",
    }


def _collect_mesh_entries(raw_layers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One mesh entry per DISTINCT ``run_id`` found across ``raw_layers`` (a
    case's peak + per-frame flood-depth layers all share the same run, so a
    naive per-layer scan would duplicate the same mesh many times)."""
    entries: list[dict[str, Any]] = []
    seen_run_ids: set[str] = set()
    for layer in raw_layers:
        resolved_uri = _unwrap_tile_template(str(layer.get("uri") or ""))
        parsed = _parse_s3_uri(resolved_uri)
        run_id = parsed[1].split("/", 1)[0] if parsed else None
        if run_id and run_id in seen_run_ids:
            continue
        entry = _mesh_entry_for_layer(layer)
        if entry is not None:
            if run_id:
                seen_run_ids.add(run_id)
            entries.append(entry)
    return entries


# --------------------------------------------------------------------------- #
# Style translation: TiTiler rescale/colormap_name -> QGIS pseudocolor stops
# --------------------------------------------------------------------------- #


def _style_from_layer(layer: dict[str, Any]) -> tuple[float, float, str]:
    """Resolve ``(vmin, vmax, colormap_name)`` for a raster layer.

    Sources, in order: the layer's data-driven ``legend`` (LegendKey dict with
    ``vmin``/``vmax``/``colormap``), then the ``rescale=`` / ``colormap_name=``
    query params of the layer's uri (the TiTiler display template carries
    them). Fallback: viridis over 0..1."""
    legend = layer.get("legend")
    if isinstance(legend, dict):
        vmin, vmax, cmap = legend.get("vmin"), legend.get("vmax"), legend.get("colormap")
        if vmin is not None and vmax is not None and cmap:
            try:
                return float(vmin), float(vmax), str(cmap)
            except (TypeError, ValueError):
                pass

    uri = str(layer.get("uri") or "")
    qs = parse_qs(urlparse(uri).query)
    rescale = (qs.get("rescale") or [None])[0]
    cmap = (qs.get("colormap_name") or [None])[0]
    vmin = vmax = None
    if rescale and "," in rescale:
        lo_s, hi_s = unquote(rescale).split(",", 1)
        try:
            vmin, vmax = float(lo_s), float(hi_s)
        except ValueError:
            vmin = vmax = None
    if vmin is not None and vmax is not None and math.isfinite(vmin) and math.isfinite(vmax):
        return vmin, vmax, (cmap or "viridis")
    return 0.0, 1.0, (cmap or "viridis")


def _colormap_stops(cmap_name: str, vmin: float, vmax: float, n: int = 5) -> list[tuple[float, str]]:
    """Sample ``n`` interpolated ``(value, "#rrggbb")`` stops from the
    matplotlib colormap ``cmap_name`` over [vmin, vmax].

    Lookup is CASE-INSENSITIVE on a miss: TiTiler style params carry lowercase
    colormap names (``ylgnbu``, ``blues``) while matplotlib's registry is
    case-sensitive (``YlGnBu``, ``Blues``) -- without the retry every real
    flood-depth export silently degraded to viridis. Truly unknown names fall
    back to viridis (honest degrade, never a crash)."""
    from matplotlib import colormaps
    from matplotlib.colors import to_hex

    try:
        cmap = colormaps[cmap_name]
    except (KeyError, TypeError):
        folded = str(cmap_name or "").lower()
        match = next((n_ for n_ in colormaps if n_.lower() == folded), None)
        if match is not None:
            cmap = colormaps[match]
        else:
            logger.warning(
                "export_case_to_qgis: unknown colormap %r -- falling back to viridis",
                cmap_name,
            )
            cmap = colormaps["viridis"]
    if vmax <= vmin:  # degenerate range -> nudge so QGIS gets distinct stops
        vmax = vmin + 1.0
    stops: list[tuple[float, str]] = []
    for i in range(n):
        t = i / (n - 1)
        stops.append((vmin + t * (vmax - vmin), to_hex(cmap(t))))
    return stops


def _raster_pipe_element(vmin: float, vmax: float, cmap_name: str) -> ET.Element:
    """Build the ``<pipe>`` node carrying a QGIS 3.x singleband pseudocolor
    renderer with 5 interpolated stops. Shared by the inline ``.qgs`` maplayer
    AND the sidecar ``.qml`` (single seam -- see module docstring).

    Transparency: nodata is transparent via the empty ``nodataColor`` default;
    when the ramp starts at exactly 0 (flood/depth-style rescale=0,N) a
    raster-transparency single-value entry additionally makes 0-value cells
    fully transparent -- 0 depth is dry land, never a black cell."""
    pipe = ET.Element("pipe")
    renderer = ET.SubElement(
        pipe,
        "rasterrenderer",
        {
            "type": "singlebandpseudocolor",
            "opacity": "1",
            "alphaBand": "-1",
            "band": "1",
            "nodataColor": "",
            "classificationMin": repr(float(vmin)),
            "classificationMax": repr(float(vmax)),
        },
    )
    if float(vmin) == 0.0:
        transparency = ET.SubElement(renderer, "rasterTransparency")
        value_list = ET.SubElement(transparency, "singleValuePixelList")
        ET.SubElement(
            value_list,
            "pixelListEntry",
            {"min": "0", "max": "0", "percentTransparent": "100"},
        )
    shader = ET.SubElement(renderer, "rastershader")
    ramp = ET.SubElement(
        shader,
        "colorrampshader",
        {
            "colorRampType": "INTERPOLATED",
            "classificationMode": "1",
            "clip": "0",
            "minimumValue": repr(float(vmin)),
            "maximumValue": repr(float(vmax)),
            "labelPrecision": "4",
        },
    )
    for value, color in _colormap_stops(cmap_name, vmin, vmax):
        ET.SubElement(
            ramp,
            "item",
            {
                "value": repr(float(value)),
                "color": color,
                "alpha": "255",
                "label": f"{value:.4g}",
            },
        )
    ET.SubElement(pipe, "brightnesscontrast", {"brightness": "0", "contrast": "0", "gamma": "1"})
    ET.SubElement(pipe, "rasterresampler", {"maxOversampling": "2"})
    return pipe


def _qml_bytes(vmin: float, vmax: float, cmap_name: str) -> bytes:
    """Serialize a standalone QGIS layer-style document (``.qml``) carrying
    the SAME pseudocolor pipe the ``.qgs`` embeds inline.

    Written as a sidecar next to every exported GeoTIFF so a consumer that
    adds the raster STANDALONE (the TRID3NT QGIS plugin's ``loadNamedStyle``
    call; QGIS also auto-loads a same-stem ``.qml`` on manual add) renders the
    web colormap instead of default grayscale."""
    root = ET.Element(
        "qgis",
        {
            "version": "3.28.0-Firenze",
            "styleCategories": "AllStyleCategories",
            "hasScaleBasedVisibilityFlag": "0",
            "maxScale": "0",
            "minScale": "1e+08",
        },
    )
    root.append(_raster_pipe_element(vmin, vmax, cmap_name))
    ET.SubElement(root, "blendMode").text = "0"
    header = b"<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
    return header + ET.tostring(root, encoding="utf-8", xml_declaration=False)


# --------------------------------------------------------------------------- #
# .qgs project XML (hand-built, QGIS 3.x -- NO PyQGIS)
# --------------------------------------------------------------------------- #


def _spatialrefsys_4326(parent: ET.Element) -> None:
    srs = ET.SubElement(parent, "spatialrefsys", {"nativeFormat": "Wkt"})
    ET.SubElement(srs, "wkt").text = _WGS84_WKT
    ET.SubElement(srs, "proj4").text = "+proj=longlat +datum=WGS84 +no_defs"
    ET.SubElement(srs, "srsid").text = "3452"
    ET.SubElement(srs, "srid").text = "4326"
    ET.SubElement(srs, "authid").text = "EPSG:4326"
    ET.SubElement(srs, "description").text = "WGS 84"
    ET.SubElement(srs, "projectionacronym").text = "longlat"
    ET.SubElement(srs, "ellipsoidacronym").text = "EPSG:7030"
    ET.SubElement(srs, "geographicflag").text = "true"


def _build_qgs_xml(
    project_name: str,
    entries: list[dict[str, Any]],
    extent: tuple[float, float, float, float] | None,
) -> bytes:
    """Serialize the minimal-but-valid QGIS 3.x project XML.

    ``entries`` are exported-layer records in CASE LAYER ORDER, each carrying
    ``layer_id`` (QGIS layer id), ``name``, ``kind`` ("vector"/"raster"),
    ``datasource`` (relative: ``./export.gpkg|layername=X`` or ``./<n>.tif``),
    and for rasters the style triple ``(vmin, vmax, cmap)``."""
    root = ET.Element(
        "qgis",
        {
            "projectname": project_name,
            "version": "3.28.0-Firenze",
            "saveUser": "grace2",
            "saveUserFull": "GRACE-2 export_case_to_qgis",
        },
    )
    ET.SubElement(root, "homePath", {"path": ""})
    ET.SubElement(root, "title").text = project_name

    project_crs = ET.SubElement(root, "projectCrs")
    _spatialrefsys_4326(project_crs)

    # Layer tree: mirrors the case's layer ORDER (first case layer = first
    # tree row, i.e. drawn on top in QGIS's tree-order convention).
    tree = ET.SubElement(root, "layer-tree-group")
    for e in entries:
        ET.SubElement(
            tree,
            "layer-tree-layer",
            {
                "id": e["layer_id"],
                "name": e["name"],
                "source": e["datasource"],
                "providerKey": "ogr" if e["kind"] == "vector" else "gdal",
                "checked": "Qt::Checked",
                "expanded": "1",
                "legend_exp": "",
                "patch_size": "-1,-1",
            },
        )

    # Initial view extent = case AOI (or union of layer bounds), EPSG:4326.
    if extent is not None:
        canvas = ET.SubElement(root, "mapcanvas", {"name": "theMapCanvas", "annotationsVisible": "1"})
        ET.SubElement(canvas, "units").text = "degrees"
        ext = ET.SubElement(canvas, "extent")
        ET.SubElement(ext, "xmin").text = repr(float(extent[0]))
        ET.SubElement(ext, "ymin").text = repr(float(extent[1]))
        ET.SubElement(ext, "xmax").text = repr(float(extent[2]))
        ET.SubElement(ext, "ymax").text = repr(float(extent[3]))
        ET.SubElement(canvas, "rotation").text = "0"
        dest = ET.SubElement(canvas, "destinationsrs")
        _spatialrefsys_4326(dest)

    layers_el = ET.SubElement(root, "projectlayers")
    for e in entries:
        ml = ET.SubElement(
            layers_el,
            "maplayer",
            {
                "type": e["kind"],
                "autoRefreshEnabled": "0",
                "refreshOnNotifyEnabled": "0",
                "styleCategories": "AllStyleCategories",
                "legendPlaceholderImage": "",
            }
            | ({"geometry": e["geometry"]} if e["kind"] == "vector" and e.get("geometry") else {}),
        )
        ET.SubElement(ml, "id").text = e["layer_id"]
        ET.SubElement(ml, "datasource").text = e["datasource"]
        ET.SubElement(ml, "layername").text = e["name"]
        if e["kind"] == "vector":
            # Vectors are rewritten into the GPKG as EPSG:4326 by construction.
            srs_el = ET.SubElement(ml, "srs")
            _spatialrefsys_4326(srs_el)
            ET.SubElement(ml, "provider", {"encoding": "UTF-8"}).text = "ogr"
        else:
            # Raster: omit <srs> so QGIS probes the GeoTIFF's native CRS
            # (rasters are copied verbatim, NOT reprojected).
            ET.SubElement(ml, "provider").text = "gdal"
            vmin, vmax, cmap = e["style"]
            ml.append(_raster_pipe_element(vmin, vmax, cmap))
        ET.SubElement(ml, "blendMode").text = "0"
        ET.SubElement(ml, "flags").text = "Identifiable|Removable|Searchable"

    order = ET.SubElement(ET.SubElement(root, "layerorder"), "customOrder", {"enabled": "0"})
    for e in entries:
        ET.SubElement(order, "item").text = e["layer_id"]

    header = b"<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
    return header + ET.tostring(root, encoding="utf-8", xml_declaration=False)


# --------------------------------------------------------------------------- #
# Per-layer export helpers
# --------------------------------------------------------------------------- #


def _vector_gdf_from_layer(layer: dict[str, Any]) -> "Any":
    """Read a vector layer into a GeoDataFrame (EPSG:4326).

    ``inline_geojson`` (FeatureCollection dict) wins when present; otherwise
    the layer ``uri`` bytes are materialized to a temp file with the right
    suffix and read via geopandas/pyogrio. Raises on any failure (caller
    converts to a per-layer skip)."""
    import geopandas as gpd

    inline = layer.get("inline_geojson")
    if isinstance(inline, dict) and inline.get("features") is not None:
        gdf = gpd.GeoDataFrame.from_features(inline.get("features") or [], crs="EPSG:4326")
    else:
        uri = str(layer.get("uri") or "")
        if not uri:
            raise ValueError("vector layer has neither inline_geojson nor uri")
        data = _read_uri_bytes(uri)
        lower = _strip_query(uri).lower()
        suffix = next((ext for ext in _VECTOR_EXTENSIONS if lower.endswith(ext)), ".fgb")
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False, prefix="grace2_qgisexp_")
        try:
            tmp.write(data)
            tmp.close()
            try:
                gdf = gpd.read_file(tmp.name, engine="pyogrio")
            except Exception:  # noqa: BLE001 -- retry with the default engine
                gdf = gpd.read_file(tmp.name)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    gdf = gdf[gdf.geometry.notna()]
    if len(gdf) == 0:
        raise ValueError("vector layer has no features with geometry")
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    elif str(gdf.crs).upper() != "EPSG:4326":
        gdf = gdf.to_crs("EPSG:4326")
    return gdf


def _geometry_token(gdf: "Any") -> str:
    """Map the GeoDataFrame's dominant geometry to the QGIS maplayer
    ``geometry`` attribute token."""
    try:
        geom_type = str(gdf.geometry.geom_type.mode().iat[0])
    except Exception:  # noqa: BLE001
        return "Polygon"
    if "Point" in geom_type:
        return "Point"
    if "LineString" in geom_type or "Line" in geom_type:
        return "Line"
    return "Polygon"


def _raster_bounds_4326(path: str) -> tuple[float, float, float, float] | None:
    """Best-effort raster extent in EPSG:4326 (None on failure -- extent is a
    view nicety, never a gate)."""
    try:
        import rasterio
        from rasterio.warp import transform_bounds

        with rasterio.open(path) as ds:
            b = ds.bounds
            if ds.crs is not None and str(ds.crs).upper() != "EPSG:4326":
                return tuple(float(v) for v in transform_bounds(ds.crs, "EPSG:4326", *b))  # type: ignore[return-value]
            return (float(b.left), float(b.bottom), float(b.right), float(b.top))
    except Exception as exc:  # noqa: BLE001
        logger.warning("export_case_to_qgis: raster bounds probe failed for %s: %s", path, exc)
        return None


def _union_extent(
    boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    finite = [b for b in boxes if b and all(math.isfinite(v) for v in b)]
    if not finite:
        return None
    return (
        min(b[0] for b in finite),
        min(b[1] for b in finite),
        max(b[2] for b in finite),
        max(b[3] for b in finite),
    )


# --------------------------------------------------------------------------- #
# Case-layer resolution (persistence seam, with the explicit-layers fallback)
# --------------------------------------------------------------------------- #


async def _layers_from_case(case_id: str) -> tuple[list[dict[str, Any]], list[float] | None, str]:
    """Resolve ``(layer dicts, case bbox, case title)`` for ``case_id`` via the
    app-level ``Persistence`` singleton (``telemetry.get_persistence`` -- the
    same lazy seam telemetry uses, monkeypatchable in tests). Layers come from
    the Case doc's persisted ``loaded_layer_summaries`` (ProjectLayerSummary
    dicts -- the same source the cold case manifest marshals)."""
    from ..telemetry import get_persistence

    try:
        persistence = get_persistence()
    except Exception:  # noqa: BLE001
        persistence = None
    if persistence is None:
        raise CaseNotFoundError(
            f"cannot look up case {case_id!r}: the persistence backend is not "
            f"available from this process. Pass the explicit `layers` list "
            f"(name/layer_type/uri per layer) instead."
        )
    case = await persistence.get_case(case_id)
    if case is None:
        raise CaseNotFoundError(f"case {case_id!r} not found.")
    layers = [dict(entry) for entry in (case.loaded_layer_summaries or [])]
    bbox = list(case.bbox) if case.bbox else None
    return layers, bbox, case.title or case_id


# --------------------------------------------------------------------------- #
# Tool registration
# --------------------------------------------------------------------------- #


@register_tool(
    _EXPORT_CASE_TO_QGIS_METADATA,
    # Writes files into the local export dir => not read-only / not
    # idempotent-free of side effects, but re-running with the same inputs
    # rewrites the same folder contents (idempotent), destroys nothing that
    # is not its own output, and reaches object storage (open world).
    read_only_hint=False,
    open_world_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
)
async def export_case_to_qgis(
    case_id: str | None = None,
    layers: list[dict] | None = None,
    output_dir: str | None = None,
    project_name: str | None = None,
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Export a case's layers as a ready-to-open desktop QGIS project.

    USE THIS when the user asks to EXPORT a case (or a set of layers) TO QGIS,
    download the case for desktop GIS, or get a GeoPackage / GeoTIFF / .qgz
    bundle of what is on the map. It writes a self-contained folder holding:
    one GeoPackage (``export.gpkg``) with ALL vector layers in EPSG:4326,
    a local GeoTIFF copy of every raster layer, and a ``project.qgz`` QGIS 3.x
    project that opens directly in QGIS with the case's layer order, raster
    color styling translated from the web map, and the case AOI as the initial
    view extent.

    Provide EXACTLY ONE of ``case_id`` or ``layers``:

    Parameters:
        case_id: the case to export -- its persisted layer list, layer order,
            bbox, and title drive the project. Preferred form.
        layers: explicit layer list (fallback when no case applies): each item
            is ``{"name": str, "layer_type": "raster"|"vector", "uri": str}``
            (``uri`` may be a local path, ``s3://`` object, ``http(s)://`` URL,
            or the TiTiler display tile template; a vector item may carry
            ``inline_geojson`` instead of ``uri``).
        output_dir: destination folder. Default:
            ``${GRACE2_EXPORT_DIR or ~/trid3nt-exports}/<case-or-project>-<hash>/``.
        project_name: QGIS project title; defaults to the case title / case_id.

    Returns:
        {
          "status": "ok" | "partial",   # "partial" when some layers skipped
          "qgz_path": str,              # the .qgz to open in QGIS
          "gpkg_path": str | None,      # None when the case has no vectors
          "exported_vector_count": int,
          "exported_raster_count": int,
          "qml_paths": [str, ...],      # sidecar .qml style per raster
          "skipped": [{"name": str, "reason": str}, ...],
          "output_dir": str,
          "mesh": [                     # additive (MDAL phase 1); [] if none
            {
              "kind": "mesh",
              "format": "sfincs_map_netcdf",
              "s3_uri": str,            # runs-bucket sfincs_map.nc -- NOT
                                         # copied into output_dir; the caller
                                         # downloads it itself
              "crs_authid": str | None, # e.g. "EPSG:32616"; None if unresolved
              "name": str,              # e.g. "SFINCS mesh (a1b2c3d4)"
            },
            ...
          ],
        }

    Raises:
        ExportCaseError: typed (FR-AS-11) -- INVALID_INPUT (not exactly one of
            case_id/layers), CASE_NOT_FOUND, NO_EXPORTABLE_LAYERS (zero layers
            or every layer skipped -- an empty export never claims success).
    """
    if (case_id is None) == (layers is None):
        raise ExportInputError(
            "provide exactly one of `case_id` or `layers` (got "
            f"case_id={'set' if case_id is not None else 'unset'}, "
            f"layers={'set' if layers is not None else 'unset'})."
        )

    case_bbox: list[float] | None = None
    if case_id is not None:
        raw_layers, case_bbox, case_title = await _layers_from_case(case_id)
        title = project_name or case_title
        slug_seed = case_id
    else:
        raw_layers = [dict(entry) for entry in (layers or []) if isinstance(entry, dict)]
        title = project_name or "trid3nt-export"
        slug_seed = title

    if not raw_layers:
        raise NoExportableLayersError(
            f"{'case ' + case_id if case_id else 'the provided layer list'} "
            f"has no layers to export."
        )

    # --- Output dir --------------------------------------------------------
    if output_dir:
        out = Path(output_dir).expanduser()
    else:
        base = Path(os.environ.get("GRACE2_EXPORT_DIR") or (Path.home() / "trid3nt-exports"))
        uris = [str(l.get("uri") or l.get("layer_id") or l.get("name") or "") for l in raw_layers]
        out = base / f"{_sanitize_name(slug_seed)}-{_short_hash(slug_seed, *uris)}"
    out.mkdir(parents=True, exist_ok=True)
    gpkg_path = out / "export.gpkg"

    # --- Per-layer export (skip-not-fail; case layer ORDER preserved) ------
    entries: list[dict[str, Any]] = []  # .qgs records, in case layer order
    qml_paths: list[str] = []  # sidecar style files, raster export order
    skipped: list[dict[str, str]] = []
    bounds: list[tuple[float, float, float, float]] = []
    used_names: set[str] = set()
    n_vec = n_ras = 0

    for idx, layer in enumerate(raw_layers):
        name = str(layer.get("name") or layer.get("layer_id") or f"layer_{idx + 1}")
        safe = _sanitize_name(name)
        n = 2
        while safe in used_names:
            safe = f"{_sanitize_name(name)}_{n}"
            n += 1

        uri = str(layer.get("uri") or "")
        resolved_uri = _unwrap_tile_template(uri) if uri else uri
        layer_type = layer.get("layer_type") or _infer_layer_type(resolved_uri or uri)
        if layer_type not in ("raster", "vector"):
            # WMS/tile-only layers (or anything we cannot type) are honestly
            # skipped -- there is no downloadable artifact to hand QGIS.
            skipped.append({"name": name, "reason": f"unsupported or unknown layer type (uri={uri or 'absent'!r})"})
            continue

        layer_qgis_id = f"{safe}_{uuid.uuid4().hex}"
        try:
            if layer_type == "vector":
                gdf = _vector_gdf_from_layer({**layer, "uri": resolved_uri})
                # Drop mixed/nested-object columns GPKG cannot carry.
                for col in list(gdf.columns):
                    if col != gdf.geometry.name and gdf[col].map(
                        lambda v: isinstance(v, (dict, list))
                    ).any():
                        gdf[col] = gdf[col].map(
                            lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v
                        )
                gdf.to_file(gpkg_path, layer=safe, driver="GPKG")
                b = gdf.total_bounds
                bounds.append((float(b[0]), float(b[1]), float(b[2]), float(b[3])))
                entries.append(
                    {
                        "layer_id": layer_qgis_id,
                        "name": name,
                        "kind": "vector",
                        "geometry": _geometry_token(gdf),
                        "datasource": f"./export.gpkg|layername={safe}",
                    }
                )
                n_vec += 1
            else:
                if not resolved_uri:
                    raise ValueError("raster layer has no uri")
                data = _read_uri_bytes(resolved_uri)
                tif_path = out / f"{safe}.tif"
                tif_path.write_bytes(data)
                rb = _raster_bounds_4326(str(tif_path))
                if rb is not None:
                    bounds.append(rb)
                vmin, vmax, cmap = _style_from_layer({**layer, "uri": uri or resolved_uri})
                # Sidecar .qml (same translation as the .qgz pipe) so the
                # QGIS plugin's standalone-add path renders the web colormap
                # instead of default grayscale. A sidecar write failure is a
                # style-only degrade, never a reason to skip a good raster.
                try:
                    qml_path = out / f"{safe}.qml"
                    qml_path.write_bytes(_qml_bytes(vmin, vmax, cmap))
                    qml_paths.append(str(qml_path))
                except OSError as exc:
                    logger.warning(
                        "export_case_to_qgis: could not write style sidecar "
                        "for %r (%s) -- raster exported unstyled",
                        name,
                        exc,
                    )
                entries.append(
                    {
                        "layer_id": layer_qgis_id,
                        "name": name,
                        "kind": "raster",
                        "datasource": f"./{safe}.tif",
                        "style": (vmin, vmax, cmap),
                    }
                )
                n_ras += 1
            used_names.add(safe)
        except Exception as exc:  # noqa: BLE001 -- per-layer skip, not a hard fail
            logger.warning(
                "export_case_to_qgis: skipping layer %r (%s: %s)",
                name,
                type(exc).__name__,
                exc,
            )
            skipped.append({"name": name, "reason": f"{type(exc).__name__}: {exc}"})

    if not entries:
        raise NoExportableLayersError(
            "no layer could be exported "
            f"({len(skipped)} skipped: "
            + "; ".join(f"{s['name']}: {s['reason']}" for s in skipped)
            + "). An empty export never claims success."
        )

    # --- Project extent: case AOI wins; else union of exported bounds ------
    extent: tuple[float, float, float, float] | None = None
    if case_bbox and len(case_bbox) == 4 and all(math.isfinite(float(v)) for v in case_bbox):
        extent = (float(case_bbox[0]), float(case_bbox[1]), float(case_bbox[2]), float(case_bbox[3]))
    else:
        extent = _union_extent(bounds)

    # --- .qgs -> .qgz -------------------------------------------------------
    qgs_bytes = _build_qgs_xml(title, entries, extent)
    qgz_path = out / "project.qgz"
    with zipfile.ZipFile(qgz_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.qgs", qgs_bytes)

    # --- Mesh siblings (MDAL phase 1, additive) -----------------------------
    # Best-effort: an unreachable runs bucket must never turn a good
    # GeoTIFF/GeoPackage export into a failure -- the mesh list is just empty.
    try:
        mesh_entries = _collect_mesh_entries(raw_layers)
    except Exception as exc:  # noqa: BLE001
        logger.warning("export_case_to_qgis: mesh discovery failed (%s)", exc)
        mesh_entries = []

    result = {
        "status": "ok" if not skipped else "partial",
        "qgz_path": str(qgz_path),
        "gpkg_path": str(gpkg_path) if n_vec > 0 else None,
        "exported_vector_count": n_vec,
        "exported_raster_count": n_ras,
        "qml_paths": qml_paths,
        "skipped": skipped,
        "output_dir": str(out),
        "mesh": mesh_entries,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        "export_case_to_qgis: exported %d vector + %d raster layer(s) to %s "
        "(%d skipped, %d mesh)",
        n_vec,
        n_ras,
        out,
        len(skipped),
        len(mesh_entries),
    )
    return result
