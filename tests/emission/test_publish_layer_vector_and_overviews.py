"""publish_layer: the benign vector no-op, and overview enforcement.

BENIGN VECTOR NO-OP:
  publish_layer is RASTER-ONLY. A vector (.fgb/.geojson/...) handed to it is
  already a store object the plugin opens natively, and GDAL cannot open a
  FlatGeobuf as a raster COG. So it returns a benign, NON-error result: no
  raise (the step card stays green), no registration, and a calm
  function_response so the agent narrates honestly and does not re-call.

OVERVIEW ENFORCEMENT:
  A no-overview COG renders SPOTTY (per-strip range requests time out cold;
  QGIS cannot downsample for low zooms). Before a raster is registered,
  publish_layer VALIDATES the COG has overviews and AUTO-TRANSLATES to a
  tiled+overview COG when missing, then publishes THAT. A raster that ALREADY
  has overviews is published unchanged.

These exercise the pure-helper layer (``_ensure_raster_has_overviews``,
``_is_vector_uri``, ``_benign_vector_noop``) plus the publish path end-to-end
with real GeoTIFF bytes built by rasterio - no network I/O.
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from trid3nt_server.emission.publish import (
    PublishLayerError,
    _benign_vector_noop,
    _build_cog_with_overviews,
    _ensure_raster_has_overviews,
    _is_vector_uri,
    _raster_has_overviews,
    publish_layer,
)


# --------------------------------------------------------------------------- #
# GeoTIFF byte builders (real rasterio rasters so overview inspection is real)
# --------------------------------------------------------------------------- #


def _flat_geotiff_bytes(size: int = 1024) -> bytes:
    """A georeferenced single-band GeoTIFF with NO overviews."""
    data = (np.random.rand(size, size) * 255).astype("uint8")
    transform = rasterio.transform.from_origin(0, size, 1, 1)
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            height=size,
            width=size,
            count=1,
            dtype="uint8",
            crs="EPSG:4326",
            transform=transform,
        ) as dst:
            dst.write(data, 1)
        return mem.read()


def _cog_with_overviews_bytes(size: int = 1024) -> bytes:
    """A tiled GeoTIFF that HAS overviews built in (the desired publish shape)."""
    flat = _flat_geotiff_bytes(size)
    out = _build_cog_with_overviews(flat)
    assert out is not None, "test setup: could not build an overview COG"
    return out


# --------------------------------------------------------------------------- #
# The benign vector no-op (helpers)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "uri",
    [
        "s3://b/roads.fgb",
        "s3://b/rivers.geojson",
        "s3://b/parcels.geoparquet",
        "s3://b/x.parquet",
        "s3://b/y.gpkg",
        "s3://b/z.shp",
        "s3://b/dir/data.json",
        "S3://B/UPPER.FGB",  # case-insensitive
        "s3://b/trailing.fgb/",  # trailing slash tolerated
    ],
)
def test_is_vector_uri_true_for_vector_extensions(uri: str) -> None:
    assert _is_vector_uri(uri) is True


@pytest.mark.parametrize(
    "uri",
    [
        "s3://b/flood_depth_peak.tif",
        "gs://b/hillshade.tif",
        "s3://b/relief.tiff",
        "https://host/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=x",
    ],
)
def test_is_vector_uri_false_for_rasters(uri: str) -> None:
    assert _is_vector_uri(uri) is False


def test_benign_vector_noop_is_non_error_string() -> None:
    """The benign signal does NOT raise and is a clear, honest message."""
    msg = _benign_vector_noop("s3://b/roads.fgb", "roads-layer")
    assert isinstance(msg, str)
    assert "noop" in msg.lower()
    assert "vector" in msg.lower()
    # Must steer the LLM away from retrying.
    assert "roads-layer" in msg


# --------------------------------------------------------------------------- #
# The benign vector no-op (end to end)
# --------------------------------------------------------------------------- #


def test_publish_layer_vector_returns_benign_and_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A vector: NO raise, NO registration."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        "trid3nt_server.emission.publish.observe_published_layer",
        lambda *a, **k: calls.append((a, k)),
    )

    result = publish_layer(layer_uri="s3://bucket/roads.fgb", layer_id="roads")

    # 1. It returned a benign string (no exception).
    assert isinstance(result, str)
    assert result.startswith("noop")
    # 2. observe_published_layer was NEVER called for the vector.
    assert calls == [], f"vector no-op must not register a layer face; got {calls}"


def test_publish_layer_geojson_returns_benign_not_error() -> None:
    """A .geojson vector also returns benign (does not raise)."""
    out = publish_layer(layer_uri="s3://bucket/rivers.geojson", layer_id="rivers")
    assert out.startswith("noop")


def test_publish_layer_raster_still_raises_for_non_s3() -> None:
    """A non-vector, non-s3 raster handle still raises (unchanged behavior)."""
    with pytest.raises(PublishLayerError) as exc:
        publish_layer(layer_uri="gs://legacy/bucket/x.tif", layer_id="flood")
    assert exc.value.error_code == "LAYER_URI_NOT_FOUND"


# --------------------------------------------------------------------------- #
# Overview detection
# --------------------------------------------------------------------------- #


def test_raster_has_overviews_false_for_flat_geotiff() -> None:
    assert _raster_has_overviews(_flat_geotiff_bytes()) is False


def test_raster_has_overviews_true_for_overview_cog() -> None:
    assert _raster_has_overviews(_cog_with_overviews_bytes()) is True


def test_raster_has_overviews_none_for_non_raster() -> None:
    """Unreadable / non-raster bytes → None (cannot determine → fail-open)."""
    assert _raster_has_overviews(b"NOT A RASTER") is None


def test_build_cog_with_overviews_adds_overviews() -> None:
    """The auto-translate produces a COG whose band-1 overviews are non-empty."""
    flat = _flat_geotiff_bytes()
    assert _raster_has_overviews(flat) is False
    cog = _build_cog_with_overviews(flat)
    assert cog is not None
    assert _raster_has_overviews(cog) is True


# --------------------------------------------------------------------------- #
# _ensure_raster_has_overviews (local-path round trip)
# --------------------------------------------------------------------------- #


def test_ensure_overviews_auto_translates_when_missing(tmp_path) -> None:
    """A no-overview COG is auto-translated; the returned URI points at a NEW
    overview-bearing COG (the original is left untouched)."""
    src = tmp_path / "flat.tif"
    src.write_bytes(_flat_geotiff_bytes())

    out_uri = _ensure_raster_has_overviews(str(src))

    # The published URI must differ from the source (a fresh sibling).
    assert out_uri != str(src), "missing-overview raster must be auto-translated"
    with rasterio.open(out_uri) as ds:
        assert ds.overviews(1), "auto-translated COG must carry overviews"
    # The original is untouched (still no overviews).
    with rasterio.open(str(src)) as orig:
        assert orig.overviews(1) == []


def test_ensure_overviews_unchanged_when_already_present(tmp_path) -> None:
    """A COG that ALREADY has overviews is published unchanged (same URI)."""
    src = tmp_path / "good_cog.tif"
    src.write_bytes(_cog_with_overviews_bytes())

    out_uri = _ensure_raster_has_overviews(str(src))

    assert out_uri == str(src), "overview-bearing COG must publish unchanged"


def test_ensure_overviews_fail_open_on_unreadable(tmp_path) -> None:
    """An unreadable raster fails open: URI returned unchanged (legacy)."""
    src = tmp_path / "junk.tif"
    src.write_bytes(b"NOT A RASTER")
    out_uri = _ensure_raster_has_overviews(str(src))
    assert out_uri == str(src)


def test_ensure_overviews_fail_open_on_missing_path() -> None:
    """A non-existent local path fails open (read returns None)."""
    out_uri = _ensure_raster_has_overviews("/nonexistent/path/raster.tif")
    assert out_uri == "/nonexistent/path/raster.tif"


# --------------------------------------------------------------------------- #
# End to end: auto-translate, then the store uri as the envelope
# --------------------------------------------------------------------------- #


def test_publish_layer_auto_translates_no_overview_cog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raster lacking overviews: publish_layer reads it, auto-translates to a
    NEW overview COG, and returns the NEW s3 URI as the envelope uri."""
    flat_bytes = _flat_geotiff_bytes()
    written: dict[str, bytes] = {}

    def _fake_read(uri: str) -> bytes | None:
        # The overview check reads the SOURCE; the style resolver then re-reads
        # the (post-translate) overview URI to probe the band/palette.
        # Accept both: serve the flat bytes for the source, None for the new
        # overview URI (resolver degrades to a safe default — this test asserts
        # the URI routing, not the resolved style).
        if uri == "s3://bucket/runs/flat.tif":
            return flat_bytes
        return None

    def _fake_write(uri: str, cog_bytes: bytes) -> str:
        # Simulate the s3 sibling write; assert the bytes carry overviews.
        assert _raster_has_overviews(cog_bytes) is True
        new_uri = "s3://bucket/runs/overviews/NEWULID.tif"
        written[new_uri] = cog_bytes
        return new_uri

    monkeypatch.setattr(
        "trid3nt_server.emission.publish._read_raster_bytes", _fake_read
    )
    monkeypatch.setattr(
        "trid3nt_server.emission.publish._write_overview_cog", _fake_write
    )

    out = publish_layer(
        layer_uri="s3://bucket/runs/flat.tif", layer_id="flood-demo"
    )

    # The envelope uri must be the AUTO-TRANSLATED (overview) COG, not the
    # original no-overview source.
    assert out == "s3://bucket/runs/overviews/NEWULID.tif"
    assert written, "an overview COG should have been written"


