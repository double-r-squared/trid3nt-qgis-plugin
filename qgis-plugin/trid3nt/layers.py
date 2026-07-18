"""Layer materialization -- turn agent LayerEvents into native QGIS layers.

The differentiator: every layer the agent publishes lands in the QGIS layer
tree, grouped under "TRID3NT <case>".

  raster  the local agent publishes a ready TiTiler XYZ tile TEMPLATE
          (contains {z}/{x}/{y}) in ``uri`` -> QGIS XYZ raster layer
          (``type=xyz&url=...``, wms provider).
  vector  inline GeoJSON (the agent's additive ``inline_geojson`` merge) ->
          temp ``.geojson`` file -> ogr layer. An ``s3://`` uri without inline
          GeoJSON resolves through the local MinIO http form
          (``http://127.0.0.1:9000/<bucket>/<key>`` via GDAL ``/vsicurl/``)
          when mode=local; in remote mode it is skipped with a status line
          (milestone 2+: presigned fetch).

Dedup: by ``layer_id`` -- session-state is replayed on every emit (A.7
replace-not-reconcile), so the same rows arrive many times per turn.

Temporal animation: after layers land (both the live-stream and the
exported-case paths), frame-sequence rasters (``Flood_depth_step_1..N``,
``F+03h`` stacks, ISO valid-time frames -- the same series the web scrubber
groups) are stamped with per-layer fixed temporal ranges so the built-in
QGIS Temporal Controller plays them natively. Pure grouping/range math lives
in ``temporal`` (tested without QGIS); ``stamp_temporal`` here applies it.

Mesh outputs (MDAL phase 1): ``materialize_export`` also loads each of the
export plan's ``mesh_entries`` (``case_export.plan_export_layers`` -- a
locally-downloaded SFINCS ``sfincs_map.nc``) as a native
``QgsMeshLayer(local_path, name, "mdal")``, a SIBLING of the export's
vector/raster layers (never nested into an animation subgroup -- a mesh
carries its OWN internal dataset-group time series, it is not a member of
the flood-depth-COG frame sequence). QGIS's MDAL provider reports an EMPTY
crs() for a SFINCS quadtree NetCDF (proven live 2026-07-10), so
``setCrs(QgsCoordinateReferenceSystem(crs_authid))`` is applied explicitly
from the export entry's ``crs_authid``; when that is unresolved the layer is
still added with an honest dock note instead of a silent wrong-CRS render.
The active scalar dataset group is set to the ``maximum_water_depth_timemax``
group with the LARGEST time suffix (the final cumulative peak-depth field --
MDAL's own group ORDER is alphabetical, not chronological, so picking "the
last group" by index would often land on an EARLY, near-zero timestep
instead) so the mesh renders something meaningful the instant it lands. The
libhdf5 "File Type" attribute warnings QGIS's MDAL/netCDF backend prints on
open are benign (proven live) and are not treated as failure -- only
``layer.isValid()`` gates success.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
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

from . import temporal
from .plugin_settings import MODE_LOCAL, PluginSettings
from .trid3nt_client import LayerEvent, qgis_xyz_uri, s3_to_http

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

#: Prefix shared by every TRID3NT-owned layer-tree group: the live per-case
#: group ("TRID3NT <case>", ``LayerMaterializer.set_case``) AND the "Open
#: case in QGIS" export group ("TRID3NT export <case>",
#: ``materialize_export``). ITEM A (case-switch clear) matches on this
#: prefix so BOTH kinds are swept on a case-open rebind. The OpenStreetMap
#: basemap is added directly at layerTreeRoot (never inside a group -- see
#: ``ensure_basemap``) so it never matches this prefix and is never touched.
_GROUP_PREFIX = "TRID3NT "


def _safe_filename(name: str) -> str:
    return _SAFE_NAME.sub("_", name).strip("_") or "layer"


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


# -- Temporal Controller stamping (frame-sequence animation) ----------------- #


def _temporal_qdt(dt) -> QDateTime:
    """An aware-UTC ``datetime`` -> ``QDateTime`` (ISO round trip -- the
    trailing Z parses as UTC on both Qt5 and Qt6)."""
    return QDateTime.fromString(dt.strftime("%Y-%m-%dT%H:%M:%SZ"), Qt.ISODate)


def _fixed_temporal_mode(props):
    """The FixedTemporalRange mode enum, across QGIS API generations."""
    try:
        from qgis.core import Qgis

        return Qgis.RasterTemporalMode.FixedTemporalRange
    except (ImportError, AttributeError):
        return props.ModeFixedTemporalRange


def _apply_fixed_range(layer, begin, end) -> None:
    """Stamp one raster layer: FixedTemporalRange [begin, end), active."""
    props = layer.temporalProperties()
    props.setMode(_fixed_temporal_mode(props))
    props.setFixedTemporalRange(
        QgsDateTimeRange(_temporal_qdt(begin), _temporal_qdt(end))
    )
    props.setIsActive(True)


def _widen_project_temporal_range(begin, end) -> None:
    """Grow the project temporal range to cover [begin, end) so the Temporal
    Controller picks the sequence up immediately (existing coverage is kept)."""
    begin_qdt, end_qdt = _temporal_qdt(begin), _temporal_qdt(end)
    settings = QgsProject.instance().timeSettings()
    try:
        current = settings.temporalRange()
        if (
            current is not None
            and not current.isInfinite()
            and current.begin().isValid()
            and current.end().isValid()
        ):
            if current.begin() < begin_qdt:
                begin_qdt = current.begin()
            if current.end() > end_qdt:
                end_qdt = current.end()
    except (AttributeError, TypeError):
        pass  # unreadable current range -- just set the group's span
    settings.setTemporalRange(QgsDateTimeRange(begin_qdt, end_qdt))


def stamp_temporal(raster_layers, stamped_counts: Optional[dict] = None) -> List[str]:
    """Stamp frame-sequence rasters for the native Temporal Controller.

    Detects frame groups among ``raster_layers`` (``temporal.group_frame_layers``
    -- the web scrubber's grouping), stamps each member with a per-frame
    FixedTemporalRange (ISO valid-times when the labels carry them, else a
    synthetic today-00:00-UTC + 1 h/step clock), activates the properties,
    and widens the project temporal range to span the group.

    ``stamped_counts`` (stem -> member count, per-case state) makes replays
    idempotent: a group is (re)stamped only when it gains members. Never
    raises -- failures become honest notes.
    """
    notes: List[str] = []
    by_name: dict = {}
    for layer in raster_layers:
        try:
            by_name.setdefault(layer.name(), layer)
        except (AttributeError, RuntimeError):  # deleted/half-built layer
            continue
    for group in temporal.group_frame_layers(list(by_name)):
        count = len(group.members)
        if stamped_counts is not None and stamped_counts.get(group.stem) == count:
            continue
        try:
            ranges = temporal.assign_frame_ranges(group)
            for name, begin, end in ranges:
                _apply_fixed_range(by_name[name], begin, end)
            _widen_project_temporal_range(ranges[0][1], ranges[-1][2])
        except Exception as exc:  # noqa: BLE001 -- honest note, never a crash
            notes.append(
                f"temporal stamp for sequence '{group.stem}' failed "
                f"({type(exc).__name__}: {exc})"
            )
            continue
        if stamped_counts is not None:
            stamped_counts[group.stem] = count
        notes.append(
            f"{count}-frame sequence '{group.stem}' stamped for the Temporal "
            "Controller (View > Panels > Temporal Controller, press play)"
        )
    return notes


# -- animation grouping (ITEM C: nested subgroup, not flat siblings) -------- #
#
# DESIGN NOTE (proven live 2026-07-10): a frame-sequence raster is placed
# into its animation subgroup AT INSERTION TIME (``_ensure_animation_subgroup``
# below, consulted by ``LayerMaterializer._add_raster`` BEFORE the layer's
# tree node is ever created) -- never moved there after the fact.
#
# An earlier version built every raster flat first, then RELOCATED the
# frame-sequence members into a subgroup afterward (via
# ``removeChildNode``+``insertLayer``, then ``takeChild``+``insertChildNode``
# when the first approach looked suspect). BOTH relocation strategies proved
# unsafe against a REAL Qt event loop: reproduced with a minimal script
# (construct group -> relocate members -> call ``QCoreApplication.
# processEvents()`` a few times) -- the relocated nodes' underlying C++
# objects got silently destroyed once the loop next drained, even though
# ``findLayerIds()`` read back correctly immediately after the move. A
# synchronous single-shot script never pumps the Qt loop, so it never
# caught this; the live dock does (every ``pump``/timer tick), which is
# exactly where it surfaced -- an animation subgroup that read "N frames
# grouped" then silently emptied moments later. Root cause is very likely a
# SIP ownership-transfer gap between the group's C++-side child list and
# the Python-side node wrapper for a RELOCATED node (a freshly-``insertLayer``
# -created node, which is never relocated, does not exhibit it -- verified
# by direct construction-into-a-nested-subgroup surviving the same
# processEvents() churn cleanly). Placing at creation time sidesteps the
# whole relocation code path.


def _ensure_animation_subgroup(parent_group, stem: str, count: int):
    """Find-or-create the animation subgroup for a frame-sequence ``stem``
    with ``count`` members, RENAMING an existing prefix-matched subgroup in
    place when the count grew (a plain property rename -- no node move, no
    reconstruction, safe). Never places a member itself; callers insert
    directly into the returned group at construction time."""
    prefix = f"{stem} (animation, "
    subgroup_name = f"{prefix}{count} frames)"
    for existing in parent_group.findGroups():
        if existing.name().startswith(prefix):
            if existing.name() != subgroup_name:
                existing.setName(subgroup_name)
            return existing
    subgroup = parent_group.insertGroup(0, subgroup_name)
    subgroup.setExpanded(False)
    return subgroup


def _frame_membership(names) -> dict:
    """``{layer_name: (stem, member_count)}`` for every name that is part of
    a detected frame-sequence group (``temporal.group_frame_layers`` -- the
    SAME grouping ``stamp_temporal`` stamps). Names not part of any
    (>= 2-member) sequence are simply absent -- callers treat that as "stays
    flat"."""
    membership: dict = {}
    for group in temporal.group_frame_layers(list(names)):
        count = len(group.members)
        for member in group.members:
            membership[member.name] = (group.stem, count)
    return membership


def _animation_group_notes(raster_layers, grouped_counts: dict) -> List[str]:
    """Dock notes for frame-sequence groups that changed size THIS call.
    Pure bookkeeping -- placement already happened at insertion time (see
    the module docstring above); this only decides what to SAY, using the
    same idempotent stem->count dedup pattern as ``stamp_temporal``."""
    notes: List[str] = []
    names = []
    for layer in raster_layers:
        try:
            names.append(layer.name())
        except (AttributeError, RuntimeError):  # deleted/half-built layer
            continue
    for group in temporal.group_frame_layers(names):
        count = len(group.members)
        if grouped_counts.get(group.stem) == count:
            continue
        grouped_counts[group.stem] = count
        notes.append(
            f"{group.stem}: {count} frames grouped - open View > Panels > "
            "Temporal Controller and press play to animate."
        )
    return notes


# -- mesh outputs (MDAL phase 1) --------------------------------------------- #


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


class LayerMaterializer:
    """Per-connection materializer: one group, one added-id set, one temp dir."""

    def __init__(self, settings: PluginSettings):
        self._settings = settings
        self._added_ids: set[str] = set()
        self._group_name: Optional[str] = None
        self._temp_dir: Optional[str] = None
        self._case_rasters: List = []  # QgsRasterLayer refs, this case
        self._stamped_counts: dict = {}  # frame-group stem -> stamped size
        self._grouped_counts: dict = {}  # frame-group stem -> animated size (ITEM C)
        #: Layers added by the MOST RECENT ``materialize``/``materialize_export``
        #: call (reset at the top of each) -- lets the dock zoom to "what just
        #: landed" without re-deriving it from notes strings.
        self.last_added_layers: List = []

    # -- lifecycle ------------------------------------------------------------- #

    def set_case(self, case_id: str, title: Optional[str] = None) -> None:
        """Bind to a case: clears every stale TRID3NT layer-tree group (ITEM
        A -- this materializer's own previous-case group AND any "Open case
        in QGIS" export groups, which otherwise accumulate across switches),
        names the fresh layer-tree group, and resets dedup/animation state
        so the case-open replay always repaints from a clean slate."""
        label = title or case_id[:8]
        self._group_name = f"TRID3NT {label}"
        self._clear_stale_groups()
        self._added_ids.clear()
        self._case_rasters = []
        self._stamped_counts = {}
        self._grouped_counts = {}
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
        if self._temp_dir is None or not os.path.isdir(self._temp_dir):
            self._temp_dir = tempfile.mkdtemp(prefix="trid3nt_qgis_")
        return self._temp_dir

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
        rasters_before = len(self._case_rasters)
        self.last_added_layers = []
        # ITEM C: decide frame-sequence membership UP FRONT (existing case
        # rasters + this batch's about-to-land raster events), so a brand
        # new frame member is placed straight into its animation subgroup
        # at construction time -- see the module docstring above the
        # animation-grouping helpers for why members are never relocated
        # after the fact.
        candidate_names = [self._safe_layer_name(l) for l in self._case_rasters]
        candidate_names.extend(
            event.name
            for event in events
            if event.layer_type == "raster" and event.layer_id not in self._added_ids
        )
        frame_membership = _frame_membership([n for n in candidate_names if n])
        for event in events:
            if event.layer_id in self._added_ids:
                continue
            try:
                note = self._materialize_one(event, frame_membership)
            except Exception as exc:  # noqa: BLE001
                note = f"layer '{event.name}': failed ({type(exc).__name__}: {exc})"
            if note is not None:
                # Mark handled even on skip/failure so the same row does not
                # re-note on every session-state replay of the snapshot.
                self._added_ids.add(event.layer_id)
                notes.append(note)
        # Frame-sequence rasters landed in their animation subgroups above
        # (placement is per-layer, at insertion time); this only stamps the
        # native Temporal Controller ranges and announces newly-(re)grouped
        # sequences. Only when this snapshot added a raster; the per-case
        # stamped/grouped counts dicts keep session-state replays (and the
        # case-open replay, which flows through this same method) idempotent.
        if len(self._case_rasters) != rasters_before:
            notes.extend(stamp_temporal(self._case_rasters, self._stamped_counts))
            notes.extend(_animation_group_notes(self._case_rasters, self._grouped_counts))
        return notes

    @staticmethod
    def _safe_layer_name(layer) -> str:
        try:
            return layer.name()
        except (AttributeError, RuntimeError):  # deleted/half-built layer
            return ""

    def _materialize_one(
        self, event: LayerEvent, frame_membership: Optional[dict] = None
    ) -> Optional[str]:
        if event.layer_type == "raster":
            return self._add_raster(event, frame_membership or {})
        if event.layer_type in ("vector", "geojson"):
            return self._add_vector(event)
        return f"layer '{event.name}': type '{event.layer_type}' not supported yet -- skipped"

    def _add_raster(self, event: LayerEvent, frame_membership: dict) -> str:
        template = event.tile_template
        if not template:
            return (
                f"raster '{event.name}': no XYZ tile template on the event -- skipped "
                "(WMS-only rasters land in a later milestone)"
            )
        layer = QgsRasterLayer(qgis_xyz_uri(template), event.name, "wms")
        if not layer.isValid():
            return f"raster '{event.name}': QGIS rejected the XYZ uri -- skipped"
        self._case_rasters.append(layer)
        # ITEM C: a recognized frame-sequence member goes straight into its
        # (found-or-created-or-renamed) animation subgroup; everything else
        # stays flat under the case group.
        destination = None
        membership = frame_membership.get(event.name)
        if membership is not None:
            stem, count = membership
            destination = _ensure_animation_subgroup(self._ensure_group(), stem, count)
        return self._add_to_group(
            layer, event, f"raster '{event.name}' added (XYZ tiles)", group=destination
        )

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
            return self._add_to_group(layer, event, f"vector '{event.name}' added (inline GeoJSON)")

        if event.uri.startswith("s3://"):
            if self._settings.mode != MODE_LOCAL:
                return (
                    f"vector '{event.name}': s3 uri in remote mode -- skipped "
                    "(no presigned fetch yet)"
                )
            http = s3_to_http(event.uri, self._settings.minio_endpoint)
            if not http:
                return f"vector '{event.name}': unparseable s3 uri -- skipped"
            layer = QgsVectorLayer(f"/vsicurl/{http}", event.name, "ogr")
            if not layer.isValid():
                return f"vector '{event.name}': MinIO fetch failed ({http}) -- skipped"
            return self._add_to_group(layer, event, f"vector '{event.name}' added (MinIO)")

        return f"vector '{event.name}': no inline GeoJSON and non-s3 uri -- skipped"

    # -- exported-case materialization (milestone 2, Open case in QGIS) -------- #

    def materialize_export(self, plan, group_label: str) -> List[str]:
        """Add an exported case's layers (``case_export.ExportPlan``) to the
        CURRENT project under their own group.

        Documented decision (see ``case_export``): layers are ADDED, the
        returned ``project.qgz`` is never opened via ``QgsProject.read()`` --
        that would REPLACE the user's open project (unsaved work + the live
        chat-session group would be lost). Never raises; every skip/failure
        is an honest note.
        """
        notes: List[str] = []
        self.last_added_layers = []
        root = QgsProject.instance().layerTreeRoot()
        group_name = f"TRID3NT export {group_label}"
        group = root.findGroup(group_name)
        if group is None:
            group = root.insertGroup(0, group_name)

        def _add(layer, label: str, destination=None) -> None:
            if not layer.isValid():
                notes.append(f"export layer '{label}': QGIS rejected it -- skipped")
                return
            QgsProject.instance().addMapLayer(layer, False)
            (destination or group).insertLayer(0, layer)
            self.last_added_layers.append(layer)
            notes.append(f"export layer '{label}' added")

        for name in plan.vector_layers:
            _add(
                QgsVectorLayer(f"{plan.gpkg_path}|layername={name}", name, "ogr"),
                name,
            )
        export_rasters: List = []
        raster_styles = getattr(plan, "raster_styles", None) or {}
        # ITEM C: decide frame-sequence membership UP FRONT from every raster
        # stem in this export (the whole batch lands in one call, so unlike
        # the live-stream path there is no "existing + new" split -- see the
        # animation-grouping module docstring for why placement happens at
        # construction time rather than a post-hoc relocation).
        stems = [os.path.splitext(os.path.basename(p))[0] for p in plan.raster_paths]
        frame_membership = _frame_membership(stems)
        for path in plan.raster_paths:
            stem = os.path.splitext(os.path.basename(path))[0]
            layer = QgsRasterLayer(path, stem, "gdal")
            if layer.isValid():
                export_rasters.append(layer)
            destination = None
            membership = frame_membership.get(stem)
            if membership is not None:
                anim_stem, count = membership
                destination = _ensure_animation_subgroup(group, anim_stem, count)
            _add(layer, stem, destination=destination)
            # Sidecar .qml (the export tool's TiTiler-derived pseudocolor
            # ramp): without it a GeoTIFF renders default grayscale --
            # near-black flood frames. loadNamedStyle is Qt5/Qt6-neutral;
            # a style failure is an honest note, never a lost layer.
            qml = raster_styles.get(path)
            if qml and layer.isValid():
                notes.append(self._apply_named_style(layer, stem, qml))
        # Frame-sequence rasters (Flood_depth_step_1..N GeoTIFFs) landed in
        # their animation subgroups above; stamp the native Temporal
        # Controller ranges and announce the grouping (fresh throwaway dedup
        # dict -- the whole export lands in one call, unlike the incremental
        # live stream, so every recognized sequence is "new" every time).
        notes.extend(stamp_temporal(export_rasters))
        notes.extend(_animation_group_notes(export_rasters, {}))

        # Mesh outputs (MDAL phase 1): SIBLINGS of the export's vector/raster
        # layers, never nested into an animation subgroup -- a mesh carries
        # its OWN internal dataset-group time series, it is not a member of
        # the flood-depth-COG frame sequence. Entries with no local_path
        # (remote mode, or a failed download) are skipped here WITHOUT a
        # duplicate note -- case_export.plan_export_layers already recorded
        # the honest reason in plan.notes (extended below).
        loaded_mesh_count = 0
        for entry in getattr(plan, "mesh_entries", None) or []:
            local_path = entry.get("local_path")
            name = entry.get("name") or "SFINCS mesh"
            if not local_path:
                continue
            layer = QgsMeshLayer(local_path, name, "mdal")
            if not layer.isValid():
                notes.append(f"mesh '{name}': QGIS/MDAL rejected the file -- skipped")
                continue
            crs_authid = entry.get("crs_authid")
            crs = QgsCoordinateReferenceSystem(crs_authid) if crs_authid else None
            if crs is not None and crs.isValid():
                layer.setCrs(crs)
            else:
                notes.append(f"{name}: mesh CRS unknown - set manually via layer properties")
            # Choose the DEFAULT active scalar group: SFINCS cumulative peak-depth
            # first (flood meshes), else a tracer/concentration group (TELEMAC dye
            # meshes -- MDAL would otherwise show VELOCITY U, not the plume).
            if not _select_peak_depth_dataset_group(layer):
                _select_tracer_dataset_group(layer)
            QgsProject.instance().addMapLayer(layer, False)
            group.insertLayer(0, layer)
            self.last_added_layers.append(layer)
            loaded_mesh_count += 1
            notes.append(
                f"{name}: native mesh loaded - dataset groups selectable in "
                "Layer Properties; install the Crayfish plugin for "
                "time-series plots and mesh export."
            )

        notes.extend(plan.notes)
        if not plan.vector_layers and not plan.raster_paths and loaded_mesh_count == 0:
            notes.append(
                "export produced no loadable layers -- nothing added "
                f"(status={plan.status or 'unknown'})"
            )
        return notes

    @staticmethod
    def _apply_named_style(layer, label: str, qml_path: str) -> str:
        """Apply a sidecar .qml to an added layer; returns an honest note.

        ``QgsMapLayer.loadNamedStyle`` returns ``(message, ok)`` on both the
        Qt5 and Qt6 PyQGIS bindings; handled defensively in case a binding
        flattens it. Never raises -- a bad style must not lose the layer.
        """
        try:
            result = layer.loadNamedStyle(qml_path)
            if isinstance(result, (tuple, list)) and len(result) >= 2:
                message, ok = str(result[0]), bool(result[1])
            else:
                message, ok = "", bool(result)
            if not ok:
                return (
                    f"style for '{label}' did not apply"
                    + (f" ({message})" if message else "")
                    + " -- layer kept with default rendering"
                )
            try:
                layer.triggerRepaint()
            except (AttributeError, RuntimeError):
                pass
            return f"style applied to '{label}' (web colormap)"
        except Exception as exc:  # noqa: BLE001 -- honest note, never a crash
            return (
                f"style for '{label}' failed ({type(exc).__name__}: {exc}) "
                "-- layer kept with default rendering"
            )

    def _add_to_group(self, layer, event: LayerEvent, note: str, group=None) -> str:
        """Add ``layer`` to the project + insert its tree node into
        ``group`` (default: this materializer's flat case group). ``group``
        lets ITEM C place a frame-sequence member straight into its
        animation subgroup at construction time -- see the module docstring
        above the animation-grouping helpers for why members are never
        relocated into a subgroup after the fact."""
        if event.opacity is not None:
            try:
                layer.setOpacity(max(0.0, min(1.0, float(event.opacity))))
            except (AttributeError, TypeError, ValueError):
                pass
        QgsProject.instance().addMapLayer(layer, False)
        target = group if group is not None else self._ensure_group()
        node = target.insertLayer(0, layer)
        if node is not None and not event.visible:
            node.setItemVisibilityChecked(False)
        self.last_added_layers.append(layer)
        return note

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

    def last_added_export_extent(self, dest_crs) -> Optional["QgsRectangle"]:
        """Combined extent of ALL layers added by the most recent
        ``materialize_export()`` call -- exported GeoTIFFs carry real GDAL
        extents (unlike the live XYZ tile layers), so rasters count here
        too."""
        return self.combined_extent(dest_crs, self.last_added_layers)
