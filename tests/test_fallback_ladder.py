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


def test_declining_refuses_in_its_own_words_and_leaves_a_visible_row() -> None:
    """A decline must NOT re-raise the gap error that tells the user to permit
    the rung they just declined, and it must leave a trace."""
    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise LadderGap("gap", covered_fraction=0.5, gap_note="half missing")
        return "merged"

    with pytest.raises(LadderRefused) as ei:
        walk_ladder(_ladder(_rung("alt", "cross_dataset")), params={},
                    attempt=_attempt, allow=("alt",), gate=lambda **_k: False)
    assert "declined at the fallback gate" in str(ei.value)
    assert "half missing" in str(ei.value)
    assert isinstance(ei.value.__cause__, LadderGap)
    rows = {r.rung: r for r in ei.value.activation.to_contract()}
    assert rows["alt"].coverage == 0.0
    assert "declined" in (rows["alt"].note or "")


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


class _TypedError(Exception):
    def __init__(self, message: str, code: str = "PRIMARY_BAD") -> None:
        super().__init__(message)
        self.error_code = code
        self.retryable = False


def test_primary_typed_error_survives_a_later_rungs_failure() -> None:
    """No rung may launder the PRIMARY's error_code / retryable into its own."""
    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise _TypedError("the bbox is outside the covered envelope")
        raise RuntimeError("mirror host unreachable")

    with pytest.raises(_TypedError) as ei:
        walk_ladder(_ladder(_rung("alt", "same_data")), params={},
                    attempt=_attempt, allow=("alt",), gate=lambda **_k: True)
    assert ei.value.error_code == "PRIMARY_BAD"
    assert ei.value.retryable is False
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert [r.rung for r in ei.value.fallback_activation.records] == ["primary", "alt"]


def test_untyped_failure_never_escapes_the_walker_bare() -> None:
    def _attempt(_r: Any, _p: Any) -> Any:
        raise RuntimeError("kaboom")

    with pytest.raises(LadderRefused) as ei:
        walk_ladder(_ladder(), params={}, attempt=_attempt, gate=lambda **_k: True)
    assert ei.value.error_code == "TEST_REFUSED"
    assert "kaboom" in str(ei.value)
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_exempt_request_stamps_no_coverage_claim() -> None:
    """A ladder whose coverage check the request exempted may not claim 1.0."""
    ladder = Ladder(
        capability="test_cap",
        rungs=(_rung("primary", "primary"), _rung("alt", "cross_dataset")),
        refuse_error_code="TEST_REFUSED",
        coverage_exempt_params=("force_base",),
    )
    _r, act = walk_ladder(ladder, params={"force_base": True},
                          attempt=lambda _r, _p: "served", gate=lambda **_k: True)
    assert act.coverage_unverified is True
    assert act.to_contract() == []
    assert act.narration() is None

    _r2, act2 = walk_ladder(ladder, params={"force_base": False},
                            attempt=lambda _r, _p: "served", gate=lambda **_k: True)
    assert act2.coverage_unverified is False
    assert [r.rung for r in act2.to_contract()] == ["primary"]


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


def test_unanswered_gate_on_a_live_session_is_a_decline() -> None:
    """Labeled defaults are for runs with NOBODY to ask. Once the card is on a
    live session, silence is a no (the input-review gate's semantics)."""
    import asyncio

    from trid3nt_contracts import new_ulid
    from trid3nt_contracts.payload_warning import PayloadWarningEnvelopePayload
    from trid3nt_server.gates import fallback as gate_mod

    class _Emitter:
        session_id = "sess"
        sent: list = []

        async def send_envelope(self, kind: str, env: Any) -> None:
            self.sent.append((kind, env))

    envelope = PayloadWarningEnvelopePayload(
        warning_id=new_ulid(), tool_name="fetch_topobathy",
        tool_args={}, estimated_mb=0.0, threshold_mb=0.0,
        recommendation="approve?", options=["proceed", "cancel"], ttl_seconds=1,
    )
    emitter = _Emitter()
    assert asyncio.run(gate_mod._present_and_wait(emitter, envelope)) is False
    assert emitter.sent and emitter.sent[0][0] == "tool-payload-warning"


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


