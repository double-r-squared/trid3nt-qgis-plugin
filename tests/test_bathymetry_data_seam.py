"""The topobathy row's bed seam: the BlueTopo source and the per-class ladders.

Offline. Every network edge is a fixture: the tile scheme is a GeoPackage built
here, and a tile header is a stub whose only content is the datum string the
gate reads. What is exercised is the DECISIONS - which ladder governs which
water, which rung ships, what a partial cover reports, what a refusal names.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trid3nt_server.fallbacks import registered_ladders, resolve_ladder
from trid3nt_server.tools.fetchers._router.hooks import bluetopo as bt
from trid3nt_server.tools.fetchers._router.hooks import topobathy as tb
from trid3nt_server.tools.fetchers._router.hooks import topobathy_class as tc
from trid3nt_server.tools.fetchers._router.hooks.topobathy import (
    TopobathyCoverageGapError,
    validate_topobathy,
)

COASTAL_AOI = (-85.75, 29.55, -85.25, 30.20)


# --------------------------------------------------------------------------- #
# The classifier: from the rows the reach chain already holds.
# --------------------------------------------------------------------------- #


def test_a_tidal_ftype_classifies_the_reach_as_coastal_estuary() -> None:
    for ftype in sorted(tc.TIDAL_FTYPES):
        assert tc.classify_water_body(
            water_features=[{"properties": {"ftype": ftype}}]
        ) == "coastal_estuary"


def test_no_mapped_water_surface_classifies_the_reach_as_a_small_inland_stream() -> None:
    # A channel too narrow to be mapped as an area is a flowline only, and the
    # water fetcher says so in its own caveats: the absence is a real answer.
    assert tc.classify_water_body(
        water_features=[], mapped_water_fraction=0.0
    ) == "small_inland_stream"


def test_a_wide_inland_river_refuses_naming_what_would_have_decided_it() -> None:
    with pytest.raises(tc.WaterBodyClassUnknown) as excinfo:
        tc.classify_water_body(
            water_features=[{"properties": {"ftype": 460}}],
            mapped_water_fraction=0.9,
        )
    assert excinfo.value.missing, "a refusal that names nothing missing is useless"
    assert "National Channel Framework" in " ".join(excinfo.value.missing)


def test_the_classifier_never_guesses_a_class_from_an_unknown_ftype() -> None:
    with pytest.raises(tc.WaterBodyClassUnknown):
        tc.classify_water_body(water_features=[{"properties": {"ftype": 378}}])


# --------------------------------------------------------------------------- #
# The per-class ladders.
# --------------------------------------------------------------------------- #


def test_the_coastal_ladder_puts_bluetopo_above_the_cudem_composite() -> None:
    ladder = resolve_ladder("fetch_topobathy", {"water_body_class": "coastal_estuary"})
    assert ladder is not None
    names = [r.name for r in ladder.rungs]
    assert names.index("bluetopo") < names.index("cudem_nearshore")
    assert ladder.primary_rung.name == "bluetopo"


def test_falling_from_bluetopo_to_the_cudem_composite_is_a_declared_cross_dataset_rung() -> None:
    ladder = resolve_ladder("fetch_topobathy", {"water_body_class": "coastal_estuary"})
    assert ladder is not None
    rung = ladder.alternative("cudem_nearshore")
    # It must be an ALTERNATIVE (gated, permitted by name), not something the
    # capability may lay down on its own.
    assert rung is not None and rung.consequence == "cross_dataset"


def test_no_ladder_anywhere_ships_an_ehydro_rung() -> None:
    # eHydro's queryable layer carries a horizontal projection and NO vertical
    # datum, so its bed cannot state its datum; the rung STOPPED on that.
    for ladder in registered_ladders().values():
        assert not any("ehydro" in r.name for r in ladder.rungs)


def test_a_navigable_river_has_no_ladder_and_refuses_naming_its_stopped_primary() -> None:
    # With eHydro stopped only BlueTopo is left, and one source is not a
    # degradation path: a ladder with nothing under its primary declares no
    # alternative for anyone to permit.
    assert "navigable_river" not in tc.CLASS_LADDERS
    reason = tc.STOPPED_CLASSES["navigable_river"]
    assert "eHydro" in reason and "vertical datum" in reason


def test_a_small_inland_stream_has_no_ladder_and_refuses_naming_both_gaps() -> None:
    assert "small_inland_stream" not in tc.CLASS_LADDERS
    reason = tc.STOPPED_CLASSES["small_inland_stream"]
    assert "NXSDB" in reason and "synthetic" in reason


def test_the_synthetic_slot_is_stated_as_deferred_rather_than_forgotten() -> None:
    """No synthetic bathymetry is produced, by ruling rather than by backlog.

    The distinction is the whole value of stating the empty slot: a rung nobody
    has got to yet invites the next hand to fill it, and a rung somebody decided
    to leave empty tells them the decision is not theirs to make.
    """
    reason = tc.STOPPED_CLASSES["small_inland_stream"]
    assert "DEFERRED BY RULING" in reason
    assert "does not exist yet" not in reason


@pytest.mark.parametrize(
    ("water_body_class", "named"),
    [("small_inland_stream", "NXSDB"), ("navigable_river", "eHydro")],
)
def test_a_stopped_class_refuses_before_the_cache_and_before_the_network(
    water_body_class: str, named: str
) -> None:
    with pytest.raises(TopobathyCoverageGapError) as excinfo:
        validate_topobathy(None, {"bbox": COASTAL_AOI,
                                  "water_body_class": water_body_class})
    assert excinfo.value.error_code == "TOPOBATHY_COVERAGE_GAP"
    assert named in str(excinfo.value)


def test_every_declared_class_either_ladders_or_stops_by_name() -> None:
    # A class the row can state must resolve to something: a ladder that serves
    # it, or a stop that says what is missing. Silence is the third answer this
    # forbids.
    for name in tc.WATER_BODY_CLASSES:
        assert (name in tc.CLASS_LADDERS) != (name in tc.STOPPED_CLASSES)


def test_an_undeclared_class_keeps_the_rows_unclassed_ladder() -> None:
    # A class nobody declared is not a class anybody may assume, so a request
    # that states none is served exactly as before.
    default = registered_ladders()["fetch_topobathy"]
    assert resolve_ladder("fetch_topobathy", {}) is default
    assert resolve_ladder("fetch_topobathy", {"water_body_class": None}) is default


def test_every_class_ladder_ends_at_refuse_with_the_rows_own_error_code() -> None:
    for ladder in tc.CLASS_LADDERS.values():
        assert ladder.terminal.consequence == "refuse"
        assert ladder.refuse_error_code == "TOPOBATHY_COVERAGE_GAP"


# --------------------------------------------------------------------------- #
# The BlueTopo source: discovery, the datum gate, and what a partial cover says.
# --------------------------------------------------------------------------- #


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
    assert tb.painted_fraction(grid) == pytest.approx(0.60)
    assert tb.painted_fraction(numpy.full((4, 4), numpy.float32("nan"))) == 0.0
    assert tb.painted_fraction(numpy.zeros((0, 0))) == 0.0


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
        tb, "_composite_sources_to_array",
        lambda sources, *_a, **_kw: (half, None, "EPSG:4326",
                                     [True] * len(sources),
                                     [None] * len(sources)))
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


def test_a_partial_bluetopo_cover_is_reported_as_a_gap_not_a_whole_bed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trid3nt_contracts.execution import BlueTopoResult
    from trid3nt_server.tools import TOOL_REGISTRY

    partial = BlueTopoResult(
        layer_id="x", name="x", layer_type="raster", uri="s3://x.tif",
        coverage_fraction=0.42,
    )
    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_bluetopo",
        TOOL_REGISTRY["fetch_bluetopo"].__class__(
            **{**TOOL_REGISTRY["fetch_bluetopo"].__dict__,
               "fn": lambda **_kw: partial}),
    )
    with pytest.raises(TopobathyCoverageGapError) as excinfo:
        tc.serve_bluetopo_bed(bbox=COASTAL_AOI)
    # The walker reads BOTH off it: how much was covered, and what the gap is.
    assert excinfo.value.covered_fraction == pytest.approx(0.42)
    assert "42.0%" in excinfo.value.gap_note


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


# --------------------------------------------------------------------------- #
# The declaration itself.
# --------------------------------------------------------------------------- #


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
    assert spec.corpus, "a new source needs its retrieval phrasings"


def test_the_topobathy_row_declares_the_water_body_class_it_ladders_on() -> None:
    from trid3nt_server.tools.fetchers._router.spec import load_spec_from_path

    spec = load_spec_from_path(Path(
        "trid3nt_server/tools/fetchers/ocean/fetch_topobathy/source.yaml"))
    param = spec.params["water_body_class"]
    assert sorted(param.values or []) == sorted(tc.WATER_BODY_CLASSES)
