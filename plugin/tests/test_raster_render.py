"""QGIS-native raster rendering tests.

Covers, with an in-memory stubbed ``qgis`` package (the established
``test_milestone2`` pattern -- no QGIS install required):

* uri resolution in ``LayerMaterializer._add_raster``: an ``s3://...tif`` COG
  uri becomes ``QgsRasterLayer("/vsis3/<bucket>/<key>", name, "gdal")``, and
  anything that is not a store uri is an honest skip;
* the styling seam: the ``.qml`` the legend carries reaches
  ``loadNamedStyle``, and a layer whose file already carries its colours (no
  ``.qml`` on the legend) keeps QGIS's own renderer untouched;
* the temporal seam: a row's DECLARED ``valid_from``/``valid_to`` window
  becomes the layer's fixed temporal range, and a row that declares none is
  left alone.

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

from stub_server import RASTER_LAYER_ROW  # noqa: E402


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
        """Just enough of QDateTime for the declared-instant parse + compare."""

        def __init__(self, text="", valid=True):
            self.text, self._valid = text, valid

        @staticmethod
        def fromString(text, fmt=None):
            import datetime as _dt

            try:
                _dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
            except ValueError:
                return _FakeQDateTime(text, valid=False)
            return _FakeQDateTime(text)

        def isValid(self):
            return self._valid

        def toString(self, fmt=None):
            return self.text

        def __lt__(self, other):
            return self.text < other.text

        def __gt__(self, other):
            return self.text > other.text

        def __eq__(self, other):
            return isinstance(other, _FakeQDateTime) and self.text == other.text

    class _FakeQt:
        ISODate = 1

        class DateFormat:
            ISODate = 1

    class _FakeLayerNode:
        def __init__(self, layer):
            self._layer = layer
            self.visibility = True

        def layer(self):
            return self._layer

        def isVisible(self):
            return self.visibility

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

        def findLayer(self, layer_id):
            def walk(node):
                for child in node.children_:
                    if isinstance(child, _FakeGroup):
                        found = walk(child)
                        if found is not None:
                            return found
                    elif child.layer().id() == layer_id:
                        return child
                return None

            return walk(self)

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

        def mapLayers(self):
            return {layer.id(): layer for layer in self.added}

        def removeMapLayers(self, ids):
            pass

        def timeSettings(self):
            if not hasattr(self, "_time"):
                self._time = _FakeTimeSettings()
            return self._time

    class _FakeTemporalProps:
        def __init__(self):
            self.mode = None
            self.range = None
            self.active = False

        ModeFixedTemporalRange = "fixed"

        def setMode(self, mode):
            self.mode = mode

        def setFixedTemporalRange(self, rng):
            self.range = rng

        def setIsActive(self, active):
            self.active = active

    class _FakeRasterLayer:
        instances = []
        #: per-construction knobs (reset by each _import_layers call)
        next_valid = True
        #: what ``loadNamedStyle`` reports back, and the renderer name it
        #: leaves behind (a document that loads without changing the render
        #: is the case the seam has to notice).
        next_style_result = ("", True)
        next_styled_renderer = "QgsSingleBandPseudoColorRenderer"

        def __init__(self, path, name, provider=""):
            self.path, self._name, self.provider = path, name, provider
            self._valid = _FakeRasterLayer.next_valid
            self.renderer_name = "QgsMultiBandColorRenderer"
            self.loaded_qml = None
            self.opacity = None
            self.temporal = _FakeTemporalProps()
            self.properties = {}
            _FakeRasterLayer.instances.append(self)

        def isValid(self):
            return self._valid

        def id(self):
            return f"{self._name}_{id(self)}"

        def setCustomProperty(self, key, value):
            self.properties[key] = value

        def customProperty(self, key, default=None):
            return self.properties.get(key, default)

        def name(self):
            return self._name

        def renderer(self):
            return type(self.renderer_name, (), {})()

        def loadNamedStyle(self, path):
            with open(path, "r", encoding="utf-8") as fh:
                self.loaded_qml = fh.read()
            message, ok = _FakeRasterLayer.next_style_result
            if ok:
                self.renderer_name = _FakeRasterLayer.next_styled_renderer
            return message, ok

        def temporalProperties(self):
            return self.temporal

        def setOpacity(self, opacity):
            self.opacity = opacity

    class _FakeVectorLayer(_FakeRasterLayer):
        pass

    class _FakeTimeSettings:
        def __init__(self):
            self.range = None

        def temporalRange(self):
            return self.range

        def setTemporalRange(self, rng):
            self.range = rng

    qtcore = types.ModuleType("qgis.PyQt.QtCore")
    qtcore.QSettings = _FakeQSettings
    qtcore.QDateTime = _FakeQDateTime
    qtcore.Qt = _FakeQt
    pyqt = types.ModuleType("qgis.PyQt")
    pyqt.QtCore = qtcore
    core = types.ModuleType("qgis.core")

    class _FakeRange:
        def __init__(self, begin=None, end=None):
            self._begin, self._end = begin, end

        def begin(self):
            return self._begin

        def end(self):
            return self._end

        def isInfinite(self):
            return self._begin is None

    core.QgsDateTimeRange = _FakeRange
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
    qgis_mod = types.ModuleType("qgis")
    qgis_mod.PyQt = pyqt
    qgis_mod.core = core

    stub_keys = (
        "qgis",
        "qgis.PyQt",
        "qgis.PyQt.QtCore",
        "qgis.core",
    )
    saved = {k: sys.modules.get(k) for k in stub_keys}
    sys.modules.update(
        {
            "qgis": qgis_mod,
            "qgis.PyQt": pyqt,
            "qgis.PyQt.QtCore": qtcore,
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
        Project=_FakeProject,
    )
    return layers, fakes


class _Settings:
    mode = "local"


def _event(layers, row_or_fields):
    """Build a ``LayerEvent`` (the class the imported layers module holds)
    from a stub-server row dict / plain field dict."""
    row = dict(row_or_fields)
    return layers.LayerEvent(
        layer_id=row["layer_id"],
        name=row.get("name") or row["layer_id"],
        layer_type=row.get("layer_type", "raster"),
        uri=row.get("uri", ""),
        inline_geojson=row.get("inline_geojson"),
        opacity=row.get("opacity"),
        visible=row.get("visible", True),
        legend=row.get("legend"),
        raw=row,
    )


# --------------------------------------------------------------------------- #
# uri resolution
# --------------------------------------------------------------------------- #


class TestStoreUriResolution(unittest.TestCase):
    def test_s3_uri_becomes_a_vsis3_gdal_layer(self):
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        notes = m.materialize([_event(layers, RASTER_LAYER_ROW)])
        layer = fakes.RasterLayer.instances[0]
        self.assertEqual(layer.path, "/vsis3/trid3nt-runs/dem/asheville.tif")
        self.assertEqual(layer.provider, "gdal")
        self.assertTrue(any("streamed via /vsis3" in n for n in notes), notes)
        # event.opacity -> setOpacity
        self.assertEqual(layer.opacity, 1.0)

    def test_non_store_uri_is_an_honest_skip(self):
        """A uri that is not an s3:// object is skipped with a note, never
        silently dropped and never guessed at."""
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
        self.assertEqual(fakes.RasterLayer.instances, [])
        self.assertTrue(any("skipped" in n for n in notes), notes)

    def test_raster_without_uri_is_honest_skip(self):
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        notes = m.materialize(
            [_event(layers, {"layer_id": "01NOURIAAAAAAAAAAAAAAAAAAA", "name": "empty", "uri": ""})]
        )
        self.assertEqual(fakes.RasterLayer.instances, [])
        self.assertTrue(any("skipped" in n for n in notes), notes)

# --------------------------------------------------------------------------- #
# the un-emit
# --------------------------------------------------------------------------- #


class TestTheUnEmitReachesTheLayerTree(unittest.TestCase):
    def test_a_visibility_flip_on_a_row_already_seen_reaches_the_layer_tree(self):
        """Taking a layer off the canvas is a row that arrives again, flipped.

        Session state is replayed whole on every emit, so a layer this side has
        already added arrives many times; the row's ``visible`` is the un-emit,
        and it has to be applied to a layer the materializer will not add twice.
        """
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        m.materialize([_event(layers, RASTER_LAYER_ROW)])
        node = fakes.Project.instance().layerTreeRoot().findLayer(
            fakes.RasterLayer.instances[0].id())
        self.assertTrue(node.isVisible())

        hidden = dict(RASTER_LAYER_ROW, visible=False)
        m.materialize([_event(layers, hidden)])
        self.assertEqual(len(fakes.RasterLayer.instances), 1)
        self.assertFalse(node.isVisible())

        m.materialize([_event(layers, dict(RASTER_LAYER_ROW, visible=True))])
        self.assertTrue(node.isVisible())


# --------------------------------------------------------------------------- #
# the declared preset, loaded as QGIS's own style document
# --------------------------------------------------------------------------- #


_QML = (
    '<!DOCTYPE qgis><qgis version="3.40"><pipe><rasterrenderer '
    'type="singlebandpseudocolor" band="1"/></pipe></qgis>'
)


class TestDeclaredStyleIsLoadedNotRebuilt(unittest.TestCase):
    def test_the_legends_qml_reaches_load_named_style(self):
        """The document the producer resolved is what QGIS reads.

        Not a colour-ramp NAME this side looks up in a table of its own - that
        table was a mirror of the server's, and a mirror drifts.
        """
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        row = dict(RASTER_LAYER_ROW)
        row["legend"] = {"kind": "continuous", "qml": _QML}
        notes = m.materialize([_event(layers, row)])
        layer = fakes.RasterLayer.instances[0]
        self.assertEqual(layer.loaded_qml, _QML)
        self.assertTrue(any("styled from the declared preset" in n for n in notes), notes)

    def test_a_layer_that_carries_its_own_colours_keeps_qgis_own_renderer(self):
        """No ``.qml`` means the file is already painted - an RGB(A) composite or
        a COG with a band-1 colour table. Overriding QGIS there would repaint a
        picture the producer had already painted."""
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        row = dict(RASTER_LAYER_ROW)
        row["legend"] = {"kind": "classed", "classes": [{"value": 11, "color": "#0f0",
                                                         "label": "forest"}]}
        m.materialize([_event(layers, row)])
        layer = fakes.RasterLayer.instances[0]
        self.assertIsNone(layer.loaded_qml)
        self.assertEqual(layer.renderer_name, "QgsMultiBandColorRenderer")

    def test_a_rejected_document_is_an_honest_note_not_a_lost_layer(self):
        layers, fakes = _import_layers()
        fakes.RasterLayer.next_style_result = ("not well formed", False)
        m = layers.LayerMaterializer(settings=_Settings())
        row = dict(RASTER_LAYER_ROW)
        row["legend"] = {"kind": "continuous", "qml": _QML}
        notes = m.materialize([_event(layers, row)])
        self.assertEqual(len(fakes.RasterLayer.instances), 1)
        self.assertTrue(any("style not loaded" in n for n in notes), notes)

    def test_a_document_that_loads_without_changing_the_render_says_so(self):
        """``loadNamedStyle``'s boolean is well-formedness only, so a document
        that parses and changes nothing must not read as a styled layer."""
        layers, fakes = _import_layers()
        fakes.RasterLayer.next_styled_renderer = "QgsMultiBandColorRenderer"
        m = layers.LayerMaterializer(settings=_Settings())
        row = dict(RASTER_LAYER_ROW)
        row["legend"] = {"kind": "continuous", "qml": _QML}
        notes = m.materialize([_event(layers, row)])
        self.assertTrue(any("renderer is unchanged" in n for n in notes), notes)


# --------------------------------------------------------------------------- #
# the declared validity window, stamped as the layer's temporal range
# --------------------------------------------------------------------------- #


class TestDeclaredTemporalWindow(unittest.TestCase):
    def test_a_frames_declared_window_becomes_its_fixed_temporal_range(self):
        """The producer held the instant; this side stamps it.

        Nothing here reads a time out of the layer NAME, so a frame named in any
        language still plays, and a name that merely LOOKS like a step number
        cannot manufacture a clock.
        """
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        row = dict(RASTER_LAYER_ROW)
        row["valid_from"] = "2026-06-22T18:00:00Z"
        row["valid_to"] = "2026-06-22T18:10:00Z"
        notes = m.materialize([_event(layers, row)])
        layer = fakes.RasterLayer.instances[0]
        self.assertTrue(layer.temporal.active)
        self.assertEqual(layer.temporal.range.begin().toString(), "2026-06-22T18:00:00Z")
        self.assertEqual(layer.temporal.range.end().toString(), "2026-06-22T18:10:00Z")
        self.assertTrue(any("Temporal Controller" in n for n in notes), notes)

    def test_the_project_range_grows_to_cover_the_window(self):
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        row = dict(RASTER_LAYER_ROW)
        row["valid_from"] = "2026-06-22T18:00:00Z"
        row["valid_to"] = "2026-06-22T18:10:00Z"
        m.materialize([_event(layers, row)])
        span = fakes.Project.instance().timeSettings().temporalRange()
        self.assertEqual(span.begin().toString(), "2026-06-22T18:00:00Z")
        self.assertEqual(span.end().toString(), "2026-06-22T18:10:00Z")

    def test_a_row_that_declares_no_window_is_left_alone(self):
        """A still is not a frame. It gets no invented clock and no stamp."""
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        m.materialize([_event(layers, RASTER_LAYER_ROW)])
        layer = fakes.RasterLayer.instances[0]
        self.assertFalse(layer.temporal.active)
        self.assertIsNone(layer.temporal.range)

    def test_an_unparseable_instant_is_absent_not_guessed(self):
        layers, fakes = _import_layers()
        m = layers.LayerMaterializer(settings=_Settings())
        row = dict(RASTER_LAYER_ROW)
        row["valid_from"] = "sometime tuesday"
        row["valid_to"] = "2026-06-22T18:10:00Z"
        m.materialize([_event(layers, row)])
        self.assertFalse(fakes.RasterLayer.instances[0].temporal.active)


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
