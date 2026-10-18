"""Real-QGIS harness for the mesh probe, run as a SUBPROCESS by its wrapper.

argv[1] is a two-face CF-UGRID file whose "head" group is 10 then 11 on the
west face and 20 then 21 on the east, in metres, an hour apart. Prints the
entries as one JSON line after MESH-PROBE."""

from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsMeshLayer,
    QgsPointXY,
    QgsProject,
)

app = QgsApplication([], False)
app.initQgis()

from plugin.render.mesh_probe import mesh_entries  # noqa: E402

shown = QgsMeshLayer(sys.argv[1], "Head", "mdal")
hidden = QgsMeshLayer(sys.argv[1], "Hidden head", "mdal")
assert shown.isValid() and hidden.isValid()
for layer in (shown, hidden):
    settings = layer.rendererSettings()
    settings.setActiveScalarDatasetGroup(0)
    layer.setRendererSettings(settings)
project = QgsProject.instance()
project.addMapLayers([shown, hidden])
project.layerTreeRoot().findLayer(hidden.id()).setItemVisibilityChecked(False)

crs = QgsCoordinateReferenceSystem("EPSG:32617")
out = {name: mesh_entries(QgsPointXY(*xy), crs) for name, xy in (
    ("west", (500050.0, 4000050.0)), ("east", (500150.0, 4000050.0)),
    ("outside", (600000.0, 4000050.0)))}
print("MESH-PROBE " + json.dumps(out))
