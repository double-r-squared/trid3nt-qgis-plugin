#!/usr/bin/env python3
"""Tests for the COMBINED SFINCS quadtree BUILD+SOLVE worker entrypoint.

Two surfaces:

  * PURE-PYTHON unit tests (NO cht_sfincs import) — build-spec validation, time
    parsing, the two caveat fixes (snapwave knob mapping = CAVEAT 2; time-column
    normalizer = CAVEAT 1), manifest composition, object-URI parsing, the
    cell-budget estimator + cap (synthetic, no rasters), the SFINCS-binary
    invocation + output expansion, and build_deck's dispatch with cht mocked out.
    These run anywhere (the agent CI venv, this box's system python) WITHOUT the
    GPL library.

  * An OPT-IN integration test (run_full_deck_build) that authors a real
    quadtree+SnapWave deck via cht_sfincs against the spike venv where the GPL
    library is installed (auto-refinement from a synthetic sloping-beach raster +
    building obstacles), with all object-store I/O mocked to local files. The
    SOLVE half is NOT exercised here (no SFINCS binary on the dev box) — it is
    unit-tested via the binary-invocation tests with a fake echo binary. Skipped
    automatically when cht_sfincs is not importable.

Run pure-python set (no GPL needed):
    python services/workers/sfincs_deckbuilder/test_entrypoint.py

Run including the cht integration test (against the spike venv):
    services/workers/sfincs_quadtree_spike/.venv/bin/python \
        services/workers/sfincs_deckbuilder/test_entrypoint.py --with-cht
"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent

# Import the entrypoint module by path so the test runs regardless of how the
# package is on sys.path (CI venv vs spike venv vs in-container).
_spec = importlib.util.spec_from_file_location(
    "sfincs_deckbuilder_entrypoint", HERE / "entrypoint.py"
)
ep = importlib.util.module_from_spec(_spec)  # type: ignore
_spec.loader.exec_module(ep)  # type: ignore


def _cht_available() -> bool:
    return importlib.util.find_spec("cht_sfincs") is not None


def _geo_stack_available() -> bool:
    """True when the heavy geo stack (numpy/xarray/xugrid) is importable.

    The dispatch test mocks cht but patches real ``xugrid.UgridDataArray`` /
    ``xarray.DataArray`` attributes, so it needs the geo stack present even
    though it never touches the GPL library.
    """
    return all(
        importlib.util.find_spec(m) is not None
        for m in ("numpy", "xarray", "xugrid")
    )


def _rasterio_available() -> bool:
    """True when numpy + rasterio are importable (the bathymetry-sampler test)."""
    return all(
        importlib.util.find_spec(m) is not None for m in ("numpy", "rasterio")
    )


# Pre-import the heavy scientific stack ONCE at module load (when present) so it
# stays cached in sys.modules for the whole process. numpy/scipy C-extensions
# cannot be initialised twice per process, so a later mock.patch.dict that
# *removes* a real module would make a subsequent re-import crash. By importing
# them here (real, cached) we ensure the dispatch test's mocks only ever patch
# attributes, never evict the real C-extension modules. No-op if cht (and hence
# the geo stack) is not installed.
if _cht_available():  # pragma: no cover - import side effect only
    import numpy  # noqa: F401
    import xarray  # noqa: F401
    import xugrid  # noqa: F401


def _valid_spec(deck_dir_uri="s3://b/cache/sfincs_setup/x/deck/",
                manifest_uri="s3://b/cache/sfincs_setup/x/manifest.json") -> dict:
    return {
        "run_id": "01HRUN",
        "aoi": {"bbox": [-85.5, 29.9, -85.3, 30.1], "target_epsg": 32616},
        "topobathy": {"cog_uri": "s3://b/topo.tif", "bathymetry_present": True},
        "grid": {
            "x0": 600000.0, "y0": 3200000.0, "nmax": 16, "mmax": 24,
            "dx": 200.0, "dy": 200.0, "rotation": 0.0,
            "refinement_polygons_uri": "s3://b/refine.fgb",
        },
        "mask": {
            "zmin": -1000.0, "zmax": 2.0,
            "open_boundary_polygon_uri": "s3://b/wl.fgb",
            "open_boundary_zmin": -1000.0, "open_boundary_zmax": 2.0,
        },
        "snapwave": {
            "mask_zmin": -1000.0, "mask_zmax": 2.0,
            "open_boundary_polygon_uri": "s3://b/wave.fgb",
            "gamma": 0.8, "dtheta": 15.0, "hmin": 0.1,
        },
        "forcing": {
            "tref": "20181010 000000",
            "tstart": "20181010 000000",
            "tstop": "20181010 020000",
            "snapwave_boundary": {
                "points": [
                    {"x": 600100.0, "y": 3201600.0,
                     "hs": 3.0, "tp": 12.0, "wd": 270.0, "ds": 20.0}
                ]
            },
        },
        "output": {"deck_dir_uri": deck_dir_uri, "manifest_uri": manifest_uri},
    }


# --------------------------------------------------------------------------- #
# Pure-python tests (no cht import)
# --------------------------------------------------------------------------- #


class ObjectUriTests(unittest.TestCase):
    def test_split_s3(self):
        self.assertEqual(
            ep._split_object_uri("s3://bucket/a/b/c.json"),
            ("s3", "bucket", "a/b/c.json"),
        )

    def test_split_gs(self):
        self.assertEqual(
            ep._split_object_uri("gs://bucket/k"), ("gs", "bucket", "k")
        )

    def test_split_rejects_bad_scheme(self):
        with self.assertRaises(ValueError):
            ep._split_object_uri("http://x/y")

    def test_split_rejects_missing_key(self):
        with self.assertRaises(ValueError):
            ep._split_object_uri("s3://bucket")

    def test_output_scheme_env(self):
        with mock.patch.dict("os.environ", {"TRID3NT_OBJECT_STORE": "s3"}):
            self.assertEqual(ep._output_scheme(), "s3")
        with mock.patch.dict("os.environ", {"TRID3NT_OBJECT_STORE": "gcs"}):
            self.assertEqual(ep._output_scheme(), "gs")


class TimeParseTests(unittest.TestCase):
    def test_sfincs_ascii(self):
        self.assertEqual(
            ep.parse_sfincs_time("20181010 000000"),
            _dt.datetime(2018, 10, 10, 0, 0, 0),
        )

    def test_iso_with_z(self):
        self.assertEqual(
            ep.parse_sfincs_time("2018-10-10T02:00:00Z"),
            _dt.datetime(2018, 10, 10, 2, 0, 0),
        )

    def test_datetime_passthrough_strips_tz(self):
        aware = _dt.datetime(2018, 10, 10, tzinfo=_dt.timezone.utc)
        self.assertIsNone(ep.parse_sfincs_time(aware).tzinfo)

    def test_bad_raises(self):
        with self.assertRaises(ep.BuildSpecError):
            ep.parse_sfincs_time("not-a-time")


class ValidateSpecTests(unittest.TestCase):
    def test_valid_roundtrip(self):
        out = ep.validate_build_spec(_valid_spec())
        self.assertEqual(out["aoi"]["target_epsg"], 32616)
        self.assertTrue(out["output"]["deck_dir_uri"].endswith("/"))
        self.assertEqual(
            out["_parsed_times"]["tref"], _dt.datetime(2018, 10, 10, 0, 0, 0)
        )

    def test_deck_dir_uri_gets_trailing_slash(self):
        spec = _valid_spec(deck_dir_uri="s3://b/deck")  # no trailing slash
        out = ep.validate_build_spec(spec)
        self.assertEqual(out["output"]["deck_dir_uri"], "s3://b/deck/")

    def test_missing_aoi_raises(self):
        spec = _valid_spec()
        del spec["aoi"]
        with self.assertRaises(ep.BuildSpecError):
            ep.validate_build_spec(spec)

    def test_missing_grid_field_raises(self):
        spec = _valid_spec()
        del spec["grid"]["dx"]
        with self.assertRaises(ep.BuildSpecError):
            ep.validate_build_spec(spec)

    def test_tstop_before_tstart_raises(self):
        spec = _valid_spec()
        spec["forcing"]["tstop"] = "20181009 000000"
        with self.assertRaises(ep.BuildSpecError):
            ep.validate_build_spec(spec)

    def test_missing_topobathy_cog_raises(self):
        spec = _valid_spec()
        del spec["topobathy"]["cog_uri"]
        with self.assertRaises(ep.BuildSpecError):
            ep.validate_build_spec(spec)


class Caveat2HerbersTests(unittest.TestCase):
    """CAVEAT 2 — snapwave_use_herbers is FORCED to 1 (infragravity run-up)."""

    def test_default_is_one(self):
        knobs = ep.snapwave_inp_overrides(_valid_spec())
        self.assertEqual(knobs["snapwave_use_herbers"], 1)

    def test_agent_stale_zero_is_overridden_to_one(self):
        # The agent composer emits snapwave.use_herbers=0 (the old, known-bad
        # setting). The worker IGNORES that bare field and forces 1.
        spec = _valid_spec()
        spec["snapwave"]["use_herbers"] = 0
        knobs = ep.snapwave_inp_overrides(spec)
        self.assertEqual(knobs["snapwave_use_herbers"], 1)

    def test_deliberate_escape_hatch_turns_off(self):
        # Only the explicit force_no_herbers flag turns Herbers back off.
        spec = _valid_spec()
        spec["snapwave"]["force_no_herbers"] = True
        knobs = ep.snapwave_inp_overrides(spec)
        self.assertEqual(knobs["snapwave_use_herbers"], 0)

    def test_other_knobs_carry_proven_defaults(self):
        knobs = ep.snapwave_inp_overrides(_valid_spec())
        self.assertEqual(knobs["snapwave_gamma"], 0.8)
        self.assertEqual(knobs["snapwave_dtheta"], 15.0)
        self.assertEqual(knobs["snapwave_hmin"], 0.1)
        self.assertEqual(knobs["snapwave_igwaves"], 1)


class DtwaveCadenceTests(unittest.TestCase):
    """DEFECT 2 - the SnapWave coupling cadence ``dtwave`` knob threading."""

    def test_dtwave_absent_when_not_pinned(self):
        # When the agent does not pin dtwave, snapwave_inp_overrides omits it
        # (build_deck owns the default = output cadence). A bare _valid_spec has
        # no snapwave.dtwave.
        knobs = ep.snapwave_inp_overrides(_valid_spec())
        self.assertNotIn("dtwave", knobs)

    def test_dtwave_threaded_when_pinned(self):
        spec = _valid_spec()
        spec["snapwave"]["dtwave"] = 300.0
        knobs = ep.snapwave_inp_overrides(spec)
        self.assertEqual(knobs["dtwave"], 300.0)
        # It is a BARE SFINCS var, NOT a snapwave_* knob.
        self.assertNotIn("snapwave_dtwave", knobs)


class SnapwaveShallowBoundaryWarningTests(unittest.TestCase):
    """HONESTY GATE - detect the SnapWave shallow-boundary stdout warning."""

    def test_marker_present(self):
        with tempfile.TemporaryDirectory() as d:
            deck = Path(d)
            (deck / "sfincs.stdout").write_text(
                "ERROR SnapWave - depth at boundary input point 661738.9 "
                "3314426.2 dropped below 5 m: 1.79 ... Please specify input in "
                "deeper water.\n"
            )
            self.assertTrue(ep._snapwave_shallow_boundary_warning(deck))

    def test_marker_absent(self):
        with tempfile.TemporaryDirectory() as d:
            deck = Path(d)
            (deck / "sfincs.stdout").write_text("Normal end of run\n")
            self.assertFalse(ep._snapwave_shallow_boundary_warning(deck))

    def test_missing_stdout_is_false(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(ep._snapwave_shallow_boundary_warning(Path(d)))


@unittest.skipUnless(
    importlib.util.find_spec("numpy") is not None
    and importlib.util.find_spec("geopandas") is not None
    and importlib.util.find_spec("shapely") is not None
    and importlib.util.find_spec("pyproj") is not None,
    "geo stack (numpy/geopandas/shapely/pyproj) required",
)
class DepthAwareSeawardEdgeTests(unittest.TestCase):
    """DEFECT 1 (worker) - derive_seaward_open_boundary_polygon picks the DEEPEST
    domain edge, not the nearest-to-incident-points edge."""

    def _fake_sf(self, xc, yc, swmask, zb_unused=None, dx=1.0):
        import numpy as np

        class _GridData:
            def __init__(self, swm):
                self.attrs = {"dx": dx}
                self._d = {
                    "snapwave_mask": mock.MagicMock(
                        values=np.asarray(swm, dtype=float)
                    )
                }

            def __getitem__(self, k):
                return self._d[k]

        fake_sf = mock.MagicMock()
        fake_sf.grid.face_coordinates.return_value = (
            np.asarray(xc, dtype=float), np.asarray(yc, dtype=float)
        )
        fake_sf.grid.data = _GridData(swmask)
        fake_sf.grid.dx = dx
        return fake_sf

    def _grid_5x5(self):
        # 5x5 face centres at integer coords 0..4 (25 faces, all active).
        xc, yc = [], []
        for j in range(5):  # y
            for i in range(5):  # x
                xc.append(float(i))
                yc.append(float(j))
        swmask = [1] * 25
        return xc, yc, swmask

    def test_picks_deepest_south_edge(self):
        import numpy as np

        xc, yc, swmask = self._grid_5x5()
        # zb positive-up: deep (-12) along the SOUTH edge (y==0), shallow / land
        # everywhere else. The deepest edge mean is SOUTH.
        zb = []
        for j in range(5):
            for _i in range(5):
                zb.append(-12.0 if j == 0 else (1.0 if j == 4 else -0.5))
        # Incident points sit to the EAST (would mislead the old nearest-point
        # heuristic), but depth-awareness must still pick SOUTH.
        points = [{"x": 10.0, "y": 2.0}]
        poly = ep.derive_seaward_open_boundary_polygon(
            self._fake_sf(xc, yc, swmask),
            points, 32616, zb=np.asarray(zb),
        )
        self.assertIsNotNone(poly)
        # The south-edge band polygon's y-extent hugs the southern face (y ~ 0),
        # not the north (y ~ 4): its max-y is well below the domain's max-y (4).
        miny, maxy = poly.total_bounds[1], poly.total_bounds[3]
        self.assertLess(maxy, 3.0)  # confined to the southern band
        self.assertLessEqual(miny, 0.0)

    def test_falls_back_without_zb(self):
        # No zb -> the nearest-incident-point heuristic (east point -> east edge).
        xc, yc, swmask = self._grid_5x5()
        points = [{"x": 10.0, "y": 2.0}]  # far east
        poly = ep.derive_seaward_open_boundary_polygon(
            self._fake_sf(xc, yc, swmask), points, 32616, zb=None
        )
        self.assertIsNotNone(poly)
        minx = poly.total_bounds[0]
        # East-edge band: its min-x is well to the east (near x ~ 4), not x ~ 0.
        self.assertGreater(minx, 1.0)


class AgentSpecShapeTests(unittest.TestCase):
    """The worker tolerates the agent composer's real build_spec shape."""

    def _agent_spec(self) -> dict:
        # Mirrors model_flood_scenario.py _compose_and_upload_deckbuild_spec.
        return {
            "schema_version": "v1",
            "deck_id": "01HDECK",
            "aoi": {"bbox": [-85.5, 29.9, -85.3, 30.1], "target_epsg": None},
            "topobathy": {"cog_uri": "s3://b/topo.tif", "bathymetry_present": True},
            "grid": {
                "grid_resolution_m": 100.0,
                "x0": 600000.0, "y0": 3200000.0, "nmax": 16, "mmax": 24,
                "dx": 200.0, "dy": 200.0, "rotation": 0.0, "epsg": 32616,
            },
            "mask": {"zmin": None, "zmax": None},
            "snapwave": {
                "use_herbers": 0, "time_column_owned_by_cht": True,
                "gamma": 0.8, "dtheta": 15.0, "hmin": 0.1, "igwaves": 1,
            },
            "forcing": {
                "tref": "20181010 000000", "tstart": "20181010 000000",
                "tstop": "20181010 020000", "duration_hours": 2.0,
                "surge_forcing": {
                    "waterlevel": {"timeseries_uri": "s3://b/bzs.csv",
                                   "locations_uri": "s3://b/bnd.fgb"},
                    "discharge": {"timeseries_uri": "s3://b/dis.csv",
                                  "locations_uri": "s3://b/src.fgb"},
                },
            },
            "output": {"deck_dir_uri": "s3://b/cache/x/deck/",
                       "manifest_uri": "s3://b/cache/x/manifest.json"},
        }

    def test_validate_defaults_null_epsg(self):
        out = ep.validate_build_spec(self._agent_spec())
        self.assertEqual(out["aoi"]["target_epsg"], ep.DEFAULT_TARGET_EPSG)

    def test_validate_missing_grid_geometry_raises(self):
        spec = self._agent_spec()
        spec["grid"] = {"grid_resolution_m": 100.0}  # no x0/y0/...
        with self.assertRaises(ep.BuildSpecError):
            ep.validate_build_spec(spec)

    def test_caveat2_forced_on_agent_spec(self):
        knobs = ep.snapwave_inp_overrides(self._agent_spec())
        self.assertEqual(knobs["snapwave_use_herbers"], 1)

    def test_resolve_nested_surge_forcing(self):
        blocks = ep.resolve_forcing_blocks(self._agent_spec())
        self.assertEqual(blocks["waterlevel"]["timeseries_uri"], "s3://b/bzs.csv")
        self.assertEqual(blocks["discharge"]["locations_uri"], "s3://b/src.fgb")
        self.assertIsNone(blocks["snapwave_boundary"])

    def test_resolve_direct_forcing_shape(self):
        # A direct (non-nested) forcing.* shape also resolves.
        spec = self._agent_spec()
        spec["forcing"]["waterlevel"] = {"timeseries_uri": "s3://d/ts",
                                         "locations_uri": "s3://d/loc"}
        blocks = ep.resolve_forcing_blocks(spec)
        self.assertEqual(blocks["waterlevel"]["timeseries_uri"], "s3://d/ts")


