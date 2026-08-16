"""fetch_lter_records record fold (ADR 0203): the LTER/EDI reader via the router.

Offline: a synthetic EML metadata body + a small TSV data entity stand in for the
DataONE resolve responses (the shared transport ``_get_raw`` is monkeypatched by
URL), and the in-memory read_through injector caches the record dict. The real
DataONE network path is unchanged (exercised live). Covers package-id parsing (both
spellings), EML entity extraction + selection, the delimited parse + window filter +
per-column summary, and the route() -> dict record shape.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from trid3nt_server.data.fetchers._router import router
from trid3nt_server.data.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
)
from trid3nt_server.data.fetchers._router.executors import http_json
from trid3nt_server.data.fetchers._router.hooks import lter_records as lr
from trid3nt_server.data.fetchers._router.spec import load_spec_from_path

LTER_SPEC = load_spec_from_path(
    Path(__file__).resolve().parents[1]
    / "trid3nt_server/data/fetchers/hydrology/fetch_lter_records/source.yaml"
)

# A minimal EML with one dataTable (tab-delimited, 1 header line) carrying a Date,
# a Discharge (m3/s), a Water_Level (m), and a Flag column -- the Coweeta shape.
_EML = """<?xml version="1.0"?>
<eml:eml xmlns:eml="eml://ecoinformatics.org/eml-2.1.1">
 <dataset>
  <dataTable>
   <entityName>3037_BC9</entityName>
   <physical><distribution><online>
     <url>https://pasta.lternet.edu/package/data/eml/knb-lter-cwt/3037/19/DATAHASH</url>
   </online></distribution>
    <dataFormat><textFormat>
      <numHeaderLines>1</numHeaderLines>
      <simpleDelimited><fieldDelimiter>\\t</fieldDelimiter></simpleDelimited>
    </textFormat></dataFormat>
   </physical>
   <attributeList>
    <attribute><attributeName>Date</attributeName></attribute>
    <attribute><attributeName>Discharge</attributeName>
      <measurementScale><ratio><unit><standardUnit>cubicMetersPerSecond</standardUnit></unit></ratio></measurementScale>
    </attribute>
    <attribute><attributeName>Water_Level</attributeName>
      <measurementScale><ratio><unit><standardUnit>meter</standardUnit></unit></ratio></measurementScale>
    </attribute>
    <attribute><attributeName>Flag_Discharge</attributeName></attribute>
   </attributeList>
  </dataTable>
  <otherEntity>
   <entityName>3037.kml</entityName>
   <physical><distribution><online>
     <url>https://pasta.lternet.edu/package/data/eml/knb-lter-cwt/3037/19/KMLHASH</url>
   </online></distribution></physical>
  </otherEntity>
 </dataset>
