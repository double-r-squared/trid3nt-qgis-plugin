"""Harness: a processing-request run in a headless QGIS session.

Run as a SUBPROCESS by its wrapper test under the interpreter that carries
``qgis.core`` and the Processing plugin. A tiny DEM is written and added to the
project; ``native:slope`` runs over it BY CANVAS NAME and the output is written
to a file, summarized and never added to the project; a snippet reads the project back and a raising
snippet answers with its traceback. A case layer named by its store uri opens
through the dock's ``/vsis3`` path against a stub store, and reproject -> IDW
chains through it; a layer the map paints capped is read from its full source;
a uri the store does not hold is refused by name."""

from __future__ import annotations

import http.server
import os
import sys
import tempfile
import threading

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


class _StubStore(http.server.BaseHTTPRequestHandler):
    """A path-style object store over a local directory: HEAD and ranged GET."""

    root = ""

    def _object(self):
        path = os.path.join(self.root, self.path.split("?", 1)[0].lstrip("/"))
        return path if os.path.isfile(path) else None

    def do_HEAD(self):  # noqa: N802 -- BaseHTTPRequestHandler's name
        path = self._object()
        self.send_response(200 if path else 404)
        self.send_header("Content-Length", str(os.path.getsize(path) if path else 0))
        self.end_headers()

    def do_GET(self):  # noqa: N802 -- BaseHTTPRequestHandler's name
        path = self._object()
        if path is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with open(path, "rb") as fh:
            body = fh.read()
        ranged = self.headers.get("Range", "")
        if ranged.startswith("bytes="):
            first, _, last = ranged[len("bytes="):].partition("-")
            start, end = int(first), min(int(last or len(body) - 1), len(body) - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
            body = body[start:end + 1]
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _store_chain(tmp: str) -> None:
    """reproject -> IDW where the IDW names the reprojected case layer by its
    store uri, as the agent hands it on the wire."""
    import json
    import shutil

    from plugin.render.layers import configure_store_access
    from plugin.render.processing import run_processing_request

    root = os.path.join(tmp, "store")
    os.makedirs(os.path.join(root, "runs", "qgis-chain"))
    _StubStore.root = root
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubStore)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    assert configure_store_access(
        f"http://127.0.0.1:{server.server_address[1]}", "k", "s", "us-east-1") is None
    points = os.path.join(tmp, "pts.geojson")
    with open(points, "w") as fh:
        json.dump({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"z": z},
             "geometry": {"type": "Point", "coordinates": xy}}
            for z, xy in ((1.0, [-82.42, 42.97]), (3.0, [-82.41, 42.98]),
                          (2.0, [-82.40, 42.96]))]}, fh)
    reprojected = run_processing_request({
        "request_id": "01HARNESSPROCESSINGSTOREAA", "kind": "algorithm",
        "algorithm": "native:reprojectlayer",
        "params": {"INPUT": points, "TARGET_CRS": "EPSG:32617"},
    })
    assert reprojected["status"] == "ok", reprojected
    written = reprojected["result"]["source"].split("|", 1)[0]
    shutil.copy(written, os.path.join(root, "runs", "qgis-chain", "OUTPUT.gpkg"))
    uri = "s3://runs/qgis-chain/OUTPUT.gpkg"
    grid = run_processing_request({
        "request_id": "01HARNESSPROCESSINGSTOREBB", "kind": "algorithm",
        "algorithm": "gdal:gridinversedistance", "params": {"INPUT": uri, "Z_FIELD": "z"},
    })
    assert grid["status"] == "ok", grid
    assert grid["result"]["kind"] == "raster" and grid["result"]["crs"] == "EPSG:32617", grid
    print(f"[processing] reproject -> IDW over the case layer {uri} read through /vsis3")

    _capped_copy(root)

    missing = "s3://runs/qgis-gone/OUTPUT.gpkg"
    refused = run_processing_request({
        "request_id": "01HARNESSPROCESSINGSTORECC", "kind": "algorithm",
        "algorithm": "gdal:gridinversedistance", "params": {"INPUT": missing, "Z_FIELD": "z"},
    })
    assert refused["status"] == "error" and f"case layer {missing!r}" in refused["error"], refused
    print("[processing] a case layer the store does not hold is refused by name")
    server.shutdown()
    server.server_close()


