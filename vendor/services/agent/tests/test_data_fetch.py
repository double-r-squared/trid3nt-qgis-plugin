"""Unit tests for the 4 data-fetch atomic tools (job-0033, M4 Stage C).

Coverage:
- Each tool's ``@register_tool`` lands a registered entry with the expected
  TTL class + source class + cacheable flag.
- ``round_bbox_to_resolution`` is deterministic and snaps to a stable grid.
- Bbox quantization at a single resolution produces the same canonicalized
  params dict for two callers within the same grid cell.
- ``fetch_dem`` routes through ``read_through`` (mocked GCS + mocked
  py3dep): cache miss invokes the fetcher and returns a ``LayerURI``.
- ``fetch_buildings`` routes through ``read_through`` (mocked GCS + mocked
  Planetary Computer STAC search): no-matching-items raises
  ``UpstreamAPIError`` (no sentinel written).
- ``fetch_population`` routes through ``read_through`` (mocked Census REST
  + mocked GCS): a single-state CONUS bbox yields a FeatureCollection.
- ``geocode_location`` routes through ``read_through`` (mocked Nominatim
  REST + mocked GCS): returns ``{name, bbox, latitude, longitude, source}``
  shape and emits a ``location-resolved``-eligible payload.
- ``BboxInvalidError`` paths (degenerate bbox, out-of-range lat/lon, bbox
  area over guardrail).
- Mocked external-API failures re-raise as ``UpstreamAPIError`` from inside
  ``read_through`` with no sentinel written to the cache.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools import data_fetch
from grace2_agent.tools.data_fetch import (
    BboxInvalidError,
    GeocodeNoMatchError,
    UpstreamAPIError,
    fetch_buildings,
    fetch_dem,
    fetch_population,
    geocode_location,
    round_bbox_to_resolution,
)


# Fort Myers, FL — small bbox for live + mocked path testing.
FORT_MYERS_BBOX = (-81.92, 26.55, -81.80, 26.68)
PINNED_NOW = datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc)


class _S3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeStorageClient:
    """In-memory S3 double (GCP decommissioned). ``store`` keyed by object KEY.

    Returns the per-test active instance installed by the autouse
    ``_route_cache_to_inmemory_s3`` fixture so the tool's real S3 read-through
    (boto3) reads/writes the same store the test inspects.
    """

    _active: "FakeStorageClient | None" = None

    def __new__(cls) -> "FakeStorageClient":
        if cls._active is not None:
            return cls._active
        return super().__new__(cls)

    def __init__(self) -> None:
        if getattr(self, "_init", False):
            return
        self._init = True
        self.store: dict[str, bytes] = {}
        self.last_put: dict | None = None

    def get_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        try:
            data = self.store[Key]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": _S3Body(data)}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        data = Body.read() if hasattr(Body, "read") else Body
        self.store[Key] = data
        self.last_put = {"Bucket": Bucket, "Key": Key, "ContentType": ContentType}
        return {}


@pytest.fixture(autouse=True)
def _route_cache_to_inmemory_s3(monkeypatch):
    """Route boto3 S3 (the cache shim's only object store) to an in-memory double."""
    import boto3

    FakeStorageClient._active = None
    client = FakeStorageClient()
    FakeStorageClient._active = client

    def _factory(service_name, *a, **k):
        assert service_name == "s3"
        return client

    monkeypatch.setattr(boto3, "client", _factory)
    try:
        yield client
    finally:
        FakeStorageClient._active = None


# ---------------------------------------------------------------------------
# Registration: every tool lands with the right metadata.
# ---------------------------------------------------------------------------


def test_fetch_dem_is_registered_with_static_30d():
    entry = TOOL_REGISTRY["fetch_dem"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "dem"
    assert entry.metadata.cacheable is True


def test_fetch_buildings_is_registered_with_static_30d():
    entry = TOOL_REGISTRY["fetch_buildings"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "buildings"
    assert entry.metadata.cacheable is True


def test_fetch_population_is_registered_with_static_30d():
    entry = TOOL_REGISTRY["fetch_population"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "population"
    assert entry.metadata.cacheable is True


def test_geocode_location_is_registered_with_dynamic_1h():
    entry = TOOL_REGISTRY["geocode_location"]
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "geocode"
    assert entry.metadata.cacheable is True


def test_registry_contains_job_0039_subset_after_eager_import():
    """job-0039 acceptance: this job's 3 new fetchers are registered + M4 fetchers + passthroughs.

    Inside the test process, the eager-import surface is whatever the test
    module triggers — ``tools/__init__.py`` (passthroughs, FROZEN) + the
    explicit ``import grace2_agent.tools.data_fetch`` at the top of this
    test file (which fires this job's three new ``@register_tool``
    decorators alongside the M4 four). Parallel sprint-07 imports
    (``qgis_discovery`` from job-0034, ``solver`` from job-0041) are
    triggered by ``main._import_tools_registry()`` — see the
    ``--startup-only`` evidence below for the live ≥11-tool assertion the
    kickoff calls out.
    """
    names = set(TOOL_REGISTRY.keys())
    expected_subset = {
        "qgis_process",
        "fetch_dem",
        "fetch_buildings",
        "fetch_population",
        "geocode_location",
        # job-0039 (this job):
        "fetch_landcover",
        "fetch_river_geometry",
        "lookup_precip_return_period",
    }
    assert expected_subset.issubset(names), f"missing: {expected_subset - names}"
    # 1 passthrough + 4 M4 fetchers + 3 new fetchers = 8 minimum in test
    # context; ≥8 tolerates qgis_discovery / solver / pipeline-emitter
    # imports landing in parallel.
    assert len(names) >= 8


# ---------------------------------------------------------------------------
# round_bbox_to_resolution — engine-side quantization (OQ-32-QUANTIZATION-LOCATION).
# ---------------------------------------------------------------------------


def test_round_bbox_to_resolution_is_deterministic():
    """Two calls with the same bbox + resolution produce identical output."""
    q1 = round_bbox_to_resolution(FORT_MYERS_BBOX, 10)
    q2 = round_bbox_to_resolution(FORT_MYERS_BBOX, 10)
    assert q1 == q2


def test_round_bbox_to_resolution_collapses_floating_point_jitter():
    """Two callers whose bbox edges differ by sub-meter floats hit the same key.

    This is the dedup-via-quantization property: 1e-7 degrees of jitter
    (sub-meter) at 10m resolution should snap to the same grid cell.
    """
    base = (-81.9000001, 26.5500001, -81.8000001, 26.6800001)
    jitter = (-81.9000002, 26.5500002, -81.8000002, 26.6800002)
    qb = round_bbox_to_resolution(base, 10)
    qj = round_bbox_to_resolution(jitter, 10)
    assert qb == qj


def test_round_bbox_to_resolution_envelopes_input():
    """The quantized bbox covers (>=) the input bbox on all sides."""
    q = round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    assert q[0] <= FORT_MYERS_BBOX[0]
    assert q[1] <= FORT_MYERS_BBOX[1]
    assert q[2] >= FORT_MYERS_BBOX[2]
    assert q[3] >= FORT_MYERS_BBOX[3]


def test_round_bbox_to_resolution_rejects_degenerate_bbox():
    with pytest.raises(BboxInvalidError):
        round_bbox_to_resolution((-81.9, 26.5, -81.9, 26.6), 10)  # min_lon == max_lon


def test_round_bbox_to_resolution_rejects_out_of_range_lat():
    with pytest.raises(BboxInvalidError):
        round_bbox_to_resolution((-81.9, -95.0, -81.8, 26.6), 10)


# ---------------------------------------------------------------------------
# fetch_dem — mocked py3dep + mocked GCS happy path.
# ---------------------------------------------------------------------------


def test_fetch_dem_happy_path_writes_through_cache(monkeypatch):
    """A miss invokes the mocked py3dep fetcher, writes COG bytes, returns LayerURI."""
    fake_storage = FakeStorageClient()
    monkeypatch.setattr(
        data_fetch, "_fetch_3dep_dem_bytes", lambda bbox, res: b"FAKE_COG_BYTES"
    )

    # Run via read_through directly with the storage_client injected — the
    # tool function builds its own client. So instead, we monkeypatch the
    # google.cloud.storage import path inside read_through by overriding the
    # cache module's import-lookup. Cleanest: import the module and patch.
    from grace2_agent.tools import cache as cache_mod

    original_read_through = cache_mod.read_through

    def patched_read_through(*args: Any, **kwargs: Any):
        kwargs.setdefault("storage_client", fake_storage)
        kwargs.setdefault("now", PINNED_NOW)
        return original_read_through(*args, **kwargs)

    monkeypatch.setattr(data_fetch, "read_through", patched_read_through)

    layer = fetch_dem(FORT_MYERS_BBOX, resolution_m=10)
    assert layer.layer_type == "raster"
    assert layer.style_preset == "continuous_dem"
    assert layer.uri.startswith("s3://grace2-hazard-cache-226996537797/cache/static-30d/dem/")
    assert layer.uri.endswith(".tif")
    assert layer.units == "meters"
    # LANE-C (#159 follow-up #4): the returned layer declares the requested
    # (quantized) extent so the AOI-pin reuse + zoom-to know the DEM's intent.
    assert layer.bbox is not None
    assert tuple(layer.bbox) == round_bbox_to_resolution(FORT_MYERS_BBOX, 10)

    # The in-memory S3 store should now hold the COG bytes.
    paths_written = list(fake_storage.store.keys())
    assert len(paths_written) == 1
    assert fake_storage.store[paths_written[0]] == b"FAKE_COG_BYTES"
    # GCP decommissioned: TTL eviction is an S3 bucket-lifecycle rule, so no
    # per-object customTime is written; assert the boto3 put landed instead.
    assert fake_storage.last_put is not None
    assert fake_storage.last_put["Key"] == paths_written[0]


def test_fetch_dem_rejects_continent_scale_bbox():
    """The 5,000,000 km^2 hard ceiling rejects continent-scale bboxes.

    F16-for-DEM (2026-07-10): the old flat 10,000 km^2 hard-fail is replaced
    by a pixel-budget auto-coarsen (see the tests below) -- only
    continent-scale bboxes still hard-fail, mirroring fetch_landcover's
    5,000,000 km^2 ceiling (commit 21cd123).
    """
    whole_conus = (-125.0, 24.0, -66.0, 50.0)  # ~8,000,000 km^2
    with pytest.raises(BboxInvalidError, match="5,000,000"):
        fetch_dem(whole_conus, resolution_m=30)


# ---------------------------------------------------------------------------
# F16-for-DEM (2026-07-10): state-scale auto-coarsen, mirroring the
# fetch_landcover treatment (commit 21cd123). Live failure this fixes:
# "show me the hillshade in the bounding box" over Washington state ->
# fetch_dem(bbox=<WA state>, source='3dep', resolution_m=30) hard-failed
# "bbox area 230638.1 km^2 exceeds 10000 km^2 guardrail". The hard-fail zone
# is now served via a 4000 px/axis pixel-budget auto-coarsen.
# ---------------------------------------------------------------------------

# The exact bbox from the live failure report.
_WA_STATE_BBOX = (-124.837922, 45.543029, -116.914037, 49.003324)


def _effective_res_from_layer(layer) -> int:
    """Parse the effective resolution stamped into ``layer_id``'s ``<N>m`` suffix."""
    return int(layer.layer_id.rsplit("-", 1)[-1].rstrip("m"))


def _install_fake_dem_fetch(monkeypatch, fake_storage) -> None:
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch, "_fetch_3dep_dem_bytes", lambda bbox, res: b"FAKE_COG_BYTES"
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )


def test_fetch_dem_state_scale_no_hard_fail(monkeypatch):
    """The WA-state bbox (~230,638 km^2) must not raise BboxInvalidError.

    The hard-fail that was at > 10,000 km^2 is replaced by auto-coarsening;
    only continent-scale bboxes (> 5,000,000 km^2) still hard-fail.
    """
    fake_storage = FakeStorageClient()
    _install_fake_dem_fetch(monkeypatch, fake_storage)

    layer = fetch_dem(_WA_STATE_BBOX, resolution_m=30)
    effective = _effective_res_from_layer(layer)
    # WA long axis ~599 km / 4000 px -> ~150 m; must exceed the 30 m request
    # and stay well inside the extended ladder's coarsest rung (900 m).
    assert 30 < effective <= 900
    assert "coarsened from 30m" in layer.name
    assert "hillshade/overview" in layer.name


def test_fetch_dem_bypass_enforces_pixel_budget(monkeypatch):
    """A direct state-scale call at the tool's 10 m default (gate bypassed)
    must still be coarsened by the TOOL against the pixel budget -- the
    delivered grid, not the request, is what ``layer_id``/``name`` describe."""
    fake_storage = FakeStorageClient()
    _install_fake_dem_fetch(monkeypatch, fake_storage)

    layer = fetch_dem(_WA_STATE_BBOX, resolution_m=10)
    effective = _effective_res_from_layer(layer)
    assert 10 < effective <= 900
    assert "coarsened from 10m" in layer.name


def test_fetch_dem_explicit_coarse_resolution_honored(monkeypatch):
    """An explicit coarse resolution_m on a SMALL bbox is delivered exactly --
    the tool never further coarsens below what was explicitly requested, and
    an honored (not budget-forced) coarse request carries no coarsening note."""
    fake_storage = FakeStorageClient()
    _install_fake_dem_fetch(monkeypatch, fake_storage)

    layer = fetch_dem(FORT_MYERS_BBOX, resolution_m=300)
    effective = _effective_res_from_layer(layer)
    assert effective == 300
    assert "coarsened" not in layer.name


def test_fetch_dem_tiny_bbox_native_resolution_untouched(monkeypatch):
    """A tiny (~100 m) bbox at a fine (site-scale) resolution is delivered
    untouched -- the pixel-budget floor never coarsens a small-AOI fine
    request. (Fort Myers -- ~13 km across -- is itself too large for a 1 m
    request to stay under the 4000 px budget, so this uses a building-scale
    bbox instead.)"""
    fake_storage = FakeStorageClient()
    _install_fake_dem_fetch(monkeypatch, fake_storage)

    tiny_bbox = (-81.9010, 26.5500, -81.9000, 26.5510)  # ~100 m x 110 m
    layer = fetch_dem(tiny_bbox, resolution_m=1)
    effective = _effective_res_from_layer(layer)
    assert effective == 1
    assert "coarsened" not in layer.name


def test_fetch_dem_upstream_failure_reraises(monkeypatch):
    """An upstream py3dep failure surfaces as UpstreamAPIError; no sentinel written."""
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    def boom(_bbox, _res):
        raise UpstreamAPIError("py3dep is unreachable")

    monkeypatch.setattr(data_fetch, "_fetch_3dep_dem_bytes", boom)
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )
    with pytest.raises(UpstreamAPIError):
        fetch_dem(FORT_MYERS_BBOX, resolution_m=10)
    # No sentinel written.
    assert fake_storage.store == {}


# ---------------------------------------------------------------------------
# LANE-C (#159 follow-up #4): fetch_dem coverage gate. 3DEP can return a DEM
# SHORT on an edge (the live south-edge clip -> 79% height hillshade); the gate
# raises a typed DemPartialCoverageError so we never silently mesh / hillshade a
# clipped DEM, per the data-source-fallback norm.
# ---------------------------------------------------------------------------


def _fake_dem_dataarray(bounds, crs="EPSG:4326"):
    """A minimal rioxarray DataArray spanning ``bounds`` in ``crs``.

    ``bounds`` = ``(left, bottom, right, top)``. Used to drive the real
    ``_dem_wgs84_bounds`` reprojection + ``_bbox_covers`` coverage check inside
    ``_fetch_3dep_dem_bytes`` without hitting the live 3DEP service.
    """
    import numpy as np
    import rioxarray  # noqa: F401 — registers the .rio accessor
    import xarray as xr
    from rasterio.transform import from_bounds

    left, bottom, right, top = bounds
    h, w = 8, 8
    da = xr.DataArray(
        np.ones((h, w), dtype="float32"),
        dims=("y", "x"),
        coords={
            "y": np.linspace(top, bottom, h),
            "x": np.linspace(left, right, w),
        },
    )
    da = da.rio.write_crs(crs)
    da.rio.write_transform(from_bounds(left, bottom, right, top, w, h), inplace=True)
    return da


def test_fetch_3dep_full_coverage_passes(monkeypatch):
    """A DEM that fully spans the requested bbox serializes to COG bytes (no raise)."""
    req = (-97.755, 30.26, -97.725, 30.285)
    # py3dep returns a raster slightly LARGER than the request -> full coverage.
    full = (-97.76, 30.255, -97.72, 30.29)
    import py3dep

    monkeypatch.setattr(
        py3dep, "get_dem", lambda bbox, resolution: _fake_dem_dataarray(full)
    )
    data = data_fetch._fetch_3dep_dem_bytes(req, 10)
    assert isinstance(data, (bytes, bytearray)) and len(data) > 0


def test_fetch_3dep_south_edge_short_raises_partial_coverage(monkeypatch):
    """A DEM short on the SOUTH edge (the live bug) raises DemPartialCoverageError."""
    req = (-97.755, 30.26, -97.725, 30.285)
    # The returned raster starts ~0.005 deg NORTH of the requested south edge
    # (well past the coverage tolerance) -> under-covers the requested height.
    short_south = (-97.755, 30.265, -97.725, 30.285)
    import py3dep

    monkeypatch.setattr(
        py3dep, "get_dem", lambda bbox, resolution: _fake_dem_dataarray(short_south)
    )
    with pytest.raises(data_fetch.DemPartialCoverageError) as exc:
        data_fetch._fetch_3dep_dem_bytes(req, 10)
    assert exc.value.error_code == "DEM_PARTIAL_COVERAGE"


def test_dem_partial_coverage_is_upstream_subclass():
    """DemPartialCoverageError subclasses UpstreamAPIError so the urban workflow's
    1m->10m fallback (except Exception) still fires on a partial 1m tile."""
    assert issubclass(data_fetch.DemPartialCoverageError, UpstreamAPIError)


def test_bbox_covers_flags_material_shortfall():
    """_bbox_covers: full coverage True; any material edge shortfall False;
    a sub-tolerance shortfall still True (no false partial-coverage flag)."""
    req = (-97.755, 30.26, -97.725, 30.285)
    tol = data_fetch._DEM_COVERAGE_TOL_DEG
    assert data_fetch._bbox_covers((-97.76, 30.25, -97.72, 30.29), req) is True
    # South-edge clip well past tolerance -> partial.
    assert data_fetch._bbox_covers((-97.755, 30.27, -97.725, 30.285), req) is False
    # Sub-tolerance clip -> still covers (absorb a half-cell edge snap).
    assert (
        data_fetch._bbox_covers(
            (-97.755, 30.26 + tol * 0.4, -97.725, 30.285), req
        )
        is True
    )


# ---------------------------------------------------------------------------
# fetch_buildings — mocked STAC search.
# ---------------------------------------------------------------------------


def _patch_read_through(monkeypatch, fake_storage):
    """Route ``data_fetch.read_through`` through a FakeStorageClient + pinned now."""
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )


def test_fetch_buildings_happy_path_msft(monkeypatch):
    """Explicit source='msft' tries MS first; mocked bytes write through cache."""
    fake_storage = FakeStorageClient()
    monkeypatch.setattr(
        data_fetch, "_fetch_msft_buildings_bytes", lambda bbox: b"FAKE_FGB_BYTES"
    )
    _patch_read_through(monkeypatch, fake_storage)

    layer = fetch_buildings(FORT_MYERS_BBOX, source="msft")
    assert layer.layer_type == "vector"
    assert layer.style_preset == "affected_buildings"
    assert layer.name == "Buildings (MSFT)"
    assert layer.uri.startswith(
        "s3://grace2-hazard-cache-226996537797/cache/static-30d/buildings/"
    )
    assert layer.uri.endswith(".fgb")


def test_fetch_buildings_default_source_is_osm(monkeypatch):
    """Default (no source kwarg) routes to OSM Overpass, NOT MS."""
    fake_storage = FakeStorageClient()
    osm_called: list[Any] = []

    def fake_osm(bbox, on_tags=None):
        osm_called.append(bbox)
        return b"FAKE_OSM_FGB"

    def boom_msft(bbox):  # pragma: no cover — must not be reached on default
        raise AssertionError("msft must not be called when default source is osm")

    monkeypatch.setattr(data_fetch, "_fetch_osm_buildings_bytes", fake_osm)
    monkeypatch.setattr(data_fetch, "_fetch_msft_buildings_bytes", boom_msft)
    _patch_read_through(monkeypatch, fake_storage)

    layer = fetch_buildings(FORT_MYERS_BBOX)
    assert layer.layer_type == "vector"
    assert layer.name == "Buildings (OSM)"
    assert layer.uri.endswith(".fgb")
    assert len(osm_called) == 1


def test_fetch_buildings_osm_happy_path(monkeypatch):
    """Explicit source='osm' invokes the OSM Overpass fetcher."""
    fake_storage = FakeStorageClient()
    monkeypatch.setattr(
        data_fetch,
        "_fetch_osm_buildings_bytes",
        lambda bbox, on_tags=None: b"FAKE_OSM_FGB",
    )
    _patch_read_through(monkeypatch, fake_storage)

    layer = fetch_buildings(FORT_MYERS_BBOX, source="osm")
    assert layer.name == "Buildings (OSM)"
    assert layer.uri.endswith(".fgb")
    # The OSM bytes landed in the cache under an osm-keyed path.
    assert len(fake_storage.store) == 1
    assert next(iter(fake_storage.store.values())) == b"FAKE_OSM_FGB"


def test_fetch_buildings_falls_back_to_msft_when_osm_fails(monkeypatch):
    """OSM upstream failure → automatic fallback to MS; result is the MS layer."""
    fake_storage = FakeStorageClient()

    def osm_boom(bbox, on_tags=None):
        raise UpstreamAPIError("Overpass unreachable")

    monkeypatch.setattr(data_fetch, "_fetch_osm_buildings_bytes", osm_boom)
    monkeypatch.setattr(
        data_fetch, "_fetch_msft_buildings_bytes", lambda bbox: b"FAKE_MSFT_FGB"
    )
    _patch_read_through(monkeypatch, fake_storage)

    layer = fetch_buildings(FORT_MYERS_BBOX, source="osm")
    # Fell back to MS — the layer name + cache key reflect the source actually used.
    assert layer.name == "Buildings (MSFT)"
    assert "-msft" in layer.layer_id
    assert next(iter(fake_storage.store.values())) == b"FAKE_MSFT_FGB"


def test_fetch_buildings_falls_back_to_osm_when_msft_fails(monkeypatch):
    """Requested source='msft' failing → fallback to OSM; result is the OSM layer."""
    fake_storage = FakeStorageClient()

    def msft_boom(bbox):
        raise UpstreamAPIError("abfs:// asset cannot be downloaded")

    monkeypatch.setattr(data_fetch, "_fetch_msft_buildings_bytes", msft_boom)
    monkeypatch.setattr(
        data_fetch,
        "_fetch_osm_buildings_bytes",
        lambda bbox, on_tags=None: b"FAKE_OSM_FGB",
    )
    _patch_read_through(monkeypatch, fake_storage)

    layer = fetch_buildings(FORT_MYERS_BBOX, source="msft")
    assert layer.name == "Buildings (OSM)"
    assert "-osm" in layer.layer_id
    assert next(iter(fake_storage.store.values())) == b"FAKE_OSM_FGB"


def test_fetch_buildings_both_sources_fail_raises_honest_error(monkeypatch):
    """Both sources failing raises an UpstreamAPIError naming BOTH attempts; no sentinel."""
    fake_storage = FakeStorageClient()

    def osm_boom(bbox, on_tags=None):
        raise UpstreamAPIError("Overpass returned no footprints")

    def msft_boom(bbox):
        raise UpstreamAPIError("STAC asset is abfs:// only")

    monkeypatch.setattr(data_fetch, "_fetch_osm_buildings_bytes", osm_boom)
    monkeypatch.setattr(data_fetch, "_fetch_msft_buildings_bytes", msft_boom)
    _patch_read_through(monkeypatch, fake_storage)

    with pytest.raises(UpstreamAPIError) as excinfo:
        fetch_buildings(FORT_MYERS_BBOX)  # default osm -> fallback msft
    msg = str(excinfo.value)
    assert "osm" in msg and "msft" in msg
    assert "both sources" in msg.lower()
    # No sentinel written to the cache on the all-failed path.
    assert fake_storage.store == {}


def test_fetch_buildings_rejects_unknown_source():
    with pytest.raises(BboxInvalidError):
        fetch_buildings(FORT_MYERS_BBOX, source="usgs-nationalmap")


def test_build_overpass_buildings_ql_selects_ways_and_relations():
    """The Overpass QL selects building ways AND relations with (s,w,n,e) corners."""
    ql = data_fetch._build_overpass_buildings_ql(FORT_MYERS_BBOX)
    assert 'way["building"]' in ql
    assert 'relation["building"]' in ql
    assert "out geom;" in ql
    # Fort Myers bbox = (-81.92, 26.55, -81.80, 26.68); Overpass wants
    # (south, west, north, east) = (26.55, -81.92, 26.68, -81.80).
    assert "(26.55,-81.92,26.68,-81.8)" in ql or "26.55,-81.92,26.68,-81.8" in ql


def test_extract_building_features_assembles_polygons_and_relations():
    """Closed ways -> Polygon; multipolygon relations -> (Multi)Polygon; junk dropped."""
    pytest.importorskip("shapely")
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 111,
                "tags": {"building": "yes", "name": "Block A"},
                "geometry": [
                    {"lat": 26.60, "lon": -81.85},
                    {"lat": 26.60, "lon": -81.84},
                    {"lat": 26.61, "lon": -81.84},
                    {"lat": 26.61, "lon": -81.85},
                    {"lat": 26.60, "lon": -81.85},
                ],
            },
            {
                "type": "relation",
                "id": 222,
                "tags": {"building": "commercial"},
                "members": [
                    {
                        "type": "way",
                        "role": "outer",
                        "geometry": [
                            {"lat": 26.62, "lon": -81.87},
                            {"lat": 26.62, "lon": -81.86},
                            {"lat": 26.63, "lon": -81.86},
                            {"lat": 26.63, "lon": -81.87},
                            {"lat": 26.62, "lon": -81.87},
                        ],
                    },
                    {
                        "type": "way",
                        "role": "inner",
                        "geometry": [
                            {"lat": 26.625, "lon": -81.868},
                            {"lat": 26.625, "lon": -81.862},
                            {"lat": 26.628, "lon": -81.862},
                            {"lat": 26.625, "lon": -81.868},
                        ],
                    },
                ],
            },
            # A node element (not a building polygon) must be dropped.
            {"type": "node", "id": 333, "lat": 26.6, "lon": -81.8},
            # A degenerate way (2 points) must be dropped.
            {
                "type": "way",
                "id": 444,
                "tags": {"building": "yes"},
                "geometry": [
                    {"lat": 26.60, "lon": -81.85},
                    {"lat": 26.60, "lon": -81.84},
                ],
            },
        ]
    }
    features, tags_by_fid = data_fetch._extract_building_features(payload)
    assert len(features) == 2
    geom_types = {g.geom_type for g, _a in features}
    assert geom_types <= {"Polygon", "MultiPolygon"}
    ids = {a["osm_id"] for _g, a in features}
    assert ids == {111, 222}
    # INLINE props are SLIM (frontend-perf fix): id-only, no building/name.
    for _g, attrs in features:
        assert set(attrs) == {"osm_id", "osm_type", "fid"}
        assert "building" not in attrs
        assert "name" not in attrs
    # The composite fid is "<first-letter-of-type><id>".
    fids = {a["fid"] for _g, a in features}
    assert fids == {"w111", "r222"}
    # The FULL tag bag is captured in the sidecar map, keyed by fid.
    assert tags_by_fid["w111"] == {"building": "yes", "name": "Block A"}
    assert tags_by_fid["r222"] == {"building": "commercial"}


