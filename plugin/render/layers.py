"""Layer materialization -- turn agent LayerEvents into native QGIS layers.

ONE STORE ONE SCHEME: every reference is an ``s3://`` uri read through
``/vsis3``, and the ONE cache hop is MDAL, which has no ``/vsi`` layer. A
preset is a BIRTH default: an adopted layer is never repainted."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from typing import List, Optional, Tuple
from xml.sax.saxutils import quoteattr

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

#: The row a mesh preset binds its dataset group with. QGIS remaps the
#: document's own group index through this NAME when it loads the style, so
#: the name has to be one the OPEN layer carries.
_MESH_GROUP_BINDING = re.compile(
    r"(<name-to-global-index\b[^>]*\bname=)(\"[^\"]*\"|'[^']*')")

#: The OSM raster tile TEMPLATE ensure_basemap() adds (contains {z}/{x}/{y}).
_OSM_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_OSM_LAYER_NAME = "OpenStreetMap"

# Base-map preset library. Each entry is (layer name, XYZ template, zmax), and
# the names double as the QGIS layer names, so switching presets can find and
# remove the previous one.
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

#: Prefix of every TRID3NT-owned layer-tree group. A case-open rebind sweeps
#: every group matching it, so a group left by another case is cleaned. A
#: basemap is added directly at layerTreeRoot, never inside a group, so it
#: never matches this prefix and is never touched.
_GROUP_PREFIX = "TRID3NT "

#: The case a TRID3NT group belongs to, stamped on the group node. A title can
#: collide between two cases; the id cannot, and a case-open has to be able to
#: tell its OWN group (adopted, with whatever styling its layers now carry)
#: from another case's (swept).
_CASE_PROPERTY = "trid3nt/case_id"


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
    """True when ``pid`` names a live process. Any error other than "no such
    process" reads as ALIVE, so the stale sweep never deletes a dir it cannot
    PROVE is dead."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:  # noqa: BLE001 -- permission / unsupported -> assume alive
        return True
    return True


def sweep_stale_session_dirs() -> int:
    """Remove crash-leftover session temp dirs; return the count swept. A dir
    goes only when its owner PID is DEAD or unreadable, so a concurrent live
    instance's staging is never deleted under it. Never raises."""
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
    """The STREAMED label every ``/vsis3`` layer carries, so a streamed layer
    is never confused with a downloaded one."""
    tail = f", {extra}" if extra else ""
    return f"{kind} streamed via /vsis3 (no local copy{tail})"


# -- the store, configured once --------------------------------------------- #


def configure_store_access(
    endpoint: str, access_key: str, secret_key: str, region: str
) -> Optional[str]:
    """Point GDAL's ``/vsis3`` at the object store, ONCE per session. A remote
    store is this endpoint VALUE, never a second code path. Returns an honest
    note when GDAL is absent or unusable, else None."""
    try:
        from osgeo import gdal
    except Exception as exc:  # noqa: BLE001 -- no GDAL bindings: honest note
        return f"store access not configured ({type(exc).__name__}: {exc})"
    # ``endpoint`` is a base url, but GDAL wants the host:port alone plus an
    # explicit ``AWS_HTTPS`` flag, and MinIO serves path-style buckets, so
    # virtual hosting goes off. PAM is disabled process-wide because GDAL
    # writes a ``.aux.xml`` sidecar BESIDE the dataset it opened -- against
    # ``/vsis3`` that is a write into the store on every read.
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
    """Make the CHOSEN base-map preset the one on the map, inserted LAST so it
    sits at the bottom of the stack, and remove any OTHER preset's layer. None
    when the chosen preset is already there; never raises."""
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
    """Zoom ``canvas`` to ``rect``, already in the canvas' own CRS, scaled out
    by ``margin`` so features are not flush against the view edge. False is a
    no-op on an empty, absent or non-finite rect; never raises."""
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
    bbox, transformed into the canvas' destination CRS. False is a no-op on any
    transform failure; never raises."""
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
    Anything the parser refuses is an ABSENT time, never a guessed one."""
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
    """Stamp a raster with the validity window its row DECLARED, so the
    Temporal Controller plays the sequence off the producer's own instants. A
    row declaring no window is not a frame and is left alone; never raises."""
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
    A SELAFIN records no origin for the seconds it counts, so without this the
    controller scrubs 1900. Never raises."""
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


def _mesh_group_names(layer) -> List[str]:
    """Every dataset group the OPEN layer reports, in MDAL's own spelling."""
    names: List[str] = []
    for i in range(layer.datasetGroupCount()):
        try:
            names.append(
                layer.datasetGroupMetadata(QgsMeshDatasetIndex(i, 0)).name() or "")
        except Exception:  # noqa: BLE001 -- a bad group index is skipped, not fatal
            names.append("")
    return names


