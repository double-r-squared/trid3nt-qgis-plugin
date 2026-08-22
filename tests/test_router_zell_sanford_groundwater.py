"""Router coverage for fetch_water_table_depth + fetch_aquifer_thickness +
fetch_aquifer_transmissivity (ADR 0298).

Three staged-dataset fetchers over the Zell & Sanford 2020 CONUS surficial
groundwater release, built by ``scripts/stage_zell_sanford_groundwater.py``.
Depth to water and transmissivity are the published rasters; saturated
thickness is DERIVED as ``b = T / K`` from the release's own transmissivity
and hydraulic conductivity. Transmissivity was built and validated in the
same landing but parked (ADR 0298 Decision 7: ``NormalizeSpec.quantity`` is a
single static stamp, so it could not ride ``fetch_aquifer_thickness`` without
mislabelling a m2/day layer as a saturated thickness); NATE ruled REGISTER,
so it now has its own spec.

These OFFLINE tests cover all three specs' identity and metadata, the
staged-uri resolution (including the staged-404 / no-endpoint config-error
split from a genuine EMPTY), the coverage envelope read off the real staged
grid, the honest encodings the spec CLAIMS -- negative depths preserved rather
than clamped, off domain reading EMPTY -- the payload estimate, and the
retrieval corpora.

The live values these pin came from the staged objects: Story County IA reads a
median depth of 5.02 m and a median thickness of 93.09 m; Maricopa County AZ
(bbox [-113.3350468, 32.5049739, -111.0399049, 34.0481432]) reads a median
depth of 43.939 m and a median thickness of 71.523 m; Oahu, Anchorage and San
Juan refuse; mid-Lake-Michigan and Key West are inside the envelope but off the
model's active domain and read EMPTY. Transmissivity's own live-staging
validation (``--step build --dataset transmissivity``) reproduced the paper's
west/east contrast exactly: CONUS median west of 100W 14.05 m2/day vs east
87.94 m2/day.

(Corrected 2026-08-21: an earlier draft of this docstring claimed 9.34 m /
135.97 m for Maricopa County with no recorded bbox -- unreproducible from any
discoverable county bbox. Re-run live against the county bbox above; see the
ADR 0298 correction note.)
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
_STAGED_PREFIX = "s3://trid3nt-cache/staged/zell_sanford_groundwater/"

#: All three specs are the same shape over the same grid, so every structural
#: test runs against all three.
_NAMES = [
    "fetch_water_table_depth", "fetch_aquifer_thickness",
    "fetch_aquifer_transmissivity",
]
_PREFIX = {
    "fetch_water_table_depth": "WATER_TABLE",
    "fetch_aquifer_thickness": "AQUIFER_THICKNESS",
    "fetch_aquifer_transmissivity": "AQUIFER_TRANSMISSIVITY",
}


@pytest.fixture(scope="module")
def specs():
    tree = compose_specs_from_tree()
    return {n: tree[n] for n in _NAMES}


@pytest.fixture(params=_NAMES)
def spec(request, specs):
    return specs[request.param]


class _FakeSrc:
    """Minimal windowed-COG stand-in over a float32 NaN-nodata staged grid."""

    def __init__(self, arr, bbox=None):
        self._arr = arr
        self.nodata = float("nan")
        self.height, self.width = arr.shape
        self.crs = "EPSG:4326"
        self.transform = rtransform.from_bounds(
            *(bbox or _BBOX), self.width, self.height
        )

    def read(self, _band, window=None):
        r0 = int(getattr(window, "row_off", 0))
        c0 = int(getattr(window, "col_off", 0))
        h = int(getattr(window, "height", self.height))
        w = int(getattr(window, "width", self.width))
        return self._arr[r0:r0 + h, c0:c0 + w]

    def window_transform(self, window):
        return self.transform


def _patch_open(monkeypatch, arr, seen: dict | None = None, bbox=None):
    @contextlib.contextmanager
    def _fake_open(url):
        if seen is not None:
            seen["url"] = url
        yield _FakeSrc(arr, bbox)

    monkeypatch.setattr(
        "trid3nt_server.data.fetchers._router.transport.open_windowed_cog", _fake_open
    )
    # Staged bucket/key -> url resolution happens (and refuses to fall back to
    # real AWS) BEFORE this open is reached, so every mocked read needs a
    # resolvable endpoint even though ``_fake_open`` never uses it.
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://minio.local:9000")


# --------------------------------------------------------------------------- #
# Spec identity + metadata flags.
# --------------------------------------------------------------------------- #


def test_spec_identity(spec):
    assert spec.name in _NAMES
    assert spec.shape == "raster-cog"
    assert spec.error_code_prefix == _PREFIX[spec.name]
    assert spec.input_error_suffix == "INPUT_INVALID"
    assert spec.empty_error_suffix == "EMPTY"
    assert spec.supports_global_query is False
    assert spec.cache.ttl_class == "static-30d"
    assert spec.normalize.units == (
        "m2/day" if spec.name == "fetch_aquifer_transmissivity" else "m"
    )
    assert spec.ingest["access"] == "direct_window"
    assert spec.ingest["nodata_gate"] is True


def test_quantity_names_the_actual_measurement(specs):
    """The emitted quantity must not blur the three: a DEPTH from the land
    surface down, a THICKNESS of saturated material below that, and the
    TRANSMISSIVITY (m2/day) their ratio and product relate them through."""
    assert specs["fetch_water_table_depth"].normalize.quantity == "water_table_depth"
    assert (specs["fetch_aquifer_thickness"].normalize.quantity
            == "aquifer_saturated_thickness")
    assert (specs["fetch_aquifer_transmissivity"].normalize.quantity
            == "aquifer_transmissivity")


def test_metadata_flags(spec):
    m = synthesize_metadata(spec)
    assert m.name == spec.name
    assert m.ttl_class == "static-30d"
    assert m.cacheable is True
    assert m.supports_global_query is False
    assert m.payload_mb_estimator_name == "estimate_payload_mb"


def test_payload_estimate_declared(spec):
    """The large-payload seam resolves a real per-area estimate, not a guess."""
    est = synthesize_payload_estimator(spec)
    story_county = est(bbox=_BBOX)
    conus = est(bbox=[-125.0, 24.0, -66.5, 50.0])
    assert 0.0 < story_county < 1.0
    assert conus > story_county
    assert spec.payload_estimate.model == "bbox_area"


def test_style_preset_resolves_in_the_qgis_registry(spec):
    """A preset absent from the registry silently renders a wrong colormap."""
    from trid3nt_server.data.publish_layer import publish_layer as pl

    assert pl._registry_style_params(spec.output.style_preset) is not None


def test_the_three_presets_are_distinct(specs):
    """Depth, thickness and transmissivity must not share a ramp -- their
    ranges and their meanings differ (a wetness reading, a quantity, and a
    long-tailed flow-capacity field)."""
    presets = [specs[n].output.style_preset for n in _NAMES]
    assert len(set(presets)) == len(presets)


def test_corpus_carries_the_natural_question(spec):
    assert len(spec.corpus) >= 6
    joined = " ".join(spec.corpus).lower()
    if spec.name == "fetch_water_table_depth":
        assert "how deep is the water table here" in joined
        assert "depth to groundwater" in joined
    elif spec.name == "fetch_aquifer_thickness":
        assert "how thick is the aquifer here" in joined
        assert "saturated thickness" in joined
    else:
        assert "how transmissive is the shallow aquifer here" in joined
        assert "transmissivity" in joined


# --------------------------------------------------------------------------- #
# Staged-object resolution: bucket/key in the spec, host from the environment.
# --------------------------------------------------------------------------- #


def test_endpoint_is_a_staged_object(spec):
    url = spec.endpoints["data"].url
    assert url.startswith(_STAGED_PREFIX)
    assert url.endswith(".tif")
    # Single object per spec: no url_by_param indirection to get wrong.
    assert "url_by_param" not in spec.ingest


def test_both_specs_read_the_same_release_version(specs):
    versions = {
        s.endpoints["data"].url.split("/")[-2] for s in specs.values()
    }
    assert versions == {"zellsanford2020-v1"}


def test_the_read_resolves_the_configured_endpoint(spec, monkeypatch):
    seen: dict = {}
    _patch_open(monkeypatch, np.full((4, 4), 12.0, dtype="float32"), seen)
    raster_cog._direct_window_to_array(spec, {"bbox": _BBOX})
    assert seen["url"].startswith("http://minio.local:9000/trid3nt-cache/staged/")
    assert seen["url"].endswith(spec.endpoints["data"].url.split("/")[-1])


def test_missing_endpoint_raises_typed_config_error_not_empty(spec, monkeypatch):
    """A staged object is uploaded for its full declared coverage, so a failure
    to resolve it is a deployment defect, never 'no data for this AOI'."""
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog._direct_window_to_array(spec, {"bbox": _BBOX})
    assert ei.value.error_code == f"{_PREFIX[spec.name]}_UPSTREAM_ERROR"
    assert ei.value.retryable is True
    assert "STAGED_OBJECT_UNAVAILABLE" in str(ei.value)


def test_staged_404_raises_typed_config_error_not_empty(spec, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://minio.local:9000")
    from trid3nt_server.data.fetchers._router.transport.errors import TransportNotFound

    @contextlib.contextmanager
    def _fake_open_404(url):
        raise TransportNotFound("simulated 404", status=404)
        yield  # pragma: no cover

    monkeypatch.setattr(
        "trid3nt_server.data.fetchers._router.transport.open_windowed_cog",
        _fake_open_404,
    )
    with pytest.raises(RouterUpstreamError) as ei:
        raster_cog._direct_window_to_array(spec, {"bbox": _BBOX})
    assert ei.value.error_code == f"{_PREFIX[spec.name]}_UPSTREAM_ERROR"
    assert "STAGED_OBJECT_UNAVAILABLE" in str(ei.value)


def test_staged_uri_without_a_key_is_refused():
    with pytest.raises(ValueError):
        _staged.staged_object_url("s3://bucketonly")


# --------------------------------------------------------------------------- #
# Coverage envelope: the staged grid's REAL bounds, not the generic CONUS box.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("label,bbox", [
    ("Oahu, Hawaii", [-158.3, 21.2, -157.6, 21.8]),
    ("Anchorage, Alaska", [-150.1, 61.0, -149.5, 61.4]),
    ("San Juan, Puerto Rico", [-66.2, 18.3, -65.9, 18.5]),
])
def test_outside_conus_is_refused_naming_the_coverage_limit(spec, label, bbox):
    with pytest.raises(RouterInputError) as ei:
        router.route(spec, {"bbox": bbox})
    assert ei.value.error_code == f"{_PREFIX[spec.name]}_INPUT_INVALID"
    assert "CONUS" in str(ei.value)


def test_envelope_is_the_staged_grids_own_bounds(spec):
    """Read off the staged objects, which share one grid. Borrowing the generic
    router._CONUS_BBOX (gridmet's, south 25.05) would false-refuse AOIs this
    grid covers -- the Key West lesson from ADR 0297."""
    assert tuple(spec.gates.conus_bbox) == (
        -127.873333, 23.235556, -65.362222, 51.546667,
    )
    # Strictly wider than the generic envelope on the south and west sides --
    # otherwise declaring a per-spec box bought nothing.
    assert spec.gates.conus_bbox[1] < router._CONUS_BBOX[1]
    assert spec.gates.conus_bbox[0] < router._CONUS_BBOX[0]


def test_key_west_passes_the_gate_and_fails_honestly_on_the_read(spec, monkeypatch):
    """Key West (24.53N) is inside the staged envelope, so the cheap pre-fetch
    gate must NOT refuse it -- but the model's active domain stops short of the
    Keys, so the READ is where it honestly reports no coverage. Live-proven:
    the staged window there is entirely nodata."""
    key_west = [-81.85, 24.53, -81.75, 24.60]
    router._apply_gates(spec, {"bbox": key_west})  # must not raise
    _patch_open(monkeypatch, np.full((4, 4), np.nan, dtype="float32"), bbox=key_west)
    with pytest.raises(RouterEmptyError) as ei:
        raster_cog._direct_window_to_array(spec, {"bbox": key_west})
    assert ei.value.error_code == f"{_PREFIX[spec.name]}_EMPTY"


# --------------------------------------------------------------------------- #
# Honesty floor: what the grid actually encodes.
# --------------------------------------------------------------------------- #


def test_off_domain_window_raises_empty_not_a_fabricated_layer(spec, monkeypatch):
    """Open ocean, the Gulf and the Great Lakes are nodata on this grid (unlike
    reitz_2017 recharge, which stamps inland water a finite 0.0). Live-proven:
    a mid-Lake-Michigan window is 0/8100 finite on both objects."""
    _patch_open(monkeypatch, np.full((4, 4), np.nan, dtype="float32"))
    with pytest.raises(RouterEmptyError) as ei:
        raster_cog._direct_window_to_array(spec, {"bbox": _BBOX})
    assert ei.value.error_code == f"{_PREFIX[spec.name]}_EMPTY"


def test_partially_valid_window_is_not_gated(spec, monkeypatch):
    arr = np.full((4, 4), np.nan, dtype="float32")
    arr[2, 2] = 17.0
    _patch_open(monkeypatch, arr)
    out, _tf, _crs = raster_cog._direct_window_to_array(spec, {"bbox": _BBOX})
    assert np.nanmax(out) == pytest.approx(17.0)


def test_values_pass_through_unscaled(spec, monkeypatch):
    """The staged COG is already metres; the read must not rescale it."""
    _patch_open(monkeypatch, np.full((4, 4), 93.09, dtype="float32"))
    out, _tf, _crs = raster_cog._direct_window_to_array(spec, {"bbox": _BBOX})
    assert np.allclose(out, 93.09)


def test_negative_depths_survive_the_read_unclamped(specs, monkeypatch):
    """Where the simulated water table stands ABOVE land surface (wetlands,
    stream corridors) the depth is negative -- down to -25.68 m CONUS-wide, and
    -0.23 m in the Story County acceptance window. Clamping them to zero would
    erase the model's groundwater DISCHARGE areas."""
    spec = specs["fetch_water_table_depth"]
    arr = np.array([[-0.231, -2.264], [5.02, 37.153]], dtype="float32")
    _patch_open(monkeypatch, arr)
    out, _tf, _crs = raster_cog._direct_window_to_array(spec, {"bbox": _BBOX})
    assert out.min() == pytest.approx(-2.264, abs=1e-4)
    assert (out < 0).sum() == 2
    assert np.isfinite(out).all()


# --------------------------------------------------------------------------- #
# The caveats have to state the limits that were actually verified.
# --------------------------------------------------------------------------- #


def test_depth_caveats_state_the_verified_limits(specs):
    joined = " ".join(specs["fetch_water_table_depth"].caveats).lower()
    assert "conus only" in joined
    assert "negative" in joined            # discharge areas, not an error
    assert "saturates" in joined           # clipped at the prescribed model bottom
    assert "simulated" in joined and "not measured" in joined
    assert "38,316" in joined              # the calibration fit, stated concretely


def test_thickness_caveats_refuse_to_overclaim(specs):
    """The derived thickness is the MODELLED surficial system's, over a
    PRESCRIBED model bottom -- not a mapped or drilled aquifer thickness. The
    caveats must say so, and must carry the independent cross-check that showed
    it reading systematically high."""
    joined = " ".join(specs["fetch_aquifer_thickness"].caveats).lower()
    assert "not a mapped aquifer thickness" in joined
    assert "prescribed" in joined
    assert "bdticm" in joined              # the independent cross-check
    assert "ogallala" in joined            # named confined aquifers excluded
    assert "b = t / k" in joined           # the derivation is disclosed


def test_thickness_docstring_leads_with_the_limit(specs):
    """A reader who stops after the first screen must still learn that the base
    is a modelling choice."""
    doc = specs["fetch_aquifer_thickness"].docstring.lower()
    assert "read this first" in doc
    assert "prescribed" in doc


def test_depth_and_thickness_declare_one_shared_grid(specs):
    """All three are faces of one model solution; a divergent envelope would let
    one answer where another refuses."""
    a, b, c = (specs[n] for n in _NAMES)
    assert a.gates.conus_bbox == b.gates.conus_bbox == c.gates.conus_bbox
    assert a.normalize.crs == b.normalize.crs == c.normalize.crs == "EPSG:4326"
    assert (a.payload_estimate.mb_per_sq_deg == b.payload_estimate.mb_per_sq_deg
            == c.payload_estimate.mb_per_sq_deg)


def test_transmissivity_caveats_state_the_west_east_contrast(specs):
    """The paper's headline regional finding must be stated, and stated on the
    MEDIAN -- the mean reads backwards because a handful of western alluvial
    basins run past 100,000 m2/day."""
    joined = " ".join(specs["fetch_aquifer_transmissivity"].caveats).lower()
    assert "conus only" in joined
    assert "lower in the west" in joined
    assert "median" in joined and "mean" in joined
    assert "t = k x b" in joined or "t = k * b" in joined


def test_transmissivity_units_and_quantity_are_its_own(specs):
    """The whole reason this got its own spec: quantity/units must never be
    borrowed from the thickness spec (a m2/day value stamped as metres of
    saturated thickness would be a fabricated layer)."""
    t = specs["fetch_aquifer_transmissivity"]
    assert t.normalize.units == "m2/day"
    assert t.normalize.quantity == "aquifer_transmissivity"
    b = specs["fetch_aquifer_thickness"]
    assert t.normalize.units != b.normalize.units
    assert t.normalize.quantity != b.normalize.quantity