def test_fetch_osm_buildings_bytes_empty_raises_upstream(monkeypatch):
    """No building elements -> honest UpstreamAPIError (no silent dead-end)."""
    pytest.importorskip("geopandas")
    monkeypatch.setattr(
        data_fetch, "_post_overpass_buildings", lambda ql: {"elements": []}
    )
    with pytest.raises(UpstreamAPIError):
        data_fetch._fetch_osm_buildings_bytes(FORT_MYERS_BBOX)


def test_fetch_osm_buildings_bytes_writes_flatgeobuf(monkeypatch):
    """A mocked Overpass payload assembles + clips + serializes to FlatGeobuf bytes."""
    pytest.importorskip("geopandas")
    pytest.importorskip("pyogrio")
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 555,
                "tags": {"building": "yes"},
                "geometry": [
                    {"lat": 26.60, "lon": -81.85},
                    {"lat": 26.60, "lon": -81.84},
                    {"lat": 26.61, "lon": -81.84},
                    {"lat": 26.61, "lon": -81.85},
                    {"lat": 26.60, "lon": -81.85},
                ],
            }
        ]
    }
    monkeypatch.setattr(
        data_fetch, "_post_overpass_buildings", lambda ql: payload
    )
    raw = data_fetch._fetch_osm_buildings_bytes(FORT_MYERS_BBOX)
    assert isinstance(raw, bytes) and len(raw) > 0
    # Round-trip the FlatGeobuf to confirm a building polygon survived.
    import io as _io

    import geopandas as gpd  # type: ignore[import-not-found]

    gdf = gpd.read_file(_io.BytesIO(raw))
    assert len(gdf) == 1
    assert gdf.geometry.iloc[0].geom_type in ("Polygon", "MultiPolygon")


def test_fetch_osm_buildings_bytes_emits_slim_columns_and_tags_sidecar(monkeypatch):
    """Inline FGB carries ONLY id-only props; on_tags gets the full tag bag."""
    pytest.importorskip("geopandas")
    pytest.importorskip("pyogrio")
    payload = {
        "elements": [
            {
                "type": "way",
                "id": 777,
                "tags": {
                    "building": "house",
                    "name": "Maison",
                    "height": "8",
                    "addr:street": "Rue X",
                },
                "geometry": [
                    {"lat": 26.60, "lon": -81.85},
                    {"lat": 26.60, "lon": -81.84},
                    {"lat": 26.61, "lon": -81.84},
                    {"lat": 26.61, "lon": -81.85},
                    {"lat": 26.60, "lon": -81.85},
                ],
            }
        ]
    }
    monkeypatch.setattr(
        data_fetch, "_post_overpass_buildings", lambda ql: payload
    )
    captured: list[dict] = []
    raw = data_fetch._fetch_osm_buildings_bytes(
        FORT_MYERS_BBOX, on_tags=lambda m: captured.append(m)
    )

    # The inline FGB columns are SLIM: only the id-only props survive (no
    # building / name / height inline) so the frontend GeoJSON stays small.
    import io as _io

    import geopandas as gpd  # type: ignore[import-not-found]

    gdf = gpd.read_file(_io.BytesIO(raw))
    non_geom_cols = {c for c in gdf.columns if c != "geometry"}
    assert non_geom_cols == {"osm_id", "osm_type", "fid"}
    assert "building" not in non_geom_cols
    assert "name" not in non_geom_cols
    assert gdf["fid"].iloc[0] == "w777"

    # on_tags received the FULL tag bag keyed by fid for the enrich sidecar.
    assert len(captured) == 1
    assert captured[0]["w777"] == {
        "building": "house",
        "name": "Maison",
        "height": "8",
        "addr:street": "Rue X",
    }


def test_fetch_buildings_osm_writes_tags_sidecar(monkeypatch):
    """End-to-end: fetch_buildings(osm) writes a sibling .tags.json sidecar."""
    fake_storage = FakeStorageClient()
    payload_tags = {"w777": {"building": "house", "name": "Maison"}}

    def fake_osm(bbox, on_tags=None):
        if on_tags is not None:
            on_tags(payload_tags)
        return b"FAKE_OSM_FGB"

    sidecar_writes: list[tuple] = []

    def fake_sidecar(bbox, source, tags_by_fid):
        sidecar_writes.append((source, tags_by_fid))

    monkeypatch.setattr(data_fetch, "_fetch_osm_buildings_bytes", fake_osm)
    monkeypatch.setattr(
        data_fetch, "_write_buildings_tags_sidecar", fake_sidecar
    )
    _patch_read_through(monkeypatch, fake_storage)

    layer = fetch_buildings(FORT_MYERS_BBOX, source="osm")
    assert layer.name == "Buildings (OSM)"
    # The sidecar writer was invoked with the osm tag bag.
    assert len(sidecar_writes) == 1
    assert sidecar_writes[0][0] == "osm"
    assert sidecar_writes[0][1] == payload_tags


def test_buildings_cache_uri_sidecar_is_sibling_of_fgb():
    """The .tags.json sidecar URI shares the SAME <key> as the .fgb."""
    fgb = data_fetch.buildings_cache_uri(FORT_MYERS_BBOX, "osm", "fgb")
    tags = data_fetch.buildings_cache_uri(
        FORT_MYERS_BBOX, "osm", data_fetch.BUILDINGS_TAGS_SIDECAR_EXT
    )
    assert fgb.endswith(".fgb")
    assert tags.endswith(".tags.json")
    # Same directory + same key stem (strip the differing extensions).
    assert fgb[: -len(".fgb")] == tags[: -len(".tags.json")]


