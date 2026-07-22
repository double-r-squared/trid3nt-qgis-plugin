"""publish_layer TiTiler tile-base derivation tests (sprint-14-aws CloudFront).

The AWS deployment publishes rasters as TiTiler XYZ tile TEMPLATES, baking the
full tile URL into the layer handle using ``GRACE2_TILE_SERVER_BASE``. This
suite pins that the SAME seam works for BOTH:

  - today's value  ``http://54.185.114.233:8080`` (IP:port origin), and
  - the post-cutover value ``https://<cf-domain>`` (path-less https origin),

so flipping the env to the CloudFront domain (an orchestrator deploy step, NOT
done here) yields an https tile template with zero code change. Also pins:

  - the ``/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=...`` path is appended
    correctly for both (single ``/cog/`` join, no double slash), and the
    ``s3://`` URI is percent-encoded into ``?url=``;
  - the IDEMPOTENT republish guard (publish_layer.py) still short-circuits an
    already-resolved ``https`` ``/cog/tiles/`` template (returns it verbatim);
  - a trailing slash on the base is tolerated (``rstrip('/')``);
  - UNSET base on an s3 deploy fails fast + honest (RASTER_PUBLISH_UNAVAILABLE),
    proving the default stays OFF until the orchestrator sets the env.

These exercise ONLY the ``storage_scheme()=='s3'`` branch, which is the first
statement in ``publish_layer`` — no Cloud Run / GCS / TiTiler network I/O.
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from grace2_agent.tools.publish_layer import PublishLayerError, publish_layer

# A representative s3:// COG handle. quote(..., safe='') is what the tool uses,
# so the expected ?url= value is computed the same way the implementation does.
S3_URI = "s3://grace2-hazard-runs-226996537797/runs/ian/flood_depth_peak.tif"
ENCODED = quote(S3_URI, safe="")


@pytest.fixture(autouse=True)
def _s3_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the AWS/s3 publish branch for every test in this module."""
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")


# The S3_URI handle (``.../flood_depth_peak.tif``) infers the
# ``continuous_flood_depth`` family in the F51 no-preset path, so the no-preset
# template now carries the flood ramp suffix (style_params is NEVER empty for a
# continuous raster). These base-derivation tests therefore assert on the
# percent-encoded ``?url=`` PREFIX (the base-join contract) rather than an exact
# tail. ``_read_raster_bytes`` for the fake S3_URI returns None here (the key
# does not exist), so the palette probe + band-stats are skipped and the typed
# flood registry entry is what resolves.
_FLOOD_SUFFIX = "&rescale=0,3&colormap_name=ylgnbu"


def test_http_ip_port_base_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Today's http IP:port base yields the legacy http tile template."""
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", "http://54.185.114.233:8080")
    template = publish_layer(layer_uri=S3_URI, layer_id="flood-demo")
    assert template == (
        f"http://54.185.114.233:8080/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
        f"?url={ENCODED}{_FLOOD_SUFFIX}"
    )
    # No double slash where the base meets the /cog/ path.
    assert "8080//cog" not in template
    assert template.startswith("http://")


def test_https_cloudfront_base_after_cutover(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path-less https CloudFront base yields an https tile template.

    Domain is illustrative — NOT hardcoded into the tool; it comes purely from
    the env the orchestrator sets at deploy time.
    """
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", "https://d123abc.cloudfront.net")
    template = publish_layer(layer_uri=S3_URI, layer_id="flood-demo")
    assert template == (
        f"https://d123abc.cloudfront.net/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
        f"?url={ENCODED}{_FLOOD_SUFFIX}"
    )
    assert template.startswith("https://")
    assert "net//cog" not in template
    # The percent-encoded s3 uri rides in ?url= so TiTiler reads the COG.
    assert f"?url={ENCODED}" in template


def test_https_base_with_trailing_slash_is_tolerated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing slash on the base must not produce a double slash."""
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", "https://d123abc.cloudfront.net/")
    template = publish_layer(layer_uri=S3_URI, layer_id="flood-demo")
    assert template == (
        f"https://d123abc.cloudfront.net/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
        f"?url={ENCODED}{_FLOOD_SUFFIX}"
    )
    assert "net//cog" not in template


def test_flood_style_preset_appends_rescale_and_colormap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """continuous_flood_depth adds the blue-ramp render params on the https base."""
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", "https://d123abc.cloudfront.net")
    template = publish_layer(
        layer_uri=S3_URI,
        layer_id="flood-demo",
        style_preset="continuous_flood_depth",
    )
    assert template.startswith(
        "https://d123abc.cloudfront.net/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    )
    assert template.endswith(f"?url={ENCODED}&rescale=0,3&colormap_name=ylgnbu")


def test_idempotent_guard_returns_https_template_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-resolved https /cog/tiles/ template is returned unchanged.

    The composer-published-then-LLM-republished path: layer_uri is ALREADY the
    full https tile template. The guard must short-circuit (no re-encode, no
    error) so the emission wrap-site announces the layer.
    """
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", "https://d123abc.cloudfront.net")
    already = (
        f"https://d123abc.cloudfront.net/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
        f"?url={ENCODED}&rescale=0,3&colormap_name=ylgnbu"
    )
    out = publish_layer(layer_uri=already, layer_id="flood-demo")
    assert out == already


def test_idempotent_guard_also_matches_http_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard matches BOTH http and https /cog/tiles/ templates (legacy)."""
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", "https://d123abc.cloudfront.net")
    already = (
        "http://54.185.114.233:8080/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
        f"?url={ENCODED}"
    )
    out = publish_layer(layer_uri=already, layer_id="flood-demo")
    assert out == already


def test_unset_base_on_s3_deploy_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the seam UNSET, the s3 publish path raises typed + terminal.

    This is the OFF-by-default proof: nothing publishes an https template until
    the orchestrator sets GRACE2_TILE_SERVER_BASE.
    """
    monkeypatch.delenv("GRACE2_TILE_SERVER_BASE", raising=False)
    with pytest.raises(PublishLayerError) as exc:
        publish_layer(layer_uri=S3_URI, layer_id="flood-demo")
    assert exc.value.error_code == "RASTER_PUBLISH_UNAVAILABLE"
    assert exc.value.retryable is False


def test_non_s3_uri_on_s3_deploy_is_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-s3:// handle on the s3 branch is a typed (retryable) error."""
    monkeypatch.setenv("GRACE2_TILE_SERVER_BASE", "https://d123abc.cloudfront.net")
    with pytest.raises(PublishLayerError) as exc:
        publish_layer(layer_uri="gs://legacy/bucket/x.tif", layer_id="flood-demo")
    assert exc.value.error_code == "LAYER_URI_NOT_FOUND"
