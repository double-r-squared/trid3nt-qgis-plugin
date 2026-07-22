"""Unit tests for the single LayerURI emission seam (job-0254, Decision 11).

Coverage:
  * Guardrail pass/block matrix -- the §1 leak fix promoted to an invariant,
    RELAXED for s3:// rasters by the TiTiler exit / QGIS-native swap (the
    plugin reads raw s3:// COGs via /vsicurl/):
      - raster + raw s3:// COG -> PASS (identity)  [the NEW publish shape]
      - raster + http(s) WMS   -> PASS (identity)
      - raster + raw gs://     -> DROP (return None) [no reachable face]
      - raster + file://       -> DROP (return None) [plugin cannot reach]
      - raster + empty uri     -> DROP (return None) [nothing to fetch]
      - vector + gs:// / s3:// (inline-GeoJSON path, job-0175) -> PASS (identity)
      - vector + http(s)       -> PASS (identity)
  * ``SIGNED_URLS`` dormant scaffold — default false; ``true`` logs a WARNING
    referencing Decision 11 and is otherwise byte-identical (no behavior change).
  * Byte-identity: a passed-through LayerURI is the SAME object (no copy / no
    field mutation), so envelope payloads are byte-identical when SIGNED_URLS
    is absent.
"""

from __future__ import annotations

import logging

import pytest
from trid3nt_contracts.execution import LayerURI

from trid3nt_server.layer_uri_emit import (
    SIGNED_URLS_ENV,
    emit_layer_uri,
    signed_urls_enabled,
)


def _layer(layer_type: str, uri: str, layer_id: str = "L1") -> LayerURI:
    return LayerURI(
        layer_id=layer_id,
        name="demo",
        layer_type=layer_type,  # type: ignore[arg-type]
        uri=uri,
        style_preset="preset",
    )


# --------------------------------------------------------------------------- #
# Guardrail pass/block matrix
# --------------------------------------------------------------------------- #


def test_raster_s3_cog_uri_passes_identity() -> None:
    """THE NEW CONTRACT (TiTiler exit / QGIS-native swap): a raster carrying a
    raw s3:// COG uri PASSES the seam unchanged -- publish_layer now returns the
    raw s3:// COG and the QGIS plugin reads it via /vsicurl/. This reverses the
    job-0290c browser-era s3 drop."""
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
    """A vector LayerURI carrying gs:// is the inline-GeoJSON path (job-0175):
    the emitter reads the uri server-side and ships inline GeoJSON; the browser
    never fetches gs://. The seam MUST NOT break this — it passes untouched."""
    layer = _layer("vector", "gs://bucket/alerts.fgb")
    out = emit_layer_uri(layer)
    assert out is layer
    assert out.uri == "gs://bucket/alerts.fgb"


def test_vector_s3_uri_passes_untouched() -> None:
    """A vector LayerURI carrying s3:// is the same inline-GeoJSON path -- the
    emitter reads the uri server-side; the seam passes it untouched."""
    layer = _layer("vector", "s3://bucket/runs/r1/alerts.fgb")
    assert emit_layer_uri(layer) is layer


def test_vector_https_uri_passes_identity() -> None:
    layer = _layer("vector", "https://host/alerts.geojson")
    assert emit_layer_uri(layer) is layer


def test_vsigs_and_local_raster_pass_through() -> None:
    """The guardrail targets only the raw ``gs://`` scheme (the leak shape).
    Other raster uri schemes (vsigs, local paths) are not the leak class and
    pass through — they are not produced on the client path today, but the
    seam must not over-block."""
    assert emit_layer_uri(_layer("raster", "/vsigs/bucket/x.tif")) is not None
    assert emit_layer_uri(_layer("raster", "/tmp/local.tif")) is not None


# --------------------------------------------------------------------------- #
# SIGNED_URLS dormant scaffold (Decision 11)
# --------------------------------------------------------------------------- #


def test_signed_urls_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SIGNED_URLS_ENV, raising=False)
    assert signed_urls_enabled() is False


@pytest.mark.parametrize("val", ["true", "True", "TRUE", "1", "yes", "YES"])
def test_signed_urls_truthy_values(
    monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    monkeypatch.setenv(SIGNED_URLS_ENV, val)
    assert signed_urls_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "", "  ", "off", "maybe"])
def test_signed_urls_falsy_values(
    monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    monkeypatch.setenv(SIGNED_URLS_ENV, val)
    assert signed_urls_enabled() is False


def test_signed_urls_true_is_byte_identical_noop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """SIGNED_URLS=true is DORMANT: it logs a WARNING referencing Decision 11
    but changes NO emission behavior — a passed-through layer is byte-identical
    to the flag-absent case (same object, same dumped payload)."""
    layer = _layer("raster", "https://qgis.run.app/wms?LAYERS=flood")

    monkeypatch.delenv(SIGNED_URLS_ENV, raising=False)
    out_absent = emit_layer_uri(layer)

    monkeypatch.setenv(SIGNED_URLS_ENV, "true")
    with caplog.at_level(logging.WARNING, logger="trid3nt_server.layer_uri_emit"):
        out_true = emit_layer_uri(layer)

    # Identical object and identical serialized payload (byte-identical wire).
    assert out_absent is layer
    assert out_true is layer
    assert out_absent.model_dump(mode="json") == out_true.model_dump(mode="json")

    # A WARNING was logged referencing Decision 11 (no-op signal).
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "Decision 11" in msgs
    assert SIGNED_URLS_ENV in msgs


def test_signed_urls_true_still_drops_raster_gs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guardrail still applies under SIGNED_URLS=true (dormant): a raw
    gs:// raster is still dropped, because no direct-fetch surface exists to
    sign for yet (Decision 11)."""
    monkeypatch.setenv(SIGNED_URLS_ENV, "true")
    assert emit_layer_uri(_layer("raster", "gs://b/x.tif")) is None


def test_warning_logged_on_drop(caplog: pytest.LogCaptureFixture) -> None:
    """The drop path logs a WARNING (so the audit/telemetry can see leaks were
    refused) — not silent."""
    with caplog.at_level(logging.WARNING, logger="trid3nt_server.layer_uri_emit"):
        emit_layer_uri(_layer("raster", "gs://b/flood.tif", layer_id="flood_9"))
    msgs = "\n".join(r.getMessage() for r in caplog.records)
    assert "DROPPING" in msgs
    assert "flood_9" in msgs
