"""Layer materialization -- turn agent LayerEvents into native QGIS layers.

The differentiator: every layer the agent publishes lands in the QGIS layer
tree, grouped under "TRID3NT <case>".

ONE STORE, ONE SCHEME: every layer reference is an ``s3://bucket/key`` uri and
GDAL reads it natively through ``/vsis3/`` (``s3_to_vsis3``). The endpoint and
credentials are process-wide GDAL configuration applied once
(``configure_store_access``), so pointing at a remote store is an endpoint
VALUE, never a second code path -- there is no local-path-versus-remote branch
anywhere below.

  raster  ``QgsRasterLayer("/vsis3/<bucket>/<key>", name, "gdal")`` styled by
          ``loadNamedStyle`` from the ``.qml`` the event's ``legend`` carries -
          the resolved preset, in QGIS's own style format. A COG that already
          carries its colours (RGB(A), an embedded colour table) ships no
          ``.qml`` and keeps QGIS's own default renderer, which is already the
          correct render. Ranged reads: overviews and windows only.
  vector  ``QgsVectorLayer("/vsis3/<bucket>/<key>", name, "ogr")`` -- FlatGeobuf
          reads its spatial index ranged, so QGIS fetches only the intersecting
          features, NO local copy. The agent's additive ``inline_geojson`` merge
          is INLINE data (not a store object), so it stages to the session temp
          dir as a small ``.geojson`` -> ogr layer, labeled as staged.
  mesh    the ONE cache hop: MDAL has no ``/vsi`` layer, so the object is copied
          out of the store through GDAL's own VSI reader into a SESSION-scoped
          temp dir (``trid3nt_session_<tag>`` under the platform temp), cleaned
          up on dock disconnect/close, with a stale-session sweep at plugin
          start for crash leftovers. Every layer note says STREAMED vs STAGED --
          nothing ever lands outside the session temp, and a staged layer is
          always labeled.

Dedup: by ``layer_id`` -- session-state is replayed on every emit (A.7
replace-not-reconcile), so the same rows arrive many times per turn.

Temporal: a layer states its own clock and this side stamps it. One frame of
a sequence carries the ``valid_from``/``valid_to`` window its producer already
held, so the built-in Temporal Controller plays the sequence with no name to
parse and no synthetic clock. A mesh carries a ``reference_time``: MDAL owns
the time axis inside a SELAFIN but the file records no origin for it, so the
row says when zero was and ``setReferenceTime`` moves the whole extent onto
the run's own clock.

Mesh outputs (MDAL, the ONE staged format): a ``layer_type == "mesh"`` event
(SFINCS ``sfincs_map.nc`` and kin) STAGES to the session temp dir first --
QGIS's MDAL provider demands a local path -- then loads
``QgsMeshLayer(local_path, name, "mdal")`` (``_add_mesh``). QGIS's MDAL
provider reports an EMPTY crs() for a SELAFIN and for a SFINCS quadtree NetCDF
(proven live), so ``setCrs(QgsCoordinateReferenceSystem(crs_authid))`` is applied
explicitly from the event's ``crs_authid`` (carried on the row); when that is
unresolved the layer is still added with an honest dock note instead of a
silent wrong-CRS render. The active scalar dataset group is set to the
``maximum_water_depth_timemax`` group with the LARGEST time suffix (the final
cumulative peak-depth field -- MDAL's own group ORDER is alphabetical, not
chronological, so picking "the last group" by index would often land on an
EARLY, near-zero timestep instead), else a tracer/concentration group, so the
mesh renders something meaningful the instant it lands. The libhdf5 "File Type"
attribute warnings QGIS's MDAL/netCDF backend prints on open are benign (proven
live) and are not treated as failure -- only ``layer.isValid()`` gates success.
The staged ``.nc`` lives under the session temp dir and is swept on
disconnect/close (session TTL) -- never a persistent download.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from typing import List, Optional, Tuple

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsDateTimeRange,
    QgsMeshDatasetIndex,
    QgsMeshLayer,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QDateTime, Qt

from . import formatting
from ..plugin_settings import PluginSettings
from ..net.trid3nt_client import LayerEvent, qgis_xyz_uri, s3_to_vsis3

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")

#: SFINCS quadtree dataset group name for a cumulative max-depth field ending
#: at time offset N (seconds) -- see ``_select_peak_depth_dataset_group``.
_PEAK_DEPTH_GROUP_RE = re.compile(r"^maximum_water_depth_timemax:(\d+)$")

#: Tracer/concentration dataset-group name fragments (TELEMAC-2D DYE, generic
#: tracers) -- ``_select_tracer_dataset_group`` prefers one of these as the
#: active scalar so a mesh whose "interesting" field is a tracer (not depth)
#: renders the plume by default instead of MDAL's first group (velocity/bed).
_TRACER_GROUP_HINTS = ("dye", "tracer", "concentration", "conc")

#: The OSM raster tile TEMPLATE ensure_basemap() adds (contains {z}/{x}/{y}).
_OSM_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_OSM_LAYER_NAME = "OpenStreetMap"

# BK-1: base-map preset library (Settings dropdown). Each entry = (layer name,
# XYZ template, zmax). Names double as the QGIS layer names so switching
# presets can find + remove the previous one. ESRI imagery is the satellite
# view NATE wants under the TELEMAC mesh wireframe.
BASEMAP_PRESETS = {
    "OpenStreetMap": (_OSM_LAYER_NAME, _OSM_TEMPLATE, 19),
    "ESRI World Imagery (satellite)": (
        "ESRI World Imagery",
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
        "MapServer/tile/{z}/{y}/{x}",
        19,
    ),
    "CartoDB Dark Matter": (
        "CartoDB Dark Matter",
        "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        20,
    ),
}
_ALL_BASEMAP_LAYER_NAMES = [v[0] for v in BASEMAP_PRESETS.values()]

#: Prefix of every TRID3NT-owned layer-tree group -- the live per-case group
#: ("TRID3NT <case>", ``LayerMaterializer.set_case``). ITEM A (case-switch
#: clear) matches on this prefix so every such group is swept on a case-open
#: rebind (a legacy "TRID3NT export <case>" group from a pre-streaming session
#: still matches and is cleaned too). The OpenStreetMap basemap is added
#: directly at layerTreeRoot (never inside a group -- see ``ensure_basemap``)
#: so it never matches this prefix and is never touched.
_GROUP_PREFIX = "TRID3NT "


def _safe_filename(name: str) -> str:
    return _SAFE_NAME.sub("_", name).strip("_") or "layer"


# -- session-scoped staging temp dir
#
# Streaming is the path; the ONE cache hop (MDAL, which has no /vsi layer)
# stages a local copy. Every staged byte lives under a per-SESSION
# subdir (``trid3nt_session_<tag>``) beneath the platform temp, is cleaned up
# when the dock disconnects/closes, and any crash leftover is swept at plugin
# start. A dir carries its owner PID so the start sweep can tell a crash
# leftover (dead PID) from a CONCURRENT live QGIS instance (live PID -- never
# swept), so nothing a running session staged is ever deleted out from under it.

_SESSION_DIR_PREFIX = "trid3nt_session_"
_OWNER_PID_FILE = ".owner_pid"


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` names a live process. ``os.kill(pid, 0)`` is the POSIX
    liveness probe; any error other than "no such process" (permission, or a
    platform where signal 0 is unsupported) is treated as ALIVE so the stale
    sweep never deletes a dir it cannot prove is dead."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:  # noqa: BLE001 -- permission / unsupported -> assume alive
        return True
    return True


def sweep_stale_session_dirs() -> int:
    """Remove crash-leftover session temp dirs at plugin start; return the count
    swept. A ``trid3nt_session_*`` dir is removed only when its owner PID is
    DEAD (or unreadable) -- a dir owned by a live process (a concurrent QGIS
    instance, or this one) is left alone. Best-effort: never raises."""
    swept = 0
    try:
        root = tempfile.gettempdir()
        for name in os.listdir(root):
            if not name.startswith(_SESSION_DIR_PREFIX):
                continue
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            pid = None
            try:
                with open(os.path.join(path, _OWNER_PID_FILE), encoding="utf-8") as f:
                    pid = int(f.read().strip())
            except (OSError, ValueError):
                pid = None
            if pid is not None and _pid_alive(pid):
                continue  # a live session owns it -- never touch
            shutil.rmtree(path, ignore_errors=True)
            swept += 1
    except Exception:  # noqa: BLE001 -- honest no-op, never a crash on start
        return swept
    return swept


def _streamed_note(kind: str, extra: str = "") -> str:
    """The STREAMED label (no local copy) -- the honesty-floor marker every
    /vsis3 layer carries so a remote-streamed layer is never confused with a
    downloaded one."""
    tail = f", {extra}" if extra else ""
    return f"{kind} streamed via /vsis3 (no local copy{tail})"


# -- the store, configured once --------------------------------------------- #


def configure_store_access(
    endpoint: str, access_key: str, secret_key: str, region: str
) -> Optional[str]:
    """Point GDAL's ``/vsis3`` at the object store. Called once per session.

    ``endpoint`` is the store's base url (``http://host:9000``); GDAL wants the
    host:port alone plus an explicit ``AWS_HTTPS`` flag, and MinIO serves
    path-style buckets, so virtual hosting is off. PAM is disabled process-wide
    because GDAL writes a ``.aux.xml`` sidecar BESIDE the dataset it opened --
    against ``/vsis3`` that is a write into the store on every read.

    Returns an honest note on failure (GDAL absent / unusable), else None.
    """
    try:
        from osgeo import gdal
    except Exception as exc:  # noqa: BLE001 -- no GDAL bindings: honest note
        return f"store access not configured ({type(exc).__name__}: {exc})"
    scheme, _, rest = (endpoint or "").strip().rpartition("://")
    host = rest.strip("/")
    for key, value in (
        ("AWS_S3_ENDPOINT", host),
        ("AWS_HTTPS", "YES" if scheme == "https" else "NO"),
        ("AWS_VIRTUAL_HOSTING", "FALSE"),
        ("AWS_ACCESS_KEY_ID", access_key),
        ("AWS_SECRET_ACCESS_KEY", secret_key),
        ("AWS_REGION", region),
        ("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR"),
        ("GDAL_PAM_ENABLED", "NO"),
    ):
        gdal.SetConfigOption(key, value)
    return None


# -- basemap + canvas zoom (the "canvas is just white" fix) ------------------ #


def ensure_basemap(preset: str = "OpenStreetMap") -> Optional[str]:
    """Ensure the CHOSEN base-map preset (BK-1 Settings dropdown) is the one
    on the map: adds it if missing (inserted LAST -- bottom of the stack) and
    removes any OTHER preset's layer so switching in Settings swaps cleanly.
    Returns a status note, or None when the chosen preset is already there.
    Never raises -- a rejected uri is an honest note, not a crash.
    """
    name, template, zmax = BASEMAP_PRESETS.get(
        preset, BASEMAP_PRESETS["OpenStreetMap"]
    )
    project = QgsProject.instance()
    # drop other presets' layers (switching satellite <-> dark <-> osm)
    removed_other = False
    for other in _ALL_BASEMAP_LAYER_NAMES:
        if other == name:
            continue
        for lyr in project.mapLayersByName(other):
            project.removeMapLayer(lyr.id())
            removed_other = True
    if project.mapLayersByName(name):
        return f"basemap switched to {name}" if removed_other else None
    uri = qgis_xyz_uri(template, zmin=0, zmax=zmax)
    layer = QgsRasterLayer(uri, name, "wms")
    if not layer.isValid():
        return f"{name} basemap: QGIS rejected the XYZ uri -- skipped"
    project.addMapLayer(layer, False)
    project.layerTreeRoot().addLayer(layer)  # appends LAST -- bottom of stack
    return f"{name} basemap added"


def zoom_to_extent(canvas, rect: Optional["QgsRectangle"], margin: float = 0.1) -> bool:
    """Zoom ``canvas`` to ``rect`` (already in the canvas' own CRS), scaled
    out by ``margin`` (10% default) so features are not flush against the
    view edge. Returns False (no-op) on an empty/None rect or any failure --
    never raises.
    """
    try:
        if rect is None or rect.isEmpty():
            return False
        # A NaN/inf bound DEFEATS ``isEmpty()`` (every NaN comparison is False),
        # so a rectangle built from a layer whose extent is non-finite (a mesh
        # with an all-nodata scalar, a broken CRS transform) slips through here.
        # Feeding it to the native ``scale()`` / ``setExtent()`` propagates the
        # non-finite double into the canvas map-to-pixel transform -- the same
        # class of native numeric hazard the formatting clamps guard. Refuse it.
        if not all(
            formatting.is_finite_number(b)
            for b in (rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())
        ):
            return False
        scaled = QgsRectangle(rect)
        scaled.scale(1.0 + margin)
        canvas.setExtent(scaled)
        canvas.refresh()
        return True
    except Exception:  # noqa: BLE001 -- honest no-op, never a crash
        return False


def zoom_to_bbox4326(
    canvas, bbox: Tuple[float, float, float, float], margin: float = 0.1
) -> bool:
    """Zoom ``canvas`` to an EPSG:4326 ``(lon_min, lat_min, lon_max, lat_max)``
    bbox, transformed to the canvas' destination CRS via
    ``QgsCoordinateTransform`` (the project's transform context), scaled out
    by ``margin``. Returns False (no-op) on any transform failure -- never
    raises.
    """
    try:
        lon_min, lat_min, lon_max, lat_max = bbox
        rect = QgsRectangle(lon_min, lat_min, lon_max, lat_max)
        dst_crs = canvas.mapSettings().destinationCrs()
        src_crs = QgsCoordinateReferenceSystem("EPSG:4326")
        if src_crs != dst_crs:
            transform = QgsCoordinateTransform(
                src_crs, dst_crs, QgsProject.instance().transformContext()
            )
            rect = transform.transformBoundingBox(rect)
    except Exception:  # noqa: BLE001 -- honest no-op, never a crash
        return False
    return zoom_to_extent(canvas, rect, margin=margin)


# -- Temporal Controller stamping -------------------------------------------- #


def _temporal_qdt(text) -> Optional[QDateTime]:
    """A DECLARED ISO-8601 UTC instant -> ``QDateTime``, else ``None``.

    The trailing Z parses as UTC on both Qt5 and Qt6; anything the parser
    refuses is an absent time, never a guessed one.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    stamp = QDateTime.fromString(text.strip(), Qt.DateFormat.ISODate)
    return stamp if stamp.isValid() else None


def _widen_project_temporal_range(begin: QDateTime, end: QDateTime) -> None:
    """Grow the project temporal range to cover [begin, end) so the Temporal
    Controller picks the sequence up immediately (existing coverage is kept)."""
    settings = QgsProject.instance().timeSettings()
    try:
        current = settings.temporalRange()
        if (
            current is not None
            and not current.isInfinite()
            and current.begin().isValid()
            and current.end().isValid()
        ):
            if current.begin() < begin:
                begin = current.begin()
            if current.end() > end:
                end = current.end()
    except (AttributeError, TypeError):
        pass  # unreadable current range -- just set this layer's span
    settings.setTemporalRange(QgsDateTimeRange(begin, end))


def _fixed_temporal_mode(props):
    """The FixedTemporalRange mode enum, across QGIS API generations."""
    try:
        from qgis.core import Qgis

        return Qgis.RasterTemporalMode.FixedTemporalRange
    except (ImportError, AttributeError):
        return props.ModeFixedTemporalRange


def stamp_raster_temporal(layer, event: LayerEvent) -> Optional[str]:
    """Stamp a raster with the validity window its row DECLARED.

    One frame of a sequence carries its own ``valid_from``/``valid_to``, so the
    Temporal Controller plays the sequence from the producer's own instants --
    there is no name to parse and no synthetic clock to invent. A row that
    declares no window is not a frame and is left alone. Never raises.
    """
    begin = _temporal_qdt((event.raw or {}).get("valid_from"))
    end = _temporal_qdt((event.raw or {}).get("valid_to"))
    if begin is None or end is None:
        return None
    try:
        props = layer.temporalProperties()
        props.setMode(_fixed_temporal_mode(props))
        props.setFixedTemporalRange(QgsDateTimeRange(begin, end))
        props.setIsActive(True)
        _widen_project_temporal_range(begin, end)
    except Exception as exc:  # noqa: BLE001 -- honest note, never a lost layer
        return f"temporal stamp failed ({type(exc).__name__}: {exc})"
    return (
        f"valid {begin.toString(Qt.DateFormat.ISODate)} - the Temporal "
        "Controller plays the sequence (View > Panels > Temporal Controller)"
    )


def stamp_mesh_temporal(layer, event: LayerEvent) -> Optional[str]:
    """Point a mesh layer's time axis at the instant its run DECLARED as zero.

    MDAL activates a mesh layer's temporal properties itself, but a SELAFIN
    records no origin for the seconds it counts, so the controller scrubs 1900
    until the run says when zero was. Never raises.
    """
    reference = _temporal_qdt((event.raw or {}).get("reference_time"))
    if reference is None:
        return None
    try:
        layer.setReferenceTime(reference)
        props = layer.temporalProperties()
        props.setIsActive(True)
        extent = props.timeExtent()
        if extent.begin().isValid() and extent.end().isValid():
            _widen_project_temporal_range(extent.begin(), extent.end())
    except Exception as exc:  # noqa: BLE001 -- honest note, never a lost layer
        return f" -- temporal stamp failed ({type(exc).__name__}: {exc})"
    return (
        f" -- time axis from {reference.toString(Qt.DateFormat.ISODate)}; scrub "
        "it in View > Panels > Temporal Controller"
    )


# -- mesh outputs (MDAL) ----------------------------------------------------- #


def _select_peak_depth_dataset_group(layer) -> bool:
    """Set ``layer``'s active scalar dataset group to the
    ``maximum_water_depth_timemax:<seconds>`` group with the LARGEST time
    suffix -- the cumulative max-depth field at the end of the run, i.e. the
    real "peak flood depth" (MDAL enumerates dataset groups in the file's own
    variable order, which is ALPHABETICAL for these names, not chronological
    -- naively picking "the last matching group encountered" can land on an
    EARLY timestep instead of the true peak). Returns True when a group was
    selected; False (a no-op) when the mesh carries no such group -- QGIS's
    own default selection stands, never a crash.
    """
    best_index = None
    best_time = -1
    for i in range(layer.datasetGroupCount()):
        try:
            name = layer.datasetGroupMetadata(QgsMeshDatasetIndex(i, 0)).name()
        except Exception:  # noqa: BLE001 -- a bad group index is skipped, not fatal
            continue
        match = _PEAK_DEPTH_GROUP_RE.match(name or "")
        if not match:
            continue
        t = int(match.group(1))
        if t > best_time:
            best_time = t
            best_index = i
    if best_index is None:
        return False
    settings = layer.rendererSettings()
    settings.setActiveScalarDatasetGroup(best_index)
    layer.setRendererSettings(settings)
    return True


def _select_tracer_dataset_group(layer) -> bool:
    """Set ``layer``'s active scalar dataset group to a TRACER/concentration
    group (TELEMAC-2D ``DYE``, or any group whose name hints a tracer) so the
    plume is the DEFAULT-rendered field. MDAL activates its FIRST group (for a
    TELEMAC ``.slf`` that is VELOCITY U -- not the dye), so without this the
    native mesh loads but shows the wrong variable. Returns True when a tracer
    group was selected; False (a no-op) when the mesh carries none -- QGIS's own
    default selection stands, never a crash. Additive to
    ``_select_peak_depth_dataset_group`` (SFINCS depth wins first; this is the
    fallback for tracer meshes) so no flood engine regresses."""
    for i in range(layer.datasetGroupCount()):
        try:
            name = layer.datasetGroupMetadata(QgsMeshDatasetIndex(i, 0)).name()
        except Exception:  # noqa: BLE001 -- a bad group index is skipped, not fatal
            continue
        low = (name or "").lower()
        if any(h in low for h in _TRACER_GROUP_HINTS):
            settings = layer.rendererSettings()
            settings.setActiveScalarDatasetGroup(i)
            layer.setRendererSettings(settings)
            return True
    return False


def _clamp_mesh_scalar_classification(layer) -> Optional[str]:
    """Pin EVERY scalar dataset group's colour classification to a FINITE range.

    The mesh analogue of the raster ``sane_range`` guard, and the reason it is
    load-bearing on arm64: an MDAL scalar group whose statistics are degenerate
    -- an all-nodata timestep, an all-dry SFINCS depth field, a group the
    temporal scrubber steps onto that carries no wet cell -- reports a NaN/inf
    (or zero-span) ``minimum()/maximum()``. Left to itself QGIS builds the mesh
    colour-ramp legend from exactly that range, and the SAME non-finite ->
    INT_MAX precision saturation that crashes the raster path in
    ``qt_doubleToAscii`` fires here (the 0.3.8 fix never covered meshes).

    We clamp ALL groups, not merely the initially-active one. The active group
    is set once at add time, but the user drives the mesh live: switching the
    active scalar group from the layer styling panel hands that group's
    classification straight to the native renderer, so a single degenerate
    NON-active group is a latent mid-session crash (the SFINCS/TELEMAC meshes
    here carry many groups -- depth, water level, velocity magnitude, tracer --
    and any one can be all-dry / all-nodata). Pinning a user-set classification
    on every group additionally stops QGIS re-deriving a per-timestep range
    while the Temporal Controller scrubs, so an empty timestep inside an
    otherwise-wet group cannot regenerate a NaN range either. Each group's
    extremes route through ``formatting.sane_range`` and are written back with
    ``setClassificationMinimumMaximum`` BEFORE the layer reaches the canvas.
    Returns an honest substitution note counting the degenerate groups, or
    ``None`` when every range was already sane / there is nothing to classify.
    Never raises -- a defensive no-op beats a lost mesh.
    """
    try:
        settings = layer.rendererSettings()
        group_count = int(layer.datasetGroupCount())
    except Exception:  # noqa: BLE001 -- classification is best-effort, never fatal
        return None
    touched = 0
    degenerate = 0
    for group_index in range(group_count):
        try:
            scalar = settings.scalarSettings(group_index)
            if scalar is None:
                continue
            meta = layer.datasetGroupMetadata(QgsMeshDatasetIndex(group_index, 0))
            raw_min, raw_max = meta.minimum(), meta.maximum()
            vmin, vmax = formatting.sane_range(raw_min, raw_max)
            scalar.setClassificationMinimumMaximum(vmin, vmax)
            settings.setScalarSettings(group_index, scalar)
            touched += 1
            if not formatting.is_sane_range(raw_min, raw_max):
                degenerate += 1
        except Exception:  # noqa: BLE001 -- a bad group is skipped, not fatal
            continue
    if touched == 0:
        return None
    try:
        layer.setRendererSettings(settings)
    except Exception:  # noqa: BLE001 -- never fatal
        return None
    if degenerate == 0:
        return None
    plural = "s" if degenerate != 1 else ""
    return (
        f" -- {degenerate} degenerate scalar range{plural} clamped to a "
        "finite colour scale (native legend crash-guard)"
    )


# -- the resolved preset, loaded as QGIS's own style document ----------------- #


def load_declared_style(layer, legend: Optional[dict], temp_dir: str) -> Optional[str]:
    """Load the layer's RESOLVED preset onto it, and say what happened.

    The legend carries the preset already resolved into a ``.qml`` - QGIS's own
    declarative style format - so the render is QGIS reading its own document
    rather than this side rebuilding a renderer out of a colour-ramp name and a
    range. ``qml`` is absent for a layer whose file already carries its colours
    (an RGB(A) composite, a COG with a band-1 colour table): QGIS's own default
    renderer IS the correct render for those, and overriding it would repaint a
    picture the producer had already painted.

    ``loadNamedStyle``'s boolean is well-formedness only, so a document that
    loads without changing the renderer still reports honestly. Never raises --
    a styling failure is a note, never a lost layer.
    """
    qml = (legend or {}).get("qml") if isinstance(legend, dict) else None
    if not isinstance(qml, str) or not qml.strip():
        return None
    path = os.path.join(temp_dir, f"style_{uuid.uuid4().hex[:12]}.qml")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(qml)
        before = _renderer_tag(layer)
        message, ok = layer.loadNamedStyle(path)
        if not ok:
            return f"style not loaded ({message or 'rejected by QGIS'})"
        after = _renderer_tag(layer)
        if after == before:
            return f"style loaded but the renderer is unchanged ({after})"
        return f"styled from the declared preset ({after})"
    except Exception as exc:  # noqa: BLE001 -- honest note, never a lost layer
        return f"style load failed ({type(exc).__name__}: {exc})"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _renderer_tag(layer) -> str:
    """The layer's current renderer identity, for the before/after read-back."""
    try:
        renderer = layer.renderer()
        return type(renderer).__name__ if renderer is not None else "none"
    except (AttributeError, RuntimeError):
        return "none"


class LayerMaterializer:
    """Per-connection materializer: one group, one added-id set, one temp dir."""

    def __init__(self, settings: PluginSettings):
        self._settings = settings
        self._added_ids: set[str] = set()
        self._group_name: Optional[str] = None
        #: Per-session staging dir tag. One materializer = one dock
        #: connection = one session; its ``trid3nt_session_<tag>`` subdir holds
        #: every staged (non-streamable) artifact and is swept on close.
        self._session_tag: str = uuid.uuid4().hex[:12]
        self._temp_dir: Optional[str] = None
        #: Layers added by the MOST RECENT ``materialize`` call (reset at its
        #: top) -- lets the dock zoom to "what just landed" without re-deriving
        #: it from notes strings.
        self.last_added_layers: List = []

    # -- lifecycle ------------------------------------------------------------- #

    def set_case(self, case_id: str, title: Optional[str] = None) -> None:
        """Bind to a case: clears every stale TRID3NT layer-tree group (ITEM
        A -- this materializer's own previous-case group AND any "Open case
        in QGIS" export groups, which otherwise accumulate across switches),
        names the fresh layer-tree group, and resets dedup state so the
        case-open replay always repaints from a clean slate."""
        label = title or case_id[:8]
        self._group_name = f"TRID3NT {label}"
        self._clear_stale_groups()
        self._added_ids.clear()
        self.last_added_layers = []

    def _clear_stale_groups(self) -> None:
        """Remove every layer-tree group whose name starts with the TRID3NT
        group PREFIX (the live per-case group AND any "Open case in QGIS"
        export groups -- both share the prefix), along with the layers each
        one owns. Always clears ALL of them, including one that happens to
        share the incoming case's own group name -- a case-open always
        repaints its layers fresh (dedup state is reset right after), so
        keeping a same-named group around would only risk stacking
        duplicate layers into it.

        NEVER touches the OpenStreetMap basemap (added directly at
        layerTreeRoot, never inside a group -- see ``ensure_basemap``) or
        any non-TRID3NT group/layer the user added themselves. Never raises
        -- a half-torn-down project tree must not crash a case switch.
        """
        try:
            project = QgsProject.instance()
            root = project.layerTreeRoot()
            stale = [g for g in root.findGroups() if g.name().startswith(_GROUP_PREFIX)]
            for group in stale:
                try:
                    layer_ids = group.findLayerIds()
                    if layer_ids:
                        project.removeMapLayers(layer_ids)
                    root.removeChildNode(group)
                except Exception:  # noqa: BLE001 -- best-effort per-group cleanup
                    continue
        except Exception:  # noqa: BLE001 -- honest no-op, never a crash
            pass

    def _ensure_temp_dir(self) -> str:
        """The SESSION staging dir (``trid3nt_session_<tag>`` under the platform
        temp), created on first use with an owner-PID marker so a later plugin
        start can distinguish this dir's crash leftover from a concurrent live
        instance's. Recreated if it was swept underneath us."""
        if self._temp_dir is None or not os.path.isdir(self._temp_dir):
            path = os.path.join(
                tempfile.gettempdir(), f"{_SESSION_DIR_PREFIX}{self._session_tag}"
            )
            os.makedirs(path, exist_ok=True)
            try:
                with open(os.path.join(path, _OWNER_PID_FILE), "w", encoding="utf-8") as f:
                    f.write(str(os.getpid()))
            except OSError:
                pass  # marker is best-effort; staging still works without it
            self._temp_dir = path
        return self._temp_dir

    def cleanup_session(self) -> None:
        """Remove this session's staging dir and everything staged in it (the
        session-TTL cleanup): called on dock disconnect and on dock
        close/plugin unload. Best-effort -- a cleanup failure is never a crash,
        and any residue is caught by ``sweep_stale_session_dirs`` next start."""
        path = self._temp_dir
        self._temp_dir = None
        if path and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)

    def _ensure_group(self):
        root = QgsProject.instance().layerTreeRoot()
        name = self._group_name or "TRID3NT"
        group = root.findGroup(name)
        if group is None:
            group = root.insertGroup(0, name)
        return group

    # -- materialization -------------------------------------------------------- #

    def materialize(self, events: List[LayerEvent]) -> List[str]:
        """Add any NEW layers from a session-state snapshot.

        Returns human-readable status notes (one per action/skip) for the
        dock's status lines. Never raises -- a bad layer yields a note, not a
        crash (honesty floor: failures are visible, not silent).
        """
        notes: List[str] = []
        self.last_added_layers = []
        for event in events:
            if event.layer_id in self._added_ids:
                # A layer already on the canvas can still be TAKEN OFF it: the
                # un-emit half of the presentation surface arrives as a
                # visibility flip on a row this materializer has already seen.
                self._apply_visibility(event)
                continue
            try:
                note = self._materialize_one(event)
            except Exception as exc:  # noqa: BLE001
                note = f"layer '{event.name}': failed ({type(exc).__name__}: {exc})"
            if note is not None:
                # Mark handled even on skip/failure so the same row does not
                # re-note on every session-state replay of the snapshot.
                self._added_ids.add(event.layer_id)
                notes.append(note)
        return notes

    def _materialize_one(self, event: LayerEvent) -> Optional[str]:
        if event.layer_type == "raster":
            return self._add_raster(event)
        if event.layer_type in ("vector", "geojson"):
            return self._add_vector(event)
        if event.layer_type in ("mesh", "ugrid"):
            return self._add_mesh(event)
        return f"layer '{event.name}': type '{event.layer_type}' not supported yet -- skipped"

    def _add_raster(self, event: LayerEvent) -> str:
        """A COG read in place through ``/vsis3``, styled from the event's legend."""
        path = s3_to_vsis3(event.uri or "")
        if path is None:
            return (
                f"raster '{event.name}': not an s3:// COG uri ({event.uri}) "
                "-- skipped"
            )
        layer = QgsRasterLayer(path, event.name, "gdal")
        if not layer.isValid():
            return f"raster '{event.name}': COG did not load ({path}) -- skipped"
        notes = [_streamed_note("COG raster")]
        notes.extend(n for n in (
            load_declared_style(layer, event.legend, self._ensure_temp_dir()),
            stamp_raster_temporal(layer, event),
        ) if n)
        return self._add_to_group(
            layer, event, f"raster '{event.name}' added ({'; '.join(notes)})")

    def _add_vector(self, event: LayerEvent) -> str:
        if event.inline_geojson is not None:
            path = os.path.join(
                self._ensure_temp_dir(),
                f"{_safe_filename(event.name)}_{event.layer_id[:8]}.geojson",
            )
            with open(path, "w", encoding="utf-8") as f:
                json.dump(event.inline_geojson, f)
            layer = QgsVectorLayer(path, event.name, "ogr")
            if not layer.isValid():
                return f"vector '{event.name}': GeoJSON did not load -- skipped"
            return self._add_to_group(
                layer, event, self._vector_note(
                    layer, event, "staged to session temp, inline GeoJSON"))

        path = s3_to_vsis3(event.uri or "")
        if path is None:
            return f"vector '{event.name}': no inline GeoJSON and non-s3 uri -- skipped"
        layer = QgsVectorLayer(path, event.name, "ogr")
        if not layer.isValid():
            return f"vector '{event.name}': stream failed ({path}) -- skipped"
        return self._add_to_group(
            layer, event, self._vector_note(layer, event, _streamed_note("vector")))

    def _vector_note(self, layer, event: LayerEvent, source_label: str) -> str:
        """Style the vector from its declared preset and say what landed."""
        style_note = load_declared_style(
            layer, event.legend, self._ensure_temp_dir())
        tail = f"; {style_note}" if style_note else ""
        return f"vector '{event.name}' added ({source_label}{tail})"

    def _stage_s3_to_session(self, s3_uri: str, filename: str) -> Optional[str]:
        """Copy a store object into the SESSION temp dir; return the local path.

        The ONE cache hop, for MDAL alone -- read through the SAME ``/vsis3``
        GDAL uses for everything else, so there is no second credential path.
        Returns None on any failure; the caller turns that into an honest skip
        note. The staged file is swept on disconnect/close."""
        src = s3_to_vsis3(s3_uri)
        if src is None:
            return None
        dest = os.path.join(self._ensure_temp_dir(), filename)
        try:
            from osgeo import gdal

            handle = gdal.VSIFOpenL(src, "rb")
            if handle is None:
                return None
            try:
                with open(dest, "wb") as f:
                    while True:
                        chunk = gdal.VSIFReadL(1, 1 << 20, handle)
                        if not chunk:
                            break
                        f.write(chunk)
            finally:
                gdal.VSIFCloseL(handle)
        except Exception:  # noqa: BLE001 -- honest None, caller notes the skip
            return None
        return dest

    def _add_mesh(self, event: LayerEvent) -> str:
        """Native MDAL mesh (SFINCS ``sfincs_map.nc``, TELEMAC ``*.slf`` and kin)
        -- a STAGED format. QGIS's MDAL provider demands a local path, so the
        object stages to the session temp dir first, then loads as
        ``QgsMeshLayer(local_path, name, "mdal")``. The staged filename PRESERVES
        the source object's extension (``.slf`` for a SELAFIN, ``.nc`` for a
        netCDF): MDAL selects its driver partly by extension, so a SELAFIN staged
        as ``.nc`` would be rejected. CRS comes from the row's ``crs_authid`` (MDAL
        reports an empty crs() for a SELAFIN and a SFINCS quadtree grid); the active
        scalar group is the cumulative peak-depth field, else a tracer group. Every
        outcome is an honest note; never raises."""
        uri = event.uri or ""
        if uri.startswith("s3://"):
            # Preserve the source extension so MDAL's extension-sensitive driver
            # selection loads a SELAFIN (.slf) as SELAFIN, not netCDF; default .nc
            # only when the uri carries no extension of its own.
            src_ext = os.path.splitext(uri.split("?", 1)[0])[1] or ".nc"
            fname = f"{_safe_filename(event.name)}_{event.layer_id[:8]}{src_ext}"
            local_path = self._stage_s3_to_session(uri, fname)
            if not local_path:
                return f"mesh '{event.name}': could not stage {uri} -- skipped"
        elif os.path.isfile(uri):
            local_path = uri  # already-local mesh path (test/headless drive)
        else:
            return f"mesh '{event.name}': non-s3 / unreadable uri ({uri}) -- skipped"
        layer = QgsMeshLayer(local_path, event.name, "mdal")
        if not layer.isValid():
            return f"mesh '{event.name}': QGIS/MDAL rejected the file -- skipped"
        note = (
            f"mesh '{event.name}' added (staged to session temp; MDAL "
            f"{os.path.splitext(local_path)[1].lstrip('.') or 'mesh'})"
        )
        crs_authid = (event.raw or {}).get("crs_authid")
        if isinstance(crs_authid, str) and crs_authid:
            crs = QgsCoordinateReferenceSystem(crs_authid)
            if crs.isValid():
                layer.setCrs(crs)
            else:
                note += " -- CRS unresolved, set manually via layer properties"
        else:
            note += " -- CRS unknown, set manually via layer properties"
        if not _select_peak_depth_dataset_group(layer):
            _select_tracer_dataset_group(layer)
        clamp_note = _clamp_mesh_scalar_classification(layer)
        if clamp_note:
            note += clamp_note
        temporal_note = stamp_mesh_temporal(layer, event)
        if temporal_note:
            note += temporal_note
        return self._add_to_group(layer, event, note)

    # -- project insertion helper -------------------------------------------- #

    def _add_to_group(self, layer, event: LayerEvent, note: str, group=None) -> str:
        """Add ``layer`` to the project + insert its tree node into
        ``group`` (default: this materializer's flat case group). ``group``
        lets ITEM C place a frame-sequence member straight into its
        animation subgroup at construction time -- see the module docstring
        above the animation-grouping helpers for why members are never
        relocated into a subgroup after the fact."""
        if formatting.is_finite_number(event.opacity):
            # ``max(0, min(1, nan))`` returns NaN (NaN defeats both bounds), so
            # guard finiteness BEFORE the native ``setOpacity`` rather than rely
            # on the clamp -- the boundary sweep already drops a non-finite
            # opacity, this is the belt-and-braces at the native seam.
            try:
                layer.setOpacity(max(0.0, min(1.0, float(event.opacity))))
            except (AttributeError, TypeError, ValueError):
                pass
        QgsProject.instance().addMapLayer(layer, False)
        # Charts-window 2026-08-04: stamp the source uri + layer id so the
        # ChartsWindow "Locate on map" affordance can match a chart's
        # ``source_layer_uri`` back to the loaded layer it was computed from.
        try:
            if event.uri:
                layer.setCustomProperty("trid3nt/source_uri", event.uri)
            layer.setCustomProperty("trid3nt/layer_id", event.layer_id)
        except Exception:  # noqa: BLE001 -- stamping is best-effort metadata
            pass
        target = group if group is not None else self._ensure_group()
        node = target.insertLayer(0, layer)
        if node is not None and not event.visible:
            node.setItemVisibilityChecked(False)
        self.last_added_layers.append(layer)
        return note

    def _apply_visibility(self, event) -> None:
        """Match the layer tree to the row's ``visible``. Never raises."""
        try:
            root = QgsProject.instance().layerTreeRoot()
            for layer in QgsProject.instance().mapLayers().values():
                if layer.customProperty("trid3nt/layer_id") != event.layer_id:
                    continue
                node = root.findLayer(layer.id())
                if node is not None and node.isVisible() != bool(event.visible):
                    node.setItemVisibilityChecked(bool(event.visible))
        except Exception:  # noqa: BLE001 -- visibility is best-effort, never fatal
            pass

    # -- extent union (canvas-zoom fallback, item 1) ---------------------------- #

    def combined_extent(self, dest_crs, layers: Optional[List] = None) -> Optional["QgsRectangle"]:
        """Combined extent of ``layers`` (default: ``self.last_added_layers``),
        each transformed into ``dest_crs``. Layers with an empty extent or an
        unresolvable CRS transform are skipped, never raised on. Returns None
        when nothing usable was found.
        """
        combined: Optional[QgsRectangle] = None
        for layer in (self.last_added_layers if layers is None else layers):
            try:
                extent = layer.extent()
                if extent is None or extent.isEmpty():
                    continue
                crs = layer.crs()
                if crs != dest_crs:
                    transform = QgsCoordinateTransform(
                        crs, dest_crs, QgsProject.instance().transformContext()
                    )
                    extent = transform.transformBoundingBox(extent)
            except Exception:  # noqa: BLE001 -- skip this layer, never raise
                continue
            if combined is None:
                combined = QgsRectangle(extent)
            else:
                combined.combineExtentWith(extent)
        return combined

    def last_added_vector_extent(self, dest_crs) -> Optional["QgsRectangle"]:
        """Combined extent of the VECTOR layers added by the most recent
        live ``materialize()`` call. XYZ raster layers (the live tile
        publishes) report a whole-world extent, so only vectors count here --
        the canvas-zoom fallback when a case-open carries no bbox."""
        vectors = [l for l in self.last_added_layers if isinstance(l, QgsVectorLayer)]
        return self.combined_extent(dest_crs, vectors)
