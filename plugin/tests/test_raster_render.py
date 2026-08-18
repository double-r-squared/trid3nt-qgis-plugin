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
  registry can emit (scans ``src/.../publish_layer.py`` so registry
  drift fails here instead of rendering grey).

Run via ``make test`` from plugin/.
"""

from __future__ import annotations

import importlib
import math
import os
import re
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
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
    core.QgsMeshDatasetIndex = type(
        "QgsMeshDatasetIndex", (), {"__init__": lambda self, group=0, dataset=0: None}
    )
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
    plugin_root = os.path.join(os.path.dirname(__file__), "..", "..")
    sys.path.insert(0, plugin_root)
    pkg_keys = [k for k in list(sys.modules) if k.split(".")[0] == "plugin"]
    saved_pkg = {k: sys.modules.pop(k) for k in pkg_keys}
    try:
        layers = importlib.import_module("plugin.render.layers")
    finally:
        sys.path.remove(plugin_root)
        for k in [k for k in list(sys.modules) if k.split(".")[0] == "plugin"]:
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
        self.assertTrue(any("streamed via /vsicurl" in n for n in notes), notes)
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
        self.assertTrue(any("streamed via /vsicurl" in n for n in notes), notes)


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
        plugin_root = os.path.join(os.path.dirname(__file__), "..", "..")
        sys.path.insert(0, plugin_root)
        try:
            # Pure-python module (no qgis imports) -- direct import is safe.
            from plugin.render import ramps

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
        "server tree not present next to plugin/",
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


# --------------------------------------------------------------------------- #
# degenerate-numeric crash defenses (the 0.3.14 sweep)
# --------------------------------------------------------------------------- #


class _FakeDatasetIndex:
    """Stand-in for ``QgsMeshDatasetIndex(group, dataset)`` that remembers its
    group, so a fake mesh layer can map an index back to a group's metadata."""

    def __init__(self, group=0, dataset=0):
        self.group = group
        self.dataset = dataset


class _FakeGroupMeta:
    def __init__(self, mn, mx, name="depth"):
        self._mn, self._mx, self._name = mn, mx, name

    def minimum(self):
        return self._mn

    def maximum(self):
        return self._mx

    def name(self):
        return self._name


class _FakeScalarSettings:
    def __init__(self):
        self.cmin = self.cmax = None

    def setClassificationMinimumMaximum(self, mn, mx):
        self.cmin, self.cmax = mn, mx


class _FakeMeshRendererSettings:
    def __init__(self, scalars):
        self._scalars = scalars  # index -> _FakeScalarSettings
        self.set_indices = []

    def scalarSettings(self, idx):
        return self._scalars.get(idx)

    def setScalarSettings(self, idx, settings):
        self.set_indices.append(idx)


class _FakeMeshLayer:
    """N scalar groups, each ``(min, max)``. The clamp must reach EVERY group
    (not just the active one) -- a mid-session styling-panel switch to a
    degenerate non-active group is the crash this closes."""

    def __init__(self, ranges):
        # ranges: list of (min, max)
        self.scalars = {i: _FakeScalarSettings() for i in range(len(ranges))}
        self._metas = {
            i: _FakeGroupMeta(mn, mx) for i, (mn, mx) in enumerate(ranges)
        }
        self._settings = _FakeMeshRendererSettings(self.scalars)
        self.renderer_settings_set = False

    def datasetGroupCount(self):
        return len(self.scalars)

    def rendererSettings(self):
        return self._settings

    def setRendererSettings(self, settings):
        self.renderer_settings_set = True

    def datasetGroupMetadata(self, idx):
        # idx is the (monkeypatched) QgsMeshDatasetIndex(group, 0) -> we stashed
        # the group on it via the fake __init__ below.
        return self._metas[idx.group]


