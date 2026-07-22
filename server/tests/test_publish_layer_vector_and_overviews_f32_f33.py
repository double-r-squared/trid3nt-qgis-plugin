"""publish_layer F32 (benign vector no-op) + F33 (overview enforcement) tests.

F32 — BENIGN VECTOR REJECTION:
  publish_layer is RASTER-ONLY. Vectors (.fgb/.geojson/...) handed to it are
  ALREADY rendered on the map inline (Wave 4.9 GeoJSON via add_loaded_layer).
  Pre-F32 the tool RAISED ``PUBLISH_LAYER_VECTOR_NOT_RASTER`` → a red
  "Publishing layer… failed" card on a layer the user can already see. F32 turns
  that into a benign, NON-error result: no raise (so the step card stays green),
  no tile template, no ``observe_published_layer`` registration (no hanging-tile
  face), and a calm function_response so the agent narrates honestly + does not
  re-call. Covered on BOTH the s3 (AWS/TiTiler) and gs (GCS/worker) branches.

F33 — OVERVIEW ENFORCEMENT:
  A no-overview COG renders SPOTTY (per-strip range requests time out cold;
  TiTiler/QGIS Server can't downsample for low zooms). Before a raster's tile
  template / WMS face is registered, publish_layer now VALIDATES the COG has
  overviews and AUTO-TRANSLATES to a tiled+overview COG when missing (reusing
  ``compute_hillshade._translate_to_cog`` with a rasterio fallback), then
  publishes THAT. A raster that ALREADY has overviews is published unchanged.

These exercise the pure-helper layer (``_ensure_raster_has_overviews``,
``_is_vector_uri``, ``_benign_vector_noop``) plus the s3 branch end-to-end with
real GeoTIFF bytes built by rasterio — no Cloud Run / GCS / TiTiler network I/O.
"""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile

