"""Case export: the layer group a case opens into, cleared on reuse.

No QGIS required - Qt widgets are excluded.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from plugin.net import trid3nt_client as tc  # noqa: E402



def _make_gpkg(path: str, tables: list) -> None:
    """Minimal gpkg_contents so the pure sqlite3 listing works."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)"
    )
    for name, data_type in tables:
        conn.execute(
            "INSERT INTO gpkg_contents VALUES (?, ?)", (name, data_type)
        )
    conn.commit()
    conn.close()


class TestCaseGroupClearing(unittest.TestCase):
    """The case-switch group clear: one case group, no leftovers.

    ``layers.py`` imports ``qgis`` at module top, so an in-memory fake models the
    layer-tree nesting that the stale-group sweep drives directly."""

    def _import_layers(self):
        import importlib
        import types

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

        class _FakeLayerNode:
            """Stands in for QgsLayerTreeLayer."""

            def __init__(self, layer):
                self._layer = layer
                self._visible = True

            def layer(self):
                return self._layer

            def itemVisibilityChecked(self):
                return self._visible

            def setItemVisibilityChecked(self, checked):
                self._visible = checked

        class _FakeGroup:
            """Stands in for QgsLayerTreeGroup (and QgsLayerTreeRoot, which
            IS a group in real QGIS -- same fake class serves both)."""

            def __init__(self, name=""):
                self._name = name
                self.children_ = []  # list[_FakeGroup | _FakeLayerNode]
                self._expanded = True
                self._properties = {}

            def name(self):
                return self._name

            def setCustomProperty(self, key, value):
                self._properties[key] = value

            def customProperty(self, key, default=None):
                return self._properties.get(key, default)

            def setName(self, name):
                self._name = name

            def setExpanded(self, expanded):
                self._expanded = expanded

            def isExpanded(self):
                return self._expanded

            def children(self):
                return list(self.children_)

            def findGroup(self, name):
                for child in self.children_:
                    if isinstance(child, _FakeGroup) and child.name() == name:
                        return child
                return None

            def findGroups(self):
                return [c for c in self.children_ if isinstance(c, _FakeGroup)]

            def insertGroup(self, idx, name):
                group = _FakeGroup(name)
                self.children_.insert(0, group) if idx == 0 else self.children_.append(group)
                return group

            def insertLayer(self, idx, layer):
                node = _FakeLayerNode(layer)
                self.children_.insert(0, node) if idx == 0 else self.children_.append(node)
                return node

            def findLayer(self, layer_or_id):
                target_id = (
                    layer_or_id if isinstance(layer_or_id, str) else layer_or_id.id()
                )
                for child in self.children_:
                    if isinstance(child, _FakeLayerNode) and child.layer().id() == target_id:
                        return child
                    if isinstance(child, _FakeGroup):
                        found = child.findLayer(layer_or_id)
                        if found is not None:
                            return found
                return None

            def findLayerIds(self):
                ids = []
                for child in self.children_:
                    if isinstance(child, _FakeLayerNode):
                        ids.append(child.layer().id())
                    elif isinstance(child, _FakeGroup):
                        ids.extend(child.findLayerIds())
                return ids

            def removeChildNode(self, node):
                if node in self.children_:
                    self.children_.remove(node)

            def takeChild(self, node):
                if node in self.children_:
                    self.children_.remove(node)
                    return True
                return False

            def insertChildNode(self, idx, node):
                self.children_.insert(0, node) if idx == 0 else self.children_.append(node)

        class _FakeProject:
            _instance = None

            @classmethod
            def instance(cls):
                if cls._instance is None:
                    cls._instance = cls()
                return cls._instance

            def __init__(self):
                self._root = _FakeGroup("")
                self.added = []
                self.removed_ids = []

            def layerTreeRoot(self):
                return self._root

            def addMapLayer(self, layer, add_to_legend=True):
                self.added.append(layer)

            def mapLayers(self):
                return {layer.id(): layer for layer in self.added}

            def removeMapLayers(self, ids):
                self.removed_ids.extend(ids)
                self.added = [l for l in self.added if l.id() not in ids]

        class _FakeRasterLayer:
            _counter = 0
            instances = []
            style_result = ("", True)

            def __init__(self, path, name, provider=""):
                self._path, self._name = path, name
                _FakeRasterLayer._counter += 1
                self._id = f"{name}_{_FakeRasterLayer._counter}"
                self.style_loads = []
                self.repainted = False
                self._properties = {}
                _FakeRasterLayer.instances.append(self)

            def setCustomProperty(self, key, value):
                self._properties[key] = value

            def customProperty(self, key, default=None):
                return self._properties.get(key, default)

            def isValid(self):
                return True

            def id(self):
                return self._id

            def name(self):
                return self._name

            def loadNamedStyle(self, qml_path):
                self.style_loads.append(qml_path)
                return self.style_result

            def triggerRepaint(self):
                self.repainted = True

        class _FakeVectorLayer(_FakeRasterLayer):
            pass

        qtcore = types.ModuleType("qgis.PyQt.QtCore")
        qtcore.QSettings = _FakeQSettings
        qtcore.QDateTime = _FakeQDateTime
        qtcore.Qt = _FakeQt
        pyqt = types.ModuleType("qgis.PyQt")
        pyqt.QtCore = qtcore
        core = types.ModuleType("qgis.core")
        core.QgsDateTimeRange = type("QgsDateTimeRange", (), {})
        core.QgsProject = _FakeProject
        core.QgsRasterLayer = _FakeRasterLayer
        core.QgsVectorLayer = _FakeVectorLayer
        core.QgsCoordinateReferenceSystem = type("QgsCoordinateReferenceSystem", (), {})
        core.QgsCoordinateTransform = type("QgsCoordinateTransform", (), {})
        core.QgsRectangle = type("QgsRectangle", (), {})
        # Not exercised by these tests (no mesh events here) -- just need to
        # exist so ``layers.py``'s module-level import succeeds (MDAL phase 1).
        core.QgsMeshDatasetIndex = type("QgsMeshDatasetIndex", (), {})
        core.QgsMeshLayer = type("QgsMeshLayer", (), {})
        # QGIS-native raster rendering names (TiTiler->QGIS swap) -- not
        # exercised by these tests; existence only so the module import
        # succeeds. The raster-render tests exercise them for real.
        core.QgsColorRampShader = type("QgsColorRampShader", (), {})
        core.QgsPalettedRasterRenderer = type("QgsPalettedRasterRenderer", (), {})
        core.QgsRasterShader = type("QgsRasterShader", (), {})
        core.QgsSingleBandPseudoColorRenderer = type(
            "QgsSingleBandPseudoColorRenderer", (), {}
        )
        core.QgsStyle = type("QgsStyle", (), {})
        qtgui = types.ModuleType("qgis.PyQt.QtGui")
        qtgui.QColor = type("QColor", (), {})
        pyqt.QtGui = qtgui
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
        return layers, core.QgsProject, _FakeRasterLayer

    def _event(self, tc_mod, layer_id, name, uri=None):
        return tc_mod.LayerEvent(
            layer_id=layer_id,
            name=name,
            layer_type="raster",
            uri=uri or f"s3://trid3nt-runs/case/{layer_id}.tif",
        )

    # -- ITEM A: case-switch clears stale TRID3NT groups ------------------- #

    def test_set_case_clears_previous_case_group_and_its_layers(self):
        from plugin.net import trid3nt_client as tc

        layers, FakeProject, _fake_raster = self._import_layers()
        m = layers.LayerMaterializer(settings=None)
        # The basemap: added directly to root, NEVER inside a group.
        root = FakeProject.instance().layerTreeRoot()
        osm = _fake_raster("", "OpenStreetMap")
        FakeProject.instance().addMapLayer(osm, False)
        root.insertLayer(-1, osm)

        m.set_case("case-a", "Case A")
        m.materialize([self._event(tc, "L1", "DEM")])
        self.assertEqual([g.name() for g in root.findGroups()], ["TRID3NT Case A"])

        m.set_case("case-b", "Case B")
        # Case A's group is gone before Case B's layers land.
        self.assertEqual(root.findGroups(), [])
        # The basemap (never inside a group) survives untouched.
        self.assertIn(osm, FakeProject.instance().added)
        self.assertNotIn(osm.id(), FakeProject.instance().removed_ids)

        m.materialize([self._event(tc, "L2", "Bridges")])
        names = [g.name() for g in root.findGroups()]
        self.assertEqual(names, ["TRID3NT Case B"])

    def test_reselecting_the_same_case_does_not_duplicate_layers(self):
        from plugin.net import trid3nt_client as tc

        layers, FakeProject, _fake_raster = self._import_layers()
        m = layers.LayerMaterializer(settings=None)
        root = FakeProject.instance().layerTreeRoot()

        m.set_case("case-a", "Case A")
        m.materialize([self._event(tc, "L1", "DEM")])
        m.set_case("case-a", "Case A")  # re-open the SAME case
        m.materialize([self._event(tc, "L1", "DEM")])

        self.assertEqual([g.name() for g in root.findGroups()], ["TRID3NT Case A"])
        case_group = root.findGroup("TRID3NT Case A")
        self.assertEqual(len(case_group.findLayerIds()), 1)  # no duplicate


if __name__ == "__main__":
    unittest.main(verbosity=2)
