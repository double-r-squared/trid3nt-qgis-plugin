"""Real-QGIS harness for the layer clocks -- run as a SUBPROCESS by
``test_mesh_temporal`` (needs qgis.core; the pure test venv does not have it,
so the parent test probes the system interpreter and skips honestly when
absent).

What it proves, on the INSTALLED QGIS rather than on a description of it:

  * MDAL opens a real SELAFIN as a mesh layer whose temporal properties are
    already active, and whose reference time is 1900 - the file records no
    origin for the seconds it counts, which is why a run has to state one;
  * ``stamp_mesh_temporal`` moves the whole time extent onto the instant the
    row DECLARED, so the controller scrubs the run's own clock;
  * a row that declares no reference time is left on MDAL's own axis rather
    than given an invented one;
  * ``load_declared_style`` loads a preset ``.qml`` onto a real raster and the
    renderer CHANGES (loadNamedStyle's boolean is well-formedness only, so the
    post-load state is what the gate reads);
  * a raster carrying its own colour table and no ``.qml`` keeps QGIS's own
    paletted renderer - the render this side no longer rebuilds.

Argument: the path to a SELAFIN to open. Prints QT-MESH-TEMPORAL-OK and exits 0
on success; prints the failing assertion and exits 1 otherwise.
"""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsMeshLayer,
    QgsRasterLayer,
)
from qgis.PyQt.QtCore import Qt  # noqa: E402

_REFERENCE = "2026-09-01T12:00:00Z"

#: The smallest document the continuous preset writes, as its writer writes it.
_QML = (
    '<!DOCTYPE qgis>\n<qgis version="3.40">\n  <pipe>\n'
    '    <rasterrenderer type="singlebandpseudocolor" band="1" opacity="1"'
    ' alphaBand="-1" classificationMin="0" classificationMax="10"'
    ' nodataColor="">\n'
    "      <rastershader>\n"
    '        <colorrampshader colorRampType="INTERPOLATED" classificationMode="1"'
    ' clip="0" minimumValue="0" maximumValue="10" labelPrecision="4">\n'
    '          <item value="0" color="#440154" alpha="255" label="0"/>\n'
    '          <item value="10" color="#fde725" alpha="255" label="10"/>\n'
    "        </colorrampshader>\n"
    "      </rastershader>\n"
    "    </rasterrenderer>\n"
    "  </pipe>\n</qgis>\n"
)


def _event(layers, **fields):
    row = dict(fields)
    return layers.LayerEvent(
        layer_id=row.get("layer_id", "L1"),
        name=row.get("name", "layer"),
        layer_type=row.get("layer_type", "mesh"),
        uri=row.get("uri", ""),
        raw=row,
    )


def _write_tif(path: str, paletted: bool) -> None:
    import numpy as np
    from osgeo import gdal, osr

    gdal.UseExceptions()
    ds = gdal.GetDriverByName("GTiff").Create(
        path, 4, 4, 1, gdal.GDT_Byte if paletted else gdal.GDT_Float32)
    ds.SetGeoTransform([0.0, 0.001, 0.0, 0.0, 0.0, -0.001])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    if paletted:
        table = gdal.ColorTable()
        for i, colour in enumerate(
                [(0, 0, 0, 255), (255, 0, 0, 255), (0, 255, 0, 255)]):
            table.SetColorEntry(i, colour)
        band.SetRasterColorTable(table)
        band.WriteArray(np.array([[0, 1, 2, 1]] * 4, dtype="uint8"))
    else:
        band.WriteArray(np.arange(16, dtype="float32").reshape(4, 4))
    ds.FlushCache()


#: The QGIS application, held at module scope for the life of the process: Qt
#: aborts the moment it is released, and this harness never tears it down.
_APP: "QgsApplication | None" = None


def main(slf_path: str) -> None:
    global _APP

    _APP = QgsApplication([], False)
    _APP.initQgis()
    from plugin.render import layers

    mesh = QgsMeshLayer(slf_path, "results", "mdal")
    assert mesh.isValid(), f"MDAL rejected {slf_path}"
    props = mesh.temporalProperties()
    assert props.isActive(), "MDAL left the mesh non-temporal"
    origin = props.referenceTime().toString(Qt.DateFormat.ISODate)
    assert origin.startswith("1900"), (
        f"the fixture already carries an origin ({origin}); this harness "
        "proves the stamp against a SELAFIN's own missing one")

    note = layers.stamp_mesh_temporal(
        mesh, _event(layers, reference_time=_REFERENCE))
    assert note and _REFERENCE in note, f"unexpected note: {note!r}"
    stamped = mesh.temporalProperties().referenceTime().toString(
        Qt.DateFormat.ISODate)
    assert stamped == _REFERENCE, f"reference time is {stamped}"
    extent = mesh.temporalProperties().timeExtent()
    begin = extent.begin().toString(Qt.DateFormat.ISODate)
    assert begin.startswith("2026-09-01"), f"time extent still at {begin}"
    print(f"mesh time extent: {begin} -> "
          f"{extent.end().toString(Qt.DateFormat.ISODate)}", flush=True)

    untouched = QgsMeshLayer(slf_path, "unstamped", "mdal")
    assert layers.stamp_mesh_temporal(untouched, _event(layers)) is None
    assert untouched.temporalProperties().referenceTime().toString(
        Qt.DateFormat.ISODate).startswith("1900"), (
        "a row that declared no origin was given one anyway")

    tmp = tempfile.mkdtemp(prefix="trid3nt_mesh_temporal_harness_")
    continuous = os.path.join(tmp, "continuous.tif")
    _write_tif(continuous, paletted=False)
    raster = QgsRasterLayer(continuous, "continuous", "gdal")
    assert raster.isValid(), "harness raster did not load"
    before = type(raster.renderer()).__name__
    style_note = layers.load_declared_style(raster, {"qml": _QML}, tmp)
    after = type(raster.renderer()).__name__
    assert style_note and "styled from the declared preset" in style_note, (
        f"unexpected style note: {style_note!r}")
    assert after == "QgsSingleBandPseudoColorRenderer", (
        f"renderer is {after}, not the preset's")
    print(f"raster renderer: {before} -> {after}", flush=True)

    paletted_path = os.path.join(tmp, "paletted.tif")
    _write_tif(paletted_path, paletted=True)
    painted = QgsRasterLayer(paletted_path, "paletted", "gdal")
    assert painted.isValid(), "harness paletted raster did not load"
    assert layers.load_declared_style(painted, {"kind": "classed"}, tmp) is None
    assert type(painted.renderer()).__name__ == "QgsPalettedRasterRenderer", (
        "QGIS did not keep the COG's own colour table")

    print("QT-MESH-TEMPORAL-OK", flush=True)


if __name__ == "__main__":
    # QGIS's own C++ teardown faults on this build with the harness layers still
    # alive, and a segfault at exit says nothing about what was measured. The
    # verdict is the flushed token, so the process ends on the measurement.
    try:
        main(sys.argv[1])
        _rc = 0
    except BaseException:  # noqa: BLE001 -- the traceback IS the failure report
        import traceback

        traceback.print_exc()
        _rc = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_rc)
