"""Router coverage for fetch_groundwater_recharge (ADR 0297).

The first STAGED-DATASET fetcher: the served objects are COGs this repo built
from two published USGS CONUS recharge releases (Reitz et al. 2017, Wolock 2003)
via ``scripts/stage_groundwater_recharge.py``, and the spec names them by bucket
and key so the transport resolves the host from the active object-store endpoint.

These OFFLINE tests cover the spec identity + metadata flags, the staged-uri
resolution (including the no-real-AWS-fallback refusal and the staged-404
config-error split from a genuine EMPTY), the ``source`` enum -> object
mapping, the CONUS gate (the coverage limit an out-of-CONUS AOI is refused
with, plus the spec's own widened envelope), the NaN-nodata honesty gate that
an all-ocean window must trip, the per-source inland-water split (reitz_2017
0.0 vs wolock_2003 NaN), the payload estimate, and the retrieval corpus.
"""

from __future__ import annotations

import contextlib

import numpy as np
import pytest
import rasterio.transform as rtransform

from trid3nt_server.data.fetchers._router import router
from trid3nt_server.data.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.data.fetchers._router.executors import raster_cog
from trid3nt_server.data.fetchers._router.router import (
    synthesize_metadata,
    synthesize_payload_estimator,
)
from trid3nt_server.data.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.data.fetchers._router.transport import staged as _staged

#: Story County, Iowa -- the live-acceptance AOI.
_BBOX = [-93.70, 41.86, -93.20, 42.21]
_STAGED_PREFIX = "s3://trid3nt-cache/staged/groundwater_recharge/"


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_groundwater_recharge"]


class _FakeSrc:
    """Minimal windowed-COG stand-in over a float32 NaN-nodata staged grid."""

    def __init__(self, arr):
        self._arr = arr
        self.nodata = float("nan")
        self.height, self.width = arr.shape
        self.crs = "EPSG:4326"
        self.transform = rtransform.from_bounds(*_BBOX, self.width, self.height)

    def read(self, _band, window=None):
        r0 = int(getattr(window, "row_off", 0))
        c0 = int(getattr(window, "col_off", 0))
        h = int(getattr(window, "height", self.height))
        w = int(getattr(window, "width", self.width))
        return self._arr[r0:r0 + h, c0:c0 + w]

    def window_transform(self, window):
        return self.transform


def _patch_open(monkeypatch, arr, seen: dict | None = None):
    @contextlib.contextmanager
    def _fake_open(url):
        if seen is not None:
            seen["url"] = url
        yield _FakeSrc(arr)

    monkeypatch.setattr(
        "trid3nt_server.data.fetchers._router.transport.open_windowed_cog", _fake_open
    )
    # The staged bucket/key -> url resolution now happens (and refuses to fall
    # back to real AWS -- B2) BEFORE this open is reached, so every mocked-read
    # test needs a resolvable endpoint even though ``_fake_open`` never uses it.
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://minio.local:9000")


# --------------------------------------------------------------------------- #
# Spec identity + metadata flags.
# --------------------------------------------------------------------------- #


def test_spec_identity(spec):
    assert spec.name == "fetch_groundwater_recharge"
    assert spec.shape == "raster-cog"
    assert spec.error_code_prefix == "RECHARGE"
    assert spec.input_error_suffix == "INPUT_INVALID"
    assert spec.empty_error_suffix == "EMPTY"
    assert spec.supports_global_query is False
    assert spec.cache.ttl_class == "static-30d"
    assert spec.normalize.units == "mm/yr"
    assert spec.normalize.quantity == "groundwater_recharge"
    assert spec.ingest["access"] == "direct_window"
    assert spec.ingest["nodata_gate"] is True


def test_metadata_flags(spec):
    m = synthesize_metadata(spec)
    assert m.name == "fetch_groundwater_recharge"
    assert m.source_class == "groundwater_recharge"
    assert m.ttl_class == "static-30d"
    assert m.cacheable is True
    assert m.supports_global_query is False
    assert m.payload_mb_estimator_name == "estimate_payload_mb"


def test_payload_estimate_declared(spec):
    """The large-payload seam resolves a real per-area estimate, not a guess."""
    est = synthesize_payload_estimator(spec)
    story_county = est(bbox=_BBOX)
    conus = est(bbox=[-125.0, 24.0, -66.5, 50.0])
    assert 0.0 < story_county < 1.0          # a county window is trivially small
    assert conus > story_county              # and it scales with area
    assert spec.payload_estimate.model == "bbox_area"


def test_style_preset_resolves_in_the_qgis_registry(spec):
    """A preset absent from the registry silently renders a wrong colormap."""
    from trid3nt_server.data.publish_layer import publish_layer as pl

    assert pl._registry_style_params(spec.output.style_preset) is not None


