"""Open a UGRID file through QGIS's own MDAL and print what it reads, as JSON.

Run by the system interpreter that carries PyQGIS; one line out: every dataset
group's name, whether it lies on faces, and its step times in hours."""

import json
import sys

from qgis.core import QgsApplication, QgsMeshDatasetIndex, QgsMeshLayer

app = QgsApplication([], False)
app.initQgis()
layer = QgsMeshLayer(sys.argv[1], "mesh", "mdal")
provider = layer.dataProvider()
groups = {}
for g in range(provider.datasetGroupCount()):
    meta = provider.datasetGroupMetadata(g)
    groups[meta.name()] = {
        "faces": meta.dataType() == meta.DataOnFaces,
        "hours": [provider.datasetMetadata(QgsMeshDatasetIndex(g, i)).time()
                  for i in range(provider.datasetCount(g))],
        "reference": meta.referenceTime().toString("yyyy-MM-dd HH:mm:ss")}
print(json.dumps({"valid": layer.isValid(), "faces": provider.faceCount(),
                  "groups": groups}))
app.exitQgis()
