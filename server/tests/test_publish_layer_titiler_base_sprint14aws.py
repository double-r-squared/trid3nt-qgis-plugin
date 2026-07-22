"""publish_layer raw-s3 envelope pins (TiTiler exit; formerly tile-base tests).

The QGIS plugin is the ONLY client and loads COGs DIRECTLY via GDAL
``/vsicurl/`` (the same MinIO s3->http translation it already uses for
FlatGeobuf vectors), so ``publish_layer`` no longer mints TiTiler XYZ tile
TEMPLATES and no longer reads ``GRACE2_TILE_SERVER_BASE``. This suite (which
previously pinned the tile-base derivation) now pins the swapped contract:

  - a raster publish returns the raw ``s3://`` COG URI VERBATIM - no
    ``/cog/tiles/`` path, no ``{z}/{x}/{y}`` placeholders, regardless of
    whether the legacy env var is set (the env read is GONE);
  - the data-driven LEGEND (colormap name + vmin/vmax) is stashed keyed by
    the returned ``s3://`` uri, so the pipeline emitter's ``layer.uri``
    lookup still matches the envelope uri;
  - ``observe_published_layer`` registers the s3 COG as the DATA uri with
    NO separate display face (the raw COG IS the envelope uri);
  - LEGACY republish: an old persisted case's ``/cog/tiles/...?url=<cog>``
    template handed back to publish_layer is UNWRAPPED to its embedded s3
    COG (the ``export_case_to_qgis._unwrap_tile_template`` trick) and flows
    through the normal raster path -> the NEW raw-s3 envelope shape; a
    template with no recoverable COG is returned verbatim (degraded);
  - a non-s3 raster URI still raises the typed LAYER_URI_NOT_FOUND error.

No TiTiler / network I/O - ``_read_raster_bytes`` is patched to fail open so
style resolution lands on the typed flood registry entry.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from grace2_agent.tools import publish_layer as pl
from grace2_agent.tools.publish_layer import (
    PublishLayerError,
    pop_legend_for_uri,
    publish_layer,
)

MOD = pl

# A representative s3:// COG handle (flood-family so the F51 no-preset path
# infers continuous_flood_depth and resolves the typed registry ramp).
S3_URI = "s3://trid3nt-runs/runs/ian/flood_depth_peak.tif"
ENCODED = quote(S3_URI, safe="")

# A legacy TiTiler tile TEMPLATE wrapping S3_URI (the shape old persisted
# cases still carry in their envelopes / registries).
LEGACY_TEMPLATE = (
    "https://d123abc.cloudfront.net/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    f"?url={ENCODED}&rescale=0,3&colormap_name=ylgnbu"
)


@pytest.fixture(autouse=True)
def _s3_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the AWS/s3 publish branch + a fail-open bytes read (no network)."""
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: None)


def test_raster_publish_returns_raw_s3_uri() -> None:
    """The envelope uri slot gets the raw s3:// COG - no template mint."""
    out = publish_layer(layer_uri=S3_URI, layer_id="flood-demo")
    assert out == S3_URI
    assert "/cog/tiles/" not in out
    assert "{z}/{x}/{y}" not in out


def test_tile_server_base_env_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting the legacy env var changes NOTHING (the env read is removed)."""
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", "https://d123abc.cloudfront.net")
    out = publish_layer(layer_uri=S3_URI, layer_id="flood-demo")
    assert out == S3_URI
    assert "cloudfront" not in out


def test_unset_env_no_longer_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old RASTER_PUBLISH_UNAVAILABLE gate is gone - unset env publishes."""
    monkeypatch.delenv("GRACE2_TILE_SERVER_BASE", raising=False)
    out = publish_layer(layer_uri=S3_URI, layer_id="flood-demo")
    assert out == S3_URI


def test_flood_legend_stashed_by_s3_uri() -> None:
    """The flood ramp rides the LEGEND (keyed by the envelope s3 uri), not a
    tile-URL query string: colormap NAME + vmin/vmax recoverable for the
    plugin renderer."""
    out = publish_layer(
        layer_uri=S3_URI,
        layer_id="flood-demo",
        style_preset="continuous_flood_depth",
    )
    legend = pop_legend_for_uri(out)
    assert legend is not None
    assert legend.kind == "continuous"
    assert legend.colormap == "ylgnbu"
    assert legend.vmin == 0.0
    assert legend.vmax == 3.0


def test_observe_registers_data_uri_without_display_face(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """observe_published_layer records the s3 COG as the DATA uri; there is no
    separate wms/display face any more (the raw COG IS the envelope uri)."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        "grace2_agent.tools.publish_layer.observe_published_layer",
        lambda *a, **k: calls.append((a, k)),
    )
    publish_layer(layer_uri=S3_URI, layer_id="flood-demo")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "flood-demo"
    assert kwargs.get("gcs_uri") == S3_URI
    assert kwargs.get("wms_url") is None


def test_legacy_template_input_unwraps_to_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old case's tile-template handle republishes as the NEW raw-s3 shape:
    the embedded url= COG is unwrapped and flows through the raster path."""
    out = publish_layer(layer_uri=LEGACY_TEMPLATE, layer_id="flood-demo")
    assert out == S3_URI
    # ...and the fresh legend is stashed under the NEW s3 envelope uri.
    legend = pop_legend_for_uri(out)
    assert legend is not None and legend.colormap == "ylgnbu"


def test_legacy_http_template_also_unwraps() -> None:
    """The unwrap matches http (IP:port origin) templates too."""
    legacy_http = (
        "http://54.185.114.233:8080/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
        f"?url={ENCODED}"
    )
    out = publish_layer(layer_uri=legacy_http, layer_id="flood-demo")
    assert out == S3_URI


def test_legacy_template_without_cog_returns_verbatim() -> None:
    """A template with NO recoverable url= COG is returned unchanged (degraded
    legacy behavior - the plugin unwraps what it can on its side)."""
    foreign = (
        "https://d123abc.cloudfront.net/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    )
    out = publish_layer(layer_uri=foreign, layer_id="flood-demo")
    assert out == foreign


def test_non_s3_uri_is_typed_error() -> None:
    """A non-s3:// raster handle is still a typed (retryable) error."""
    with pytest.raises(PublishLayerError) as exc:
        publish_layer(layer_uri="gs://legacy/bucket/x.tif", layer_id="flood-demo")
    assert exc.value.error_code == "LAYER_URI_NOT_FOUND"
    assert exc.value.retryable is True