def _square_geometry(
    lon_min: float, lat_min: float, lon_max: float, lat_max: float
) -> list[dict[str, float]]:
    """Closed-ring Overpass ``geometry`` for an axis-aligned square (lon/lat box)."""
    return [
        {"lat": lat_min, "lon": lon_min},
        {"lat": lat_min, "lon": lon_max},
        {"lat": lat_max, "lon": lon_max},
        {"lat": lat_max, "lon": lon_min},
        {"lat": lat_min, "lon": lon_min},
    ]


def test_fetch_osm_buildings_retains_edge_straddling_footprint(monkeypatch):
    """A building straddling the LEFT bbox edge is RETAINED (intersects, not clipped).

    NATE's bug: "asked for buildings in the bbox, missed some on the LEFT". The
    fetcher must keep a footprint that pokes outside the AOI edge — and keep it
    WHOLE (un-sliced), not chopped at the boundary.
    """
    pytest.importorskip("geopandas")
    pytest.importorskip("pyogrio")
    min_lon, min_lat, max_lon, max_lat = FORT_MYERS_BBOX  # left = -81.92
    # Building half outside the LEFT edge: spans from just outside min_lon to
    # just inside it.
    half_w = 0.001
    left_straddle = _square_geometry(
        min_lon - half_w, min_lat + 0.01, min_lon + half_w, min_lat + 0.02
    )
    payload = {
        "elements": [
            {"type": "way", "id": 1, "tags": {"building": "yes"},
             "geometry": left_straddle},
        ]
    }
    monkeypatch.setattr(data_fetch, "_post_overpass_buildings", lambda ql: payload)
    raw = data_fetch._fetch_osm_buildings_bytes(FORT_MYERS_BBOX)

    import io as _io

    import geopandas as gpd  # type: ignore[import-not-found]

    gdf = gpd.read_file(_io.BytesIO(raw))
    assert len(gdf) == 1, "edge-straddling building must be retained"
    # Whole, un-sliced: the western extent still reaches outside the bbox edge
    # (a clip would have snapped it to exactly min_lon).
    geom = gdf.geometry.iloc[0]
    assert geom.bounds[0] < min_lon, "footprint must NOT be clipped to the bbox edge"


def test_fetch_osm_buildings_excludes_fully_outside_footprint(monkeypatch):
    """A building wholly outside the bbox is EXCLUDED (honest UpstreamAPIError)."""
    pytest.importorskip("geopandas")
    min_lon, min_lat, max_lon, max_lat = FORT_MYERS_BBOX
    # Entirely west of (left of) the bbox, no intersection.
    outside = _square_geometry(
        min_lon - 0.05, min_lat + 0.01, min_lon - 0.04, min_lat + 0.02
    )
    payload = {
        "elements": [
            {"type": "way", "id": 2, "tags": {"building": "yes"},
             "geometry": outside},
        ]
    }
    monkeypatch.setattr(data_fetch, "_post_overpass_buildings", lambda ql: payload)
    with pytest.raises(UpstreamAPIError):
        data_fetch._fetch_osm_buildings_bytes(FORT_MYERS_BBOX)


def test_fetch_osm_buildings_symmetric_edge_coverage(monkeypatch):
    """Footprints straddling EACH of the four bbox edges are all retained.

    Guards against any side (left/right/top/bottom) being preferentially
    dropped — the intersects filter must be symmetric.
    """
    pytest.importorskip("geopandas")
    pytest.importorskip("pyogrio")
    min_lon, min_lat, max_lon, max_lat = FORT_MYERS_BBOX
    d = 0.001
    mid_lon = (min_lon + max_lon) / 2.0
    mid_lat = (min_lat + max_lat) / 2.0
    elements = [
        # Left edge straddle.
        {"type": "way", "id": 10, "tags": {"building": "yes"},
         "geometry": _square_geometry(min_lon - d, mid_lat - d, min_lon + d, mid_lat + d)},
        # Right edge straddle.
        {"type": "way", "id": 11, "tags": {"building": "yes"},
         "geometry": _square_geometry(max_lon - d, mid_lat - d, max_lon + d, mid_lat + d)},
        # Bottom edge straddle.
        {"type": "way", "id": 12, "tags": {"building": "yes"},
         "geometry": _square_geometry(mid_lon - d, min_lat - d, mid_lon + d, min_lat + d)},
        # Top edge straddle.
        {"type": "way", "id": 13, "tags": {"building": "yes"},
         "geometry": _square_geometry(mid_lon - d, max_lat - d, mid_lon + d, max_lat + d)},
    ]
    payload = {"elements": elements}
    monkeypatch.setattr(data_fetch, "_post_overpass_buildings", lambda ql: payload)
    raw = data_fetch._fetch_osm_buildings_bytes(FORT_MYERS_BBOX)

    import io as _io

    import geopandas as gpd  # type: ignore[import-not-found]

    gdf = gpd.read_file(_io.BytesIO(raw))
    assert len(gdf) == 4, "all four edge-straddling buildings must be retained"


# ---------------------------------------------------------------------------
# fetch_population — mocked Census REST.
# ---------------------------------------------------------------------------


def test_fetch_population_acs_opt_in_routes_to_acs_branch(monkeypatch):
    """Tier-2 opt-in: explicit ``dataset="acs_2022"`` still routes to ACS.

    Appendix F.1 makes WorldPop the Tier-1 default (see the no-dataset-arg
    test below), but the existing ACS path stays callable for tract-level
    precision queries — that's the Tier-2 routing rule.
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_acs_population_bytes",
        lambda bbox, dataset: b'{"type":"FeatureCollection","features":[]}',
    )
    # Guard: WorldPop branch must not be touched on this code path.
    def _worldpop_should_not_be_called(_bbox, _dataset):  # pragma: no cover
        raise AssertionError(
            "WorldPop branch should not be invoked when dataset='acs_2022' is passed"
        )

    monkeypatch.setattr(
        data_fetch, "_fetch_worldpop_population_bytes", _worldpop_should_not_be_called
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    layer = fetch_population(FORT_MYERS_BBOX, dataset="acs_2022")
    assert layer.layer_type == "vector"
    assert layer.units == "people"
    assert layer.uri.startswith(
        "s3://grace2-hazard-cache-226996537797/cache/static-30d/population/"
    )
    assert layer.uri.endswith(".json")


def test_fetch_population_default_routes_to_worldpop_not_acs(monkeypatch):
    """Appendix F.1 (v0.3.16): ``fetch_population(bbox)`` defaults to WorldPop.

    The default-arg path MUST hit the WorldPop branch and MUST NOT hit the
    ACS branch (a Tier-2 source that requires a Census API key for non-
    trivial volume — Tier-1 preference rule says no-key defaults).
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    worldpop_calls: list[tuple[Any, str]] = []

    def _capturing_worldpop(bbox, dataset, target_resolution_m=1000):
        worldpop_calls.append((bbox, dataset))
        return b"FAKE_WORLDPOP_COG_BYTES"

    monkeypatch.setattr(
        data_fetch, "_fetch_worldpop_population_bytes", _capturing_worldpop
    )
    # Guard: ACS branch must not be touched on the default path.
    def _acs_should_not_be_called(_bbox, _dataset):  # pragma: no cover
        raise AssertionError(
            "ACS branch (Tier-2, key-required) must not be invoked by the default "
            "fetch_population(bbox) call — Appendix F.1 says Tier-1 (WorldPop) is "
            "the default."
        )

    monkeypatch.setattr(
        data_fetch, "_fetch_acs_population_bytes", _acs_should_not_be_called
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    layer = fetch_population(FORT_MYERS_BBOX)  # no dataset= arg
    assert worldpop_calls, "WorldPop fetcher should have been called for default path"
    assert worldpop_calls[0][1].startswith("worldpop_"), worldpop_calls
    assert layer.layer_type == "raster"  # WorldPop is a raster COG, not a GeoJSON FC
    assert layer.units == "people"
    assert layer.uri.startswith(
        "s3://grace2-hazard-cache-226996537797/cache/static-30d/population/"
    )
    assert layer.uri.endswith(".tif")


def test_fetch_population_worldpop_writes_tif_cog_to_cache(monkeypatch):
    """The WorldPop default branch writes a ``.tif`` COG to the population cache prefix."""
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_worldpop_population_bytes",
        lambda bbox, dataset, target_resolution_m=1000: b"FAKE_WORLDPOP_COG_BYTES",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    layer = fetch_population(FORT_MYERS_BBOX, dataset="worldpop_2020")
    # Cache landed at .tif under the population prefix.
    paths = list(fake_storage.store.keys())
    assert len(paths) == 1
    assert paths[0].startswith("cache/static-30d/population/")
    assert paths[0].endswith(".tif")
    assert fake_storage.store[paths[0]] == b"FAKE_WORLDPOP_COG_BYTES"
    # GCP decommissioned: TTL eviction is an S3 bucket-lifecycle rule (no
    # per-object customTime); assert the boto3 put landed instead.
    assert fake_storage.last_put is not None
    # LayerURI return shape is raster + meters/people.
    assert layer.layer_type == "raster"
    assert layer.uri.endswith(".tif")


def test_fetch_population_rejects_unknown_dataset():
    """A dataset that's neither WorldPop nor ACS is rejected as BboxInvalidError."""
    with pytest.raises(BboxInvalidError):
        fetch_population(FORT_MYERS_BBOX, dataset="landscan")


# ---------------------------------------------------------------------------
# Phase-2 WorldPop 100m opt-in (resolution lever).
# ---------------------------------------------------------------------------


def test_worldpop_url_for_100m_returns_unadj_native_url():
    """resolution_m<=100 -> base Global_2000_2020 tree + _UNadj suffix (NO _1km)."""
    url = data_fetch._worldpop_url_for("USA", 2020, resolution_m=100)
    assert "Global_2000_2020/" in url
    assert "Global_2000_2020_1km" not in url
    assert url.endswith("usa_ppp_2020_UNadj.tif"), url
    assert "_1km_Aggregated" not in url


def test_worldpop_url_for_default_returns_1km_url():
    """The 1km default URL is byte-identical to the prior fixed behavior."""
    default_url = data_fetch._worldpop_url_for("USA", 2020)
    explicit_1km = data_fetch._worldpop_url_for("USA", 2020, resolution_m=1000)
    assert default_url == explicit_1km
    assert default_url == (
        "https://data.worldpop.org/GIS/Population/Global_2000_2020_1km/2020/"
        "USA/usa_ppp_2020_1km_Aggregated.tif"
    )


# ---------------------------------------------------------------------------
# WorldPop vintage-year normalize-then-validate (the 'goes18 vs goes-18'
# identifier-format class): a year outside the published Global_2000_2020
# product window MUST fail LOUD with a clear typed error at parse time, NOT
# build a well-formed URL into a non-existent path that 404s downstream.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("year", [2000, 2005, 2010, 2015, 2020])
def test_worldpop_year_from_dataset_accepts_in_range_vintages(year):
    """Every vintage in [2000,2020] parses to the exact int year."""
    assert data_fetch._worldpop_year_from_dataset(f"worldpop_{year}") == year


@pytest.mark.parametrize(
    "year",
    [1999, 1850, 2021, 2024, 2030],
)
def test_worldpop_year_from_dataset_rejects_out_of_range_year(year):
    """An out-of-range vintage (e.g. the docstring-advertised 2024) fails LOUD.

    Regression for the 'goes18 vs goes-18' identifier-format hazard: previously
    ``worldpop_2024`` composed a well-formed URL into a non-existent path and
    only surfaced as a bare HTTP 404 after a network round-trip. The typed
    error must name the dataset, the offending year, and the supported window.
    """
    dataset = f"worldpop_{year}"
    with pytest.raises(UpstreamAPIError) as excinfo:
        data_fetch._worldpop_year_from_dataset(dataset)
    msg = str(excinfo.value)
    assert dataset in msg, msg
    assert str(year) in msg, msg
    assert "[2000,2020]" in msg, msg


def test_worldpop_year_from_dataset_rejects_non_numeric_suffix():
    """A non-numeric vintage suffix fails with the 'worldpop_YYYY' guidance."""
    with pytest.raises(UpstreamAPIError) as excinfo:
        data_fetch._worldpop_year_from_dataset("worldpop_latest")
    assert "worldpop_YYYY" in str(excinfo.value)


def test_worldpop_year_from_dataset_rejects_non_worldpop_prefix():
    """A dataset that is not a worldpop_ token is rejected before parsing."""
    with pytest.raises(UpstreamAPIError) as excinfo:
        data_fetch._worldpop_year_from_dataset("acs_2022")
    assert "WorldPop branch" in str(excinfo.value)


def test_fetch_worldpop_population_bytes_rejects_2024_before_network(monkeypatch):
    """fetch_population(worldpop_2024) raises a typed error WITHOUT any HTTP call.

    The validation must fire at parse time (before requests.get / rasterio),
    so the malformed identifier never reaches the network as a bare 404. We
    fail the test if any HTTP request is attempted.
    """

    def _no_network(*_a, **_kw):  # pragma: no cover - must not be reached
        raise AssertionError("requests.get must NOT be called for an out-of-range year")

    monkeypatch.setattr(data_fetch.requests, "get", _no_network)

    with pytest.raises(UpstreamAPIError) as excinfo:
        data_fetch._fetch_worldpop_population_bytes(FORT_MYERS_BBOX, "worldpop_2024")
    assert "2024" in str(excinfo.value)
    assert "[2000,2020]" in str(excinfo.value)


def test_worldpop_url_built_only_for_validated_year_matches_real_format():
    """A validated in-range year composes the EXACT published bucket path."""
    year = data_fetch._worldpop_year_from_dataset("worldpop_2020")
    url = data_fetch._worldpop_url_for("USA", year)
    assert url == (
        "https://data.worldpop.org/GIS/Population/Global_2000_2020_1km/2020/"
        "USA/usa_ppp_2020_1km_Aggregated.tif"
    )


def _patch_population_cache(monkeypatch, fake_storage):
    """Route fetch_population's read_through through a fake storage client."""
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )


def test_fetch_population_cache_key_includes_target_resolution_m(monkeypatch):
    """100m vs 1km fetches get DISTINCT cache keys (different upstream products)."""
    captured: list[int] = []

    def _capturing_worldpop(bbox, dataset, target_resolution_m=1000):
        captured.append(target_resolution_m)
        return f"FAKE_WORLDPOP_{target_resolution_m}".encode()

    monkeypatch.setattr(
        data_fetch, "_fetch_worldpop_population_bytes", _capturing_worldpop
    )

    fake_storage = FakeStorageClient()
    _patch_population_cache(monkeypatch, fake_storage)

    # Default 1km then opt-in 100m on the SAME bbox -> two distinct cache keys.
    fetch_population(FORT_MYERS_BBOX, dataset="worldpop_2020")
    fetch_population(
        FORT_MYERS_BBOX, dataset="worldpop_2020", target_resolution_m=100
    )

    assert captured == [1000, 100], captured
    keys = list(fake_storage.store.keys())
    assert len(keys) == 2, keys  # distinct keys -> both fetches landed separately
    assert keys[0] != keys[1], keys


# ---------------------------------------------------------------------------
# geocode_location — mocked Nominatim.
# ---------------------------------------------------------------------------


def test_geocode_location_happy_path(monkeypatch):
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod
    import json as _json

    fake_payload = {
        "name": "Fort Myers, Lee County, Florida, United States",
        "latitude": 26.6406,
        "longitude": -81.8723,
        "bbox": [-81.93, 26.55, -81.78, 26.71],
        "source": "nominatim",
        "query": "Fort Myers, FL",
        "osm_type": "relation",
        "osm_id": 12345,
        "place_id": 67890,
    }
    monkeypatch.setattr(
        data_fetch,
        "_fetch_nominatim_geocode_bytes",
        lambda query: _json.dumps(fake_payload).encode("utf-8"),
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    result = geocode_location("Fort Myers, FL")
    assert result["source"] == "nominatim"
    assert result["bbox"] == [-81.93, 26.55, -81.78, 26.71]
    assert "Fort Myers" in result["name"]
    # No s3:// URI leaks into the returned payload (Tier separation).
    assert "s3://" not in str(result)


def test_geocode_location_rejects_empty_query():
    with pytest.raises(BboxInvalidError):
        geocode_location("   ")


# ---------------------------------------------------------------------------
# geocode_location — state-snap fallback (NATE directive 2026-06-17).
#
# A vague/regional query ("south Florida") that geocodes to an arbitrary /
# wrong-state OSM feature must snap to the full state bbox with an honest note,
# while a PRECISE in-state query ("Fort Myers, FL") must pass through unchanged.
# ---------------------------------------------------------------------------


def _bind_geocode_cache(monkeypatch):
    """Wire read_through to a fresh fake-storage client (shared test plumbing)."""
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )


