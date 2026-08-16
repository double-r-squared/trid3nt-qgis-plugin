"""Offline tests for the ADR 0226 finite-fault upgrade (measured-inversion rung).

Three layers, all offline (no network / MinIO / clawpack):
  * ``parse_fsp`` -- PURE parse of a cached USGS finite-fault ``.fsp`` fixture
    (the real 2021 M8.2 Chignik inversion, header + first rows).
  * ``fetch_finite_fault_model`` -- the ComCat-products I/O boundary, with
    ``_http_get`` monkeypatched: product present -> parsed; absent -> None (the
    degrade rung); present-but-no-fsp -> typed FiniteFaultError.
  * the composer fallback LADDER LABELS -- ``geoclaw_inundation`` stamps
    ``basis="measured_inversion"`` (naming the product) when a finite-fault model
    resolves, else the DERIVED single-subfault ``fault_mechanism`` label.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trid3nt_server.agent.workflows.geoclaw import finite_fault as ff

_FIXTURE = Path(__file__).parent / "fixtures" / "finite_fault" / "chignik_ak0219neiszm_1.fsp"


# --------------------------------------------------------------------------- #
# parse_fsp (pure).
# --------------------------------------------------------------------------- #
def test_parse_fsp_reads_mechanism_and_patches():
    model = ff.parse_fsp(_FIXTURE.read_text())
    assert model.n_subfaults == 12  # the trimmed fixture keeps 12 data rows
    # whole-fault mechanism from the header (STRK=243, DIP=14).
    p0 = model.patches[0]
    assert p0.strike_deg == 243.0 and p0.dip_deg == 14.0
    # subfault dims from Dx=Dz=14 km -> metres.
    assert p0.length_m == 14000.0 and p0.width_m == 14000.0
    # depth km -> m (the first Chignik row Z=8.4917 km).
    assert abs(p0.depth_m - 8491.7) < 1.0
    assert model.magnitude is not None and abs(model.magnitude - 8.27) < 0.05


def test_parse_fsp_footprint_and_slip_range():
    model = ff.parse_fsp(_FIXTURE.read_text())
    min_lon, min_lat, max_lon, max_lat = model.footprint_bbox
    # the Chignik rupture sits on the Alaska Peninsula (negative lon, ~55 N).
    assert -161.0 < min_lon < -154.0 and 54.0 < min_lat < 57.5
    assert max_lon > min_lon and max_lat > min_lat
    assert model.max_slip_m > 0.0


def test_parse_fsp_ignores_scalar_loc_header_line():
    # REGRESSION: the "% Loc : LAT = .. LON = .." scalar header must NOT be mistaken
    # for the per-subfault DATA column header (that put slip/Y into lon/lat).
    model = ff.parse_fsp(_FIXTURE.read_text())
    for p in model.patches:
        assert -180.0 <= p.lon <= 180.0 and -90.0 <= p.lat <= 90.0


def test_parse_fsp_no_mechanism_raises():
    with pytest.raises(ff.FiniteFaultError) as ei:
        ff.parse_fsp("% just a comment\n1 2 3\n")
    assert ei.value.error_code == "FINITE_FAULT_FSP_NO_MECHANISM"


def test_to_csvfault_text_column_header_matches_reader():
    model = ff.parse_fsp(_FIXTURE.read_text())
    csv = ff.to_csvfault_text(model)
    header = csv.splitlines()[0]
    # the exact clawpack CSVFault column names (units in parens -> standard units).
    assert header == "longitude,latitude,depth(m),length(m),width(m),strike,dip,rake,slip(m)"
    assert len(csv.splitlines()) == 1 + model.n_subfaults


# --------------------------------------------------------------------------- #
# fetch_finite_fault_model (I/O boundary; _http_get monkeypatched).
# --------------------------------------------------------------------------- #
def _detail_with_finite_fault() -> bytes:
    return json.dumps({
        "properties": {"products": {"finite-fault": [{
            "code": "ak0219neiszm_1", "version": "1", "updateTime": 1635188938271,
            "contents": {"complete_inversion.fsp": {
                "url": "https://example.test/complete_inversion.fsp"}},
        }]}}
    }).encode()


def test_fetch_finite_fault_present_parses():
    fsp = _FIXTURE.read_text().encode()

    def _http(url: str) -> bytes:
        return fsp if url.endswith(".fsp") else _detail_with_finite_fault()

    model = ff.fetch_finite_fault_model("ak0219neiszm", _http_get_fn=_http)
    assert model is not None
    assert model.n_subfaults == 12
    assert model.product_id == "ak0219neiszm_1"
    assert model.fsp_url and model.product_url
    assert "finite-fault product ak0219neiszm_1" in model.provenance_label


def test_fetch_finite_fault_absent_returns_none_degrade():
    # No finite-fault product -> None (the caller falls to single-subfault).
    def _http(url: str) -> bytes:
        return json.dumps({"properties": {"products": {}}}).encode()

    assert ff.fetch_finite_fault_model("evt", _http_get_fn=_http) is None


def test_fetch_finite_fault_present_no_fsp_raises():
    def _http(url: str) -> bytes:
        return json.dumps({"properties": {"products": {"finite-fault": [{
            "code": "x_1", "contents": {"FFM.geojson": {"url": "u"}}}]}}}).encode()

    with pytest.raises(ff.FiniteFaultError) as ei:
        ff.fetch_finite_fault_model("evt", _http_get_fn=_http)
    assert ei.value.error_code == "FINITE_FAULT_NO_FSP"


def test_fetch_finite_fault_detail_unreachable_degrades():
    def _http(url: str) -> bytes:
        raise OSError("connection refused")

    # An unreachable event detail is NOT fatal -> degrade to single-subfault.
    assert ff.fetch_finite_fault_model("evt", _http_get_fn=_http) is None


# --------------------------------------------------------------------------- #
# Composer fallback ladder labels (geoclaw_inundation).
# --------------------------------------------------------------------------- #
def _fake_event():
    from trid3nt_server.agent.workflows.geoclaw.earthquake_source import ResolvedEarthquake
    return ResolvedEarthquake(
        lon=-157.9, lat=55.4, magnitude=8.2, depth_km=32.0,
        event_id="ak0219neiszm", place="Alaska Peninsula",
        time="2021-07-29T06:15:47Z")


def _fake_model(n=396):
    return ff.FiniteFaultModel(
        patches=[ff.FiniteFaultPatch(-157.9 + i * 0.01, 55.4, 8000.0, 243.0, 14.0,
                                     93.0, 1.0, 14000.0, 14000.0) for i in range(n)],
        magnitude=8.27, product_id="ak0219neiszm_1", product_version="1",
        fsp_url="https://example.test/complete_inversion.fsp",
        product_url="https://earthquake.usgs.gov/product/finite-fault/ak0219neiszm_1",
    )


def _install_composer_stubs(monkeypatch, *, model):
    """Stub the composer's I/O so geoclaw_inundation runs to the model dispatch and
    we can capture the assembled run_args + provenance labels offline."""
    from trid3nt_server.agent.workflows.geoclaw.inundation import inundation as comp

    monkeypatch.setattr(comp, "resolve_earthquake_source", lambda *a, **k: _fake_event())
    monkeypatch.setattr(comp, "fetch_finite_fault_model", lambda eid: model)
    monkeypatch.setattr(comp, "stage_finite_fault_csv", lambda csv: "s3://c/finite_fault.csv")

    async def _fake_gate(*, tool_name, mode, entries, params):
        return SimpleNamespace(cancelled=False, cancel_reason=None,
                               entries=entries, params=params)
    monkeypatch.setattr(comp, "gate_input_review", _fake_gate)

    captured: dict = {}

    async def _fake_model_run(run_args, **kwargs):
        captured["run_args"] = run_args
        captured["synthetic_inputs"] = kwargs.get("synthetic_inputs")
        from trid3nt_contracts.geoclaw_contracts import GeoClawDepthLayerURI
        return GeoClawDepthLayerURI(
            uri="s3://runs/peak.tif", layer_id="x", name="Peak flood depth",
            style_preset="continuous_flood_depth", bbox=tuple(run_args.bbox),
            role="primary", max_depth_m=1.0, flooded_area_km2=1.0,
            max_inundation_m=1.0, scenario="tsunami")
    monkeypatch.setattr(comp, "model_geoclaw_inundation", _fake_model_run)
    return captured


@pytest.mark.asyncio
async def test_composer_finite_fault_present_measured_label(monkeypatch):
    from trid3nt_server.agent.workflows.geoclaw.inundation.inundation import geoclaw_inundation
    captured = _install_composer_stubs(monkeypatch, model=_fake_model())
    await geoclaw_inundation(
        bbox=(-159.8, 55.0, -158.8, 55.6), earthquake_source="Alaska Peninsula")
    ra = captured["run_args"]
    assert ra.finite_fault_uri == "s3://c/finite_fault.csv"
    assert ra.finite_fault_footprint is not None
    bases = {si.param: si.basis for si in captured["synthetic_inputs"]}
    assert bases.get("finite_fault_model") == "measured_inversion"
    # the derived single-subfault mechanism label is REPLACED by the inversion.
    assert "fault_mechanism" not in bases


@pytest.mark.asyncio
async def test_composer_finite_fault_absent_derived_label(monkeypatch):
    from trid3nt_server.agent.workflows.geoclaw.inundation.inundation import geoclaw_inundation
    captured = _install_composer_stubs(monkeypatch, model=None)
    await geoclaw_inundation(
        bbox=(-159.8, 55.0, -158.8, 55.6), earthquake_source="Alaska Peninsula")
    ra = captured["run_args"]
    assert ra.finite_fault_uri is None
    bases = {si.param: si.basis for si in captured["synthetic_inputs"]}
    assert "finite_fault_model" not in bases
    assert bases.get("fault_mechanism") == "derived"