def test_corpus_carries_natural_recharge_phrasings(spec):
    assert len(spec.corpus) >= 6
    joined = " ".join(spec.corpus).lower()
    assert "recharge" in joined
    assert any("modflow" in q.lower() for q in spec.corpus)


# --------------------------------------------------------------------------- #
# Staged-object resolution: bucket/key in the spec, host from the environment.
# --------------------------------------------------------------------------- #


def test_both_sources_point_at_staged_objects(spec):
    urls = spec.ingest["url_by_param"]["map"]
    assert set(urls) == {"reitz_2017", "wolock_2003"}
    assert all(u.startswith(_STAGED_PREFIX) for u in urls.values())
    assert spec.endpoints["data"].url == urls["reitz_2017"]


def test_staged_uri_resolves_against_the_configured_endpoint(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://minio.local:9000/")
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    assert _staged.staged_object_url("s3://buck/a/b.tif") == "http://minio.local:9000/buck/a/b.tif"


def test_staged_uri_raises_when_no_endpoint_override(monkeypatch):
    """No real-AWS fallback: this repo's AWS account is decommissioned, so an
    unset endpoint is always a deployment misconfiguration, never a valid
    target -- it must fail loud, not silently resolve a real bucket."""
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    with pytest.raises(_staged.StagedEndpointNotConfigured):
        _staged.staged_object_url("s3://buck/a/b.tif")


def test_staged_uri_without_a_key_is_refused():
    with pytest.raises(ValueError):
        _staged.staged_object_url("s3://bucketonly")


@pytest.mark.parametrize("source,fragment", [
    ("reitz_2017", "reitz2017-v1/recharge_total_2000_2013_mmyr.tif"),
    ("wolock_2003", "wolock2003-v1/recharge_bfi_runoff_mmyr.tif"),
])
def test_source_param_selects_the_staged_object(spec, monkeypatch, source, fragment):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://minio.local:9000")
    seen: dict = {}
    _patch_open(monkeypatch, np.full((4, 4), 150.0, dtype="float32"), seen)
    raster_cog._direct_window_to_array(spec, {"bbox": _BBOX, "source": source})
    assert seen["url"] == f"http://minio.local:9000/trid3nt-cache/staged/groundwater_recharge/{fragment}"


# --------------------------------------------------------------------------- #
# Coverage limit + honesty floor.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("label,bbox", [
    ("Oahu, Hawaii", [-158.3, 21.2, -157.6, 21.8]),
    ("Anchorage, Alaska", [-150.1, 61.0, -149.5, 61.4]),
    ("San Juan, Puerto Rico", [-66.2, 18.3, -65.9, 18.5]),
])
def test_outside_conus_is_refused_naming_the_coverage_limit(spec, label, bbox):
    """Both grids stop at the CONUS border; an AOI beyond it never gets a layer."""
    with pytest.raises(RouterInputError) as ei:
        router.route(spec, {"bbox": bbox})
    assert ei.value.error_code == "RECHARGE_INPUT_INVALID"
    assert "CONUS" in str(ei.value)


def test_all_nan_window_raises_empty_not_a_fabricated_layer(spec, monkeypatch):
    """A genuinely all-NaN window (fully off both grids' land extent, e.g. open
    ocean) reads as honest no-coverage. This is NOT what reitz_2017 does over
    the Great Lakes -- see the two tests below, which pin that split."""
    _patch_open(monkeypatch, np.full((4, 4), np.nan, dtype="float32"))
    with pytest.raises(RouterEmptyError) as ei:
        raster_cog._direct_window_to_array(spec, {"bbox": _BBOX, "source": "reitz_2017"})
    assert ei.value.error_code == "RECHARGE_EMPTY"


# --------------------------------------------------------------------------- #
# B1 follow-up: inland-water behavior differs by source (the corrected caveat).
# reitz_2017 stamps inland water 0.0 (finite); wolock_2003 stamps it NaN. Live-
# proven against the real staged Lake Michigan window (mean=0.0, all 144 pixels
# finite on reitz_2017; all-NaN on wolock_2003, raising RECHARGE_EMPTY).
# --------------------------------------------------------------------------- #


def test_lake_michigan_reitz2017_serves_zero_not_an_error(spec, monkeypatch):
    """reitz_2017 (the default source) encodes inland water as a FINITE 0.0, not
    a nodata sentinel -- an AOI entirely over a Great Lake serves a plausible
    all-zero raster, it does NOT raise RECHARGE_EMPTY."""
    _patch_open(monkeypatch, np.zeros((4, 4), dtype="float32"))
    out, _tf, _crs = raster_cog._direct_window_to_array(
        spec, {"bbox": _BBOX, "source": "reitz_2017"}
    )
    assert np.all(out == 0.0)
    assert np.isfinite(out).all()


