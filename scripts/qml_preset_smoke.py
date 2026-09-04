"""Load every preset the family can write into the installed QGIS and read it back.

``loadNamedStyle`` returns a boolean about WELL-FORMEDNESS, so believing it is
how a document that parses but styles nothing gets shipped. This asserts the
POST-LOAD STATE instead: the renderer QGIS ends up holding is the one the
document asked for, the stops read back at the values and colours written, and
the range the layer reports is the range the preset resolved.

Run it with the SYSTEM python (the one QGIS is installed against), from the
repo root:

    QT_QPA_PLATFORM=offscreen python3 scripts/qml_preset_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trid3nt_server.emission import presets  # noqa: E402

_FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")
    if not ok:
        _FAILURES.append(f"{name}: {detail}")


def _write(tmp: Path, stem: str, document: str) -> str:
    path = tmp / f"{stem}.qml"
    path.write_text(document, encoding="utf-8")
    return str(path)


def _raster_fixture(tmp: Path) -> str:
    import numpy as np
    from osgeo import gdal, osr

    path = tmp / "raster.tif"
    ds = gdal.GetDriverByName("GTiff").Create(str(path), 16, 16, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((0, 1, 0, 16, 0, -1))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(
        np.linspace(0.0, 3.0, 256).reshape(16, 16).astype("float32"))
    ds = None
    return str(path)


def _mesh_fixture(tmp: Path) -> str:
    path = tmp / "mesh.2dm"
    path.write_text(
        "MESH2D\n"
        "ND 1 0.0 0.0 0.0\nND 2 1.0 0.0 1.0\nND 3 1.0 1.0 2.0\nND 4 0.0 1.0 1.0\n"
        "E3T 1 1 2 3 1\nE3T 2 1 3 4 1\n",
        encoding="utf-8")
    return str(path)


def continuous(tmp: Path) -> None:
    from qgis.core import QgsRasterLayer

    resolved = presets.resolve(
        presets.Preset(kind="continuous", ramp="ylgnbu", units="m",
                       label="Flood depth",
                       scale=presets.Scale(policy="fixed", range=(0.0, 3.0))))
    layer = QgsRasterLayer(_raster_fixture(tmp), "r", "gdal")
    before = layer.renderer().type()
    _msg, ok = layer.loadNamedStyle(_write(tmp, "continuous", resolved.qml()))
    check("continuous / loads", bool(ok))
    renderer = layer.renderer()
    check("continuous / renderer is singlebandpseudocolor",
          renderer.type() == "singlebandpseudocolor", renderer.type())
    check("continuous / the render changed", renderer.type() != before, before)
    items = renderer.shader().rasterShaderFunction().colorRampItemList()
    stops = presets.ramp_stops("ylgnbu")
    check("continuous / stop count", len(items) == len(stops), str(len(items)))
    check("continuous / stops read back exactly",
          [i.color.name() for i in items] == list(stops),
          str([i.color.name() for i in items]))
    check("continuous / range read back",
          (items[0].value, items[-1].value) == (0.0, 3.0),
          f"{items[0].value}..{items[-1].value}")
    check("continuous / the legend carries the units",
          items[-1].label.endswith(" m"), items[-1].label)


def classed(tmp: Path) -> None:
    from qgis.core import QgsFeature, QgsGeometry, QgsRasterLayer, QgsVectorLayer

    # A classed RASTER: discrete bands, one per declared break.
    raster_style = presets.resolve(presets.Preset(
        kind="classed", units="t/ha/yr",
        classes=((0.0, 1.0, "#ffffcc", "< 1"), (1.0, 3.0, "#bd0026", "1-3"))))
    layer = QgsRasterLayer(_raster_fixture(tmp), "r", "gdal")
    _msg, ok = layer.loadNamedStyle(_write(tmp, "classed_raster", raster_style.qml()))
    check("classed[raster] / loads", bool(ok))
    check("classed[raster] / renderer is singlebandpseudocolor",
          layer.renderer().type() == "singlebandpseudocolor", layer.renderer().type())
    shader = layer.renderer().shader().rasterShaderFunction()
    check("classed[raster] / the shader is DISCRETE",
          shader.colorRampType() == shader.Discrete, str(shader.colorRampType()))
    breaks = [(i.value, i.color.name(), i.label) for i in shader.colorRampItemList()]
    check("classed[raster] / breaks read back exactly",
          breaks == [(1.0, "#ffffcc", "< 1"), (3.0, "#bd0026", "1-3")], str(breaks))

    resolved = presets.resolve(presets.Preset(
        kind="classed", geometry="polygon", units="t/ha/yr",
        classes=((0.0, 1.0, "#ffffcc", "< 1 (very low)"),
                 (1.0, 5.0, "#feb24c", "1-5 (low)"),
                 (5.0, 100.0, "#bd0026", ">= 5 (high)"))))
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326&field=value:double", "v", "memory")
    feature = QgsFeature(layer.fields())
    feature.setGeometry(QgsGeometry.fromWkt("POLYGON((0 0,1 0,1 1,0 1,0 0))"))
    feature.setAttribute("value", 0.5)
    layer.dataProvider().addFeatures([feature])
    before = layer.renderer().type()
    _msg, ok = layer.loadNamedStyle(_write(tmp, "classed_vector", resolved.qml()))
    check("classed[vector] / loads", bool(ok))
    renderer = layer.renderer()
    check("classed[vector] / renderer is graduatedSymbol",
          renderer.type() == "graduatedSymbol", renderer.type())
    check("classed[vector] / the render changed", renderer.type() != before, before)
    check("classed[vector] / classifies on the declared attribute",
          renderer.classAttribute() == "value", renderer.classAttribute())
    breaks = [(r.lowerValue(), r.upperValue(), r.symbol().color().name(), r.label())
              for r in renderer.ranges()]
    check("classed[vector] / breaks read back exactly",
          breaks == [(0.0, 1.0, "#ffffcc", "< 1 (very low)"),
                     (1.0, 5.0, "#feb24c", "1-5 (low)"),
                     (5.0, 100.0, "#bd0026", ">= 5 (high)")], str(breaks))


def reference(tmp: Path) -> None:
    from qgis.core import QgsVectorLayer

    for geometry, wkb, symbol_type in (("point", "Point", "marker"),
                                       ("line", "LineString", "line"),
                                       ("polygon", "Polygon", "fill")):
        resolved = presets.resolve(
            presets.Preset(kind="reference", geometry=geometry,  # type: ignore[arg-type]
                           color="#1f78b4", label="NHDPlus flowlines"))
        layer = QgsVectorLayer(f"{wkb}?crs=EPSG:4326", "v", "memory")
        _msg, ok = layer.loadNamedStyle(_write(tmp, f"reference_{geometry}", resolved.qml()))
        check(f"reference[{geometry}] / loads", bool(ok))
        renderer = layer.renderer()
        check(f"reference[{geometry}] / renderer is singleSymbol",
              renderer.type() == "singleSymbol", renderer.type())
        symbol = renderer.symbol()
        check(f"reference[{geometry}] / symbol matches the geometry",
              symbol.symbolLayer(0).layerType() in
              {"marker": "SimpleMarker", "line": "SimpleLine", "fill": "SimpleFill"}[symbol_type],
              symbol.symbolLayer(0).layerType())
        check(f"reference[{geometry}] / colour read back",
              symbol.color().name() == "#1f78b4", symbol.color().name())


def mesh(tmp: Path) -> None:
    from qgis.core import QgsMeshLayer

    resolved = presets.resolve(presets.Preset(
        kind="mesh", ramp="rdylbu", units="mg/L", label="Dissolved oxygen",
        dataset_group="Bed Elevation",
        scale=presets.Scale(policy="fixed", range=(-5.0, 7.0))))
    layer = QgsMeshLayer(_mesh_fixture(tmp), "m", "mdal")
    check("mesh / fixture is a mesh", layer.isValid())
    _msg, ok = layer.loadNamedStyle(_write(tmp, "mesh", resolved.qml()))
    check("mesh / loads", bool(ok))
    settings = layer.rendererSettings()
    check("mesh / the declared group is active",
          settings.activeScalarDatasetGroup() == 0,
          str(settings.activeScalarDatasetGroup()))
    scalar = settings.scalarSettings(0)
    check("mesh / range read back",
          (scalar.classificationMinimum(), scalar.classificationMaximum()) == (-5.0, 7.0),
          f"{scalar.classificationMinimum()}..{scalar.classificationMaximum()}")
    items = scalar.colorRampShader().colorRampItemList()
    stops = presets.ramp_stops("rdylbu")
    check("mesh / stops read back exactly",
          [i.color.name() for i in items] == list(stops),
          str([i.color.name() for i in items]))
    check("mesh / the legend carries the units",
          items[-1].label.endswith(" mg/L"), items[-1].label)


def main() -> int:
    from qgis.core import QgsApplication

    QgsApplication.setPrefixPath("/usr", True)
    app = QgsApplication([], False)
    app.initQgis()
    try:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            for kind in presets.KINDS:
                print(f"{kind}:")
                {"continuous": continuous, "classed": classed,
                 "reference": reference, "mesh": mesh}[kind](tmp)
    finally:
        app.exitQgis()
    if _FAILURES:
        print(f"\nFAILED {len(_FAILURES)}:")
        for line in _FAILURES:
            print(f"  {line}")
        return 1
    print("\nall_passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
