"""``fetch_chs_nonna``: CHS NONNA-10, the bed under the Canadian channels.

Offline. The network edge is one recorded GetCoverage body over the St. Clair
River at Port Huron and one over Canadian Lake St. Clair, where CHS has surveyed
nothing. What is exercised is the DECISIONS: that the request goes out on the
mosaic's own frame and asks for 4326 back, that the service's fill becomes an
absence rather than a bed at the datum plane, that the row states the zero the
offset service converts, and where the sort puts this source on the bed ladder.
"""

from __future__ import annotations

import base64

import numpy as np
import pytest
import yaml

from trid3nt_contracts.coverage import Coverage
from trid3nt_server.tools.fetchers._fetch_common import PixelBudgetExceededError
from trid3nt_server.tools.fetchers._router import spec as _spec
from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError, RouterInputError)
from trid3nt_server.tools.fetchers._router.spec import (
    SpecLoadError, compose_specs_from_tree, load_spec, served_frames)
from trid3nt_server.tools.fetchers._router.transport import ogc_adapter
from trid3nt_server.tools.fetchers.ocean.fetch_chs_nonna import hooks as nonna
from trid3nt_server.tools.search.match import Need, match

#: Port Huron, on the St. Clair River where it leaves Lake Huron.
PORT_HURON = (-82.4213, 42.9636)

#: The AOI the recorded excerpt was read over, a 12 x 12 window across the
#: Canadian bank of the reach.
REACH = [-82.4300, 42.9600, -82.4180, 42.9700]

#: Canadian Lake St. Clair, which NONNA-10 does not paint at all.
UNSURVEYED = [-82.5800, 42.5000, -82.5600, 42.5200]

#: Recorded from ``GET .../geoserver/wcs?...&Coverage=nonna:NONNA 10 Coverage
#: &CRS=EPSG:3857&WIDTH=12&HEIGHT=12&FORMAT=GeoTIFF&RESPONSE_CRS=EPSG:4326``
#: over ``REACH``: 1,442 bytes of float32 GeoTIFF, the west of the window
#: unsurveyed and the east reading the channel bank down to -9.3 m.
_REACH_TIFF = base64.b64decode("".join((
    "TU0AKgAAAAgAEQEAAAMAAAABAAwAAAEBAAMAAAABAAwAAAECAAMAAAABACAAAAEDAAMA"
    "AAABAAEAAAEGAAMAAAABAAEAAAEVAAMAAAABAAEAAAEaAAUAAAABAAAA3AEbAAUAAAAB"
    "AAAA5AEoAAMAAAABAAEAAAFCAAMAAAABABAAAAFDAAMAAAABABAAAAFEAAQAAAABAAAB"
    "ogFFAAQAAAABAAAEAAFTAAMAAAABAAMAAIXYAAwAAAAQAAAA7IevAAMAAAAQAAABbKSB"
    "AAIAAAAWAAABjAAAAAAAAAAAAAEAAAABAAAAAQAAAAE/UGJN0vGqqwAAAAAAAAAAAAAA"
    "AAAAAADAVJuFHrhR7AAAAAAAAAAAv0tOgbToFVUAAAAAAAAAAEBFfCj1wo9cAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/"
    "8AAAAAAAAAABAAEAAAADBAAAAAABAAIEAQAAAAEAAQgAAAAAARDmMy40MDI4MjM0NjYz"
    "ODUyODg2RTM4AH9///9/f///f3///39///9/f///f3///39///9/f///f3///8DDOFHA"
    "wzhRwMM4UQAAAAAAAAAAAAAAAAAAAAB/f///f3///39///9/f///f3///39///9/f///"
    "f3///39////AwzhRwMM4UcDDOFEAAAAAAAAAAAAAAAAAAAAAf3///39///9/f///f3//"
    "/39///9/f///f3///39///9/f///wMVh6MDFYejA+HjWAAAAAAAAAAAAAAAAAAAAAH9/"
    "//9/f///f3///39///9/f///f3///39///9/f///f3///8DFYejAxWHowPh41gAAAAAA"
    "AAAAAAAAAAAAAAB/f///f3///39///9/f///f3///39///9/f///f3///39////AvBrs"
    "wLwa7MEFwS8AAAAAAAAAAAAAAAAAAAAAf3///39///9/f///f3///39///9/f///f3//"
    "/39////AvBrswLwa7MC8GuzBBFJ8AAAAAAAAAAAAAAAAAAAAAH9///9/f///f3///39/"
    "//9/f///f3///39///9/f///wLRUosDIwGTAyMBkwQYecQAAAAAAAAAAAAAAAAAAAAB/"
    "f///f3///39///9/f///f3///39////Ag9cBwIPXAcCixCTBCLRxwQi0ccEItHEAAAAA"
    "AAAAAAAAAAAAAAAAf3///39///9/f///f3///39///9/f///wH++mcB/vpnAwTOawQjT"
    "CsEI0wrBCyVlAAAAAAAAAAAAAAAAAAAAAH9///9/f///f3///39///9/f///f3///8B/"
    "vpnAf76ZwMEzmsEI0wrBCNMKwQslZQAAAAAAAAAAAAAAAAAAAAB/f///f3///39///9/"
    "f///f3///8AzLPjAhescwIXrHMEI6K3BCOitwQjorcEU75sAAAAAAAAAAAAAAAAAAAAA"
    "f3///39///9/f///f3///39////AMyz4wOq43MDquNzBC9CKwQyQo8EMkKPADkF1AAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAA=")))

