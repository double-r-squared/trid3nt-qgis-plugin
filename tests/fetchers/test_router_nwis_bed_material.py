"""``fetch_nwis_bed_material``: the Result/Station join and its three drops.

The Result service reports one row per grain-size class with the class as free
text and no coordinates; the Station service carries the site's location and its
own datums. Covered here: the basis-text parse, the Bottom-material/percent
filters (each drop counted, never folded silently in), the station join dropping
an unmatched sample, and the honest empty refusal."""

from __future__ import annotations

import pandas as pd
import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError, RouterInputError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.hydrology.fetch_nwis_bed_material import hooks as nb


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_nwis_bed_material"]


def _params(**over):
    base = {"bbox": [-121.4, 37.9, -121.2, 38.1], "characteristic": "Bed sediment particle size"}
    base.update(over)
    return base


def _station_df():
    return pd.DataFrame([
        {"MonitoringLocationIdentifier": "USGS-11304810",
         "MonitoringLocationName": "SAN JOAQUIN R BL GARWOOD BRIDGE A STOCKTON CA",
         "MonitoringLocationTypeName": "Stream",
         "LatitudeMeasure": 37.93547987, "LongitudeMeasure": -121.3302245,
         "HorizontalCoordinateReferenceSystemDatumName": "NAD83",
         "VerticalMeasure/MeasureValue": 0, "VerticalMeasure/MeasureUnitCode": "feet",
         "VerticalCoordinateReferenceSystemDatumName": "NAVD88",
         "HUCEightDigitCode": "18040003"},
    ])


def _result_row(site, activity, basis, value, unit="%", subdivision="Bottom material",
                 date="2010-09-15"):
    return {"MonitoringLocationIdentifier": site, "ActivityIdentifier": activity,
            "ActivityStartDate": date, "ActivityMediaSubdivisionName": subdivision,
            "ResultMeasureValue": value, "ResultMeasure/MeasureUnitCode": unit,
            "ResultParticleSizeBasisText": basis, "CharacteristicName": "Bed sediment particle size",
            "SampleCollectionEquipmentName": "Sampler, US BMH-60"}


def _result_df(rows):
    return pd.DataFrame(rows)


def test_an_absent_bbox_refuses_before_the_network(spec):
    with pytest.raises(RouterInputError) as excinfo:
        nb.validate(spec, {"characteristic": "Bed sediment particle size"})
    assert excinfo.value.error_code == "NWIS_BED_MATERIAL_INPUT_ERROR"


def test_an_empty_characteristic_refuses_before_the_network(spec):
    with pytest.raises(RouterInputError) as excinfo:
        nb.validate(spec, {"bbox": [-121.4, 37.9, -121.2, 38.1], "characteristic": ""})
    assert excinfo.value.error_code == "NWIS_BED_MATERIAL_INPUT_ERROR"


def test_a_well_formed_request_validates_clean(spec):
    nb.validate(spec, _params())


@pytest.mark.parametrize("basis,column", [
    ("< 2 mm", "pct_finer_2mm"),
    ("<1 mm", "pct_finer_1mm"),
    ("<0.0625mm", "pct_finer_0p0625mm"),
    ("< 0.0625 mm", "pct_finer_0p0625mm"),
    ("< 4.75 mm", "pct_finer_4p75mm"),
    ("< 22.4 mm", "pct_finer_22p4mm"),
])
def test_the_three_spellings_of_a_threshold_parse_to_the_same_column(basis, column):
    assert nb._basis_column(basis) == column


@pytest.mark.parametrize("basis", ["0.063-4mm", "coarse sand", "phi 2", ""])
def test_a_non_less_than_basis_is_not_forced_onto_a_threshold(basis):
    assert nb._basis_column(basis) is None


def test_read_joins_station_geometry_onto_the_bottom_material_percent_samples(spec, monkeypatch):
    import dataretrieval.wqp as wqp

    monkeypatch.setattr(wqp, "what_sites", lambda **kw: (_station_df(), None))
    monkeypatch.setattr(wqp, "get_results", lambda **kw: (_result_df([
        _result_row("USGS-11304810", "A1", "< 2 mm", 100),
        _result_row("USGS-11304810", "A1", "<0.0625mm", 7),
        # dropped: a mass reading, not percent
        _result_row("USGS-11304810", "A1", "< 1 mm", 50, unit="g"),
        # dropped: Surface Water, not Bottom material
        _result_row("USGS-11304810", "A1", "< 4 mm", 90, subdivision="Surface Water"),
        # dropped: not a recognized less-than threshold
        _result_row("USGS-11304810", "A1", "0.063-4mm", 33),
        # dropped: no Station record for this site
        _result_row("USGS-99999999", "B1", "< 2 mm", 80),
    ]), None))

    rows = nb.read(spec, _params(), timeout_s=30.0)
    assert len(rows) == 1
    props = rows[0]["properties"]
    assert rows[0]["geometry"] == {"type": "Point", "coordinates": [-121.3302245, 37.93547987]}
    assert props["site_id"] == "USGS-11304810"
    assert props["horizontal_datum"] == "NAD83"
    assert props["vertical_datum"] == "NAVD88"
    assert props["pct_finer_2mm"] == 100.0
    assert props["pct_finer_0p0625mm"] == 7.0
    assert props["pct_finer_1mm"] is None  # the gram row never lands
    assert props["n_size_classes"] == 2


def test_read_with_no_surviving_sample_refuses_by_name(spec, monkeypatch):
    import dataretrieval.wqp as wqp

    monkeypatch.setattr(wqp, "what_sites", lambda **kw: (_station_df(), None))
    monkeypatch.setattr(wqp, "get_results", lambda **kw: (_result_df([]), None))
    with pytest.raises(RouterEmptyError) as excinfo:
        nb.read(spec, _params(), timeout_s=30.0)
    assert excinfo.value.error_code == "NWIS_BED_MATERIAL_NO_SAMPLES"


def test_the_source_surfaces_from_its_own_corpus_phrasings():
    from pathlib import Path

    import yaml

    from trid3nt_server.tools.search.search_tools import search_tools as dd
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    dd._get_index()
    here = Path(nb.__file__).resolve().parent
    queries = (yaml.safe_load((here / "corpus.yaml").read_text()) or {})["fetch_nwis_bed_material"]
    assert queries
    assert any("fetch_nwis_bed_material" in retrieve_visible_tools(q, None, 8) for q in queries), (
        "fetch_nwis_bed_material surfaces in NO top-8 for any of its corpus queries")