# --- _extract_us_state edge cases ------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        # Directional / qualifier stripping.
        ("south Florida", "Florida"),
        ("protected areas in south Florida", "Florida"),
        ("central Texas", "Texas"),
        ("upstate New York", "New York"),
        ("greater metro Los Angeles California", "California"),
        # F71: vernacular sub-state regions whose TAIL (after qualifier strip)
        # is a full state name resolve via steps (2)/(2b) — NATE's headline
        # "South Florida" case. (Interior-position matches like "the Florida
        # Panhandle" were intentionally NOT added — see the reverted (2c) note
        # in _extract_us_state; the any-position scan regressed "Kansas City, MO"
        # and "the Washington Monument".)
        ("Southern California", "California"),
        ("Central Texas", "Texas"),
        ("South Florida", "Florida"),
        # Full-name match BEFORE directional strip would eat the prefix.
        ("west virginia", "West Virginia"),
        ("north carolina", "North Carolina"),
        ("new mexico", "New Mexico"),
        ("rhode island", "Rhode Island"),
        # Bare state names.
        ("Kansas", "Kansas"),
        ("california", "California"),
        # USPS abbreviation in the "City, ST" idiom.
        ("Fort Myers, FL", "Florida"),
        ("wildfires near Los Angeles, CA", "California"),
        # County form still detects the state.
        ("Lee County Florida", "Florida"),
        # DC variants.
        ("Washington DC", "District of Columbia"),
        ("district of columbia", "District of Columbia"),
    ],
)
def test_extract_us_state_detects(query, expected):
    assert data_fetch._extract_us_state(query) == expected


@pytest.mark.parametrize(
    "query",
    [
        "",
        "   ",
        "Houston",            # city, not a state
        "Gulf of Mexico",     # marine zone by name
        "in the woods",       # "in" must NOT match Indiana (word-boundary guard)
        "or maybe later",     # "or" must NOT match Oregon
        "Canada",             # not a US state
        "Puerto Rico",        # territory — has no offline bbox row
        # BARE dangerous 2-letter words (whole query) must NOT leak a state via
        # resolve_state_code's unconditional 2-letter fast path (step-4 guard).
        "in",
        "or",
        "ok",
        "hi",
        "me",
        "co",
        "la",
        # ...and queries that REDUCE to a bare dangerous word after the
        # leading-qualifier strip ("the or" -> "or").
        "the or",
        "near or",
        # F71 sliding-window guard: a bare dangerous 2-letter word sitting in an
        # INTERIOR position (not head/tail) must STILL NOT leak a state — the
        # full-name scanner matches FULL state names only, never abbreviations.
        "fly in a plane",     # interior "in" must NOT match Indiana
        "this or that thing",  # interior "or" must NOT match Oregon
        "park me here please",  # interior "me" must NOT match Maine
    ],
)
def test_extract_us_state_rejects(query):
    assert data_fetch._extract_us_state(query) is None


def test_extract_us_state_abbreviation_word_boundary_guard():
    """A dangerous bare English word ('in', 'or') is not a state abbreviation.

    But the SAME letters in the comma idiom ('Bloomington, IN') ARE.
    """
    assert data_fetch._extract_us_state("flooding in the valley") is None
    assert data_fetch._extract_us_state("Bloomington, IN") == "Indiana"
    assert data_fetch._extract_us_state("Portland, OR") == "Oregon"
    # Non-string input never raises.
    assert data_fetch._extract_us_state(None) is None  # type: ignore[arg-type]
    assert data_fetch._extract_us_state(42) is None  # type: ignore[arg-type]


# --- offline backstop table plausibility -----------------------------------


@pytest.mark.parametrize(
    "state,lon_lo,lon_hi,lat_lo,lat_hi",
    [
        # (state, expected min_lon range, expected max_lat range) — generous
        # plausibility bands around known cartographic extents.
        ("Florida", -88.0, -79.0, 24.0, 31.5),
        ("California", -125.0, -113.5, 32.0, 42.5),
        ("Texas", -107.5, -93.0, 25.5, 37.0),
        ("Kansas", -102.5, -94.0, 36.5, 40.5),
        ("New York", -80.5, -71.0, 40.0, 45.5),
    ],
)
def test_us_state_bbox_table_plausible(state, lon_lo, lon_hi, lat_lo, lat_hi):
    bbox = data_fetch._US_STATE_BBOX[state]
    min_lon, min_lat, max_lon, max_lat = bbox
    # Canonical ordering.
    assert min_lon < max_lon and min_lat < max_lat
    # Within plausibility bands.
    assert lon_lo <= min_lon <= lon_hi
    assert lon_lo <= max_lon <= lon_hi
    assert lat_lo <= min_lat <= lat_hi
    assert lat_lo <= max_lat <= lat_hi


def test_us_state_bbox_table_has_50_states_plus_dc():
    assert len(data_fetch._US_STATE_BBOX) == 51
    assert "District of Columbia" in data_fetch._US_STATE_BBOX
    # Every row is a valid WGS84 ordered bbox.
    for name, bbox in data_fetch._US_STATE_BBOX.items():
        min_lon, min_lat, max_lon, max_lat = bbox
        assert -180.0 <= min_lon < max_lon <= 180.0, name
        assert -90.0 <= min_lat < max_lat <= 90.0, name


# --- (a) precise in-state query returns precise bbox unchanged --------------


def test_geocode_precise_in_state_query_not_snapped(monkeypatch):
    """'Fort Myers, FL' resolves precisely; centroid is in FL -> no widening."""
    import json as _json

    precise = {
        "name": "Fort Myers, Lee County, Florida, United States",
        "latitude": 26.6406,
        "longitude": -81.8723,
        "bbox": [-81.93, 26.55, -81.78, 26.71],
        "source": "nominatim",
        "query": "Fort Myers, FL",
        "osm_type": "relation",
        "osm_id": 12345,
        "place_id": 67890,
    }
    monkeypatch.setattr(
        data_fetch,
        "_fetch_nominatim_geocode_bytes",
        lambda query: _json.dumps(precise).encode("utf-8"),
    )
    _bind_geocode_cache(monkeypatch)

    result = geocode_location("Fort Myers, FL")
    assert result["source"] == "nominatim"
    assert result["bbox"] == [-81.93, 26.55, -81.78, 26.71]
    assert "fallback_reason" not in result


def test_geocode_precise_county_query_not_snapped(monkeypatch):
    """'Lee County Florida' (a county) stays precise — not widened to state."""
    import json as _json

    precise = {
        "name": "Lee County, Florida, United States",
        "latitude": 26.66,
        "longitude": -81.84,
        "bbox": [-82.27, 26.32, -81.56, 26.79],
        "source": "nominatim",
        "query": "Lee County Florida",
        "osm_type": "relation",
        "osm_id": 222,
        "place_id": 333,
    }
    monkeypatch.setattr(
        data_fetch,
        "_fetch_nominatim_geocode_bytes",
        lambda query: _json.dumps(precise).encode("utf-8"),
    )
    _bind_geocode_cache(monkeypatch)

    result = geocode_location("Lee County Florida")
    assert result["source"] == "nominatim"
    assert result["bbox"] == [-82.27, 26.32, -81.56, 26.79]
    assert "fallback_reason" not in result


# --- (b) wrong-state result snaps to the state with honest note -------------


def test_geocode_south_florida_wrong_state_snaps_to_florida(monkeypatch):
    """'south Florida' resolving to KANSAS snaps to FL via the offline table."""
    import json as _json

    # The pathological observed behavior: Nominatim returns a Kansas feature.
    wrong = {
        "name": "Somewhere, Kansas, United States",
        "latitude": 38.5,
        "longitude": -98.0,
        "bbox": [-98.1, 38.4, -97.9, 38.6],
        "source": "nominatim",
        "query": "south Florida",
        "osm_type": "node",
        "osm_id": 999,
        "place_id": 111,
    }
    monkeypatch.setattr(
        data_fetch,
        "_fetch_nominatim_geocode_bytes",
        lambda query: _json.dumps(wrong).encode("utf-8"),
    )
    _bind_geocode_cache(monkeypatch)
    # Force the offline-table path (no live state lookup) for a deterministic
    # bbox assertion.
    monkeypatch.setattr(
        data_fetch.requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            data_fetch.requests.RequestException("offline")
        ),
    )

    result = geocode_location("south Florida")
    assert result["source"] == "state-bbox-fallback"
    assert result["bbox"] == data_fetch._US_STATE_BBOX["Florida"]
    assert result["state_bbox_source"] == "offline-state-table"
    # Honest narration note present and truthful.
    assert "fallback_reason" in result
    assert "Florida" in result["fallback_reason"]
    assert "south Florida" in result["fallback_reason"]
    # Backward-compatible key shape preserved.
    for key in (
        "name", "bbox", "latitude", "longitude", "source", "query",
        "osm_type", "osm_id", "place_id",
    ):
        assert key in result
    assert result["osm_id"] is None
    # Centroid is inside the Florida bbox.
    fl = data_fetch._US_STATE_BBOX["Florida"]
    assert fl[0] <= result["longitude"] <= fl[2]
    assert fl[1] <= result["latitude"] <= fl[3]


def test_geocode_capitalized_south_florida_snaps_to_florida_centroid(monkeypatch):
    """F71 headline: 'South Florida' -> KANSAS hit -> snap; centroid inside FL.

    NATE 2026-06-17 confirmed geocode_location('South Florida') resolved to
    Kansas every time (no comma; the bare-word guard skipped comma-less tokens).
    The fix extracts the full state NAME 'Florida' from the comma-less phrase,
    the Kansas centroid fails the in-state sanity check, and the result snaps to
    the Florida bbox via the state-bbox-fallback. We mock Nominatim to return a
    Kansas hit and assert the snapped bbox's centroid lands inside Florida.
    """
    import json as _json

    kansas_hit = {
        "name": "Some Place, Kansas, United States",
        "latitude": 38.5,
        "longitude": -98.0,
        "bbox": [-98.1, 38.4, -97.9, 38.6],
        "source": "nominatim",
        "query": "South Florida",
        "osm_type": "node",
        "osm_id": 4242,
        "place_id": 5353,
    }
    monkeypatch.setattr(
        data_fetch,
        "_fetch_nominatim_geocode_bytes",
        lambda query: _json.dumps(kansas_hit).encode("utf-8"),
    )
    _bind_geocode_cache(monkeypatch)
    # Force the offline-table path so the bbox/centroid are deterministic.
    monkeypatch.setattr(
        data_fetch.requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            data_fetch.requests.RequestException("offline")
        ),
    )

    result = geocode_location("South Florida")

    # The snap fired with the contracted source + an honest fallback note.
    assert result["source"] == "state-bbox-fallback"
    assert "fallback_reason" in result
    assert "Florida" in result["fallback_reason"]

    # The returned bbox's CENTROID is inside the Florida envelope (the whole
    # point of F71 — it is NOT in Kansas).
    min_lon, min_lat, max_lon, max_lat = result["bbox"]
    cx = 0.5 * (min_lon + max_lon)
    cy = 0.5 * (min_lat + max_lat)
    fl = data_fetch._US_STATE_BBOX["Florida"]
    assert fl[0] <= cx <= fl[2]
    assert fl[1] <= cy <= fl[3]
    # And the reported centroid lat/lon (used to snap the map) is also in FL.
    assert fl[0] <= result["longitude"] <= fl[2]
    assert fl[1] <= result["latitude"] <= fl[3]


def test_geocode_bare_dangerous_word_does_not_snap_to_state(monkeypatch):
    """F71 guard: a bare 'in'/'or' query never resolves to a state (no snap).

    'in' must NOT leak to Indiana and 'or' must NOT leak to Oregon — so when the
    primary geocode of such a token finds no match, the typed GeocodeNoMatchError
    must propagate (no state detected -> no silent snap).
    """

    def _boom(query):
        raise GeocodeNoMatchError(f"Could not locate {query!r}.")

    monkeypatch.setattr(data_fetch, "_fetch_nominatim_geocode_bytes", _boom)
    _bind_geocode_cache(monkeypatch)

    # No state is detected for these bare dangerous words, so the failure is
    # NOT swallowed by a state-snap.
    assert data_fetch._extract_us_state("in") is None
    assert data_fetch._extract_us_state("or") is None
    for q in ("in", "or"):
        with pytest.raises(GeocodeNoMatchError):
            geocode_location(q)


def test_geocode_wrong_state_prefers_live_osm_state_boundary(monkeypatch):
    """When the live state lookup succeeds, the snap uses the OSM admin bbox."""
    import json as _json

    wrong = {
        "name": "Somewhere, Kansas, United States",
        "latitude": 38.5,
        "longitude": -98.0,
        "bbox": [-98.1, 38.4, -97.9, 38.6],
        "source": "nominatim",
        "query": "south Florida",
        "osm_type": "node",
        "osm_id": 999,
        "place_id": 111,
    }
    monkeypatch.setattr(
        data_fetch,
        "_fetch_nominatim_geocode_bytes",
        lambda query: _json.dumps(wrong).encode("utf-8"),
    )
    _bind_geocode_cache(monkeypatch)

    # Nominatim featuretype=state returns the real FL admin boundingbox
    # ([south, north, west, east] strings, per Nominatim convention).
    class _FakeStateResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "boundingbox": ["24.396", "31.001", "-87.635", "-79.974"],
                    "lat": "27.7",
                    "lon": "-83.8",
                }
            ]

    captured = {}

    def _fake_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["params"] = params
        return _FakeStateResp()

    monkeypatch.setattr(data_fetch.requests, "get", _fake_get)

    result = geocode_location("south Florida")
    assert result["source"] == "state-bbox-fallback"
    assert result["state_bbox_source"] == "nominatim-state"
    # bbox normalized to [min_lon, min_lat, max_lon, max_lat].
    assert result["bbox"] == [-87.635, 24.396, -79.974, 31.001]
    # The live state lookup was scoped to the US with featuretype=state.
    assert captured["params"]["countrycodes"] == "us"
    assert captured["params"]["featuretype"] == "state"


# --- (c) no-result + state detected snaps to state --------------------------


def test_geocode_no_result_with_state_detected_snaps(monkeypatch):
    """Nominatim returns nothing, but 'south Florida' has a detectable state.

    GeocodeNoMatchError subclasses UpstreamAPIError, so the state-snap fallback
    STILL fires when a US state is recognized in the query.
    """

    def _boom(query):
        raise GeocodeNoMatchError(f"Could not locate {query!r}.")

    monkeypatch.setattr(data_fetch, "_fetch_nominatim_geocode_bytes", _boom)
    _bind_geocode_cache(monkeypatch)
    # Offline path for deterministic bbox.
    monkeypatch.setattr(
        data_fetch.requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            data_fetch.requests.RequestException("offline")
        ),
    )

    result = geocode_location("protected areas in south Florida")
    assert result["source"] == "state-bbox-fallback"
    assert result["bbox"] == data_fetch._US_STATE_BBOX["Florida"]
    assert "Florida" in result["fallback_reason"]


# --- (d) no state + no result still raises (no silent swallow) --------------


def test_geocode_no_result_no_state_still_raises(monkeypatch):
    """A genuine no-match with NO detectable state propagates GeocodeNoMatchError."""

    def _boom(query):
        raise GeocodeNoMatchError(f"Could not locate {query!r}.")

    monkeypatch.setattr(data_fetch, "_fetch_nominatim_geocode_bytes", _boom)
    _bind_geocode_cache(monkeypatch)

    with pytest.raises(GeocodeNoMatchError):
        geocode_location("Atlantis")


# --- typed GEOCODE_NO_MATCH from the real Nominatim fetch branches -----------


class _FakeGeocodeResp:
    """Minimal requests.Response stand-in returning a fixed JSON body."""

    status_code = 200

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


def test_geocode_empty_body_raises_typed_no_match(monkeypatch):
    """An empty Nominatim result body for an unknown non-US place raises
    GeocodeNoMatchError with the non-retryable GEOCODE_NO_MATCH code.

    Drives the real ``_fetch_nominatim_geocode_bytes`` empty-result branch
    (no _fetch_nominatim_geocode_bytes monkeypatch) so the typed-error contract
    is locked end-to-end through ``geocode_location``.
    """
    monkeypatch.setattr(
        data_fetch.requests,
        "get",
        lambda *a, **kw: _FakeGeocodeResp([]),
    )
    _bind_geocode_cache(monkeypatch)

    # "Atlantis" has no detectable US state, so the no-match error propagates
    # instead of being swallowed by a state-snap.
    assert data_fetch._extract_us_state("Atlantis") is None
    with pytest.raises(GeocodeNoMatchError) as excinfo:
        geocode_location("Atlantis")
    assert excinfo.value.error_code == "GEOCODE_NO_MATCH"
    assert excinfo.value.retryable is False