class TestMeshScalarClassificationClamp(unittest.TestCase):
    """The mesh analogue of the raster ``sane_range`` guard -- a degenerate
    MDAL scalar group range (all-nodata / all-dry / an empty scrubber
    timestep) is pinned to a finite classification BEFORE the native mesh
    renderer builds its colour-ramp legend (the arm64 SIGBUS the 0.3.8 raster
    fix never covered). ALL groups are clamped, so switching the active scalar
    group mid-session cannot hand a degenerate range to the native renderer."""

    def _clamp(self, ranges):
        layers, _ = _import_layers()
        # QgsMeshDatasetIndex(group, 0) must expose ``group`` so the fake layer
        # can map an index back to its group metadata.
        layers.QgsMeshDatasetIndex = _FakeDatasetIndex
        layer = _FakeMeshLayer(ranges)
        note = layers._clamp_mesh_scalar_classification(layer)
        return layer, note

    def test_nan_group_range_is_clamped_to_finite_default(self):
        layer, note = self._clamp([(float("nan"), float("nan"))])
        s = layer.scalars[0]
        # never a NaN into setClassificationMinimumMaximum
        self.assertTrue(math.isfinite(s.cmin))
        self.assertTrue(math.isfinite(s.cmax))
        self.assertLess(s.cmin, s.cmax)
        self.assertTrue(layer.renderer_settings_set)
        self.assertIsNotNone(note)
        self.assertIn("degenerate", note)

    def test_inf_and_zero_span_are_clamped(self):
        for mn, mx in ((0.0, float("inf")), (5.0, 5.0), (float("-inf"), 1.0)):
            layer, note = self._clamp([(mn, mx)])
            s = layer.scalars[0]
            self.assertTrue(math.isfinite(s.cmin))
            self.assertTrue(math.isfinite(s.cmax))
            self.assertLess(s.cmin, s.cmax)
            self.assertIsNotNone(note)

    def test_finite_range_is_pinned_without_a_note(self):
        # A sane range is still pinned (user-set classification -> QGIS will not
        # re-derive a per-timestep range while scrubbing) but yields no note.
        layer, note = self._clamp([(0.2, 3.4)])
        s = layer.scalars[0]
        self.assertEqual(s.cmin, 0.2)
        self.assertEqual(s.cmax, 3.4)
        self.assertIsNone(note)

    def test_degenerate_NON_active_group_is_still_clamped(self):
        # The crux of NATE's mid-session crash: group 0 (depth) is sane and
        # rendered at add time, but group 1 (an all-dry water-level field) is
        # NaN. Switching to group 1 in the styling panel would crash unless we
        # clamped it up front. Every group is reached.
        layer, note = self._clamp([(0.0, 2.5), (float("nan"), float("nan"))])
        self.assertEqual(layer.scalars[0].cmin, 0.0)          # sane group pinned
        self.assertTrue(math.isfinite(layer.scalars[1].cmin))  # bad group clamped
        self.assertTrue(math.isfinite(layer.scalars[1].cmax))
        self.assertLess(layer.scalars[1].cmin, layer.scalars[1].cmax)
        self.assertIn("1 degenerate", note)

    def test_no_scalar_groups_is_a_noop(self):
        layer, note = self._clamp([])
        self.assertFalse(layer.renderer_settings_set)
        self.assertIsNone(note)


class _FakeRect:
    def __init__(self, xmin, ymin, xmax, ymax, empty=False):
        self._b = (xmin, ymin, xmax, ymax)
        self._empty = empty
        self.scaled = False

    def isEmpty(self):
        return self._empty

    def xMinimum(self):
        return self._b[0]

    def yMinimum(self):
        return self._b[1]

    def xMaximum(self):
        return self._b[2]

    def yMaximum(self):
        return self._b[3]

    def scale(self, factor):
        self.scaled = True


class _FakeCanvas:
    def __init__(self):
        self.extent_set = False

    def setExtent(self, rect):
        self.extent_set = True

    def refresh(self):
        pass


class TestZoomToExtentFiniteGuard(unittest.TestCase):
    """A NaN/inf layer extent DEFEATS ``isEmpty()`` (NaN comparisons are all
    False); the finiteness guard stops it before the native ``setExtent`` /
    map-to-pixel transform ingests a non-finite double."""

    def test_nan_extent_is_refused(self):
        layers, _ = _import_layers()
        # monkeypatch the copy-constructor QgsRectangle to echo the fake back
        layers.QgsRectangle = lambda r: r
        canvas = _FakeCanvas()
        rect = _FakeRect(float("nan"), 0.0, 1.0, 1.0)
        self.assertFalse(layers.zoom_to_extent(canvas, rect))
        self.assertFalse(canvas.extent_set)

    def test_inf_extent_is_refused(self):
        layers, _ = _import_layers()
        layers.QgsRectangle = lambda r: r
        canvas = _FakeCanvas()
        rect = _FakeRect(0.0, 0.0, float("inf"), 1.0)
        self.assertFalse(layers.zoom_to_extent(canvas, rect))
        self.assertFalse(canvas.extent_set)

    def test_finite_extent_zooms(self):
        layers, _ = _import_layers()
        layers.QgsRectangle = lambda r: r
        canvas = _FakeCanvas()
        rect = _FakeRect(-85.0, 29.0, -84.0, 30.0)
        self.assertTrue(layers.zoom_to_extent(canvas, rect))
        self.assertTrue(canvas.extent_set)


