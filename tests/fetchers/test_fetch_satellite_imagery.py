"""``fetch_satellite_imagery``, on the frames-list output shape.

Covered: registration and metadata; the server index as the ONLY vocabulary, including
the refusal that lists what the server serves; the one cadence branch - a step for
geostationary, the pass list for polar - and the window of one instant; and the honesty
floor from an un-registered sector through to every frame degrading."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router import registration as reg
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
)
from trid3nt_server.tools.fetchers._router.executors import animation_frames as EX
from trid3nt_server.tools.fetchers.imagery.fetch_satellite_imagery import hooks as SI
from trid3nt_server.tools.fetchers.imagery._satellite_slider import (
    SliderEmptyError,
    ts_int_to_iso,
)

_BBOX = (-122.5, 39.5, -121.5, 40.5)
_SPEC = reg.get_spec("fetch_satellite_imagery")

#: The geostationary registration the index publishes for a full disk: where the bird
#: sits, how far away, and the scan angle its disk subtends in zoom-0 pixels.
_LAT_LON_QUERY = {
    "lon0": -137.0, "sat_alt": 42171.7, "max_rad_x": 0.151337, "max_rad_y": 0.150988,
    "disk_radius_x_z0": 338, "disk_radius_y_z0": 337,
}

#: A stand-in for the server's definition file, in its shape: a menu heading, a product
#: one sector excludes, the registration on the geostationary birds only, and a
#: steerable mesoscale box, which nothing can place on the ground.
_INDEX = {
    "goes-18": {
        "sectors": {
            "conus": {
                "default_product": "geocolor",
                "defaults": {"minutes_between_images": 5},
                "missing_products": ["cira_blended_tpw"],
                "tile_size": 625, "max_zoom_level": 4,
            },
            "full_disk": {
                "default_product": "geocolor",
                "defaults": {"minutes_between_images": 10},
                "lat_lon_query": _LAT_LON_QUERY,
                "tile_size": 678, "max_zoom_level": 5,
            },
            "mesoscale_01": {
                "default_product": "geocolor",
                "defaults": {"minutes_between_images": 1},
                "tile_size": 500, "max_zoom_level": 2,
            },
        },
        "products": {
            "individual_abi_bands": {"product_title": "----------INDIVIDUAL ABI BANDS----------"},
            "geocolor": {"product_title": "GeoColor (CIRA)"},
            "cira_geofire": {"product_title": "GeoFire (CIRA)"},
            "cira_blended_tpw": {"product_title": "Blended TPW"},
        },
    },
    "jpss": {
        "sectors": {
            "conus": {
                "default_product": "cira_geocolor",
                "defaults": {"minutes_between_images": 51},
                "tile_size": 500, "max_zoom_level": 5,
            }
        },
        "products": {
            "cira_geocolor": {"product_title": "GeoColor (CIRA)"},
            "cira_natural_fire_color": {"product_title": "Day Fire (CIRA)"},
        },
    },
    "himawari": {
        "sectors": {
            "full_disk": {
                "default_product": "geocolor",
                "defaults": {"minutes_between_images": 10},
                "lat_lon_query": dict(_LAT_LON_QUERY, lon0=140.69),
                "tile_size": 688, "max_zoom_level": 5,
            }
        },
        "products": {"geocolor": {"product_title": "GeoColor (CIRA)"}},
    },
}


def _ts(y, mo, d, h, mi):
    return int(f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}00")


#: Five-minute geostationary stamps across two hours.
_GEO_TS = [_ts(2026, 9, 14, 20, m) for m in range(0, 60, 5)] + [
    _ts(2026, 9, 14, 21, m) for m in range(0, 60, 5)
]

#: Irregular overpasses, two in local daylight at the AOI longitude and one at night.
_POLAR_TS = [
    _ts(2026, 9, 12, 9, 30),
    _ts(2026, 9, 12, 20, 40),
    _ts(2026, 9, 13, 21, 20),
]

_WINDOW = dict(start_utc="2026-09-14T20:00:00Z", end_utc="2026-09-14T22:00:00Z")


class _R:
    def __init__(self, uri):
        self.uri = uri


@pytest.fixture
def stub(monkeypatch):
    """The index, the availability read and the object store, all off the network."""
    monkeypatch.setattr(SI, "load_index", lambda sc, **kw: _INDEX)
    monkeypatch.setattr(SI, "pick_zoom_for_aoi", lambda *a, **k: 3)
    monkeypatch.setattr(SI, "usable_zoom", lambda *a, **k: a[4])
    monkeypatch.setattr(
        EX, "read_through",
        lambda metadata, params, ext, fetch_fn, **keyed: _R(
            uri=f"s3://fake/{params['ts_int']}.tif"),
    )

    def _stamps(ts):
        monkeypatch.setattr(SI, "fetch_slider_timestamps", lambda *a, **k: list(ts))

    return _stamps


def _run(stamps=None, **kw):
    if stamps is not None:
        stamps()
    return TOOL_REGISTRY["fetch_satellite_imagery"].fn(bbox=_BBOX, **kw)


def test_registered_and_spec_served():
    assert "fetch_satellite_imagery" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_satellite_imagery"]
    assert entry.metadata.source_class == "satellite_imagery"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.cacheable is True
    assert "fetch_satellite_imagery" in reg.registered_spec_names()


def test_index_object_is_extracted_from_the_definition_file():
    js = 'var x = 1;\n var json =\n {"satellites": {"a": {"b": "}"}}}; \n var y = 2;'
    assert SI._extract_json_object(js) == {"satellites": {"a": {"b": "}"}}}


def test_index_extraction_refuses_a_truncated_file():
    with pytest.raises(ValueError):
        SI._extract_json_object('var json = {"satellites": {')


def test_sector_products_drops_headings_and_sector_exclusions():
    served = SI.sector_products(_INDEX["goes-18"], "conus")
    assert served == ["geocolor", "cira_geofire"]


def test_geostationary_is_read_from_the_stated_sub_satellite_longitude():
    assert SI.is_geostationary(_INDEX["goes-18"]) is True
    assert SI.is_geostationary(_INDEX["jpss"]) is False


def test_unknown_satellite_lists_what_the_server_serves(stub):
    with pytest.raises(RouterInputError) as ei:
        _run(satellite="terra")
    assert "himawari" in str(ei.value) and "jpss" in str(ei.value)


def test_unknown_sector_lists_that_satellites_sectors(stub):
    with pytest.raises(RouterInputError) as ei:
        _run(satellite="goes-18", sector="japan")
    assert "full_disk" in str(ei.value)


def test_unknown_product_lists_the_served_products(stub):
    with pytest.raises(RouterInputError) as ei:
        _run(satellite="goes-18", sector="conus", product="day_fire")
    assert "cira_geofire" in str(ei.value)


def test_product_excluded_by_the_sector_is_refused(stub):
    with pytest.raises(RouterInputError):
        _run(satellite="goes-18", sector="conus", product="cira_blended_tpw")


def test_a_sector_nothing_can_place_on_the_ground_is_refused(stub):
    with pytest.raises(RouterInputError) as ei:
        _run(satellite="goes-18", sector="mesoscale_01")
    assert "goes-18/full_disk" in str(ei.value)


def test_a_disk_the_index_registers_is_served(stub):
    layers = _run(lambda: stub(_GEO_TS), satellite="himawari", sector="full_disk",
                  end_utc="2026-09-14T20:33:00Z")
    assert len(layers) == 1 and layers[0].name.startswith("HIMAWARI geocolor")


def test_day_only_is_refused_for_a_geostationary_satellite(stub):
    with pytest.raises(RouterInputError) as ei:
        _run(satellite="goes-18", sector="conus", day_only=True, **_WINDOW)
    assert "step_minutes" in str(ei.value)


def test_step_minutes_is_refused_for_a_polar_satellite(stub):
    with pytest.raises(RouterInputError) as ei:
        _run(satellite="jpss", sector="conus", step_minutes=10, **_WINDOW)
    assert "day_only" in str(ei.value)


def test_backwards_window_is_refused(stub):
    with pytest.raises(RouterInputError):
        _run(start_utc="2026-09-14T22:00:00Z", end_utc="2026-09-14T20:00:00Z",
             satellite="goes-18")


def test_unparseable_time_is_refused(stub):
    with pytest.raises(RouterInputError):
        _run(satellite="goes-18", end_utc="whenever")


def test_step_frames_thins_to_the_requested_cadence():
    kept = SI._step_frames(_GEO_TS, 10)
    assert kept == [t for t in _GEO_TS if int(str(t)[10:12]) % 10 == 0]


def test_step_frames_at_the_native_cadence_keeps_every_stamp():
    assert SI._step_frames(_GEO_TS, 5) == _GEO_TS


def test_pass_frames_keeps_local_daylight_only():
    day = SI._pass_frames(_POLAR_TS, -122.0, day_only=True)
    assert day == _POLAR_TS[1:]
    assert SI._pass_frames(_POLAR_TS, -122.0, day_only=False) == _POLAR_TS


def test_cap_keeps_the_endpoints():
    capped = SI._cap(list(range(100)), 10)
    assert capped[0] == 0 and capped[-1] == 99 and len(capped) == 10


def test_geostationary_window_returns_ordered_stepped_frames(stub):
    layers = _run(lambda: stub(_GEO_TS), satellite="goes-18", sector="conus",
                  product="geocolor", step_minutes=10, **_WINDOW)
    assert [lyr.name for lyr in layers] == [
        f"GOES-18 geocolor step {n} {ts_int_to_iso(t)}"
        for n, t in enumerate(SI._step_frames(_GEO_TS, 10), start=1)
    ]
    assert [lyr.valid_from for lyr in layers] == [lyr.valid_from for lyr in layers]
    assert all(a.valid_to == b.valid_from for a, b in zip(layers, layers[1:]))
    assert all(lyr.bbox == _BBOX and lyr.style["kind"] == "continuous" for lyr in layers)


def test_polar_window_returns_the_daylight_pass_list(stub):
    layers = _run(lambda: stub(_POLAR_TS), satellite="jpss", sector="conus",
                  product="cira_natural_fire_color", day_only=True,
                  start_utc="2026-09-12T00:00:00Z", end_utc="2026-09-14T00:00:00Z")
    assert [lyr.name.rsplit(" ", 1)[-1] for lyr in layers] == [
        ts_int_to_iso(t) for t in _POLAR_TS[1:]
    ]
    assert layers[0].layer_id.startswith("slider-jpss-conus-cira_natural_fire_color-")


def test_product_defaults_to_the_sectors_own(stub):
    layers = _run(lambda: stub(_POLAR_TS), satellite="jpss", sector="conus",
                  start_utc="2026-09-12T00:00:00Z", end_utc="2026-09-14T00:00:00Z")
    assert all("cira_geocolor" in lyr.name for lyr in layers)


def test_one_instant_is_one_frame(stub):
    layers = _run(lambda: stub(_GEO_TS), satellite="goes-18", sector="conus",
                  end_utc="2026-09-14T20:33:00Z")
    assert len(layers) == 1
    assert layers[0].name.endswith(ts_int_to_iso(_ts(2026, 9, 14, 20, 35)))
    assert layers[0].valid_from is None


def test_an_instant_beyond_the_index_is_empty_not_a_far_snap(stub):
    with pytest.raises(RouterEmptyError) as ei:
        _run(lambda: stub(_GEO_TS), satellite="goes-18", sector="conus",
             end_utc="2026-09-14T23:30:00Z")
    assert ei.value.error_code == "SATELLITE_IMAGERY_EMPTY"


def test_empty_window_raises_the_typed_empty(stub):
    with pytest.raises(RouterEmptyError) as ei:
        _run(lambda: stub(_GEO_TS), satellite="goes-18", sector="conus",
             start_utc="2026-09-13T00:00:00Z", end_utc="2026-09-13T06:00:00Z")
    assert ei.value.error_code == "SATELLITE_IMAGERY_EMPTY"


def test_honesty_floor_every_frame_degraded(stub, monkeypatch):
    stub(_GEO_TS)
    monkeypatch.setattr(
        SI, "stitch_slider_mosaic",
        lambda *a, **k: (_ for _ in ()).throw(SliderEmptyError("off grid")),
    )

    def _rt(metadata, params, ext, fetch_fn, **keyed):
        fetch_fn()
        return _R("s3://never")

    monkeypatch.setattr(EX, "read_through", _rt)
    with pytest.raises(RouterEmptyError) as ei:
        TOOL_REGISTRY["fetch_satellite_imagery"].fn(
            bbox=_BBOX, satellite="goes-18", sector="conus", **_WINDOW
        )
    assert ei.value.error_code == "SATELLITE_IMAGERY_EMPTY"


def test_bbox_none_raises_input_error(stub):
    with pytest.raises(RouterInputError):
        TOOL_REGISTRY["fetch_satellite_imagery"].fn(bbox=None)


def test_is_daytime_pass_by_local_solar_hour():
    assert SI._is_daytime_pass(_ts(2026, 9, 12, 20, 40), -122.0) is True
    assert SI._is_daytime_pass(_ts(2026, 9, 12, 9, 30), -122.0) is False


def test_parse_utc_accepts_iso_and_bare_date():
    assert SI._parse_utc(_SPEC, "2026-09-14T20:00:00Z") == datetime(
        2026, 9, 14, 20, tzinfo=timezone.utc
    )
    assert SI._parse_utc(_SPEC, "2026-09-14") == datetime(2026, 9, 14, tzinfo=timezone.utc)
