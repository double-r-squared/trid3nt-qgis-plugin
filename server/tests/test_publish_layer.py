"""Unit tests for the ``publish_layer`` atomic tool.

The GCP-era Cloud Run PyQGIS-worker dispatch path (and its tests: mocked
JobsClient round-trips, gs:///vsigs staging, GCS layer_uri validation, .qgs
verification) was removed with the cloud strip - rasters publish exclusively
through the s3 + TiTiler branch, which is covered by
``test_publish_layer_titiler_base_sprint14aws.py`` /
``test_publish_layer_titiler_style_resolver_f51.py`` /
``test_publish_layer_vector_and_overviews_f32_f33.py``.

Coverage here:
1. ``test_publish_layer_registered`` - tool appears in TOOL_REGISTRY with
   the correct metadata (cacheable=False, ttl_class="live-no-cache",
   source_class="publish_layer").
2. ``test_parse_qgs_key`` - ``_parse_qgs_key`` extracts the object key from
   ``gs://`` / ``s3://`` .qgs URIs (the vector-WMS seam parser).
3. ``derive_layer_id`` - registered-handle / basename-stem / ULID-fallback
   derivation (2026-07-08 small-model resilience).
4. ``derive_readable_layer_name`` - OPEN-9: a bare-ULID layer_id never
   reaches the UI's layer list when a better signal is available.
"""

from __future__ import annotations

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.publish_layer import (
    PublishLayerError,
    derive_layer_id,
    derive_readable_layer_name,
    _parse_qgs_key,
    publish_layer,
)


# --------------------------------------------------------------------------- #
# Test 1 - tool registration
# --------------------------------------------------------------------------- #


def test_publish_layer_registered() -> None:
    """publish_layer is in TOOL_REGISTRY with correct metadata."""
    # Import the module to trigger registration (mirrors _import_tools_registry).
    import trid3nt_server.tools.publish_layer  # noqa: F401

    assert "publish_layer" in TOOL_REGISTRY, (
        f"publish_layer not found in TOOL_REGISTRY; keys={sorted(TOOL_REGISTRY)}"
    )
    entry = TOOL_REGISTRY["publish_layer"]
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class == "publish_layer"
    assert entry.fn is publish_layer


# --------------------------------------------------------------------------- #
# Test 2 - _parse_qgs_key (vector-WMS seam parser)
# --------------------------------------------------------------------------- #


def test_parse_qgs_key() -> None:
    """_parse_qgs_key extracts the object key from a gs:// or s3:// URI."""
    assert _parse_qgs_key("gs://legacy-cloud-qgs/sample.qgs") == "sample.qgs"
    assert _parse_qgs_key("gs://bucket/subdir/project.qgs") == "subdir/project.qgs"
    assert _parse_qgs_key("s3://bucket/subdir/project.qgs") == "subdir/project.qgs"

    with pytest.raises(PublishLayerError) as exc_info:
        _parse_qgs_key("/vsigs/bucket/file.qgs")
    assert exc_info.value.error_code == "QGS_URI_PARSE_ERROR"

    with pytest.raises(PublishLayerError) as exc_info:
        _parse_qgs_key("gs://no-key-here/")
    assert exc_info.value.error_code == "QGS_URI_PARSE_ERROR"


# --------------------------------------------------------------------------- #
# 2026-07-08 - layer_id is OPTIONAL (small-model resilience)
#
# Live evidence: local 8B models call publish_layer without layer_id at all
# (TypeError: publish_layer() missing 1 required positional argument:
# 'layer_id'). The arg now defaults to None and is DERIVED - registered
# handle for the resolved layer_uri, else the URI basename stem, else a
# fresh layer-<ulid>.
# --------------------------------------------------------------------------- #


def test_derive_layer_id_prefers_registered_handle() -> None:
    """A layer_uri the registry knows derives the producing tool's layer_id."""
    from trid3nt_server.uri_registry import (
        get_uri_registry,
        reset_uri_registries_for_tests,
    )

    reset_uri_registries_for_tests()
    try:
        reg = get_uri_registry("sess-derive-layer-id")
        reg.record(
            "dem-3dep-10m",
            uri="s3://trid3nt-cache/cache/static-30d/fetch_dem/abc.tif",
            tool_name="fetch_dem",
        )
        derived = derive_layer_id(
            "s3://trid3nt-cache/cache/static-30d/fetch_dem/abc.tif", reg
        )
        assert derived == "dem-3dep-10m"
    finally:
        reset_uri_registries_for_tests()