class Caveat1TimeColumnTests(unittest.TestCase):
    """CAVEAT 1 — SnapWave time columns must be tref-relative (0-anchored)."""

    def test_rebases_epoch_scale_to_zero(self):
        with tempfile.TemporaryDirectory() as d:
            deck = Path(d)
            # Reproduce the spike's flawed epoch-scale bhs (242524800, +7200).
            (deck / "snapwave.bhs").write_text(
                "242524800.000  3.000\n242532000.000  3.000\n"
            )
            rewritten = ep.normalize_snapwave_time_columns(
                deck, _dt.datetime(2018, 10, 10)
            )
            self.assertIn("snapwave.bhs", rewritten)
            lines = (deck / "snapwave.bhs").read_text().splitlines()
            self.assertAlmostEqual(float(lines[0].split()[0]), 0.0)
            self.assertAlmostEqual(float(lines[1].split()[0]), 7200.0)
            # value column preserved
            self.assertAlmostEqual(float(lines[0].split()[1]), 3.0)

    def test_already_tref_relative_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            deck = Path(d)
            (deck / "snapwave.btp").write_text("0.000  12.000\n7200.000  12.000\n")
            rewritten = ep.normalize_snapwave_time_columns(
                deck, _dt.datetime(2018, 10, 10)
            )
            self.assertEqual(rewritten, [])
            lines = (deck / "snapwave.btp").read_text().splitlines()
            self.assertAlmostEqual(float(lines[0].split()[0]), 0.0)

    def test_missing_files_noop(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                ep.normalize_snapwave_time_columns(Path(d), _dt.datetime(2018, 1, 1)),
                [],
            )