def test_geocode_malformed_boundingbox_raises_typed_no_match(monkeypatch):
    """A top hit whose boundingbox is the wrong length raises the typed
    GeocodeNoMatchError (non-retryable GEOCODE_NO_MATCH) from the real fetch.
    """
    malformed = [
        {
            "display_name": "Somewhere",
            "lat": "10.0",
            "lon": "20.0",
            # Only two values -> len(bb) != 4 -> malformed-boundingbox branch.
            "boundingbox": ["10.0", "11.0"],
        }
    ]
    monkeypatch.setattr(
        data_fetch.requests,
        "get",
        lambda *a, **kw: _FakeGeocodeResp(malformed),
    )
    _bind_geocode_cache(monkeypatch)

    assert data_fetch._extract_us_state("Atlantis") is None
    with pytest.raises(GeocodeNoMatchError) as excinfo:
        geocode_location("Atlantis")
    assert excinfo.value.error_code == "GEOCODE_NO_MATCH"
    assert excinfo.value.retryable is False


# --- _resolve_state_bbox falls back to offline table on live failure --------


def test_resolve_state_bbox_falls_back_to_table(monkeypatch):
    monkeypatch.setattr(
        data_fetch.requests,
        "get",
        lambda *a, **kw: (_ for _ in ()).throw(
            data_fetch.requests.RequestException("down")
        ),
    )
    bbox, lat, lon, source = data_fetch._resolve_state_bbox("Texas")
    assert source == "offline-state-table"
    assert bbox == data_fetch._US_STATE_BBOX["Texas"]
    # Centroid inside the bbox.
    assert bbox[0] <= lon <= bbox[2]
    assert bbox[1] <= lat <= bbox[3]


# ---------------------------------------------------------------------------
# job-0039 — fetch_landcover (NLCD MRLC WMS).
# ---------------------------------------------------------------------------


from grace2_agent.tools.data_fetch import (  # noqa: E402 — after main test surface
    fetch_landcover,
    fetch_river_geometry,
    lookup_precip_return_period,
)


def test_fetch_landcover_is_registered_with_static_30d():
    """Registration assertion: ``fetch_landcover`` registered with the right metadata."""
    entry = TOOL_REGISTRY["fetch_landcover"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "landcover"
    assert entry.metadata.cacheable is True


def test_fetch_landcover_docstring_records_access_tier():
    """§F.1.1 docstring discipline: tier name MUST appear in the docstring.

    Live verification (2026-06-07) found NLCD is **Tier 2 (OGC service —
    MRLC WMS)**, NOT the Tier 3 the kickoff inferred. Either tier label
    must be present (we want to enforce *some* tier is named, not which one
    — the deviation is captured as OQ-39-NLCD-TIER-DEVIATION).
    """
    doc = fetch_landcover.__doc__ or ""
    assert "Access pattern:" in doc, "docstring must name the access tier per §F.1.1"
    assert "Tier" in doc, "docstring must name the access tier per §F.1.1"


def test_fetch_landcover_returns_nlcd_vintage_year_sidecar(monkeypatch):
    """Invariant 7 mitigation per OQ-4 §4: vintage year MUST be sidecar to LayerURI.

    ``build_sfincs_model`` (job-0042) consumes the vintage year to validate
    the Manning's mapping CSV covers the NLCD class encoding before the
    HydroMT roughness component is invoked. Skipping this would surface the
    silent-wrong-answer failure mode HydroMT exhibits for unmatched classes.

    Because ``LayerURI`` is FROZEN with ``extra="forbid"``, the sidecar is
    a top-level key on the returned dict, NOT a LayerURI field. The kickoff's
    example syntax ``LayerURI.metadata[...]`` is illustrative — see
    OQ-39-LANDCOVER-RETURN-SHAPE-CONTRACT-PROMOTION.
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_nlcd_landcover_bytes",
        lambda bbox, year, resolution_m=30: b"FAKE_NLCD_GEOTIFF_BYTES",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    result = fetch_landcover(FORT_MYERS_BBOX, dataset="nlcd_2021")
    assert isinstance(result, dict), "fetch_landcover returns a dict (LayerURI + sidecar)"
    assert "layer" in result, "dict must carry the LayerURI under key 'layer'"
    assert "nlcd_vintage_year" in result, "Invariant 7 sidecar required"
    assert result["nlcd_vintage_year"] == 2021
    assert result["dataset"] == "nlcd_2021"
    # job-0044 hotfix: switched WMS -> WCS 1.0.0 because WMS GetMap returned
    # palette-encoded indices instead of canonical NLCD class integers.
    assert result["source"] == "mrlc-wcs"

    layer = result["layer"]
    assert layer.layer_type == "raster"
    assert layer.style_preset == "categorical_landcover"
    assert layer.units == "nlcd_class_code"
    assert layer.uri.startswith(
        "s3://grace2-hazard-cache-226996537797/cache/static-30d/landcover/"
    )
    assert layer.uri.endswith(".tif")


def test_fetch_landcover_routes_through_read_through_writes_cache(monkeypatch):
    """FR-CE-8: ``fetch_landcover`` routes through ``read_through`` (cache shim)."""
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_nlcd_landcover_bytes",
        lambda bbox, year, resolution_m=30: b"FAKE_NLCD_GEOTIFF_BYTES",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    fetch_landcover(FORT_MYERS_BBOX, dataset="nlcd_2021")
    # Cache landed at .tif under the landcover prefix.
    paths = list(fake_storage.store.keys())
    assert len(paths) == 1
    assert paths[0].startswith("cache/static-30d/landcover/")
    assert paths[0].endswith(".tif")
    assert fake_storage.store[paths[0]] == b"FAKE_NLCD_GEOTIFF_BYTES"
    # GCP decommissioned: TTL eviction is an S3 bucket-lifecycle rule (no
    # per-object customTime); assert the boto3 put landed instead.
    assert fake_storage.last_put is not None


def test_fetch_landcover_quantizes_bbox_to_30m_nlcd_grid(monkeypatch):
    """Per-source quantization (acceptance criterion 3): NLCD 30 m native grid.

    Two callers whose bbox edges differ by sub-meter floats at 30 m
    resolution should hit the same cache key (dedup-via-quantization).
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_nlcd_landcover_bytes",
        lambda bbox, year, resolution_m=30: b"FAKE_NLCD_GEOTIFF_BYTES",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    base = (-81.9000001, 26.5500001, -81.8000001, 26.6800001)
    jitter = (-81.9000002, 26.5500002, -81.8000002, 26.6800002)
    r1 = fetch_landcover(base, dataset="nlcd_2021")
    r2 = fetch_landcover(jitter, dataset="nlcd_2021")
    # Both should hit the same cache entry (one stored path).
    assert len(fake_storage.store) == 1
    assert r1["layer"].uri == r2["layer"].uri


def test_fetch_landcover_rejects_unknown_dataset():
    with pytest.raises(BboxInvalidError):
        fetch_landcover(FORT_MYERS_BBOX, dataset="usgs_nlcd_2023_v3")


# ---------------------------------------------------------------------------
# dataset alias fix - bare 'nlcd' / 'nlcd_' resolve to the default vintage.
#
# Live drive found the model calling dataset='nlcd', hitting the typed
# error, retrying with dataset='nlcd_' (also a typed error), then finally
# landing on 'nlcd_2021'. Each retry re-triggered the resolution-confirm
# gate on the same bbox and the second gate hung forever (see server.py
# turn-memory fix). This section proves the aliases resolve without a
# retry loop, while an explicit bad vintage still errors.
# ---------------------------------------------------------------------------


def test_fetch_landcover_bare_nlcd_alias_resolves_to_default_vintage(monkeypatch):
    """``dataset='nlcd'`` (no vintage) is accepted as an alias for the default."""
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_nlcd_landcover_bytes",
        lambda bbox, year, resolution_m=30: b"FAKE_NLCD_GEOTIFF_BYTES",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    result = fetch_landcover(FORT_MYERS_BBOX, dataset="nlcd")
    assert result["nlcd_vintage_year"] == 2021
    assert result["dataset"] == data_fetch._DEFAULT_NLCD_DATASET


def test_fetch_landcover_trailing_underscore_nlcd_alias_resolves_to_default_vintage(
    monkeypatch,
):
    """``dataset='nlcd_'`` (trailing underscore, no year) is accepted the same way."""
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_nlcd_landcover_bytes",
        lambda bbox, year, resolution_m=30: b"FAKE_NLCD_GEOTIFF_BYTES",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    result = fetch_landcover(FORT_MYERS_BBOX, dataset="nlcd_")
    assert result["nlcd_vintage_year"] == 2021
    assert result["dataset"] == data_fetch._DEFAULT_NLCD_DATASET


def test_fetch_landcover_unknown_vintage_year_still_errors(monkeypatch):
    """An explicit but out-of-catalog vintage (e.g. 'nlcd_1875') still errors.

    The alias fix must NOT loosen validation of explicit 'nlcd_YYYY' values --
    only bare 'nlcd' / 'nlcd_' get the default-vintage fallback. 1875 parses
    as a valid int but is not in the MRLC WCS catalog, so this exercises the
    real (unmocked) ``_fetch_nlcd_landcover_bytes`` year check, which raises
    before any network call is made.
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )
    with pytest.raises(UpstreamAPIError):
        fetch_landcover(FORT_MYERS_BBOX, dataset="nlcd_1875")


def test_fetch_landcover_esa_worldcover_not_implemented(monkeypatch):
    """ESA WorldCover opt-in is reserved; v0.1 substrate raises UpstreamAPIError."""
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )
    with pytest.raises(UpstreamAPIError):
        fetch_landcover(FORT_MYERS_BBOX, dataset="esa_worldcover_2021")


def test_fetch_landcover_rejects_oversized_bbox():
    """The 5,000,000 km^2 hard ceiling rejects continent-scale bboxes.

    State/multi-state bboxes (the old 10,000 km^2 hard-fail zone) are now
    served via auto-coarsened resolution through the fetch-resolution gate;
    only continent-scale requests still hard-fail.
    """
    continent = (-125.0, 24.0, -66.0, 50.0)  # whole CONUS, ~ 16M km^2
    with pytest.raises(BboxInvalidError):
        fetch_landcover(continent, dataset="nlcd_2021")


# ---------------------------------------------------------------------------
# job-0044 hotfix — fetch_landcover (WCS 1.0.0 path, palette-encoding fix).
# ---------------------------------------------------------------------------


def test_fetch_landcover_uses_wcs_not_wms_after_hotfix():
    """job-0044: the fetcher MUST issue WCS 1.0.0 GetCoverage, not WMS GetMap.

    Path A (palette decode) vs Path B (WCS GetCoverage) was live-probed; Path B
    won because canonical NLCD class integers come straight from the server,
    avoiding the OQ-42-NLCD-WMS-PALETTE-ENCODING silent-wrong-answer condition
    that bounced job-0042's validation gate. This test pins the choice — if
    someone reverts to WMS the band values become palette indices again and
    SFINCS dispatch silently breaks.
    """
    # Inspect the WCS coverage table — the symbol is the substrate hook the
    # hotfix introduced; reverting it would remove the alias.
    assert hasattr(data_fetch, "_MRLC_WCS_URL")
    assert hasattr(data_fetch, "_NLCD_WCS_COVERAGE_BY_YEAR")
    assert data_fetch._MRLC_WCS_URL.endswith("/wcs")
    # 2021 (the default) and 2019 (the second-most-recent discrete vintage)
    # are both in the WCS catalog.
    assert 2021 in data_fetch._NLCD_WCS_COVERAGE_BY_YEAR
    assert 2019 in data_fetch._NLCD_WCS_COVERAGE_BY_YEAR
    # The coverage IDs use the qualified ``mrlc_display:`` workspace prefix
    # WCS expects (per the 2026-06-07 live probe).
    assert data_fetch._NLCD_WCS_COVERAGE_BY_YEAR[2021].startswith(
        "mrlc_display:NLCD_2021_Land_Cover_L48"
    )


def test_fetch_landcover_cache_key_source_is_mrlc_wcs(monkeypatch):
    """job-0044 cache-migration policy: cache-key params carry source=mrlc-wcs.

    The job-0039 substrate landed cache entries under source=mrlc-wms (WMS
    GetMap); after the hotfix the cache-key tag flips to mrlc-wcs so the
    palette-encoded entries naturally evict on TTL (30 days from their write
    time) rather than colliding with the new canonical-bytes entries.
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_nlcd_landcover_bytes",
        lambda bbox, year, resolution_m=30: b"FAKE_NLCD_GEOTIFF_BYTES_WCS",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    result = fetch_landcover(FORT_MYERS_BBOX, dataset="nlcd_2021")
    # Source tag in the returned dict is mrlc-wcs.
    assert result["source"] == "mrlc-wcs"
    # Same cache prefix (cache/static-30d/landcover/) but the key hash differs
    # from the WMS-source hash because the source string is part of the
    # canonicalized params dict the cache key is derived from.
    paths = list(fake_storage.store.keys())
    assert len(paths) == 1
    assert paths[0].startswith("cache/static-30d/landcover/")
    assert paths[0].endswith(".tif")


# ---------------------------------------------------------------------------
# job-0324 follow-up — STALE-CACHE fix: landcover cache key MUST change so a
# post-fix fetch MISSES the pre-fix (palette-less) COG and regenerates a
# colored, palette-preserving one. The bake-NLCD-into-hillshade demo rendered
# grey because the static-30d cache served a palette-LESS COG written before
# deploy #3's palette-preservation fix; bumping a landcover-only cache-version
# salt evicts those entries on the next fetch.
# ---------------------------------------------------------------------------


def test_landcover_cache_version_salt_present_and_folded_into_params():
    """The landcover-only cache-version salt exists and is part of the params.

    The salt is what makes the post-fix key differ from the stale pre-fix key.
    It must live ONLY in the landcover params dict (not in the shared
    ``compute_cache_key`` salt) so no other tool's cache key changes.
    """
    assert hasattr(data_fetch, "_LANDCOVER_CACHE_VERSION")
    # v2 = post-job-0324 palette-preserving COGs (v1 was the stale palette-less
    # generation). Any bump > 1 forces a clean regenerate.
    assert data_fetch._LANDCOVER_CACHE_VERSION >= 2


def test_landcover_cache_key_changed_after_palette_fix():
    """A fetch with the SAME bbox now computes a DIFFERENT cache key than the
    pre-fix entry — i.e. it would MISS the stale palette-less COG.

    Reconstructs the OLD params dict (no cache_version salt) and the NEW params
    dict (with the salt) exactly as ``fetch_landcover`` builds them, hashes both
    via ``compute_cache_key`` (the same function the cache shim uses), and
    asserts the keys differ. This is the load-bearing assertion that post-fix
    fetches no longer hit the grey, palette-less cached COG.
    """
    from grace2_agent.tools.cache import compute_cache_key

    quantized = data_fetch._round_bbox_to_30m_nlcd(FORT_MYERS_BBOX)

    # OLD (pre-fix) params — what the tool wrote before the salt was added.
    old_params = {
        "bbox": list(quantized),
        "dataset": "nlcd_2021",
        "source": "mrlc-wcs",
    }
    # NEW (post-fix) params — exactly what fetch_landcover now builds.
    new_params = {
        "bbox": list(quantized),
        "dataset": "nlcd_2021",
        "source": "mrlc-wcs",
        "cache_version": data_fetch._LANDCOVER_CACHE_VERSION,
    }

    source_id = data_fetch._FETCH_LANDCOVER_METADATA.source_class
    ttl_class = data_fetch._FETCH_LANDCOVER_METADATA.ttl_class
    old_key = compute_cache_key(source_id, old_params, ttl_class, now=PINNED_NOW)
    new_key = compute_cache_key(source_id, new_params, ttl_class, now=PINNED_NOW)

    assert old_key != new_key, (
        "landcover cache key must change after the palette-fix salt bump so "
        "post-fix fetches miss the stale palette-less COG"
    )


def test_fetch_landcover_writes_cache_at_new_salted_key(monkeypatch):
    """End-to-end: ``fetch_landcover`` writes the COG at the NEW salted key.

    Drives the real tool through the (mocked) cache shim and confirms the cache
    object it lands at matches the salted-params key — NOT the old un-salted key
    that the stale palette-less COG occupies.
    """
    from grace2_agent.tools.cache import (
        cache_path,
        compute_cache_key,
    )
    from grace2_agent.tools import cache as cache_mod

    fake_storage = FakeStorageClient()
    monkeypatch.setattr(
        data_fetch,
        "_fetch_nlcd_landcover_bytes",
        lambda bbox, year, resolution_m=30: b"FAKE_NLCD_GEOTIFF_BYTES_PALETTE_PRESERVED",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    fetch_landcover(FORT_MYERS_BBOX, dataset="nlcd_2021")

    quantized = data_fetch._round_bbox_to_30m_nlcd(FORT_MYERS_BBOX)
    new_params = {
        "bbox": list(quantized),
        "dataset": "nlcd_2021",
        "source": "mrlc-wcs",
        # Auto-coarsen feature: params now carry the effective resolution
        # (30 m native for a small bbox like Fort Myers).
        "resolution_m": 30,
        "cache_version": data_fetch._LANDCOVER_CACHE_VERSION,
    }
    expected_key = compute_cache_key(
        "landcover", new_params, "static-30d", now=PINNED_NOW
    )
    expected_path = cache_path("landcover", "static-30d", expected_key, "tif")

    assert expected_path in fake_storage.store, (
        "COG must land at the NEW salted key, not the stale un-salted key"
    )
    # And NOT at the old un-salted key (which holds the grey palette-less COG).
    old_params = {
        "bbox": list(quantized),
        "dataset": "nlcd_2021",
        "source": "mrlc-wcs",
    }
    old_key = compute_cache_key(
        "landcover", old_params, "static-30d", now=PINNED_NOW
    )
    old_path = cache_path("landcover", "static-30d", old_key, "tif")
    assert old_path not in fake_storage.store


def test_fetch_nlcd_landcover_bytes_issues_wcs_1_0_0_getcoverage(monkeypatch):
    """The internal fetcher issues a WCS 1.0.0 GetCoverage request, not WMS GetMap.

    Asserts the actual request shape so a future refactor can't silently
    flip back to WMS without this test catching it. Captures the kwargs the
    fetcher passes into ``requests.get``.
    """
    captured: dict = {}

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "image/tiff"}
        content = b"\x49\x49\x2a\x00" + b"\x00" * 256  # TIFF magic prefix
        text = ""

        def raise_for_status(self):
            return None

    def _capture_get(url, params=None, headers=None, timeout=None, **_kw):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(data_fetch.requests, "get", _capture_get)
    out = data_fetch._fetch_nlcd_landcover_bytes(FORT_MYERS_BBOX, 2021)
    assert isinstance(out, bytes) and len(out) > 4
    # URL is the WCS endpoint, not WMS.
    assert captured["url"].endswith("/wcs"), captured["url"]
    # Params shape is WCS 1.0.0 GetCoverage with Coverage + CRS + BBOX +
    # WIDTH + HEIGHT + FORMAT.
    p = captured["params"]
    assert p["service"] == "WCS"
    assert p["version"] == "1.0.0"
    assert p["request"] == "GetCoverage"
    assert p["Coverage"].startswith("mrlc_display:NLCD_2021_Land_Cover_L48")
    assert p["CRS"] == "EPSG:4326"
    assert "BBOX" in p
    assert "WIDTH" in p and "HEIGHT" in p
    assert p["FORMAT"] == "GeoTIFF"
    # MUST NOT be the WMS shape.
    assert p.get("layers") is None  # WMS would use ``layers``
    assert p.get("format") is None  # WMS GetMap shape


def test_fetch_nlcd_landcover_bytes_surfaces_geoserver_exception(monkeypatch):
    """If the WCS server returns an OGC ExceptionReport XML, surface UpstreamAPIError.

    The WCS endpoint returns 200 + ``application/xml`` with an
    ``ows:ExceptionReport`` body when (e.g.) the projection mapping bug fires
    or the requested area is sub-pixel. We MUST NOT cache that body as if it
    were a GeoTIFF — the no-sentinel-on-failure cache contract demands a
    typed raise instead.
    """

    class _FakeXMLResp:
        status_code = 200
        headers = {"content-type": "application/xml"}
        content = b"<?xml version=\"1.0\"?><ows:ExceptionReport/>"
        text = "<?xml version=\"1.0\"?><ows:ExceptionReport/>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        data_fetch.requests,
        "get",
        lambda *a, **kw: _FakeXMLResp(),
    )
    with pytest.raises(UpstreamAPIError):
        data_fetch._fetch_nlcd_landcover_bytes(FORT_MYERS_BBOX, 2021)