def _active_scalar_group(layer) -> int:
    """The group index the mesh renders its scalar from right now."""
    try:
        return int(layer.rendererSettings().activeScalarDatasetGroup())
    except Exception:  # noqa: BLE001 -- unreadable settings read as "none active"
        return -1


def bind_declared_mesh_style(layer, legend: Optional[dict], temp_dir: str) -> str:
    """Load the declared preset onto a mesh, bound to one of ITS OWN groups.
    ALWAYS returns a note tail: an unbound quantity is a fact about the render,
    never a silence. Never raises -- a styling failure is a note, not a loss."""
    # A mesh preset paints ONE dataset group and QGIS binds that group BY NAME,
    # remapping the document's ``name-to-global-index`` row against the open
    # layer's groups on load; a name no group carries leaves the layer with NO
    # active scalar group -- a document that loads and renders nothing. MDAL
    # spells a SELAFIN's groups in the fixed-width names the file itself
    # carries (``dye             mgl``), so the declared quantity is resolved
    # against the names the OPEN layer reports and the match is written into
    # the document before QGIS reads it.
    qml = (legend or {}).get("qml") if isinstance(legend, dict) else None
    if not isinstance(qml, str) or not qml.strip():
        return " -- no declared preset on the row; MDAL's own default group stands"
    try:
        match = _MESH_GROUP_BINDING.search(qml)
        declared = match.group(2)[1:-1].strip() if match else ""
        if not declared:
            return (" -- the declared preset names no dataset group; MDAL's own "
                    "default group stands")
        names = _mesh_group_names(layer)
        wanted = declared.upper()
        index = next(
            (i for i, name in enumerate(names)
             if name.strip().upper().startswith(wanted)), None)
        if index is None:
            carried = ", ".join(repr(n.strip()) for n in names) or "none"
            return (f" -- the declared quantity {declared!r} names none of this "
                    f"mesh's dataset groups ({carried}); MDAL's own default "
                    "group stands")
        before = _active_scalar_group(layer)
        bound = names[index]
        document = _MESH_GROUP_BINDING.sub(
            lambda m: m.group(1) + quoteattr(bound), qml, count=1)
        ok, message = _load_style_document(layer, document, temp_dir)
        if not ok:
            return (f" -- the declared preset was rejected "
                    f"({message or 'rejected by QGIS'}); MDAL's own default "
                    "group stands")
        # loadNamedStyle's boolean is well-formedness only: QGIS accepts a
        # document whose renderer block it then drops, so the BINDING is read
        # back off the layer rather than believed.
        if _active_scalar_group(layer) != index:
            settings = layer.rendererSettings()
            settings.setActiveScalarDatasetGroup(before)
            layer.setRendererSettings(settings)
            return (f" -- the declared preset for {declared!r} did not bind; "
                    "MDAL's own default group stands")
        scalar = layer.rendererSettings().scalarSettings(index)
        # A CLIPPED shader leaves part of the mesh unpainted on purpose - below
        # the field's floor the quantity is absent - so the note says so rather
        # than leaving a reader to read holes as a failed render.
        clipped = ", clipped below" if scalar.colorRampShader().clip() else ""
        return (f" -- {bound.strip()!r} styled from the declared preset "
                f"({scalar.classificationMinimum():g} to "
                f"{scalar.classificationMaximum():g}{clipped})")
    except Exception as exc:  # noqa: BLE001 -- honest note, never a lost layer
        return f" -- mesh style load failed ({type(exc).__name__}: {exc})"


def _clamp_mesh_scalar_classification(layer) -> Optional[str]:
    """Pin EVERY scalar dataset group's classification to a FINITE range,
    before the layer reaches the canvas. Returns a substitution note counting
    the degenerate groups, or None when every range was already sane."""
    # A degenerate group -- an all-nodata timestep, an all-dry depth field --
    # reports a NaN/inf or zero-span minimum and maximum, and QGIS builds the
    # colour-ramp legend from exactly that, firing the same non-finite ->
    # INT_MAX precision saturation the raster path guards against.
    #
    # ALL groups are clamped, not merely the initially-active one: the user
    # switches the active scalar group live from the styling panel, which hands
    # that group's classification straight to the native renderer, so one
    # degenerate NON-active group is a latent mid-session crash. Pinning every
    # group also stops QGIS re-deriving a per-timestep range while the Temporal
    # Controller scrubs, so an empty timestep cannot regenerate a NaN range.
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
    """Load the layer's RESOLVED preset onto it and say what happened. An
    ABSENT ``qml`` means the file already carries its own colours and QGIS's
    default IS the correct render, so nothing is overridden."""
    # ``loadNamedStyle``'s boolean is well-formedness only, so a document that
    # loads without changing the renderer still has to report honestly.
    qml = (legend or {}).get("qml") if isinstance(legend, dict) else None
    if not isinstance(qml, str) or not qml.strip():
        return None
    try:
        before = _renderer_tag(layer)
        ok, message = _load_style_document(layer, qml, temp_dir)
        if not ok:
            return f"style not loaded ({message or 'rejected by QGIS'})"
        after = _renderer_tag(layer)
        if after == before:
            return f"style loaded but the renderer is unchanged ({after})"
        return f"styled from the declared preset ({after})"
    except Exception as exc:  # noqa: BLE001 -- honest note, never a lost layer
        return f"style load failed ({type(exc).__name__}: {exc})"


