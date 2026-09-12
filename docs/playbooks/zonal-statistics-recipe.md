# Session recipe: zonal statistics (raster values within zones)

Aggregating a value raster within zones is QGIS's own `native:zonalstatisticsfb`,
run in the user's session over the layers already on the map; no tool of this
product reimplements it.

## Vector-polygon zones (one row of statistics per feature)

Fetch the value raster and the zone polygons first (`fetch_dem`,
`fetch_administrative_boundaries`, a run's depth layer), then:

```
run_qgis_algorithm("native:zonalstatisticsfb", {
    "INPUT": "<zone polygons canvas name>",
    "INPUT_RASTER": "<value raster canvas name>",
    "RASTER_BAND": 1,
    "COLUMN_PREFIX": "depth_",
    "STATISTICS": [0, 1, 2, 5, 6],
})
```

The statistics codes are the algorithm's own (0 count, 1 sum, 2 mean, 3 median,
4 stddev, 5 min, 6 max, 7 range, 8 minority, 9 majority, 10 variety, 11 variance,
12 all). The output polygon layer carries one `depth_*` column per statistic and
is added to the project; the summary comes back to the model.

## Raster threshold zones ("pixels >= T are in-zone")

Two algorithms: `native:rastercalc` (or `gdal:rastercalculator`) builds the mask
`("value@1" >= T)`, then `native:rasterlayerstatistics` over the masked raster,
or `gdal:polygonize` on the mask followed by the vector recipe above.

## A number the model narrates

When the answer is one number rather than a layer, a `run_pyqgis` snippet reads
it off the zonal output's attribute table:

```python
from qgis.core import QgsProject
layer = QgsProject.instance().mapLayersByName("Zonal Statistics")[0]
result = {f["depth_mean"] for f in layer.getFeatures()}
```

Population and building exposure ("how many people or structures in the zone")
is `compute_exposure_summary` over the fetched population and footprint layers.