# ---------------------------------------------------------------------------
# F33/F39 fix — fetch_landcover COG with overviews + exact-bbox clip.
# ---------------------------------------------------------------------------


def _make_flat_nlcd_geotiff_bytes(bbox, width=900, height=900, pad=False):
    """Build a flat (no-overview) single-band uint8 GeoTIFF for ``bbox``.

    Mimics the MRLC WCS GetCoverage output: strip-organized, NO overviews. If
    ``pad`` is True the raster covers a bbox slightly LARGER than ``bbox`` so
    the clip step has a fringe to trim (proves the exact-bbox clip works).
    """
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    src_bbox = bbox
    if pad:
        min_lon, min_lat, max_lon, max_lat = bbox
        dx = (max_lon - min_lon) * 0.1
        dy = (max_lat - min_lat) * 0.1
        src_bbox = (min_lon - dx, min_lat - dy, max_lon + dx, max_lat + dy)

    data = (np.random.randint(11, 95, size=(height, width))).astype("uint8")
    transform = from_bounds(*src_bbox, width, height)
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        path = f.name
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        nodata=255,
    ) as dst:
        dst.write(data, 1)
    with open(path, "rb") as f:
        out = f.read()
    os.unlink(path)
    return out


def test_landcover_bytes_to_cog_adds_overviews_and_clips_bbox():
    """The new COG helper emits overviews AND clips to the EXACT requested bbox."""
    import rasterio

    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    # Flat raster that OVERHANGS the bbox so the clip has something to trim.
    flat = _make_flat_nlcd_geotiff_bytes(quantized, pad=True)

    # Sanity: the flat input has NO overviews.
    assert not data_fetch._has_overviews(flat)

    cog = data_fetch._landcover_bytes_to_cog(flat, quantized)
    assert isinstance(cog, bytes) and len(cog) > 0

    # (1) Overviews present (the TiTiler zoomed-out-tile fix).
    assert data_fetch._has_overviews(cog), "COG output must carry internal overviews"

    # (2) Output extent clipped to the requested bbox (~1 px tolerance at 30 m).
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        f.write(cog)
        cog_path = f.name
    try:
        with rasterio.open(cog_path) as src:
            b = src.bounds
            # 30 m ~ 0.0003 deg; allow 2 px slack for pixel snapping.
            tol = 0.0006
            assert abs(b.left - quantized[0]) < tol, (b.left, quantized[0])
            assert abs(b.bottom - quantized[1]) < tol, (b.bottom, quantized[1])
            assert abs(b.right - quantized[2]) < tol, (b.right, quantized[2])
            assert abs(b.top - quantized[3]) < tol, (b.top, quantized[3])
            # Tiled (COG driver default 512x512), not strip-organized.
            assert src.profile.get("blockxsize") is not None
    finally:
        os.unlink(cog_path)


def test_fetch_nlcd_landcover_bytes_output_has_overviews(monkeypatch):
    """End-to-end internal fetcher: NLCD bytes come back as a COG with overviews.

    Mocks the OGC adapter so the WCS GeoTIFF is a flat (no-overview) raster,
    then asserts the fetcher's returned bytes (what gets cached) carry
    overviews — the F33/F39 spotty-render root-cause fix.
    """
    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    flat = _make_flat_nlcd_geotiff_bytes(quantized)
    assert not data_fetch._has_overviews(flat)

    class _FakeOGCResp:
        content = flat
        content_type = "image/tiff"

    import grace2_agent.tools.ogc_adapter as ogc_mod

    monkeypatch.setattr(
        ogc_mod, "fetch_ogc_layer", lambda *a, **kw: _FakeOGCResp()
    )

    out = data_fetch._fetch_nlcd_landcover_bytes(FORT_MYERS_BBOX, 2021)
    assert isinstance(out, bytes) and len(out) > 0
    assert data_fetch._has_overviews(out), (
        "cached NLCD bytes must be a COG with overviews (TiTiler zoom fix)"
    )


# ---------------------------------------------------------------------------
# job-0324 — colormap preservation across the COG re-write paths.
#
# REGRESSION: NLCD land cover is a single-band palette-index COG with an
# EMBEDDED GDAL color table; TiTiler colorizes from it. job-0316's
# overviews/clip re-writes dropped the table → land cover renders solid GREY.
# Every re-write path (_clip_raster_bytes_to_bbox, _rasterio_translate_to_cog,
# and the full _landcover_bytes_to_cog pipeline) must carry the table forward.
# Continuous rasters (no color table) must pass through UNCHANGED — never
# fabricate a colormap.
# ---------------------------------------------------------------------------


# A representative NLCD-style palette: a handful of class indices → RGBA.
_NLCD_COLORMAP = {
    0: (0, 0, 0, 0),
    11: (72, 109, 162, 255),  # open water
    21: (222, 197, 197, 255),  # developed, open space
    41: (56, 129, 78, 255),  # deciduous forest
    81: (220, 217, 57, 255),  # pasture/hay
    90: (186, 217, 235, 255),  # woody wetlands
    255: (0, 0, 0, 0),  # nodata
}


# job-2026-07-09 -- NLCD state-scale opaque-black-ocean fix.
#
# The real MRLC WCS 1.0.0 GetCoverage embeds class 0 ("Background", used for
# pixels outside the classified CONUS extent -- ocean, international waters)
# as OPAQUE BLACK -- live-verified against the real endpoint 2026-07-09 (a
# bbox off the Washington coast). ``_NLCD_COLORMAP`` above hand-assumed 0 was
# already transparent, which is why the earlier colormap-preservation tests
# never caught this: they preserve whatever table they are handed, and the
# fixture handed them was already "clean". This second colormap matches the
# REAL observed encoding so the background-transparency fix has something
# real to fix.
_NLCD_COLORMAP_REAL_MRLC = {
    0: (0, 0, 0, 255),  # Background -- REAL MRLC WCS encoding: OPAQUE (the bug)
    11: (70, 107, 159, 255),  # open water
    21: (222, 197, 197, 255),  # developed, open space
    41: (104, 171, 95, 255),  # deciduous forest
    81: (220, 217, 57, 255),  # pasture/hay
    90: (184, 217, 235, 255),  # woody wetlands
    255: (255, 255, 255, 0),  # declared nodata -- correctly transparent
}


def _make_paletted_nlcd_geotiff_bytes_with_background(
    bbox, width=200, height=200, background_frac=0.3, nodata=255
):
    """Build a paletted NLCD-style GeoTIFF carrying REAL class-0 background pixels.

    Mirrors ``_make_paletted_nlcd_geotiff_bytes`` but (a) uses the REAL
    observed MRLC colormap (``_NLCD_COLORMAP_REAL_MRLC``, class 0 = opaque
    black) and (b) actually plants some class-0 "Background" pixels in the
    data (a state-scale AOI reaching into open ocean) -- the scenario the
    existing fixtures never exercised. ``nodata=None`` builds a raster with
    NO declared nodata tag, for the promote-0-to-nodata branch.
    """
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    classes = np.array([11, 21, 41, 81, 90], dtype="uint8")
    data = classes[np.random.randint(0, len(classes), size=(height, width))]
    n_bg = int(height * width * background_frac)
    flat = data.reshape(-1)
    bg_idx = np.random.choice(flat.size, size=n_bg, replace=False)
    flat[bg_idx] = 0
    data = flat.reshape(height, width)
    transform = from_bounds(*bbox, width, height)
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        path = f.name
    kwargs = dict(
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
    )
    if nodata is not None:
        kwargs["nodata"] = nodata
    with rasterio.open(path, "w", **kwargs) as dst:
        dst.write(data, 1)
        dst.write_colormap(1, _NLCD_COLORMAP_REAL_MRLC)
    with open(path, "rb") as f:
        out = f.read()
    os.unlink(path)
    return out


def test_fix_nlcd_background_transparency_folds_zero_into_declared_nodata():
    """The live bug fix: class-0 background pixels must render transparent.

    Root cause: GDAL forces alpha=0 ONLY for the color-table entry matching
    the DECLARED ``nodata`` value; every other entry (including class 0,
    which MRLC's real WCS response leaves opaque black) is forced back to
    alpha=255 on write regardless of what we ask ``write_colormap`` for.
    So the fix must remap 0-valued pixels into the existing (already
    transparent) nodata sentinel -- verified here end to end.
    """
    import rasterio
    import tempfile

    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    raw = _make_paletted_nlcd_geotiff_bytes_with_background(quantized, nodata=255)
    assert _colormap_of_bytes(raw)[0] == (0, 0, 0, 255)  # sanity: bug present

    fixed = data_fetch._fix_nlcd_background_transparency(raw)

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        f.write(fixed)
        path = f.name
    try:
        with rasterio.open(path) as src:
            band1 = src.read(1)
            assert not (band1 == 0).any(), "background pixels must be remapped away"
            assert src.nodata == 255
            cmap = src.colormap(1)
            # The value now covering every former-background pixel (255)
            # renders transparent -- the actual fix outcome.
            assert cmap[255][3] == 0
            # Real classes must be byte-for-byte untouched.
            for idx in (11, 21, 41, 81, 90):
                assert cmap[idx] == _NLCD_COLORMAP_REAL_MRLC[idx]
    finally:
        os.unlink(path)


def test_fix_nlcd_background_transparency_promotes_zero_when_no_nodata_declared():
    """No declared ``nodata`` at all: 0 is promoted to be the declared nodata."""
    import rasterio
    import tempfile

    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    raw = _make_paletted_nlcd_geotiff_bytes_with_background(quantized, nodata=None)

    fixed = data_fetch._fix_nlcd_background_transparency(raw)

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        f.write(fixed)
        path = f.name
    try:
        with rasterio.open(path) as src:
            assert src.nodata == 0
            cmap = src.colormap(1)
            # GDAL now forces index 0 (the declared nodata) transparent.
            assert cmap[0][3] == 0
    finally:
        os.unlink(path)


def test_fix_nlcd_background_transparency_noop_when_no_background_pixels():
    """No class-0 pixels present -- nothing to fix, bytes pass through unchanged."""
    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    raw = _make_paletted_nlcd_geotiff_bytes(quantized)  # no 0-valued pixels

    fixed = data_fetch._fix_nlcd_background_transparency(raw)
    assert fixed == raw


def test_fix_nlcd_background_transparency_noop_without_colormap():
    """A continuous (non-paletted) raster is NEVER given a fabricated colormap."""
    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    flat = _make_flat_nlcd_geotiff_bytes(quantized)
    assert _colormap_of_bytes(flat) is None  # sanity: no table to begin with

    fixed = data_fetch._fix_nlcd_background_transparency(flat)
    assert fixed == flat
    assert _colormap_of_bytes(fixed) is None, "must NOT fabricate a colormap"


def test_fetch_nlcd_landcover_bytes_fixes_background_transparency_end_to_end(
    monkeypatch,
):
    """Wiring check: ``_fetch_nlcd_landcover_bytes`` applies the fix BEFORE the
    COG re-write pipeline, so the cached/published COG never carries the bug.
    """
    import rasterio
    import tempfile

    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    raw = _make_paletted_nlcd_geotiff_bytes_with_background(
        quantized, width=64, height=64, nodata=255
    )

    class _FakeResp:
        status_code = 200
        headers = {"content-type": "image/tiff"}
        content = raw
        text = ""

        def raise_for_status(self):
            return None

    monkeypatch.setattr(
        data_fetch.requests, "get", lambda *a, **kw: _FakeResp()
    )
    out = data_fetch._fetch_nlcd_landcover_bytes(FORT_MYERS_BBOX, 2021)

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        f.write(out)
        path = f.name
    try:
        with rasterio.open(path) as src:
            band1 = src.read(1)
            assert not (band1 == 0).any(), (
                "published COG must not carry background(0)-valued pixels; "
                "the opaque-black-ocean bug would still be live"
            )
    finally:
        os.unlink(path)