def _load_style_document(layer, document: str, temp_dir: str) -> Tuple[bool, str]:
    """Hand QGIS a ``.qml`` to read -> ``(loaded, message)``. ``loadNamedStyle``
    takes a PATH, so the document is written into the session temp dir and
    removed again: a style is a message, not an artifact."""
    path = os.path.join(temp_dir, f"style_{uuid.uuid4().hex[:12]}.qml")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(document)
        message, ok = layer.loadNamedStyle(path)
        return bool(ok), (message or "")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _renderer_tag(layer) -> str:
    """The layer's current rendering identity, for the before/after read-back.
    Renderer TYPE alone does not discriminate on a vector -- QGIS's default is
    already single-symbol -- so the tag carries the SYMBOL too."""
    try:
        renderer = layer.renderer()
        if renderer is None:
            return "none"
        tag = type(renderer).__name__
        symbol = getattr(renderer, "symbol", None)
        if symbol is None:
            return tag
        drawn = symbol()
        return (f"{tag}/{drawn.symbolLayer(0).layerType()} "
                f"{drawn.color().name()}")
    except (AttributeError, IndexError, RuntimeError):
        return "none"


class LayerMaterializer:
    """Per-connection materializer: one group, one added-id set, one temp dir."""

    def __init__(self, settings: PluginSettings):
        self._settings = settings
        self._added_ids: set[str] = set()
        self._group_name: Optional[str] = None
        self._case_id: Optional[str] = None
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
        """Bind to a case: adopt the layers the project already holds for it,
        clear every OTHER TRID3NT group, and name this case's group. An adopted
        layer is NEVER rebuilt and no preset is loaded over it."""
        # That adoption is what makes a preset a BIRTH default: the user's own
        # restyling is the project's to keep, and repainting it on reopen would
        # overrule a choice the user made in QGIS.
        label = title or case_id[:8]
        self._group_name = f"TRID3NT {label}"
        self._case_id = case_id
        self._added_ids.clear()
        self.last_added_layers = []
        self._clear_stale_groups(case_id)
        self._added_ids.update(self._adopt_existing_layers())

    def _clear_stale_groups(self, case_id: str) -> None:
        """Remove every TRID3NT layer-tree group EXCEPT this case's own. A
        basemap, and anything the user added themselves, is never touched.
        Never raises: a half-torn-down tree must not crash a case switch."""
        # Ownership is by the stamped case ID, not the group name, so a second
        # case whose title happens to collide is still a different group.
        try:
            project = QgsProject.instance()
            root = project.layerTreeRoot()
            stale = [g for g in root.findGroups()
                     if g.name().startswith(_GROUP_PREFIX)
                     and g.customProperty(_CASE_PROPERTY) != case_id]
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

    def _adopt_existing_layers(self) -> set:
        """The layer ids this case's surviving group already holds. Adopting
        them stops the replay adding a second copy, and stops a preset loading
        over a styling choice already made."""
        adopted: set = set()
        try:
            for layer in QgsProject.instance().mapLayers().values():
                layer_id = layer.customProperty("trid3nt/layer_id")
                if isinstance(layer_id, str) and layer_id:
                    adopted.add(layer_id)
        except Exception:  # noqa: BLE001 -- an unreadable tree adopts nothing
            return set()
        return adopted

    def _ensure_temp_dir(self) -> str:
        """The SESSION staging dir, created on first use with an owner-PID
        marker so a later start can tell a crash leftover from a live
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
        """Remove this session's staging dir and everything staged in it.
        Best-effort: a failure is never a crash, and residue is caught by the
        stale sweep at the next start."""
        path = self._temp_dir
        self._temp_dir = None
        if path and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)

    def _ensure_group(self):
        root = QgsProject.instance().layerTreeRoot()
        name = self._group_name or "TRID3NT"
        # By CASE first: the case is the identity, and a case reopened under a
        # different title must land back in the group its layers already live
        # in rather than beside it.
        group = next((g for g in root.findGroups()
                      if self._case_id
                      and g.customProperty(_CASE_PROPERTY) == self._case_id), None)
        if group is None:
            group = root.findGroup(name) or root.insertGroup(0, name)
        if self._case_id:
            group.setCustomProperty(_CASE_PROPERTY, self._case_id)
        return group

    # -- materialization -------------------------------------------------------- #

    def materialize(self, events: List[LayerEvent]) -> List[str]:
        """Add any NEW layers from a session-state snapshot -> one status note
        per action or skip. Never raises: a bad layer yields a note, so a
        failure is VISIBLE rather than silent."""
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
        """Copy a store object into the SESSION temp dir -> the local path.
        The ONE cache hop, read through the SAME ``/vsis3`` as everything else
        so there is no second credential path. None on any failure."""
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
        """Native MDAL mesh, the one STAGED format: the provider demands a
        local path. CRS comes from the row, because MDAL reports an empty
        ``crs()`` for a SELAFIN and a quadtree grid. Every outcome is a note."""
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
        note += self._load_mesh_datasets(layer, event)
        crs_authid = (event.raw or {}).get("crs_authid")
        if isinstance(crs_authid, str) and crs_authid:
            crs = QgsCoordinateReferenceSystem(crs_authid)
            if crs.isValid():
                layer.setCrs(crs)
            else:
                note += " -- CRS unresolved, set manually via layer properties"
        else:
            note += " -- CRS unknown, set manually via layer properties"
        # The clamp runs BEFORE the preset: it pins EVERY group's
        # classification to a finite range, and the declared style then wins on
        # the one group it binds (QGIS leaves the other groups' settings
        # untouched when it loads a mesh style).
        clamp_note = _clamp_mesh_scalar_classification(layer)
        if clamp_note:
            note += clamp_note
        note += bind_declared_mesh_style(
            layer, event.legend, self._ensure_temp_dir())
        temporal_note = stamp_mesh_temporal(layer, event)
        if temporal_note:
            note += temporal_note
        return self._add_to_group(layer, event, note)

    def _load_mesh_datasets(self, layer, event: LayerEvent) -> str:
        """Load the row's own dataset files onto the mesh, before it is styled.

        A derived group is written BESIDE the mesh it was measured over, so the
        layer that paints one has to carry both files. Every outcome is a note.
        """
        declared = (event.raw or {}).get("dataset_uris")
        if not isinstance(declared, list) or not declared:
            return ""
        loaded, missed = 0, []
        for index, uri in enumerate(declared):
            if not isinstance(uri, str) or not uri:
                continue
            if uri.startswith("s3://"):
                ext = os.path.splitext(uri.split("?", 1)[0])[1] or ".dat"
                path = self._stage_s3_to_session(
                    uri, f"{_safe_filename(event.name)}_{event.layer_id[:8]}"
                         f"_{index}{ext}")
            else:
                path = uri if os.path.isfile(uri) else None
            if path and layer.addDatasets(path):
                loaded += 1
            else:
                missed.append(uri)
        note = f" -- {loaded} dataset group(s) loaded beside the mesh" if loaded else ""
        if missed:
            note += (f" -- MDAL rejected or could not stage "
                     f"{', '.join(repr(u) for u in missed)}")
        return note

    # -- project insertion helper -------------------------------------------- #

    def _add_to_group(self, layer, event: LayerEvent, note: str, group=None) -> str:
        """Add ``layer`` to the project and insert its tree node into
        ``group``, defaulting to this materializer's case group. Placement is
        at CONSTRUCTION time: a node is never relocated afterwards."""
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
        # Stamp the source uri and layer id, so a chart's ``source_layer_uri``
        # can be matched back to the loaded layer it was computed from.
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

    # -- extent union (canvas-zoom fallback) ----------------------------------- #

    def combined_extent(self, dest_crs, layers: Optional[List] = None) -> Optional["QgsRectangle"]:
        """Combined extent of ``layers``, default the most recent additions,
        each transformed into ``dest_crs``. An empty extent or an unresolvable
        transform is skipped; None when nothing usable was found."""
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
        materialize call. Only vectors count, because an XYZ raster reports a
        whole-world extent that would swallow the zoom."""
        vectors = [l for l in self.last_added_layers if isinstance(l, QgsVectorLayer)]
        return self.combined_extent(dest_crs, vectors)
