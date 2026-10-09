"""Harness: a layer-request answered by a headless QGIS session.

Run as a SUBPROCESS by its wrapper test under the interpreter that carries
``qgis.core`` and the Processing plugin. OFFLINE: the provider is ``ogr`` over a
GeoPackage written here, and the ingest route is a local stub, so the seam is
proved without a network. Mode open puts the layer on the map; mode materialise
windows it to the asked bbox, exports it and uploads it; an unopenable uri
answers with the provider's own text. A raster row proves the two asks the
request carries: the spacing it was asked for lands on the exported grid, and a
request with no bbox exports the whole published layer.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.core import QgsApplication, QgsProject  # noqa: E402

_KEY = "01harnesslayerrequestkey"
_BBOX = [-114.0, 31.3, -112.0, 33.0]

#: The synthetic raster: EPSG:4326, a 0.002 deg native grid over one degree
#: square, so an asked 300 m is plainly coarser than native and the whole-layer
#: export has an extent to compare against.
_RASTER_ORIGIN = (-114.0, 34.0)
_RASTER_PIXELS = 500
_RASTER_STEP = 0.002
_RASTER_BBOX = [-114.0, 33.0, -113.0, 34.0]


def _metres_per_degree(lat: float) -> tuple:
    """(east-west, north-south) metres in a degree at ``lat``."""
    import math

    phi = math.radians(lat)
    return (
        111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi)
        + 0.118 * math.cos(5 * phi),
        111132.92 - 559.82 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
        - 0.0023 * math.cos(6 * phi),
    )


class _IngestStub(http.server.BaseHTTPRequestHandler):
    uploads: list = []

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        _IngestStub.uploads.append((self.path, body))
        payload = json.dumps({"s3_uri": "s3://cache/user-uploads/01ULID/x.gpkg"})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload.encode("utf-8"))

    def log_message(self, *args) -> None:
        return


def _write_source(path: str) -> None:
    from osgeo import ogr, osr

    driver = ogr.GetDriverByName("GPKG")
    source = driver.CreateDataSource(path)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    layer = source.CreateLayer("cells", srs, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("dm", ogr.OFTInteger))
    # One polygon inside the asked window and one well outside it, so the
    # materialised export proves the window and not just the export.
    for dm, (x0, y0) in ((4, (-113.0, 32.0)), (1, (-100.0, 40.0))):
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("dm", dm)
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in ((x0, y0), (x0 + 0.5, y0), (x0 + 0.5, y0 + 0.5), (x0, y0 + 0.5),
                     (x0, y0)):
            ring.AddPoint_2D(x, y)
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        feature.SetGeometry(poly)
        layer.CreateFeature(feature)
    source = None


def _write_raster(path: str) -> None:
    from osgeo import gdal, osr

    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(path, _RASTER_PIXELS, _RASTER_PIXELS, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform(
        (_RASTER_ORIGIN[0], _RASTER_STEP, 0.0, _RASTER_ORIGIN[1], 0.0, -_RASTER_STEP)
    )
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    dataset.SetProjection(srs.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.Fill(2.5)
    band.FlushCache()
    dataset = None


def _raster_cases(tmp: str, base_url: str) -> None:
    """The two asks a raster row carries, through the running plugin seam."""
    from osgeo import gdal

    from plugin.render.layer_request import run_layer_request

    tif = os.path.join(tmp, "grid.tif")
    _write_raster(tif)

    asked_m = 300.0
    answer = run_layer_request({
        "key": _KEY, "provider": "gdal", "uri": tif, "name": "grid",
        "bbox": _RASTER_BBOX, "mode": "materialise", "resolution_m": asked_m,
    }, base_url=base_url)
    assert answer["error"] is None, answer
    path, body = _IngestStub.uploads[-1]
    assert f"filename={_KEY}.tif" in path, path
    out = os.path.join(tmp, "at_300m.tif")
    with open(out, "wb") as handle:
        handle.write(body)
    exported = gdal.Open(out)
    transform = exported.GetGeoTransform()
    exported = None
    x_step, y_step = abs(transform[1]), abs(transform[5])
    centre_lat = (_RASTER_BBOX[1] + _RASTER_BBOX[3]) / 2.0
    per_lon, per_lat = _metres_per_degree(centre_lat)
    assert abs(x_step * per_lon - asked_m) < 0.5, (x_step * per_lon, asked_m)
    assert abs(y_step * per_lat - asked_m) < 0.5, (y_step * per_lat, asked_m)
    assert abs(x_step - _RASTER_STEP) > 1e-6, "the export kept the native grid"
    print(f"[layer-request] a raster materialised at 300 m has a "
          f"{x_step * per_lon:.1f} m by {y_step * per_lat:.1f} m cell, not the "
          f"native {_RASTER_STEP * per_lon:.1f} m")

    answer = run_layer_request({
        "key": _KEY, "provider": "gdal", "uri": tif, "name": "grid",
        "bbox": None, "mode": "materialise",
    }, base_url=base_url)
    assert answer["error"] is None, answer
    _path, body = _IngestStub.uploads[-1]
    out = os.path.join(tmp, "whole.tif")
    with open(out, "wb") as handle:
        handle.write(body)
    exported = gdal.Open(out)
    size = (exported.RasterXSize, exported.RasterYSize)
    exported = None
    assert size == (_RASTER_PIXELS, _RASTER_PIXELS), size
    print(f"[layer-request] no bbox exported the whole layer: {size[0]}x{size[1]} px")


def main() -> int:
    QgsApplication.setPrefixPath("/usr", True)
    app = QgsApplication([], False)
    app.initQgis()
    sys.path.append(os.path.join(app.prefixPath(), "share", "qgis", "python", "plugins"))
    from processing.core.Processing import Processing

    Processing.initialize()
    from plugin.render.layer_request import run_layer_request

    tmp = tempfile.mkdtemp(prefix="trid3nt_layer_")
    gpkg = os.path.join(tmp, "cells.gpkg")
    _write_source(gpkg)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _IngestStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"

    opened = run_layer_request({
        "key": _KEY, "provider": "ogr", "uri": gpkg, "name": "cells",
        "bbox": _BBOX, "mode": "open",
    })
    assert opened == {"key": _KEY, "uri": None, "error": None}, opened
    names = [l.name() for l in QgsProject.instance().mapLayers().values()]
    assert "cells" in names, names
    print(f"[layer-request] mode open put 'cells' on the map through the ogr "
          f"provider ({len(names)} layer(s))")

    materialised = run_layer_request({
        "key": _KEY, "provider": "ogr", "uri": gpkg, "name": "cells",
        "bbox": _BBOX, "mode": "materialise",
    }, base_url=base_url)
    assert materialised["error"] is None, materialised
    assert materialised["uri"] == "s3://cache/user-uploads/01ULID/x.gpkg", materialised
    path, body = _IngestStub.uploads[-1]
    assert f"filename={_KEY}.gpkg" in path, path
    assert body[:4] == b"SQLi", body[:16]
    exported = os.path.join(tmp, "exported.gpkg")
    with open(exported, "wb") as handle:
        handle.write(body)
    from osgeo import ogr

    read_back = ogr.Open(exported)
    values = [f.GetField("dm") for f in read_back.GetLayer(0)]
    read_back = None
    assert values == [4], values
    print(f"[layer-request] mode materialise uploaded {len(body)} bytes as "
          f"{_KEY}.gpkg, windowed to the asked bbox ({values})")

    failed = run_layer_request({
        "key": _KEY, "provider": "ogr", "uri": os.path.join(tmp, "no_such.gpkg"),
        "name": "missing", "bbox": _BBOX, "mode": "open",
    })
    assert failed["uri"] is None and "did not open missing" in failed["error"], failed
    print("[layer-request] an unopenable uri answers with the provider's own text")

    unknown = run_layer_request({
        "key": _KEY, "provider": "ogr", "uri": gpkg, "name": "cells",
        "bbox": _BBOX, "mode": "download",
    })
    assert unknown["error"] == "unknown layer mode 'download'", unknown
    print("[layer-request] an unknown mode is refused, never guessed")

    _raster_cases(tmp, base_url)

    server.shutdown()
    server.server_close()
    # The project outlives the application object otherwise, and a layer still
    # holding its provider through teardown aborts the process.
    QgsProject.instance().removeAllMapLayers()
    app.exitQgis()
    return 0


if __name__ == "__main__":
    sys.exit(main())