class ManifestTests(unittest.TestCase):
    def test_compose_manifest_shape(self):
        with tempfile.TemporaryDirectory() as d:
            deck = Path(d)
            (deck / "sfincs.nc").write_bytes(b"\x00")
            (deck / "sfincs.inp").write_text("x")
            (deck / "snapwave.bhs").write_text("0.0 3.0\n")
            m = ep.compose_manifest(deck, "s3://b/cache/x/deck/")
            self.assertEqual(m["sfincs_args"], [])
            self.assertEqual(
                m["outputs"],
                ["sfincs_map.nc", "*.nc", "*.tif", "mesh.geojson"],
            )
            uris = {i["gs_uri"]: i["dest"] for i in m["inputs"]}
            self.assertEqual(
                uris["s3://b/cache/x/deck/sfincs.nc"], "sfincs.nc"
            )
            self.assertEqual(
                uris["s3://b/cache/x/deck/sfincs.inp"], "sfincs.inp"
            )
            # legacy field name "gs_uri" retained for the solve worker
            self.assertTrue(all("gs_uri" in i and "dest" in i for i in m["inputs"]))


# --------------------------------------------------------------------------- #
# Combined-worker NEW surfaces — all pure-python (no cht, no rasters).
# --------------------------------------------------------------------------- #


class CellBudgetTests(unittest.TestCase):
    """estimate_quadtree_cells + apply_cell_budget (synthetic, no cht)."""

    def test_estimate_base_only(self):
        # No refinement => exactly the base grid count.
        self.assertEqual(ep.estimate_quadtree_cells(16, 24, {}), 16 * 24)

    def test_estimate_full_level1_quadruples_covered(self):
        # 100% of the base grid refined ONE level => every base cell -> 4 cells.
        base = 10 * 10
        est = ep.estimate_quadtree_cells(10, 10, {1: 1.0})
        self.assertEqual(est, base * 4)

    def test_estimate_partial_two_levels(self):
        # 50% covered to level 1, 25% covered to level 2 (subset of the 50%).
        # base*(1-0.5) + base*(0.5-0.25)*4 + base*(0.25)*16
        base = 100
        est = ep.estimate_quadtree_cells(10, 10, {1: 0.5, 2: 0.25})
        expected = base * 0.5 + base * 0.25 * 4 + base * 0.25 * 16
        self.assertEqual(est, int(round(expected)))

    def test_budget_cap_drops_finest_level(self):
        # Level 2 over budget, level 1 fits => cap to level 1 with a note.
        # base=10000, full level-2 coverage => 10000*16 = 160k > 50k budget;
        # level-1 coverage => 10000*4 = 40k <= 50k.
        allowed, notes = ep.apply_cell_budget(
            100, 100, {1: 1.0, 2: 1.0}, max_cells=50_000
        )
        self.assertEqual(allowed, 1)
        self.assertTrue(any("reduced max refinement level" in n for n in notes))

    def test_budget_cap_keeps_all_when_under(self):
        allowed, notes = ep.apply_cell_budget(
            10, 10, {1: 0.5, 2: 0.25}, max_cells=10_000
        )
        self.assertEqual(allowed, 2)
        self.assertEqual(notes, [])

    def test_budget_cap_base_over_budget(self):
        # Even the base grid exceeds the budget => level 0, loud note.
        allowed, notes = ep.apply_cell_budget(
            1000, 1000, {1: 1.0}, max_cells=1000
        )
        self.assertEqual(allowed, 0)
        self.assertTrue(any("base grid" in n for n in notes))


