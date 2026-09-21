"""The bed rows and the ranked list they are laid in.

Offline. Every network edge is a fixture: the BlueTopo tile scheme is a
GeoPackage built here, the CUDEM manifest is a list of names, and a tile header
is a stub whose only content is the datum string the gate reads. What is
exercised is the DECISIONS - which source states what, what a partial cover
reports, what a refusal names, and the order the sort lays them in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trid3nt_server.tools.fetchers import _tile_mosaic
from trid3nt_server.tools.fetchers.ocean.fetch_bluetopo import hooks as bt
from trid3nt_server.tools.fetchers.ocean.fetch_cudem import hooks as cu
from trid3nt_server.tools.fetchers.ocean.fetch_etopo import hooks as et
from trid3nt_server.tools.fetchers.ocean.fetch_regional_coastal_dem import (
    hooks as rc,
)

COASTAL_AOI = (-85.75, 29.55, -85.25, 30.20)




@pytest.fixture()
def tile_scheme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tile scheme with one delivered 4 m tile, one 16 m tile, one undelivered."""
    geopandas = pytest.importorskip("geopandas")
    from shapely.geometry import box

    frame = geopandas.GeoDataFrame(
        {
            "tile": ["FINE", "COARSE", "UNDELIVERED"],
            "GeoTIFF_Link": [
                "https://example.invalid/BlueTopo/FINE/FINE.tiff",
                "https://example.invalid/BlueTopo/COARSE/COARSE.tiff",
                None,
            ],
            "Resolution": ["4m", "16m", None],
            "UTM": ["16", "16", None],
            "Delivered_Date": ["2026-01-01", "2026-01-01", None],
            "geometry": [
                box(-85.6, 30.0, -85.5, 30.1),
                box(-85.5, 30.0, -85.4, 30.1),
                box(-85.4, 30.0, -85.3, 30.1),
            ],
        },
        crs="EPSG:4326",
    )
    path = tmp_path / "scheme.gpkg"
    frame.to_file(path, driver="GPKG")
    monkeypatch.setattr(bt, "_tile_scheme_path", lambda **_kw: str(path))
    return path


def test_a_tile_row_with_no_delivered_link_is_not_data(tile_scheme: Path) -> None:
    rows = bt.select_bluetopo_tiles((-85.6, 30.0, -85.3, 30.1))
    assert [r["tile"] for r in sorted(rows, key=lambda r: r["tile"])] == [
        "COARSE", "FINE"
    ]


def test_the_selected_tiles_run_coarsest_first_so_the_finest_paints_last(
    tile_scheme: Path,
) -> None:
    rows = bt.select_bluetopo_tiles((-85.6, 30.0, -85.3, 30.1))
    assert [r["resolution"] for r in rows] == ["16m", "4m"]


def test_coverage_is_the_painted_bed_not_the_delivered_footprint() -> None:
    """A tile that intersects the whole AOI can still leave a quarter of it
    nodata: BlueTopo publishes bed for navigationally significant water only, and
    crediting the footprint reported a bed the programme does not publish."""
    numpy = pytest.importorskip("numpy")

    grid = numpy.full((10, 10), 1.0, dtype="float32")
    grid[:, :4] = numpy.float32("nan")  # a delivered tile, 40% of it unpainted
    assert _tile_mosaic.painted_fraction(grid) == pytest.approx(0.60)
    assert _tile_mosaic.painted_fraction(
        numpy.full((4, 4), numpy.float32("nan"))) == 0.0
    assert _tile_mosaic.painted_fraction(numpy.zeros((0, 0))) == 0.0


def test_a_half_nan_tile_grades_the_gap_gate_on_what_it_painted(
    monkeypatch: pytest.MonkeyPatch, tile_scheme: Path
) -> None:
    """The whole chain on one synthetic tile: half the AOI painted, so the rung
    reports 0.50 and the ladder gate calls it a gap rather than a whole bed."""
    numpy = pytest.importorskip("numpy")

    half = numpy.full((8, 8), 3.0, dtype="float32")
    half[:4, :] = numpy.float32("nan")
    monkeypatch.setattr(bt, "assert_navd88_tile", lambda _p: "NAVD88")
    monkeypatch.setattr(
        bt, "mosaic",
        lambda sources, *_a, **_kw: (half, None, "EPSG:4326",
                                     [True] * len(sources)))
    recorded: dict = {}
    monkeypatch.setattr(bt, "record_provenance", recorded.update)

    bt.read_bluetopo(None, {"bbox": (-85.6, 30.0, -85.3, 30.1)}, timeout_s=10.0)
    assert recorded["coverage_fraction"] == pytest.approx(0.50)
    assert recorded["rung_coverage"] == {"bluetopo": pytest.approx(0.50)}


