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

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts import new_ulid

from trid3nt_server.emission.layer_uri_emit import (
    publish_input_layer,
    publish_raster_input_cog,
)
from trid3nt_server.emission.pipeline_emitter import (
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
        "trid3nt_server.emission.pipeline_emitter._read_vector_uri_as_geojson",
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
# (1b) publish_raster_input_cog -- the EXISTING-COG raster input seam
#      (ADR 0227: the bathymetry-consuming coastal templates surface their
#      fetched topobathy the same way the flood DEM path does).
# ===========================================================================
_PUBLISH_LAYER_TARGET = (
    "trid3nt_server.agent.tools.publish_layer.publish_layer.publish_layer"
)


@pytest.mark.asyncio
async def test_publish_raster_input_cog_surfaces_with_provenance():
    """An existing s3:// COG rounds through publish_layer (mocked) and reaches
    the emitter as a role="context" raster carrying the provenance name + the
    continuous_dem ramp. This is the 0217-lesson gate: a valid input LayerURI
    MUST reach the emitter, never silently drop."""
    published: list[dict] = []

    def _mock_publish_layer(layer_uri, layer_id, style_preset, name=None, **kw):  # noqa: ANN001
        published.append(
            {"layer_uri": layer_uri, "layer_id": layer_id,
             "style_preset": style_preset, "name": name}
        )
        return "s3://test-runs/RID/input-bathymetry.tif"

    emitter = _emitter()
    with patch(_PUBLISH_LAYER_TARGET, side_effect=_mock_publish_layer):
        ok = await publish_raster_input_cog(
            emitter,
            cog_uri="s3://test-cache/topobathy/aoi.tif",
            layer_id="input-bathymetry-RID",
            name='Input: bathymetry (topobathy, native CUDEM 1/9")',
            style_preset="continuous_dem",
        )

    assert ok is True
    # It rode the EXISTING object (no re-upload): the cog_uri went straight to
    # publish_layer as the layer_uri.
    assert len(published) == 1
    assert published[0]["layer_uri"] == "s3://test-cache/topobathy/aoi.tif"
    assert published[0]["style_preset"] == "continuous_dem"
    # The valid LayerURI reached the emitter (cannot silently drop).
    assert len(emitter._loaded_layers) == 1
    row = emitter._loaded_layers[0]
    assert row.role == "context"
    assert row.layer_type == "raster"
    assert row.style_preset == "continuous_dem"
    assert row.name.startswith("Input: bathymetry (")
    assert row.uri == "s3://test-runs/RID/input-bathymetry.tif"


@pytest.mark.asyncio
async def test_publish_raster_input_cog_publish_failure_non_fatal():
    """A publish_layer PublishLayerError is swallowed (best-effort): returns
    False, surfaces nothing, NEVER raises -- a failed input can never fail the
    solve."""
    from trid3nt_server.agent.tools.publish_layer.publish_layer import (
        PublishLayerError,
    )

    def _boom(*a, **k):
        raise PublishLayerError("PUBLISH_FAILED", "boom")

    emitter = _emitter()
    with patch(_PUBLISH_LAYER_TARGET, side_effect=_boom):
        ok = await publish_raster_input_cog(
            emitter, cog_uri="s3://c/x.tif", layer_id="input-bathymetry-x",
            name="Input: bathymetry (x)", style_preset="continuous_dem",
        )
    assert ok is False
    assert emitter._loaded_layers == []


@pytest.mark.asyncio
async def test_publish_raster_input_cog_none_emitter_or_uri_noop():
    """No emitter bound OR a falsy cog_uri -> no-op, returns False, no raise,
    and publish_layer is never even called."""
    called = {"n": 0}

    def _spy(*a, **k):  # pragma: no cover - must not run
        called["n"] += 1
        return "s3://x"

    with patch(_PUBLISH_LAYER_TARGET, side_effect=_spy):
        assert await publish_raster_input_cog(
            None, cog_uri="s3://c/x.tif", layer_id="i", name="n",
            style_preset="continuous_dem",
        ) is False
        assert await publish_raster_input_cog(
            _emitter(), cog_uri="", layer_id="i", name="n",
            style_preset="continuous_dem",
        ) is False
    assert called["n"] == 0


# ===========================================================================
# (2) OpenQuake fault serialization + composer wiring.
# ===========================================================================
import trid3nt_server.agent.workflows.openquake.psha.psha as seismic  # noqa: E402
from trid3nt_server.agent.workflows.openquake.psha.psha import (  # noqa: E402
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

    import trid3nt_server.agent.tools.simulation.solver.solver as solver_mod

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
    import trid3nt_server.agent.tools.simulation.solver.solver as solver_mod

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
    import trid3nt_server.agent.tools.simulation.solver.solver as solver_mod

    class _BoomS3:
        def put_object(self, **kw):
            raise RuntimeError("s3 down")

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _BoomS3())
    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "test-runs")
    assert make_fault_sources_layer_uri([_FAULT_REC], run_id="RID") is None