class TestCategoricalNonFiniteAnchorsDropped(unittest.TestCase):
    """A categorical legend whose class anchors carry NaN/inf must not ferry a
    non-finite value into the gradient-fallback native ramp items."""

    def test_nan_class_values_are_filtered_before_the_native_ramp(self):
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        m.materialize(
            [
                _event(
                    layers,
                    {
                        "layer_id": "01NANCLASSAAAAAAAAAAAAAAAA",
                        "name": "Broken categorical",
                        "uri": "s3://trid3nt-runs/x/y.tif",
                        "legend": {
                            "kind": "categorical",
                            "classes": [
                                {"value": float("nan"), "color": "#ffffcc"},
                                {"value": 1.0, "color": "#fed976"},
                                {"value": float("inf"), "color": "#e31a1c"},
                                {"value": 5.0, "color": "#800026"},
                            ],
                        },
                    },
                )
            ]
        )
        renderer = fakes.RasterLayer.instances[0].renderer
        self.assertIsInstance(renderer, fakes.PseudoColorRenderer)
        # the two finite anchors (1.0, 5.0) drive a sane, finite range
        self.assertTrue(math.isfinite(renderer.cmin))
        self.assertTrue(math.isfinite(renderer.cmax))
        for item in renderer.shader.fn.items:
            self.assertTrue(math.isfinite(item.value), item.value)


class TestColorRampItemsRejectNonFiniteOffset(unittest.TestCase):
    """An explicit stops list carrying a non-finite offset must not produce a
    non-finite ``ColorRampItem`` value (``min/max`` pass NaN through)."""

    def test_nan_offset_stop_is_dropped(self):
        layers, _ = _import_layers()
        items, _note = layers._color_ramp_items(
            [[0.0, "#000000"], [float("nan"), "#808080"], [1.0, "#ffffff"]],
            0.0,
            10.0,
        )
        for item in items:
            self.assertTrue(math.isfinite(item.value), item.value)


class TestMeshStagingExtension(unittest.TestCase):
    """ADR 0283: a native mesh stages under its SOURCE extension, not a hardcoded
    ``.nc`` -- MDAL's driver selection is extension-sensitive, so a SELAFIN staged
    as ``.nc`` could be rejected. The staged filename derives its extension from
    the uri; ``.nc`` is only the default when the uri carries none."""

    def _capture_staged_fname(self, uri):
        layers, _ = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        captured = {}

        def _fake_stage(s3_uri, filename):
            captured["fname"] = filename
            return None  # honest miss -> _add_mesh returns a skip note; fname captured

        m._stage_s3_to_session = _fake_stage
        event = _event(
            layers,
            {"layer_id": "model-results-mesh-01ABC", "name": "Model results",
             "layer_type": "mesh", "uri": uri, "crs_authid": "EPSG:32617"},
        )
        m._add_mesh(event)
        return captured.get("fname")

    def test_selafin_stages_as_slf(self):
        fname = self._capture_staged_fname(
            "s3://trid3nt-runs/01ABC/r2d_river.slf"
        )
        self.assertTrue(fname.endswith(".slf"), fname)

    def test_netcdf_stages_as_nc(self):
        fname = self._capture_staged_fname(
            "s3://trid3nt-runs/01ABC/sfincs_map.nc"
        )
        self.assertTrue(fname.endswith(".nc"), fname)

    def test_extensionless_uri_defaults_to_nc(self):
        fname = self._capture_staged_fname("s3://trid3nt-runs/01ABC/mesh_object")
        self.assertTrue(fname.endswith(".nc"), fname)


if __name__ == "__main__":
    unittest.main()