def test_publish_layer_overview_cog_published_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raster that ALREADY has overviews: URI unchanged, no re-translate."""
    good = _cog_with_overviews_bytes()

    monkeypatch.setattr(
        "trid3nt_server.emission.publish._read_raster_bytes",
        lambda uri: good,
    )

    def _must_not_write(uri: str, cog_bytes: bytes) -> str:  # pragma: no cover
        raise AssertionError("must NOT re-translate an overview-bearing COG")

    monkeypatch.setattr(
        "trid3nt_server.emission.publish._write_overview_cog", _must_not_write
    )

    out = publish_layer(
        layer_uri="s3://bucket/runs/good.tif", layer_id="flood-demo"
    )
    # Original s3 URI is returned verbatim as the envelope uri.
    assert out == "s3://bucket/runs/good.tif"


# --------------------------------------------------------------------------- #
# job-0324 — colormap preservation in the overview-enforcement re-write.
#
# NLCD land cover is a single-band palette-index COG with an EMBEDDED GDAL
# color table; TiTiler colorizes from it. _build_cog_with_overviews's
# re-translate MUST carry that table forward or the layer renders solid GREY.
# Non-paletted rasters (DEM/hillshade/flood depth) must pass through with NO
# fabricated colormap, and overviews must still build in both cases.
# --------------------------------------------------------------------------- #


_NLCD_COLORMAP = {
    0: (0, 0, 0, 0),
    11: (72, 109, 162, 255),
    21: (222, 197, 197, 255),
    41: (56, 129, 78, 255),
    81: (220, 217, 57, 255),
    90: (186, 217, 235, 255),
    255: (0, 0, 0, 0),
}


def _paletted_geotiff_bytes(size: int = 1024) -> bytes:
    """A flat single-band uint8 GeoTIFF WITH an embedded color table, no overviews."""
    classes = np.array([11, 21, 41, 81, 90], dtype="uint8")
    data = classes[np.random.randint(0, len(classes), size=(size, size))]
    transform = rasterio.transform.from_origin(0, size, 1, 1)
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            height=size,
            width=size,
            count=1,
            dtype="uint8",
            crs="EPSG:4326",
            transform=transform,
            nodata=255,
        ) as dst:
            dst.write(data, 1)
            dst.write_colormap(1, _NLCD_COLORMAP)
        return mem.read()


def _colormap_of(raster_bytes: bytes):
    with MemoryFile(raster_bytes) as mem, mem.open() as src:
        try:
            return src.colormap(1)
        except ValueError:
            return None


def _colorinterp0_name(raster_bytes: bytes) -> str:
    with MemoryFile(raster_bytes) as mem, mem.open() as src:
        return src.colorinterp[0].name


def _assert_colormap_round_trip_equal(src_bytes: bytes, out_bytes: bytes) -> None:
    """Output band-1 table must equal the SOURCE's round-tripped table.

    GDAL's GTiff palette writer normalizes alpha on write, so we compare the
    output against the source's own ``colormap(1)`` (apples-to-apples) rather
    than a hand-written RGBA dict. A mismatch = the re-write changed the table.
    """
    src_cmap = _colormap_of(src_bytes)
    assert src_cmap is not None, "test fixture lost its colormap"
    out_cmap = _colormap_of(out_bytes)
    assert out_cmap is not None, "re-write dropped the colormap (job-0324)"
    for idx in _NLCD_COLORMAP:
        assert out_cmap.get(idx) == src_cmap.get(idx), (
            idx,
            out_cmap.get(idx),
            src_cmap.get(idx),
        )


def test_build_cog_with_overviews_preserves_colormap() -> None:
    """The overview re-write keeps the embedded NLCD color table AND builds
    overviews — the job-0324 grey-land-cover fix."""
    flat = _paletted_geotiff_bytes()
    assert _colormap_of(flat) is not None  # sanity: source has a table
    assert _raster_has_overviews(flat) is False

    cog = _build_cog_with_overviews(flat)
    assert cog is not None

    _assert_colormap_round_trip_equal(flat, cog)
    # Overviews still present.
    assert _raster_has_overviews(cog) is True
    # Band marked palette so TiTiler treats pixels as indices.
    assert _colorinterp0_name(cog) == "palette"


def test_build_cog_with_overviews_rasterio_preserves_colormap() -> None:
    """The pure-rasterio fallback path (no GDAL CLI) also preserves the table."""
    from trid3nt_server.emission.publish import _build_cog_with_overviews_rasterio

    flat = _paletted_geotiff_bytes()
    cog = _build_cog_with_overviews_rasterio(flat)
    assert cog is not None
    _assert_colormap_round_trip_equal(flat, cog)
    assert _raster_has_overviews(cog) is True


def test_build_cog_with_overviews_no_colormap_unchanged() -> None:
    """A continuous (DEM-like) raster gets overviews but NO fabricated colormap."""
    flat = _flat_geotiff_bytes()
    assert _colormap_of(flat) is None  # sanity: no table

    cog = _build_cog_with_overviews(flat)
    assert cog is not None
    assert _colormap_of(cog) is None, "must NOT fabricate a colormap on non-paletted"
    assert _raster_has_overviews(cog) is True
    assert _colorinterp0_name(cog) != "palette"