class ValidateCombinedSpecTests(unittest.TestCase):
    """The combined spec's NEW optional fields validate + default correctly."""

    def test_defaults_max_cells_and_levels(self):
        out = ep.validate_build_spec(_valid_spec())
        self.assertEqual(out["grid"]["max_cells"], ep.DEFAULT_MAX_CELLS)
        self.assertEqual(out["grid"]["refinement_levels"], 2)

    def test_honours_explicit_budget_and_levels(self):
        spec = _valid_spec()
        spec["grid"]["max_cells"] = 500_000
        spec["grid"]["refinement_levels"] = 3
        out = ep.validate_build_spec(spec)
        self.assertEqual(out["grid"]["max_cells"], 500_000)
        self.assertEqual(out["grid"]["refinement_levels"], 3)

    def test_rejects_nonpositive_budget(self):
        spec = _valid_spec()
        spec["grid"]["max_cells"] = 0
        with self.assertRaises(ep.BuildSpecError):
            ep.validate_build_spec(spec)

    def test_rejects_bad_building_mode(self):
        spec = _valid_spec()
        spec["buildings"] = {"footprints_uri": "s3://b/bld.fgb", "mode": "bogus"}
        with self.assertRaises(ep.BuildSpecError):
            ep.validate_build_spec(spec)

    def test_accepts_building_modes(self):
        for mode in ("thin_dams", "raise_subgrid", "exclude"):
            spec = _valid_spec()
            spec["buildings"] = {"footprints_uri": "s3://b/bld.fgb", "mode": mode}
            out = ep.validate_build_spec(spec)
            self.assertEqual(out["buildings"]["mode"], mode)