def _make_paletted_nlcd_geotiff_bytes(bbox, width=900, height=900, pad=False):
    """Build a flat single-band uint8 GeoTIFF WITH an embedded color table.

    Mirrors the real MRLC WCS NLCD product: strip-organized, NO overviews,
    palette-index band carrying an embedded GDAL color table. ``pad`` overhangs
    the bbox so the clip step has a fringe to trim.
    """
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    src_bbox = bbox
    if pad:
        min_lon, min_lat, max_lon, max_lat = bbox
        dx = (max_lon - min_lon) * 0.1
        dy = (max_lat - min_lat) * 0.1
        src_bbox = (min_lon - dx, min_lat - dy, max_lon + dx, max_lat + dy)

    # Only use class indices that exist in the colormap.
    classes = np.array([11, 21, 41, 81, 90], dtype="uint8")
    data = classes[np.random.randint(0, len(classes), size=(height, width))]
    transform = from_bounds(*src_bbox, width, height)
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        path = f.name
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=transform,
        nodata=255,
    ) as dst:
        dst.write(data, 1)
        dst.write_colormap(1, _NLCD_COLORMAP)
    with open(path, "rb") as f:
        out = f.read()
    os.unlink(path)
    return out


def _colormap_of_bytes(tif_bytes):
    """Return the band-1 colormap of a GeoTIFF (bytes) or ``None`` if absent."""
    import tempfile

    import rasterio

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        f.write(tif_bytes)
        path = f.name
    try:
        with rasterio.open(path) as src:
            try:
                return src.colormap(1)
            except ValueError:
                return None
    finally:
        os.unlink(path)


def _colorinterp0_of_bytes(tif_bytes):
    """Return band-1 ColorInterp name of a GeoTIFF (bytes)."""
    import tempfile

    import rasterio

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        f.write(tif_bytes)
        path = f.name
    try:
        with rasterio.open(path) as src:
            return src.colorinterp[0].name
    finally:
        os.unlink(path)


def _assert_colormap_round_trip_equal(src_bytes, out_bytes):
    """Output band-1 color table must equal the SOURCE's round-tripped table.

    Comparing against the source's own ``colormap(1)`` (not the pre-write dict)
    is the apples-to-apples check: GDAL's GTiff palette writer normalizes the
    alpha component on write (opaque entries come back with a=255), so the
    contract is "the table survives the re-write intact", not "matches my
    hand-written RGBA". A per-index mismatch here means the re-write CHANGED the
    table (the grey-land-cover regression).
    """
    src_cmap = _colormap_of_bytes(src_bytes)
    assert src_cmap is not None, "test fixture lost its colormap"
    out_cmap = _colormap_of_bytes(out_bytes)
    assert out_cmap is not None, "re-write dropped the colormap (job-0324 regression)"
    for idx in _NLCD_COLORMAP:
        assert out_cmap.get(idx) == src_cmap.get(idx), (
            idx,
            out_cmap.get(idx),
            src_cmap.get(idx),
        )


def test_clip_raster_bytes_preserves_colormap():
    """``_clip_raster_bytes_to_bbox`` carries the embedded color table forward."""
    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    paletted = _make_paletted_nlcd_geotiff_bytes(quantized, pad=True)
    assert _colormap_of_bytes(paletted) is not None  # sanity: source has one

    clipped = data_fetch._clip_raster_bytes_to_bbox(paletted, quantized)
    _assert_colormap_round_trip_equal(paletted, clipped)
    # Band marked palette so TiTiler treats pixels as indices.
    assert _colorinterp0_of_bytes(clipped) == "palette"


def test_rasterio_translate_to_cog_preserves_colormap_and_overviews():
    """``_rasterio_translate_to_cog`` keeps the colormap AND builds overviews."""
    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    paletted = _make_paletted_nlcd_geotiff_bytes(quantized)
    assert not data_fetch._has_overviews(paletted)

    cog = data_fetch._rasterio_translate_to_cog(paletted)
    assert isinstance(cog, bytes) and len(cog) > 0
    # Colormap preserved (vs the source's round-tripped table).
    _assert_colormap_round_trip_equal(paletted, cog)
    # Overviews still present (the F33 fix must not regress either).
    assert data_fetch._has_overviews(cog), "COG translate must keep overviews"


def test_landcover_bytes_to_cog_preserves_colormap_overviews_and_clip():
    """Full NLCD pipeline: colormap + overviews + exact-bbox clip TOGETHER."""
    import rasterio
    import tempfile

    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    paletted = _make_paletted_nlcd_geotiff_bytes(quantized, pad=True)

    cog = data_fetch._landcover_bytes_to_cog(paletted, quantized)
    assert isinstance(cog, bytes) and len(cog) > 0

    # (1) Colormap preserved end-to-end.
    _assert_colormap_round_trip_equal(paletted, cog)

    # (2) Overviews present.
    assert data_fetch._has_overviews(cog)

    # (3) Clipped to the requested bbox (~2 px slack at 30 m).
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        f.write(cog)
        cog_path = f.name
    try:
        with rasterio.open(cog_path) as src:
            b = src.bounds
            tol = 0.0006
            assert abs(b.left - quantized[0]) < tol
            assert abs(b.bottom - quantized[1]) < tol
            assert abs(b.right - quantized[2]) < tol
            assert abs(b.top - quantized[3]) < tol
    finally:
        os.unlink(cog_path)


def test_clip_raster_bytes_no_colormap_passes_through_unchanged():
    """A continuous raster (NO color table) is NOT given a fabricated colormap."""
    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    flat = _make_flat_nlcd_geotiff_bytes(quantized, pad=True)
    assert _colormap_of_bytes(flat) is None  # sanity: no table to begin with

    clipped = data_fetch._clip_raster_bytes_to_bbox(flat, quantized)
    assert _colormap_of_bytes(clipped) is None, "must NOT fabricate a colormap"
    # colorinterp must remain gray (not flipped to palette).
    assert _colorinterp0_of_bytes(clipped) != "palette"


def test_rasterio_translate_to_cog_no_colormap_passes_through_unchanged():
    """COG translate of a non-paletted raster: overviews built, NO colormap added."""
    quantized = data_fetch.round_bbox_to_resolution(FORT_MYERS_BBOX, 30)
    flat = _make_flat_nlcd_geotiff_bytes(quantized)

    cog = data_fetch._rasterio_translate_to_cog(flat)
    assert isinstance(cog, bytes) and len(cog) > 0
    assert _colormap_of_bytes(cog) is None, "must NOT fabricate a colormap on DEM-like"
    assert data_fetch._has_overviews(cog), "overviews still build for non-paletted"


# ---------------------------------------------------------------------------
# job-0039 — fetch_river_geometry (NHDPlus HR HUC4 region download).
# ---------------------------------------------------------------------------


def test_fetch_river_geometry_is_registered_with_static_30d():
    entry = TOOL_REGISTRY["fetch_river_geometry"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "river_geometry"
    assert entry.metadata.cacheable is True


def test_fetch_river_geometry_docstring_records_tier_4():
    """§F.1.1 docstring discipline: Tier 4 (region download + local clip)."""
    doc = fetch_river_geometry.__doc__ or ""
    assert "Access pattern:" in doc
    assert "Tier 4" in doc


def test_fetch_river_geometry_happy_path_returns_layer_uri(monkeypatch):
    """OSM-primary fetcher (mocked) + mocked GCS → vector LayerURI on the .fgb path.

    Job: OSM Overpass is the PRIMARY river-geometry source. The happy path
    mocks the primary fetcher and asserts the LayerURI shape (vector / .fgb /
    river_geometry cache prefix). layer_id is now provider-agnostic
    (``rivers-<lon>-<lat>``), no longer HUC4-coded, because the source is
    decided by the internal fallback chain at fetch time.
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_osm_waterway_geometry_bytes",
        lambda bbox, *a, **kw: b"FAKE_FLATGEOBUF_BYTES",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    layer = fetch_river_geometry(FORT_MYERS_BBOX)
    assert layer.layer_type == "vector"
    assert layer.uri.startswith(
        "s3://grace2-hazard-cache-226996537797/cache/static-30d/river_geometry/"
    )
    assert layer.uri.endswith(".fgb")
    # Provider-agnostic layer_id; renders inline (NOT published via publish_layer).
    assert layer.layer_id.startswith("rivers-")
    assert layer.name == "Rivers & Streams"
    # job-3: the river vector carries a WATER preset (osm_waterways), mirroring
    # fetch_roads_osm's osm_roads — NOT the continuous_dem raster ramp that was
    # wrongly applied to this line vector (which made the web client hash a
    # random per-layer-id colour: yellow on one AOI, blue on another).
    assert layer.style_preset == "osm_waterways"


def test_fetch_river_geometry_cache_key_distinct_per_bbox(monkeypatch):
    """Two disjoint regions must NOT collide on the cache key.

    The cache key is keyed on the quantized bbox (+ best-effort HUC4), so two
    small boxes in different regions (Fort Myers vs LA basin) produce
    different cache paths even though both flow through the OSM-primary chain.
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_osm_waterway_geometry_bytes",
        lambda bbox, *a, **kw: b"FAKE_FLATGEOBUF_BYTES",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    fl_layer = fetch_river_geometry(FORT_MYERS_BBOX)
    # CA south coast — small bbox in the LA basin.
    ca_bbox = (-118.4, 33.8, -118.2, 34.0)
    ca_layer = fetch_river_geometry(ca_bbox)
    assert fl_layer.uri != ca_layer.uri, "different bboxes must hit different cache keys"


def test_fetch_river_geometry_rejects_unknown_source():
    with pytest.raises(BboxInvalidError):
        fetch_river_geometry(FORT_MYERS_BBOX, source="merit_hydro")


def test_fetch_river_geometry_works_outside_huc4_envelope_via_osm(monkeypatch):
    """A bbox outside every v0.1 HUC4 envelope still succeeds via OSM-primary.

    Root-cause fix: previously a bbox center outside the hardcoded HUC4
    envelopes dead-ended with "could not route bbox to a HUC4 region". Now OSM
    Overpass is the primary path, so an out-of-HUC4 bbox returns a valid vector
    LayerURI (the OSM fetcher is mocked here so no network is touched).
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_osm_waterway_geometry_bytes",
        lambda bbox, *a, **kw: b"FAKE_FLATGEOBUF_BYTES",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    # Kansas — a CONUS bbox not in any v0.1 HUC4 envelope (the old failure case).
    kansas_bbox = (-97.4, 37.6, -97.2, 37.8)
    assert data_fetch._huc4_for_bbox(kansas_bbox) is None
    layer = fetch_river_geometry(kansas_bbox)
    assert layer.layer_type == "vector"
    assert layer.uri.endswith(".fgb")


def test_fetch_river_geometry_rejects_oversized_bbox():
    """The 5000 km^2 guardrail blocks multi-HUC4 stitching attempts.

    Bbox center sits inside HUC4 0309 (South Florida envelope: lon
    [-82.0, -80.0], lat [25.0, 27.5]) so the HUC4 routing accepts it; the
    bbox itself is sized to exceed the 5000 km^2 area guardrail (~25,000
    km^2 here) so the area guardrail fires.
    """
    # Bbox center (-81.0, 26.25) inside HUC4 0309 envelope; ~25k km^2 area.
    oversized_inside_huc4 = (-81.9, 25.5, -80.1, 27.0)
    with pytest.raises(BboxInvalidError):
        fetch_river_geometry(oversized_inside_huc4)


# ---------------------------------------------------------------------------
# F30 fix — fetch_river_geometry OSM Overpass PRIMARY path + fallback ordering.
# ---------------------------------------------------------------------------


# A small bbox over a couple of synthetic "rivers". KANSAS_BBOX is NOT in any
# v0.1 HUC4 envelope — the exact case that used to dead-end.
KANSAS_BBOX = (-97.4, 37.6, -97.2, 37.8)


def _fake_overpass_waterway_payload(bbox):
    """Build a fake Overpass JSON response with waterways spanning the bbox.

    Two ways: one river crossing the bbox left-to-right (spans the full
    width), one stream that extends OUTSIDE the bbox on the right edge so the
    clip step has something to trim.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    return {
        "elements": [
            {
                "type": "way",
                "id": 1001,
                "tags": {"waterway": "river", "name": "Big River"},
                # spans the full bbox width along mid-latitude
                "geometry": [
                    {"lat": mid_lat, "lon": min_lon},
                    {"lat": mid_lat, "lon": 0.5 * (min_lon + max_lon)},
                    {"lat": mid_lat, "lon": max_lon},
                ],
            },
            {
                "type": "way",
                "id": 1002,
                "tags": {"waterway": "stream", "name": "Edge Creek"},
                # starts inside, extends well outside the right edge
                "geometry": [
                    {"lat": min_lat + 0.01, "lon": max_lon - 0.01},
                    {"lat": min_lat + 0.01, "lon": max_lon + 0.5},
                ],
            },
        ]
    }


def test_fetch_river_geometry_osm_returns_bbox_filling_geometry(monkeypatch):
    """PRIMARY OSM path: waterways fill the whole bbox and are clipped to it.

    Mocks the Overpass POST, runs the real FGB-serialization + clip path, then
    decodes the FlatGeobuf and asserts (a) features are present, (b) the
    union's x-extent spans most of the bbox width (fills the bbox, unlike the
    NLDI seed-trace), and (c) NO geometry spills outside the requested bbox
    (the clip trimmed the stream that ran off the right edge).
    """
    import geopandas as gpd
    from shapely.geometry import box as shapely_box

    captured = {}

    def _fake_post(url, data=None, headers=None, timeout=None, **_kw):
        captured["url"] = url
        captured["ql"] = (data or {}).get("data")

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self_inner):
                return _fake_overpass_waterway_payload(KANSAS_BBOX)

        return _Resp()

    monkeypatch.setattr(data_fetch.requests, "post", _fake_post)

    quantized = data_fetch.round_bbox_to_resolution(KANSAS_BBOX, 10)
    fgb_bytes = data_fetch._fetch_osm_waterway_geometry_bytes(quantized)
    assert isinstance(fgb_bytes, bytes) and len(fgb_bytes) > 0

    # The Overpass QL targets waterways, not highways, and uses (s,w,n,e).
    assert "waterway" in captured["ql"]
    assert "river|stream|canal" in captured["ql"]
    assert captured["url"].endswith("/api/interpreter")

    # Decode the FlatGeobuf and verify geometry fills + is clipped to the bbox.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        fgb_path = f.name
    try:
        gdf = gpd.read_file(fgb_path)
    finally:
        os.unlink(fgb_path)

    assert len(gdf) >= 1, "OSM path must return at least one waterway feature"
    minx, miny, maxx, maxy = gdf.total_bounds
    bbox_w = quantized[2] - quantized[0]
    # Fills the bbox: union x-extent spans most of the width (the NLDI seed
    # trace would only cover a connected sub-network, not the full bbox).
    assert (maxx - minx) >= 0.5 * bbox_w
    # Clipped: nothing spills outside the requested bbox (small float epsilon).
    eps = 1e-6
    assert minx >= quantized[0] - eps
    assert maxx <= quantized[2] + eps
    assert miny >= quantized[1] - eps
    assert maxy <= quantized[3] + eps


def test_fetch_river_geometry_falls_back_to_nhdplus_when_osm_fails(monkeypatch):
    """Fallback ordering: OSM primary fails → NHDPlus HR (when HUC4 resolves) is used."""
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    calls = []

    def _osm_boom(bbox, *a, **kw):
        calls.append("osm")
        raise UpstreamAPIError("simulated Overpass outage")

    def _nhd_ok(bbox, huc4):
        calls.append(("nhd", huc4))
        return b"FAKE_NHDPLUS_FLATGEOBUF"

    monkeypatch.setattr(data_fetch, "_fetch_osm_waterway_geometry_bytes", _osm_boom)
    monkeypatch.setattr(data_fetch, "_fetch_nhdplushr_geometry_bytes", _nhd_ok)
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    # Fort Myers routes to HUC4 0309, so the NHDPlus fallback is available.
    layer = fetch_river_geometry(FORT_MYERS_BBOX)
    assert layer.layer_type == "vector"
    assert layer.uri.endswith(".fgb")
    # OSM was tried FIRST, then NHDPlus HR with the resolved HUC4.
    assert calls[0] == "osm"
    assert calls[1] == ("nhd", "0309")


