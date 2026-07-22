"""task #207: surface engine INPUT data as renderable role="input" layers.

Every engine run consumes renderable inputs (OpenQuake fault traces, SFINCS
DEM / landcover / rivers, SWMM building footprints) but historically only the
RESULT layer was published. These tests pin the new surfacing seam:

  (1) ``publish_input_layer`` -- the shared helper: forces role="input" +
      bbox=None, is best-effort (NEVER raises), and respects the emit_layer_uri
      guardrail (a raw-object-store raster is DROPPED, a vector passes).
  (2) OpenQuake fault serialization -> a valid GeoJSON FeatureCollection of
      LineStrings carrying the click-inspect props, and the composer emits a
      role="input" fault vector ONLY when real faults were used (and nothing
      extra when no real faults).
  (3) SFINCS surfaces the river vector + the DEM/landcover rasters as
      role="input" (publish_layer mocked).
  (4) a failure to surface an input does NOT raise (the solve is unaffected).

Everything I/O-bound (S3 put, publish_layer, the solver chain) is MOCKED -- no
network / boto3 is touched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from grace2_contracts.execution import LayerURI
from grace2_contracts import new_ulid

from grace2_agent.layer_uri_emit import publish_input_layer
from grace2_agent.pipeline_emitter import (
    _CURRENT_EMITTER,
    PipelineEmitter,
)


class _Sink:
    async def __call__(self, text: str) -> None:  # pragma: no cover - trivial
        import json

        json.loads(text)


def _emitter() -> PipelineEmitter:
    return PipelineEmitter(session_id=new_ulid(), sink=_Sink())


# ===========================================================================
# (1) publish_input_layer -- the shared helper.
# ===========================================================================
@pytest.mark.asyncio
async def test_publish_input_layer_forces_role_input_and_no_bbox():
    """A vector with role!=input + a bbox is COPIED to role="input" + bbox=None
    (an input must render non-intrusively and emit NO competing zoom-to)."""
    import json

    frames: list[dict] = []

    async def _capture(text: str) -> None:
        frames.append(json.loads(text))

    emitter = PipelineEmitter(session_id=new_ulid(), sink=_capture)
    layer = LayerURI(
        layer_id="rivers-1",
        name="Rivers",
        layer_type="vector",
        uri="s3://runs/r/rivers.fgb",
        style_preset="osm_waterways",
        role="primary",
        bbox=(-1.0, -1.0, 1.0, 1.0),
    )
    # Stub the (vector) inline-read so add_loaded_layer does not hit S3.
    with patch(
        "grace2_agent.pipeline_emitter._read_vector_uri_as_geojson",
        return_value={"type": "FeatureCollection", "features": []},
    ):
        ok = await publish_input_layer(emitter, layer)

    assert ok is True
    assert len(emitter._loaded_layers) == 1
    row = emitter._loaded_layers[0]
    assert row.role == "input"
    assert row.layer_id == "rivers-1"
    # bbox forced to None => NO zoom-to map-command was emitted for the input.
    map_cmds = [f for f in frames if f.get("type") == "map-command"]
    zoom_tos = [
        f for f in map_cmds if (f.get("payload") or {}).get("command") == "zoom-to"
    ]
    assert zoom_tos == [], f"an input must not emit a zoom-to; got {zoom_tos}"


@pytest.mark.asyncio
async def test_publish_input_layer_surfaces_raw_s3_raster():
    """NEW CONTRACT (TiTiler exit / QGIS-native swap): a raster carrying a raw
    s3:// COG uri PASSES the guardrail (the plugin reads it via /vsicurl/) and
    IS surfaced as an input row."""
    emitter = _emitter()
    layer = LayerURI(
        layer_id="dem-raw",
        name="DEM",
        layer_type="raster",
        uri="s3://runs/r/dem.tif",  # raw s3 COG - now renderable
        style_preset="continuous_dem",
        role="input",
    )
    ok = await publish_input_layer(emitter, layer)
    assert ok is True
    assert len(emitter._loaded_layers) == 1
    assert emitter._loaded_layers[0].uri == "s3://runs/r/dem.tif"
    assert emitter._loaded_layers[0].role == "input"


@pytest.mark.asyncio
async def test_publish_input_layer_drops_gs_raster():
    """A raster carrying a raw gs:// uri is still DROPPED by the guardrail
    (no face on this stack can fetch it) -> not surfaced, returns False,
    NEVER raises."""
    emitter = _emitter()
    layer = LayerURI(
        layer_id="dem-gs",
        name="DEM",
        layer_type="raster",
        uri="gs://runs/r/dem.tif",  # genuinely un-renderable
        style_preset="continuous_dem",
        role="input",
    )
    ok = await publish_input_layer(emitter, layer)
    assert ok is False
    assert emitter._loaded_layers == []


@pytest.mark.asyncio
async def test_publish_input_layer_none_emitter_is_noop():
    """No emitter bound (verify/CI direct-call) -> no-op, returns False, no raise."""
    layer = LayerURI(
        layer_id="x", name="x", layer_type="vector", uri="s3://r/x.fgb",
        style_preset="p", role="input",
    )
    assert await publish_input_layer(None, layer) is False
    assert await publish_input_layer(_emitter(), None) is False


@pytest.mark.asyncio
async def test_publish_input_layer_swallows_add_loaded_layer_failure():
    """A failure inside add_loaded_layer is swallowed (best-effort): returns
    False, NEVER raises -- the solve is unaffected."""
    emitter = _emitter()

    async def _boom(_layer):
        raise RuntimeError("emit blew up")

    emitter.add_loaded_layer = _boom  # type: ignore[method-assign]
    layer = LayerURI(
        layer_id="v", name="v", layer_type="vector", uri="s3://r/v.fgb",
        style_preset="p", role="input",
    )
    # Must NOT raise.
    ok = await publish_input_layer(emitter, layer)
    assert ok is False


# ===========================================================================
# (2) OpenQuake fault serialization + composer wiring.
# ===========================================================================
import grace2_agent.workflows.model_seismic_hazard_scenario as seismic  # noqa: E402
from grace2_agent.workflows.model_seismic_hazard_scenario import (  # noqa: E402
    FAULT_LINE_STYLE_PRESET,
    fault_records_to_feature_collection,
    make_fault_sources_layer_uri,
)

_FAULT_REC = {
    "name": "San Andreas (Peninsula)",
    "geometry": [[-122.45, 37.50], [-122.30, 37.70], [-122.20, 37.88]],
    "net_slip_rate_mm_yr": 17.0,
    "slip_type": "Dextral",
    "catalog_name": "GEM",
}


def test_fault_records_to_feature_collection_shape_and_props():
    """A record -> a LineString feature carrying name / net_slip_rate_mm_yr /
    slip_type (+ catalog_name); a <2-vertex (degenerate) trace is SKIPPED."""
    degenerate = {"name": "pt", "geometry": [[-1.0, 1.0]], "net_slip_rate_mm_yr": 3.0}
    fc = fault_records_to_feature_collection([_FAULT_REC, degenerate])

    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1  # the degenerate trace was dropped
    ft = fc["features"][0]
    assert ft["type"] == "Feature"
    assert ft["geometry"]["type"] == "LineString"
    assert ft["geometry"]["coordinates"] == [
        [-122.45, 37.50], [-122.30, 37.70], [-122.20, 37.88]
    ]
    props = ft["properties"]
    assert props["name"] == "San Andreas (Peninsula)"
    assert props["net_slip_rate_mm_yr"] == 17.0
    assert props["slip_type"] == "Dextral"
    assert props["catalog_name"] == "GEM"


def test_fault_records_to_feature_collection_empty():
    assert fault_records_to_feature_collection([]) == {
        "type": "FeatureCollection",
        "features": [],
    }


def test_make_fault_sources_layer_uri_uploads_and_is_role_input(monkeypatch):
    """make_fault_sources_layer_uri serializes + uploads to the runs bucket and
    returns a role="input" vector LayerURI (bbox=None, fault_line preset, with a
    LegendKey). S3 is mocked."""
    puts: list[dict] = []

    class _FakeS3:
        def put_object(self, **kw):
            puts.append(kw)

    import grace2_agent.tools.solver as solver_mod

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _FakeS3())
    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "test-runs")

    layer = make_fault_sources_layer_uri([_FAULT_REC], run_id="RID")
    assert layer is not None
    assert layer.layer_type == "vector"
    assert layer.role == "input"
    assert layer.bbox is None
    assert layer.style_preset == FAULT_LINE_STYLE_PRESET
    assert layer.uri == "s3://test-runs/RID/fault_sources.geojson"
    assert layer.legend is not None and layer.legend.kind == "categorical"
    # The FC was actually uploaded.
    assert len(puts) == 1
    assert puts[0]["Key"] == "RID/fault_sources.geojson"


def test_make_fault_sources_layer_uri_no_features_returns_none(monkeypatch):
    """No drawable traces => None (best-effort, no upload)."""
    import grace2_agent.tools.solver as solver_mod

    called = {"put": False}

    class _FakeS3:
        def put_object(self, **kw):  # pragma: no cover - must not run
            called["put"] = True

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _FakeS3())
    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "test-runs")
    # A single degenerate record yields zero features.
    assert make_fault_sources_layer_uri(
        [{"name": "pt", "geometry": [[-1.0, 1.0]], "net_slip_rate_mm_yr": 3.0}],
        run_id="RID",
    ) is None
    assert called["put"] is False


def test_make_fault_sources_layer_uri_s3_failure_is_non_fatal(monkeypatch):
    """An S3 put failure returns None (the fault input is simply absent), NEVER
    raises."""
    import grace2_agent.tools.solver as solver_mod

    class _BoomS3:
        def put_object(self, **kw):
            raise RuntimeError("s3 down")

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _BoomS3())
    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "test-runs")
    assert make_fault_sources_layer_uri([_FAULT_REC], run_id="RID") is None


# --- composer end-to-end (mocked): emits a role="input" fault vector ONLY when
#     real faults were used; nothing extra when no real faults. ---------------
from grace2_contracts.openquake_contracts import OpenQuakeRunArgs  # noqa: E402
from grace2_contracts.openquake_contracts import SeismicHazardLayerURI  # noqa: E402
from grace2_agent.workflows.postprocess_openquake import (  # noqa: E402
    SEISMIC_HAZARD_STYLE_PRESET,
)
from grace2_agent.workflows.model_seismic_hazard_scenario import (  # noqa: E402
    assemble_build_spec,
)

_BBOX = (-122.55, 37.45, -122.15, 37.90)


def _fault_result(faults, note=None):
    return {
        "catalog": "gem", "bbox": list(_BBOX), "fault_count": len(faults),
        "faults": faults, "note": note, "source": "GEM",
    }


def _seismic_layer(run_id="BATCHRID"):
    return SeismicHazardLayerURI(
        layer_id=f"seismic-hazard-{run_id}",
        name="Seismic hazard",
        layer_type="raster",
        uri="file:///tmp/hazard.tif",
        style_preset=SEISMIC_HAZARD_STYLE_PRESET,
        return_period_years=475.0,
        max_hazard_value=0.62,
        hazard_area_km2=100.0,
        n_sites=9,
    )


def _wire_seismic_mocks(monkeypatch):
    monkeypatch.setattr(
        seismic, "stage_openquake_build_spec",
        lambda run_args, run_id, *, fault_sources=None: "s3://cache/spec.json",
    )

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

    import grace2_agent.tools.solver as solver_mod

    monkeypatch.setattr(
        solver_mod, "run_solver",
        lambda *, solver, model_setup_uri, compute_class: _Handle(),
        raising=False,
    )
    monkeypatch.setattr(solver_mod, "wait_for_completion", _fake_wait, raising=False)
    monkeypatch.setattr(
        seismic, "_download_batch_hazard_csv",
        lambda run_result, run_id: "lon,lat,PGA-0.1\n-122.4,37.6,0.6\n",
    )
    monkeypatch.setattr(seismic, "postprocess_openquake", lambda *a, **k: _seismic_layer())

    async def _no_charts(*a, **k):
        return None

    monkeypatch.setattr(seismic, "_emit_oq_curve_charts", _no_charts)
    # Mock the S3 upload inside make_fault_sources_layer_uri (real serialize).
    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "test-runs")

    class _FakeS3:
        def put_object(self, **kw):
            return None

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _FakeS3())


@pytest.mark.asyncio
async def test_composer_emits_fault_input_when_real_faults(monkeypatch):
    """When real faults are used, the composer surfaces a role="input" fault
    VECTOR layer (the fault_sources.geojson) on the emitter."""
    import grace2_agent.tools.fetch_fault_sources as ff

    _wire_seismic_mocks(monkeypatch)
    emitter = _emitter()
    token = _CURRENT_EMITTER.set(emitter)
    try:
        with patch.object(
            ff, "fetch_fault_sources", return_value=_fault_result([_FAULT_REC])
        ), patch(
            "grace2_agent.pipeline_emitter._read_vector_uri_as_geojson",
            return_value=fault_records_to_feature_collection([_FAULT_REC]),
        ):
            await seismic.model_seismic_hazard_scenario(
                OpenQuakeRunArgs(bbox=_BBOX), compute_class="standard"
            )
    finally:
        _CURRENT_EMITTER.reset(token)

    fault_rows = [
        l for l in emitter._loaded_layers if l.layer_id.startswith("fault-sources-")
    ]
    assert len(fault_rows) == 1, (
        f"expected one role=input fault vector; got "
        f"{[l.layer_id for l in emitter._loaded_layers]}"
    )
    assert fault_rows[0].role == "input"
    assert fault_rows[0].layer_type == "vector"


@pytest.mark.asyncio
async def test_composer_emits_no_fault_input_when_no_real_faults(monkeypatch):
    """When NO real fault intersects the AOI, the composer emits NO fault input
    layer (nothing extra surfaced)."""
    import grace2_agent.tools.fetch_fault_sources as ff

    _wire_seismic_mocks(monkeypatch)
    emitter = _emitter()
    token = _CURRENT_EMITTER.set(emitter)
    try:
        with patch.object(
            ff, "fetch_fault_sources",
            return_value=_fault_result([], note="No GEM faults in this AOI."),
        ):
            await seismic.model_seismic_hazard_scenario(
                OpenQuakeRunArgs(bbox=_BBOX), compute_class="standard"
            )
    finally:
        _CURRENT_EMITTER.reset(token)

    fault_rows = [
        l for l in emitter._loaded_layers if l.layer_id.startswith("fault-sources-")
    ]
    assert fault_rows == [], (
        f"no fault input must be surfaced on the synthetic path; got {fault_rows}"
    )


# ===========================================================================
# (3) SFINCS surfaces river vector + DEM/landcover rasters as role="input".
# ===========================================================================
import grace2_agent.workflows.model_flood_scenario as flood  # noqa: E402
from grace2_agent.workflows.model_flood_scenario import model_flood_scenario  # noqa: E402


def _flood_input_layer(kind: str) -> LayerURI:
    if kind == "rivers":
        return LayerURI(
            layer_id="rivers-test", name="Rivers", layer_type="vector",
            uri="s3://test-cache/rivers/test.fgb", style_preset="osm_waterways",
            role="input",
        )
    return LayerURI(
        layer_id=f"{kind}-test", name=f"{kind} layer", layer_type="raster",
        uri=f"s3://test-cache/{kind}/test.tif",
        style_preset="continuous_dem" if kind == "dem" else "categorical_landcover",
        role="input",
    )


@pytest.mark.asyncio
async def test_sfincs_surfaces_dem_landcover_river_as_inputs(monkeypatch):
    """The flood composer surfaces the river VECTOR (no publish round-trip) and
    the DEM + landcover RASTERS (publish_layer mocked) as role="input"."""
    run_id = new_ulid()
    handle = ExecutionHandle_helper(run_id)
    landcover_result = {"layer": _flood_input_layer("landcover"), "nlcd_vintage_year": 2021}
    precip_result = {
        "precip_inches": 8.0, "vintage_volume": "NOAA Atlas 14",
        "project_area": "FL", "return_period_years": 100, "duration_hours": 24,
    }

    class _ModelSetup:
        setup_id = new_ulid()
        solver = "sfincs"
        setup_uri = "s3://cache/setup/x"
        grid_resolution_m = 30.0
        bbox = (-81.92, 26.55, -81.80, 26.68)
        parameters: dict = {}
        created_at = datetime.now(timezone.utc)

    _rid = run_id

    class _RunResultOK:
        run_id = _rid
        handle_id = handle.handle_id
        status = "complete"
        output_uri = f"s3://grace2-hazard-runs/{_rid}/"
        started_at = datetime.now(timezone.utc)
        completed_at = datetime.now(timezone.utc)
        duration_seconds = 1.0
        error_code = None
        error_message = None
        cancellation_reason = None
        batch_compute_meta = None

    peak_layer = LayerURI(
        layer_id=f"flood-depth-peak-{run_id}", name="Peak flood depth",
        layer_type="raster", uri=f"gs://runs/{run_id}/flood_depth_peak.tif",
        style_preset="continuous_flood_depth", role="primary", units="meters",
    )

    async def _wfc(_handle):
        return _RunResultOK()

    publish_calls: list[str] = []

    def _mock_publish_layer(layer_uri, layer_id, style_preset, **kw):  # noqa: ANN001
        publish_calls.append(layer_id)
        from urllib.parse import quote

        return (
            "https://titiler.test/cog/tiles/{z}/{x}/{y}.png"
            f"?url={quote(layer_uri, safe='')}&rescale=0,3"
        )

    emitter = _emitter()
    token = _CURRENT_EMITTER.set(emitter)
    try:
        with (
            patch.object(flood, "fetch_dem", return_value=_flood_input_layer("dem")),
            patch.object(flood, "fetch_landcover", return_value=landcover_result),
            patch.object(flood, "fetch_river_geometry", return_value=_flood_input_layer("rivers")),
            patch.object(flood, "lookup_precip_return_period", return_value=precip_result),
            patch.object(flood, "build_sfincs_model", return_value=_ModelSetup()),
            patch.object(flood, "run_solver", return_value=handle),
            patch.object(flood, "wait_for_completion", side_effect=_wfc),
            patch.object(
                flood, "postprocess_flood",
                return_value=([peak_layer], {"max_depth_m": 1.0, "crs": "EPSG:32617", "units": "meters"}),
            ),
            patch.object(flood, "publish_layer", side_effect=_mock_publish_layer),
            patch(
                "grace2_agent.pipeline_emitter._read_vector_uri_as_geojson",
                return_value={"type": "FeatureCollection", "features": []},
            ),
        ):
            await model_flood_scenario(
                bbox=(-81.92, 26.55, -81.80, 26.68),
                return_period_yr=100,
                duration_hr=24,
                compute_class="medium",
            )
    finally:
        _CURRENT_EMITTER.reset(token)

    # DEM + landcover inputs went through a publish_layer round-trip.
    input_pub = {c.rsplit("-", 1)[0] for c in publish_calls if c.startswith("input-")}
    assert input_pub == {"input-dem", "input-landcover"}, (
        f"DEM + landcover must publish as inputs; got {publish_calls}"
    )

    # The emitter carries the surfaced inputs, all role="input".
    input_rows = [l for l in emitter._loaded_layers if l.role == "input"]
    names = {l.layer_id for l in input_rows}
    # river vector + the 2 published rasters surfaced.
    assert any(n.startswith("input-dem") for n in names), names
    assert any(n.startswith("input-landcover") for n in names), names
    assert any(n == "rivers-test" for n in names), names
    assert all(l.role == "input" for l in input_rows)


# ===========================================================================
# (4 bonus) SWMM building footprints surfaced as a role="input" vector.
# ===========================================================================
from grace2_agent.workflows.model_urban_flood_swmm import (  # noqa: E402
    make_buildings_input_layer_uri,
)


def test_make_buildings_input_layer_uri_uploads_role_input(monkeypatch):
    """A buildings FeatureCollection uploads to the runs bucket + returns a
    role="input" vector LayerURI (bbox=None). S3 mocked."""
    import grace2_agent.tools.solver as solver_mod

    puts: list[dict] = []

    class _FakeS3:
        def put_object(self, **kw):
            puts.append(kw)

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _FakeS3())
    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "test-runs")

    fc = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[]]}, "properties": {}}
        ],
    }
    layer = make_buildings_input_layer_uri(fc, run_id="RID")
    assert layer is not None
    assert layer.layer_type == "vector"
    assert layer.role == "input"
    assert layer.bbox is None
    assert layer.uri == "s3://test-runs/RID/buildings_input.geojson"
    assert len(puts) == 1


def test_make_buildings_input_layer_uri_empty_returns_none(monkeypatch):
    """An empty / non-FC input returns None (best-effort, no upload, no raise)."""
    import grace2_agent.tools.solver as solver_mod

    monkeypatch.setattr(
        solver_mod, "_get_s3_client",
        lambda: (_ for _ in ()).throw(AssertionError("must not upload")),
    )
    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "test-runs")
    assert make_buildings_input_layer_uri(None, run_id="RID") is None
    assert make_buildings_input_layer_uri(
        {"type": "FeatureCollection", "features": []}, run_id="RID"
    ) is None


# Small ExecutionHandle factory (avoids importing the whole flood-test harness).
from grace2_contracts.execution import ExecutionHandle  # noqa: E402


def ExecutionHandle_helper(run_id: str) -> ExecutionHandle:
    return ExecutionHandle(
        handle_id=new_ulid(),
        run_id=run_id,
        solver="sfincs",
        compute_class="standard",
        workflows_execution_id="projects/t/locations/us/workflows/w/executions/e",
        workflow_name="grace-2-sfincs-orchestrator",
        workflow_location="us-central1",
        submitted_at=datetime.now(timezone.utc),
    )