def test_lake_michigan_wolock2003_raises_empty(spec, monkeypatch):
    """wolock_2003 encodes inland water as NaN -- the same AOI raises
    RECHARGE_EMPTY on this source, unlike reitz_2017."""
    _patch_open(monkeypatch, np.full((4, 4), np.nan, dtype="float32"))
    with pytest.raises(RouterEmptyError) as ei:
        raster_cog._direct_window_to_array(spec, {"bbox": _BBOX, "source": "wolock_2003"})
    assert ei.value.error_code == "RECHARGE_EMPTY"


def test_partially_valid_window_is_not_gated(spec, monkeypatch):
    arr = np.full((4, 4), np.nan, dtype="float32")
    arr[2, 2] = 167.0
    _patch_open(monkeypatch, arr)
    out, _tf, _crs = raster_cog._direct_window_to_array(
        spec, {"bbox": _BBOX, "source": "reitz_2017"}
    )
    assert np.nanmax(out) == pytest.approx(167.0)


def test_values_pass_through_unscaled(spec, monkeypatch):
    """The staged COG is already mm/yr; the read must not rescale it."""
    _patch_open(monkeypatch, np.full((4, 4), 166.7, dtype="float32"))
    out, _tf, _crs = raster_cog._direct_window_to_array(
        spec, {"bbox": _BBOX, "source": "reitz_2017"}
    )
    assert np.allclose(out, 166.7)


def test_caveats_state_the_conus_limit_and_the_source_disagreement(spec):
    joined = " ".join(spec.caveats).lower()
    assert "conus only" in joined
    assert "irrigation" in joined
    assert "independent" in joined


def test_caveats_state_the_true_inland_water_split(spec):
    """B1: the caveat must state the TRUTH (0.0 on reitz_2017, NaN on
    wolock_2003), not the false 'Great Lakes always raises EMPTY' claim."""
    joined = " ".join(spec.caveats).lower()
    assert "0.0" in joined and "nan" in joined
    assert "reitz_2017" in joined and "wolock_2003" in joined


# --------------------------------------------------------------------------- #
# B2 follow-up: a staged 404 is a config/upstream defect, never a coverage
# answer. Live-proven: an unset AWS_ENDPOINT_URL and a configured-but-wrong
# endpoint both raise RouterUpstreamError (RECHARGE_UPSTREAM_ERROR), never
# RouterEmptyError -- CONUS is fully covered by the staged objects, so a 404
# here is always a deployment defect.
# --------------------------------------------------------------------------- #


def test_missing_endpoint_raises_typed_config_error_not_empty(spec, monkeypatch):
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog._direct_window_to_array(spec, {"bbox": _BBOX, "source": "reitz_2017"})
    assert ei.value.error_code == "RECHARGE_UPSTREAM_ERROR"
    assert ei.value.retryable is True
    assert "STAGED_OBJECT_UNAVAILABLE" in str(ei.value)


def test_configured_endpoint_with_404_raises_typed_config_error_not_empty(spec, monkeypatch):
    """The endpoint resolves, but the object 404s (wrong bucket/deployment) --
    still a config/upstream defect, not a coverage answer, because a staged
    object is known to exist for its declared coverage."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://minio.local:9000")
    import contextlib

    from trid3nt_server.data.fetchers._router.transport.errors import TransportNotFound

    @contextlib.contextmanager
    def _fake_open_404(url):
        raise TransportNotFound("simulated 404", status=404)
        yield  # pragma: no cover

    monkeypatch.setattr(
        "trid3nt_server.data.fetchers._router.transport.open_windowed_cog", _fake_open_404
    )
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog._direct_window_to_array(spec, {"bbox": _BBOX, "source": "reitz_2017"})
    assert ei.value.error_code == "RECHARGE_UPSTREAM_ERROR"
    assert "STAGED_OBJECT_UNAVAILABLE" in str(ei.value)


# --------------------------------------------------------------------------- #
# conus_bbox non-blocker: the spec's own staged-grid envelope, not gridmet's.
# --------------------------------------------------------------------------- #


def test_key_west_is_inside_the_specs_own_conus_envelope(spec):
    """Key West (24.55N) is south of the generic gridmet gate (25.05) but north
    of the Reitz grid's real southern edge (24.0625) -- the spec's own
    conus_bbox must not false-refuse it."""
    key_west_bbox = [-81.85, 24.53, -81.75, 24.60]
    router._apply_gates(spec, {"bbox": key_west_bbox})  # must not raise


def test_hawaii_alaska_puerto_rico_still_refused_under_the_spec_envelope(spec):
    """The widened envelope must still exclude AK/HI/PR."""
    for bbox in (
        [-158.3, 21.2, -157.6, 21.8],   # Oahu
        [-150.1, 61.0, -149.5, 61.4],   # Anchorage
        [-66.2, 18.3, -65.9, 18.5],     # San Juan
    ):
        with pytest.raises(RouterInputError) as ei:
            router._apply_gates(spec, {"bbox": bbox})
        assert ei.value.error_code == "RECHARGE_INPUT_INVALID"