</eml:eml>"""

_TSV = "\n".join(
    [
        "Date\tDischarge\tWater_Level\tFlag_Discharge",
        "2015-12-22 00:00:00\t0.31\t0.12\t",
        "2015-12-24 07:00:00\t8.60\t0.55\t",
        "2015-12-30 23:00:00\t1.70\t0.20\t",
        "2016-01-05 00:00:00\t0.90\t0.15\t",  # outside the Dec window
    ]
)

_META_URL = lr._dataone_resolve_url(lr._metadata_pid("knb-lter-cwt", "3037", "19"))
_DATA_URL = lr._dataone_resolve_url(
    "https://pasta.lternet.edu/package/data/eml/knb-lter-cwt/3037/19/DATAHASH"
)


def _inject_read_through(monkeypatch, store: dict[str, bytes]):
    from trid3nt_server.data.cache import (
        CACHE_BUCKET, ReadThroughResult, cache_path, compute_cache_key, is_cacheable,
    )
    now = _dt.datetime(2026, 8, 8, 12, 0, 0, tzinfo=_dt.timezone.utc)

    def patched(metadata, params, ext, fetch_fn, **kw):
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        source_id = metadata.source_class or metadata.name
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=now)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{CACHE_BUCKET}/{path}"
        if path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    monkeypatch.setattr(router, "read_through", patched)


def _inject_transport(monkeypatch):
    """Route the metadata + data resolve URLs to the synthetic bodies (both the
    resolve-phase _get and the record-phase _get share http_json._get_raw)."""
    from trid3nt_server.data.fetchers._router.executors import chained_resolution

    def fake(plan):
        if plan.url == _META_URL:
            return _EML.encode("utf-8")
        if plan.url == _DATA_URL:
            return _TSV.encode("utf-8")
        raise AssertionError(f"unexpected URL {plan.url}")

    monkeypatch.setattr(http_json, "_get_raw", fake)
    monkeypatch.setattr(chained_resolution, "_get", lambda spec, plan: fake(plan))


# --------------------------------------------------------------------------- #
# Registration + shape.
# --------------------------------------------------------------------------- #


def test_lter_promoted_as_record_spec():
    from trid3nt_server.data import TOOL_REGISTRY

    entry = TOOL_REGISTRY["fetch_lter_records"]
    assert entry.metadata.source_class == "lter_records"
    assert entry.fn.__module__.endswith("_promoted.fetch_lter_records")


def test_lter_shape_is_record():
    assert LTER_SPEC.shape == "record"
    assert LTER_SPEC.output.layer_type == "record"
    assert LTER_SPEC.hooks.resolve_build == "lter_records.resolve_build"
    assert LTER_SPEC.hooks.build_request == "lter_records.build_request"
    assert LTER_SPEC.hooks.record == "lter_records.build_record"


# --------------------------------------------------------------------------- #
# Pure hooks.
# --------------------------------------------------------------------------- #


def test_parse_package_id_both_spellings():
    assert lr._parse_package_id("P", "knb-lter-cwt.3037/19") == ("knb-lter-cwt", "3037", "19")
    assert lr._parse_package_id("P", "knb-lter-cwt.3037.19") == ("knb-lter-cwt", "3037", "19")


def test_parse_package_id_rejects_malformed():
    for bad in ("knb-lter-cwt", "justascope", ""):
        with pytest.raises(RouterInputError):
            lr._parse_package_id("P", bad)


def test_interpret_delimiter():
    assert lr._interpret_delimiter("\\t") == "\t"
    assert lr._interpret_delimiter(",") == ","
    assert lr._interpret_delimiter(None) == "\t"


def test_entity_blocks_and_selection():
    blocks = lr._entity_blocks(_EML)
    assert [b["name"] for b in blocks] == ["3037_BC9", "3037.kml"]
    dt = blocks[0]
    assert dt["delimiter"] == "\t" and dt["skip"] == 1
    assert dt["units"]["Discharge"] == "cubicMetersPerSecond"
    assert dt["units"]["Water_Level"] == "meter"
    # default -> first dataTable; index; name substring.
    assert lr._select_entity("P", blocks, None)["name"] == "3037_BC9"
    assert lr._select_entity("P", blocks, "2")["name"] == "3037.kml"
    assert lr._select_entity("P", blocks, "BC9")["name"] == "3037_BC9"
    with pytest.raises(RouterInputError):
        lr._select_entity("P", blocks, "nope")


def test_pick_value_cols_drops_flags_and_date():
    cols = ["Date", "Discharge", "Water_Level", "Flag_Discharge"]
    units = {"Discharge": "m3/s", "Water_Level": "m"}
    vc = lr._pick_value_cols(cols, units, "Date", None)
    assert vc == ["Discharge", "Water_Level"]  # Flag_ and Date dropped


def test_resolve_parse_merges_data_url_and_hints():
    update = lr.resolve_parse(LTER_SPEC, {"package_id": "knb-lter-cwt.3037/19"}, [_EML.encode()])
    assert update["_data_url"] == _DATA_URL
    assert update["_delimiter"] == "\t" and update["_skip"] == 1
    assert update["_entity_name"] == "3037_BC9"


# --------------------------------------------------------------------------- #
# End-to-end route() -> record dict.
# --------------------------------------------------------------------------- #


def test_route_returns_discharge_series(monkeypatch):
    _inject_read_through(monkeypatch, {})
    _inject_transport(monkeypatch)

    result = router.route(
        LTER_SPEC,
        {
            "package_id": "knb-lter-cwt.3037/19",
            "start_date": "2015-12-22",
            "end_date": "2015-12-30",
        },
    )
    assert isinstance(result, dict)
    assert result["entity"] == "3037_BC9"
    assert result["date_col"] == "Date"
    assert "Discharge" in result["value_columns"]
    assert result["units"]["Discharge"] == "cubicMetersPerSecond"
    assert result["n_rows"] == 3  # the Jan row is outside the window
    assert result["summary"]["Discharge"]["peak"] == pytest.approx(8.60)
    assert result["summary"]["Discharge"]["min"] == pytest.approx(0.31)
    assert result["first_ts"] == "2015-12-22 00:00:00"
    assert result["last_ts"] == "2015-12-30 23:00:00"


def test_route_value_cols_override(monkeypatch):
    _inject_read_through(monkeypatch, {})
    _inject_transport(monkeypatch)
    result = router.route(
        LTER_SPEC, {"package_id": "knb-lter-cwt.3037.19", "value_cols": ["Water_Level"]}
    )
    assert result["value_columns"] == ["Water_Level"]
    assert result["summary"]["Water_Level"]["peak"] == pytest.approx(0.55)


def test_route_empty_window_raises(monkeypatch):
    _inject_read_through(monkeypatch, {})
    _inject_transport(monkeypatch)
    with pytest.raises(RouterEmptyError) as exc:
        router.route(
            LTER_SPEC,
            {"package_id": "knb-lter-cwt.3037/19", "start_date": "1990-01-01", "end_date": "1990-01-02"},
        )
    assert exc.value.error_code == "LTER_RECORDS_EMPTY"
