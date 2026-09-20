"""Unit tests for the vertical-datum check the bed slot runs on ingestion.

Covered: two sources on one datum, a source that states none refusing by name,
two that state different ones refusing with both spelled out, a single source,
a LAYER stating its own zero against a source NAME reading its spec row, and the
caller's own error family stamped onto the refusal, and WHERE an offset row
is asked - on the owing source's own footprint, nearest the question's seed."""

from __future__ import annotations

import pytest

from trid3nt_server.inputs import vertical_datum
from trid3nt_server.inputs.vertical_datum import DatumError, one_datum


def _stating(datums: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vertical_datum, "_stated", lambda name: datums.get(name, ""))


def test_two_sources_on_one_datum(monkeypatch: pytest.MonkeyPatch) -> None:
    _stating({"gauge": "IGLD 1985", "bed": "IGLD 1985"}, monkeypatch)
    assert one_datum("gauge", "bed") == "IGLD 1985"


def test_one_source_states_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    _stating({"bed": "NAVD88"}, monkeypatch)
    assert one_datum("bed") == "NAVD88"


def test_an_unstated_datum_refuses_and_names_the_row(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _stating({"gauge": "IGLD 1985"}, monkeypatch)
    with pytest.raises(DatumError) as caught:
        one_datum("gauge", "bed")
    assert caught.value.error_code == "DATUM_UNSTATED"
    assert "bed" in str(caught.value)


def test_differing_datums_refuse_with_both_spelled_out(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _stating({"gauge": "IGLD 1985", "bed": "NAVD88"}, monkeypatch)
    with pytest.raises(DatumError) as caught:
        one_datum("gauge", "bed")
    assert caught.value.error_code == "DATUMS_DIFFER"
    assert "IGLD 1985" in str(caught.value) and "NAVD88" in str(caught.value)


def test_the_callers_error_family_is_stamped(
        monkeypatch: pytest.MonkeyPatch) -> None:
    _stating({"gauge": "IGLD 1985", "bed": "NAVD88"}, monkeypatch)
    with pytest.raises(DatumError) as caught:
        one_datum("gauge", "bed", code_prefix="TELEMAC3D_")
    assert caught.value.error_code == "TELEMAC3D_DATUMS_DIFFER"


def test_a_layer_states_its_own_datum_and_a_name_reads_its_source_row(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A survey carries a local project datum nothing in its spec row knows, so
    what the caller holds is what is read; a bare name still reads the row."""
    from trid3nt_contracts.execution import LayerURI
    from trid3nt_server.inputs.vertical_datum import datum_of

    _stating({"fetch_dem": "NAVD88"}, monkeypatch)
    survey = LayerURI(layer_id="s", name="the survey", layer_type="raster",
                      uri="s3://x/s.tif", vertical_datum="CRD")
    assert datum_of(survey) == "CRD"
    assert datum_of("fetch_dem") == "NAVD88"
    with pytest.raises(DatumError) as caught:
        one_datum(survey, "fetch_dem")
    assert caught.value.error_code == "DATUMS_DIFFER"
    assert "the survey" in str(caught.value) and "CRD" in str(caught.value)


#: The Willamette channel survey's own published shift: USACE states in the
#: survey's metadata that the Columbia River Datum sits 5.28 ft above NAVD88 at
#: river mile 9.7, and nothing else knows it - VDatum serves no project datum.
_CRD_TO_NAVD88_M = 1.6093


class _Survey:
    """A survey layer as the eHydro fetcher publishes it."""

    vertical_datum = "SD (Columbia River Datum: CRD)"
    datum_offset_m = _CRD_TO_NAVD88_M
    datum_offset_frame = "NAVD88"
    name = "channel survey soundings"


def test_a_source_on_the_runs_own_frame_is_not_shifted() -> None:
    from trid3nt_server.inputs.vertical_datum import onto_frame

    aligned = onto_frame({"vertical_datum": "NAVD88 (metres, positive up)",
                          "name": "3dep"}, "NAVD88")
    assert (aligned.shift_m, aligned.note) == (0.0, "")


def test_a_survey_reaches_the_runs_frame_through_the_shift_it_publishes() -> None:
    from trid3nt_server.inputs.vertical_datum import onto_frame

    aligned = onto_frame(_Survey(), "NAVD88")
    assert aligned.shift_m == pytest.approx(_CRD_TO_NAVD88_M)
    assert aligned.datum == "NAVD88"
    assert "own metadata" in aligned.note


def test_a_frame_the_run_does_not_state_asks_nothing() -> None:
    from trid3nt_server.inputs.vertical_datum import onto_frame

    assert onto_frame(_Survey(), None).shift_m == 0.0


def test_a_source_publishing_no_shift_owes_an_offset_row() -> None:
    """The RUNTIME declares the row; the coercion reads it and fetches nothing."""
    from trid3nt_server.inputs.vertical_datum import offset_ask

    ask = offset_ask({"vertical_datum": "NGVD29", "name": "the gauge"},
                     "NAVD88", at=[-122.669, 45.518])
    assert ask == {"point": [-122.669, 45.518], "from_frame": "ngvd29",
                   "to_frame": "navd88"}


def test_the_coercion_reads_the_rows_value_and_fetches_nothing(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from trid3nt_server.inputs import vertical_datum as vd
    from trid3nt_server.tools import TOOL_REGISTRY

    def _never(**_kwargs: object) -> object:
        raise AssertionError("the coercion called the fetch")

    monkeypatch.setitem(TOOL_REGISTRY, "fetch_vertical_datum_offset",
                        type("_Row", (), {"fn": staticmethod(_never)})())
    aligned = vd.onto_frame(
        {"vertical_datum": "NGVD29", "name": "the gauge"}, "NAVD88",
        offset={"offset_m": 1.057, "from_frame": "NGVD29", "to_frame": "NAVD88",
                "source": "NOAA VDatum", "uncertainty_m": 0.053})
    assert aligned.shift_m == pytest.approx(1.057)
    assert "NOAA VDatum" in aligned.note


def test_a_source_that_publishes_its_own_shift_owes_no_row() -> None:
    from trid3nt_server.inputs.vertical_datum import offset_ask

    assert offset_ask(_Survey(), "NAVD88", at=[-122.669, 45.518]) is None


def test_a_source_already_on_the_runs_frame_owes_no_row() -> None:
    from trid3nt_server.inputs.vertical_datum import offset_ask

    assert offset_ask({"vertical_datum": "NAVD88 (metres, positive up)",
                       "name": "3dep"}, "NAVD88", at=[-122.669, 45.518]) is None


def test_a_frame_the_fetch_does_not_serve_owes_no_row() -> None:
    """A district's project datum is not a VDatum frame: no row, and the
    alignment below refuses naming both."""
    from trid3nt_server.inputs.vertical_datum import offset_ask

    assert offset_ask({"vertical_datum": "SD (Columbia River Datum: CRD)",
                       "name": "the survey"}, "NAVD88",
                      at=[-122.669, 45.518]) is None


def test_a_pair_nothing_measures_refuses_naming_both_frames() -> None:
    from trid3nt_server.inputs.vertical_datum import onto_frame

    with pytest.raises(DatumError) as caught:
        onto_frame({"vertical_datum": "CRD", "name": "the survey"}, "NAVD88")
    assert caught.value.error_code == "DATUMS_DIFFER"
    assert "CRD" in str(caught.value) and "NAVD88" in str(caught.value)


def test_a_supplied_surface_states_no_per_feature_zero(tmp_path) -> None:
    """A per-feature zero is a vector record's fact, so a raster is not read for
    one: a bathymetry handed in as a surface states no datum and owes no row."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    from trid3nt_server.inputs.vertical_datum import offset_ask, record_datum

    path = tmp_path / "bed.tif"
    with rasterio.open(path, "w", driver="GTiff", width=4, height=4, count=1,
                       dtype="float32", crs="EPSG:26910",
                       transform=from_origin(470_000.0, 5_048_000.0, 20.0, 20.0)
                       ) as dst:
        dst.write(np.full((4, 4), -3.0, dtype="float32"), 1)
    assert record_datum(str(path)) == ""
    assert offset_ask(str(path), "NAVD88", at=[-123.2, 45.5]) is None


#: A REACH of the St. Clair River: the ground a survey measured, the seed the
#: question names on it, and a domain box whose centre the survey misses.
_SURVEYED_EAST_OF = -82.43
_SEED_ON_THE_SURVEY = (-82.41, 42.99)
_SEED_OFF_THE_SURVEY = (-82.47, 42.99)
_DOMAIN_BOX = (-82.50, 42.93, -82.40, 43.04)


def _half_measured(tmp_path) -> dict[str, str]:
    """A survey raster measuring only the east half of its own rectangle."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "survey.tif"
    values = np.full((9, 10), -8.0, dtype="float32")
    values[:, :5] = -9999.0
    with rasterio.open(path, "w", driver="GTiff", width=10, height=9, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=-9999.0,
                       transform=from_origin(-82.48, 43.03, 0.01, 0.01)) as dst:
        dst.write(values, 1)
    return {"uri": str(path), "vertical_datum": "IGLD85", "name": "the survey"}


def _standing_on_the_box(monkeypatch: pytest.MonkeyPatch) -> None:
    from trid3nt_server.workflows.runtime import domain as runtime_domain

    monkeypatch.setattr(runtime_domain, "current_domain",
                        lambda: runtime_domain.Domain(bbox=_DOMAIN_BOX))


def test_a_footprint_missing_the_box_centre_is_asked_at_the_seed(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The survey measured nothing at the domain's centre and measured the seed,
    so the seed is where its offset is asked."""
    from trid3nt_server.inputs.vertical_datum import offset_ask

    _standing_on_the_box(monkeypatch)
    ask = offset_ask(_half_measured(tmp_path), "NAVD88", at=_SEED_ON_THE_SURVEY)
    west, south, east, north = _DOMAIN_BOX
    assert (west + east) / 2.0 < _SURVEYED_EAST_OF
    assert ask["point"] == pytest.approx(list(_SEED_ON_THE_SURVEY))
    assert ask["from_frame"] == "igld85" and ask["to_frame"] == "navd88"


def test_a_seed_off_the_footprint_is_asked_at_the_nearest_measured_point(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The seed sits where the survey measured nothing: the ask walks to the
    nearest point of the survey's OWN footprint, not to a centre of anything."""
    from trid3nt_server.inputs.vertical_datum import offset_ask

    _standing_on_the_box(monkeypatch)
    lon, lat = offset_ask(_half_measured(tmp_path), "NAVD88",
                          at=_SEED_OFF_THE_SURVEY)["point"]
    assert lon == pytest.approx(_SURVEYED_EAST_OF)
    assert lat == pytest.approx(_SEED_OFF_THE_SURVEY[1])


def test_a_point_the_service_does_not_reach_refuses_naming_it(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """VDatum answers an uncovered point with a sentinel height, and the refusal
    that reads it states the point that was asked."""
    import json

    from trid3nt_server.inputs.vertical_datum import OFFSET_FETCH, offset_ask
    from trid3nt_server.tools.fetchers._router.registration import get_spec
    from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError
    from trid3nt_server.tools.fetchers.ocean.fetch_vertical_datum_offset.hooks \
        import record

    _standing_on_the_box(monkeypatch)
    ask = offset_ask(_half_measured(tmp_path), "NAVD88", at=_SEED_OFF_THE_SURVEY)
    with pytest.raises(RouterEmptyError) as caught:
        record(get_spec(OFFSET_FETCH), dict(ask),
               [json.dumps({"t_z": -999999.0}).encode("utf-8")])
    lon, lat = ask["point"]
    assert f"({lon}, {lat})" in str(caught.value)
