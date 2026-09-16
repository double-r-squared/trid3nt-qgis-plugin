"""Unit tests for the vertical-datum check the bed slot runs on ingestion.

Covered: two sources on one datum, a source that states none refusing by name,
two that state different ones refusing with both spelled out, a single source,
a LAYER stating its own zero against a source NAME reading its spec row, and the
caller's own error family stamped onto the refusal."""

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


def test_a_source_publishing_no_shift_has_the_offset_fetched(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The RUNTIME declares the offset row: no question writes one."""
    from trid3nt_server.inputs.vertical_datum import onto_frame
    from trid3nt_server.tools import TOOL_REGISTRY

    asked: dict[str, object] = {}

    def _offset(**kwargs: object) -> dict[str, object]:
        asked.update(kwargs)
        return {"offset_m": 1.057, "from_frame": "NGVD29", "to_frame": "NAVD88",
                "source": "NOAA VDatum", "uncertainty_m": 0.053}

    monkeypatch.setitem(TOOL_REGISTRY, "fetch_vertical_datum_offset",
                        type("_Row", (), {"fn": staticmethod(_offset)})())
    aligned = onto_frame({"vertical_datum": "NGVD29", "name": "the gauge"},
                         "NAVD88", at=[-122.669, 45.518])
    assert aligned.shift_m == pytest.approx(1.057)
    assert asked["from_frame"] == "ngvd29" and asked["to_frame"] == "navd88"


def test_a_pair_nothing_measures_refuses_naming_both_frames(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from trid3nt_server.inputs.vertical_datum import onto_frame
    from trid3nt_server.tools import TOOL_REGISTRY

    monkeypatch.delitem(TOOL_REGISTRY, "fetch_vertical_datum_offset",
                        raising=False)
    with pytest.raises(DatumError) as caught:
        onto_frame({"vertical_datum": "CRD", "name": "the survey"}, "NAVD88")
    assert caught.value.error_code == "DATUMS_DIFFER"
    assert "CRD" in str(caught.value) and "NAVD88" in str(caught.value)