def test_skip_land_gap_does_not_cite_the_land_fill_the_caller_disabled(
    monkeypatch,
) -> None:
    monkeypatch.setattr(tb, "_select_cudem_tiles", lambda *_a, **_k: list(_EXHIBIT_TILES))
    with pytest.raises(tb.TopobathyCoverageGapError) as ei:
        tb.validate_topobathy(
            None, {"bbox": list(_EXHIBIT_BBOX), "skip_land": True}
        )
    text = str(ei.value)
    assert "fake landmass" not in text
    assert "skip_land" in text and "NODATA" in text


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


#: Four CUDEM tiles that BLANKET the exhibit AOI: the footprint gate passes, so
#: any gap can only come from a tile that drops mid-merge.
_FULL_COVER_TILES = [
    "https://x/ncei19_n29X75_w085X50_2019v1.tif",
    "https://x/ncei19_n30X00_w085X50_2019v1.tif",
    "https://x/ncei19_n29X75_w085X75_2019v1.tif",
    "https://x/ncei19_n30X00_w085X75_2019v1.tif",
]
#: Its share of the AOI is 0.010 / 0.0225 -- dropping it leaves ~56%.
_DROPPED_TILE = "https://x/ncei19_n30X00_w085X50_2019v1.tif"


def _patch_full_cudem_with_one_unreadable_tile(monkeypatch, tmp_path) -> None:
    """Footprint PROMISES 100%; one tile is unreadable so the merge paints ~56%."""
    cudem = str(tmp_path / "cudem.tif")
    etopo = str(tmp_path / "etopo.tif")
    land = str(tmp_path / "land.tif")
    _synth_raster(cudem, _EXHIBIT_BBOX, -5.0)
    _synth_raster(etopo, _EXHIBIT_BBOX, -30.0)
    _synth_raster(land, _EXHIBIT_BBOX, 12.0)
    monkeypatch.setattr(tb, "_select_cudem_tiles",
                        lambda *_a, **_k: list(_FULL_COVER_TILES))
    monkeypatch.setattr(tb, "_select_etopo_tiles", lambda *_a, **_k: [etopo])
    monkeypatch.setattr(tb, "_assert_navd88", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(tb, "_fetch_3dep_land_to_file", lambda *_a, **_k: land)
    real = tb._composite_sources_to_array

    def _strip(sources, target_crs, bbox, **kw):
        out = []
        for s in sources:
            bare = s[len("/vsicurl/"):] if s.startswith("/vsicurl/") else s
            if bare == _DROPPED_TILE:
                out.append(str(tmp_path / "gone.tif"))  # unreadable -> skipped
            elif bare in _FULL_COVER_TILES:
                out.append(cudem)
            else:
                out.append(bare)
        return real(out, target_crs, bbox, **kw)

    monkeypatch.setattr(tb, "_composite_sources_to_array", _strip)


def test_a_tile_that_drops_mid_merge_raises_the_gap_not_a_silent_land_fill(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """The footprint gate promised full coverage; the merge must reconcile the
    promise against what ACTUALLY painted rather than land-fill the hole."""
    _patch_full_cudem_with_one_unreadable_tile(monkeypatch, tmp_path)
    tb.validate_topobathy(None, {"bbox": list(_EXHIBIT_BBOX)})  # footprint says OK

    with pytest.raises(tb.TopobathyCoverageGapError) as ei:
        TOOL_REGISTRY["fetch_topobathy"].fn(bbox=list(_EXHIBIT_BBOX))
    exc = ei.value
    assert exc.covered_fraction == pytest.approx(0.5555556, abs=1e-5)
    assert "PAINTED only" in str(exc) and "3 of the 4" in str(exc)


def test_the_declared_rung_fills_a_mid_merge_drop_too(
    monkeypatch, tmp_path, fake_s3
) -> None:
    _patch_full_cudem_with_one_unreadable_tile(monkeypatch, tmp_path)
    res = TOOL_REGISTRY["fetch_topobathy"].fn(
        bbox=list(_EXHIBIT_BBOX), fallback=("etopo_bathy_base",)
    )
    assert isinstance(res, TopobathyResult)
    assert "PARTIAL-CUDEM BATHYMETRY" in (res.fallback_warning or "")


def test_an_exempted_request_never_stamps_a_positive_false_coverage_row(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """force_bathy_base skips the coverage check; the raster is part ETOPO, so a
    'cudem_nearshore / coverage=1.0' row would be an affirmatively false claim."""
    _patch_partial_cudem(monkeypatch, tmp_path)
    res = TOOL_REGISTRY["fetch_topobathy"].fn(
        bbox=list(_EXHIBIT_BBOX), force_bathy_base=True
    )
    assert list(res.fallbacks or []) == []
    assert "PARTIAL-CUDEM BATHYMETRY" in (res.fallback_warning or "")


def test_emit_seam_carries_activation_rows_onto_a_reemitted_layer() -> None:
    from trid3nt_contracts.common import FallbackActivation
    from trid3nt_contracts.execution import LayerURI
    from trid3nt_server.emission.layer_uri_emit import emit_layer_uri

    layer = LayerURI(
        layer_id="input-topobathy-abc", name="Coastal bed", layer_type="raster",
        uri="s3://bucket/bed.tif", style_preset="continuous_dem", role="context",
    )
    rows = [
        FallbackActivation(capability="fetch_topobathy", rung="cudem_nearshore",
                           consequence="primary", coverage=0.889),
        FallbackActivation(capability="fetch_topobathy", rung="etopo_bathy_base",
                           consequence="cross_dataset", coverage=0.111),
    ]
    out = emit_layer_uri(layer, fallbacks=rows)
    assert out is not None
    assert [r.rung for r in out.fallbacks] == ["cudem_nearshore", "etopo_bathy_base"]
    assert "11% etopo_bathy_base [cross_dataset]" in (out.fallback_note or "")
    # Idempotent: a second pass neither duplicates rows nor the narration.
    again = emit_layer_uri(out, fallbacks=rows)
    assert len(again.fallbacks) == 2
    assert again.fallback_note == out.fallback_note


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


# --------------------------------------------------------------------------- #
# Every exposed caller DECLARES its rung at the call site (F1b, NATE's
# migrate-all ruling). A declared constant nobody passes is not a policy: these
# assert the kwarg reaches the fetch, not just that the tuple exists.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "module_path,func_name,constant",
    [
        ("trid3nt_server.workflows.swan.wave_field.wave_field",
         "_fetch_bathy_for_swan", "_SWAN_BATHY_FALLBACK"),
        ("trid3nt_server.workflows.geoclaw.inundation.inundation",
         "_fetch_topo_for_geoclaw", "_GEOCLAW_BATHY_FALLBACK"),
        ("trid3nt_server.workflows.schism.tidal_hydro.tidal_hydro",
         "_fetch_bathymetry_cog", "_SCHISM_BATHY_FALLBACK"),
    ],
)
def test_composer_call_site_passes_its_declared_fallback(
    module_path: str, func_name: str, constant: str
) -> None:
    import importlib
    import inspect

    mod = importlib.import_module(module_path)
    src = inspect.getsource(getattr(mod, func_name))
    assert f"fallback={constant}" in src or f'"fallback"] = {constant}' in src, (
        f"{func_name} declares {constant} but does not pass it to the fetch"
    )
    ladder = get_ladder("fetch_topobathy")
    assert all(ladder.alternative(n) is not None for n in getattr(mod, constant))


def test_sfincs_coastal_call_site_passes_its_declared_fallback() -> None:
    import inspect

    from trid3nt_server.workflows.sfincs.flood import flood as fl

    src = inspect.getsource(fl.model_flood_scenario)
    assert "fallback=_SFINCS_BATHY_FALLBACK" in src
    ladder = get_ladder("fetch_topobathy")
    assert all(
        ladder.alternative(n) is not None for n in fl._SFINCS_BATHY_FALLBACK
    )


def test_sfincs_handles_a_ladder_refusal_as_a_failed_envelope() -> None:
    """A declined/failed rung raises LadderRefused, not TopobathyError: the
    coastal handler must catch it or the run dies as an untyped crash."""
    import inspect

    from trid3nt_server.workflows.sfincs.flood import flood as fl

    src = inspect.getsource(fl.model_flood_scenario)
    assert "except (TopobathyError, LadderRefused) as exc:" in src


def _stub_registry_entry(fn: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(fn=fn)


def test_geoclaw_walks_the_rung_and_never_reaches_the_land_only_dem(
    monkeypatch, tmp_path, fake_s3
) -> None:
    from trid3nt_server.workflows.geoclaw.inundation import inundation as gc

    _patch_partial_cudem(monkeypatch, tmp_path)
    dem_calls: list[dict] = []
    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_dem",
        _stub_registry_entry(lambda **kw: dem_calls.append(kw)),
    )

    sink: list[Any] = []
    uri, label = gc._fetch_topo_for_geoclaw(_EXHIBIT_BBOX, activation_sink=sink)

    assert uri.startswith("s3://")
    assert {r.rung for r in sink} == {"cudem_nearshore", "etopo_bathy_base"}
    assert "etopo_bathy_base [cross_dataset]" in label
    assert dem_calls == [], "the land-only 3DEP DEM must never serve a GeoClaw bed"


def test_geoclaw_declining_the_rung_refuses_instead_of_land_filling(
    monkeypatch, tmp_path, fake_s3
) -> None:
    from trid3nt_server.gates import fallback as gate_mod
    from trid3nt_server.workflows.geoclaw.inundation import inundation as gc

    _patch_partial_cudem(monkeypatch, tmp_path)
    monkeypatch.setattr(gate_mod, "confirm_fallback", lambda **_k: False)
    dem_calls: list[dict] = []
    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_dem",
        _stub_registry_entry(lambda **kw: dem_calls.append(kw)),
    )

    with pytest.raises(gc.GeoClawComposerError) as ei:
        gc._fetch_topo_for_geoclaw(_EXHIBIT_BBOX)
    assert ei.value.error_code == "GEOCLAW_NO_BATHYMETRY"
    assert "declined at the fallback gate" in str(ei.value)
    assert dem_calls == []


def test_schism_walks_the_rung_and_never_reaches_the_land_only_dem(
    monkeypatch, tmp_path, fake_s3
) -> None:
    import asyncio

    from trid3nt_server.workflows.schism.tidal_hydro import tidal_hydro as sc

    _patch_partial_cudem(monkeypatch, tmp_path)
    dem_calls: list[dict] = []
    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_dem",
        _stub_registry_entry(lambda **kw: dem_calls.append(kw)),
    )
    monkeypatch.setattr(sc, "_download_uri_to_tmp", lambda uri: "/tmp/bed.tif")

    sink: list[Any] = []
    local, label = asyncio.run(
        sc._fetch_bathymetry_cog(list(_EXHIBIT_BBOX), activation_sink=sink)
    )
    assert (local, label) == ("/tmp/bed.tif", "topobathy")
    assert {r.rung for r in sink} == {"cudem_nearshore", "etopo_bathy_base"}
    assert dem_calls == []


def test_schism_declining_the_rung_refuses_instead_of_land_filling(
    monkeypatch, tmp_path, fake_s3
) -> None:
    import asyncio

    from trid3nt_server.gates import fallback as gate_mod
    from trid3nt_server.workflows.schism.tidal_hydro import tidal_hydro as sc

    _patch_partial_cudem(monkeypatch, tmp_path)
    monkeypatch.setattr(gate_mod, "confirm_fallback", lambda **_k: False)
    dem_calls: list[dict] = []
    monkeypatch.setitem(
        TOOL_REGISTRY, "fetch_dem",
        _stub_registry_entry(lambda **kw: dem_calls.append(kw)),
    )

    with pytest.raises(sc.SchismScenarioError) as ei:
        asyncio.run(sc._fetch_bathymetry_cog(list(_EXHIBIT_BBOX)))
    assert ei.value.error_code == "SCHISM_BATHYMETRY_UNAVAILABLE"
    assert dem_calls == []


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
