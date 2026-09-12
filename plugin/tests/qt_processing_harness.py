"""Harness: a processing-request run in a headless QGIS session.

Run as a SUBPROCESS by its wrapper test under the interpreter that carries
``qgis.core`` and the Processing plugin. A tiny DEM is written and added to the
project; ``native:slope`` runs over it BY CANVAS NAME and the output lands in
the project with its summary; a snippet reads the project back and a raising
snippet answers with its traceback."""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.core import QgsApplication, QgsProject, QgsRasterLayer  # noqa: E402


def _write_dem(path: str) -> None:
    import numpy as np
    from osgeo import gdal, osr

    n = 40
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, n, n, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((500000.0, 30.0, 0.0, 4001200.0, 0.0, -30.0))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32611)
    ds.SetProjection(srs.ExportToWkt())
    rows = np.arange(n, dtype="float32")[:, None]
    ds.GetRasterBand(1).WriteArray(np.repeat(1000.0 - rows * 2.0, n, axis=1))
    ds.FlushCache()
    ds = None


def main() -> int:
    QgsApplication.setPrefixPath("/usr", True)
    app = QgsApplication([], False)
    app.initQgis()
    sys.path.append(os.path.join(app.prefixPath(), "share", "qgis", "python", "plugins"))
    from processing.core.Processing import Processing

    Processing.initialize()
    from plugin.render.processing import run_processing_request

    tmp = tempfile.mkdtemp(prefix="trid3nt_proc_")
    dem_path = os.path.join(tmp, "dem.tif")
    _write_dem(dem_path)
    dem = QgsRasterLayer(dem_path, "DEM")
    assert dem.isValid(), "the harness DEM did not open"
    QgsProject.instance().addMapLayer(dem)

    algo = run_processing_request({
        "request_id": "01HARNESSPROCESSINGALGAAAA",
        "kind": "algorithm",
        "algorithm": "native:slope",
        "params": {"INPUT": "DEM", "Z_FACTOR": 1.0},
    })
    assert algo["status"] == "ok", algo
    summary = algo["result"]
    assert summary["kind"] == "raster" and summary["band_count"] == 1, summary
    assert summary["crs"] == "EPSG:32611", summary
    names = [l.name() for l in QgsProject.instance().mapLayers().values()]
    assert summary["layer_name"] in names, (summary, names)
    slope = QgsProject.instance().mapLayer(summary["layer_id"])
    stats = slope.dataProvider().bandStatistics(1)
    # A 2 m drop per 30 m row is a 3.81 degree slope everywhere inside.
    assert abs(stats.mean - 3.81) < 0.2, stats.mean
    print(f"[processing] native:slope over the canvas DEM -> {summary['layer_name']} "
          f"({summary['width']}x{summary['height']}, mean slope {stats.mean:.2f} deg)")

    unknown = run_processing_request({
        "request_id": "01HARNESSPROCESSINGALGBBBB",
        "kind": "algorithm", "algorithm": "native:no_such_thing", "params": {},
    })
    assert unknown["status"] == "error" and "no Processing algorithm" in unknown["error"], unknown

    code = run_processing_request({
        "request_id": "01HARNESSPROCESSINGCODEAAA",
        "kind": "code",
        "code": (
            "layer = QgsProject.instance().mapLayersByName('DEM')[0]\n"
            "print('hello from the session')\n"
            "result = {'width': layer.width(), 'crs': layer.crs().authid()}\n"
        ),
    })
    assert code["status"] == "ok", code
    assert code["result"] == {"value": {"width": 40, "crs": "EPSG:32611"}}, code
    assert "hello from the session" in code["stdout"], code
    print("[processing] run_pyqgis snippet read the project back and printed to stdout")

    failed = run_processing_request({
        "request_id": "01HARNESSPROCESSINGCODEBBB",
        "kind": "code", "code": "result = undefined_name\n",
    })
    assert failed["status"] == "error" and "NameError" in failed["error"], failed
    print("[processing] a raising snippet answers with its traceback")
    app.exitQgis()
    return 0


if __name__ == "__main__":
    sys.exit(main())
