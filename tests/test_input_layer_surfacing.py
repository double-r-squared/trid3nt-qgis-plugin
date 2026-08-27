"""Input-layer surfacing, post ADR 0244 S2 collapse.

Router-fetched renderable inputs now surface via the emit-on-fetch router seam
(``route()`` -> ``maybe_emit_input_on_fetch``); the per-family ``_surface_*``
helpers + hand-written composer emission call sites were deleted in S2. What
remains here pins the pieces the seam does NOT own:

  (1) ``publish_input_layer`` / ``publish_raster_input_cog`` -- the emission
      PRIMITIVES the seam itself rides: force role + bbox=None, best-effort
      (NEVER raise), honour the emit_layer_uri guardrail (raw-object raster
      DROPPED, vector passes).
  (2) OpenQuake ``make_fault_sources_layer_uri`` + ``fault_records_to_feature_collection``
      -- the fault-trace serialization util (kept, exported; the composer no
      longer hand-surfaces -- ``fetch_fault_sources`` returns a renderable
      FaultSourcesResult the seam publishes).
  (3) river_dye's IN-WORKER bed-bathymetry surfacing -- a worker-COG the router
      seam cannot cover (sampled inside the solver container).
  (SWEEP) the ADR 0244 single-path guard: no ``_surface_*input*`` helper and no
      hand-written input-emission call for router-fetched data may reappear.

Everything I/O-bound (S3 put, publish_layer, the solver chain) is MOCKED -- no
network / boto3 is touched. The per-family composer-surfacing cases were removed;
the seam is now pinned by ``test_emit_on_fetch_seam.py``.
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
    "trid3nt_server.emission.publish.publish_layer"
)
_COG_EXISTS_TARGET = "trid3nt_server.emission.layer_uri_emit._cog_object_exists"


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
    with patch(_COG_EXISTS_TARGET, return_value=True), \
         patch(_PUBLISH_LAYER_TARGET, side_effect=_mock_publish_layer):
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
    from trid3nt_server.emission.publish import (
        PublishLayerError,
    )

    def _boom(*a, **k):
        raise PublishLayerError("PUBLISH_FAILED", "boom")

    emitter = _emitter()
    with patch(_COG_EXISTS_TARGET, return_value=True), \
         patch(_PUBLISH_LAYER_TARGET, side_effect=_boom):
        ok = await publish_raster_input_cog(
            emitter, cog_uri="s3://c/x.tif", layer_id="input-bathymetry-x",
            name="Input: bathymetry (x)", style_preset="continuous_dem",
        )
    assert ok is False
    assert emitter._loaded_layers == []


@pytest.mark.asyncio
async def test_publish_raster_input_cog_skips_missing_object_loudly(caplog):
    """THE HONESTY FIX: the dead-COG class -- a manifest that recorded a
    filename the store never actually received. head_object (mocked) reports
    absent -> the object is SKIPPED before ever reaching publish_layer: no
    404 layer registered, a LOUD warning naming the layer_id + uri, returns
    False. NEVER raises."""
    called = {"n": 0}

    def _spy(*a, **k):  # pragma: no cover - must not run
        called["n"] += 1
        return "s3://x"

    emitter = _emitter()
    with patch(_COG_EXISTS_TARGET, return_value=False), \
         patch(_PUBLISH_LAYER_TARGET, side_effect=_spy), \
         caplog.at_level(
             "WARNING", logger="trid3nt_server.emission.layer_uri_emit"):
        ok = await publish_raster_input_cog(
            emitter, cog_uri="s3://test-runs/RID/bed_bathymetry.tif",
            layer_id="input-river-bed-DEAD", name="Input: river bed bathymetry",
            style_preset="continuous_dem", role="context",
        )

    assert ok is False
    assert emitter._loaded_layers == []
    assert called["n"] == 0  # publish_layer never even called -- no upload race
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "SKIPPING" in msgs
    assert "input-river-bed-DEAD" in msgs
    assert "s3://test-runs/RID/bed_bathymetry.tif" in msgs


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
import trid3nt_server.workflows.openquake.psha.psha as seismic  # noqa: E402
from trid3nt_server.workflows.openquake.psha.psha import (  # noqa: E402
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

    import trid3nt_server.workflows.solver.solver as solver_mod

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
    import trid3nt_server.workflows.solver.solver as solver_mod

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
    import trid3nt_server.workflows.solver.solver as solver_mod

    class _BoomS3:
        def put_object(self, **kw):
            raise RuntimeError("s3 down")

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _BoomS3())
    monkeypatch.setattr(solver_mod, "_get_runs_bucket", lambda: "test-runs")
    assert make_fault_sources_layer_uri([_FAULT_REC], run_id="RID") is None


# ===========================================================================
# (SWEEP) ADR 0244 single-path guard.
#
# After the S2 collapse the emit-on-fetch router seam (route() ->
# maybe_emit_input_on_fetch) is the ONLY way a router-FETCHED renderable input
# surfaces as a role=context "Input:" row. No composer may re-introduce a
# per-family ``_surface_*input*`` helper or a hand-written
# publish_input_layer / publish_raster_input_cog call for router-fetched data.
#
# What legitimately REMAINS (and is allow-listed below, each with its reason) is
# emission the seam does NOT cover:
#   * MESH previews          - the generated mesh is not a router fetch.
#   * RESULT / derived COGs   - a solver's own secondary output layer.
#   * IN-WORKER COGs          - bathymetry sampled inside the solver container,
#                               which never touches route() (telemac3d bottom,
#                               swan bathy). No TELEMAC domain is in this class
#                               any more: every bed the family solves on is a
#                               declared router fetch and the seam surfaces it.
#   * BARE-OSM fetches        - agitation's breakwaters bypass the router (an
#                               S3 loose end, ADR 0244 S3).
#   * USER-DATA overlays      - a point/vector built from a user-supplied
#                               location (MODFLOW well / observed heads).
#
# A NEW input-emission site fails this test: route the fetch through the seam
# (its render declaration surfaces it for free) or, if it is genuinely one of
# the exempt classes above, add it here WITH a reason.
# ===========================================================================
import pathlib  # noqa: E402
import re  # noqa: E402

_WORKFLOWS_DIR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "trid3nt_server" / "workflows"
)

# relpath (from workflows/) -> (n_input_emission_calls, reason). Sum is the only
# input-emission the tree is allowed to keep post-collapse.
_ALLOWLISTED_INPUT_EMISSION: dict[str, tuple[int, str]] = {
    "geoclaw/inundation/inundation.py": (2, "mesh preview + particle result"),
    "hecras/flood_2d/flood_2d.py": (1, "mesh preview"),
    "hecras/levee_breach/levee_breach.py": (1, "mesh preview"),
    "hecras/riverine_flood/riverine_flood.py": (1, "mesh preview"),
    "mesh/gate.py": (1, "the mesh under construction, presented at the gate as an editable MDAL layer - an AUTHORED domain, not a router fetch, so no emit-on-fetch seam can cover it; one home for every mesher's presentation"),
    "modflow/capture_zone/capture_zone.py": (2, "observed-wells user-data overlay + backward-PRT pathlines (result-derived context, not a router fetch)"),
    "modflow/thermal_plume/thermal_plume.py": (1, "injection-well user-data point"),
    "openquake/scenario_gmf/scenario_gmf.py": (1, "computed GMF-spread result COG"),
    "openquake/secondary_perils/secondary_perils.py": (1, "computed landslide result COG"),
    "schism/baroclinic_circulation/baroclinic_circulation.py": (1, "bottom-salinity result (mesh now rides the seam, ADR 0286)"),
    "schism/pahm_surge/pahm_surge.py": (1, "storm best-track result (mesh now rides the seam, ADR 0286)"),
    "schism/results_mesh_seam.py": (1, "the seam-side out2d/salinity mesh publisher - framework emission, one home for all schism legs (ADR 0286)"),
    "sfincs/flood/flood.py": (1, "mesh preview"),
    "swan/wave_field/wave_field.py": (1, "in-worker bathymetry COG"),
    "telemac/steps/coastal.py": (1, "the CONTEXT half of the coastal inundation split - the total water-depth field beside the answer layer. A SOLVED product, not a fetch, so no emit-on-fetch seam can cover it; surfaced through this seam so it lands as role=context and never fights the answer for the camera (TELEMAC wave B)"),
    "telemac/release_layer.py": (1, "the seam-side release/outfall point publisher - a resolved PARAM (drawn or derived), not a router fetch, so no emit-on-fetch seam can cover it; one home for all telemac legs"),
    "telemac/results_mesh_seam.py": (1, "the seam-side SELAFIN mesh publisher - framework emission, one home for all telemac legs"),
    "telemac/steps/forcing.py": (1, "NWM discharge station point, its name pinned to the RESOLVED cycle for its caption, which the fetch (visualize=False) never exposes to the generic seam (ADR 0309)"),
    "telemac/steps/mesh_preview.py": (1, "the approve-mesh wireframe preview"),
    "telemac/steps/products.py": (3, "deposition + oil-slick + DO-field results"),
    "telemac/steps/stratified.py": (1, "the TELEMAC-3D BOTTOM companion layer - the surface layer rides the dispatch seam, so only its pair-mate is emitted here"),
}

# NONE survive. The last bespoke input-surfacing helper rode an in-worker bed COG
# the router seam could not cover; the reach family's bed is a declared fetch now,
# so the helper and its COG are both gone and the seam is the only path.
_ALLOWLISTED_SURFACE_HELPERS: set[str] = set()

# Immediate paren only (a real call ``name(...``); a prose mention like
# "publish_input_layer (never raises)" has a space and must NOT count.
_EMISSION_CALL = re.compile(r"\b(publish_input_layer|publish_raster_input_cog)\(")
_SURFACE_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(_surface_\w*input\w*)\s*\(", re.M)


def _iter_workflow_py() -> list[pathlib.Path]:
    return [p for p in _WORKFLOWS_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def test_sweep_no_surface_input_helpers_except_worker_cog():
    """No per-family ``_surface_*input*`` helper may be (re)introduced -- the seam
    surfaces fetched inputs. The sole exception is the in-worker bed-COG helper."""
    offenders: list[str] = []
    for path in _iter_workflow_py():
        for name in _SURFACE_DEF.findall(path.read_text(encoding="utf-8")):
            if name not in _ALLOWLISTED_SURFACE_HELPERS:
                offenders.append(f"{path.relative_to(_WORKFLOWS_DIR)}::{name}")
    assert not offenders, (
        "router-fetched inputs surface via the emit-on-fetch seam (ADR 0244); "
        "delete these hand-written _surface_*input* helpers:\n  "
        + "\n  ".join(offenders)
    )


def test_sweep_input_emission_calls_match_allowlist():
    """Every hand-written publish_input_layer / publish_raster_input_cog CALL in a
    composer must be an allow-listed non-seam-covered emission (mesh / result /
    in-worker COG / bare-OSM / user-data overlay). A new site -> route the fetch
    through the seam, or allow-list it here with a reason."""
    found: dict[str, int] = {}
    for path in _iter_workflow_py():
        n = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith(("import ", "from ", "#")):
                continue
            n += len(_EMISSION_CALL.findall(line))
        if n:
            found[str(path.relative_to(_WORKFLOWS_DIR))] = n

    expected = {rel: cnt for rel, (cnt, _reason) in _ALLOWLISTED_INPUT_EMISSION.items()}
    unexpected = {
        rel: cnt for rel, cnt in found.items()
        if rel not in expected or cnt != expected[rel]
    }
    missing = {
        rel: cnt for rel, cnt in expected.items()
        if found.get(rel, 0) != cnt
    }
    assert not unexpected and not missing, (
        "input-emission call sites drifted from the ADR 0244 allow-list.\n"
        f"unexpected/changed (route through the seam or allow-list w/ reason): {unexpected}\n"
        f"allow-listed but not found (update the allow-list): {missing}"
    )
