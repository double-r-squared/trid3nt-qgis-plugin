"""End-to-end LOCAL-lane proof for the PySWMM quasi-2D urban-flood engine
(sprint-16 P4, Path A).

Exercises the full LOCAL chain on a SMALL SYNTHETIC AOI (a tilted-plane DEM with
a central pit + two building footprints + a tagged RED-wall / GREEN-flap-gate
barrier FeatureCollection + a synthetic nested hyetograph via the design-storm
depth) WITHOUT any live network fetch:

    build_and_stage_swmm_deck (build_swmm_mesh, P2)
      -> run_swmm_local (pyswmm IN-PROCESS, the dev primary path, P4)
      -> postprocess_swmm (rasterize node depths -> peak + frames, P3)
      -> model_swmm_urban_flood composer (peak returned + frames emitted
         out-of-band via a fake emitter)
      -> swmm_urban_flood tool (SWMMRunArgs coercion + typed-error surface)

This is the P4 acceptance: a REAL solved ``.out`` produces a peak primary
``SWMMDepthLayerURI`` + a contiguous "Flood depth step N" animation frame group,
end to end. Solver registration + the SWMM ``LocalSolverSpec`` are pinned too.

pyswmm + swmm-api + rasterio are required; the heavy E2E tests skip if absent.
The lightweight registration tests need none of them.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import pytest

# --- Lightweight registration tests (no SWMM dep) ------------------------- #

_WEB_STEP_TOKEN_RE = re.compile(r"\b(?:step|frame|idx|index)\s*\+?(\d{1,4})\b", re.I)


def test_swmm_urban_flood_registered_and_typed_error():
    """The LLM-facing tool is registered + returns a typed error dict (never
    raises) on a missing/invalid bbox."""
    import asyncio

    import trid3nt_server.tools as T
    from trid3nt_server.workflows.swmm.urban_flood.urban_flood import swmm_urban_flood

    assert "swmm_urban_flood" in T.TOOL_REGISTRY

    # No bbox -> typed error dict, not a raise.
    out = asyncio.run(swmm_urban_flood(bbox=None))
    assert out["status"] == "error"
    assert out["error_code"] == "SWMM_PARAMS_INCOMPLETE"

    # A non-bbox string -> typed invalid-params error.
    out2 = asyncio.run(swmm_urban_flood(bbox="not-a-bbox"))
    assert out2["status"] == "error"
    assert out2["error_code"] == "SWMM_PARAMS_INVALID"


def test_swmm_urban_flood_obstacles_alias_does_not_trip_params_invalid(monkeypatch):
    """The LLM-invented 'obstacles' building_representation normalizes to 'drop'
    in SWMMRunArgs (contract alias validator) so the tool's FIRST attempt does
    NOT return SWMM_PARAMS_INVALID -> it proceeds into the workflow (no visible
    self-correcting retry loop). We stub the composer to capture the normalized
    run_args without running the heavy solver chain."""
    import asyncio

    from trid3nt_server.workflows.swmm.urban_flood import urban_flood as RT

    captured: dict = {}

    async def _fake_composer(run_args, **kwargs):  # noqa: ANN001, ANN003
        captured["run_args"] = run_args
        # short-circuit with a typed error AFTER capture (no solver needed); the
        # tool maps it to an error dict, but we only assert on the run_args.
        raise RT.UrbanFloodWorkflowError("URBAN_STUB", "stub: no solve in unit test")

    monkeypatch.setattr(RT, "model_swmm_urban_flood", _fake_composer)

    out = asyncio.run(
        RT.swmm_urban_flood(
            bbox=[-85.32, 35.02, -85.28, 35.06],
            building_representation="obstacles",
        )
    )
    # The composer was reached with a NORMALIZED run_args (alias -> "drop"); the
    # early SWMM_PARAMS_INVALID guard did NOT fire on "obstacles".
    assert "run_args" in captured, out
    assert captured["run_args"].building_representation == "drop"
    # The tool surfaces the stub workflow error (NOT SWMM_PARAMS_INVALID).
    assert out["status"] == "error"
    assert out["error_code"] == "URBAN_STUB"


def test_swmm_urban_flood_bogus_building_representation_is_params_invalid(monkeypatch):
    """A genuinely-bogus building_representation still trips the honest
    SWMM_PARAMS_INVALID guard (the alias map does not silently coerce it) and the
    composer is never reached."""
    import asyncio

    from trid3nt_server.workflows.swmm.urban_flood import urban_flood as RT

    reached = {"composer": False}

    async def _fake_composer(run_args, **kwargs):  # noqa: ANN001, ANN003
        reached["composer"] = True
        raise AssertionError("composer must not be reached for a bogus param")

    monkeypatch.setattr(RT, "model_swmm_urban_flood", _fake_composer)

    out = asyncio.run(
        RT.swmm_urban_flood(
            bbox=[-85.32, 35.02, -85.28, 35.06],
            building_representation="bananas",
        )
    )
    assert reached["composer"] is False
    assert out["status"] == "error"
    assert out["error_code"] == "SWMM_PARAMS_INVALID"


# --- Heavy end-to-end chain (needs pyswmm + swmm-api + rasterio) ---------- #
swmm_api = pytest.importorskip("swmm_api")
pyswmm = pytest.importorskip("pyswmm")
rasterio = pytest.importorskip("rasterio")

from trid3nt_contracts.swmm_contracts import SWMMDepthLayerURI, SWMMRunArgs  # noqa: E402
from trid3nt_server.workflows.swmm.urban_flood.urban_flood import (  # noqa: E402
    model_swmm_urban_flood,
)
from trid3nt_server.workflows.swmm.run_swmm import (  # noqa: E402
    build_and_stage_swmm_deck,
    run_swmm_local,
)


@pytest.fixture(autouse=True)
def _stub_overland_manning_resolution(monkeypatch):
    """law 9 (ADR 0285 P4): the composer DERIVES overland Manning's n from NLCD (or
    REFUSES). Stub it offline so the full-chain composer tests never hit the MRLC
    fetch; a real friction value (0.03 / user) still flows to the deck."""
    from trid3nt_contracts.common import SyntheticInput
    from trid3nt_server.workflows.shared.roughness_resolve import ManningResolution
    import trid3nt_server.workflows.swmm.urban_flood.urban_flood as _uf

    async def _fake_resolve(bbox, user_manning, *, param_name, units="s/m^(1/3)", **_kw):
        supplied = user_manning is not None
        n = float(user_manning) if supplied else 0.03
        return ManningResolution(
            manning_n=n,
            source="user_supplied" if supplied else "nlcd_area_weighted",
            entry=SyntheticInput(
                param=param_name, value=round(n, 4), units=units,
                basis="user" if supplied else "derived", consequence="physics",
                real_source_if_any=(None if supplied
                                    else "fetch_landcover (NLCD area-weighted Manning's n)"),
                note="test-stubbed overland Manning's n.",
            ),
            meta={"source": "test_stub"},
        )

    monkeypatch.setattr(_uf, "resolve_overland_manning", _fake_resolve)


_N = 16  # small grid -> fast solve
_CELL = 10.0
_EPSG = 32616  # UTM 16N (valid projected metres)
_OX, _OY = 500000.0, 4000000.0


def _write_dem_geotiff(path: Path) -> None:
    """Tilted plane draining to the low corner + a central pit (P0-spike shape)."""
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    ii, jj = np.meshgrid(np.arange(_N), np.arange(_N), indexing="ij")
    plane = 30.0 - 0.02 * _CELL * (ii + jj)
    ci = cj = (_N - 1) / 2.0
    pit = 2.0 * np.exp(-((ii - ci) ** 2 + (jj - cj) ** 2) / (2.0 * 3.0**2))
    dem = (plane - pit).astype("float32")
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "height": _N,
        "width": _N,
        "crs": CRS.from_epsg(_EPSG),
        "transform": from_origin(_OX, _OY, _CELL, _CELL),
        "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(dem, 1)


def _cell_lonlat(i: int, j: int) -> tuple[float, float]:
    """Centroid (lon, lat) of grid cell (i, j) in EPSG:4326."""
    from rasterio.transform import from_origin, xy
    from rasterio.warp import transform as warp_transform

    t = from_origin(_OX, _OY, _CELL, _CELL)
    x, y = xy(t, i, j)
    lons, lats = warp_transform(f"EPSG:{_EPSG}", "EPSG:4326", [x], [y])
    return lons[0], lats[0]


def _footprint_over_cell(i: int, j: int) -> dict:
    """A small WGS84 building polygon centered on cell (i, j)."""
    lon, lat = _cell_lonlat(i, j)
    d = 0.00004
    ring = [
        [lon - d, lat - d], [lon + d, lat - d],
        [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d],
    ]
    return {"type": "Feature", "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


def _tagged_barriers() -> dict:
    """A RED wall + GREEN flap-gate barrier FeatureCollection along cell edges."""
    # Wall along the edge between cells (5,5)-(5,6); flap gate (10,5)-(10,6).
    def _edge_line(a: tuple[int, int], b: tuple[int, int]) -> list[list[float]]:
        la = _cell_lonlat(*a)
        lb = _cell_lonlat(*b)
        return [list(la), list(lb)]

    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"barrier_type": "wall"},
                "geometry": {"type": "LineString", "coordinates": _edge_line((5, 5), (5, 6))},
            },
            {
                "type": "Feature",
                "properties": {"barrier_type": "flap_gate"},
                "geometry": {"type": "LineString", "coordinates": _edge_line((10, 5), (10, 6))},
            },
        ],
    }


@pytest.fixture()
def synthetic_inputs(tmp_path: Path):
    dem_path = tmp_path / "dem.tif"
    _write_dem_geotiff(dem_path)
    footprints = {
        "type": "FeatureCollection",
        "features": [_footprint_over_cell(7, 7), _footprint_over_cell(8, 8)],
    }
    barriers = _tagged_barriers()
    return str(dem_path), footprints, barriers


def _fake_upload(local_cog, run_id, runs_bucket=None, *, dest_filename="swmm_depth_peak.tif"):  # noqa: ANN001
    return f"gs://test-runs/{run_id}/{dest_filename}"


def _titiler_template(layer_uri: str) -> str:
    """A TiTiler-style published tile template (the publish_layer success shape).

    BREAK A: the composer now routes the raw object-store COG through
    publish_layer (the render chokepoint) before returning/emitting it. In-test
    there is no QGIS/TiTiler worker, so we stub publish_layer with a deterministic
    http(s) template that embeds the source uri as the ``url=`` query param - this
    mirrors the live TiTiler tile-URL shape and gives each frame a DISTINCT
    renderable url (distinct _layer_identity_key -> no dedup collapse).
    """
    from urllib.parse import quote

    return (
        "https://tiles.example/cog/tiles/{z}/{x}/{y}.png"
        f"?url={quote(layer_uri, safe='')}"
    )


def _patch_publish_layer(monkeypatch, calls: list | None = None):  # noqa: ANN001
    """Stub model_swmm_urban_flood.publish_layer to the TiTiler template shape.

    The peak + each frame are published through this seam; without the stub the
    composer would hit the real (absent) QGIS worker, publish would fail, and the
    honest-drop path would strip the peak's renderable url + drop all frames.
    """

    def _pub(layer_uri, layer_id, style_preset=None, **kwargs):  # noqa: ANN001
        if calls is not None:
            calls.append(
                {"layer_uri": layer_uri, "layer_id": layer_id, "style_preset": style_preset}
            )
        return _titiler_template(layer_uri)

    monkeypatch.setattr(
        "trid3nt_server.workflows.swmm.urban_flood.urban_flood.publish_layer", _pub
    )


class _MeshUploadS3:
    """A put-capable fake S3 client for the #156 mesh.geojson upload.

    The mesh layer is now uploaded to the runs bucket via the solver
    _get_s3_client().put_object seam (durability fix: the s3:// uri survives deck
    cleanup + reconnect). Stub the client so the composer's mesh emit succeeds
    end to end in-test (no live object store)."""

    def __init__(self) -> None:
        self.puts: list[dict] = []
        self._store: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kw):  # noqa: ANN003
        body = kw.get("Body")
        data = body.read() if hasattr(body, "read") else body
        self.puts.append({"Bucket": kw["Bucket"], "Key": kw["Key"], "Body": data})
        self._store[(kw["Bucket"], kw["Key"])] = data
        return {}

    def get_object(self, Bucket, Key):  # noqa: ANN001, N803
        # Stateful GET so the ADR-0282 outputs.json write -> seam read-back
        # round-trip works end to end in-test (the composer's _read_swmm_frame_layers
        # reads the manifest this fake stored on put_object).
        data = self._store.get((Bucket, Key))
        if data is None:
            raise KeyError(f"no object s3://{Bucket}/{Key}")
        import io

        return {"Body": io.BytesIO(data)}


def _install_mesh_upload_s3(monkeypatch) -> "_MeshUploadS3":
    """Bind a put-capable fake S3 client + a runs bucket so make_swmm_mesh_layer_uri
    uploads mesh.geojson durably in-test (returns the fake for assertions).

    Uses monkeypatch.setattr on the solver module global so the bound client +
    runs bucket are auto-restored at test teardown (no global leak)."""
    from trid3nt_server.workflows.solver import solver as solver_mod

    fake = _MeshUploadS3()
    monkeypatch.setenv("TRID3NT_RUNS_BUCKET", "test-runs-bucket")
    # _get_s3_client() returns the module global _S3_CLIENT when set; patching it
    # here is auto-reverted by monkeypatch (unlike solver.set_s3_client which sets
    # a global that would leak across tests).
    monkeypatch.setattr(solver_mod, "_S3_CLIENT", fake)
    monkeypatch.setattr(solver_mod, "_RUNS_BUCKET", None)
    return fake


class _FakeEmitter:
    """Captures the out-of-band frame emissions + the zoom-to map command."""

    def __init__(self) -> None:
        self.loaded_layers: list = []
        self.map_commands: list = []
        self.substep_labels: list = []

    async def add_loaded_layer(self, layer) -> None:  # noqa: ANN001
        self.loaded_layers.append(layer)

    async def emit_map_command(self, kind, payload) -> None:  # noqa: ANN001
        self.map_commands.append((kind, payload))

    # task-168: nested-substep API surface. This fake binds no real top-level
    # parent step, so substep yields None (the contract's "emitter bound but no
    # parent running" no-op case); begin_substeps is a no-op. The composer body
    # runs byte-identically whether or not a parent is bound. The raw labels are
    # recorded so a test can assert which internal operations were wrapped.
    @asynccontextmanager
    async def substep(self, raw_name):  # noqa: ANN001
        self.substep_labels.append(raw_name)
        yield None

    def begin_substeps(self, total) -> None:  # noqa: ANN001
        pass


def test_build_and_run_local_lane_produces_solved_out(synthetic_inputs):
    """build_and_stage_swmm_deck -> run_swmm_local solves a REAL deck headless
    in-process and the .out exists with the barriers + buildings applied."""
    dem_path, footprints, barriers = synthetic_inputs
    run_args = SWMMRunArgs(
        bbox=(-88.0, 36.0, -87.99, 36.01),  # bbox is provenance-only here
        total_rain_depth_mm=120.0,
        storm_duration_hr=1.0,  # short storm keeps the solve fast
        rain_interval_min=5,
        target_resolution_m=10.0,
        building_representation="drop",
        barriers=barriers,
        mass_balance_tolerance_pct=100.0,  # tiny deck: only need a real .out
    )
    staging = build_and_stage_swmm_deck(
        run_args, dem_path=dem_path, building_footprints=footprints
    )
    # buildings dropped + at least one wall + at least one flap gate snapped.
    assert staging.build.n_buildings_dropped >= 1
    assert staging.build.n_walls >= 1
    assert staging.build.n_flap_gates >= 1

    run = run_swmm_local(staging)
    assert Path(run.out_path).exists()
    assert run.n_steps > 1  # a multi-step solve (frames can form)
    assert run.continuity_error_pct is not None


def test_full_local_chain_emits_peak_plus_frames(synthetic_inputs, monkeypatch):
    """The composer runs the FULL local chain end to end (synthetic DEM ->
    deck -> in-process solve -> postprocess), returns the PEAK primary
    SWMMDepthLayerURI, and emits a contiguous 'Flood depth step N' frame group
    out-of-band via the emitter. Upload is stubbed; no live fetch."""
    import asyncio

    dem_path, footprints, barriers = synthetic_inputs

    # Stub the COG upload (no object store in-test).
    monkeypatch.setattr(
        "trid3nt_server.workflows.swmm.postprocess_swmm._upload_cog_to_runs_bucket",
        _fake_upload,
    )
    # BREAK A: stub publish_layer so the peak + each frame are routed through the
    # render chokepoint and come back as renderable http(s) tile templates.
    publish_calls: list = []
    _patch_publish_layer(monkeypatch, publish_calls)
    # #156 durability: the mesh.geojson is uploaded to the runs bucket via the
    # solver S3 seam; bind a put-capable fake client so the mesh emits as s3://.
    _install_mesh_upload_s3(monkeypatch)
    # Bind a fake emitter so the out-of-band frame emission is captured (mirrors
    # the WS dispatch ContextVar binding).
    from trid3nt_server.emission import pipeline_emitter as pe

    fake = _FakeEmitter()
    token = pe._CURRENT_EMITTER.set(fake)
    try:
        run_args = SWMMRunArgs(
            bbox=(-88.0, 36.0, -87.99, 36.01),
            total_rain_depth_mm=120.0,
            storm_duration_hr=1.0,
            rain_interval_min=5,
            target_resolution_m=10.0,
            building_representation="drop",
            barriers=barriers,
            mass_balance_tolerance_pct=100.0,
        )
        peak = asyncio.run(
            model_swmm_urban_flood(
                run_args,
                dem_path=dem_path,
                building_footprints=footprints,
                run_id="run-urban",
                cleanup_deck=True,
            )
        )
    finally:
        pe._CURRENT_EMITTER.reset(token)

    # --- peak primary: the run_modflow-style single returned LayerURI ---------
    assert isinstance(peak, SWMMDepthLayerURI)
    assert peak.role == "primary"
    # OBSERVABILITY (NATE): the synthetic deck drops >= 1 building as an obstacle,
    # so the peak name carries the obstacle count (visible in the LayerPanel /
    # narration) in addition to the base "Peak flood depth" label.
    assert peak.name.startswith("Peak flood depth"), peak.name
    assert "as obstacles)" in peak.name, peak.name
    assert peak.layer_id == "swmm-depth-peak-run-urban"
    assert peak.style_preset == "continuous_flood_depth"
    assert peak.max_depth_m >= 0.0
    assert peak.flooded_area_km2 >= 0.0
    assert peak.n_buildings_affected >= 0
    # barriers echoed back for rendering (RED walls / GREEN flap gates).
    assert peak.barriers is not None
    assert peak.barriers["type"] == "FeatureCollection"
    # BREAK A: the returned peak carries a PUBLISHED renderable http(s) tile URL,
    # NOT a raw s3:///gs:// COG (which the emit guardrail would drop from the map).
    assert peak.uri.startswith("http"), peak.uri
    assert not peak.uri.startswith("s3://") and not peak.uri.startswith("gs://")

    # --- emitted layers partition: mesh context layer(s) + the depth frames ----
    # NATE task #156: model_swmm_urban_flood now emits a quasi-2D computational
    # "mesh_grid" CONTEXT vector layer via add_loaded_layer right after the deck
    # build (before the depth frames), so fake.loaded_layers carries that mesh
    # layer ALONGSIDE the SWMMDepthLayerURI depth frames. The depth FRAMES are
    # still the "Flood depth step N" SWMMDepthLayerURI group; the mesh is a
    # NON-SWMMDepthLayerURI addition (an inline-geojson vector, NOT published).
    # ``make_buildings_input_layer_uri`` separately emits an "input"-role
    # "Building footprints (N)" vector layer (also non-SWMMDepthLayerURI) when
    # footprints are supplied, so the non-depth-frame layers now partition into
    # the mesh (style_preset="mesh_grid") plus that buildings-input layer
    # (style_preset="osm_buildings") -- not a single "mesh_layers" bucket.
    depth_frames = [
        f for f in fake.loaded_layers if isinstance(f, SWMMDepthLayerURI)
    ]
    non_frame_layers = [
        f for f in fake.loaded_layers if not isinstance(f, SWMMDepthLayerURI)
    ]
    mesh_layers = [f for f in non_frame_layers if f.style_preset == "mesh_grid"]

    # ADR 0244 S2: the composer no longer hand-emits a buildings-input layer --
    # the fetched OSM footprints surface via the emit-on-fetch router seam (pinned
    # in test_emit_on_fetch_seam.py). Here footprints are passed in directly (no
    # route() fetch), so the ONLY non-depth-frame layer is the #156 mesh.
    assert len(non_frame_layers) == len(mesh_layers), (
        f"unexpected non-depth-frame layer(s): {[getattr(m, 'name', m) for m in non_frame_layers]}"
    )

    # Exactly ONE computational-mesh context layer, with its #156 contract.
    assert len(mesh_layers) == 1, (
        f"expected exactly one mesh context layer; got {len(mesh_layers)}: "
        f"{[getattr(m, 'name', m) for m in mesh_layers]}"
    )
    mesh = mesh_layers[0]
    assert mesh.style_preset == "mesh_grid", mesh.style_preset
    assert mesh.role == "context", mesh.role
    assert mesh.layer_type == "vector", mesh.layer_type
    assert mesh.name.startswith("Computational mesh"), mesh.name
    # #156 DURABILITY: the mesh uri is a DURABLE s3:// runs-bucket object, NOT a
    # local /tmp deck-staging path that deck cleanup would delete (the shipped
    # bug: on re-emit/reconnect the deleted /tmp path made the mesh VANISH).
    assert mesh.uri.startswith("s3://"), mesh.uri
    assert "/tmp/" not in mesh.uri
    assert mesh.uri.endswith("/mesh.geojson"), mesh.uri

    # --- frames emitted OUT-OF-BAND as a contiguous "Flood depth step N" group ---
    frames = depth_frames
    assert len(frames) >= 2, f"expected a multi-frame animation group; got {len(frames)}"
    assert all(isinstance(f, SWMMDepthLayerURI) for f in frames)
    assert all(f.role == "context" for f in frames)
    names = [f.name for f in frames]
    assert names == [f"Flood depth step {i}" for i in range(1, len(frames) + 1)]
    for name in names:
        assert _WEB_STEP_TOKEN_RE.search(name) is not None, name
    # BREAK A: every emitted frame carries a PUBLISHED renderable http(s) URL.
    assert all(f.uri.startswith("http") for f in frames), [f.uri for f in frames]
    # DISTINCT uris (distinct runs-bucket keys -> distinct published url -> no
    # dedup collapse).
    uris = [f.uri for f in frames]
    assert len(set(uris)) == len(uris)
    assert peak.uri not in uris
    # the peak is NOT in the emitted frame set (it is the returned layer).
    assert all(f.layer_id != peak.layer_id for f in frames)

    # BREAK A: publish_layer fired once for the peak + once per emitted frame.
    # The mesh context layer is an inline-geojson vector and is NOT published, so
    # it does NOT add to the publish_calls count.
    assert len(publish_calls) == 1 + len(frames), (
        f"expected publish_layer x{1 + len(frames)} (peak + {len(frames)} frames); "
        f"got {len(publish_calls)}: {[c['layer_id'] for c in publish_calls]}"
    )
    assert publish_calls[0]["layer_id"] == "swmm-depth-peak-run-urban"
    assert all(c["style_preset"] == "continuous_flood_depth" for c in publish_calls)

    # --- zoom-on-area-first emitted before the solve ---
    assert any(k == "zoom-to" for k, _ in fake.map_commands)


def test_tool_wrapper_drives_full_chain(synthetic_inputs, monkeypatch):
    """The LLM-facing swmm_urban_flood tool drives the same chain and returns
    a SWMMDepthLayerURI (the add_loaded_layer gate target) - DEM fetch stubbed to
    the synthetic file so no network is touched."""
    import asyncio

    dem_path, footprints, barriers = synthetic_inputs

    monkeypatch.setattr(
        "trid3nt_server.workflows.swmm.postprocess_swmm._upload_cog_to_runs_bucket",
        _fake_upload,
    )
    # BREAK A: stub publish_layer (no QGIS/TiTiler worker in-test).
    _patch_publish_layer(monkeypatch)
    # #156 durability: bind a put-capable fake S3 so the mesh.geojson upload
    # succeeds in-test (the mesh emits as a durable s3:// uri).
    _install_mesh_upload_s3(monkeypatch)
    # Stub the composer's DEM + buildings acquisition to the synthetic inputs so
    # the tool path needs no live fetch.
    monkeypatch.setattr(
        "trid3nt_server.workflows.swmm.urban_flood.urban_flood._fetch_dem_for_urban",
        lambda bbox, uri_sink=None: (dem_path, "synthetic"),
    )
    monkeypatch.setattr(
        "trid3nt_server.workflows.swmm.urban_flood.urban_flood._fetch_buildings_for_urban",
        lambda bbox: footprints,
    )
    monkeypatch.setattr(
        "trid3nt_server.workflows.swmm.urban_flood.urban_flood._atlas14_total_depth_mm",
        lambda bbox, rp, dur: 120.0,
    )

    from trid3nt_server.workflows.swmm.urban_flood.urban_flood import swmm_urban_flood

    out = asyncio.run(
        swmm_urban_flood(
            bbox=[-88.0, 36.0, -87.99, 36.01],
            storm_duration_hr=1.0,
            rain_interval_min=5,
            target_resolution_m=10.0,
            building_representation="drop",
            barriers=barriers,
            mass_balance_tolerance_pct=100.0,
        )
    )
    assert isinstance(out, SWMMDepthLayerURI), out
    assert out.role == "primary"
    assert out.layer_id.startswith("swmm-depth-peak-")
    assert out.max_depth_m >= 0.0
    # BREAK A: the returned peak carries a published renderable http(s) URL.
    assert out.uri.startswith("http"), out.uri
