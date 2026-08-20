"""Fallback-ladder contract: the rung schema, the ONE walker, the loudness gate.

All offline. The SWAN bathymetry ladder is exercised through the real router with
the topobathy delegate's source-discovery edges patched to synthetic rasters (the
idiom test_router_topobathy uses).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_contracts.common import render_fallback_line
from trid3nt_contracts.execution import TopobathyResult
from trid3nt_server.data import TOOL_REGISTRY
from trid3nt_server.data.fetchers._router.hooks import topobathy as tb
from trid3nt_server.fallbacks import (
    Ladder,
    LadderGap,
    LadderRefused,
    Rung,
    get_ladder,
    walk_ladder,
)
from trid3nt_server.gates.fallback import confirm_fallback, gate_fires, labeled_default

#: The AOI from the SWAN bathymetry-forensics exhibit (Port St Joe FL): CUDEM's
#: 1/9" collection stops mid-AOI, and the uncovered corner is where the flat 0 m
#: land fill painted the fake landmass.
_EXHIBIT_BBOX = (-85.55, 29.70, -85.40, 29.85)
_EXHIBIT_TILES = [
    "https://x/ncei19_n29X75_w085X50_2019v1.tif",
    "https://x/ncei19_n30X00_w085X50_2019v1.tif",
    "https://x/ncei19_n30X00_w085X75_2019v1.tif",
]
_SMOKE_BBOX = (-85.45, 29.92, -85.38, 29.98)


def _rung(name: str, consequence: str, **kw: Any) -> Rung:
    return Rung(name=name, consequence=consequence, describes=f"{name} describes", **kw)


def _ladder(*alternatives: Rung, user: Rung | None = None) -> Ladder:
    rungs = ((user,) if user else ()) + (_rung("primary", "primary"),) + alternatives
    return Ladder(capability="test_cap", rungs=rungs, refuse_error_code="TEST_REFUSED")


# --------------------------------------------------------------------------- #
# Rung / Ladder schema.
# --------------------------------------------------------------------------- #


def test_ladder_requires_exactly_one_primary() -> None:
    with pytest.raises(ValueError, match="exactly ONE primary"):
        Ladder(capability="c", rungs=(_rung("a", "same_data"),), refuse_error_code="X")


def test_user_supplied_rung_must_be_the_top_rung() -> None:
    user = _rung("u", "user_supplied", supplies_param="p")
    with pytest.raises(ValueError, match="TOP rung"):
        Ladder(
            capability="c",
            rungs=(_rung("primary", "primary"), user),
            refuse_error_code="X",
        )


def test_alternative_must_carry_a_degradation_class() -> None:
    with pytest.raises(ValueError, match="an alternative"):
        Ladder(
            capability="c",
            rungs=(_rung("primary", "primary"), _rung("b", "refuse")),
            refuse_error_code="X",
        )


def test_user_supplied_rung_needs_a_supplies_param() -> None:
    with pytest.raises(ValueError, match="supplies_param"):
        Rung(name="u", consequence="user_supplied", describes="d")


def test_rung_has_exactly_one_invocation_form() -> None:
    with pytest.raises(ValueError, match="exactly one invocation form"):
        Rung(name="r", consequence="same_data", describes="d", source="s", call="m:f")


def test_terminal_rung_is_refuse() -> None:
    assert _ladder().terminal.consequence == "refuse"


# --------------------------------------------------------------------------- #
# The walker.
# --------------------------------------------------------------------------- #


def test_primary_serves_whole_request_and_is_recorded() -> None:
    result, act = walk_ladder(
        _ladder(), params={"bbox": 1}, attempt=lambda _r, _p: "served",
        gate=lambda **_k: True,
    )
    assert result == "served"
    assert [(r.rung, r.coverage) for r in act.records] == [("primary", 1.0)]
    assert act.degraded is False
    assert act.narration() is None


def test_undeclared_gap_propagates_the_typed_error_verbatim() -> None:
    def _attempt(_r: Any, _p: Any) -> Any:
        raise LadderGap("gap!", covered_fraction=0.889, gap_note="CUDEM stops here")

    with pytest.raises(LadderGap) as ei:
        walk_ladder(_ladder(_rung("alt", "cross_dataset")), params={},
                    attempt=_attempt, gate=lambda **_k: True)
    assert "gap!" in str(ei.value)
    assert [r.rung for r in ei.value.fallback_activation.records] == ["primary"]


def test_declared_rung_fills_the_gap_and_splits_coverage() -> None:
    calls: list[dict] = []

    def _attempt(rung: Any, params: dict) -> Any:
        calls.append(dict(params))
        if rung.name == "primary":
            raise LadderGap("gap", covered_fraction=0.889, gap_note="CUDEM stops here")
        return "merged"

    alt = Rung(name="alt", consequence="cross_dataset", describes="coarse global bed",
               params={"force_bathy_base": True})
    result, act = walk_ladder(_ladder(alt), params={"bbox": 1}, attempt=_attempt,
                              allow=("alt",), gate=lambda **_k: True)
    assert result == "merged"
    assert act.degraded is True
    shares = {r.rung: round(r.coverage, 3) for r in act.records}
    assert shares == {"primary": 0.889, "alt": 0.111}
    # the rung's params reached the retry, and only the retry.
    assert "force_bathy_base" not in calls[0]
    assert calls[1]["force_bathy_base"] is True
    assert "89% primary" in act.coverage_summary()
    assert "11% alt [cross_dataset]" in (act.narration() or "")


def test_declined_rung_leaves_the_typed_error_standing() -> None:
    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise LadderGap("gap", covered_fraction=0.5, gap_note="half missing")
        return "merged"

    with pytest.raises(LadderGap):
        walk_ladder(_ladder(_rung("alt", "cross_dataset")), params={},
                    attempt=_attempt, allow=("alt",), gate=lambda **_k: False)


def test_gap_that_no_rung_could_fill_refuses_naming_the_gap() -> None:
    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise LadderGap("gap", covered_fraction=0.5, gap_note="half the AOI")
        raise RuntimeError("ETOPO host unreachable")

    with pytest.raises(LadderRefused) as ei:
        walk_ladder(_ladder(_rung("alt", "cross_dataset")), params={},
                    attempt=_attempt, allow=("alt",), gate=lambda **_k: True)
    assert ei.value.error_code == "TEST_REFUSED"
    assert "half the AOI" in str(ei.value)
    assert "ETOPO host unreachable" in str(ei.value)
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_user_supplied_rung_outranks_every_derived_rung() -> None:
    user = Rung(name="user_supplied", consequence="user_supplied", supplies_param="own",
                describes="the caller's own data")
    result, act = walk_ladder(
        _ladder(user=user), params={"own": "s3://mine.tif"},
        attempt=lambda r, p: p["own"] if r.name == "user_supplied" else "derived",
        gate=lambda **_k: True,
    )
    assert result == "s3://mine.tif"
    assert [r.rung for r in act.records] == ["user_supplied"]
    assert act.degraded is False


def test_user_rung_stands_aside_when_the_user_supplied_nothing() -> None:
    user = Rung(name="user_supplied", consequence="user_supplied", supplies_param="own",
                describes="the caller's own data")
    _result, act = walk_ladder(_ladder(user=user), params={},
                               attempt=lambda _r, _p: "derived", gate=lambda **_k: True)
    assert [r.rung for r in act.records] == ["primary"]


def test_permitting_an_undeclared_rung_is_a_call_site_bug() -> None:
    with pytest.raises(ValueError, match="declares no alternative rung"):
        walk_ladder(_ladder(), params={}, attempt=lambda _r, _p: "x",
                    allow=("nope",), gate=lambda **_k: True)


def test_failed_rung_descends_and_records_the_failure() -> None:
    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise RuntimeError("host unreachable")
        return "mirror"

    result, act = walk_ladder(_ladder(_rung("alt", "same_data")), params={},
                              attempt=_attempt, allow=("alt",), gate=lambda **_k: True)
    assert result == "mirror"
    assert act.records[0].coverage == 0.0
    assert "host unreachable" in (act.records[0].note or "")
    assert act.records[1].coverage == 1.0


def test_activation_contract_rows_drop_zero_coverage_attempts() -> None:
    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise RuntimeError("boom")
        return "mirror"

    _r, act = walk_ladder(_ladder(_rung("alt", "same_data")), params={},
                          attempt=_attempt, allow=("alt",), gate=lambda **_k: True)
    rows = act.to_contract()
    assert [r.rung for r in rows] == ["alt"]
    assert rows[0].consequence == "same_data"


def test_render_fallback_line_is_silent_on_an_undegraded_run() -> None:
    _r, act = walk_ladder(_ladder(), params={}, attempt=lambda _r, _p: "x",
                          gate=lambda **_k: True)
    assert render_fallback_line(act.to_contract()) is None


# --------------------------------------------------------------------------- #
# The loudness floor.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "consequence,mode,fires",
    [
        ("same_data", "auto", False),
        ("same_data", "user_gated", False),
        ("cross_dataset", "auto", False),
        ("cross_dataset", "user_gated", True),
        ("synthetic", "auto", True),
        ("synthetic", "user_gated", True),
    ],
)
def test_loudness_floor(consequence: str, mode: str, fires: bool) -> None:
    assert gate_fires(consequence, mode) is fires


def test_labeled_defaults_refuse_only_synthetic() -> None:
    assert labeled_default("same_data") is True
    assert labeled_default("cross_dataset") is True
    assert labeled_default("synthetic") is False


def test_headless_gate_applies_the_labeled_default() -> None:
    # No emitter bound (a direct-call / canary run): never hangs.
    assert confirm_fallback(capability="c", rung=_rung("a", "cross_dataset"),
                            gate_mode="user_gated") is True
    assert confirm_fallback(capability="c", rung=_rung("a", "synthetic"),
                            gate_mode="auto") is False


def test_same_data_rung_walks_without_asking() -> None:
    assert confirm_fallback(capability="c", rung=_rung("m", "same_data"),
                            gate_mode="user_gated") is True


# --------------------------------------------------------------------------- #
# The SWAN bathymetry ladder: coverage math + the gap.
# --------------------------------------------------------------------------- #


def test_bathymetry_ladder_shape() -> None:
    ladder = get_ladder("fetch_topobathy")
    assert ladder is not None
    assert ladder.user_rung is not None and ladder.user_rung.supplies_param == "dem_uri"
    assert ladder.primary_rung.name == "cudem_nearshore"
    assert [r.name for r in ladder.alternatives] == ["etopo_bathy_base"]
    assert ladder.alternative("etopo_bathy_base").consequence == "cross_dataset"
    assert ladder.refuse_error_code == "TOPOBATHY_COVERAGE_GAP"


def test_cudem_coverage_fraction_on_the_exhibit_aoi() -> None:
    frac = tb.cudem_coverage_fraction(_EXHIBIT_BBOX, _EXHIBIT_TILES)
    assert frac == pytest.approx(8.0 / 9.0, abs=1e-6)


def test_cudem_coverage_is_unknown_when_a_footprint_cannot_be_parsed() -> None:
    assert tb.cudem_coverage_fraction(_EXHIBIT_BBOX, ["/tmp/synthetic.tif"]) is None


def test_cudem_coverage_full_when_tiles_blanket_the_aoi() -> None:
    assert tb.cudem_coverage_fraction(
        (-85.45, 29.92, -85.38, 29.98), ["x/ncei19_n30X00_w085X50_2019v1.tif"]
    ) == pytest.approx(1.0)


def test_partial_coverage_raises_the_ladder_gap(monkeypatch) -> None:
    monkeypatch.setattr(tb, "_select_cudem_tiles", lambda *_a, **_k: list(_EXHIBIT_TILES))
    with pytest.raises(tb.TopobathyCoverageGapError) as ei:
        tb.validate_topobathy(None, {"bbox": list(_EXHIBIT_BBOX)})
    exc = ei.value
    assert exc.error_code == "TOPOBATHY_COVERAGE_GAP"
    assert exc.covered_fraction == pytest.approx(8.0 / 9.0, abs=1e-6)
    assert "89%" in str(exc) and "etopo_bathy_base" in str(exc)
    assert isinstance(exc, LadderGap)


@pytest.mark.parametrize(
    "params",
    [
        {"force_bathy_base": True},
        {"skip_cudem": True},
        {"include_regional_fine": True},
    ],
)
def test_coverage_gate_is_exempt_when_a_global_base_or_fine_leg_is_requested(
    monkeypatch, params: dict
) -> None:
    monkeypatch.setattr(tb, "_select_cudem_tiles", lambda *_a, **_k: list(_EXHIBIT_TILES))
    tb.validate_topobathy(None, {"bbox": list(_EXHIBIT_BBOX), **params})


def test_zero_cudem_tiles_is_not_a_coverage_gap(monkeypatch) -> None:
    monkeypatch.setattr(tb, "_select_cudem_tiles", lambda *_a, **_k: [])
    tb.validate_topobathy(None, {"bbox": list(_EXHIBIT_BBOX)})


def test_unreachable_tile_index_never_claims_a_gap(monkeypatch) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise tb.TopobathyUpstreamError("index down")

    monkeypatch.setattr(tb, "_select_cudem_tiles", _boom)
    tb.validate_topobathy(None, {"bbox": list(_EXHIBIT_BBOX)})


# --------------------------------------------------------------------------- #
# End-to-end through the router: the A/B the exhibit demands.
# --------------------------------------------------------------------------- #


def _synth_raster(path: str, bbox, fill: float) -> None:
    west, south, east, north = bbox
    with rasterio.open(
        path, "w", driver="GTiff", height=20, width=20, count=1, dtype="float32",
        crs="EPSG:4326", nodata=-9999.0,
        transform=from_origin(west, north, (east - west) / 20, (north - south) / 20),
    ) as dst:
        dst.write(np.full((20, 20), fill, dtype="float32"), 1)


def _patch_partial_cudem(monkeypatch, tmp_path) -> None:
    """Real (parseable) CUDEM tile NAMES for the coverage gate, synthetic rasters
    for the merge -- the gap is a footprint fact, the merge is a pixel fact."""
    cudem = str(tmp_path / "cudem.tif")
    etopo = str(tmp_path / "etopo.tif")
    land = str(tmp_path / "land.tif")
    _synth_raster(cudem, _EXHIBIT_BBOX, -5.0)
    _synth_raster(etopo, _EXHIBIT_BBOX, -30.0)
    _synth_raster(land, _EXHIBIT_BBOX, 12.0)
    monkeypatch.setattr(tb, "_select_cudem_tiles", lambda *_a, **_k: list(_EXHIBIT_TILES))
    monkeypatch.setattr(tb, "_select_etopo_tiles", lambda *_a, **_k: [etopo])
    monkeypatch.setattr(tb, "_assert_navd88", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(tb, "_fetch_3dep_land_to_file", lambda *_a, **_k: land)
    real = tb._composite_sources_to_array

    def _strip(sources, target_crs, bbox, **kw):
        stripped = [
            (s[len("/vsicurl/"):] if s.startswith("/vsicurl/") else s) for s in sources
        ]
        return real([cudem if s in _EXHIBIT_TILES else s for s in stripped],
                    target_crs, bbox, **kw)

    monkeypatch.setattr(tb, "_composite_sources_to_array", _strip)


def test_undeclared_call_refuses_and_names_where_cudem_ends(
    monkeypatch, tmp_path, fake_s3
) -> None:
    _patch_partial_cudem(monkeypatch, tmp_path)
    with pytest.raises(tb.TopobathyCoverageGapError) as ei:
        TOOL_REGISTRY["fetch_topobathy"].fn(bbox=list(_EXHIBIT_BBOX))
    assert "has NO nearshore bathymetry source" in str(ei.value)
    assert "fake landmass" in str(ei.value)


def test_declared_rung_fills_the_gap_loudly_with_coverage(
    monkeypatch, tmp_path, fake_s3
) -> None:
    _patch_partial_cudem(monkeypatch, tmp_path)
    res = TOOL_REGISTRY["fetch_topobathy"].fn(
        bbox=list(_EXHIBIT_BBOX), fallback=("etopo_bathy_base",)
    )
    assert isinstance(res, TopobathyResult)
    rows = {r.rung: r for r in res.fallbacks}
    assert set(rows) == {"cudem_nearshore", "etopo_bathy_base"}
    assert rows["cudem_nearshore"].coverage == pytest.approx(8.0 / 9.0, abs=1e-6)
    assert rows["etopo_bathy_base"].coverage == pytest.approx(1.0 / 9.0, abs=1e-6)
    assert rows["etopo_bathy_base"].consequence == "cross_dataset"
    assert res.fallback_note and "etopo_bathy_base [cross_dataset]" in res.fallback_note


def test_user_supplied_bed_wins_over_the_whole_ladder(monkeypatch, tmp_path) -> None:
    _patch_partial_cudem(monkeypatch, tmp_path)
    res = TOOL_REGISTRY["fetch_topobathy"].fn(
        bbox=list(_EXHIBIT_BBOX), dem_uri="s3://mine/survey.tif"
    )
    assert res.uri == "s3://mine/survey.tif"
    assert [r.rung for r in res.fallbacks] == ["user_supplied"]
    assert res.fallbacks[0].consequence == "user_supplied"
    assert [e.basis for e in res.synthetic_inputs] == ["user"]


def test_swan_declares_the_bathy_base_rung() -> None:
    from trid3nt_server.workflows.swan.wave_field import wave_field as wf

    assert wf._SWAN_BATHY_FALLBACK == ("etopo_bathy_base",)
    ladder = get_ladder("fetch_topobathy")
    assert all(ladder.alternative(n) is not None for n in wf._SWAN_BATHY_FALLBACK)


def test_swan_stamps_the_bed_ladder_onto_its_result() -> None:
    from trid3nt_contracts.common import FallbackActivation
    from trid3nt_contracts.swan_contracts import SwanRunArgs, WaveFieldLayerURI
    from trid3nt_server.workflows.swan.wave_field import wave_field as wf

    peak = WaveFieldLayerURI(
        layer_id="p", name="Peak wave height", layer_type="raster", uri="s3://b/p.tif",
        style_preset="continuous_wave_height", role="primary", units="meters",
        max_hs_m=1.0, mean_tp_s=8.0, mean_dir_deg=180.0, wave_area_km2=1.0,
        mode="stationary",
    )
    rows = [
        FallbackActivation(capability="fetch_topobathy", rung="cudem_nearshore",
                           consequence="primary", coverage=0.889),
        FallbackActivation(capability="fetch_topobathy", rung="etopo_bathy_base",
                           consequence="cross_dataset", coverage=0.111),
    ]
    out = wf._stamp_swan_provenance(peak, SwanRunArgs(bbox=_SMOKE_BBOX), rows)
    assert [r.rung for r in out.fallbacks] == ["cudem_nearshore", "etopo_bathy_base"]
    assert "Wave bed:" in (out.fallback_note or "")
    assert "11% etopo_bathy_base [cross_dataset]" in out.fallback_note
