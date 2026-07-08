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
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import List, Optional

from qgis.core import QgsDateTimeRange, QgsProject, QgsRasterLayer, QgsVectorLayer
from qgis.PyQt.QtCore import QDateTime, Qt

from . import temporal
from .plugin_settings import MODE_LOCAL, PluginSettings
from .trid3nt_client import LayerEvent, qgis_xyz_uri, s3_to_http

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_filename(name: str) -> str:
    return _SAFE_NAME.sub("_", name).strip("_") or "layer"


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


class LayerMaterializer:
    """Per-connection materializer: one group, one added-id set, one temp dir."""

    def __init__(self, settings: PluginSettings):
        self._settings = settings
        self._added_ids: set[str] = set()
        self._group_name: Optional[str] = None
        self._temp_dir: Optional[str] = None
        self._case_rasters: List = []  # QgsRasterLayer refs, this case
        self._stamped_counts: dict = {}  # frame-group stem -> stamped size

    # -- lifecycle ------------------------------------------------------------- #

    def set_case(self, case_id: str, title: Optional[str] = None) -> None:
        """Bind to a case: names the layer-tree group + resets dedup."""
        label = title or case_id[:8]
        self._group_name = f"TRID3NT {label}"
        self._added_ids.clear()
        self._case_rasters = []
        self._stamped_counts = {}

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
        for event in events:
            if event.layer_id in self._added_ids:
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
        # Frame-sequence rasters -> native Temporal Controller animation.
        # Only when this snapshot added a raster; the per-case stamped-counts
        # dict keeps session-state replays idempotent.
        if len(self._case_rasters) != rasters_before:
            notes.extend(stamp_temporal(self._case_rasters, self._stamped_counts))
        return notes

    def _materialize_one(self, event: LayerEvent) -> Optional[str]:
        if event.layer_type == "raster":
            return self._add_raster(event)
        if event.layer_type in ("vector", "geojson"):
            return self._add_vector(event)
        return f"layer '{event.name}': type '{event.layer_type}' not supported yet -- skipped"

    def _add_raster(self, event: LayerEvent) -> str:
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
        return self._add_to_group(layer, event, f"raster '{event.name}' added (XYZ tiles)")

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
        root = QgsProject.instance().layerTreeRoot()
        group_name = f"TRID3NT export {group_label}"
        group = root.findGroup(group_name)
        if group is None:
            group = root.insertGroup(0, group_name)

        def _add(layer, label: str) -> None:
            if not layer.isValid():
                notes.append(f"export layer '{label}': QGIS rejected it -- skipped")
                return
            QgsProject.instance().addMapLayer(layer, False)
            group.insertLayer(0, layer)
            notes.append(f"export layer '{label}' added")

        for name in plan.vector_layers:
            _add(
                QgsVectorLayer(f"{plan.gpkg_path}|layername={name}", name, "ogr"),
                name,
            )
        export_rasters: List = []
        raster_styles = getattr(plan, "raster_styles", None) or {}
        for path in plan.raster_paths:
            stem = os.path.splitext(os.path.basename(path))[0]
            layer = QgsRasterLayer(path, stem, "gdal")
            if layer.isValid():
                export_rasters.append(layer)
            _add(layer, stem)
            # Sidecar .qml (the export tool's TiTiler-derived pseudocolor
            # ramp): without it a GeoTIFF renders default grayscale --
            # near-black flood frames. loadNamedStyle is Qt5/Qt6-neutral;
            # a style failure is an honest note, never a lost layer.
            qml = raster_styles.get(path)
            if qml and layer.isValid():
                notes.append(self._apply_named_style(layer, stem, qml))
        # Frame-sequence rasters (Flood_depth_step_1..N GeoTIFFs) -> native
        # Temporal Controller animation, same as the live-stream path.
        notes.extend(stamp_temporal(export_rasters))
        notes.extend(plan.notes)
        if not plan.vector_layers and not plan.raster_paths:
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

    def _add_to_group(self, layer, event: LayerEvent, note: str) -> str:
        if event.opacity is not None:
            try:
                layer.setOpacity(max(0.0, min(1.0, float(event.opacity))))
            except (AttributeError, TypeError, ValueError):
                pass
        QgsProject.instance().addMapLayer(layer, False)
        group = self._ensure_group()
        node = group.insertLayer(0, layer)
        if node is not None and not event.visible:
            node.setItemVisibilityChecked(False)
        return note