def test_an_aoi_no_delivered_tile_reaches_refuses_by_name(
    tile_scheme: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(bt.BlueTopoCoverageGapError) as excinfo:
        bt.read_bluetopo(None, {"bbox": (-80.0, 25.0, -79.9, 25.1)}, timeout_s=10.0)
    assert excinfo.value.error_code == "BLUETOPO_COVERAGE_GAP"


class _StubTile:
    def __init__(self, wkt: str, vertical_tag: str) -> None:
        self._wkt, self._tag = wkt, vertical_tag

    def __enter__(self) -> "_StubTile":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    @property
    def crs(self) -> "_StubTile":
        return self

    def to_wkt(self) -> str:
        return self._wkt

    def tags(self) -> dict[str, str]:
        return {"VERTICALDATUMWKT": self._tag}


def _stub_open(monkeypatch: pytest.MonkeyPatch, wkt: str, tag: str) -> None:
    import rasterio

    monkeypatch.setattr(rasterio, "open", lambda *_a, **_kw: _StubTile(wkt, tag))


def test_a_tile_that_states_navd88_passes_the_datum_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_open(monkeypatch, 'COMPD_CS["NAD83 / UTM zone 16N + navd88"]',
               'VERTCRS["navd88"]')
    assert bt.assert_navd88_tile("/vsicurl/whatever.tiff") == "NAVD88"


def test_a_tile_that_states_no_navd88_refuses_rather_than_merging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_open(monkeypatch, 'COMPD_CS["NAD83 / UTM zone 16N + MLLW"]', 'VERTCRS["mllw"]')
    with pytest.raises(bt.BlueTopoDatumError) as excinfo:
        bt.assert_navd88_tile("/vsicurl/whatever.tiff")
    assert excinfo.value.error_code == "BLUETOPO_DATUM_MISMATCH"


def test_the_envelope_states_the_datum_in_provenance() -> None:
    fields = bt.envelope_bluetopo(
        None, {"bbox": COASTAL_AOI}, None, None,
        provenance={
            "vertical_datum": "NAVD88", "tile_count": 3,
            "resolution_tiers": ["4m", "8m"], "coverage_fraction": 0.87,
            "rung_coverage": {"bluetopo": 0.87},
        },
    )
    assert fields["vertical_datum"] == "NAVD88"
    assert fields["tile_count"] == 3
    assert fields["resolution_tiers"] == ["4m", "8m"]
    assert fields["coverage_fraction"] == pytest.approx(0.87)
    assert "NAVD88" in fields["name"]




def test_the_bed_resolution_lever_is_resolution_m_from_the_spec_to_the_merge(
    monkeypatch: pytest.MonkeyPatch, tile_scheme: Path
) -> None:
    """One lever name across the raster fetchers: what the spec declares is what the
    rung edge hands over and what the hook passes into the composite."""
    numpy = pytest.importorskip("numpy")
    from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

    spec = load_spec_from_path(Path(
        "trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/source.yaml"))
    assert "resolution_m" in spec.params
    assert [d.param for d in spec.resolution_declarations] == ["resolution_m"]

    passed: dict = {}
    monkeypatch.setattr(bt, "assert_navd88_tile", lambda _p: "NAVD88")
    monkeypatch.setattr(bt, "record_provenance", lambda _fields: None)
    monkeypatch.setattr(
        bt, "mosaic",
        lambda sources, *_a, **kw: (passed.update(kw)
                                    or (numpy.full((4, 4), 3.0, dtype="float32"),
                                        None, "EPSG:4326",
                                        [True] * len(sources))))

    bt.read_bluetopo(None, {"bbox": (-85.6, 30.0, -85.3, 30.1), "resolution_m": 8},
                     timeout_s=10.0)
    assert passed == {"resolution_m": 8.0, "source": "fetch_bluetopo"}


def test_a_bed_resolution_that_is_not_a_cell_size_refuses_by_the_lever_name() -> None:
    with pytest.raises(bt.BlueTopoInputError) as excinfo:
        bt.validate_bluetopo(None, {"bbox": COASTAL_AOI, "resolution_m": 0})
    assert "resolution_m" in str(excinfo.value)


def test_the_bluetopo_spec_declares_the_delegate_hooks_and_the_result_model() -> None:
    from trid3nt_contracts.execution import LAYER_RESULT_MODELS
    from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

    spec = load_spec_from_path(Path(
        "trid3nt_server/tools/fetchers/ocean/fetch_bluetopo/source.yaml"))
    assert spec.hooks is not None
    assert spec.hooks.delegate == "bluetopo.read"
    assert spec.hooks.delegate_validate == "bluetopo.validate"
    assert spec.hooks.envelope == "bluetopo.envelope"
    assert spec.output.result_model in LAYER_RESULT_MODELS
    assert spec.normalize.datum == "NAVD88"




#: Every source a recipe may hand ``set_bed``: the rows whose quantity IS bed
#: elevation. Read off the tree rather than listed, so a new one joins the rule
#: by existing rather than by somebody remembering to add it here.
def _bed_capable_rows() -> dict:
    from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree

    return {name: spec for name, spec in compose_specs_from_tree().items()
            if spec.normalize.quantity == "elevation"}


def test_every_bed_capable_source_row_states_its_vertical_datum() -> None:
    rows = _bed_capable_rows()
    assert rows, "no elevation source rows found"
    unstated = sorted(name for name, spec in rows.items()
                      if not spec.vertical_datum)
    assert not unstated, (
        f"{unstated} would paint a bed whose reference nobody carried")


def test_the_lake_row_pins_one_product_of_the_mixed_mosaic() -> None:
    """DEM_all merges the lake-datum grids, the NAVD88 coastal tiles, the same
    tiles on MHW and the EGM2008 global bases under one name, so the row that
    reads it as a bed names the ONE product it means."""
    from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

    spec = load_spec_from_path(Path(
        "trid3nt_server/tools/fetchers/ocean/fetch_greatlakes_bathymetry/"
        "source.yaml"))
    rule = spec.ingest["imageserver"]["export_query"]["mosaicRule"]
    assert "Name='greatlakes_lakedatum'" in rule
    assert spec.vertical_datum == "LWD_IGLD85"


def test_the_bathymetry_sources_are_found_through_their_class(
        class_routes_to_the_match) -> None:
    """A covered fetcher carries no corpus: its class is the door."""
    for fetcher in ("fetch_bluetopo", "fetch_greatlakes_bathymetry",
                    "fetch_cudem", "fetch_etopo",
                    "fetch_regional_coastal_dem"):
        class_routes_to_the_match("bathymetry", fetcher)


#: The run's own AOI over the St. Clair River between Lake Huron and Lake
#: St. Clair, where the mosaic holds no sounding at all.
ST_CLAIR_RIVER_AOI = (-82.475404, 42.886362, -82.404648, 42.99302)


def _lake_spec():
    from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

    return load_spec_from_path(Path(
        "trid3nt_server/tools/fetchers/ocean/fetch_greatlakes_bathymetry/"
        "source.yaml"))


def test_the_lake_rings_stop_at_the_lakes_and_not_the_rivers_between_them() -> None:
    """The coverage extent is the footprint the soundings have: inside each lake
    and nowhere along the channels that join them."""
    from shapely.geometry import Point, Polygon

    rings = [Polygon(r) for r in _lake_spec().coverage[0].extent.rings]
    west, south, east, north = ST_CLAIR_RIVER_AOI
    for corner in ((west, south), (east, north), (east, south), (west, north)):
        assert not any(ring.contains(Point(*corner)) for ring in rings), corner
    # The lakes the corridor runs between are still covered, and so is the lake
    # the old rectangle around Erie was the only ring to reach.
    for lake in ((-82.35, 43.15), (-82.70, 42.45), (-81.50, 41.90)):
        assert any(ring.contains(Point(*lake)) for ring in rings), lake


def test_the_lake_grid_publishes_its_own_fill_as_nodata(monkeypatch) -> None:
    """Zero is what the mosaic writes where it sounded nothing, so a consumer
    reading coverage off the raster sees an absence rather than a flat bed at
    the datum plane."""
    import io

    import numpy as np
    import rasterio
    from rasterio import transform as rtransform

    from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError
    from trid3nt_server.tools.fetchers._router.executors import raster_cog

    def _tiff(values):
        buf = io.BytesIO()
        with rasterio.open(
                buf, "w", driver="GTiff", height=2, width=2, count=1,
                dtype="float32", crs="EPSG:4326",
                transform=rtransform.from_bounds(-82.5, 42.8, -82.4, 42.9, 2, 2)
        ) as dst:
            dst.write(np.asarray(values, dtype="float32"), 1)
        return buf.getvalue()

    tp = "trid3nt_server.tools.fetchers._router.transport"
    spec, params = _lake_spec(), {"bbox": list(ST_CLAIR_RIVER_AOI)}

    monkeypatch.setattr(f"{tp}.get_client", lambda: object())
    monkeypatch.setattr(f"{tp}.get_bytes",
                        lambda *a, **k: (_tiff([[0.0, -9.3], [0.0, -4.1]]),
                                         "image/tiff", "x"))
    with rasterio.open(io.BytesIO(raster_cog.execute(spec, params))) as src:
        band = src.read(1)
    assert np.isnan(band[0, 0]) and np.isnan(band[1, 0])
    assert band[0, 1] == pytest.approx(-9.3) and band[1, 1] == pytest.approx(-4.1)

    monkeypatch.setattr(f"{tp}.get_bytes",
                        lambda *a, **k: (_tiff([[0.0, 0.0], [0.0, 0.0]]),
                                         "image/tiff", "x"))
    with pytest.raises(RouterEmptyError) as raised:
        raster_cog.execute(spec, params)
    assert raised.value.error_code == "GREATLAKES_BATHYMETRY_EMPTY"


#: A quarter-degree CUDEM tile name, which is where the footprint comes from.
_CUDEM_TILE = "https://x/dem/ncei19_n30X00_w085X50_2019v1.tif"


def test_a_cudem_tile_name_is_its_footprint() -> None:
    """The quarter-degree square is read off the NW corner in the filename; a
    name that carries no corner is a tile whose footprint nobody can state."""
    assert cu.tile_box(_CUDEM_TILE) == pytest.approx(
        (-85.5, 29.75, -85.25, 30.0))
    assert cu.tile_box("https://x/dem/readme.tif") is None


def test_only_the_tiles_that_touch_the_aoi_are_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    far = "https://x/dem/ncei19_n42X00_w070X00_2019v1.tif"
    monkeypatch.setattr(cu, "_manifest_urls", lambda _t: [_CUDEM_TILE, far])
    assert cu.select_tiles((-85.45, 29.80, -85.30, 29.95)) == [_CUDEM_TILE]


def test_an_aoi_no_cudem_tile_reaches_refuses_rather_than_serving_something_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cu, "_manifest_urls", lambda _t: [_CUDEM_TILE])
    with pytest.raises(cu.CudemCoverageGapError) as excinfo:
        cu.read_cudem(None, {"bbox": (-80.0, 25.0, -79.9, 25.1)}, timeout_s=10.0)
    assert excinfo.value.error_code == "CUDEM_COVERAGE_GAP"


