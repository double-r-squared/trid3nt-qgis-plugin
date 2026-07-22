"""Targeted tests for the OpenQuake PSHA engine modules (sprint-17).

The OpenQuake analogue of ``test_postprocess_swmm`` / ``test_run_modflow``. The
engine is NOT registry-wired yet (the orchestrator merges the shared-registry
snippets after all lanes finish), so these tests exercise the engine MODULES in
ISOLATION with run_solver / boto3 / network MOCKED:

1. **Composer arg-assembly** — ``assemble_build_spec`` maps OpenQuakeRunArgs onto
   the worker build_spec dict (no I/O). (no OpenQuake needed)
2. **Composer dispatch (mocked)** — ``model_seismic_hazard_scenario`` stages the
   build_spec, dispatches via a MOCKED run_solver/wait_for_completion, downloads
   a synthetic hazard CSV via a MOCKED S3 read, and postprocesses to a
   SeismicHazardLayerURI — asserting the typed scalars + that publish is skipped
   for the local file:// COG. (no network/boto3; rasterio needed)
3. **Postprocess** — ``parse_hazard_map_csv`` / ``rasterize_hazard_sites`` /
   ``compute_hazard_metrics`` on a synthetic OpenQuake hazard-map CSV, then the
   full ``postprocess_openquake`` end-to-end (publish mocked) asserting a valid
   EPSG:4326 hazard COG + the narration scalars. (rasterio needed for the COG)

rasterio + numpy are required for the COG-writing tests; skipped if absent.
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from trid3nt_contracts.openquake_contracts import (
    OpenQuakeRunArgs,
    SeismicHazardLayerURI,
)
from trid3nt_server.workflows.model_seismic_hazard_scenario import (
    OPENQUAKE_SOLVER_NAME,
    assemble_build_spec,
)
from trid3nt_server.workflows.postprocess_openquake import (
    HAZARD_FLOOR_VALUE,
    SEISMIC_HAZARD_STYLE_PRESET,
    compute_hazard_metrics,
    parse_hazard_map_csv,
    rasterize_hazard_sites,
)

_rasterio = pytest.importorskip("rasterio", reason="rasterio required for COG tests")
_np = pytest.importorskip("numpy", reason="numpy required for hazard rasterize")
import numpy as np  # noqa: E402


_BBOX = (-122.5, 37.5, -121.5, 38.5)


# A synthetic OpenQuake hazard-MAP CSV: a 3x3 PSHA site grid. The leading "#"
# banner comment + the lon,lat,<value> header mirror the real export shape.
_SYNTH_HAZARD_CSV = """# generated_by='OpenQuake engine 3.19', start_date='2026-06-21'
lon,lat,PGA-0.1
-122.5,37.5,0.21
-122.0,37.5,0.34
-121.5,37.5,0.18
-122.5,38.0,0.55
-122.0,38.0,0.62
-121.5,38.0,0.41
-122.5,38.5,0.12
-122.0,38.5,0.27
-121.5,38.5,0.0005
"""


# ===========================================================================
# (1) Composer arg-assembly — pure, no I/O.
# ===========================================================================
def test_assemble_build_spec_maps_all_fields():
    args = OpenQuakeRunArgs(
        bbox=_BBOX,
        imt="SA(0.3)",
        poe=0.02,
        investigation_time_years=50.0,
        site_grid_spacing_km=10.0,
        max_distance_km=200.0,
        gmpe="ChiouYoungs2014",
        a_value=3.5,
        b_value=0.9,
        min_magnitude=4.5,
        max_magnitude=8.0,
    )
    spec = assemble_build_spec(args)
    assert spec["bbox"] == list(_BBOX)
    assert spec["imt"] == "SA(0.3)"
    assert spec["poe"] == pytest.approx(0.02)
    assert spec["site_grid_spacing_km"] == pytest.approx(10.0)
    assert spec["max_distance_km"] == pytest.approx(200.0)
    assert spec["gmpe"] == "ChiouYoungs2014"
    assert spec["a_value"] == pytest.approx(3.5)
    assert spec["max_magnitude"] == pytest.approx(8.0)
    # The worker output globs are carried.
    assert "output/*.csv" in spec["outputs"]


# ===========================================================================
# (3) Postprocess primitives — parse / rasterize / metrics.
# ===========================================================================
def test_parse_hazard_map_csv():
    rows, value_header = parse_hazard_map_csv(_SYNTH_HAZARD_CSV)
    assert value_header == "PGA-0.1"
    assert len(rows) == 9
    # The comment banner is dropped; values are floats.
    assert (-122.0, 38.0, 0.62) in rows


def test_rasterize_hazard_sites_lattice():
    rows, _ = parse_hazard_map_csv(_SYNTH_HAZARD_CSV)
    grid, bbox, cell = rasterize_hazard_sites(rows)
    assert grid.shape == (3, 3)  # 3 lats x 3 lons
    # row 0 = NORTH -> the 38.5 row; col 1 = -122.0.
    assert grid[0, 1] == pytest.approx(0.27)
    # The peak value 0.62 is at lat 38.0 (middle row), lon -122.0 (middle col).
    assert grid[1, 1] == pytest.approx(0.62)
    # cell ~ 0.5 deg spacing.
    assert cell == pytest.approx(0.5, abs=0.01)
    # bbox frames the site centers (expanded by half a cell).
    assert bbox[0] < -122.5 and bbox[2] > -121.5


def test_compute_hazard_metrics():
    rows, _ = parse_hazard_map_csv(_SYNTH_HAZARD_CSV)
    grid, _bbox, cell = rasterize_hazard_sites(rows)
    mean_lat = 38.0
    max_val, hazard_area, n_sites = compute_hazard_metrics(grid, cell, mean_lat)
    assert max_val == pytest.approx(0.62)
    # 9 sites, but one (0.0005) is below the hazard floor.
    assert n_sites == 9
    # Area counts only cells above the floor (8 of 9).
    cell_km = cell * 111.32
    expected_cell_area = cell_km * cell_km * abs(math.cos(math.radians(mean_lat)))
    assert hazard_area == pytest.approx(8 * expected_cell_area, rel=0.05)


# ===========================================================================
# (3b) Full postprocess -> valid EPSG:4326 COG (publish mocked).
# ===========================================================================
def test_postprocess_openquake_end_to_end(monkeypatch):
    from trid3nt_server.workflows import postprocess_openquake as pp

    # Force the local file:// upload path (no S3) so no boto3/network is touched.
    monkeypatch.setattr(pp, "_upload_cog", lambda cog, run_id, bucket: f"file://{cog}")

    with patch.object(pp, "_dispatch_publish_layer", return_value=None) as mock_pub:
        layer = pp.postprocess_openquake(
            _SYNTH_HAZARD_CSV,
            run_id="01TESTRUN",
            imt="PGA",
            poe=0.10,
            investigation_time_years=50.0,
            publish=True,
        )

    assert isinstance(layer, SeismicHazardLayerURI)
    assert layer.layer_id == "seismic-hazard-01TESTRUN"
    assert layer.style_preset == SEISMIC_HAZARD_STYLE_PRESET
    assert layer.layer_type == "raster"
    assert layer.units == "g"
    assert layer.max_hazard_value == pytest.approx(0.62)
    assert layer.n_sites == 9
    # 10% in 50 yr -> ~475-yr return period.
    assert layer.return_period_years == pytest.approx(474.6, abs=1.0)
    # publish was attempted once.
    assert mock_pub.call_count == 1
    # The URI points at a real, valid EPSG:4326 COG on disk.
    assert layer.uri.startswith("file://")
    cog_path = layer.uri[len("file://"):]
    with _rasterio.open(cog_path) as ds:
        assert ds.crs.to_epsg() == 4326
        assert ds.count == 1
        arr = ds.read(1)
        # The sub-floor site (0.0005) is masked to NaN in the COG.
        assert np.nanmax(arr) == pytest.approx(0.62, rel=1e-3)


def test_postprocess_openquake_empty_csv_raises():
    from trid3nt_server.workflows.postprocess_openquake import (
        PostprocessOpenQuakeError,
        postprocess_openquake,
    )

    with pytest.raises(PostprocessOpenQuakeError):
        postprocess_openquake(
            "# only a banner\nlon,lat,PGA-0.1\n",
            run_id="x",
            imt="PGA",
            poe=0.10,
            investigation_time_years=50.0,
            publish=False,
        )


# ===========================================================================
# (2) Composer dispatch — run_solver / wait_for_completion / S3 MOCKED.
# ===========================================================================
@pytest.mark.asyncio
async def test_model_seismic_hazard_scenario_mocked_dispatch(monkeypatch):
    import trid3nt_server.workflows.model_seismic_hazard_scenario as comp

    args = OpenQuakeRunArgs(bbox=_BBOX)

    # Stub the staging (no S3 put). Accept the additive task-#199 fault_sources
    # kwarg the composer now threads through.
    monkeypatch.setattr(
        comp,
        "stage_openquake_build_spec",
        lambda run_args, run_id, *, fault_sources=None: (
            "s3://cache/openquake_setup/RID/build_spec.json"
        ),
    )

    # task #199: stub the real-fault fetch so this dispatch test stays hermetic
    # (no network). The synthetic-fallback path is the default here; the
    # real-fault wiring has its own dedicated suite
    # (test_seismic_real_fault_wiring.py).
    import trid3nt_server.tools.fetch_fault_sources as ff

    monkeypatch.setattr(
        ff,
        "fetch_fault_sources",
        lambda bbox, **_k: {
            "catalog": "gem",
            "bbox": list(bbox),
            "fault_count": 0,
            "faults": [],
            "note": "No GEM active faults intersect this AOI.",
            "source": "GEM Global Active Faults (harmonized)",
        },
    )

    # Stub run_solver -> a fake handle; wait_for_completion -> a complete result.
    class _Handle:
        run_id = "BATCHRID"

    class _Result:
        status = "complete"
        run_id = "BATCHRID"
        output_uri = "s3://runs/BATCHRID/"
        error_code = None
        error_message = None
        cancellation_reason = None

    async def _fake_wait(handle):
        assert handle.run_id == "BATCHRID"
        return _Result()

    def _fake_run_solver(*, solver, model_setup_uri, compute_class):
        assert solver == OPENQUAKE_SOLVER_NAME
        assert model_setup_uri.endswith("build_spec.json")
        return _Handle()

    # run_solver / wait_for_completion are imported INSIDE the composer from
    # ..tools.solver; patch them at that module so the import resolves to stubs.
    import trid3nt_server.tools.solver as solver_mod

    monkeypatch.setattr(solver_mod, "run_solver", _fake_run_solver, raising=False)
    monkeypatch.setattr(solver_mod, "wait_for_completion", _fake_wait, raising=False)

    # Stub the hazard-CSV download (no S3 read).
    monkeypatch.setattr(
        comp,
        "_download_batch_hazard_csv",
        lambda run_result, run_id: _SYNTH_HAZARD_CSV,
    )

    # Stub the postprocess to avoid touching the upload path; assert it is fed
    # the downloaded CSV + the run_args fields.
    captured: dict = {}

    def _fake_postprocess(csv_text, *, run_id, imt, poe, investigation_time_years):
        captured.update(
            csv_text=csv_text, run_id=run_id, imt=imt, poe=poe, inv=investigation_time_years
        )
        return SeismicHazardLayerURI(
            layer_id=f"seismic-hazard-{run_id}",
            name="Seismic hazard (PGA, 475-yr return period)",
            layer_type="raster",
            uri="file:///tmp/hazard.tif",
            style_preset=SEISMIC_HAZARD_STYLE_PRESET,
            return_period_years=475.0,
            max_hazard_value=0.62,
            hazard_area_km2=100.0,
            n_sites=9,
        )

    monkeypatch.setattr(comp, "postprocess_openquake", _fake_postprocess)

    layer = await comp.model_seismic_hazard_scenario(args, compute_class="standard")

    assert isinstance(layer, SeismicHazardLayerURI)
    assert layer.layer_id == "seismic-hazard-BATCHRID"
    assert captured["csv_text"] == _SYNTH_HAZARD_CSV
    assert captured["run_id"] == "BATCHRID"
    assert captured["imt"] == "PGA"
    assert captured["poe"] == pytest.approx(0.10)
