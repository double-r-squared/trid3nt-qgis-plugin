"""Harness: a layer-request answered by a headless QGIS session.

Run as a SUBPROCESS by its wrapper test under the interpreter that carries
``qgis.core`` and the Processing plugin. OFFLINE: the provider is ``ogr`` over a
GeoPackage written here, and the ingest route is a local stub, so the seam is
proved without a network. Mode open puts the layer on the map; mode materialise
windows it to the asked bbox, exports it and uploads it; an unopenable uri
answers with the provider's own text.
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

    server.shutdown()
    server.server_close()
    # The project outlives the application object otherwise, and a layer still
    # holding its provider through teardown aborts the process.
    QgsProject.instance().removeAllMapLayers()
    app.exitQgis()
    return 0


if __name__ == "__main__":
    sys.exit(main())
