"""Tests for ``import_user_layer`` -- the bidirectional layer push core.

Mirrors ``test_export_qgis_http_route.py`` / ``test_case_list_http_route.py``
in spirit: real geopandas/rasterio round trips (no fakes for the artifact
parsing itself -- that IS the thing under test), but every object-store I/O
call and the Persistence seam are monkeypatched so no network/boto3/MinIO is
touched.

Covered:
  - happy vector (GeoJSON upload) -> FlatGeobuf DATA face + durable GeoJSON
    DISPLAY face + a role="input" ProjectLayerSummary merged onto the case.
  - happy raster (tiny in-memory GeoTIFF) -> publish_layer is reused
    (mocked) for the COG/tile-template step; the summary is merged in.
  - size cap -> ObjectTooLargeError.
  - bad kind -> ImportLayerInputError.
  - missing object -> ObjectNotFoundError.
  - make_aoi=True persists Case.bbox from the layer's computed bounds;
    make_aoi=False (default) leaves it untouched.
  - a second push to the SAME case replaces-by-layer_id rather than
    duplicating an unrelated existing entry (merge policy sanity).
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server import server
from trid3nt_server.tools.meta import import_user_layer as iul
from trid3nt_contracts.case import CaseSummary
from trid3nt_contracts.common import new_ulid


def _geojson_bytes() -> bytes:
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "test"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [-122.5, 45.5],
                            [-122.4, 45.5],
                            [-122.4, 45.6],
                            [-122.5, 45.6],
                            [-122.5, 45.5],
                        ]
                    ],
                },
            }
        ],
    }
    return json.dumps(fc).encode("utf-8")


def _tiny_geotiff_bytes() -> bytes:
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin

    data = np.ones((4, 4), dtype="uint8")
    transform = from_origin(-122.5, 45.6, 0.025, 0.025)
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            height=4,
            width=4,
            count=1,
            dtype="uint8",
            crs="EPSG:4326",
            transform=transform,
        ) as ds:
            ds.write(data, 1)
        return mem.read()


def _tiny_geotiff_bytes_3857() -> bytes:
    """A tiny raster in EPSG:3857 (Web Mercator) -- proves the
    ``rasterio.warp.transform_bounds`` reprojection branch."""
    import numpy as np
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin

    data = np.ones((4, 4), dtype="uint8")
    # ~ -13,000,000 / 5,700,000 m in Web Mercator is roughly -116.8, 45.6 deg.
    transform = from_origin(-13_000_000, 5_700_000, 2500, 2500)
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            height=4,
            width=4,
            count=1,
            dtype="uint8",
            crs="EPSG:3857",
            transform=transform,
        ) as ds:
            ds.write(data, 1)
        return mem.read()


class _FakePersistence:
    def __init__(self, cases: dict[str, CaseSummary]):
        self._cases = cases
        self.upserted: list[CaseSummary] = []

    async def get_case(self, case_id: str):
        return self._cases.get(case_id)

    async def upsert_case(self, case: CaseSummary, **_kw):
        self._cases[case.case_id] = case
        self.upserted.append(case)
        return case

    async def list_cases_for_user(self, user_id: str):
        return list(self._cases.values())


def _case(case_id: str, **overrides) -> CaseSummary:
    base = dict(
        case_id=case_id,
        title="Test case",
        created_at="2026-07-01T00:00:00Z",
        updated_at="2026-07-01T00:00:00Z",
        bbox=None,
        loaded_layer_summaries=[],
        layer_summary=[],
    )
    base.update(overrides)
    return CaseSummary(**base)


@pytest.fixture
def fake_persistence(monkeypatch):
    store: dict[str, CaseSummary] = {}
    fake = _FakePersistence(store)
    monkeypatch.setattr(server, "get_persistence", lambda: fake)
    return fake


def _mock_s3_object(monkeypatch, data: bytes):
    monkeypatch.setattr(iul, "_head_object_size", lambda uri: len(data))
    monkeypatch.setattr(iul, "_get_object_bytes", lambda uri: data)
    puts: list[tuple[str, bytes]] = []
    monkeypatch.setattr(
        iul,
        "_put_object_bytes",
        lambda uri, body, content_type="application/octet-stream": puts.append(
            (uri, body)
        ),
    )
    return puts


# ---------------------------------------------------------------------------
# Happy path: vector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_vector_happy_path(monkeypatch, fake_persistence):
    case_id = new_ulid()
    fake_persistence._cases[case_id] = _case(case_id)
    data = _geojson_bytes()
    _mock_s3_object(monkeypatch, data)

    result = await iul.ingest_user_layer(
        case_id=case_id,
        name="My polygon",
        kind="vector",
        s3_uri="s3://cache/user-uploads/x/poly.geojson",
    )

    assert result["status"] == "ok"
    assert result["layer_type"] == "vector"
    assert result["name"] == "My polygon"
    assert result["feature_count"] == 1
    assert result["bbox"] == pytest.approx([-122.5, 45.5, -122.4, 45.6])
    assert result["aoi_pinned"] is False

    updated = fake_persistence._cases[case_id]
    assert len(updated.loaded_layer_summaries) == 1
    summary = updated.loaded_layer_summaries[0]
    assert summary["layer_id"] == result["layer_id"]
    assert summary["layer_type"] == "vector"
    assert summary["role"] == "input"
    assert summary["uri"].startswith("s3://") and summary["uri"].endswith(".fgb")
    assert updated.bbox is None  # make_aoi defaulted False


# ---------------------------------------------------------------------------
# Happy path: raster (publish_layer mocked -- COG/TiTiler is its own tested
# seam; this test only proves the ingest wiring around it)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_raster_happy_path(monkeypatch, fake_persistence):
    from trid3nt_server.tools import publish_layer as publish_layer_mod

    case_id = new_ulid()
    fake_persistence._cases[case_id] = _case(case_id)
    data = _tiny_geotiff_bytes()
    _mock_s3_object(monkeypatch, data)

    calls = []

    def _fake_publish_layer(*, layer_uri, layer_id, name=None, **_kw):
        calls.append({"layer_uri": layer_uri, "layer_id": layer_id, "name": name})
        # TiTiler exit: publish_layer returns the raw s3:// COG uri verbatim
        # (the plugin reads it via /vsicurl/) - no tile template.
        return layer_uri

    monkeypatch.setattr(publish_layer_mod, "publish_layer", _fake_publish_layer)

    result = await iul.ingest_user_layer(
        case_id=case_id,
        name="My raster",
        kind="raster",
        s3_uri="s3://cache/user-uploads/x/dem.tif",
        make_aoi=True,
    )

    assert result["status"] == "ok"
    assert result["layer_type"] == "raster"
    assert result["aoi_pinned"] is True
    assert result["bbox"] == pytest.approx([-122.5, 45.5, -122.4, 45.6])
    assert len(calls) == 1
    assert calls[0]["layer_uri"] == "s3://cache/user-uploads/x/dem.tif"

    updated = fake_persistence._cases[case_id]
    assert len(updated.loaded_layer_summaries) == 1
    summary = updated.loaded_layer_summaries[0]
    assert summary["layer_type"] == "raster"
    assert summary["role"] == "input"
    # NEW CONTRACT (TiTiler exit): the persisted uri is the raw s3:// COG the
    # publish returned (plugin /vsicurl/), not an http tile template.
    assert summary["uri"] == "s3://cache/user-uploads/x/dem.tif"
    assert list(updated.bbox) == pytest.approx([-122.5, 45.5, -122.4, 45.6])


@pytest.mark.asyncio
async def test_ingest_raster_reprojects_non_4326_crs(monkeypatch, fake_persistence):
    """A raster in EPSG:3857 is reprojected to a EPSG:4326 bbox (proves the
    rasterio.warp.transform_bounds branch, not just the already-4326 path)."""
    from trid3nt_server.tools import publish_layer as publish_layer_mod

    case_id = new_ulid()
    fake_persistence._cases[case_id] = _case(case_id)
    data = _tiny_geotiff_bytes_3857()
    _mock_s3_object(monkeypatch, data)
    monkeypatch.setattr(
        publish_layer_mod,
        "publish_layer",
        # TiTiler exit: the raster publish echoes the raw s3:// COG uri.
        lambda *, layer_uri, layer_id, name=None, **_kw: layer_uri,
    )

    result = await iul.ingest_user_layer(
        case_id=case_id, name="Web Mercator raster", kind="raster", s3_uri="s3://b/k.tif"
    )
    assert result["status"] == "ok"
    minx, miny, maxx, maxy = result["bbox"]
    # ~(-116.8, 45.6) to (-116.7, 45.7) once reprojected out of Web Mercator.
    assert -120 < minx < -110
    assert 40 < miny < 50
    assert minx < maxx
    assert miny < maxy


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_bad_kind_rejected(fake_persistence):
    with pytest.raises(iul.ImportLayerInputError):
        await iul.ingest_user_layer(
            case_id=new_ulid(),
            name="x",
            kind="mesh",
            s3_uri="s3://cache/user-uploads/x/y.tif",
        )


@pytest.mark.asyncio
async def test_ingest_object_too_large_rejected(monkeypatch, fake_persistence):
    case_id = new_ulid()
    fake_persistence._cases[case_id] = _case(case_id)
    monkeypatch.setattr(
        iul, "_head_object_size", lambda uri: iul.MAX_INGEST_BYTES + 1
    )
    with pytest.raises(iul.ObjectTooLargeError):
        await iul.ingest_user_layer(
            case_id=case_id,
            name="Huge",
            kind="raster",
            s3_uri="s3://cache/user-uploads/x/huge.tif",
        )


@pytest.mark.asyncio
async def test_ingest_missing_object_rejected(monkeypatch, fake_persistence):
    case_id = new_ulid()
    fake_persistence._cases[case_id] = _case(case_id)

    def _missing(uri):
        raise iul.ObjectNotFoundError(f"no such object: {uri}")

    monkeypatch.setattr(iul, "_head_object_size", _missing)
    with pytest.raises(iul.ObjectNotFoundError):
        await iul.ingest_user_layer(
            case_id=case_id,
            name="Gone",
            kind="vector",
            s3_uri="s3://cache/user-uploads/x/gone.geojson",
        )


@pytest.mark.asyncio
async def test_ingest_case_not_found_rejected(monkeypatch, fake_persistence):
    data = _geojson_bytes()
    _mock_s3_object(monkeypatch, data)
    with pytest.raises(iul.CaseNotFoundError):
        await iul.ingest_user_layer(
            case_id="01NOPE",
            name="x",
            kind="vector",
            s3_uri="s3://cache/user-uploads/x/y.geojson",
        )


@pytest.mark.asyncio
async def test_ingest_missing_case_id_rejected(fake_persistence):
    with pytest.raises(iul.ImportLayerInputError):
        await iul.ingest_user_layer(
            case_id="   ", name="x", kind="vector", s3_uri="s3://b/k.geojson"
        )


@pytest.mark.asyncio
async def test_ingest_bad_s3_uri_rejected(fake_persistence):
    with pytest.raises(iul.ImportLayerInputError):
        await iul.ingest_user_layer(
            case_id=new_ulid(), name="x", kind="vector", s3_uri="/local/path.geojson"
        )


# ---------------------------------------------------------------------------
# Merge policy: a second push replaces-by-layer_id, an unrelated existing
# entry survives untouched.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_merges_alongside_existing_layers(monkeypatch, fake_persistence):
    case_id = new_ulid()
    existing = {
        "layer_id": "flood-depth-peak-abc",
        "name": "Flood depth",
        "layer_type": "raster",
        "uri": "https://tiles.example/flood",
        "style_preset": "continuous_flood_depth",
        "visible": True,
        "role": "primary",
        "temporal": False,
    }
    fake_persistence._cases[case_id] = _case(
        case_id,
        loaded_layer_summaries=[existing],
        layer_summary=["flood-depth-peak-abc"],
    )
    data = _geojson_bytes()
    _mock_s3_object(monkeypatch, data)

    result = await iul.ingest_user_layer(
        case_id=case_id, name="AOI", kind="vector", s3_uri="s3://b/aoi.geojson"
    )

    updated = fake_persistence._cases[case_id]
    ids = [d["layer_id"] for d in updated.loaded_layer_summaries]
    assert existing["layer_id"] in ids
    assert result["layer_id"] in ids
    assert len(updated.loaded_layer_summaries) == 2


# ---------------------------------------------------------------------------
# LLM tool wrapper (import_user_layer): thin wrapper around the core.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_wrapper_requires_case_id(fake_persistence):
    with pytest.raises(iul.CaseNotFoundError):
        await iul.import_user_layer(
            s3_uri="s3://b/k.geojson", name="x", kind="vector", case_id=None
        )


@pytest.mark.asyncio
async def test_tool_wrapper_happy_path(monkeypatch, fake_persistence):
    case_id = new_ulid()
    fake_persistence._cases[case_id] = _case(case_id)
    data = _geojson_bytes()
    _mock_s3_object(monkeypatch, data)

    result = await iul.import_user_layer(
        s3_uri="s3://b/aoi.geojson",
        name="AOI from chat",
        kind="vector",
        case_id=case_id,
        make_aoi=True,
    )
    assert result["status"] == "ok"
    assert result["aoi_pinned"] is True
    assert list(fake_persistence._cases[case_id].bbox) == pytest.approx(result["bbox"])