#: Recorded from the same request over ``UNSURVEYED``: HTTP 200, image/tiff, and
#: every cell the coverage's own fill value.
_UNSURVEYED_TIFF = base64.b64decode("".join((
    "TU0AKgAAAAgAEQEAAAMAAAABAAQAAAEBAAMAAAABAAQAAAECAAMAAAABACAAAAEDAAMA"
    "AAABAAEAAAEGAAMAAAABAAEAAAEVAAMAAAABAAEAAAEaAAUAAAABAAAA3AEbAAUAAAAB"
    "AAAA5AEoAAMAAAABAAEAAAFCAAMAAAABABAAAAFDAAMAAAABABAAAAFEAAQAAAABAAAB"
    "ogFFAAQAAAABAAAEAAFTAAMAAAABAAMAAIXYAAwAAAAQAAAA7IevAAMAAAAQAAABbKSB"
    "AAIAAAAWAAABjAAAAAAAAAAAAAEAAAABAAAAAQAAAAE/dHrhR64AAAAAAAAAAAAAAAAA"
    "AAAAAADAVKUeuFHrhQAAAAAAAAAAv3R64UeuGAAAAAAAAAAAAEBFQo9cKPXDAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/"
    "8AAAAAAAAAABAAEAAAADBAAAAAABAAIEAQAAAAEAAQgAAAAAARDmMy40MDI4MjM0NjYz"
    "ODUyODg2RTM4AH9///9/f///f3///39///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB/f///f3///39///9/f///AAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAf3///39///9/f///f3//"
    "/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH9/"
    "//9/f///f3///39///8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAA=")))


class _Recorded:
    """One recorded GetCoverage answer, in the adapter's own response shape."""

    def __init__(self, body: bytes) -> None:
        self.content = body
        self.content_type = "image/tiff"


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_chs_nonna"]


@pytest.fixture(scope="module")
def row(spec):
    return spec.coverage[0]


@pytest.fixture
def answered(monkeypatch):
    """Hand the delegate one recorded body and return what it asked the WCS for."""
    def serve(body: bytes) -> dict:
        asked: dict = {}

        def fake(**kwargs):
            asked.update(kwargs)
            return _Recorded(body)

        monkeypatch.setattr(ogc_adapter, "fetch_ogc_layer", fake)
        return asked

    return serve


def test_the_row_states_a_measured_bed_over_the_lakes_and_their_channels(spec, row):
    assert (row.data_class, row.kind) == ("bathymetry", "measured")
    assert row.extent.kind == "surface" and row.reach_km is None
    assert row.extent.covers(*PORT_HURON)
    # The waters the row speaks for are named on it, because the product reaches
    # far past them and chart datum is another surface out there - which the
    # caveats carry, the note being one sentence the match quotes on the wire.
    assert "connecting channels" in row.extent.note
    assert any("tidal coasts" in caveat for caveat in spec.caveats)
    assert not row.window.series and row.window.latest == "2026-04-01"


def test_the_row_counts_its_bed_from_a_zero_the_offset_service_converts(row):
    assert row.datum == "lwd_igld85"
    assert row.datum in served_frames()


def test_the_row_states_the_products_own_posting_and_its_one_value_column(row):
    assert row.resolution_m == 10.0
    assert row.value_column == "elevation"
    assert row.units == {"elevation": "m"}


def test_the_layer_is_stamped_in_the_unit_the_row_publishes(spec):
    assert spec.normalize.units == "meters"
    assert spec.normalize.quantity == "elevation"
    # The datum is stated once, on the row, and the spec adopts it from there.
    assert spec.vertical_datum == "lwd_igld85"


def test_a_row_stating_its_zero_as_prose_never_loads(spec, tmp_path):
    """The loader's rowed rule, seeded on this spec: a datum no offset can be
    asked in is a surface nothing can bring another onto."""
    raw = yaml.safe_load(
        (_spec._fetchers_root() / "ocean" / "fetch_chs_nonna"
         / "source.yaml").read_text())
    raw["coverage"][0]["datum"] = "CHS chart datum"
    with pytest.raises(SpecLoadError, match="names no frame"):
        load_spec(raw, source_hint="seeded")


