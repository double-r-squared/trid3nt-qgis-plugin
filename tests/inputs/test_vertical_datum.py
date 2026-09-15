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
