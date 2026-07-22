"""Targeted tests for the real-fault wiring of the OpenQuake seismic composer
(task #199).

These exercise the seam that makes ``model_seismic_hazard_scenario`` use REAL
active-fault sources (``fetch_fault_sources`` -> ``render_fault_source_model_xml``)
when faults intersect the AOI, and fall back HONESTLY to the synthetic area source
when none do. Everything I/O-bound (the fault fetch, run_solver /
wait_for_completion, the S3 CSV read, the postprocess upload) is MOCKED, so no
network / boto3 is touched.

The honesty floor is the contract under test: the typed
``SeismicHazardLayerURI.source_model_kind`` reflects the path the run ACTUALLY
took and NEVER reads ``"real-fault"`` when the run fell back to the synthetic
source.

Three lenses:
  (1) ``assemble_build_spec`` pure-mapping of fault_sources -> the build_spec the
      worker consumes (+ the finer real-fault site-grid default).
  (2) ``resolve_fault_sources`` calls the fetcher and degrades honestly.
  (3) ``model_seismic_hazard_scenario`` end-to-end (mocked) asserting the fetch is
      called, the staged spec carries the right source model, and the returned
      layer narrates real-vs-fallback truthfully.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from trid3nt_contracts.openquake_contracts import (
    DEFAULT_SITE_GRID_SPACING_KM,
    OpenQuakeRunArgs,
    SeismicHazardLayerURI,
)

import trid3nt_server.workflows.model_seismic_hazard_scenario as comp
from trid3nt_server.workflows.model_seismic_hazard_scenario import (
    REAL_FAULT_SITE_GRID_SPACING_KM,
    assemble_build_spec,
    resolve_fault_sources,
)
from trid3nt_server.workflows.postprocess_openquake import (
    SEISMIC_HAZARD_STYLE_PRESET,
)

# An SF peninsula AOI (the proven local San-Andreas run).
_BBOX = (-122.55, 37.45, -122.15, 37.90)

# One fault record in the shape ``fetch_fault_sources`` emits.
_FAULT_REC = {
    "name": "San Andreas (Peninsula)",
    "geometry": [[-122.45, 37.50], [-122.30, 37.70], [-122.20, 37.88]],
    "net_slip_rate_mm_yr": 17.0,
    "dip_deg": 90.0,
    "rake_deg": 180.0,
    "upper_seis_depth_km": 0.0,
    "lower_seis_depth_km": 12.0,
    "slip_type": "Dextral",
    "catalog_name": "GEM",
}


def _fault_result(faults, note=None):
    """Shape one ``fetch_fault_sources`` return value."""
    return {
        "catalog": "gem",
        "bbox": list(_BBOX),
        "fault_count": len(faults),
        "faults": faults,
        "note": note,
        "source": "GEM Global Active Faults (harmonized)",
    }


# ===========================================================================
# (1) assemble_build_spec: fault_sources mapping + finer real-fault grid.
# ===========================================================================
def test_assemble_build_spec_no_faults_is_unchanged():
    """No fault_sources => no 'fault_sources' key + the coarse default grid
    (additive: a no-fault AOI builds exactly the synthetic spec as before)."""
    spec = assemble_build_spec(OpenQuakeRunArgs(bbox=_BBOX))
    assert "fault_sources" not in spec
    assert spec["site_grid_spacing_km"] == pytest.approx(DEFAULT_SITE_GRID_SPACING_KM)


def test_assemble_build_spec_empty_faults_is_unchanged():
    """An EMPTY fault list is treated as no faults (synthetic path)."""
    spec = assemble_build_spec(OpenQuakeRunArgs(bbox=_BBOX), fault_sources=[])
    assert "fault_sources" not in spec
    assert spec["site_grid_spacing_km"] == pytest.approx(DEFAULT_SITE_GRID_SPACING_KM)


def test_assemble_build_spec_with_faults_embeds_and_refines_grid():
    """Faults present => 'fault_sources' embedded + the grid refined to the
    finer real-fault default (because the caller left the coarse default)."""
    spec = assemble_build_spec(
        OpenQuakeRunArgs(bbox=_BBOX), fault_sources=[_FAULT_REC]
    )
    assert spec["fault_sources"] and spec["fault_sources"][0]["name"] == _FAULT_REC["name"]
    # The worker turns this list into simpleFaultSources.
    assert spec["fault_sources"][0]["net_slip_rate_mm_yr"] == pytest.approx(17.0)
    assert spec["site_grid_spacing_km"] == pytest.approx(REAL_FAULT_SITE_GRID_SPACING_KM)
    # Finer than the synthetic default.
    assert REAL_FAULT_SITE_GRID_SPACING_KM < DEFAULT_SITE_GRID_SPACING_KM


def test_assemble_build_spec_explicit_grid_is_honored_over_refine():
    """An explicit user site-grid request wins over the real-fault auto-refine."""
    args = OpenQuakeRunArgs(bbox=_BBOX, site_grid_spacing_km=1.0)
    spec = assemble_build_spec(args, fault_sources=[_FAULT_REC])
    assert spec["site_grid_spacing_km"] == pytest.approx(1.0)
    assert "fault_sources" in spec


# ===========================================================================
# (2) resolve_fault_sources: calls the fetcher; degrades honestly.
# ===========================================================================
def test_resolve_fault_sources_real_path_calls_fetcher():
    """resolve_fault_sources CALLS fetch_fault_sources and, on a hit, returns the
    records + a 'real GEM active-fault' narration line."""
    import trid3nt_server.tools.fetchers.hazard.fetch_fault_sources as ff

    with patch.object(
        ff, "fetch_fault_sources", return_value=_fault_result([_FAULT_REC])
    ) as mock_fetch:
        recs, note = resolve_fault_sources(list(_BBOX))

    mock_fetch.assert_called_once()
    # The fetcher was called with the AOI bbox.
    assert list(mock_fetch.call_args.args[0]) == list(_BBOX)
    assert len(recs) == 1
    assert "real" in note.lower() and "fault" in note.lower()
    assert _FAULT_REC["name"] in note


def test_resolve_fault_sources_empty_falls_back_honestly():
    """No faults in the AOI => empty records + the fetcher's typed honest note
    (NEVER fabricates a fault, NEVER raises)."""
    import trid3nt_server.tools.fetchers.hazard.fetch_fault_sources as ff

    fetch_note = "No GEM active faults intersect this AOI."
    with patch.object(
        ff, "fetch_fault_sources", return_value=_fault_result([], note=fetch_note)
    ):
        recs, note = resolve_fault_sources(list(_BBOX))

    assert recs == []
    assert note == fetch_note


def test_resolve_fault_sources_fetch_error_degrades_to_synthetic():
    """A genuine upstream fetch error degrades to the synthetic path (empty
    records + an honest note) rather than failing the hazard run."""
    import trid3nt_server.tools.fetchers.hazard.fetch_fault_sources as ff
    from trid3nt_server.tools.fetchers.hazard.fetch_fault_sources import FaultSourcesUpstreamError

    with patch.object(
        ff, "fetch_fault_sources", side_effect=FaultSourcesUpstreamError("boom")
    ):
        recs, note = resolve_fault_sources(list(_BBOX))

    assert recs == []
    assert "synthetic" in note.lower()


# ===========================================================================
# (3) model_seismic_hazard_scenario end-to-end (mocked) — the real wiring.
# ===========================================================================
def _seismic_layer(run_id="BATCHRID"):
    """A bare SeismicHazardLayerURI as postprocess would return it (synthetic-
    area defaults; the composer is responsible for stamping the real path)."""
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


def _wire_common_mocks(monkeypatch, staged_capture):
    """Mock run_solver / wait_for_completion / CSV download / postprocess /
    charts so the composer runs without I/O. ``staged_capture`` records the
    fault_sources passed to staging."""

    def _fake_stage(run_args, run_id, *, fault_sources=None):
        staged_capture["fault_sources"] = fault_sources
        # Run the REAL assemble so we also exercise the spec mapping.
        staged_capture["spec"] = assemble_build_spec(
            run_args, fault_sources=fault_sources
        )
        return "s3://cache/openquake_setup/RID/build_spec.json"

    monkeypatch.setattr(comp, "stage_openquake_build_spec", _fake_stage)

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
        return _Result()

    def _fake_run_solver(*, solver, model_setup_uri, compute_class):
        return _Handle()

    import trid3nt_server.tools.simulation.solver as solver_mod

    monkeypatch.setattr(solver_mod, "run_solver", _fake_run_solver, raising=False)
    monkeypatch.setattr(solver_mod, "wait_for_completion", _fake_wait, raising=False)

    monkeypatch.setattr(
        comp,
        "_download_batch_hazard_csv",
        lambda run_result, run_id: "lon,lat,PGA-0.1\n-122.4,37.6,0.6\n",
    )
    monkeypatch.setattr(
        comp, "postprocess_openquake", lambda *a, **k: _seismic_layer()
    )

    # Silence the best-effort curve charts.
    async def _no_charts(*a, **k):
        return None

    monkeypatch.setattr(comp, "_emit_oq_curve_charts", _no_charts)


@pytest.mark.asyncio
async def test_composer_uses_real_faults_when_present(monkeypatch):
    """When fetch_fault_sources returns faults, the composer:
      - CALLS the fetcher,
      - stages the build_spec WITH the fault source model (+ refined grid),
      - returns a layer narrating source_model_kind == 'real-fault'.
    """
    import trid3nt_server.tools.fetchers.hazard.fetch_fault_sources as ff

    fetch_mock = patch.object(
        ff, "fetch_fault_sources", return_value=_fault_result([_FAULT_REC])
    )
    staged: dict = {}
    _wire_common_mocks(monkeypatch, staged)

    with fetch_mock as mock_fetch:
        layer = await comp.model_seismic_hazard_scenario(
            OpenQuakeRunArgs(bbox=_BBOX), compute_class="standard"
        )

    # The fetcher was actually called.
    mock_fetch.assert_called_once()
    # The staged spec carried the real fault source model + the refined grid.
    assert staged["fault_sources"] and staged["fault_sources"][0]["name"] == _FAULT_REC["name"]
    assert "fault_sources" in staged["spec"]
    assert staged["spec"]["site_grid_spacing_km"] == pytest.approx(
        REAL_FAULT_SITE_GRID_SPACING_KM
    )
    # The typed result narrates the REAL path (honesty floor).
    assert isinstance(layer, SeismicHazardLayerURI)
    assert layer.source_model_kind == "real-fault"
    assert "real" in layer.source_model_note.lower()
    assert _FAULT_REC["name"] in layer.source_model_note


@pytest.mark.asyncio
async def test_composer_falls_back_and_narrates_honestly_when_no_faults(monkeypatch):
    """When NO fault intersects the AOI, the composer:
      - still CALLS the fetcher,
      - stages the build_spec with NO fault source model (synthetic area source),
      - returns a layer narrating source_model_kind == 'synthetic-area' and
        NEVER claims real faults.
    """
    import trid3nt_server.tools.fetchers.hazard.fetch_fault_sources as ff

    fetch_note = "No GEM active faults intersect this AOI."
    fetch_mock = patch.object(
        ff, "fetch_fault_sources", return_value=_fault_result([], note=fetch_note)
    )
    staged: dict = {}
    _wire_common_mocks(monkeypatch, staged)

    with fetch_mock as mock_fetch:
        layer = await comp.model_seismic_hazard_scenario(
            OpenQuakeRunArgs(bbox=_BBOX), compute_class="standard"
        )

    mock_fetch.assert_called_once()
    # No fault source model staged -> synthetic area source (default grid).
    assert staged["fault_sources"] is None
    assert "fault_sources" not in staged["spec"]
    assert staged["spec"]["site_grid_spacing_km"] == pytest.approx(
        DEFAULT_SITE_GRID_SPACING_KM
    )
    # The typed result is HONEST: synthetic, not real-fault.
    assert layer.source_model_kind == "synthetic-area"
    assert layer.source_model_note == fetch_note
    assert "real" not in layer.source_model_kind


@pytest.mark.asyncio
async def test_composer_degrades_to_synthetic_on_fetch_error(monkeypatch):
    """A fault-fetch UPSTREAM error must NOT fail the hazard run: the composer
    degrades to the synthetic area source and narrates honestly."""
    import trid3nt_server.tools.fetchers.hazard.fetch_fault_sources as ff
    from trid3nt_server.tools.fetchers.hazard.fetch_fault_sources import FaultSourcesUpstreamError

    fetch_mock = patch.object(
        ff, "fetch_fault_sources", side_effect=FaultSourcesUpstreamError("down")
    )
    staged: dict = {}
    _wire_common_mocks(monkeypatch, staged)

    with fetch_mock:
        layer = await comp.model_seismic_hazard_scenario(
            OpenQuakeRunArgs(bbox=_BBOX), compute_class="standard"
        )

    assert staged["fault_sources"] is None
    assert layer.source_model_kind == "synthetic-area"
    assert "synthetic" in layer.source_model_note.lower()