def test_a_cudem_tile_on_a_tidal_datum_refuses_rather_than_merging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_open(monkeypatch, 'COMPD_CS["NAD83 / UTM zone 16N + MLLW"]', "mllw")
    with pytest.raises(cu.CudemDatumError) as excinfo:
        cu.assert_navd88("/vsicurl/whatever.tif")
    assert excinfo.value.error_code == "CUDEM_DATUM_MISMATCH"


def test_a_cudem_tile_stating_no_vertical_cs_carries_the_collections_navd88(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row states NAVD88 for the collection, so a tile that states nothing is
    on it; only a tile stating a DIFFERENT zero is a mismatch."""
    _stub_open(monkeypatch, 'PROJCS["NAD83 / UTM zone 16N"]', "")
    assert cu.assert_navd88("/vsicurl/whatever.tif") is None


def test_the_etopo_tile_grid_is_arithmetic_not_an_index() -> None:
    assert et.tile_url(30.0, -90.0).endswith("ETOPO_2022_v1_15s_N30W090_surface.tif")
    urls = et.select_tiles((-85.75, 29.55, -85.25, 30.20))
    assert urls == [et.tile_url(30.0, -90.0), et.tile_url(45.0, -90.0)]


def test_an_etopo_ask_spanning_the_globe_refuses_rather_than_merging_it() -> None:
    with pytest.raises(et.EtopoInputError) as excinfo:
        et.validate_etopo(None, {"bbox": (-180.0, -60.0, 180.0, 60.0)})
    assert excinfo.value.error_code == "ETOPO_INPUT_INVALID"


def test_a_coast_with_no_published_regional_collection_refuses_by_name() -> None:
    with pytest.raises(rc.RegionalCoastalDemCoverageGapError) as excinfo:
        rc.validate_regional_coastal_dem(None, {"bbox": COASTAL_AOI})
    assert excinfo.value.error_code == "REGIONAL_COASTAL_DEM_COVERAGE_GAP"
    assert "funded" in str(excinfo.value)


def test_a_regional_collection_that_cannot_be_listed_refuses_rather_than_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fine bed silently skipped is a coarser answer nobody was told about."""
    import requests

    def _boom(*_a: object, **_kw: object) -> None:
        raise OSError("no route to host")

    monkeypatch.setattr(requests, "get", _boom)
    with pytest.raises(rc.RegionalCoastalDemUpstreamError):
        rc.select_tiles((-124.0, 41.5, -123.9, 41.6))


def test_every_bed_row_states_a_cell_and_a_frame_the_offset_service_names() -> None:
    """R8 and R17 on one line: a row states the cell its ask fetches and the zero
    it counts from, as a frame name an offset can be asked in."""
    from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

    frames = {"navd88", "egm2008", "lwd_igld85"}
    for row in ("fetch_cudem", "fetch_etopo", "fetch_regional_coastal_dem"):
        spec = load_spec_from_path(Path(
            f"trid3nt_server/tools/fetchers/ocean/{row}/source.yaml"))
        coverage = next(c for c in spec.coverage if c.data_class == "bathymetry")
        assert coverage.resolution_m and coverage.resolution_m > 0
        assert (coverage.datum or "").lower() in frames
        assert coverage.units == {"elevation": "m"}
        assert coverage.value_column == "elevation"


def test_the_composite_row_is_gone_from_the_registry() -> None:
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "fetch_topobathy" not in TOOL_REGISTRY


def _bed_rank(lon: float, lat: float) -> list[str]:
    from trid3nt_server.tools.search.match import Need, match, sources_with_coverage

    choice = match(
        Need(slot="bed", data_class="bathymetry", lon=lon, lat=lat,
             frame="NAVD88", mesh_m=50.0),
        sources_with_coverage())
    return [row.fetcher for row in choice.rows if not row.excluded]


def test_the_bed_ladder_over_a_coastal_box_is_the_rows_in_the_sorts_own_order(
) -> None:
    """The ladder the bed ingestion lays: every bathymetry row that survives the
    place, in rank order, over the terrain the probe finds under all of them.

    The sort reads facts, not names: the measured records come first and the
    finest of them leads, and the global relief MODEL ranks below every one of
    them because of how its numbers were come by."""
    from trid3nt_server.tools.search.match import Need, match, sources_with_coverage

    laid = _bed_rank(-85.50, 29.90)
    assert laid == ["fetch_ehydro_surveys", "fetch_bluetopo", "fetch_cudem",
                    "fetch_etopo"]

    terrain = match(
        Need(slot="bed terrain", data_class="terrain", lon=-85.50, lat=29.90,
             frame="NAVD88", mesh_m=50.0),
        sources_with_coverage())
    assert terrain.picked == "fetch_dem"


def test_a_coast_with_a_regional_integration_ranks_it_above_the_coarser_beds(
) -> None:
    """Crescent City: CUDEM's hosted collection omits this coast and the CoNED
    integration covers it, and the sort says so without anyone naming a row."""
    laid = _bed_rank(-124.20, 41.75)
    assert laid.index("fetch_regional_coastal_dem") < laid.index("fetch_bluetopo")
    assert laid[-1] == "fetch_etopo"
