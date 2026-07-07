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
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import List, Optional

from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer

from .plugin_settings import MODE_LOCAL, PluginSettings
from .trid3nt_client import LayerEvent, qgis_xyz_uri, s3_to_http

_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_filename(name: str) -> str:
    return _SAFE_NAME.sub("_", name).strip("_") or "layer"


class LayerMaterializer:
    """Per-connection materializer: one group, one added-id set, one temp dir."""

    def __init__(self, settings: PluginSettings):
        self._settings = settings
        self._added_ids: set[str] = set()
        self._group_name: Optional[str] = None
        self._temp_dir: Optional[str] = None

    # -- lifecycle ------------------------------------------------------------- #

    def set_case(self, case_id: str, title: Optional[str] = None) -> None:
        """Bind to a case: names the layer-tree group + resets dedup."""
        label = title or case_id[:8]
        self._group_name = f"TRID3NT {label}"
        self._added_ids.clear()

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