def test_fetch_river_geometry_typed_error_when_all_sources_fail(monkeypatch):
    """Both OSM (primary) and NHDPlus HR (fallback) fail → typed UpstreamAPIError.

    Data-source-fallback norm: never a silent dead-end or hallucinated success.
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    def _osm_boom(bbox, *a, **kw):
        raise UpstreamAPIError("simulated Overpass outage")

    def _nhd_boom(bbox, huc4):
        raise UpstreamAPIError("simulated NHDPlus 404")

    monkeypatch.setattr(data_fetch, "_fetch_osm_waterway_geometry_bytes", _osm_boom)
    monkeypatch.setattr(data_fetch, "_fetch_nhdplushr_geometry_bytes", _nhd_boom)
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    with pytest.raises(UpstreamAPIError):
        fetch_river_geometry(FORT_MYERS_BBOX)


def test_fetch_river_geometry_osm_only_when_no_huc4_and_osm_fails(monkeypatch):
    """OSM fails AND no HUC4 fallback available → typed UpstreamAPIError (no dead-end)."""
    def _osm_boom(bbox, *a, **kw):
        raise UpstreamAPIError("simulated Overpass outage")

    monkeypatch.setattr(data_fetch, "_fetch_osm_waterway_geometry_bytes", _osm_boom)
    # Kansas is outside every HUC4 envelope, so there is no NHDPlus fallback.
    assert data_fetch._huc4_for_bbox(KANSAS_BBOX) is None
    with pytest.raises(UpstreamAPIError):
        data_fetch._fetch_river_geometry_bytes(
            data_fetch.round_bbox_to_resolution(KANSAS_BBOX, 10), None
        )


# ---------------------------------------------------------------------------
# waterway_type upgrade — selectable OSM waterway classes (ditch/drain widen).
# Drained-agriculture landscapes (Imperial Valley, the Fens) are dominated by
# artificial ditch/drain channels that the default river/stream/canal set
# excludes; waterway_type opts them in. PROTOTYPED live over the Imperial
# Valley + Lincolnshire Fens bboxes (default returned 1 river; +ditch+drain
# surfaced 5 drains + 2 ditches) — these tests mock Overpass to stay hermetic.
# ---------------------------------------------------------------------------


# Imperial Valley, CA — heavily drained agriculture (dense canal + ditch/drain
# network). Outside every v0.1 HUC4 envelope, so the OSM-primary path is used.
IMPERIAL_VALLEY_BBOX = (-115.58, 32.78, -115.52, 32.84)


def test_resolve_waterway_classes_default_and_aliases():
    """waterway_type resolver: None -> default; aliases + tokens normalize."""
    # None / empty / whitespace -> the default river/stream/canal tuple.
    assert data_fetch._resolve_waterway_classes(None) == ("river", "stream", "canal")
    assert data_fetch._resolve_waterway_classes("") == ("river", "stream", "canal")
    assert data_fetch._resolve_waterway_classes("   ") == ("river", "stream", "canal")
    # Convenience aliases.
    assert data_fetch._resolve_waterway_classes("all") == (
        "river",
        "stream",
        "canal",
        "ditch",
        "drain",
    )
    assert data_fetch._resolve_waterway_classes("drainage") == ("ditch", "drain")
    assert data_fetch._resolve_waterway_classes("ditches") == ("ditch", "drain")
    # Single value, case/space-insensitive.
    assert data_fetch._resolve_waterway_classes("  Ditch ") == ("ditch",)
    # Comma- and plus-joined strings.
    assert data_fetch._resolve_waterway_classes("ditch,drain") == ("ditch", "drain")
    assert data_fetch._resolve_waterway_classes("river+ditch") == ("river", "ditch")
    # List form, with order-preserving de-duplication.
    assert data_fetch._resolve_waterway_classes(
        ["ditch", "drain", "ditch"]
    ) == ("ditch", "drain")


def test_resolve_waterway_classes_rejects_unknown_tokens():
    """Unknown waterway tokens raise BboxInvalidError (closed vocabulary).

    A closed vocabulary is what keeps an LLM-invented value from injecting
    arbitrary text into the Overpass ``~"^(...)$"`` regex.
    """
    with pytest.raises(BboxInvalidError):
        data_fetch._resolve_waterway_classes("sewer")
    with pytest.raises(BboxInvalidError):
        data_fetch._resolve_waterway_classes("river,sewer")
    with pytest.raises(BboxInvalidError):
        data_fetch._resolve_waterway_classes(["ditch", 5])  # type: ignore[list-item]
    with pytest.raises(BboxInvalidError):
        data_fetch._resolve_waterway_classes(42)  # type: ignore[arg-type]


def test_build_overpass_waterway_ql_threads_selected_classes():
    """The resolved classes flow into the Overpass QL regex alternation."""
    bbox = IMPERIAL_VALLEY_BBOX
    ql_default = data_fetch._build_overpass_waterway_ql(
        bbox, data_fetch._WATERWAY_CLASSES
    )
    assert 'waterway"~"^(river|stream|canal)$"' in ql_default
    assert "ditch" not in ql_default

    ql_drainage = data_fetch._build_overpass_waterway_ql(
        bbox, data_fetch._resolve_waterway_classes("drainage")
    )
    assert 'waterway"~"^(ditch|drain)$"' in ql_drainage

    ql_all = data_fetch._build_overpass_waterway_ql(
        bbox, data_fetch._resolve_waterway_classes("all")
    )
    assert "ditch|drain" in ql_all and "river|stream|canal" in ql_all


def test_fetch_osm_waterway_threads_ditch_classes_into_query(monkeypatch):
    """End-to-end OSM path with waterway_type='drainage' surfaces ditch/drain.

    Mocks Overpass with a synthetic drained-ag response (a canal that the
    default query would catch PLUS a ditch + drain only the widened query
    asks for). Asserts (a) the POSTed QL carries the ditch|drain regex, and
    (b) the decoded FlatGeobuf contains the ditch/drain features, proving the
    selected classes flow all the way through to the serialized layer.
    """
    import geopandas as gpd

    captured = {}

    def _fake_post(url, data=None, headers=None, timeout=None, **_kw):
        captured["ql"] = (data or {}).get("data")

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self_inner):
                min_lon, min_lat, max_lon, max_lat = IMPERIAL_VALLEY_BBOX
                mid_lat = 0.5 * (min_lat + max_lat)
                return {
                    "elements": [
                        {
                            "type": "way",
                            "id": 2001,
                            "tags": {"waterway": "ditch", "name": "Field Ditch"},
                            "geometry": [
                                {"lat": mid_lat, "lon": min_lon + 0.005},
                                {"lat": mid_lat, "lon": max_lon - 0.005},
                            ],
                        },
                        {
                            "type": "way",
                            "id": 2002,
                            "tags": {"waterway": "drain", "name": "Tile Drain"},
                            "geometry": [
                                {"lat": min_lat + 0.01, "lon": min_lon + 0.01},
                                {"lat": max_lat - 0.01, "lon": min_lon + 0.01},
                            ],
                        },
                    ]
                }

        return _Resp()

    monkeypatch.setattr(data_fetch.requests, "post", _fake_post)

    quantized = data_fetch.round_bbox_to_resolution(IMPERIAL_VALLEY_BBOX, 10)
    classes = data_fetch._resolve_waterway_classes("drainage")
    fgb_bytes = data_fetch._fetch_osm_waterway_geometry_bytes(quantized, classes)
    assert isinstance(fgb_bytes, bytes) and len(fgb_bytes) > 0

    # The widened classes reached the Overpass QL regex (NOT the default set).
    assert "ditch|drain" in captured["ql"]
    assert "river|stream|canal" not in captured["ql"]

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        fgb_path = f.name
    try:
        gdf = gpd.read_file(fgb_path)
    finally:
        os.unlink(fgb_path)

    assert len(gdf) >= 2
    waterway_vals = set(gdf["waterway"].tolist())
    assert "ditch" in waterway_vals
    assert "drain" in waterway_vals


def test_fetch_river_geometry_waterway_type_distinct_cache_key(monkeypatch):
    """Distinct waterway_type -> distinct cache key; default stays unchanged.

    The default waterway_type (None) must NOT fold a waterway_classes field
    into the cache key (backward-compatible artifacts), while a non-default
    set MUST produce a distinct key so it can't alias the default artifact.
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    seen_classes = []

    def _fake_osm(bbox, waterway_classes=data_fetch._WATERWAY_CLASSES):
        seen_classes.append(tuple(waterway_classes))
        return b"FAKE_FLATGEOBUF_BYTES"

    monkeypatch.setattr(data_fetch, "_fetch_osm_waterway_geometry_bytes", _fake_osm)
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    default_layer = fetch_river_geometry(IMPERIAL_VALLEY_BBOX)
    drainage_layer = fetch_river_geometry(
        IMPERIAL_VALLEY_BBOX, waterway_type="drainage"
    )
    all_layer = fetch_river_geometry(IMPERIAL_VALLEY_BBOX, waterway_type="all")

    # Same bbox, three class sets -> three distinct cache keys.
    assert default_layer.uri != drainage_layer.uri
    assert default_layer.uri != all_layer.uri
    assert drainage_layer.uri != all_layer.uri

    # The default call passed the river/stream/canal set, while the widened
    # calls passed the ditch-bearing sets.
    assert seen_classes[0] == ("river", "stream", "canal")
    assert seen_classes[1] == ("ditch", "drain")
    assert seen_classes[2] == ("river", "stream", "canal", "ditch", "drain")


def test_fetch_river_geometry_default_cache_key_unchanged_by_upgrade(monkeypatch):
    """Backward compat: waterway_type=None and the OLD no-arg call share a key.

    The upgrade must not change the cache path of existing default callers, so
    a None waterway_type must hash identically to omitting the param entirely.
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_osm_waterway_geometry_bytes",
        lambda bbox, *a, **kw: b"FAKE_FLATGEOBUF_BYTES",
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    no_arg = fetch_river_geometry(FORT_MYERS_BBOX)
    explicit_none = fetch_river_geometry(FORT_MYERS_BBOX, waterway_type=None)
    assert no_arg.uri == explicit_none.uri


def test_fetch_river_geometry_rejects_unknown_waterway_type():
    """Unknown waterway_type at the public boundary -> BboxInvalidError."""
    with pytest.raises(BboxInvalidError):
        fetch_river_geometry(FORT_MYERS_BBOX, waterway_type="sewer")


# ---------------------------------------------------------------------------
# job-0039 — lookup_precip_return_period (NOAA Atlas 14 PFDS).
# ---------------------------------------------------------------------------


def test_lookup_precip_return_period_is_registered_with_static_30d():
    entry = TOOL_REGISTRY["lookup_precip_return_period"]
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "precip_return_period"
    assert entry.metadata.cacheable is True


def test_lookup_precip_return_period_docstring_records_tier_3():
    """§F.1.1 docstring discipline: Tier 3 (direct HTTPS point query)."""
    doc = lookup_precip_return_period.__doc__ or ""
    assert "Access pattern:" in doc
    assert "Tier 3" in doc


# Verbatim Atlas 14 PFDS response for the Fort Myers center captured 2026-06-07.
_ATLAS14_FORT_MYERS_FIXTURE = b"""Point precipitation frequency estimates (inches)
NOAA Atlas 14 Volume 9 Version 2
Data type: Precipitation depth
Time series type: Partial duration
Project area: Southeastern States
Location name (ESRI Maps): None
Station Name: None
Latitude: 26.6 Degree
Longitude: -81.9 Degree
Elevation (USGS): None None


PRECIPITATION FREQUENCY ESTIMATES
by duration for ARI (years):, 1,2,5,10,25,50,100,200,500,1000
5-min:, 0.553,0.620,0.731,0.822,0.950,1.05,1.15,1.25,1.38,1.48
10-min:, 0.810,0.908,1.07,1.20,1.39,1.54,1.68,1.83,2.02,2.17
15-min:, 0.988,1.11,1.30,1.47,1.70,1.87,2.05,2.23,2.47,2.65
30-min:, 1.60,1.79,2.11,2.37,2.74,3.02,3.31,3.60,3.99,4.28
60-min:, 2.14,2.38,2.79,3.13,3.62,4.00,4.38,4.78,5.32,5.74
2-hr:, 2.69,2.98,3.47,3.90,4.49,4.97,5.46,5.97,6.66,7.20
3-hr:, 2.92,3.25,3.81,4.30,4.99,5.54,6.11,6.71,7.53,8.17
6-hr:, 3.23,3.70,4.50,5.18,6.16,6.94,7.75,8.60,9.76,10.7
12-hr:, 3.49,4.18,5.35,6.36,7.79,8.94,10.1,11.3,13.0,14.3
24-hr:, 4.01,4.76,6.09,7.28,9.05,10.5,12.1,13.7,16.1,18.0
2-day:, 4.94,5.57,6.77,7.94,9.80,11.4,13.3,15.3,18.2,20.7
3-day:, 5.43,6.22,7.68,9.02,11.1,12.9,14.8,16.9,19.8,22.3
4-day:, 5.83,6.78,8.43,9.92,12.1,14.0,15.9,18.0,20.9,23.3
7-day:, 7.08,8.10,9.87,11.4,13.7,15.5,17.5,19.5,22.4,24.6
10-day:, 8.28,9.30,11.0,12.6,14.8,16.6,18.5,20.4,23.2,25.4
20-day:, 11.7,12.9,14.8,16.4,18.7,20.4,22.1,23.8,26.1,27.8
30-day:, 14.5,15.9,18.2,20.0,22.4,24.2,25.9,27.5,29.5,30.9
45-day:, 18.0,19.9,22.7,24.9,27.7,29.6,31.4,33.0,34.9,36.2
60-day:, 21.0,23.3,26.6,29.2,32.4,34.6,36.6,38.3,40.3,41.5

Date/time (GMT):  Sun Jun  7 07:54:20 2026
"""


def test_lookup_precip_return_period_happy_path_returns_structured_dict(monkeypatch):
    """100-year 24-hour at Fort Myers center: parsed from the fixture."""
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_atlas14_pfds_bytes",
        lambda lat, lon: _ATLAS14_FORT_MYERS_FIXTURE,
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    result = lookup_precip_return_period(
        location=(26.6, -81.9), return_period_years=100, duration_hours=24.0
    )
    assert result["precip_inches"] == pytest.approx(12.1)
    assert result["units"] == "inches"
    assert result["return_period_years"] == 100
    assert result["duration_hours"] == 24.0
    assert "Volume 9" in result["vintage_volume"]
    assert "Southeastern" in result["project_area"]
    assert result["source"] == "noaa-atlas14-pfds"
    # Quantized location echoed back.
    assert len(result["location"]) == 2


def test_lookup_precip_return_period_quantizes_location_to_atlas14_grid(monkeypatch):
    """Per-source quantization (acceptance criterion 3): 1/120 degree native grid.

    Two callers within the same Atlas 14 grid cell hit the same cache entry.
    """
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    fetch_calls: list[tuple[float, float]] = []

    def _capturing_fetch(lat, lon):
        fetch_calls.append((lat, lon))
        return _ATLAS14_FORT_MYERS_FIXTURE

    monkeypatch.setattr(data_fetch, "_fetch_atlas14_pfds_bytes", _capturing_fetch)
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    # Two locations within the same 1/120-degree grid cell (~278 m apart at
    # 26.6 latitude — 1/120 degree ≈ 309 m).
    r1 = lookup_precip_return_period(
        location=(26.6, -81.9), return_period_years=100, duration_hours=24.0
    )
    r2 = lookup_precip_return_period(
        location=(26.6005, -81.9005), return_period_years=100, duration_hours=24.0
    )
    assert r1["location"] == r2["location"]
    # Only one cache miss (second call hits the cache).
    assert len(fetch_calls) == 1
    assert len(fake_storage.store) == 1


def test_lookup_precip_return_period_rejects_unsupported_return_period():
    with pytest.raises(BboxInvalidError):
        lookup_precip_return_period(
            location=(26.6, -81.9), return_period_years=300, duration_hours=24.0
        )


def test_lookup_precip_return_period_rejects_unsupported_duration():
    with pytest.raises(BboxInvalidError):
        lookup_precip_return_period(
            location=(26.6, -81.9), return_period_years=100, duration_hours=1.5
        )


def test_lookup_precip_return_period_writes_csv_through_cache(monkeypatch):
    """FR-CE-8: the PFDS CSV is cached under cache/static-30d/precip_return_period/."""
    fake_storage = FakeStorageClient()
    from grace2_agent.tools import cache as cache_mod

    monkeypatch.setattr(
        data_fetch,
        "_fetch_atlas14_pfds_bytes",
        lambda lat, lon: _ATLAS14_FORT_MYERS_FIXTURE,
    )
    monkeypatch.setattr(
        data_fetch,
        "read_through",
        lambda *a, **kw: cache_mod.read_through(
            *a, storage_client=fake_storage, now=PINNED_NOW, **kw
        ),
    )

    lookup_precip_return_period(
        location=(26.6, -81.9), return_period_years=100, duration_hours=24.0
    )
    paths = list(fake_storage.store.keys())
    assert len(paths) == 1
    assert paths[0].startswith("cache/static-30d/precip_return_period/")
    assert paths[0].endswith(".csv")
    assert b"NOAA Atlas 14" in fake_storage.store[paths[0]]
    # GCP decommissioned: TTL eviction is an S3 bucket-lifecycle rule (no
    # per-object customTime); assert the boto3 put landed instead.
    assert fake_storage.last_put is not None
