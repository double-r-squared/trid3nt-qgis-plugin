"""QGIS-native raster rendering tests (the TiTiler -> QGIS swap).

Covers, with an in-memory stubbed ``qgis`` package (the established
``test_milestone2`` pattern -- no QGIS install required):

* DUAL-SHAPE uri resolution in ``LayerMaterializer._add_raster``:
  - NEW raw ``s3://...tif`` COG uri -> ``s3_to_http`` -> a
    ``QgsRasterLayer("/vsicurl/<minio-http>", name, "gdal")``;
  - LEGACY TiTiler XYZ tile TEMPLATE (old persisted cases) -> the
    percent-encoded ``url=`` query param unwraps to the SAME gdal path, and
    ``rescale``/``colormap_name`` are recovered from the query string for
    styling;
  - a plain non-TiTiler XYZ template still lands on the old wms branch
    (never silently dropped).
* Renderer CLASS per legend kind: continuous ->
  ``QgsSingleBandPseudoColorRenderer`` (Interpolated ``QgsColorRampShader``
  from the ``ramps`` table); categorical -> ``QgsPalettedRasterRenderer``
  from the COG's embedded GDAL color table, degrading to the gradient path
  when the table is absent.
* The ``ramps`` colormap table covers EVERY colormap name the server style
  registry can emit (scans ``server/src/.../publish_layer.py`` so registry
  drift fails here instead of rendering grey).

Run via ``make test`` from qgis-plugin/.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from stub_server import LEGACY_RASTER_LAYER_ROW, RASTER_LAYER_ROW  # noqa: E402

_SERVER_PUBLISH_LAYER = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "server",
    "src",
    "trid3nt_server",
    "tools",
    "publish_layer.py",
)

MINIO = "http://127.0.0.1:9000"


# --------------------------------------------------------------------------- #
# stubbed-qgis harness (mirrors test_milestone2's _import_layers pattern,
# extended with the raster-renderer API surface)
# --------------------------------------------------------------------------- #


def _import_layers():
    """Import ``trid3nt.render.layers`` against fake qgis modules; returns
    ``(layers_module, fakes_namespace)``."""

    class _FakeQSettings:
        def value(self, key, default=None):
            return default

        def setValue(self, key, value):
            pass

    class _FakeQDateTime:
        @staticmethod
        def fromString(text, fmt=None):
            return text

    class _FakeQt:
        ISODate = 1

    class _FakeQColor:
        def __init__(self, spec=""):
            self.spec = spec

        def name(self):
            return self.spec

    class _FakeLayerNode:
        def __init__(self, layer):
            self._layer = layer
            self.visibility = True

        def layer(self):
            return self._layer

        def setItemVisibilityChecked(self, checked):
            self.visibility = checked

    class _FakeGroup:
        def __init__(self, name=""):
            self._name = name
            self.children_ = []

        def name(self):
            return self._name

        def setName(self, name):
            self._name = name

        def setExpanded(self, expanded):
            pass

        def findGroups(self):
            return [c for c in self.children_ if isinstance(c, _FakeGroup)]

        def insertGroup(self, idx, name):
            group = _FakeGroup(name)
            self.children_.insert(0, group)
            return group

        def insertLayer(self, idx, layer):
            node = _FakeLayerNode(layer)
            self.children_.insert(0, node)
            return node

        def findLayerIds(self):
            return []

    class _FakeRoot(_FakeGroup):
        def findGroup(self, name):
            for child in self.children_:
                if isinstance(child, _FakeGroup) and child.name() == name:
                    return child
            return None

    class _FakeProject:
        _instance = None

        @classmethod
        def instance(cls):
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

        def __init__(self):
            self._root = _FakeRoot()
            self.added = []

        def layerTreeRoot(self):
            return self._root

        def addMapLayer(self, layer, add_to_legend=True):
            self.added.append(layer)

        def removeMapLayers(self, ids):
            pass

    class _FakeDataProvider:
        def __init__(self, color_table):
            self._color_table = list(color_table)

        def colorTable(self, band):
            return list(self._color_table)

    class _FakeRasterLayer:
        instances = []
        #: per-construction knobs (reset by each _import_layers call)
        next_valid = True
        next_color_table = []

        def __init__(self, path, name, provider=""):
            self.path, self._name, self.provider = path, name, provider
            self._valid = _FakeRasterLayer.next_valid
            self.renderer = None
            self.opacity = None
            self._provider = _FakeDataProvider(_FakeRasterLayer.next_color_table)
            _FakeRasterLayer.instances.append(self)

        def isValid(self):
            return self._valid

        def name(self):
            return self._name

        def dataProvider(self):
            return self._provider

        def setRenderer(self, renderer):
            self.renderer = renderer

        def setOpacity(self, opacity):
            self.opacity = opacity

    class _FakeVectorLayer(_FakeRasterLayer):
        pass

    class _FakeColorRampItem:
        def __init__(self, value, color, label=""):
            self.value, self.color, self.label = value, color, label

    class _FakeColorRampShader:
        Interpolated = 1
        ColorRampItem = _FakeColorRampItem

        def __init__(self, vmin=0.0, vmax=255.0, *args):
            self.vmin, self.vmax = vmin, vmax
            self.items = []
            self.ramp_type = None

        def setColorRampType(self, ramp_type):
            self.ramp_type = ramp_type

        def setColorRampItemList(self, items):
            self.items = list(items)

    class _FakeRasterShader:
        def __init__(self):
            self.fn = None

        def setRasterShaderFunction(self, fn):
            self.fn = fn

    class _FakePseudoColorRenderer:
        def __init__(self, provider, band, shader):
            self.provider, self.band, self.shader = provider, band, shader
            self.cmin = self.cmax = None

        def setClassificationMin(self, v):
            self.cmin = v

        def setClassificationMax(self, v):
            self.cmax = v

    class _FakePalettedRenderer:
        def __init__(self, provider, band, classes):
            self.provider, self.band, self.classes = provider, band, classes

        @staticmethod
        def colorTableToClassData(table):
            return list(table)

    class _FakeStyleDb:
        def colorRamp(self, name):
            return None  # force the hardcoded stop-table fallback (deterministic)

    class _FakeStyle:
        @staticmethod
        def defaultStyle():
            return _FakeStyleDb()

    qtcore = types.ModuleType("qgis.PyQt.QtCore")
    qtcore.QSettings = _FakeQSettings
    qtcore.QDateTime = _FakeQDateTime
    qtcore.Qt = _FakeQt
    qtgui = types.ModuleType("qgis.PyQt.QtGui")
    qtgui.QColor = _FakeQColor
    pyqt = types.ModuleType("qgis.PyQt")
    pyqt.QtCore = qtcore
    pyqt.QtGui = qtgui
    core = types.ModuleType("qgis.core")
    core.QgsDateTimeRange = type("QgsDateTimeRange", (), {})
    core.QgsProject = _FakeProject
    core.QgsRasterLayer = _FakeRasterLayer
    core.QgsVectorLayer = _FakeVectorLayer
    core.QgsCoordinateReferenceSystem = type("QgsCoordinateReferenceSystem", (), {})
    core.QgsCoordinateTransform = type("QgsCoordinateTransform", (), {})
    core.QgsRectangle = type("QgsRectangle", (), {})
    core.QgsMeshDatasetIndex = type("QgsMeshDatasetIndex", (), {})
    core.QgsMeshLayer = type("QgsMeshLayer", (), {})
    core.QgsColorRampShader = _FakeColorRampShader
    core.QgsPalettedRasterRenderer = _FakePalettedRenderer
    core.QgsRasterShader = _FakeRasterShader
    core.QgsSingleBandPseudoColorRenderer = _FakePseudoColorRenderer
    core.QgsStyle = _FakeStyle
    qgis_mod = types.ModuleType("qgis")
    qgis_mod.PyQt = pyqt
    qgis_mod.core = core

    stub_keys = (
        "qgis",
        "qgis.PyQt",
        "qgis.PyQt.QtCore",
        "qgis.PyQt.QtGui",
        "qgis.core",
    )
    saved = {k: sys.modules.get(k) for k in stub_keys}
    sys.modules.update(
        {
            "qgis": qgis_mod,
            "qgis.PyQt": pyqt,
            "qgis.PyQt.QtCore": qtcore,
            "qgis.PyQt.QtGui": qtgui,
            "qgis.core": core,
        }
    )
    plugin_root = os.path.join(os.path.dirname(__file__), "..")
    sys.path.insert(0, plugin_root)
    pkg_keys = [k for k in list(sys.modules) if k.split(".")[0] == "trid3nt"]
    saved_pkg = {k: sys.modules.pop(k) for k in pkg_keys}
    try:
        layers = importlib.import_module("trid3nt.render.layers")
    finally:
        sys.path.remove(plugin_root)
        for k in [k for k in list(sys.modules) if k.split(".")[0] == "trid3nt"]:
            sys.modules.pop(k, None)
        sys.modules.update(saved_pkg)
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    fakes = types.SimpleNamespace(
        RasterLayer=_FakeRasterLayer,
        PseudoColorRenderer=_FakePseudoColorRenderer,
        PalettedRenderer=_FakePalettedRenderer,
        Project=_FakeProject,
    )
    return layers, fakes


class _Settings:
    mode = "local"
    minio_endpoint = MINIO


def _event(layers, row_or_fields):
    """Build a ``LayerEvent`` (the class the imported layers module holds)
    from a stub-server row dict / plain field dict."""
    row = dict(row_or_fields)
    return layers.LayerEvent(
        layer_id=row["layer_id"],
        name=row.get("name") or row["layer_id"],
        layer_type=row.get("layer_type", "raster"),
        uri=row.get("uri", ""),
        wms_url=row.get("wms_url"),
        style_preset=row.get("style_preset"),
        inline_geojson=row.get("inline_geojson"),
        opacity=row.get("opacity"),
        visible=row.get("visible", True),
        legend=row.get("legend"),
        raw=row,
    )


# --------------------------------------------------------------------------- #
# dual-shape uri resolution
# --------------------------------------------------------------------------- #


class TestDualShapeUriResolution(unittest.TestCase):
    def test_raw_s3_uri_becomes_vsicurl_gdal_layer(self):
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        notes = m.materialize([_event(layers, RASTER_LAYER_ROW)])
        layer = fakes.RasterLayer.instances[0]
        self.assertEqual(
            layer.path,
            f"/vsicurl/{MINIO}/trid3nt-runs/dem/asheville.tif",
        )
        self.assertEqual(layer.provider, "gdal")
        self.assertTrue(any("COG via GDAL" in n for n in notes), notes)
        # opacity parity with the old tile layers (event.opacity -> setOpacity)
        self.assertEqual(layer.opacity, 1.0)

    def test_legacy_titiler_template_unwraps_to_same_gdal_path(self):
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        notes = m.materialize([_event(layers, LEGACY_RASTER_LAYER_ROW)])
        layer = fakes.RasterLayer.instances[0]
        self.assertEqual(
            layer.path,
            f"/vsicurl/{MINIO}/trid3nt-runs/flood/depth.tif",
        )
        self.assertEqual(layer.provider, "gdal")
        self.assertTrue(
            any("legacy tile template unwrapped" in n for n in notes), notes
        )
        # rescale=0,3 + colormap_name=ylgnbu recovered from the query string
        renderer = layer.renderer
        self.assertIsInstance(renderer, fakes.PseudoColorRenderer)
        self.assertEqual(renderer.cmin, 0.0)
        self.assertEqual(renderer.cmax, 3.0)
        colors = [item.color.spec for item in renderer.shader.fn.items]
        self.assertEqual(colors[0], "#ffffd9")   # ylgnbu low end
        self.assertEqual(colors[-1], "#081d58")  # ylgnbu high end

    def test_plain_xyz_template_keeps_wms_branch(self):
        """A non-TiTiler XYZ template (no url= param) must not be dropped --
        it still renders through the legacy wms/XYZ branch."""
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        notes = m.materialize(
            [
                _event(
                    layers,
                    {
                        "layer_id": "01PLAINXYZAAAAAAAAAAAAAAAA",
                        "name": "External tiles",
                        "uri": "https://tile.example.com/{z}/{x}/{y}.png",
                    },
                )
            ]
        )
        layer = fakes.RasterLayer.instances[0]
        self.assertEqual(layer.provider, "wms")
        self.assertIn("type=xyz&url=", layer.path)
        self.assertTrue(any("non-TiTiler template" in n for n in notes), notes)

    def test_raster_without_uri_or_template_is_honest_skip(self):
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        notes = m.materialize(
            [_event(layers, {"layer_id": "01NOURIAAAAAAAAAAAAAAAAAAA", "name": "empty", "uri": ""})]
        )
        self.assertEqual(fakes.RasterLayer.instances, [])
        self.assertTrue(any("skipped" in n for n in notes), notes)

    def test_remote_mode_is_honest_skip(self):
        layers, fakes = _import_layers()

        class _Remote(_Settings):
            mode = "remote"

        m = layers.LayerMaterializer(settings=_Remote())
        notes = m.materialize([_event(layers, RASTER_LAYER_ROW)])
        self.assertEqual(fakes.RasterLayer.instances, [])
        self.assertTrue(any("remote mode" in n for n in notes), notes)


# --------------------------------------------------------------------------- #
# renderer class per legend kind
# --------------------------------------------------------------------------- #


class TestRendererPerLegendKind(unittest.TestCase):
    def test_continuous_legend_builds_pseudocolor_renderer(self):
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        m.materialize([_event(layers, RASTER_LAYER_ROW)])
        layer = fakes.RasterLayer.instances[0]
        renderer = layer.renderer
        self.assertIsInstance(renderer, fakes.PseudoColorRenderer)
        self.assertEqual(renderer.band, 1)
        # legend vmin/vmax drive the classification range
        self.assertEqual(renderer.cmin, 600.0)
        self.assertEqual(renderer.cmax, 2100.0)
        shader_fn = renderer.shader.fn
        self.assertEqual(shader_fn.ramp_type, shader_fn.Interpolated)
        colors = [item.color.spec for item in shader_fn.items]
        self.assertEqual(colors[0], "#440154")   # viridis low
        self.assertEqual(colors[-1], "#fde725")  # viridis high
        values = [item.value for item in shader_fn.items]
        self.assertEqual(values[0], 600.0)
        self.assertEqual(values[-1], 2100.0)

    def test_categorical_legend_with_embedded_table_builds_paletted(self):
        layers, fakes = _import_layers()
        fakes.RasterLayer.next_color_table = [
            ("entry-11", "green"),
            ("entry-21", "grey"),
            ("entry-41", "forest"),
        ]
        m = layers.LayerMaterializer(settings=_Settings())
        notes = m.materialize(
            [
                _event(
                    layers,
                    {
                        "layer_id": "01CATRASTERAAAAAAAAAAAAAAA",
                        "name": "NLCD landcover",
                        "uri": "s3://trid3nt-runs/landcover/nlcd.tif",
                        "legend": {"kind": "categorical", "classes": []},
                    },
                )
            ]
        )
        layer = fakes.RasterLayer.instances[0]
        renderer = layer.renderer
        self.assertIsInstance(renderer, fakes.PalettedRenderer)
        self.assertEqual(renderer.band, 1)
        self.assertEqual(len(renderer.classes), 3)
        self.assertTrue(any("embedded color table, 3 classes" in n for n in notes), notes)

    def test_categorical_without_table_falls_back_to_legend_swatches(self):
        """No embedded GDAL palette (e.g. the sediment-yield log-binned COG):
        the legend's own class swatches drive a gradient renderer instead of
        silently defaulting to grey."""
        layers, fakes = _import_layers()
        fakes.RasterLayer.next_color_table = []
        m = layers.LayerMaterializer(settings=_Settings())
        m.materialize(
            [
                _event(
                    layers,
                    {
                        "layer_id": "01SEDIMENTAAAAAAAAAAAAAAAA",
                        "name": "Soil loss",
                        "uri": "s3://trid3nt-runs/rusle/yield.tif",
                        "legend": {
                            "kind": "categorical",
                            "classes": [
                                {"value_min": 0.0, "value_max": 1.0, "color": "#ffffcc", "label": "<1"},
                                {"value_min": 1.0, "value_max": 5.0, "color": "#fed976", "label": "1-5"},
                                {"value_min": 5.0, "value_max": 10.0, "color": "#e31a1c", "label": "5-10"},
                            ],
                        },
                    },
                )
            ]
        )
        renderer = fakes.RasterLayer.instances[0].renderer
        self.assertIsInstance(renderer, fakes.PseudoColorRenderer)
        colors = [item.color.spec for item in renderer.shader.fn.items]
        self.assertEqual(colors, ["#ffffcc", "#fed976", "#e31a1c"])
        # range spans the class anchors (bin midpoints)
        self.assertEqual(renderer.cmin, 0.5)
        self.assertEqual(renderer.cmax, 7.5)

    def test_unknown_colormap_never_defaults_to_grey(self):
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        notes = m.materialize(
            [
                _event(
                    layers,
                    {
                        "layer_id": "01UNKNOWNCMAPAAAAAAAAAAAAA",
                        "name": "Mystery field",
                        "uri": "s3://trid3nt-runs/x/y.tif",
                        "legend": {
                            "kind": "continuous",
                            "colormap": "not_a_real_ramp",
                            "vmin": 0.0,
                            "vmax": 10.0,
                        },
                    },
                )
            ]
        )
        renderer = fakes.RasterLayer.instances[0].renderer
        self.assertIsInstance(renderer, fakes.PseudoColorRenderer)
        colors = [item.color.spec for item in renderer.shader.fn.items]
        self.assertEqual(colors[0], "#440154")  # the viridis stand-in, not grey
        self.assertTrue(any("unknown colormap" in n for n in notes), notes)

    def test_no_legend_no_legacy_style_leaves_default_renderer(self):
        """Terrain/RGBA passthrough layers carry no legend BY DESIGN --
        GDAL's default render (grayscale autoscale / native RGB) is correct,
        so no renderer is forced onto them."""
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        notes = m.materialize(
            [
                _event(
                    layers,
                    {
                        "layer_id": "01TERRAINRGBAAAAAAAAAAAAAA",
                        "name": "Colored relief",
                        "uri": "s3://trid3nt-runs/terrain/relief.tif",
                    },
                )
            ]
        )
        layer = fakes.RasterLayer.instances[0]
        self.assertIsNone(layer.renderer)
        self.assertTrue(any("added (COG via GDAL)" in n for n in notes), notes)


# --------------------------------------------------------------------------- #
# ramps table completeness vs the server style registry
# --------------------------------------------------------------------------- #


class TestRampTableCoversServerRegistry(unittest.TestCase):
    """Every colormap name the server can emit must resolve to real stops.

    ``ramps.SERVER_COLORMAP_NAMES`` is a hand-synced mirror of the server
    style registry (see the ramps module docstring); this test scans the
    server source so a registry addition FAILS here until the mirror + stop
    table are updated -- colormap drift is never silent grey.
    """

    def _load_ramps(self):
        plugin_root = os.path.join(os.path.dirname(__file__), "..")
        sys.path.insert(0, plugin_root)
        try:
            # Pure-python module (no qgis imports) -- direct import is safe.
            from trid3nt.render import ramps

            return ramps
        finally:
            sys.path.remove(plugin_root)

    def test_every_mirrored_name_resolves_to_nongrey_stops(self):
        ramps = self._load_ramps()
        for name in ramps.SERVER_COLORMAP_NAMES:
            stops = ramps.resolve_stops(name)
            self.assertIsNotNone(stops, f"no ramp stops for {name!r}")
            self.assertGreaterEqual(len(stops), 2, name)
            colors = {color for _t, color in stops}
            self.assertGreater(len(colors), 1, f"{name!r} is a flat ramp")

    def test_generic_reversed_variant_resolves(self):
        ramps = self._load_ramps()
        # a *_r name with no direct table entry reverses its base
        stops = ramps.resolve_stops("viridis_r")
        self.assertIsNotNone(stops)
        self.assertEqual(stops[0][1], "#fde725")
        self.assertEqual(stops[-1][1], "#440154")

    @unittest.skipUnless(
        os.path.exists(_SERVER_PUBLISH_LAYER),
        "server tree not present next to qgis-plugin/",
    )
    def test_server_registry_is_subset_of_mirror(self):
        with open(_SERVER_PUBLISH_LAYER, "r", encoding="utf-8") as f:
            source = f.read()
        found: set[str] = set()
        # registry / family-rule tuples: ("lo,hi", "cmap")
        found.update(re.findall(r'\("[-0-9.,e]+",\s*"([a-z0-9_]+)"\)', source))
        # literal style-params emissions: &colormap_name=<cmap>
        found.update(re.findall(r"colormap_name=([a-z0-9_]+)", source))
        found.discard("name")  # the docstring placeholder "colormap_name=name"
        ramps = self._load_ramps()
        missing = sorted(found - set(ramps.SERVER_COLORMAP_NAMES))
        self.assertEqual(
            missing,
            [],
            "server style registry emits colormap names the plugin ramp "
            f"mirror does not carry: {missing} -- add them to "
            "trid3nt/render/ramps.py (SERVER_COLORMAP_NAMES + _RAMP_STOPS)",
        )
        # and every mirrored name must resolve (guards table typos)
        for name in found:
            self.assertIsNotNone(ramps.resolve_stops(name), name)


if __name__ == "__main__":
    unittest.main()
