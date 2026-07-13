"""publish_layer unknown/placeholder-handle guard (OPEN-17 class, 2026-07-13).

Live local-8B incident: a 0-event fetch raised its typed error, then the
model called ``publish_layer`` anyway with an invented handle
('LayerURI_from_previous_step'-style). Pre-guard, that bare token fell
through the uri_registry fail-open (non-URI-shaped strings pass through) and
died deep in the publish path with an unhelpful storage/GDAL error. The
guard fails at the door with a TYPED, retryable ``UNKNOWN_LAYER_HANDLE``
error that NAMES the case's actually-available handles (most recent first,
capped at 8) so the small model self-corrects or stops cleanly.

Valid-path behavior (s3:// / gs:// / http(s):// / file:// / /vsi* / absolute
paths) is byte-identical - covered by the predicate tests below plus the
pre-existing publish_layer suites.
"""

from __future__ import annotations

import pytest

from grace2_agent.tools.publish_layer import (
    PublishLayerError,
    _looks_like_unresolved_handle,
    _unknown_handle_error,
    publish_layer,
)
from grace2_agent.uri_registry import (
    activate_registry,
    ambient_layer_handle_inventory,
    deactivate_registry,
    get_uri_registry,
    reset_uri_registries_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_registries():
    reset_uri_registries_for_tests()
    yield
    reset_uri_registries_for_tests()


# ---------------------------------------------------------------------------
# Predicate: what counts as an unresolved handle.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "LayerURI_from_previous_step",
        "layer_uri_from_fetch_usgs_earthquakes",
        "usgs-earthquakes-abc12345",  # a real-shaped handle the server failed
        "qgis://project1",  # fabricated scheme
        "output_of_previous_tool",
        # Angle-bracket template placeholders - live 2026-07-13: the model
        # passed a gs-shaped one, which the scheme allowlist alone misses.
        "gs://<result-fetched_usgs_earthquakes-uri>",
        "s3://<bucket>/<key>.tif",
        "<layer_uri_from_fetch_dem>",
        # Literal-ellipsis placeholder - live 2026-07-13: slipped past the
        # scheme allowlist into the F32 benign vector no-op (fabricated
        # success-shaped "Layer published").
        "s3://.../earthquakes_layer.fgb",
        "gs://.../flood_depth_peak.tif",
        "",
        "   ",
    ],
)
def test_unresolved_handle_shapes(value: str) -> None:
    assert _looks_like_unresolved_handle(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "s3://trid3nt-cache/cache/dynamic-1h/usgs/ab12.fgb",
        "gs://grace-2-hazard-prod-runs/run-abc/flood_depth_peak.tif",
        "http://127.0.0.1:8083/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=s3%3A%2F%2Fb%2Fk.tif",
        "https://example.com/wms?service=WMS&LAYERS=x",
        "file:///tmp/x.tif",
        "/vsigs/bucket/key.tif",
        "/vsis3/bucket/key.tif",
        "/tmp/local/path.tif",
    ],
)
def test_consumable_uri_shapes_pass(value: str) -> None:
    assert _looks_like_unresolved_handle(value) is False


# ---------------------------------------------------------------------------
# Tool-level guard: typed error, retryable, names available handles.
# ---------------------------------------------------------------------------


def test_placeholder_raises_typed_error_no_registry() -> None:
    with pytest.raises(PublishLayerError) as ei:
        publish_layer(layer_uri="LayerURI_from_previous_step")
    err = ei.value
    assert err.error_code == "UNKNOWN_LAYER_HANDLE"
    assert err.retryable is True
    msg = str(err)
    assert "LayerURI_from_previous_step" in msg
    assert "no layers have been produced in this case yet" in msg
    assert "auto-publish" in msg


def test_fabricated_scheme_raises_typed_error() -> None:
    with pytest.raises(PublishLayerError) as ei:
        publish_layer(layer_uri="qgis://project1")
    assert ei.value.error_code == "UNKNOWN_LAYER_HANDLE"
    assert ei.value.retryable is True


def test_error_names_available_handles_most_recent_first() -> None:
    reg = get_uri_registry("sess-unknown-handle")
    reg.record(
        "dem-3dep-10m",
        uri="s3://trid3nt-cache/cache/static-30d/fetch_dem/aa.tif",
        tool_name="fetch_dem",
    )
    reg.record(
        "usgs-earthquakes-1a2b3c4d",
        uri="s3://trid3nt-cache/cache/dynamic-1h/usgs/bb.fgb",
        tool_name="fetch_usgs_earthquakes",
    )
    token = activate_registry(reg)
    try:
        with pytest.raises(PublishLayerError) as ei:
            publish_layer(layer_uri="LayerURI_from_previous_step")
        msg = str(ei.value)
        assert "'usgs-earthquakes-1a2b3c4d'" in msg
        assert "'dem-3dep-10m'" in msg
        # Most recent first.
        assert msg.index("usgs-earthquakes-1a2b3c4d") < msg.index("dem-3dep-10m")
        assert "pass one verbatim" in msg
        assert "auto-publish" in msg
    finally:
        deactivate_registry(token)


def test_inventory_caps_at_eight_most_recent() -> None:
    reg = get_uri_registry("sess-inventory-cap")
    for i in range(12):
        reg.record(
            f"layer-{i:02d}",
            uri=f"s3://trid3nt-cache/cache/x/{i:02d}.tif",
            tool_name="fetch_dem",
        )
    token = activate_registry(reg)
    try:
        handles = ambient_layer_handle_inventory(limit=8)
        assert len(handles) == 8
        assert handles[0] == "layer-11"
        assert handles[-1] == "layer-04"
    finally:
        deactivate_registry(token)


def test_inventory_empty_outside_dispatch() -> None:
    assert ambient_layer_handle_inventory() == []


def test_minted_uri_handles_excluded_from_inventory() -> None:
    reg = get_uri_registry("sess-minted")
    reg.record(
        "uri:aa.tif",
        uri="gs://grace-2-hazard-prod-runs/x/aa.tif",
        tool_name="fetch_dem",
    )
    token = activate_registry(reg)
    try:
        assert ambient_layer_handle_inventory() == []
        err = _unknown_handle_error("bogus_token")
        assert "no layers have been produced" in str(err)
    finally:
        deactivate_registry(token)