from grace2_agent.tools.publish_layer import (
    PublishLayerError,
    _benign_vector_noop,
    _build_cog_with_overviews,
    _build_vector_wms_url,
    _ensure_raster_has_overviews,
    _is_vector_uri,
    _parse_qgs_key,
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
# F32 — benign vector no-op (helpers)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "uri",
    [
        "s3://b/roads.fgb",
        "s3://b/rivers.geojson",
        "gs://b/admin.geojson",
        "s3://b/parcels.geoparquet",
        "s3://b/x.parquet",
        "gs://b/y.gpkg",
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
# F32 — benign vector no-op (s3 branch end-to-end)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _s3_titiler(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", "https://cf.example.net")


def test_publish_layer_vector_s3_returns_benign_no_template_no_register(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vector on the s3 branch: NO raise, NO tile template, NO registration."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: calls.append((a, k)),
    )

    result = publish_layer(layer_uri="s3://bucket/roads.fgb", layer_id="roads")

    # 1. It returned a benign string (no exception).
    assert isinstance(result, str)
    # 2. It is NOT a tile template (no hanging-tile face minted).
    assert "/cog/tiles/" not in result
    assert "{z}/{x}/{y}" not in result
    assert result.startswith("noop")
    # 3. observe_published_layer was NEVER called for the vector.
    assert calls == [], f"vector no-op must not register a layer face; got {calls}"


def test_publish_layer_geojson_s3_returns_benign_not_error(
    _s3_titiler: None,
) -> None:
    """A .geojson vector also returns benign (does not raise)."""
    out = publish_layer(layer_uri="s3://bucket/rivers.geojson", layer_id="rivers")
    assert out.startswith("noop")


def test_publish_layer_raster_s3_still_raises_for_non_s3(_s3_titiler: None) -> None:
    """A non-vector, non-s3 raster handle still raises (unchanged behavior)."""
    with pytest.raises(PublishLayerError) as exc:
        publish_layer(layer_uri="gs://legacy/bucket/x.tif", layer_id="flood")
    assert exc.value.error_code == "LAYER_URI_NOT_FOUND"


# --------------------------------------------------------------------------- #
# job-0308 - s3-branch QGIS-vector route (env-gated, NO-OP until infra exists)
#
# WHEN GRACE2_QGIS_WMS_BASE is set -> publish_layer composes a styled WMS
# GetMap URL for the vector (pointed at the AWS QGIS Server) and registers it
# as the display face. WHEN it is UNSET -> the existing benign no-op is
# returned, so live behavior is byte-for-byte unchanged until the AWS QGIS
# Server is stood up.
# --------------------------------------------------------------------------- #


def test_build_vector_wms_url_is_well_formed() -> None:
    """The helper mirrors the GCP MAP=/LAYERS= shape, pointed at the WMS base."""
    url = _build_vector_wms_url(
        "https://cf.example.net/ogc/wms",
        "s3://bucket/roads.fgb",
        "roads-layer",
        "grace2-sample.qgs",
    )
    assert url.startswith("https://cf.example.net/ogc/wms?")
    # MAP= carries the /mnt/qgs/<key> mount convention (URL-encoded).
    assert "MAP=" in url
    assert "grace2-sample.qgs" in url
    # Standard WMS GetMap envelope so uri_registry recognizes it as a render
    # face (LAYERS= + service=wms).
    assert "SERVICE=WMS" in url
    assert "REQUEST=GetMap" in url
    assert "LAYERS=roads-layer" in url
    assert "STYLES=" in url
    assert "FORMAT=image/png" in url


def test_build_vector_wms_url_recognized_as_wms_render_face() -> None:
    """The composed URL is recognized by uri_registry as a WMS display face."""
    from grace2_agent.uri_registry import _looks_like_wms

    url = _build_vector_wms_url(
        "https://cf.example.net/ogc/wms",
        "s3://bucket/rivers.geojson",
        "rivers",
        "grace2-sample.qgs",
    )
    assert _looks_like_wms(url) is True


def test_publish_layer_vector_s3_env_unset_returns_benign_no_op(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENV UNSET: vector on s3 still benign no-op (current behavior unchanged)."""
    # The _s3_titiler fixture sets storage=s3 + tile base but NOT the QGIS WMS
    # base; ensure it is absent.
    monkeypatch.delenv("GRACE2_QGIS_WMS_BASE", raising=False)
    calls: list[tuple] = []
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: calls.append((a, k)),
    )

    result = publish_layer(layer_uri="s3://bucket/roads.fgb", layer_id="roads")

    assert isinstance(result, str)
    assert result.startswith("noop")
    assert "/cog/tiles/" not in result
    assert "service=wms" not in result.lower()
    # No display face registered for the no-op.
    assert calls == [], f"unset-env vector must stay a no-op; got {calls}"


def test_publish_layer_vector_s3_env_set_returns_vector_wms_url(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENV SET: vector on s3 -> a well-formed styled WMS URL + display face."""
    monkeypatch.setenv("GRACE2_QGIS_WMS_BASE", "https://cf.example.net/ogc/wms")
    calls: list[tuple] = []
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: calls.append((a, k)),
    )

    result = publish_layer(layer_uri="s3://bucket/roads.fgb", layer_id="roads")

    # 1. A well-formed WMS URL (not a benign no-op).
    assert isinstance(result, str)
    assert not result.startswith("noop")
    assert result.startswith("https://cf.example.net/ogc/wms?")
    assert "SERVICE=WMS" in result
    assert "REQUEST=GetMap" in result
    assert "LAYERS=roads" in result
    assert "MAP=" in result
    # 2. BOTH faces registered: the s3:// data uri + the WMS display face.
    assert len(calls) == 1, f"expected one registration; got {calls}"
    (_args, kwargs) = calls[0]
    assert kwargs["gcs_uri"] == "s3://bucket/roads.fgb"
    assert kwargs["wms_url"] == result


# --------------------------------------------------------------------------- #
# job-0308 P0 (LOW forward-path): the .qgs key resolver must accept s3:// as
# well as gs://. On AWS the canonical .qgs lives at s3://...; if the QGIS-vector
# WMS branch (GRACE2_QGIS_WMS_BASE set) resolved a gs://-only key it would fail
# on the live AWS stack. The no-op-when-unset path is unaffected.
# --------------------------------------------------------------------------- #


def test_parse_qgs_key_accepts_s3() -> None:
    """An s3:// .qgs URI resolves to the same key as the gs:// form."""
    assert (
        _parse_qgs_key("s3://grace-2-hazard-prod-qgs/grace2-sample.qgs")
        == "grace2-sample.qgs"
    )
    assert (
        _parse_qgs_key("s3://bucket/nested/dir/project.qgs")
        == "nested/dir/project.qgs"
    )


def test_parse_qgs_key_accepts_gs_unchanged() -> None:
    """The gs:// path is byte-identical to before (no regression)."""
    assert (
        _parse_qgs_key("gs://grace-2-hazard-prod-qgs/grace2-sample.qgs")
        == "grace2-sample.qgs"
    )


@pytest.mark.parametrize(
    "bad_uri",
    [
        "https://host/project.qgs",  # wrong scheme
        "s3://bucket-only",  # no key component
        "gs://bucket-only",  # no key component
        "s3://bucket/",  # trailing slash, empty key
    ],
)
def test_parse_qgs_key_rejects_bad_uris(bad_uri: str) -> None:
    """Non-gs/s3 schemes and key-less URIs still raise the typed error."""
    with pytest.raises(PublishLayerError) as exc:
        _parse_qgs_key(bad_uri)
    assert exc.value.error_code == "QGS_URI_PARSE_ERROR"


def test_publish_layer_vector_s3_env_set_with_s3_qgs_uri(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENV SET + an s3:// project_qgs_uri -> the WMS branch resolves the key
    (no QGS_URI_PARSE_ERROR) and composes a styled WMS URL with that key."""
    monkeypatch.setenv("GRACE2_QGIS_WMS_BASE", "https://cf.example.net/ogc/wms")
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: None,
    )

    result = publish_layer(
        layer_uri="s3://bucket/roads.fgb",
        layer_id="roads",
        project_qgs_uri="s3://grace-2-hazard-prod-qgs/grace2-sample.qgs",
    )

    # Did NOT raise (s3 .qgs key resolved) and is a real WMS URL.
    assert result.startswith("https://cf.example.net/ogc/wms?")
    assert "SERVICE=WMS" in result
    assert "REQUEST=GetMap" in result
    # The s3 .qgs key rode into the MAP= mount param.
    assert "grace2-sample.qgs" in result


def test_publish_layer_vector_s3_env_set_trailing_slash_base(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trailing slash on the WMS base is tolerated (no double slash)."""
    monkeypatch.setenv("GRACE2_QGIS_WMS_BASE", "https://cf.example.net/ogc/wms/")
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: None,
    )

    result = publish_layer(layer_uri="s3://bucket/rivers.geojson", layer_id="rivers")

    assert result.startswith("https://cf.example.net/ogc/wms?")
    assert "ogc/wms//" not in result


def test_publish_layer_vector_s3_env_blank_falls_back_to_no_op(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank (whitespace-only after strip) WMS base falls back to the no-op."""
    monkeypatch.setenv("GRACE2_QGIS_WMS_BASE", "")
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: None,
    )

    result = publish_layer(layer_uri="s3://bucket/roads.fgb", layer_id="roads")

    assert result.startswith("noop")


# --------------------------------------------------------------------------- #
# F33 — overview detection
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
# F33 — _ensure_raster_has_overviews (local-path round trip)
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
# F33 — s3 branch end-to-end (auto-translate then tile template)
# --------------------------------------------------------------------------- #


def test_publish_layer_s3_auto_translates_no_overview_cog(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """s3 raster lacking overviews: publish_layer reads it, auto-translates to a
    NEW overview COG, and bakes the NEW s3 URI into the tile template."""
    flat_bytes = _flat_geotiff_bytes()
    written: dict[str, bytes] = {}

    def _fake_read(uri: str) -> bytes | None:
        # F33 reads the SOURCE for the overview check; F51's style resolver then
        # re-reads the (post-translate) overview URI to probe the band/palette.
        # Accept both: serve the flat bytes for the source, None for the new
        # overview URI (resolver degrades to a safe default — this test asserts
        # the URL routing, not the style suffix).
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
        "grace2_agent.tools.publish_layer._read_raster_bytes", _fake_read
    )
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer._write_overview_cog", _fake_write
    )

    template = publish_layer(
        layer_uri="s3://bucket/runs/flat.tif", layer_id="flood-demo"
    )

    # The template must reference the AUTO-TRANSLATED (overview) COG, NOT the
    # original no-overview source.
    assert "overviews%2FNEWULID.tif" in template or "overviews/NEWULID.tif" in template
    assert "runs%2Fflat.tif" not in template
    assert template.startswith("https://cf.example.net/cog/tiles/")
    assert written, "an overview COG should have been written"


def test_publish_layer_s3_overview_cog_published_unchanged(
    _s3_titiler: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """s3 raster that ALREADY has overviews: URI unchanged, no re-translate."""
    good = _cog_with_overviews_bytes()

    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer._read_raster_bytes",
        lambda uri: good,
    )

    def _must_not_write(uri: str, cog_bytes: bytes) -> str:  # pragma: no cover
        raise AssertionError("must NOT re-translate an overview-bearing COG")

    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer._write_overview_cog", _must_not_write
    )

    from urllib.parse import quote

    template = publish_layer(
        layer_uri="s3://bucket/runs/good.tif", layer_id="flood-demo"
    )
    # Original s3 URI is what rides in ?url=.
    assert f"?url={quote('s3://bucket/runs/good.tif', safe='')}" in template


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
    """The F33 overview re-write keeps the embedded NLCD color table AND builds
    overviews — the job-0324 grey-land-cover fix."""
    flat = _paletted_geotiff_bytes()
    assert _colormap_of(flat) is not None  # sanity: source has a table
    assert _raster_has_overviews(flat) is False

    cog = _build_cog_with_overviews(flat)
    assert cog is not None

    _assert_colormap_round_trip_equal(flat, cog)
    # Overviews still present (F33 must not regress).
    assert _raster_has_overviews(cog) is True
    # Band marked palette so TiTiler treats pixels as indices.
    assert _colorinterp0_name(cog) == "palette"


def test_build_cog_with_overviews_rasterio_preserves_colormap() -> None:
    """The pure-rasterio fallback path (no GDAL CLI) also preserves the table."""
    from grace2_agent.tools.publish_layer import _build_cog_with_overviews_rasterio

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