def _capped_copy(root: str) -> None:
    """A survey the map paints capped at 3 of its 50 soundings: an algorithm
    naming it by canvas name, layer id or store uri reads all 50."""
    from osgeo import ogr, osr
    from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer

    from plugin.render.processing import run_processing_request

    os.makedirs(os.path.join(root, "data", "survey"))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(
        os.path.join(root, "data", "survey", "soundings.gpkg"))
    table = ds.CreateLayer("soundings", srs, ogr.wkbPoint)
    table.CreateField(ogr.FieldDefn("z", ogr.OFTReal))
    for i in range(50):
        feature = ogr.Feature(table.GetLayerDefn())
        feature.SetField("z", float(i))
        feature.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT (-82.4{i:02d} 42.97)"))
        table.CreateFeature(feature)
    ds = None
    uri = "s3://data/survey/soundings.gpkg"
    painted = QgsVectorLayer("Point?crs=EPSG:4326&field=z:double", "Survey", "memory")
    for i in range(3):
        feature = QgsFeature(painted.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(-82.4 - i / 100, 42.97)))
        feature.setAttributes([float(i)])
        painted.dataProvider().addFeature(feature)
    painted.setCustomProperty("trid3nt/source_uri", uri)
    painted.setCustomProperty("trid3nt/layer_id", "ehydro-survey-1")
    QgsProject.instance().addMapLayer(painted)
    for named in ("Survey", "ehydro-survey-1", painted.id(), uri):
        out = run_processing_request({
            "request_id": "01HARNESSPROCESSINGFULLAA", "kind": "algorithm",
            "algorithm": "native:reprojectlayer",
            "params": {"INPUT": named, "TARGET_CRS": "EPSG:32617"},
        })
        assert out["status"] == "ok" and out["result"]["feature_count"] == 50, (named, out)
    print("[processing] a capped painted layer reaches QGIS as its full source: 50 of 50")
    merged = run_processing_request({
        "request_id": "01HARNESSPROCESSINGFULLCC", "kind": "algorithm",
        "algorithm": "native:mergevectorlayers",
        "params": {"LAYERS": ["Survey", "ehydro-survey-1"], "CRS": "EPSG:4326"},
    })
    assert merged["status"] == "ok" and merged["result"]["feature_count"] == 100, merged
    print("[processing] a painted layer named in a list reaches QGIS as its full source")

    painted.removeCustomProperty("trid3nt/source_uri")
    refused = run_processing_request({
        "request_id": "01HARNESSPROCESSINGFULLBB", "kind": "algorithm",
        "algorithm": "native:reprojectlayer",
        "params": {"INPUT": "Survey", "TARGET_CRS": "EPSG:32617"},
    })
    assert refused["status"] == "error" and "case layer 'Survey' has no full source" in (
        refused["error"]), refused
    print("[processing] a painted layer with no full source is refused by name")
    QgsProject.instance().removeMapLayer(painted.id())


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
    assert os.path.isfile(summary["source"]), summary
    assert len(QgsProject.instance().mapLayers()) == 1, "the agent's case layer paints it"
    slope = QgsRasterLayer(summary["source"], "slope")
    stats = slope.dataProvider().bandStatistics(1)
    # A layer outside the project still alive at exitQgis segfaults the exit.
    del slope
    # A 2 m drop per 30 m row is a 3.81 degree slope everywhere inside.
    assert abs(stats.mean - 3.81) < 0.2, stats.mean
    print(f"[processing] native:slope over the canvas DEM -> {summary['layer_name']} "
          f"({summary['width']}x{summary['height']}, mean slope {stats.mean:.2f} deg)")

    points = run_processing_request({
        "request_id": "01HARNESSPROCESSINGALGDDDD",
        "kind": "algorithm", "algorithm": "native:pixelstopoints",
        "params": {"INPUT_RASTER": "DEM", "RASTER_BAND": 1, "FIELD_NAME": "VALUE"},
    })
    assert points["status"] == "ok", points
    written = points["result"]["source"].split("|", 1)[0]
    assert os.path.isfile(written), points["result"]
    assert len(QgsProject.instance().mapLayers()) == 1, "the agent's case layer paints it"
    print(f"[processing] a vector output is the file {os.path.basename(written)}")

    by_id = run_processing_request({
        "request_id": "01HARNESSPROCESSINGALGCCCC",
        "kind": "algorithm", "algorithm": "native:slope",
        "params": {"INPUT": dem.id(), "Z_FACTOR": 1.0},
    })
    assert by_id["status"] == "ok", by_id
    print("[processing] a layer named by its QGIS id is found")

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
    _store_chain(tmp)
    app.exitQgis()
    return 0


if __name__ == "__main__":
    sys.exit(main())
