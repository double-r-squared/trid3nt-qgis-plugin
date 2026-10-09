"""Real-QGIS harness for the layer clocks, run as a SUBPROCESS by its wrapper.

Proved on the INSTALLED QGIS: MDAL opens a SELAFIN already temporal on a 1900
reference the file never states; stamping moves the extent onto the DECLARED
instant while a row declaring none keeps MDAL's axis; a preset style changes the
renderer; a declared quantity binds to the group MDAL actually reports; and a
dataset file written BESIDE a mesh loads onto it as a group the preset binds; a
preset that ranges from a field's floor clips below it, so the RESULT FILE's own
group masks where the derived group's nodata does."""

from __future__ import annotations

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from qgis.core import (  # noqa: E402
    QgsApplication,
    QgsMeshDatasetIndex,
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


#: The mesh preset as its writer writes it, with the DECLARED quantity in the
#: binding row QGIS remaps by name and the shader's clip flag as it writes it.
_MESH_QML = (
    "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
    '<qgis version="3.40.6" styleCategories="Symbology">\n'
    "  <mesh-renderer-settings>\n"
    '    <active-dataset-group scalar="0" vector="-1"/>\n'
    '    <scalar-settings group="0" min-val="{lo}" max-val="5" opacity="1"'
    ' interpolation-method="no-resampling">\n'
    '      <colorrampshader colorRampType="INTERPOLATED" classificationMode="1"'
    ' clip="{clip}" minimumValue="{lo}" maximumValue="5" labelPrecision="4">\n'
    '        <item value="{lo}" color="#a50026" alpha="255" label="{lo} mg/L"/>\n'
    '        <item value="5" color="#313695" alpha="255" label="5 mg/L"/>\n'
    "      </colorrampshader>\n"
    "    </scalar-settings>\n"
    "  </mesh-renderer-settings>\n"
    '  <name-to-global-index global-index="0" name="{declared}"/>\n'
    "</qgis>\n"
)

#: The groups a river result carries besides its tracer, and a quantity no
#: SELAFIN group answers to. A run names its tracer for what it releases, so the
#: group that is none of these is the one the binding proof declares.
_HYDRODYNAMIC = ("VELOCITY", "WATER DEPTH", "FREE SURFACE", "BOTTOM")
_DECLARED_ABSENT = "model_results"


def _mesh_qml(*, declared: str, clip: int = 0, lo: str = "0") -> str:
    """The mesh preset document, at the clip and range a case is proving."""
    return _MESH_QML.format(declared=declared, clip=clip, lo=lo)


def _write(tmp: str, stem: str, document: str) -> str:
    path = os.path.join(tmp, f"{stem}.qml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(document)
    return path


def _group_names(mesh):
    return [mesh.datasetGroupMetadata(QgsMeshDatasetIndex(i, 0)).name()
            for i in range(mesh.datasetGroupCount())]


def _mesh_binding(layers, slf_path: str, tmp: str) -> None:
    """The declared quantity, bound to the group the OPEN mesh reports."""
    mesh = QgsMeshLayer(slf_path, "tracer results", "mdal")
    assert mesh.isValid(), f"MDAL rejected {slf_path}"
    names = _group_names(mesh)
    print(f"mesh dataset groups: {names}", flush=True)
    matched = [i for i, n in enumerate(names)
               if not n.strip().upper().startswith(_HYDRODYNAMIC)]
    assert matched, f"the tracer fixture carries no tracer group: {names}"
    index = matched[0]
    declared = names[index].split()[0]

    # A document QGIS accepts and then renders nothing from: the declared
    # quantity is not how MDAL spells the group, so no group binds.
    unbound = QgsMeshLayer(slf_path, "unbound", "mdal")
    _msg, ok = unbound.loadNamedStyle(
        _write(tmp, "unbound", _mesh_qml(declared=declared)))
    assert ok, "QGIS rejected the mesh document outright"
    dropped = unbound.rendererSettings().activeScalarDatasetGroup()
    assert dropped == -1, (
        f"the unbound document left group {dropped} active; this harness "
        "proves the bind against a document that binds nothing")

    # Every group's classification is pinned first; the declared style then
    # wins on the one group it binds.
    layers._clamp_mesh_scalar_classification(mesh)
    note = layers.bind_declared_mesh_style(
        mesh, {"qml": _mesh_qml(declared=declared)}, tmp)
    print(f"bind note:{note}", flush=True)
    assert "styled from the declared preset" in note, note
    assert names[index].strip() in note, note
    settings = mesh.rendererSettings()
    assert settings.activeScalarDatasetGroup() == index, (
        f"active group is {settings.activeScalarDatasetGroup()}, not {index}")
    scalar = settings.scalarSettings(index)
    assert (scalar.classificationMinimum(), scalar.classificationMaximum()) == (
        0.0, 5.0), (f"declared range did not apply: "
                    f"{scalar.classificationMinimum()}.."
                    f"{scalar.classificationMaximum()}")
    colours = [i.color.name() for i in scalar.colorRampShader().colorRampItemList()]
    assert colours == ["#a50026", "#313695"], colours
    other = (index + 1) % len(names)
    kept = settings.scalarSettings(other)
    assert (kept.classificationMinimum(), kept.classificationMaximum()) != (0.0, 5.0), (
        "the declared range leaked onto a group the preset never bound")

    # A quantity no group answers to: MDAL's own default stands, said out loud.
    absent = QgsMeshLayer(slf_path, "absent", "mdal")
    before = absent.rendererSettings().activeScalarDatasetGroup()
    note = layers.bind_declared_mesh_style(
        absent, {"qml": _mesh_qml(declared=_DECLARED_ABSENT)}, tmp)
    print(f"unmatched note:{note}", flush=True)
    assert _DECLARED_ABSENT in note and "default group stands" in note, note
    assert absent.rendererSettings().activeScalarDatasetGroup() == before, (
        "an unmatched quantity moved the active group")

    # A row carrying no preset at all is still a note, never a silence.
    bare = layers.bind_declared_mesh_style(absent, None, tmp)
    assert "no declared preset" in bare, bare


def _dataset_beside_the_mesh(layers, slf_path: str, tmp: str) -> None:
    """A derived group written beside the mesh loads onto it, and binds.

    This is the whole of what a field output is now: the values the module
    measured, as the SMS ASCII dataset MDAL reads, on the mesh they were
    measured over."""
    from trid3nt_server.render.mesh_display import write_ascii_dataset

    mesh = QgsMeshLayer(slf_path, "with a derived group", "mdal")
    assert mesh.isValid(), f"MDAL rejected {slf_path}"
    before = _group_names(mesh)
    nodes = mesh.dataProvider().vertexCount()
    cells = mesh.dataProvider().faceCount()
    values = [float("nan")] * nodes
    values[0] = 5.0
    values[3] = 0.0
    group = "Peak dye concentration"
    path = os.path.join(tmp, "dye_concentration.dat")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(write_ascii_dataset(values, name=group, nodes=nodes, cells=cells))

    note = layers.LayerMaterializer._load_mesh_datasets(
        _materializer(layers), mesh,
        _event(layers, layer_id="L9", name="Peak dye concentration",
               dataset_uris=[path]))
    print(f"dataset note:{note}", flush=True)
    assert "1 dataset group(s) loaded beside the mesh" in note, note
    after = _group_names(mesh)
    assert len(after) == len(before) + 1, f"{before} -> {after}"
    assert after[-1] == group, after

    layers._clamp_mesh_scalar_classification(mesh)
    bind = layers.bind_declared_mesh_style(
        mesh, {"qml": _mesh_qml(declared=group)}, tmp)
    print(f"derived bind note:{bind}", flush=True)
    assert "styled from the declared preset" in bind, bind
    assert mesh.rendererSettings().activeScalarDatasetGroup() == len(after) - 1, (
        "the derived group did not become the active one")
    # The nodes written as nothing are nodata, not the ramp's bottom.
    index = QgsMeshDatasetIndex(len(after) - 1, 0)
    block = mesh.datasetValues(index, 0, 3)
    read = [block.value(i).scalar() for i in range(3)]
    assert read[0] == 5.0 and read[1] != read[1] and read[2] != read[2], read


def _floored_group_clips(layers, slf_path: str, tmp: str) -> None:
    """A preset ranged from a field's floor CLIPS below it, through the load.

    A group the result file carries cannot be rewritten with nothing under its
    floor, so the shader is what masks it; QGIS drops the flag with the rest of
    the block if the document is not shaped the way the writer shapes it."""
    mesh = QgsMeshLayer(slf_path, "floored tracer", "mdal")
    assert mesh.isValid(), f"MDAL rejected {slf_path}"
    names = _group_names(mesh)
    index = next(i for i, n in enumerate(names)
                 if not n.strip().upper().startswith(_HYDRODYNAMIC))
    declared = names[index].split()[0]

    layers._clamp_mesh_scalar_classification(mesh)
    note = layers.bind_declared_mesh_style(
        mesh, {"qml": _mesh_qml(declared=declared, clip=1, lo="0.25")}, tmp)
    print(f"clipped bind note:{note}", flush=True)
    assert "styled from the declared preset" in note, note
    assert "clipped below" in note, note
    shader = mesh.rendererSettings().scalarSettings(index).colorRampShader()
    assert shader.clip(), "the shader's clip flag did not survive the load"
    assert shader.minimumValue() == 0.25, shader.minimumValue()
    # QGIS's own read of a below-floor value: shaded is False, so nothing paints.
    assert shader.shade(0.1)[0] is False, shader.shade(0.1)
    assert shader.shade(1.0)[0] is True, shader.shade(1.0)


def _materializer(layers):
    """A materializer whose session temp is a real directory and nothing else."""
    made = object.__new__(layers.LayerMaterializer)
    made._temp_dir = tempfile.mkdtemp(prefix="trid3nt_mesh_datasets_")
    return made


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


def main(slf_path: str, tracer_slf_path: str) -> None:
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

    _mesh_binding(layers, tracer_slf_path, tmp)
    _floored_group_clips(layers, tracer_slf_path, tmp)
    _dataset_beside_the_mesh(layers, tracer_slf_path, tmp)

    print("QT-MESH-TEMPORAL-OK", flush=True)


if __name__ == "__main__":
    # QGIS's own C++ teardown faults on this build with the harness layers still
    # alive, and a segfault at exit says nothing about what was measured. The
    # verdict is the flushed token, so the process ends on the measurement.
    try:
        main(sys.argv[1], sys.argv[2])
        _rc = 0
    except BaseException:  # noqa: BLE001 -- the traceback IS the failure report
        import traceback

        traceback.print_exc()
        _rc = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_rc)