class SfincsBinaryInvocationTests(unittest.TestCase):
    """_run_sfincs + _expand_outputs — the SOLVE half, with a FAKE binary.

    No real SFINCS binary on the dev box; we point TRID3NT_SFINCS_BIN at a tiny
    shell script that writes a sentinel sfincs_map.nc into CWD and exits 0/N,
    proving the in-process solve invocation + output expansion are correct.
    """

    def _fake_bin(self, tmp: Path, rc: int, write_map: bool) -> Path:
        script = tmp / "fake_sfincs.sh"
        body = "#!/bin/sh\n"
        body += 'echo "fake sfincs running in $(pwd)"\n'
        if write_map:
            body += 'printf "NC" > sfincs_map.nc\n'
        body += f"exit {rc}\n"
        script.write_text(body)
        script.chmod(0o755)
        return script

    def test_run_and_expand_success(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            deck = tmp / "deck"
            deck.mkdir()
            (deck / "sfincs.inp").write_text("x")
            fake = self._fake_bin(tmp, rc=0, write_map=True)
            with mock.patch.object(ep, "SFINCS_BIN", str(fake)):
                rc, out_p, err_p = ep._run_sfincs([], deck)
            self.assertEqual(rc, 0)
            self.assertTrue(out_p.exists() and err_p.exists())
            self.assertEqual(out_p.name, "sfincs.stdout")
            outs = ep._expand_outputs(list(ep.SOLVE_OUTPUT_PATTERNS), deck)
            names = {p.name for p in outs}
            self.assertIn("sfincs_map.nc", names)

    def test_run_nonzero_exit_propagates(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            deck = tmp / "deck"
            deck.mkdir()
            fake = self._fake_bin(tmp, rc=7, write_map=False)
            with mock.patch.object(ep, "SFINCS_BIN", str(fake)):
                rc, _out, _err = ep._run_sfincs([], deck)
            self.assertEqual(rc, 7)

    def test_expand_dedupes_and_sorts(self):
        with tempfile.TemporaryDirectory() as d:
            deck = Path(d)
            (deck / "sfincs_map.nc").write_bytes(b"\x00")
            (deck / "extra.nc").write_bytes(b"\x00")
            (deck / "derived.tif").write_bytes(b"\x00")
            # sfincs_map.nc matches BOTH "sfincs_map.nc" and "*.nc" — must dedupe.
            outs = ep._expand_outputs(["sfincs_map.nc", "*.nc", "*.tif"], deck)
            names = sorted(p.name for p in outs)
            self.assertEqual(names, ["derived.tif", "extra.nc", "sfincs_map.nc"])


class CompletionUnionTests(unittest.TestCase):
    """completion.json is a UNION of the deck + solve schemas the agent polls."""

    def test_completion_payload_has_solve_and_deck_keys(self):
        captured = {}

        def fake_put_json(payload, uri):
            captured["payload"] = payload
            captured["uri"] = uri
            return uri

        with mock.patch.object(ep, "_put_json", side_effect=fake_put_json), \
                mock.patch.dict("os.environ", {"TRID3NT_OBJECT_STORE": "s3"}):
            ep._write_completion(
                run_id="R1",
                status="ok",
                exit_code=0,
                output_uris=["s3://b/R1/sfincs_map.nc", "s3://b/R1/manifest.json"],
                stdout_uri="s3://b/R1/sfincs.stdout",
                stderr_uri="s3://b/R1/sfincs.stderr",
                deck_provenance={"nr_cells": 1234, "nr_levels": 3,
                                 "manifest_uri": "s3://b/R1/manifest.json",
                                 "budget_notes": []},
                started_at="2026-06-18T00:00:00Z",
                error=None,
            )
        p = captured["payload"]
        # solve-schema keys (wait_for_completion reads these).
        for k in ("run_id", "status", "exit_code", "output_uris",
                  "sfincs_stdout_uri", "sfincs_stderr_uri", "started_at",
                  "finished_at", "error"):
            self.assertIn(k, p, f"missing solve key {k}")
        # deck-provenance union block.
        self.assertEqual(p["deck"]["nr_cells"], 1234)
        self.assertEqual(p["deck"]["nr_levels"], 3)
        self.assertTrue(captured["uri"].endswith("/R1/completion.json"))
        self.assertTrue(captured["uri"].startswith("s3://"))


def _serialization_stack_available() -> bool:
    """True when numpy + shapely + geopandas are importable.

    The mesh SERIALIZATION path (build_mesh_geodataframe) is pure
    shapely/geopandas — NO cht_sfincs / xugrid — so it is testable in the agent
    CI venv that carries the geo stack but NOT the GPL library.
    """
    return all(
        importlib.util.find_spec(m) is not None
        for m in ("numpy", "shapely", "geopandas")
    )


@unittest.skipUnless(
    _serialization_stack_available(),
    "serialization stack (numpy/shapely/geopandas) not importable",
)
class MeshGeojsonSerializationTests(unittest.TestCase):
    """Unit tests for the mesh SERIALIZATION path (no cht_sfincs / xugrid).

    Exercises build_mesh_geodataframe + emit_quadtree_mesh_geojson with MOCK
    shapely polygons + fake level / size / mask arrays. Asserts: EPSG:4326
    output, per-cell properties present, the active-cell filter, the cap +
    deterministic decimation triggers + is recorded, and that an empty input
    yields a valid empty FeatureCollection (no crash).
    """

    @staticmethod
    def _square(i: int):
        """A unique 1x1 square polygon at x-offset i (UTM-ish meters)."""
        from shapely.geometry import Polygon

        x = 600000.0 + i * 10.0
        y = 3200000.0
        return Polygon([(x, y), (x + 10.0, y), (x + 10.0, y + 10.0),
                        (x, y + 10.0)])

    def _mock_faces(self, n: int):
        import numpy as np

        polys = np.array([self._square(i) for i in range(n)], dtype=object)
        # level 0..2 cycling; size_m = base_dx / 2**level with base_dx=100.
        level = np.array([i % 3 for i in range(n)], dtype=int)
        size_m = 100.0 / np.power(2.0, level.astype(float))
        z = np.linspace(-5.0, 5.0, n)
        return polys, level, size_m, z

    def test_basic_4326_with_properties(self):
        polys, level, size_m, z = self._mock_faces(6)
        # mask: 1 (active), 1, 0 (inactive), 2 (boundary), 1, 1 -> 4 active.
        import numpy as np
        mask = np.array([1, 1, 0, 2, 1, 1], dtype=int)
        swmask = np.array([1, 0, 0, 2, 1, 0], dtype=int)

        gdf, info = ep.build_mesh_geodataframe(
            polys, level, size_m, 32616,
            z=z, mask=mask, snapwave_mask=swmask, max_features=20000,
        )

        # EPSG:4326 output.
        self.assertEqual(int(gdf.crs.to_epsg()), 4326)
        # active-cell filter: 4 active (mask==1), all kept (under cap).
        self.assertEqual(info["n_total"], 6)
        self.assertEqual(info["n_active"], 4)
        self.assertEqual(info["n_features"], 4)
        self.assertFalse(info["decimated"])
        self.assertEqual(info["stride"], 1)
        self.assertEqual(len(gdf), 4)
        # per-cell properties present.
        for col in ("level", "size_m", "z", "mask", "snapwave_mask"):
            self.assertIn(col, gdf.columns)
        # only active cells survived.
        self.assertTrue(all(int(v) == 1 for v in gdf["mask"]))
        # size_m matches base_dx / 2**level.
        for lvl, sz in zip(gdf["level"], gdf["size_m"]):
            self.assertAlmostEqual(float(sz), 100.0 / (2 ** int(lvl)), places=6)
        # reprojected coords are lon/lat-ish (UTM 16N near -85 lon, 28 lat).
        minx, miny, maxx, maxy = gdf.total_bounds
        self.assertTrue(-90.0 < minx < -80.0)
        self.assertTrue(25.0 < miny < 32.0)

    def test_no_mask_keeps_all(self):
        polys, level, size_m, _ = self._mock_faces(5)
        gdf, info = ep.build_mesh_geodataframe(
            polys, level, size_m, 32616, mask=None, max_features=20000,
        )
        self.assertEqual(info["n_active"], 5)
        self.assertEqual(info["n_features"], 5)
        self.assertEqual(len(gdf), 5)
        self.assertEqual(int(gdf.crs.to_epsg()), 4326)

    def test_cap_triggers_deterministic_decimation(self):
        import numpy as np

        n = 250
        polys, level, size_m, z = self._mock_faces(n)
        mask = np.ones(n, dtype=int)  # all active

        gdf, info = ep.build_mesh_geodataframe(
            polys, level, size_m, 32616,
            z=z, mask=mask, max_features=100,
        )
        # decimation triggers and is recorded; stride = ceil(250/100) = 3.
        self.assertTrue(info["decimated"])
        self.assertEqual(info["stride"], 3)
        self.assertEqual(info["n_active"], 250)
        # ceil-stride keeps the feature count at or under the cap.
        self.assertLessEqual(info["n_features"], 100)
        self.assertEqual(len(gdf), info["n_features"])
        # deterministic: same inputs -> same count + same first level value.
        gdf2, info2 = ep.build_mesh_geodataframe(
            polys, level, size_m, 32616,
            z=z, mask=mask, max_features=100,
        )
        self.assertEqual(info, info2)
        self.assertEqual(list(gdf["level"]), list(gdf2["level"]))

    def test_empty_input_yields_empty_collection(self):
        import numpy as np

        gdf, info = ep.build_mesh_geodataframe(
            np.array([], dtype=object),
            np.array([], dtype=int),
            np.array([], dtype=float),
            32616, mask=np.array([], dtype=int), max_features=20000,
        )
        self.assertEqual(info["n_total"], 0)
        self.assertEqual(info["n_active"], 0)
        self.assertEqual(info["n_features"], 0)
        self.assertEqual(len(gdf), 0)

    def test_all_inactive_yields_zero_features(self):
        import numpy as np

        polys, level, size_m, z = self._mock_faces(4)
        mask = np.array([0, 2, 0, 2], dtype=int)  # no active cells
        gdf, info = ep.build_mesh_geodataframe(
            polys, level, size_m, 32616, z=z, mask=mask, max_features=20000,
        )
        self.assertEqual(info["n_active"], 0)
        self.assertEqual(info["n_features"], 0)
        self.assertEqual(len(gdf), 0)

    def test_emit_writes_geojson_file_and_records_provenance(self):
        """End-to-end SERIALIZATION via emit_quadtree_mesh_geojson with a fake sf.

        extract_quadtree_faces is patched to return mock polygons (so no
        cht_sfincs / xugrid is touched); the file is actually written + parsed.
        """
        import json as _json
        import numpy as np

        n = 8
        polys, level, size_m, z = self._mock_faces(n)
        mask = np.array([1, 1, 0, 2, 1, 1, 0, 1], dtype=int)  # 5 active
        swmask = np.array([1, 0, 0, 2, 1, 1, 0, 0], dtype=int)
        fake = {
            "polygons": polys, "level": level, "size_m": size_m,
            "z": z, "mask": mask, "snapwave_mask": swmask,
            "source_epsg": 32616,
        }

        with tempfile.TemporaryDirectory() as d:
            deck = Path(d)
            provenance: dict = {}
            with mock.patch.object(ep, "extract_quadtree_faces",
                                   return_value=fake):
                out = ep.emit_quadtree_mesh_geojson(
                    object(), deck, 32616, provenance
                )
            self.assertIsNotNone(out)
            mesh_path = deck / "mesh.geojson"
            self.assertTrue(mesh_path.exists())
            fc = _json.loads(mesh_path.read_text())
            self.assertEqual(fc["type"], "FeatureCollection")
            self.assertEqual(len(fc["features"]), 5)  # active-only
            # every feature carries the per-cell props in EPSG:4326.
            props = fc["features"][0]["properties"]
            for k in ("level", "size_m", "mask"):
                self.assertIn(k, props)
            # provenance carries the mesh block (deck block -> completion.json).
            self.assertIn("mesh", provenance)
            self.assertEqual(provenance["mesh"]["crs"], "EPSG:4326")
            self.assertEqual(provenance["mesh"]["n_features"], 5)
            self.assertEqual(provenance["mesh"]["n_active_cells"], 5)
            self.assertEqual(provenance["mesh"]["n_total_cells"], 8)
            self.assertFalse(provenance["mesh"]["decimated"])
            self.assertEqual(provenance["mesh"]["max_features"],
                             ep.MESH_GEOJSON_MAX_FEATURES)

    def test_emit_never_raises_on_extract_failure(self):
        """Best-effort contract: extractor blowing up must NOT raise."""
        with tempfile.TemporaryDirectory() as d:
            deck = Path(d)
            provenance: dict = {}
            with mock.patch.object(
                ep, "extract_quadtree_faces",
                side_effect=RuntimeError("xugrid exploded"),
            ):
                out = ep.emit_quadtree_mesh_geojson(
                    object(), deck, 32616, provenance
                )
            # returns None, writes no file, records the error -> deck untouched.
            self.assertIsNone(out)
            self.assertFalse((deck / "mesh.geojson").exists())
            self.assertIn("mesh", provenance)
            self.assertIn("error", provenance["mesh"])

    def test_emit_empty_writes_valid_empty_collection(self):
        """All-inactive -> emit writes a valid empty FeatureCollection, no crash."""
        import json as _json
        import numpy as np

        polys, level, size_m, z = self._mock_faces(3)
        mask = np.array([0, 0, 2], dtype=int)  # none active
        fake = {
            "polygons": polys, "level": level, "size_m": size_m,
            "z": z, "mask": mask, "snapwave_mask": None, "source_epsg": 32616,
        }
        with tempfile.TemporaryDirectory() as d:
            deck = Path(d)
            provenance: dict = {}
            with mock.patch.object(ep, "extract_quadtree_faces",
                                   return_value=fake):
                out = ep.emit_quadtree_mesh_geojson(
                    object(), deck, 32616, provenance
                )
            self.assertIsNotNone(out)
            fc = _json.loads((deck / "mesh.geojson").read_text())
            self.assertEqual(fc["type"], "FeatureCollection")
            self.assertEqual(fc["features"], [])
            self.assertTrue(provenance["mesh"]["empty"])
            self.assertEqual(provenance["mesh"]["n_features"], 0)


@unittest.skipUnless(
    _rasterio_available(),
    "numpy/rasterio not importable (bathymetry-sampler test)",
)
class BathymetrySamplerNodataTests(unittest.TestCase):
    """The 9999-nodata leak fix: ``_sample_topobathy`` MUST mask declared
    nodata AND unflagged fill sentinels (9999 / -9999 / 1e20 / |z|>=9000) to
    NaN so out-of-coverage cells become INACTIVE, never a +9999 m wall.

    Regression for the live Mexico-Beach bug: the quadtree worker logged
    ``bathymetry sampled: z range -33.66 .. 9999.00 m`` because off-CUDEM
    offshore fill was sampled as a real +9999 m elevation.
    """

    def _write_cog(self, path, arr, *, nodata, epsg=32616):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        arr = np.asarray(arr, dtype="float32")
        h, w = arr.shape
        # 100 m pixels anchored in UTM 16N (Mexico Beach zone).
        transform = from_origin(600000.0, 3200000.0 + h * 100.0, 100.0, 100.0)
        with rasterio.open(
            path, "w", driver="GTiff", height=h, width=w, count=1,
            dtype="float32", crs=f"EPSG:{epsg}", transform=transform,
            nodata=nodata,
        ) as dst:
            dst.write(arr, 1)

    def _pixel_centres(self, path):
        """Return (xc, yc) lists at each pixel centre of the test COG."""
        import numpy as np
        import rasterio

        with rasterio.open(path) as ds:
            rows, cols = np.mgrid[0:ds.height, 0:ds.width]
            xs, ys = rasterio.transform.xy(
                ds.transform, rows.ravel(), cols.ravel()
            )
        return list(xs), list(ys)

    def test_pure_sentinel_masker(self):
        """The pure-numpy helper masks declared nodata + all big sentinels."""
        import numpy as np

        z = np.array(
            [-33.66, -5.0, 0.0, 4.2, 9999.0, -9999.0, 1e20, -1e20, np.nan],
            dtype="float32",
        )
        # Declared band nodata = -9999.0 (one of the values above).
        out = ep._mask_topobathy_sentinels(z, -9999.0)
        # Real coastal elevations survive.
        self.assertAlmostEqual(float(out[0]), -33.66, places=2)
        self.assertAlmostEqual(float(out[1]), -5.0, places=2)
        self.assertAlmostEqual(float(out[2]), 0.0, places=2)
        self.assertAlmostEqual(float(out[3]), 4.2, places=2)
        # Every sentinel / non-finite -> NaN (declared nodata, +9999, +-1e20, NaN).
        for i in (4, 5, 6, 7, 8):
            self.assertTrue(np.isnan(out[i]), f"index {i} should be NaN: {out[i]}")

    def test_sampler_masks_9999_patch_with_no_declared_nodata(self):
        """The exact live failure: a 9999 fill patch with the COG nodata flag
        UNSET (None) must still be masked to NaN, not sampled as +9999 m."""
        import numpy as np

        with tempfile.TemporaryDirectory() as d:
            cog = Path(d) / "topobathy.tif"
            # 4x4: a real sloping coastal band (-30..+5 m) with a 2x2 offshore
            # 9999 fill patch in the top-left and nodata flag deliberately UNSET.
            arr = np.array([
                [9999.0, 9999.0, -10.0, -5.0],
                [9999.0, 9999.0, -8.0, -3.0],
                [-30.0, -20.0, -2.0, 1.0],
                [-25.0, -15.0, 0.0, 5.0],
            ], dtype="float32")
            self._write_cog(cog, arr, nodata=None)
            xc, yc = self._pixel_centres(cog)
            z = ep._sample_topobathy(cog, xc, yc, 32616)
            z = np.asarray(z, dtype="float32")
            # The 4 fill cells (the 2x2 top-left patch) are NaN -> inactive.
            self.assertEqual(int(np.isnan(z).sum()), 4)
            # No +9999 wall survives.
            self.assertFalse((z >= 9000.0).any(), "9999 fill leaked into z")
            # The real coastal band survives intact.
            finite = z[np.isfinite(z)]
            self.assertEqual(finite.size, 12)
            self.assertAlmostEqual(float(np.nanmin(z)), -30.0, places=2)
            self.assertAlmostEqual(float(np.nanmax(z)), 5.0, places=2)

    def test_sampler_masks_declared_nodata(self):
        """A COG with a DECLARED nodata (e.g. -9999) masks those cells to NaN
        and leaves the real band — the corrected z-range is physical."""
        import numpy as np

        with tempfile.TemporaryDirectory() as d:
            cog = Path(d) / "topobathy.tif"
            arr = np.array([
                [-9999.0, -9999.0, -12.0],
                [-9999.0, -6.0, -2.0],
                [-30.0, -1.0, 8.0],
            ], dtype="float32")
            self._write_cog(cog, arr, nodata=-9999.0)
            xc, yc = self._pixel_centres(cog)
            z = ep._sample_topobathy(cog, xc, yc, 32616)
            z = np.asarray(z, dtype="float32")
            self.assertEqual(int(np.isnan(z).sum()), 3)
            self.assertAlmostEqual(float(np.nanmin(z)), -30.0, places=2)
            self.assertAlmostEqual(float(np.nanmax(z)), 8.0, places=2)
            self.assertFalse((np.abs(z[np.isfinite(z)]) >= 9000.0).any())


@unittest.skipUnless(
    _geo_stack_available(),
    "geo stack (numpy/xarray/xugrid) not importable",
)
class BuildDeckDispatchTests(unittest.TestCase):
    """build_deck's NON-cht orchestration verified with cht fully mocked.

    Confirms: tref/tstart/tstop set BEFORE forcing (CAVEAT 1 ordering), the
    snapwave knobs incl. use_herbers=1 land on input.variables (CAVEAT 2), the
    normalizer is invoked, and the GPL import path is exercised without the real
    library.
    """

    def test_orchestration_with_mocked_cht(self):
        spec = ep.validate_build_spec(_valid_spec())

        # Fully fake the lazy GPL/geo import surface so this test runs with NO
        # real cht / numpy / xugrid loaded (it asserts ORCHESTRATION, not numerics).
        class _Vals:
            """list-like values shim with the .sum()/comparison build_deck needs."""

            def __init__(self, data):
                self._d = list(data)

            def __eq__(self, other):
                return _Vals([1 if v == other else 0 for v in self._d])

            def __gt__(self, other):
                return _Vals([1 if v > other else 0 for v in self._d])

            def sum(self):
                return sum(self._d)

        variables = mock.MagicMock()
        fake_sf = mock.MagicMock()
        fake_sf.input.variables = variables
        grid_data = {
            "mask": mock.MagicMock(values=_Vals([1, 1, 2])),
            "snapwave_mask": mock.MagicMock(values=_Vals([1, 2, 1])),
        }
        fake_sf.grid.data.sizes = {"mesh2d_nFaces": 3}
        fake_sf.grid.data.attrs = {"nr_levels": 3}
        fake_sf.grid.data.__setitem__ = lambda *a, **k: None
        fake_sf.grid.data.__getitem__ = lambda self_, k: grid_data[k]
        fake_sf.grid.face_coordinates.return_value = (
            [1.0, 2.0, 3.0], [1.0, 2.0, 3.0]
        )
        fake_sf.path = "/tmp/does-not-matter"

        captured = {}

        def fake_sfincs_ctor(root, crs, mode):
            captured["root"] = root
            captured["crs"] = crs
            return fake_sf

        fake_cht_mod = mock.MagicMock()
        fake_cht_mod.SFINCS = fake_sfincs_ctor

        # cht_sfincs is INSERTED (it isn't a real-loaded C-extension we must
        # preserve); the real numpy/xarray/xugrid stay in sys.modules. We patch
        # only the two attributes build_deck calls on the geo stack so the
        # bathymetry assignment is a no-op without touching real C-extensions.
        import sys as _sys
        patch_xu = (
            mock.patch.object(_sys.modules["xugrid"], "UgridDataArray",
                              lambda *a, **k: object())
            if "xugrid" in _sys.modules else mock.MagicMock()
        )
        patch_xr = (
            mock.patch.object(_sys.modules["xarray"], "DataArray",
                              lambda *a, **k: object())
            if "xarray" in _sys.modules else mock.MagicMock()
        )

        with tempfile.TemporaryDirectory() as scratch:
            scratch_p = Path(scratch)
            with mock.patch.dict(
                "sys.modules", {"cht_sfincs": fake_cht_mod}
            ), patch_xu, patch_xr, \
                    mock.patch.object(ep, "_download"), \
                    mock.patch.object(ep, "_read_gdf", return_value=None), \
                    mock.patch.object(ep, "_sample_topobathy",
                                      return_value=[0.0, 0.0, 0.0]), \
                    mock.patch.object(ep, "derive_refinement_polygons",
                                      return_value=(None, {})), \
                    mock.patch.object(ep, "burn_building_obstacles",
                                      side_effect=lambda sf, spec, sc, zb, ep_: zb), \
                    mock.patch.object(ep, "normalize_snapwave_time_columns",
                                      return_value=[]) as norm:
                deck_dir, provenance = ep.build_deck(spec, scratch_p)

        # CAVEAT 2: use_herbers=1 set on input.variables.
        self.assertEqual(variables.snapwave_use_herbers, 1)
        # CAVEAT 1 ordering: tref/tstart/tstop are real datetimes.
        self.assertEqual(variables.tref, _dt.datetime(2018, 10, 10, 0, 0, 0))
        self.assertEqual(variables.tstop, _dt.datetime(2018, 10, 10, 2, 0, 0))
        # snapwave coupling on.
        self.assertTrue(variables.snapwave)
        self.assertEqual(variables.qtrfile, "sfincs.nc")
        # boundary point added from the spec.
        fake_sf.snapwave.boundary_conditions.add_point.assert_called_once()
        # write + normalizer invoked.
        fake_sf.write.assert_called_once()
        norm.assert_called_once()
        # combined worker returns (deck_dir, provenance) with the cell counts.
        self.assertEqual(provenance["nr_cells"], 3)
        self.assertEqual(provenance["nr_levels"], 3)
        self.assertEqual(captured["crs"], 32616)
        self.assertEqual(deck_dir, scratch_p / "deck")


# --------------------------------------------------------------------------- #
# Integration test (real cht_sfincs — opt-in, skipped without the GPL library)
# --------------------------------------------------------------------------- #


@unittest.skipUnless(_cht_available(), "cht_sfincs not importable (GPL image only)")
class FullDeckBuildIntegrationTests(unittest.TestCase):
    """End-to-end build_deck against real cht_sfincs with local-file I/O.

    Mirrors the proven spike's synthetic coastal AOI but drives it entirely
    through the worker's build_deck + the manifest/normalizer path, then asserts
    the deck is structurally valid AND the two caveats are fixed in the OUTPUT.
    """

    def _make_topobathy_cog(self, path: Path, target_epsg: int):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        # sloping beach: -8 m west -> +4 m east, covering the grid extent.
        nx, ny = 48, 32
        x0, y0 = 600000.0, 3200000.0
        dx = 200.0
        res = dx / 2  # finer than the grid so sampling has real values
        cols = int((24 * dx) / res)
        rows = int((16 * dx) / res)
        xs = np.linspace(0, 24 * dx, cols)
        z = (-8.0 + 12.0 * xs / (24 * dx)).astype("float32")
        arr = np.tile(z, (rows, 1)).astype("float32")
        transform = from_origin(x0, y0 + rows * res, res, res)
        with rasterio.open(
            path, "w", driver="GTiff", height=rows, width=cols, count=1,
            dtype="float32", crs=f"EPSG:{target_epsg}", transform=transform,
            nodata=float("nan"),
        ) as dst:
            dst.write(arr, 1)

    def _make_refine_polygon(self, path: Path, target_epsg: int):
        import geopandas as gpd
        from pyproj import CRS
        from shapely.geometry import Polygon

        x0, y0, dx, dy = 600000.0, 3200000.0, 200.0, 200.0
        poly = Polygon([
            (x0 + 8 * dx, y0 + 2 * dy), (x0 + 18 * dx, y0 + 2 * dy),
            (x0 + 18 * dx, y0 + 14 * dy), (x0 + 8 * dx, y0 + 14 * dy),
        ])
        gpd.GeoDataFrame(
            {"refinement_level": [2], "geometry": [poly]},
            crs=CRS.from_epsg(target_epsg),
        ).to_file(path, driver="GPKG")

    def _make_offshore_polygon(self, path: Path, target_epsg: int):
        import geopandas as gpd
        from pyproj import CRS
        from shapely.geometry import Polygon

        x0, y0, dx, dy = 600000.0, 3200000.0, 200.0, 200.0
        poly = Polygon([
            (x0 - dx, y0 - dy), (x0 + 1.0 * dx, y0 - dy),
            (x0 + 1.0 * dx, y0 + 17 * dy), (x0 - dx, y0 + 17 * dy),
        ])
        gpd.GeoDataFrame({"geometry": [poly]}, crs=CRS.from_epsg(target_epsg)).to_file(
            path, driver="GPKG"
        )

    def test_full_build(self):
        import numpy as np
        import xarray as xr

        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            target_epsg = 32616
            topo = work / "topo.tif"
            refine = work / "refine.gpkg"
            wl = work / "wl.gpkg"
            wave = work / "wave.gpkg"
            self._make_topobathy_cog(topo, target_epsg)
            self._make_refine_polygon(refine, target_epsg)
            self._make_offshore_polygon(wl, target_epsg)
            self._make_offshore_polygon(wave, target_epsg)

            spec = ep.validate_build_spec({
                "run_id": "intg",
                "aoi": {"bbox": [0, 0, 1, 1], "target_epsg": target_epsg},
                "topobathy": {"cog_uri": "s3://x/topo.tif",
                              "bathymetry_present": True},
                "grid": {
                    "x0": 600000.0, "y0": 3200000.0, "nmax": 16, "mmax": 24,
                    "dx": 200.0, "dy": 200.0, "rotation": 0.0,
                    "refinement_polygons_uri": "s3://x/refine.gpkg",
                },
                "mask": {
                    "zmin": -100.0, "zmax": 2.0,
                    "open_boundary_polygon_uri": "s3://x/wl.gpkg",
                    "open_boundary_zmin": -100.0, "open_boundary_zmax": 2.0,
                },
                "snapwave": {
                    "mask_zmin": -100.0, "mask_zmax": 2.0,
                    "open_boundary_polygon_uri": "s3://x/wave.gpkg",
                },
                "forcing": {
                    "tref": "20181010 000000",
                    "tstart": "20181010 000000",
                    "tstop": "20181010 020000",
                    "snapwave_boundary": {"points": [
                        {"x": 600100.0, "y": 3201600.0,
                         "hs": 3.0, "tp": 12.0, "wd": 270.0, "ds": 20.0}
                    ]},
                },
                "output": {"deck_dir_uri": "s3://x/deck/",
                           "manifest_uri": "s3://x/manifest.json"},
            })

            # Map the s3:// URIs in the spec to local files via a fake _download.
            uri_to_local = {
                "s3://x/topo.tif": topo,
                "s3://x/refine.gpkg": refine,
                "s3://x/wl.gpkg": wl,
                "s3://x/wave.gpkg": wave,
            }

            def fake_download(uri, dest):
                import shutil as _sh
                _sh.copy(uri_to_local[uri], dest)

            scratch = work / "scratch"
            scratch.mkdir()
            with mock.patch.object(ep, "_download", side_effect=fake_download):
                deck_dir, provenance = ep.build_deck(spec, scratch)

            # combined-worker provenance carries the build counts.
            self.assertGreater(provenance["nr_cells"], 0)
            self.assertGreaterEqual(provenance["nr_levels"], 2)
            self.assertIsInstance(provenance.get("budget_notes"), list)

            # --- deck contents present ---
            self.assertTrue((deck_dir / "sfincs.nc").exists())
            self.assertTrue((deck_dir / "sfincs.inp").exists())
            for f in ep.SNAPWAVE_TS_FILES:
                self.assertTrue((deck_dir / f).exists(), f"{f} missing")

            # --- CAVEAT 1: every snapwave time column is tref-relative (0-anchored) ---
            for f in ep.SNAPWAVE_TS_FILES:
                first = (deck_dir / f).read_text().splitlines()[0].split()[0]
                self.assertAlmostEqual(
                    float(first), 0.0, places=2,
                    msg=f"{f} first time col {first} not tref-relative (CAVEAT 1)",
                )

            # --- CAVEAT 2: sfincs.inp has snapwave_use_herbers = 1 ---
            inp = (deck_dir / "sfincs.inp").read_text()
            herbers = [ln for ln in inp.splitlines()
                       if ln.strip().startswith("snapwave_use_herbers")]
            self.assertTrue(herbers, "snapwave_use_herbers missing from sfincs.inp")
            self.assertEqual(herbers[0].split("=")[1].strip(), "1",
                             "CAVEAT 2: snapwave_use_herbers must be 1")
            self.assertIn("snapwave             = 1", inp)
            self.assertIn("qtrfile              = sfincs.nc", inp)

            # --- structural: multi-level quadtree connectivity present ---
            ds = xr.open_dataset(deck_dir / "sfincs.nc")
            try:
                self.assertIn("mesh2d_nFaces", ds.sizes)
                self.assertGreaterEqual(int(ds.attrs["nr_levels"]), 2)
                for v in ("mu1", "md1", "nu1", "nd1", "level", "mask",
                          "snapwave_mask"):
                    self.assertIn(v, ds.variables)
                level = ds["level"].values.astype(int)
                self.assertGreaterEqual(len(np.unique(level)), 2)
                sw = ds["snapwave_mask"].values.astype(int)
                self.assertGreater(int((sw == 1).sum()), 0)
                self.assertGreater(int((sw > 1).sum()), 0)
            finally:
                ds.close()

            # --- manifest composition over the real deck ---
            manifest = ep.compose_manifest(deck_dir, "s3://x/deck/")
            dests = {i["dest"] for i in manifest["inputs"]}
            self.assertIn("sfincs.nc", dests)
            self.assertIn("sfincs.inp", dests)
            self.assertEqual(manifest["outputs"],
                             ["sfincs_map.nc", "*.nc", "*.tif", "mesh.geojson"])

    def test_snapwave_boundary_derived_when_no_polygon(self):
        """REGRESSION (Mexico-Beach hm0=0): snapwave_boundary POINTS present but
        NO open_boundary_polygon_uri (the synthetic / agent path) must still yield
        a NON-EMPTY snapwave wave-boundary mask (wavebnd>0) derived from the
        seaward domain edge, so the incident wave can inject (else hm0 stays
        flat at 0). The water-level mask polygon is ALSO absent, exactly mirroring
        the live failure where the SFINCS open boundary is staged as sfincs.bnd.
        """
        import numpy as np
        import xarray as xr

        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            target_epsg = 32616
            topo = work / "topo.tif"
            refine = work / "refine.gpkg"
            self._make_topobathy_cog(topo, target_epsg)
            self._make_refine_polygon(refine, target_epsg)

            # Incident-wave point just offshore on the WEST (deepest) edge; the
            # sloping beach is -8 m west -> +4 m east, so west is seaward.
            spec = ep.validate_build_spec({
                "run_id": "swbnd",
                "aoi": {"bbox": [0, 0, 1, 1], "target_epsg": target_epsg},
                "topobathy": {"cog_uri": "s3://x/topo.tif",
                              "bathymetry_present": True},
                "grid": {
                    "x0": 600000.0, "y0": 3200000.0, "nmax": 16, "mmax": 24,
                    "dx": 200.0, "dy": 200.0, "rotation": 0.0,
                    "refinement_polygons_uri": "s3://x/refine.gpkg",
                },
                # NO mask.open_boundary_polygon_uri, NO snapwave.open_boundary_polygon_uri.
                "mask": {"zmin": -100.0, "zmax": 2.0},
                "snapwave": {"mask_zmin": -100.0, "mask_zmax": 2.0},
                "forcing": {
                    "tref": "20181010 000000",
                    "tstart": "20181010 000000",
                    "tstop": "20181010 020000",
                    "snapwave_boundary": {"points": [
                        {"x": 600000.0 - 300.0, "y": 3201600.0,
                         "hs": 3.0, "tp": 12.0, "wd": 270.0, "ds": 20.0}
                    ]},
                },
                "output": {"deck_dir_uri": "s3://x/deck/",
                           "manifest_uri": "s3://x/manifest.json"},
            })

            uri_to_local = {"s3://x/topo.tif": topo, "s3://x/refine.gpkg": refine}

            def fake_download(uri, dest):
                import shutil as _sh
                _sh.copy(uri_to_local[uri], dest)

            scratch = work / "scratch"
            scratch.mkdir()
            with mock.patch.object(ep, "_download", side_effect=fake_download):
                deck_dir, _provenance = ep.build_deck(spec, scratch)

            # The derived seaward boundary must produce a non-empty wavebnd mask.
            ds = xr.open_dataset(deck_dir / "sfincs.nc")
            try:
                sw = ds["snapwave_mask"].values.astype(int)
                self.assertGreater(
                    int((sw == 1).sum()), 0, "no active snapwave cells"
                )
                self.assertGreater(
                    int((sw > 1).sum()), 0,
                    "snapwave wave-boundary mask is EMPTY (wavebnd=0) even though "
                    "boundary points were supplied - incident wave cannot inject",
                )
            finally:
                ds.close()

    def test_derive_seaward_boundary_helper_picks_correct_edge(self):
        """Unit-level: derive_seaward_open_boundary_polygon builds a thin polygon
        on the domain edge nearest the incident-wave point, and that polygon
        actually contains outermost active-cell centres on that edge (so the cht
        snapwave neighbor-check flags them wavebnd=2)."""
        import numpy as np

        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            target_epsg = 32616
            topo = work / "topo.tif"
            self._make_topobathy_cog(topo, target_epsg)

            spec = ep.validate_build_spec({
                "run_id": "helper",
                "aoi": {"bbox": [0, 0, 1, 1], "target_epsg": target_epsg},
                "topobathy": {"cog_uri": "s3://x/topo.tif",
                              "bathymetry_present": True},
                "grid": {
                    "x0": 600000.0, "y0": 3200000.0, "nmax": 16, "mmax": 24,
                    "dx": 200.0, "dy": 200.0, "rotation": 0.0,
                },
                "mask": {"zmin": -100.0, "zmax": 2.0},
                "snapwave": {"mask_zmin": -100.0, "mask_zmax": 2.0},
                "forcing": {
                    "tref": "20181010 000000",
                    "tstart": "20181010 000000",
                    "tstop": "20181010 020000",
                },
                "output": {"deck_dir_uri": "s3://x/deck/",
                           "manifest_uri": "s3://x/manifest.json"},
            })

            def fake_download(uri, dest):
                import shutil as _sh
                _sh.copy(topo, dest)

            scratch = work / "scratch"
            scratch.mkdir()
            # Build the grid + snapwave mask directly (a trimmed build_deck head).
            from cht_sfincs import SFINCS  # type: ignore
            import xarray as xr
            import xugrid as xu

            deck = scratch / "deck"
            deck.mkdir()
            sf = SFINCS(root=str(deck), crs=target_epsg, mode="w")
            sf.grid.build(600000.0, 3200000.0, 16, 24, 200.0, 200.0, 0.0)
            xc, yc = sf.grid.face_coordinates()
            with mock.patch.object(ep, "_download", side_effect=fake_download):
                zb = ep._sample_topobathy(topo, xc, yc, target_epsg)
            ugrid2d = sf.grid.data.grid
            sf.grid.data["z"] = xu.UgridDataArray(
                xr.DataArray(data=np.asarray(zb), dims=[ugrid2d.face_dimension]),
                ugrid2d,
            )
            sf.snapwave.mask.build(zmin=-100.0, zmax=2.0)

            # Point far WEST (seaward) -> the helper should pick the west edge.
            pts = [{"x": 600000.0 - 500.0, "y": 3201600.0}]
            gdf = ep.derive_seaward_open_boundary_polygon(sf, pts, target_epsg)
            self.assertIsNotNone(gdf)
            self.assertEqual(len(gdf), 1)
            self.assertEqual(int(gdf.crs.to_epsg()), target_epsg)
            poly = gdf.geometry.iloc[0]
            # The polygon must hug the western (minimum-x) side of the domain.
            xc = np.asarray(xc, dtype=float)
            self.assertLess(poly.bounds[0], float(xc.min()) + 1.0)


if __name__ == "__main__":
    # `--with-cht` is a no-op flag for readability; the integration test
    # self-skips when cht_sfincs is unimportable.
    argv = [a for a in sys.argv if a != "--with-cht"]
    unittest.main(argv=argv, verbosity=2)
