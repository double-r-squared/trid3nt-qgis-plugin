"""Unit tests for the ``fetch_nexrad_reflectivity`` atomic tool (job-0102).

Coverage (≥4 unit + 1 live, env-guarded):

- Tool registered in TOOL_REGISTRY with the expected uncacheable metadata.
- Default product n0r returns a LayerURI pointing at the Iowa Mesonet WMS.
- product='n0q' and product='vil' produce distinct LayerURIs (different URIs +
  different layer_ids + different units).
- bbox=None yields a CONUS-scoped LayerURI (no BBOX hint, layer_id ends -conus).
- bbox=(-82,26,-81,27) yields a bbox-scoped LayerURI (BBOX present in URL).
- Unknown product raises ``NexradProductError`` (typed FR-AS-11 error).
- Malformed bbox shapes raise ``NexradBboxError``.
- Geographic-correctness gate (codified job-0086 lesson): when bbox is supplied,
  the LayerURI carries the EXACT bbox tuple AND the URL query string encodes
  the same four numbers in the documented (min_lon,min_lat,max_lon,max_lat)
  order — so a sign-flip / axis-swap bug surfaces immediately, not on-screen.
- Live (env TRID3NT_TEST_LIVE_NEXRAD=1): HEAD the n0r endpoint; expect 200 OK
  or a benign HTTP response (some WMS endpoints prefer GetCapabilities over
  HEAD; we accept <500 + body containing a WMS marker as proof-of-reach).
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_nexrad_reflectivity import (
    NexradBboxError,
    NexradProductError,
    _build_wms_url,
    fetch_nexrad_reflectivity,
)

# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_LIVE_NEXRAD = os.environ.get("TRID3NT_TEST_LIVE_NEXRAD") == "1"

# Fort Myers bbox — same convention used by sibling tools' tests. Algebraic
# identity for the geographic-correctness gate: min_lon=-82, min_lat=26.
_FORT_MYERS_BBOX = (-82.0, 26.0, -81.0, 27.0)

_IOWA_MESONET_HOST = "mesonet.agron.iastate.edu"


# ---------------------------------------------------------------------------
# Registration / metadata.
# ---------------------------------------------------------------------------


def test_tool_registered() -> None:
    """Importing the tools package registered fetch_nexrad_reflectivity."""
    assert "fetch_nexrad_reflectivity" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_nexrad_reflectivity"]
    # Uncacheable-by-construction: WMS URL passthrough; live radar pixels are
    # dynamic. See module docstring OQ-0102-CACHEABLE-FLAG-CONTRADICTION.
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.source_class is None
    assert entry.metadata.name == "fetch_nexrad_reflectivity"


# ---------------------------------------------------------------------------
# Happy-path: each product produces a correctly-shaped LayerURI.
# ---------------------------------------------------------------------------


def test_default_product_n0r_returns_layeruri() -> None:
    """Default product (n0r) returns a LayerURI with the right shape + WMS URL."""
    layer = fetch_nexrad_reflectivity()  # bbox=None, product='n0r'
    assert layer.layer_type == "raster"
    assert layer.role == "context"
    assert layer.units == "dBZ"
    assert "n0r.cgi" in layer.uri
    assert _IOWA_MESONET_HOST in layer.uri
    assert "Composite Reflectivity" in layer.name
    assert "Iowa State Mesonet" in layer.name
    assert layer.bbox is None
    assert layer.layer_id == "nexrad-n0r-conus"


def test_product_n0q_produces_distinct_layeruri() -> None:
    """product='n0q' (base reflectivity) → different URL + name + units."""
    n0r = fetch_nexrad_reflectivity(product="n0r")
    n0q = fetch_nexrad_reflectivity(product="n0q")
    assert n0r.uri != n0q.uri
    assert "n0q.cgi" in n0q.uri
    assert "Base Reflectivity" in n0q.name
    assert n0q.units == "dBZ"
    assert n0q.layer_id == "nexrad-n0q-conus"
    assert n0q.style_preset == "nexrad_n0q"


def test_product_vil_produces_distinct_layeruri() -> None:
    """product='vil' (VIL) → distinct URL + kg/m² units."""
    vil = fetch_nexrad_reflectivity(product="vil")
    assert "vil.cgi" in vil.uri
    assert "Vertically Integrated Liquid" in vil.name
    assert vil.units == "kg/m^2"
    assert vil.layer_id == "nexrad-vil-conus"
    assert vil.style_preset == "nexrad_vil"


# ---------------------------------------------------------------------------
# bbox handling.
# ---------------------------------------------------------------------------


def test_bbox_none_returns_conus_layeruri() -> None:
    """bbox=None: LayerURI carries no bbox; URL has no BBOX query."""
    layer = fetch_nexrad_reflectivity(bbox=None, product="n0r")
    assert layer.bbox is None
    parsed = urlparse(layer.uri)
    qs = parse_qs(parsed.query)
    assert "BBOX" not in qs


def test_bbox_supplied_returns_scoped_layeruri() -> None:
    """bbox supplied: LayerURI carries the bbox AND URL encodes BBOX in order.

    Geographic-correctness gate (codified job-0086 lesson): the URL query
    string MUST encode (min_lon, min_lat, max_lon, max_lat) verbatim — a
    sign-flip / axis-swap would scope the radar overlay to the wrong place.
    """
    bbox = _FORT_MYERS_BBOX
    layer = fetch_nexrad_reflectivity(bbox=bbox, product="n0r")
    assert layer.bbox == bbox

    parsed = urlparse(layer.uri)
    qs = parse_qs(parsed.query)
    assert "BBOX" in qs, "BBOX must be in URL query string when bbox supplied"
    bbox_str = qs["BBOX"][0]
    parts = [float(p) for p in bbox_str.split(",")]
    assert parts == [-82.0, 26.0, -81.0, 27.0], (
        f"BBOX order must be (min_lon,min_lat,max_lon,max_lat); got {bbox_str!r}"
    )

    # layer_id includes the bbox corners for client-side uniqueness.
    assert "nexrad-n0r-" in layer.layer_id
    assert "-82.0000" in layer.layer_id
    assert "26.0000" in layer.layer_id


# ---------------------------------------------------------------------------
# Typed errors (FR-AS-11).
# ---------------------------------------------------------------------------


def test_unknown_product_raises_typed_error() -> None:
    """Unknown product raises NexradProductError with retryable=False."""
    with pytest.raises(NexradProductError) as excinfo:
        fetch_nexrad_reflectivity(product="bogus")  # type: ignore[arg-type]
    assert excinfo.value.error_code == "NEXRAD_PRODUCT_INVALID"
    assert excinfo.value.retryable is False


def test_bbox_wrong_arity_raises_typed_error() -> None:
    with pytest.raises(NexradBboxError) as excinfo:
        fetch_nexrad_reflectivity(bbox=(-82.0, 26.0, -81.0))  # type: ignore[arg-type]
    assert excinfo.value.error_code == "NEXRAD_BBOX_INVALID"


def test_bbox_non_finite_raises_typed_error() -> None:
    with pytest.raises(NexradBboxError):
        fetch_nexrad_reflectivity(bbox=(float("nan"), 26.0, -81.0, 27.0))


def test_bbox_lon_out_of_range_raises_typed_error() -> None:
    with pytest.raises(NexradBboxError):
        fetch_nexrad_reflectivity(bbox=(-181.0, 26.0, -81.0, 27.0))


def test_bbox_degenerate_raises_typed_error() -> None:
    with pytest.raises(NexradBboxError):
        # min equals max on the longitude axis
        fetch_nexrad_reflectivity(bbox=(-82.0, 26.0, -82.0, 27.0))


def test_bbox_inverted_raises_typed_error() -> None:
    with pytest.raises(NexradBboxError):
        # min > max on the latitude axis
        fetch_nexrad_reflectivity(bbox=(-82.0, 27.0, -81.0, 26.0))


# ---------------------------------------------------------------------------
# URL builder direct.
# ---------------------------------------------------------------------------


def test_build_wms_url_includes_product_and_bbox() -> None:
    url = _build_wms_url("n0q", (-82.0, 26.0, -81.0, 27.0))
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == _IOWA_MESONET_HOST
    assert parsed.path.endswith("/n0q.cgi")
    qs = parse_qs(parsed.query)
    assert qs.get("BBOX") == ["-82.0,26.0,-81.0,27.0"]


def test_build_wms_url_unknown_product_raises() -> None:
    with pytest.raises(NexradProductError):
        _build_wms_url("xyz", None)


# ---------------------------------------------------------------------------
# Live verification (env-guarded). Asserts the Iowa Mesonet WMS endpoint is
# reachable — geographic-correctness gate for a service-URL-passthrough tool:
# if the service is reachable AND the URL we composed is the documented
# endpoint, then a bbox-scoped GetMap against it will scope to that bbox.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_NEXRAD, reason="TRID3NT_TEST_LIVE_NEXRAD!=1")
def test_live_nexrad_endpoint_reachable() -> None:
    """HEAD/GET the n0r WMS endpoint and confirm it responds with a WMS body.

    We accept any non-5xx response with a WMS-like body marker (a GetCapabilities
    request returns XML; a bare hit of the .cgi may also return capabilities).
    The point is that the URL we'd hand the client is actually live.
    """
    import urllib.request

    layer = fetch_nexrad_reflectivity(product="n0r")
    base_url = layer.uri.split("?")[0]
    # Ask for GetCapabilities — every conformant WMS responds, and the response
    # is small enough for a fast live check.
    cap_url = f"{base_url}?SERVICE=WMS&REQUEST=GetCapabilities"
    req = urllib.request.Request(
        cap_url,
        headers={"User-Agent": "trid3nt/0.1 (live test fetch_nexrad_reflectivity)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — trusted URL
        status = resp.status
        body_head = resp.read(4096)
    assert status < 500, f"HTTP {status} from {cap_url!r}"
    body_text = body_head.decode("utf-8", errors="replace").lower()
    # GetCapabilities responses identify themselves as WMS in the XML root /
    # service section. Accept either of two common identifiers.
    assert ("wms_capabilities" in body_text) or ("<service>" in body_text) or (
        "wms" in body_text
    ), f"response body does not look WMS-like: {body_head[:200]!r}"
