"""publish_layer: the envelope carries the store uri, and only that.

One store, one scheme: the QGIS plugin opens the COG natively through GDAL
``/vsis3``, so a publish emits the ``s3://`` uri itself and never mints a second
face for the same layer. This suite pins that contract:

  - a raster publish returns the ``s3://`` COG URI VERBATIM - no ``/cog/tiles/``
    path, no ``{z}/{x}/{y}`` placeholders;
  - the RESOLVED STYLE (ramp + range + the .qml) is stashed keyed by that same
    uri, so the pipeline emitter's ``layer.uri`` lookup matches the envelope;
  - ``observe_published_layer`` registers the layer against that ONE uri;
  - a non-s3 raster URI raises the typed LAYER_URI_NOT_FOUND error.

No network I/O - ``_read_raster_bytes`` is patched to fail open so style
resolution lands on the declared row's fallback range.
"""

from __future__ import annotations

import pytest

from trid3nt_server.emission import publish as pl
from trid3nt_server.emission.publish import (
    PublishLayerError,
    pop_legend_for_uri,
    publish_layer,
)

MOD = pl

# A representative s3:// COG handle, and the row a flood-depth producer
# declares for it. A NAME is not a measurement: the row is what carries the
# ramp, never the filename.
S3_URI = "s3://trid3nt-runs/runs/ian/flood_depth_peak.tif"
FLOOD_STYLE = {"kind": "continuous", "ramp": "ylgnbu", "units": "m",
               "label": "Flood depth",
               "scale": {"policy": "fixed", "range": [0, 3], "transform": "linear"}}


@pytest.fixture(autouse=True)
def _no_bytes_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-open bytes read (no network)."""
    monkeypatch.setattr(MOD, "_read_raster_bytes", lambda uri: None)


def test_raster_publish_returns_the_store_uri() -> None:
    """The envelope uri slot gets the s3:// COG itself."""
    out = publish_layer(layer_uri=S3_URI, layer_id="flood-demo")
    assert out == S3_URI
    assert "/cog/tiles/" not in out
    assert "{z}/{x}/{y}" not in out


def test_flood_style_stashed_by_s3_uri() -> None:
    """The declared row is RESOLVED and stashed by the envelope s3 uri: the
    ramp, the range and the .qml the map loads."""
    out = publish_layer(
        layer_uri=S3_URI,
        layer_id="flood-demo",
        style=FLOOD_STYLE,
    )
    legend = pop_legend_for_uri(out)
    assert legend is not None
    assert legend.kind == "continuous"
    assert legend.colormap == "ylgnbu"
    assert legend.vmin == 0.0
    assert legend.vmax == 3.0
    assert legend.qml is not None


def test_observe_registers_the_one_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    """observe_published_layer records the s3 COG. A layer has one uri."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        "trid3nt_server.emission.publish.observe_published_layer",
        lambda *a, **k: calls.append((a, k)),
    )
    publish_layer(layer_uri=S3_URI, layer_id="flood-demo")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "flood-demo"
    assert kwargs.get("uri") == S3_URI


def test_non_s3_uri_is_typed_error() -> None:
    """A non-s3:// raster handle is still a typed (retryable) error."""
    with pytest.raises(PublishLayerError) as exc:
        publish_layer(layer_uri="gs://legacy/bucket/x.tif", layer_id="flood-demo")
    assert exc.value.error_code == "LAYER_URI_NOT_FOUND"
    assert exc.value.retryable is True
