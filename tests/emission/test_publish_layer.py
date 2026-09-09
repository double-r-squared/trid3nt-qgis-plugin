"""Unit tests for ``publish_layer``'s identity derivation.

The envelope contract is pinned in ``test_publish_layer_envelope.py``; the
vector no-op and overview enforcement in
``test_publish_layer_vector_and_overviews_f32_f33.py``.

Coverage here:
1. ``test_publish_layer_is_not_a_registered_tool`` - publish is a mechanism
   the emission seam calls, never a tool a model routes to.
2. ``derive_readable_layer_name`` - a bare-ULID layer_id never reaches the
   layer list when a better signal is available.
"""

from __future__ import annotations

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.emission.publish import (
    PublishLayerError,
    derive_readable_layer_name,
    publish_layer,
)


# --------------------------------------------------------------------------- #
# Test 1 - publish is a MECHANISM, not a tool
# --------------------------------------------------------------------------- #


def test_publish_layer_is_not_a_registered_tool() -> None:
    """ADR 0313: emission is automatic, so there is no publish intent to route.

    The inverse of the assertion this test used to make. The function still
    exists and is still the one raster-publish chokepoint - it just lives in
    ``emission/`` and is called BY the emission seam, never by a model.
    """
    assert "publish_layer" not in TOOL_REGISTRY, (
        "publish_layer is registered again; emission is automatic and a "
        '"display this" intent has no meaning'
    )
    assert callable(publish_layer)


# --------------------------------------------------------------------------- #
# derive_readable_layer_name (OPEN-9, 2026-07-10): a bare-ULID layer_id must
# never reach the layer summary as the display name when a better
# signal (an explicit name, a declared label, or a URI path segment) exists.
# --------------------------------------------------------------------------- #

_BARE_ULID = "01KX5TEZ20BK86EE6DG8PSVFJK"


def test_derive_readable_layer_name_from_the_declared_label() -> None:
    """Omitted name + a declared label -> a readable name, not the bare
    ULID layer_id (the live bug this fixes)."""
    name = derive_readable_layer_name(
        None,
        _BARE_ULID,
        {"kind": "continuous", "label": "Hillshade"},
        "s3://bucket/hillshade/abc123.tif",
    )
    assert name.startswith("Hillshade")
    assert name != _BARE_ULID
    assert _BARE_ULID not in name


def test_derive_readable_layer_name_explicit_name_untouched() -> None:
    """An explicit, non-ULID-shaped name is returned VERBATIM -- no
    disambiguator appended, no override by the declared label or the URI."""
    name = derive_readable_layer_name(
        "Fort Myers Flood Depth",
        _BARE_ULID,
        {"kind": "continuous", "label": "Flood depth"},
        "s3://bucket/flood/abc123.tif",
    )
    assert name == "Fort Myers Flood Depth"


def test_derive_readable_layer_name_explicit_name_that_is_itself_a_ulid_is_ignored() -> None:
    """A 'name' that is itself just the bare ULID (a model echoing layer_id
    into both fields) is treated as NO usable name -- falls through to the
    declared label / URI derivation instead of surfacing the ULID."""
    name = derive_readable_layer_name(
        _BARE_ULID,
        _BARE_ULID,
        {"kind": "continuous", "label": "Hillshade"},
        "s3://bucket/hillshade/abc123.tif",
    )
    assert name.startswith("Hillshade")
    assert name != _BARE_ULID


def test_derive_readable_layer_name_uri_segment_fallback() -> None:
    """No name, no declared label -> derive from the source URI's
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
    """No name, no declared label, no human URI segment (flat path, hash-shaped
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
        None, "01AAAAAAAAAAAAAAAAAAAAAAAA", {"label": "Hillshade"}, "s3://b/x.tif"
    )
    name_b = derive_readable_layer_name(
        None, "01BBBBBBBBBBBBBBBBBBBBBBBB", {"label": "Hillshade"}, "s3://b/x.tif"
    )
    assert name_a != name_b