def test_the_request_goes_out_on_the_mosaics_own_frame(spec, answered):
    """GeoServer refuses a GetCoverage whose BBOX is stated in anything but the
    coverage's native frame, so the AOI is reprojected and the answer is asked
    for back on 4326."""
    asked = answered(_REACH_TIFF)
    nonna.read(spec, {"bbox": list(REACH), "resolution_m": 10.0}, timeout_s=180.0)
    assert asked["crs"] == "EPSG:3857"
    assert asked["extra_params"] == {"RESPONSE_CRS": "EPSG:4326"}
    assert asked["layer_name"] == "nonna:NONNA 10 Coverage"
    assert asked["version"] == "1.0.0" and asked["service_type"] == "WCS"
    west, south, east, north = asked["bbox"]
    assert (round(west), round(south)) == (-9176066, 5305885)
    assert (round(east), round(north)) == (-9174730, 5307407)


def test_the_asked_spacing_is_the_lattice_the_request_is_sized_to(spec, answered):
    asked = answered(_REACH_TIFF)
    nonna.read(spec, {"bbox": list(REACH), "resolution_m": 10.0}, timeout_s=180.0)
    assert (asked["width_px"], asked["height_px"]) == (98, 111)


def test_the_excerpt_reads_as_an_elevation_under_water_not_a_depth(spec, answered):
    answered(_REACH_TIFF)
    arr, transform, crs = nonna.read(
        spec, {"bbox": list(REACH), "resolution_m": 10.0}, timeout_s=180.0)
    finite = np.isfinite(arr)
    assert arr.shape == (12, 12) and str(crs) == "EPSG:4326"
    assert (transform * (0, 0)) == pytest.approx((-82.43, 42.97))
    # Positive up: the whole window is under water, so every reading is negative.
    assert arr[finite].max() == pytest.approx(-2.2227452)
    assert arr[finite].min() == pytest.approx(-9.308497)
    assert int(finite.sum()) == 55
    assert arr[0][9] == pytest.approx(-6.1006246)


def test_the_services_own_fill_becomes_an_absence_and_never_a_bed(spec, answered):
    answered(_REACH_TIFF)
    arr, _transform, _crs = nonna.read(
        spec, {"bbox": list(REACH), "resolution_m": 10.0}, timeout_s=180.0)
    assert not (arr >= nonna.NODATA).any()
    # The west of this window is unsurveyed; read as a number it would be a
    # 3.4e38 m bed, and filled to zero it would be a bed at the datum plane.
    assert bool(np.isnan(arr[0][0]))


def test_a_box_chs_surveyed_nothing_in_is_a_coverage_gap(spec, answered):
    """The service answers such a box with HTTP 200 and an all-fill raster, so
    the emptiness is read off the array."""
    answered(_UNSURVEYED_TIFF)
    with pytest.raises(RouterEmptyError) as excinfo:
        nonna.read(spec, {"bbox": list(UNSURVEYED), "resolution_m": 10.0},
                   timeout_s=180.0)
    assert excinfo.value.error_code == "CHS_NONNA_COVERAGE_GAP"


def test_a_bbox_past_the_services_budget_refuses_naming_the_spacing_that_fits(spec):
    with pytest.raises(PixelBudgetExceededError) as excinfo:
        nonna.validate(spec, {"bbox": [-83.0, 42.0, -82.0, 43.0],
                              "resolution_m": 10.0})
    assert "resolution_m=28 m" in str(excinfo.value)


def test_a_degenerate_bbox_refuses_before_the_cache_and_the_network(spec):
    with pytest.raises(RouterInputError) as excinfo:
        nonna.validate(spec, {"bbox": [-82.418, 42.96, -82.43, 42.97]})
    assert excinfo.value.error_code == "CHS_NONNA_INPUT_INVALID"


def test_the_bed_ladder_at_port_huron_lists_nonna_by_its_cell(row):
    """The sort reads the cell, so NONNA stands under the surveyed prism and the
    NAVD88 grids and over the 90 m lake mosaic - and it is the only rung already
    counted from the zero a lakes run is solved on."""
    def surface(res: float, datum: str) -> Coverage:
        return row.model_copy(update={"resolution_m": res, "datum": datum})

    choice = match(
        Need(slot="bed", data_class="bathymetry", lon=PORT_HURON[0],
             lat=PORT_HURON[1], frame="lwd_igld85", mesh_m=20.0),
        [("fetch_bluetopo", surface(2.0, "navd88")),
         ("fetch_chs_nonna", row),
         ("fetch_greatlakes_bathymetry", surface(90.0, "lwd_igld85"))])
    assert [r.fetcher for r in choice.rows] == [
        "fetch_bluetopo", "fetch_chs_nonna", "fetch_greatlakes_bathymetry"]
    assert choice.rows[1].datum == "lwd_igld85"


def test_the_source_is_found_through_its_class(class_routes_to_the_match):
    """A covered fetcher carries no corpus: its class is the door."""
    class_routes_to_the_match("bathymetry", "fetch_chs_nonna")