# --- composer end-to-end (mocked): emits a role="input" fault vector ONLY when
#     real faults were used; nothing extra when no real faults. ---------------
from trid3nt_contracts.openquake_contracts import OpenQuakeRunArgs  # noqa: E402
from trid3nt_contracts.openquake_contracts import SeismicHazardLayerURI  # noqa: E402
from trid3nt_server.agent.workflows.openquake.postprocess_openquake import (  # noqa: E402
    SEISMIC_HAZARD_STYLE_PRESET,
)
from trid3nt_server.agent.workflows.openquake.psha.psha import (  # noqa: E402
    assemble_build_spec,
)

_BBOX = (-122.55, 37.45, -122.15, 37.90)


def _fault_result(faults, note=None):
    return {
        "catalog": "gem", "bbox": list(_BBOX), "fault_count": len(faults),
        "faults": faults, "note": note, "source": "GEM",
    }


def _patch_fetch(*, return_value=None):
    """Swap the fetch_fault_sources REGISTRY SEAM (folded, ADR 0081) -- the consumer
    resolves TOOL_REGISTRY["fetch_fault_sources"].fn (a frozen RegisteredTool)."""
    import dataclasses

    from unittest.mock import MagicMock, patch

    from trid3nt_server.agent.tools import TOOL_REGISTRY

    mock = MagicMock(return_value=return_value)
    entry = dataclasses.replace(TOOL_REGISTRY["fetch_fault_sources"], fn=mock)
    return patch.dict(TOOL_REGISTRY, {"fetch_fault_sources": entry}), mock


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

    import trid3nt_server.agent.tools.simulation.solver.solver as solver_mod

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
    _wire_seismic_mocks(monkeypatch)
    emitter = _emitter()
    token = _CURRENT_EMITTER.set(emitter)
    fetch_cm, _ = _patch_fetch(return_value=_fault_result([_FAULT_REC]))
    try:
        with fetch_cm, patch(
            "trid3nt_server.emission.pipeline_emitter._read_vector_uri_as_geojson",
            return_value=fault_records_to_feature_collection([_FAULT_REC]),
        ):
            await seismic.model_openquake_psha(
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
    _wire_seismic_mocks(monkeypatch)
    emitter = _emitter()
    token = _CURRENT_EMITTER.set(emitter)
    fetch_cm, _ = _patch_fetch(return_value=_fault_result([], note="No GEM faults in this AOI."))
    try:
        with fetch_cm:
            await seismic.model_openquake_psha(
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
import trid3nt_server.agent.workflows.sfincs.flood.flood as flood  # noqa: E402
from trid3nt_server.agent.workflows.sfincs.flood.flood import model_flood_scenario  # noqa: E402


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


def _river_geometry_patch(return_value: LayerURI | None = None):
    """Patch the fetch_river_geometry registry seam (ADR 0074).

    flood.py no longer imports the twin directly -- it resolves
    ``TOOL_REGISTRY["fetch_river_geometry"].fn`` at call time. RegisteredTool
    is frozen, so swap the whole entry for one carrying a stub fn (mirrors
    ``_patch_copernicus_seam`` in test_data_fetch.py).
    """
    from trid3nt_server.agent.tools import TOOL_REGISTRY, RegisteredTool

    layer = return_value if return_value is not None else _flood_input_layer("rivers")
    orig = TOOL_REGISTRY["fetch_river_geometry"]
    return patch.dict(
        TOOL_REGISTRY,
        {
            "fetch_river_geometry": RegisteredTool(
                metadata=orig.metadata, fn=lambda **_kw: layer, module=orig.module
            )
        },
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
        output_uri = f"s3://trid3nt-runs/{_rid}/"
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
            _river_geometry_patch(),
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
                "trid3nt_server.emission.pipeline_emitter._read_vector_uri_as_geojson",
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
from trid3nt_server.agent.workflows.swmm.urban_flood.urban_flood import (  # noqa: E402
    make_buildings_input_layer_uri,
)


def test_make_buildings_input_layer_uri_uploads_role_input(monkeypatch):
    """A buildings FeatureCollection uploads to the runs bucket + returns a
    role="input" vector LayerURI (bbox=None). S3 mocked."""
    import trid3nt_server.agent.tools.simulation.solver.solver as solver_mod

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
    import trid3nt_server.agent.tools.simulation.solver.solver as solver_mod

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
from trid3nt_contracts.execution import ExecutionHandle  # noqa: E402


def ExecutionHandle_helper(run_id: str) -> ExecutionHandle:
    return ExecutionHandle(
        handle_id=new_ulid(),
        run_id=run_id,
        solver="sfincs",
        compute_class="standard",
        workflows_execution_id="projects/t/locations/us/workflows/w/executions/e",
        workflow_name="model_flood_scenario",
        workflow_location="us-central1",
        submitted_at=datetime.now(timezone.utc),
    )


# ===========================================================================
# (5) ADR 0231 input-layer parity: the TELEMAC family surfaces the fetched
#     river geometry (river_dye) + the mesh DEM bed + river flowline
#     (rain_on_grid) as role="context" Case inputs -- never hidden layers.
#     The cannot-silently-drop gate: a valid input LayerURI MUST reach the
#     emitter. publish_layer + current_emitter are mocked; no network.
# ===========================================================================
import trid3nt_server.agent.workflows.telemac.river_dye.river_dye as river_dye  # noqa: E402
import trid3nt_server.agent.workflows.telemac.rain_on_grid.rain_on_grid as rog  # noqa: E402


@pytest.mark.asyncio
async def test_river_dye_surfaces_fetched_river_layer_as_context():
    """river_dye surfaces the FETCHED river-geometry LayerURI (the fetch path)
    as a role="context" vector carrying a provenance name (re-keyed off the
    result). Rides the inline s3:// FlatGeobuf -- no publish_layer round-trip."""
    fetched = LayerURI(
        layer_id="river-fetch-raw", name="Fetch river geometry",
        layer_type="vector", uri="s3://test-cache/river_geometry/snake.fgb",
        style_preset="osm_waterways", role="input",
    )
    emitter = _emitter()
    with patch(
        "trid3nt_server.emission.pipeline_emitter._read_vector_uri_as_geojson",
        return_value={"type": "FeatureCollection", "features": []},
    ):
        ok = await river_dye._surface_river_geometry_input(
            emitter, fetched, str(fetched.uri), "Snake River near Twin Falls, Idaho"
        )
    assert ok is True
    assert len(emitter._loaded_layers) == 1
    row = emitter._loaded_layers[0]
    assert row.role == "context"
    assert row.layer_type == "vector"
    assert row.uri == "s3://test-cache/river_geometry/snake.fgb"
    assert row.name.startswith("Input: river geometry (")
    assert row.layer_id.startswith("input-river-geometry-")


@pytest.mark.asyncio
async def test_river_dye_surfaces_prefetched_river_uri_as_context():
    """The prefetched path (only an s3:// uri string, no LayerURI) still surfaces
    the flowline as a role="context" vector (osm_waterways preset)."""
    emitter = _emitter()
    with patch(
        "trid3nt_server.emission.pipeline_emitter._read_vector_uri_as_geojson",
        return_value={"type": "FeatureCollection", "features": []},
    ):
        ok = await river_dye._surface_river_geometry_input(
            emitter, None, "s3://runs/r/reach.fgb", "Eel River"
        )
    assert ok is True
    row = emitter._loaded_layers[0]
    assert row.role == "context"
    assert row.style_preset == "osm_waterways"
    assert row.uri == "s3://runs/r/reach.fgb"


@pytest.mark.asyncio
async def test_river_dye_river_input_none_emitter_and_bad_uri_noop():
    """No emitter -> no-op False; a non-object prefetch string surfaces nothing;
    NEVER raises."""
    assert await river_dye._surface_river_geometry_input(
        None, None, "s3://r/x.fgb", "X"
    ) is False
    emitter = _emitter()
    assert await river_dye._surface_river_geometry_input(
        emitter, None, "fetch_river_geometry(...)", "X"
    ) is False
    assert emitter._loaded_layers == []


class _FakeWatershedMesh:
    """Minimal WatershedMesh stand-in carrying only the ADR 0231 input uris."""

    def __init__(self, dem_uri=None, river_uri=None):
        self.dem_input_s3_uri = dem_uri
        self.river_input_s3_uri = river_uri


@pytest.mark.asyncio
async def test_rain_on_grid_surfaces_dem_and_river_as_context():
    """rain_on_grid surfaces the mesh's fetched DEM bed (publish_raster_input_cog,
    publish_layer mocked) + river flowline (publish_input_layer) as role="context"
    Case inputs -- both reach the emitter (cannot silently drop)."""
    mesh = _FakeWatershedMesh(
        dem_uri="s3://test-cache/dem/otto.tif",
        river_uri="s3://test-cache/river_geometry/otto.fgb",
    )
    emitter = _emitter()
    token = _CURRENT_EMITTER.set(emitter)

    def _mock_publish_layer(layer_uri, layer_id, style_preset, name=None, **kw):  # noqa: ANN001
        return f"s3://test-runs/RID/{layer_id}.tif"

    try:
        with (
            patch(_PUBLISH_LAYER_TARGET, side_effect=_mock_publish_layer),
            patch(
                "trid3nt_server.emission.pipeline_emitter._read_vector_uri_as_geojson",
                return_value={"type": "FeatureCollection", "features": []},
            ),
        ):
            await rog._surface_watershed_mesh_inputs(mesh, "Otto, North Carolina")
    finally:
        _CURRENT_EMITTER.reset(token)

    rows = {l.layer_id.rsplit("-", 1)[0]: l for l in emitter._loaded_layers}
    assert "input-dem" in rows, rows
    assert "input-river-geometry" in rows, rows
    dem_row = rows["input-dem"]
    assert dem_row.role == "context"
    assert dem_row.layer_type == "raster"
    assert dem_row.style_preset == "continuous_dem"
    assert dem_row.name.startswith("Input: DEM bed (")
    river_row = rows["input-river-geometry"]
    assert river_row.role == "context"
    assert river_row.layer_type == "vector"
    assert river_row.uri == "s3://test-cache/river_geometry/otto.fgb"
    assert river_row.name.startswith("Input: river geometry (")


@pytest.mark.asyncio
async def test_rain_on_grid_supplied_mesh_no_uris_is_noop():
    """A user-supplied mesh carries neither input uri -> nothing surfaced, no
    raise (best-effort)."""
    emitter = _emitter()
    token = _CURRENT_EMITTER.set(emitter)
    try:
        await rog._surface_watershed_mesh_inputs(_FakeWatershedMesh(), "X")
    finally:
        _CURRENT_EMITTER.reset(token)


# ===========================================================================
# ADR 0231: the in-worker river bed bathymetry (the row NATE explicitly named).
# The worker samples + fits the bed inside the container and writes it as a 4326
# COG (bed_bathymetry.tif) + records bed_cog in telemac_metrics.json; the composer
# rides that object through publish_raster_input_cog as a role=context input.
# ===========================================================================
@pytest.mark.asyncio
async def test_river_dye_surfaces_in_worker_bed_bathymetry_as_context(monkeypatch):
    """The bed COG the worker recorded in the result envelope reaches the emitter
    as a role="context" continuous_dem raster with a provenance name (cannot
    silently drop the in-worker bed NATE asked to visualize)."""
    from trid3nt_server.agent.tools.simulation.solver import solver as solver_mod

    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "test-runs")
    emitter = _emitter()

    def _mock_publish_layer(layer_uri, layer_id, style_preset, name=None, **kw):  # noqa: ANN001
        assert layer_uri == "s3://test-runs/RID/bed_bathymetry.tif"
        return layer_uri  # raw s3 COG passes the emit guardrail (plugin /vsicurl/)

    with patch(_PUBLISH_LAYER_TARGET, side_effect=_mock_publish_layer):
        ok = await river_dye._surface_bed_bathymetry_input(
            emitter,
            {"bed_cog": "bed_bathymetry.tif", "bed_cog_source": "usgs-3dep"},
            "RID",
            "Snake River near Twin Falls, Idaho",
        )

    assert ok is True
    assert len(emitter._loaded_layers) == 1
    row = emitter._loaded_layers[0]
    assert row.role == "context"
    assert row.layer_type == "raster"
    assert row.style_preset == "continuous_dem"
    assert row.uri == "s3://test-runs/RID/bed_bathymetry.tif"
    assert row.name.startswith("Input: river bed bathymetry (")
    assert "USGS 3DEP" in row.name
    assert row.layer_id.startswith("input-river-bed-")


@pytest.mark.asyncio
async def test_river_dye_bed_bathymetry_absent_key_and_none_emitter_noop():
    """No bed_cog key in the envelope (older image / write failed) surfaces
    nothing; a None emitter is a no-op -- both NEVER raise."""
    assert await river_dye._surface_bed_bathymetry_input(
        None, {"bed_cog": "bed_bathymetry.tif"}, "RID", "X"
    ) is False
    emitter = _emitter()
    ok = await river_dye._surface_bed_bathymetry_input(emitter, {}, "RID", "X")
    assert ok is False
    assert emitter._loaded_layers == []
    assert emitter._loaded_layers == []


# ===========================================================================
# ADR 0231 broad adoption -- per-family cannot-silently-drop pins.
# ===========================================================================
@pytest.mark.asyncio
async def test_landlab_surfaces_staged_dem_as_context(monkeypatch):
    """The ONE _composer_common adoption that lights all 13 Landlab templates:
    the staged DEM reaches the emitter as a role=context continuous_dem raster."""
    import trid3nt_server.agent.workflows.landlab._composer_common as cc

    emitter = _emitter()

    def _mock_publish_layer(layer_uri, layer_id, style_preset, name=None, **kw):  # noqa: ANN001
        return layer_uri  # raw s3 COG passes the guardrail

    with patch(_PUBLISH_LAYER_TARGET, side_effect=_mock_publish_layer):
        ok = await cc._surface_landlab_dem_input(
            emitter, "s3://cache/landlab_setup/RID/dem.tif", "USGS 3DEP 1m LiDAR")

    assert ok is True
    row = emitter._loaded_layers[0]
    assert row.role == "context" and row.layer_type == "raster"
    assert row.style_preset == "continuous_dem"
    assert row.name == "Input: DEM (USGS 3DEP 1m LiDAR)"
    assert row.layer_id.startswith("input-dem-")


@pytest.mark.asyncio
async def test_landlab_dem_none_uri_and_emitter_noop(monkeypatch):
    """No DEM uri / no emitter -> nothing surfaced; NEVER raises."""
    import trid3nt_server.agent.workflows.landlab._composer_common as cc

    assert await cc._surface_landlab_dem_input(None, "s3://x/d.tif", "src") is False
    emitter = _emitter()
    assert await cc._surface_landlab_dem_input(emitter, None, "src") is False
    assert emitter._loaded_layers == []


@pytest.mark.asyncio
async def test_telemac_rog_surfaces_nlcd_landcover_as_context(monkeypatch):
    """rain_on_grid surfaces the fetched NLCD land cover (the per-node CN2/Manning
    the infiltration derives from) as a role=context categorical input."""
    import trid3nt_server.agent.workflows.telemac.rain_on_grid.rain_on_grid as rog

    emitter = _emitter()
    token = _CURRENT_EMITTER.set(emitter)

    def _mock_publish_layer(layer_uri, layer_id, style_preset, name=None, **kw):  # noqa: ANN001
        return layer_uri

    try:
        with patch(_PUBLISH_LAYER_TARGET, side_effect=_mock_publish_layer):
            await rog._surface_landcover_input(
                "s3://cache/nlcd/otto.tif", "Otto, North Carolina")
    finally:
        _CURRENT_EMITTER.reset(token)

    row = emitter._loaded_layers[0]
    assert row.role == "context" and row.layer_type == "raster"
    assert row.style_preset == "categorical_landcover"
    assert row.name.startswith("Input: land cover (")


@pytest.mark.asyncio
async def test_telemac_rog_landcover_none_uri_noop():
    """No NLCD uri -> nothing surfaced (best-effort), no raise."""
    import trid3nt_server.agent.workflows.telemac.rain_on_grid.rain_on_grid as rog

    emitter = _emitter()
    token = _CURRENT_EMITTER.set(emitter)
    try:
        await rog._surface_landcover_input(None, "X")
    finally:
        _CURRENT_EMITTER.reset(token)
    assert emitter._loaded_layers == []


def test_swmm_fetch_dem_invokes_uri_sink(monkeypatch):
    """SWMM _fetch_dem_for_urban feeds the fetched DEM s3 uri to uri_sink (so the
    composer surfaces the terrain) -- cannot silently drop the DEM."""
    import trid3nt_server.agent.workflows.swmm.urban_flood.urban_flood as uf
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    class _Layer:
        uri = "s3://cache/dem/urban.tif"

    class _Entry:
        fn = staticmethod(lambda **kw: _Layer())

    monkeypatch.setitem(TOOL_REGISTRY, "fetch_3dep_extra", _Entry())
    monkeypatch.setattr(uf, "_localize_to_dem_path", lambda uri: "/tmp/dem.tif")
    captured: list[str] = []
    path, source = uf._fetch_dem_for_urban((-1.0, -1.0, 1.0, 1.0), captured.append)
    assert captured == ["s3://cache/dem/urban.tif"]
    assert path == "/tmp/dem.tif"


def test_swmm_fetch_dem_none_sink_is_noop(monkeypatch):
    """A None uri_sink (every legacy caller) never errors -- byte-identical."""
    import trid3nt_server.agent.workflows.swmm.urban_flood.urban_flood as uf
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    class _Layer:
        uri = "s3://cache/dem/urban.tif"

    class _Entry:
        fn = staticmethod(lambda **kw: _Layer())

    monkeypatch.setitem(TOOL_REGISTRY, "fetch_3dep_extra", _Entry())
    monkeypatch.setattr(uf, "_localize_to_dem_path", lambda uri: "/tmp/dem.tif")
    path, source = uf._fetch_dem_for_urban((-1.0, -1.0, 1.0, 1.0))  # no sink
    assert path == "/tmp/dem.tif"


def test_openquake_secondary_perils_fetch_dem_invokes_uri_sink(monkeypatch):
    """secondary_perils _fetch_dem_local feeds the fetched DEM s3 uri to uri_sink
    (the vs30/slope/CTI covariates derive from it) -- cannot silently drop."""
    import trid3nt_server.agent.workflows.openquake.secondary_perils.secondary_perils as sp
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    class _Layer:
        uri = "s3://cache/dem/sep.tif"

    class _Entry:
        fn = staticmethod(lambda **kw: _Layer())

    monkeypatch.setitem(TOOL_REGISTRY, "fetch_copernicus_dem", _Entry())
    monkeypatch.setattr(
        sp, "read_object_bytes_s3", lambda uri: b"", raising=False)
    # patch the cache import target too (imported lazily inside the function)
    import trid3nt_server.agent.tools.cache as cache_mod
    monkeypatch.setattr(cache_mod, "read_object_bytes_s3", lambda uri: b"tiff")
    import os, tempfile
    tmpdir = tempfile.mkdtemp()
    captured: list[str] = []
    sp._fetch_dem_local((-1.0, -1.0, 1.0, 1.0), tmpdir, captured.append)
    assert captured == ["s3://cache/dem/sep.tif"]