def test_derive_layer_id_falls_back_to_basename_stem() -> None:
    assert (
        derive_layer_id("s3://bucket/runs/01X/flood_depth_peak.tif")
        == "flood_depth_peak"
    )
    assert derive_layer_id("gs://bucket/dir/continuous-dem-10m.tif") == (
        "continuous-dem-10m"
    )


def test_derive_layer_id_sanitizes_and_never_returns_empty() -> None:
    assert derive_layer_id("s3://bucket/dir/my layer (v2).tif") == "my-layer-v2"
    # No basename at all -> a fresh ULID-suffixed id, never an empty string.
    derived = derive_layer_id("s3://bucket/dir/")
    assert derived.startswith("layer-") and len(derived) > len("layer-")


# --------------------------------------------------------------------------- #
# derive_readable_layer_name (OPEN-9, 2026-07-10): a bare-ULID layer_id must
# never reach the UI's layer summary as the display name when a better
# signal (explicit name, style_preset, or a URI path segment) is available.
# --------------------------------------------------------------------------- #

_BARE_ULID = "01KX5TEZ20BK86EE6DG8PSVFJK"


def test_derive_readable_layer_name_from_hillshade_style_preset() -> None:
    """Omitted name + a known style_preset -> a readable name, not the bare
    ULID layer_id (the live bug this fixes)."""
    name = derive_readable_layer_name(
        None,
        _BARE_ULID,
        "standard_hillshade",
        "https://tiles.example.com/cog/tiles/{z}/{x}/{y}?url=s3://bucket/hillshade/abc123.tif",
    )
    assert name.startswith("Hillshade")
    assert name != _BARE_ULID
    assert _BARE_ULID not in name


def test_derive_readable_layer_name_explicit_name_untouched() -> None:
    """An explicit, non-ULID-shaped name is returned VERBATIM -- no
    disambiguator appended, no override by style_preset/URI."""
    name = derive_readable_layer_name(
        "Fort Myers Flood Depth",
        _BARE_ULID,
        "continuous_flood_depth",
        "https://tiles.example.com/cog/tiles/{z}/{x}/{y}?url=s3://bucket/flood/abc123.tif",
    )
    assert name == "Fort Myers Flood Depth"


def test_derive_readable_layer_name_explicit_name_that_is_itself_a_ulid_is_ignored() -> None:
    """A 'name' that is itself just the bare ULID (a model echoing layer_id
    into both fields) is treated as NO usable name -- falls through to the
    style_preset/URI derivation instead of surfacing the ULID."""
    name = derive_readable_layer_name(
        _BARE_ULID,
        _BARE_ULID,
        "standard_hillshade",
        "https://tiles.example.com/cog/tiles/{z}/{x}/{y}?url=s3://bucket/hillshade/abc123.tif",
    )
    assert name.startswith("Hillshade")
    assert name != _BARE_ULID


def test_derive_readable_layer_name_uri_segment_fallback() -> None:
    """No name, no informative style_preset -> derive from the source URI's
    path segment (e.g. '.../hillshade/<hash>.tif' -> 'Hillshade')."""
    name = derive_readable_layer_name(
        None,
        _BARE_ULID,
        None,
        "s3://trid3nt-cache/cache/hillshade/9f8e7d6c5b4a3210.tif",
    )
    assert name.startswith("Hillshade")
    assert _BARE_ULID not in name


def test_derive_readable_layer_name_generic_fallback_never_bare_ulid() -> None:
    """No name, no style_preset, no human URI segment (flat path, hash-shaped
    stem, no parent directory to fall back to) -> a generic 'Layer' label
    with a disambiguator -- STILL never the bare ULID."""
    name = derive_readable_layer_name(
        None,
        _BARE_ULID,
        None,
        "s3://trid3nt-cache/9f8e7d6c5b4a3210fedcba9876543210.tif",
    )
    assert name.startswith("Layer")
    assert name != _BARE_ULID
    assert _BARE_ULID not in name


def test_derive_readable_layer_name_disambiguator_varies_by_layer_id() -> None:
    """Two layers in the same family get DISTINCT derived names (the
    disambiguator suffix), so they don't collide in the UI's layer list."""
    name_a = derive_readable_layer_name(
        None, "01AAAAAAAAAAAAAAAAAAAAAAAA", "standard_hillshade", "s3://b/x.tif"
    )
    name_b = derive_readable_layer_name(
        None, "01BBBBBBBBBBBBBBBBBBBBBBBB", "standard_hillshade", "s3://b/x.tif"
    )
    assert name_a != name_b
