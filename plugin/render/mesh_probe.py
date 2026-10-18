"""Map-click mesh probe: QGIS reads its own loaded mesh layers at the clicked point.

Each visible mesh layer's PAINTED scalar group is read at every saved time,
interpolated as the map draws it; a point inside no face is that layer's
"outside the mesh". Entries take the probe reply's shape, so the note block and
the chart payload read them as they read the server's."""

from __future__ import annotations

import math
from typing import Any, Dict, List

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMesh,
    QgsMeshDatasetIndex,
    QgsMeshLayer,
    QgsPointXY,
    QgsProject,
)
from qgis.PyQt.QtCore import Qt

__all__ = ["mesh_entries"]


def mesh_entries(point: QgsPointXY, crs: QgsCoordinateReferenceSystem
                 ) -> List[Dict[str, Any]]:
    """Every visible mesh layer read at ``point`` (in ``crs``, the canvas's), one
    entry per layer that paints a scalar group."""
    project = QgsProject.instance()
    root = project.layerTreeRoot()
    entries = []
    for layer in project.mapLayers().values():
        if not isinstance(layer, QgsMeshLayer):
            continue
        node = root.findLayer(layer.id())
        group = layer.rendererSettings().activeScalarDatasetGroup()
        if node is None or not node.isVisible() or group < 0:
            continue
        entries.append(_read(layer, group, point, crs, project))
    return entries


def _read(layer: QgsMeshLayer, group: int, point: QgsPointXY,
          crs: QgsCoordinateReferenceSystem, project: QgsProject) -> Dict[str, Any]:
    # The value and face lookups run on the triangulated mesh in the canvas CRS,
    # the one the renderer draws; a layer never drawn has none until updated.
    layer.updateTriangularMesh(QgsCoordinateTransform(layer.crs(), crs, project))
    meta = layer.datasetGroupMetadata(QgsMeshDatasetIndex(group))
    entry: Dict[str, Any] = {"layer_id": layer.id(), "name": layer.name(),
                             "units": meta.extraOptions().get("units") or None}
    if layer.snapOnElement(QgsMesh.Face, point, 0).isEmpty():
        return {**entry, "value": None, "note": "outside the mesh"}
    reference = layer.temporalProperties().referenceTime()
    series = []
    for i in range(layer.datasetCount(QgsMeshDatasetIndex(group))):
        index = QgsMeshDatasetIndex(group, i)
        value = layer.datasetValue(index, point, 0).scalar()
        ms = layer.datasetRelativeTimeInMilliseconds(index)
        label = (reference.addMSecs(ms).toUTC().toString(Qt.DateFormat.ISODate)
                 if reference.isValid() else f"t+{ms / 1000:g} s")
        series.append({"label": label,
                       "value": None if math.isnan(value) else float(value)})
    if len(series) == 1:
        value = series[0]["value"]
        return {**entry, "value": value,
                **({} if value is not None else {"note": "no value here"})}
    return {**entry, "series": series}
