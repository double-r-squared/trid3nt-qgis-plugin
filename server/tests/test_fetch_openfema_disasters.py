"""Tests for ``fetch_openfema_disasters`` — FEMA disaster declarations as a
county-polygon FlatGeobuf (OpenFEMA + TIGERweb join).

Covers (per the gate):
  - correctness on synthetic declaration records (per-county aggregation) and a
    synthetic county-polygon join -> FlatGeobuf round-trip;
  - honest-empty (no county-keyed declarations -> typed error; statewide-only
    rows excluded);
  - input validation (selector, bbox, state code, incident type, year);
  - the OData filter / URL builders and the bbox -> states derivation.

These tests are network-free: they exercise the parse/aggregate/join/build
helpers and the validation surface directly with synthetic inputs. The live
OpenFEMA + TIGERweb + S3 round-trip was proven separately in the promotion
prototype.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers.hazard import fetch_openfema_disasters as m


# ---------------------------------------------------------------------------
# Synthetic fixtures.
# ---------------------------------------------------------------------------


def _decl(
    *,
    state="FL",
    fips_state="12",
    fips_county="086",
    disaster=4337,
    incident="Hurricane",
    dtype="DR",
    date="2017-09-10T00:00:00.000Z",
    area="Miami-Dade (County)",
    ia=False,
    pa=True,
):
    """One synthetic OpenFEMA declaration record."""
    return {
        "state": state,
        "fipsStateCode": fips_state,
        "fipsCountyCode": fips_county,
        "disasterNumber": disaster,
        "incidentType": incident,
        "declarationType": dtype,
        "declarationDate": date,
        "designatedArea": area,
        "iaProgramDeclared": ia,
        "paProgramDeclared": pa,
    }


def _county_feature(geoid, name, *, square_at=(-80.5, 25.5)):
    """A synthetic TIGERweb county GeoJSON feature (a 0.2-deg square polygon)."""
    x0, y0 = square_at
    ring = [
        [x0, y0], [x0 + 0.2, y0], [x0 + 0.2, y0 + 0.2], [x0, y0 + 0.2], [x0, y0]
    ]
    return {
        "type": "Feature",
        "properties": {"GEOID": geoid, "NAME": name, "STATE": geoid[:2], "COUNTY": geoid[2:]},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


# ---------------------------------------------------------------------------
# Aggregation correctness.
# ---------------------------------------------------------------------------


def test_aggregate_groups_by_county_fips():
    recs = [
        _decl(disaster=4337, incident="Hurricane", date="2017-09-10T00:00:00.000Z"),
        _decl(disaster=4673, incident="Hurricane", date="2022-09-29T00:00:00.000Z", ia=True),
        _decl(disaster=4564, incident="Biological", dtype="EM", date="2020-03-25T00:00:00.000Z"),
        # A different county (Broward 011).
        _decl(fips_county="011", disaster=4337, incident="Hurricane", area="Broward (County)"),
    ]
    by_fips, n_statewide = m._aggregate_by_county(recs)
    assert n_statewide == 0
    assert set(by_fips) == {"12086", "12011"}

    md = by_fips["12086"]
    assert md["n_declarations"] == 3
    assert md["disaster_numbers"] == {"4337", "4673", "4564"}
    assert md["incident_types"] == {"Hurricane", "Biological"}
    assert md["declaration_types"] == {"DR", "EM"}
    # latest declaration is the max date, and tracks its area_name.
    assert md["latest_declaration"] == "2022-09-29T00:00:00.000Z"
    assert md["ia_program"] is True  # one row had IA
    assert md["pa_program"] is True


def test_aggregate_excludes_statewide_county_000():
    recs = [
        _decl(fips_county="000", area="Statewide"),
        _decl(fips_county="", area="(missing)"),
        _decl(fips_county="086"),
    ]
    by_fips, n_statewide = m._aggregate_by_county(recs)
    assert set(by_fips) == {"12086"}
    assert n_statewide == 2  # the 000 row + the empty-county row


def test_aggregate_pads_short_fips():
    # A 2-digit county code must zero-pad to 3 for a 5-digit FIPS.
    recs = [_decl(fips_state="6", fips_county="37")]
    by_fips, _ = m._aggregate_by_county(recs)
    assert set(by_fips) == {"06037"}


# ---------------------------------------------------------------------------
# FlatGeobuf join + round-trip.
# ---------------------------------------------------------------------------


def test_build_flatgeobuf_joins_and_roundtrips():
    geopandas = pytest.importorskip("geopandas")

    by_fips, _ = m._aggregate_by_county(
        [
            _decl(fips_county="086", disaster=4337, incident="Hurricane"),
            _decl(fips_county="086", disaster=4673, incident="Tropical Storm"),
            _decl(fips_county="011", disaster=4337, incident="Hurricane", area="Broward (County)"),
        ]
    )
    geom = {
        "12086": _county_feature("12086", "Miami-Dade County", square_at=(-80.5, 25.5)),
        "12011": _county_feature("12011", "Broward County", square_at=(-80.4, 26.0)),
    }
    fgb, extent = m._build_flatgeobuf(by_fips, geom)
    assert isinstance(fgb, (bytes, bytearray)) and len(fgb) > 0
    assert extent[0] < extent[2] and extent[1] < extent[3]

    import io

    gdf = geopandas.read_file(io.BytesIO(fgb))
    assert len(gdf) == 2
    md = gdf.set_index("county_fips").loc["12086"]
    assert md["county_name"] == "Miami-Dade County"
    assert md["n_declarations"] == 2
    assert set(md["incident_types"].split(",")) == {"Hurricane", "Tropical Storm"}
    assert md["disaster_numbers"] == "4337,4673"  # numeric sort
    assert md["state_fips"] == "12"


def test_build_flatgeobuf_clips_to_bbox():
    pytest.importorskip("geopandas")
    by_fips, _ = m._aggregate_by_county(
        [
            _decl(fips_county="086"),
            _decl(fips_county="011", area="Broward (County)"),
        ]
    )
    geom = {
        # Inside the clip bbox.
        "12086": _county_feature("12086", "Miami-Dade County", square_at=(-80.5, 25.5)),
        # Far away (won't intersect a tight Miami bbox).
        "12011": _county_feature("12011", "Broward County", square_at=(-83.0, 30.0)),
    }
    clip = (-80.6, 25.4, -80.2, 25.9)
    fgb, _extent = m._build_flatgeobuf(by_fips, geom, clip_bbox=clip)
    import io

    import geopandas

    gdf = geopandas.read_file(io.BytesIO(fgb))
    assert list(gdf["county_fips"]) == ["12086"]


def test_build_flatgeobuf_empty_join_raises():
    pytest.importorskip("geopandas")
    by_fips, _ = m._aggregate_by_county([_decl(fips_county="086")])
    # No matching geometry for 12086 -> nothing joins.
    with pytest.raises(m.OpenFemaNoDeclarationsError):
        m._build_flatgeobuf(by_fips, geom_by_fips={})


def test_fetch_bytes_no_declarations_is_typed(monkeypatch):
    # Statewide-only declarations -> nothing joins to a county -> typed error
    # whose message mentions the unmapped statewide count.
    monkeypatch.setattr(
        m,
        "_fetch_state_declarations",
        lambda *a, **k: [_decl(fips_county="000", area="Statewide")],
    )
    # No county aggregate, so the geometry fetch must never be needed; guard it.
    monkeypatch.setattr(
        m, "_fetch_county_geometry", lambda *a, **k: pytest.fail("geom must not be fetched")
    )
    with pytest.raises(m.OpenFemaNoDeclarationsError) as ei:
        m._fetch_openfema_disasters_bytes(
            states=["FL"], incident_type=None, start_fy=None, clip_bbox=None
        )
    assert "statewide" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# Parsers + URL builders.
# ---------------------------------------------------------------------------


def test_parse_declarations_reads_body():
    body = json.dumps(
        {"DisasterDeclarationsSummaries": [_decl(), _decl(fips_county="011")]}
    ).encode("utf-8")
    recs = m._parse_declarations(body)
    assert len(recs) == 2


def test_parse_declarations_empty_and_bad():
    assert m._parse_declarations(b"") == []
    with pytest.raises(m.OpenFemaUpstreamError):
        m._parse_declarations(b"{not json")
    with pytest.raises(m.OpenFemaUpstreamError):
        m._parse_declarations(json.dumps({"wrongKey": []}).encode())


def test_build_openfema_filter():
    f = m._build_openfema_filter("FL", "Hurricane", 2017)
    assert f == "state eq 'FL' and incidentType eq 'Hurricane' and fyDeclared ge 2017"
    f2 = m._build_openfema_filter("TX", None, None)
    assert f2 == "state eq 'TX'"


def test_build_openfema_url_pages():
    url = m._build_openfema_url("state eq 'FL'", skip=1000)
    assert m.OPENFEMA_URL in url
    assert "%24skip=1000" in url and "%24top=1000" in url
    assert "%24format=json" in url


def test_build_tiger_url():
    url = m._build_tiger_url("12")
    assert m.TIGER_COUNTY_URL in url
    assert "STATE%3D%2712%27" in url  # where=STATE='12'
    assert "f=geojson" in url and "returnGeometry=true" in url


# ---------------------------------------------------------------------------
# Selector resolution.
# ---------------------------------------------------------------------------


def test_resolve_states_state_code():
    assert m._resolve_states("fl", None) == ["FL"]


def test_resolve_states_bbox_single_and_multi():
    # A tight Florida bbox -> just FL.
    assert m._resolve_states(None, (-81.0, 25.5, -80.5, 26.0)) == ["FL"]
    # A bbox spanning the GA/FL line -> both.
    multi = m._resolve_states(None, (-82.5, 30.0, -81.5, 31.5))
    assert "FL" in multi and "GA" in multi


def test_resolve_states_requires_selector():
    with pytest.raises(m.OpenFemaInputError):
        m._resolve_states(None, None)


def test_resolve_states_bbox_outside_us_raises():
    with pytest.raises(m.OpenFemaInputError):
        m._resolve_states(None, (10.0, 10.0, 11.0, 11.0))


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_validate_state_code():
    assert m._validate_state_code(" fl ") == "FL"
    with pytest.raises(m.OpenFemaInputError):
        m._validate_state_code("ZZ")
    with pytest.raises(m.OpenFemaInputError):
        m._validate_state_code(12)  # type: ignore[arg-type]


def test_validate_incident_type_normalizes():
    assert m._validate_incident_type("hurricane") == "Hurricane"
    assert m._validate_incident_type("Severe Storm") == "Severe Storm"
    with pytest.raises(m.OpenFemaInputError):
        m._validate_incident_type("Sharknado")
    with pytest.raises(m.OpenFemaInputError):
        m._validate_incident_type("")


@pytest.mark.parametrize(
    "bad",
    [
        (1, 2, 3),  # wrong arity
        (-200, 0, 10, 10),  # lon out of range
        (0, -100, 10, 10),  # lat out of range
        (10, 10, 5, 20),  # west >= east
        ("a", 0, 1, 1),  # non-numeric
    ],
)
def test_validate_bbox_rejects(bad):
    with pytest.raises(m.OpenFemaInputError):
        m._validate_bbox(bad)  # type: ignore[arg-type]


def test_validate_bbox_accepts():
    assert m._validate_bbox((-80.6, 25.4, -80.0, 26.1)) == (-80.6, 25.4, -80.0, 26.1)


def test_validate_year():
    assert m._validate_year(None, label="start_year") is None
    assert m._validate_year(2017, label="start_year") == 2017
    with pytest.raises(m.OpenFemaInputError):
        m._validate_year(1800, label="start_year")
    with pytest.raises(m.OpenFemaInputError):
        m._validate_year("notayear", label="start_year")


def test_fetch_openfema_disasters_requires_selector():
    with pytest.raises(m.OpenFemaInputError):
        m.fetch_openfema_disasters()


# ---------------------------------------------------------------------------
# Metadata + estimator.
# ---------------------------------------------------------------------------


def test_metadata_registered():
    assert m._METADATA.name == "fetch_openfema_disasters"
    assert m._METADATA.cacheable is True
    assert m._METADATA.ttl_class == "semi-static-7d"
    assert m._METADATA.source_class == "openfema_disasters"


def test_estimate_payload_mb():
    assert m.estimate_payload_mb(state_code="FL") > 0
    small = m.estimate_payload_mb(bbox=(-80.6, 25.4, -80.0, 26.1))
    big = m.estimate_payload_mb(state_code="TX")
    assert small < big
