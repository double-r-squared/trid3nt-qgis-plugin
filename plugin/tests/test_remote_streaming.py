"""Remote reads -- the plugin side.

Covers three things the materializer owes:

  * session-scoped staging: ``_ensure_temp_dir`` creates one
    ``trid3nt_session_<tag>`` subdir (owner-PID marked), ``cleanup_session``
    removes it, ``sweep_stale_session_dirs`` reaps a DEAD-owner leftover but
    keeps a LIVE-owner dir (a concurrent QGIS instance);
  * STREAMED vs STAGED honesty labels on every layer note;
  * the MDAL mesh cache hop (``_add_mesh``) -- the ONE format with no /vsi
    layer, so it stages to the session dir, labeled.

No QGIS required: reuses the fake-qgis import harness from
``test_raster_render`` and monkeypatches the mesh/CRS fakes onto the imported
module (``layers.py`` holds them as module attributes).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))

from test_raster_render import _Settings, _event, _import_layers  # noqa: E402


class _CrsFake:
    def __init__(self, authid=""):
        self._authid = authid

    def isValid(self):
        return bool(self._authid) and self._authid != "EPSG:BOGUS"


class _MeshFake:
    instances: list = []
    next_valid = True

    def __init__(self, path, name, provider=""):
        self.path, self._name, self.provider = path, name, provider
        self._valid = _MeshFake.next_valid
        self.crs = None
        self.opacity = None
        _MeshFake.instances.append(self)

    def isValid(self):
        return self._valid

    def name(self):
        return self._name

    def setCrs(self, crs):
        self.crs = crs

    def datasetGroupCount(self):
        return 0  # no groups -> peak/tracer selection is a clean no-op

    def setOpacity(self, opacity):
        self.opacity = opacity


class TestSessionStagingDir(unittest.TestCase):
    def setUp(self):
        self.layers, _ = _import_layers()

    def test_ensure_temp_dir_is_session_scoped_and_pid_marked(self):
        m = self.layers.LayerMaterializer(settings=_Settings())
        path = m._ensure_temp_dir()
        self.assertTrue(os.path.isdir(path))
        base = os.path.basename(path)
        self.assertTrue(base.startswith("trid3nt_session_"), base)
        self.assertEqual(os.path.dirname(path), tempfile.gettempdir())
        with open(os.path.join(path, ".owner_pid"), encoding="utf-8") as f:
            self.assertEqual(int(f.read().strip()), os.getpid())
        # idempotent
        self.assertEqual(m._ensure_temp_dir(), path)
        m.cleanup_session()

    def test_cleanup_session_removes_the_dir(self):
        m = self.layers.LayerMaterializer(settings=_Settings())
        path = m._ensure_temp_dir()
        with open(os.path.join(path, "staged.nc"), "w", encoding="utf-8") as f:
            f.write("x")
        m.cleanup_session()
        self.assertFalse(os.path.exists(path))
        # a second cleanup is a harmless no-op
        m.cleanup_session()

    def test_sweep_reaps_dead_owner_keeps_live_owner(self):
        root = tempfile.gettempdir()
        dead = os.path.join(root, "trid3nt_session_deadtest01")
        live = os.path.join(root, "trid3nt_session_livetest01")
        nomark = os.path.join(root, "trid3nt_session_nomarktest")
        for d in (dead, live, nomark):
            os.makedirs(d, exist_ok=True)
        try:
            # a PID that cannot be alive (POSIX: 2^31-ish never-allocated)
            with open(os.path.join(dead, ".owner_pid"), "w", encoding="utf-8") as f:
                f.write("2147480000")
            with open(os.path.join(live, ".owner_pid"), "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))  # THIS process = alive
            # nomark has no .owner_pid -> unreadable owner -> treated as stale
            self.layers.sweep_stale_session_dirs()
            self.assertFalse(os.path.exists(dead), "dead-owner leftover not reaped")
            self.assertFalse(os.path.exists(nomark), "unmarked leftover not reaped")
            self.assertTrue(os.path.isdir(live), "LIVE-owner dir was wrongly reaped")
        finally:
            for d in (dead, live, nomark):
                if os.path.isdir(d):
                    import shutil

                    shutil.rmtree(d, ignore_errors=True)


class TestStreamedStagedLabels(unittest.TestCase):
    def setUp(self):
        self.layers, self.fakes = _import_layers()
        self.fakes.RasterLayer.instances = []
        self.m = self.layers.LayerMaterializer(settings=_Settings())
        self.m.set_case("case-a", "Case A")

    def test_raster_note_is_labeled_streamed(self):
        notes = self.m.materialize(
            [
                _event(
                    self.layers,
                    {
                        "layer_id": "01RASTERSTREAMAAAAAAAAAAAA",
                        "name": "DEM",
                        "uri": "s3://trid3nt-runs/dem/x.tif",
                    },
                )
            ]
        )
        self.assertTrue(
            any("streamed via /vsis3 (no local copy)" in n for n in notes), notes
        )

    def test_vector_fgb_note_is_labeled_streamed(self):
        notes = self.m.materialize(
            [
                _event(
                    self.layers,
                    {
                        "layer_id": "01VECTORSTREAMAAAAAAAAAAAA",
                        "name": "Stations",
                        "layer_type": "vector",
                        "uri": "s3://trid3nt-cache/x.fgb",
                    },
                )
            ]
        )
        self.assertTrue(any("streamed via /vsis3" in n for n in notes), notes)

    def test_inline_geojson_note_is_labeled_staged(self):
        notes = self.m.materialize(
            [
                _event(
                    self.layers,
                    {
                        "layer_id": "01INLINEGEOJSONAAAAAAAAAAA",
                        "name": "Merged points",
                        "layer_type": "vector",
                        "uri": "",
                        "inline_geojson": {"type": "FeatureCollection", "features": []},
                    },
                )
            ]
        )
        self.assertTrue(
            any("staged to session temp" in n for n in notes), notes
        )
        self.m.cleanup_session()


class TestMeshStagingFallback(unittest.TestCase):
    def setUp(self):
        self.layers, self.fakes = _import_layers()
        self.layers.QgsMeshLayer = _MeshFake
        self.layers.QgsCoordinateReferenceSystem = _CrsFake
        _MeshFake.instances = []
        _MeshFake.next_valid = True
        self.m = self.layers.LayerMaterializer(settings=_Settings())
        self.m.set_case("case-a", "Case A")

    def tearDown(self):
        self.m.cleanup_session()

    def test_local_mesh_path_loads_and_is_labeled_staged(self):
        # a mesh handed as a local path (test/headless drive) skips the download
        nc = os.path.join(self.m._ensure_temp_dir(), "sfincs_map.nc")
        with open(nc, "w", encoding="utf-8") as f:
            f.write("fake")
        notes = self.m.materialize(
            [
                _event(
                    self.layers,
                    {
                        "layer_id": "01MESHLOCALAAAAAAAAAAAAAAA",
                        "name": "SFINCS mesh",
                        "layer_type": "mesh",
                        "uri": nc,
                        "crs_authid": "EPSG:32617",
                    },
                )
            ]
        )
        self.assertEqual(len(_MeshFake.instances), 1)
        self.assertEqual(_MeshFake.instances[0].path, nc)
        self.assertTrue(any("staged to session temp" in n for n in notes), notes)
        self.assertTrue(any("MDAL nc" in n for n in notes), notes)
        # a valid crs_authid was applied, so the note does NOT ask to set it
        self.assertFalse(any("set manually" in n for n in notes), notes)

    def test_mesh_missing_crs_is_honestly_noted(self):
        nc = os.path.join(self.m._ensure_temp_dir(), "mesh2.nc")
        with open(nc, "w", encoding="utf-8") as f:
            f.write("fake")
        notes = self.m.materialize(
            [
                _event(
                    self.layers,
                    {
                        "layer_id": "01MESHNOCRSAAAAAAAAAAAAAAA",
                        "name": "mesh no crs",
                        "layer_type": "mesh",
                        "uri": nc,
                    },
                )
            ]
        )
        self.assertTrue(any("CRS unknown" in n and "set manually" in n for n in notes), notes)

    def test_s3_mesh_stages_via_session_download(self):
        # monkeypatch the download so no network is touched; the point is that
        # _add_mesh routes an s3 uri through the SESSION staging path.
        staged = os.path.join(self.m._ensure_temp_dir(), "downloaded.nc")
        with open(staged, "w", encoding="utf-8") as f:
            f.write("fake")
        self.m._stage_s3_to_session = lambda uri, fname: staged
        notes = self.m.materialize(
            [
                _event(
                    self.layers,
                    {
                        "layer_id": "01MESHS3AAAAAAAAAAAAAAAAAA",
                        "name": "s3 mesh",
                        "layer_type": "mesh",
                        "uri": "s3://trid3nt-runs/run/sfincs_map.nc",
                        "crs_authid": "EPSG:32617",
                    },
                )
            ]
        )
        self.assertEqual(len(_MeshFake.instances), 1)
        self.assertEqual(_MeshFake.instances[0].path, staged)
        self.assertTrue(any("staged to session temp" in n for n in notes), notes)

    def test_s3_mesh_stage_failure_is_honest_skip(self):
        self.m._stage_s3_to_session = lambda uri, fname: None
        notes = self.m.materialize(
            [
                _event(
                    self.layers,
                    {
                        "layer_id": "01MESHFAILAAAAAAAAAAAAAAAA",
                        "name": "bad mesh",
                        "layer_type": "mesh",
                        "uri": "s3://trid3nt-runs/run/sfincs_map.nc",
                    },
                )
            ]
        )
        self.assertEqual(_MeshFake.instances, [])
        self.assertTrue(any("could not stage" in n and "skipped" in n for n in notes), notes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
