"""Unit tests for the single LayerURI emission seam.

The guardrail matrix: a raster passes on a store COG or an http face and DROPS on
a scheme the client cannot reach or an empty uri; a vector passes on any of them,
since it rides the inline-geometry path. A passed-through layer is the SAME
object - no copy and no field mutation - so envelope payloads stay byte-identical."""

from __future__ import annotations

import logging

import pytest
from trid3nt_contracts.execution import LayerURI

from trid3nt_server.render.layer_uri_emit import emit_layer_uri


def _layer(layer_type: str, uri: str, layer_id: str = "L1") -> LayerURI:
    return LayerURI(
        layer_id=layer_id,
        name="demo",
        layer_type=layer_type,  # type: ignore[arg-type]
        uri=uri,
    )




def test_raster_s3_cog_uri_passes_identity() -> None:
    """A raster carrying a raw store COG uri PASSES the seam unchanged.

    The publish returns that uri and the plugin reads it directly, so the seam has no
    reason to drop it."""
    layer = _layer("raster", "s3://bucket/runs/r1/flood_depth_peak.tif")
    out = emit_layer_uri(layer)
    assert out is layer  # identity -- no copy, no mutation
    assert out.uri == "s3://bucket/runs/r1/flood_depth_peak.tif"


def test_raster_gs_uri_is_dropped() -> None:
    """A renderable raster carrying a raw gs:// uri is dropped (return None).

    This is exactly the publish-failure degraded path -- no face on this stack
    can fetch gs://, so emitting it only paints a broken layer row."""
    layer = _layer("raster", "gs://bucket/flood_depth_peak.tif")
    assert emit_layer_uri(layer) is None


def test_raster_file_scheme_uri_is_dropped() -> None:
    """A raster carrying a file:// uri is dropped -- a local path the plugin
    cannot be assumed to reach is not a deliverable layer face."""
    assert emit_layer_uri(_layer("raster", "file:///tmp/frame.tif")) is None


def test_raster_empty_uri_is_dropped() -> None:
    """A raster with an EMPTY uri is dropped -- nothing to fetch, never a row."""
    assert emit_layer_uri(_layer("raster", "")) is None


def test_raster_wms_http_url_passes_identity() -> None:
    """A raster carrying a QGIS WMS http(s) URL passes through UNCHANGED."""
    layer = _layer("raster", "https://qgis.run.app/wms?LAYERS=flood")
    out = emit_layer_uri(layer)
    assert out is layer  # identity — no copy, no mutation


def test_raster_http_url_passes_identity() -> None:
    layer = _layer("raster", "http://qgis.internal/wms?LAYERS=flood")
    assert emit_layer_uri(layer) is layer


def test_vector_gs_uri_passes_untouched_job0175() -> None:
    """A vector LayerURI carrying gs:// is the inline-GeoJSON path:
    the emitter reads the uri server-side and ships inline GeoJSON; the browser
    never fetches gs://. The seam MUST NOT break this -- the uri is untouched."""
    out = emit_layer_uri(_layer("vector", "gs://bucket/alerts.fgb"))
    assert out is not None
    assert out.uri == "gs://bucket/alerts.fgb"


def test_vector_s3_uri_passes_untouched() -> None:
    """A vector LayerURI carrying s3:// is the same inline-GeoJSON path -- the
    emitter reads the uri server-side; the seam passes the uri untouched."""
    out = emit_layer_uri(_layer("vector", "s3://bucket/runs/r1/alerts.fgb"))
    assert out is not None
    assert out.uri == "s3://bucket/runs/r1/alerts.fgb"


def test_vector_https_uri_passes_identity() -> None:
    out = emit_layer_uri(_layer("vector", "https://host/alerts.geojson"))
    assert out is not None
    assert out.uri == "https://host/alerts.geojson"


def test_a_vector_declaring_no_row_takes_its_kinds_default_not_the_rasters() -> None:
    """The bare default is per KIND, and a vector's kind is not a raster's: a
    row that named nothing must never resolve into a band read of a file that
    has no bands."""
    out = emit_layer_uri(_layer("vector", "s3://bucket/runs/r1/alerts.fgb"))
    assert out.legend is not None
    assert out.legend.kind == "reference"


def test_vsigs_and_local_raster_pass_through() -> None:
    """The guardrail targets only the one unreachable scheme.

    Other raster schemes are not the leak class and pass through: the seam must not
    over-block."""
    assert emit_layer_uri(_layer("raster", "/vsigs/bucket/x.tif")) is not None
    assert emit_layer_uri(_layer("raster", "/tmp/local.tif")) is not None


def test_warning_logged_on_drop(caplog: pytest.LogCaptureFixture) -> None:
    """The drop path logs a WARNING (so the audit/telemetry can see leaks were
    refused) — not silent."""
    with caplog.at_level(logging.WARNING, logger="trid3nt_server.render.layer_uri_emit"):
        emit_layer_uri(_layer("raster", "gs://b/flood.tif", layer_id="flood_9"))
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "DROPPING" in msgs
    assert "flood_9" in msgs
