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
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers._router.hooks import topobathy as tb
from trid3nt_server.fallbacks import (
    LADDER_ERROR_CODE,
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


def test_a_rung_below_the_primary_must_carry_a_below_primary_class() -> None:
    with pytest.raises(ValueError, match="below the"):
        Ladder(
            capability="c",
            rungs=(_rung("primary", "primary"), _rung("b", "refuse")),
            refuse_error_code="X",
        )


def test_an_enhancement_rung_is_declarable_but_not_permittable() -> None:
    """A source FINER than the primary is declared so the walker can name what
    painted -- but ``fallback=`` is how a caller accepts a COST, and this rung
    has none, so it must not be permittable by name."""
    lad = Ladder(
        capability="c",
        rungs=(_rung("primary", "primary"), _rung("fine", "enhancement")),
        refuse_error_code="X",
    )
    assert [r.name for r in lad.alternatives] == []
    assert lad.alternative("fine") is None
    with pytest.raises(LadderRefused, match="declares no alternative rung"):
        walk_ladder(lad, params={}, attempt=lambda _r, _p: object(),
                    allow=("fine",), gate=lambda **_k: True)


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


def test_undeclared_gap_refuses_with_the_ladders_typed_code() -> None:
    """A gap with no error_code of its own wears the ladder's terminal code --
    it is a coverage refusal, and callers dispatch on error_code."""
    def _attempt(_r: Any, _p: Any) -> Any:
        raise LadderGap("gap!", covered_fraction=0.889, gap_note="CUDEM stops here")

    with pytest.raises(LadderRefused) as ei:
        walk_ladder(_ladder(_rung("alt", "cross_dataset")), params={},
                    attempt=_attempt, gate=lambda **_k: True)
    assert ei.value.error_code == "TEST_REFUSED"
    assert "CUDEM stops here" in str(ei.value)
    assert isinstance(ei.value.__cause__, LadderGap)
    assert [r.rung for r in ei.value.activation.records] == ["primary"]


def test_a_typed_gap_still_propagates_verbatim() -> None:
    """The capability's OWN typed gap is untouched: same class, same code."""
    class _TypedGap(LadderGap):
        error_code = "CAP_GAP"
        retryable = False

    def _attempt(_r: Any, _p: Any) -> Any:
        raise _TypedGap("gap!", covered_fraction=0.5, gap_note="half")

    with pytest.raises(_TypedGap) as ei:
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


def test_a_decline_over_a_retryable_primary_keeps_the_primarys_own_error() -> None:
    """No gap was recorded: the primary failed for its OWN reason (a 503) and the
    gate question was moot. Answering with a non-retryable coverage refusal would
    tell the caller its transient upstream error is a terminal data gap."""
    class _Upstream(Exception):
        error_code = "CAP_UPSTREAM"
        retryable = True

    def _attempt(rung: Any, _p: Any) -> Any:
        raise _Upstream("CUDEM 503")

    with pytest.raises(_Upstream) as ei:
        walk_ladder(_ladder(_rung("alt", "cross_dataset")), params={},
                    attempt=_attempt, allow=("alt",), gate=lambda **_k: False)
    assert ei.value.error_code == "CAP_UPSTREAM"
    assert ei.value.retryable is True
    # The decline still left its trace on the activation.
    rows = {r.rung: r for r in ei.value.fallback_activation.records}
    assert rows["alt"].declined is True


def test_measured_paint_overrides_the_promise_on_a_rung_attempt() -> None:
    """The promise said 89/11; the rung's own fetch MEASURED 44/56. Rows report
    what painted -- a rung-injected param never exempts its attempt from that."""
    class _Result:
        rung_coverage = {"primary": 0.44, "alt": 0.56}

    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise LadderGap("gap", covered_fraction=0.89, gap_note="CUDEM stops")
        return _Result()

    ladder = Ladder(
        capability="test_cap",
        rungs=(_rung("primary", "primary"),
               _rung("alt", "cross_dataset", params={"force_base": True})),
        refuse_error_code="TEST_REFUSED",
        coverage_exempt_params=("force_base",),
    )
    _res, act = walk_ladder(ladder, params={}, attempt=_attempt, allow=("alt",),
                            gate=lambda **_k: True)
    assert act.coverage_unverified is False
    rows = {r.rung: r.coverage for r in act.to_contract()}
    assert rows == pytest.approx({"primary": 0.44, "alt": 0.56})


def test_an_alternative_serving_an_exempted_request_stamps_no_number() -> None:
    """The exemption is the REQUEST's, so it applies to whichever rung serves --
    not only the primary."""
    ladder = Ladder(
        capability="test_cap",
        rungs=(_rung("primary", "primary"), _rung("alt", "cross_dataset")),
        refuse_error_code="TEST_REFUSED",
        coverage_exempt_params=("force_base",),
    )

    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise RuntimeError("primary unavailable")
        return "served"

    _res, act = walk_ladder(ladder, params={"force_base": True}, attempt=_attempt,
                            allow=("alt",), gate=lambda **_k: True)
    assert act.coverage_unverified is True
    assert act.to_contract() == []
    assert "UNMEASURED" in (act.narration() or "")


def test_gap_plus_a_faulted_filling_rung_is_not_a_coverage_refusal() -> None:
    """A recorded gap whose filling rung fell over for its OWN reason is a LADDER
    error, not the capability's coverage code: nothing proved the gap unfillable,
    and a composer excepting on the coverage code would call the AOI sourceless."""
    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise LadderGap("gap", covered_fraction=0.5, gap_note="half the AOI")
        raise RuntimeError("ETOPO host unreachable")

    with pytest.raises(LadderRefused) as ei:
        walk_ladder(_ladder(_rung("alt", "cross_dataset")), params={},
                    attempt=_attempt, allow=("alt",), gate=lambda **_k: True)
    assert ei.value.error_code == LADDER_ERROR_CODE != "TEST_REFUSED"
    assert ei.value.retryable is False  # a bare RuntimeError claims no retry
    assert "half the AOI" in str(ei.value)          # the gap context
    assert "ETOPO host unreachable" in str(ei.value)  # AND the cause
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_gap_plus_a_RETRYABLE_fill_failure_stays_retryable() -> None:
    """The production shape: CUDEM paints 89%, the permitted ETOPO rung hits a
    MinIO hiccup. A transport fault must not read as 'this AOI has no bathymetry
    source' -- it wears the ladder code and keeps its retryability."""
    class _Transient(Exception):
        retryable = True

    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise LadderGap("gap", covered_fraction=0.89, gap_note="CUDEM stops")
        raise _Transient("EndpointConnectionError: MinIO unreachable")

    with pytest.raises(LadderRefused) as ei:
        walk_ladder(_ladder(_rung("alt", "cross_dataset")), params={},
                    attempt=_attempt, allow=("alt",), gate=lambda **_k: True)
    assert ei.value.error_code == LADDER_ERROR_CODE
    assert ei.value.retryable is True
    assert "CUDEM stops" in str(ei.value)
    assert "MinIO unreachable" in str(ei.value)


def test_gap_no_rung_permitted_keeps_the_capabilitys_coverage_code() -> None:
    """The GENUINE coverage refusal: nothing was permitted to fill the gap, so the
    capability's own typed gap error surfaces verbatim."""
    gap = LadderGap("gap", covered_fraction=0.5, gap_note="half the AOI")
    setattr(gap, "error_code", "TEST_REFUSED")

    def _attempt(_r: Any, _p: Any) -> Any:
        raise gap

    with pytest.raises(LadderGap) as ei:
        walk_ladder(_ladder(_rung("alt", "cross_dataset")), params={},
                    attempt=_attempt, gate=lambda **_k: True)
    assert ei.value is gap and ei.value.error_code == "TEST_REFUSED"


def test_a_filling_rung_that_also_gaps_keeps_the_coverage_code() -> None:
    """Both rungs measured a gap, so the refusal IS about coverage."""
    primary_gap = LadderGap("gap", covered_fraction=0.5, gap_note="half the AOI")
    setattr(primary_gap, "error_code", "TEST_REFUSED")

    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise primary_gap
        raise LadderGap("gap2", covered_fraction=0.7, gap_note="still 30% short")

    with pytest.raises(LadderGap) as ei:
        walk_ladder(_ladder(_rung("alt", "cross_dataset")), params={},
                    attempt=_attempt, allow=("alt",), gate=lambda **_k: True)
    assert ei.value.error_code == "TEST_REFUSED"


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
    """A call-site bug, but still TYPED: an untyped escape would slip past the
    composers' LadderRefused/LadderGap excepts into their catch-all."""
    with pytest.raises(LadderRefused, match="declares no alternative rung") as ei:
        walk_ladder(_ladder(), params={}, attempt=lambda _r, _p: "x",
                    allow=("nope",), gate=lambda **_k: True)
    assert ei.value.error_code == LADDER_ERROR_CODE
    assert ei.value.retryable is False


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
    assert ei.value.error_code == LADDER_ERROR_CODE
    assert "kaboom" in str(ei.value)
    assert isinstance(ei.value.__cause__, RuntimeError)


def test_an_infra_error_is_not_dressed_as_a_coverage_gap() -> None:
    """A bare ValueError from the cache / transport under a rung must not wear
    the capability's coverage code -- a composer excepting on that code would
    read a transient fault as a terminal data gap. Retryability rides through."""
    class _Retryable(Exception):
        retryable = True

    def _attempt(_r: Any, _p: Any) -> Any:
        raise _Retryable("cache bucket unreachable")

    with pytest.raises(LadderRefused) as ei:
        walk_ladder(_ladder(), params={}, attempt=_attempt, gate=lambda **_k: True)
    assert ei.value.error_code == LADDER_ERROR_CODE != "TEST_REFUSED"
    assert ei.value.retryable is True
    assert "nothing measured a coverage gap" in str(ei.value)


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
    # Not silent, though: the exempted serve narrates that nothing measured it.
    assert "UNMEASURED" in (act.narration() or "")
    assert "force_base" in (act.narration() or "")

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


def test_auto_mode_refuses_a_synthetic_rung_without_asking_a_live_session(
    monkeypatch,
) -> None:
    """AUTO means nobody is being asked, emitter or no emitter. Keying the ask on
    the presence of a channel stalled an auto run for the whole gate TTL; the
    labeled default (refuse, law 9) applies immediately -- the input-review
    gate's auto semantics."""
    import asyncio as _asyncio

    from trid3nt_server.emission import pipeline_emitter as pe

    class _Loop:
        @staticmethod
        def is_running() -> bool:
            return True

    class _Emitter:
        session_id = "sess"
        _bound_loop = _Loop()

        async def send_envelope(self, kind: str, env: Any) -> None:
            raise AssertionError("auto mode must never present a gate card")

    def _never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("auto mode must never wait on a gate answer")

    monkeypatch.setattr(_asyncio, "run_coroutine_threadsafe", _never)
    token = pe._CURRENT_EMITTER.set(_Emitter())
    try:
        assert confirm_fallback(capability="c", rung=_rung("s", "synthetic"),
                                gate_mode="auto") is False
    finally:
        pe._CURRENT_EMITTER.reset(token)


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


def test_rows_report_measured_paint_not_the_footprint_promise(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """The footprint PROMISE is 89/11. On the rung's own attempt the biggest tile
    drops, so CUDEM actually paints 44%. The stamped rows must be the measured
    44/56 -- a rung-injected param never exempts the rung's attempt from paint
    accounting."""
    _patch_partial_cudem(monkeypatch, tmp_path)
    real = tb._composite_sources_to_array
    dropped = "ncei19_n30X00_w085X50_2019v1.tif"
    gone = str(tmp_path / "gone.tif")

    def _drop_one(sources, target_crs, bbox, **kw):
        return real([gone if s.endswith(dropped) else s for s in sources],
                    target_crs, bbox, **kw)

    monkeypatch.setattr(tb, "_composite_sources_to_array", _drop_one)
    res = TOOL_REGISTRY["fetch_topobathy"].fn(
        bbox=list(_EXHIBIT_BBOX), fallback=("etopo_bathy_base",)
    )
    rows = {r.rung: r.coverage for r in res.fallbacks}
    assert rows == pytest.approx(
        {"cudem_nearshore": 4.0 / 9.0, "etopo_bathy_base": 5.0 / 9.0}, abs=1e-6
    )


def test_a_declined_rung_stays_on_the_contract_when_a_lower_rung_serves() -> None:
    """The declined row's production reader: a walk that descends PAST a declined
    rung stamps both -- what was refused and what actually served."""
    ladder = Ladder(
        capability="test_cap",
        rungs=(_rung("primary", "primary"), _rung("alt", "cross_dataset"),
               _rung("mirror", "same_data")),
        refuse_error_code="TEST_REFUSED",
    )

    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise LadderGap("gap", covered_fraction=0.5, gap_note="half missing")
        if rung.name == "alt":
            raise AssertionError("a declined rung must never be invoked")
        return "merged"

    _res, act = walk_ladder(
        ladder, params={}, attempt=_attempt, allow=("alt", "mirror"),
        gate=lambda **kw: kw["rung"].name != "alt",
    )
    rows = {r.rung: r for r in act.to_contract()}
    assert rows["alt"].coverage == 0.0 and "declined" in (rows["alt"].note or "")
    assert rows["mirror"].coverage == pytest.approx(0.5)


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
    # ... but it is not SILENT: the model-followable remedy carries its own
    # unverified note, and the adapter hoists the warning out of the repr clip.
    assert "UNMEASURED" in (res.fallback_note or "")
    assert "force_bathy_base" in (res.fallback_note or "")
    from trid3nt_server.adapters.adapter import summarize_tool_result

    payload = summarize_tool_result("fetch_topobathy", res)
    assert "PARTIAL-CUDEM BATHYMETRY" in payload["fallback_warning"]
    assert "UNMEASURED" in payload["fallback_note"]


def _patch_total_cudem_loss(monkeypatch, tmp_path) -> None:
    """Every intersecting CUDEM tile drops at the datum gate. ETOPO auto-engages
    because zero tiles survive -- the window where the merge used to return
    SUCCESS with a raw 3DEP land fill painting the water."""
    etopo = str(tmp_path / "etopo.tif")
    _synth_raster(etopo, _EXHIBIT_BBOX, -30.0)
    monkeypatch.setattr(tb, "_select_cudem_tiles",
                        lambda *_a, **_k: list(_FULL_COVER_TILES))
    monkeypatch.setattr(tb, "_select_etopo_tiles", lambda *_a, **_k: [etopo])

    def _reject(*_a: Any, **_k: Any) -> float:
        raise tb.TopobathyUpstreamError("header unreadable")

    # The merge unlinks the staged land tif, so each attempt stages its own (as
    # the real 3DEP leg does). The fill is 0.0 -- the 3DEP land DEM's flat
    # sea-level OCEAN fill, the value _mask_land_leg_ocean_fill exists to drop.
    # Staging emergent land here would make the mask a no-op and leave the
    # clobber-the-ETOPO-column fix unproven by its own evidence.
    def _stage_land(*_a: Any, **_k: Any) -> str:
        path = str(tmp_path / f"land-{len(list(tmp_path.glob('land-*.tif')))}.tif")
        _synth_raster(path, _EXHIBIT_BBOX, 0.0)
        return path

    monkeypatch.setattr(tb, "_assert_navd88", _reject)
    monkeypatch.setattr(tb, "_fetch_3dep_land_to_file", _stage_land)
    real = tb._composite_sources_to_array

    def _strip(sources, target_crs, bbox, **kw):
        return real(
            [(s[len("/vsicurl/"):] if s.startswith("/vsicurl/") else s)
             for s in sources],
            target_crs, bbox, **kw,
        )

    monkeypatch.setattr(tb, "_composite_sources_to_array", _strip)


def test_total_cudem_loss_refuses_instead_of_returning_a_land_fill_success(
    monkeypatch, tmp_path, fake_s3
) -> None:
    _patch_total_cudem_loss(monkeypatch, tmp_path)
    tb.validate_topobathy(None, {"bbox": list(_EXHIBIT_BBOX)})  # footprint says OK
    with pytest.raises(tb.TopobathyCoverageGapError) as ei:
        TOOL_REGISTRY["fetch_topobathy"].fn(bbox=list(_EXHIBIT_BBOX))
    assert ei.value.covered_fraction == 0.0
    assert "0 of the 4" in str(ei.value)


def test_total_cudem_loss_serves_the_declared_rung_with_measured_paint(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """The declared rung fills the whole AOI, and the rows say so -- 0% CUDEM /
    100% ETOPO is MEASURED paint, not the walker's promise arithmetic."""
    _patch_total_cudem_loss(monkeypatch, tmp_path)
    res = TOOL_REGISTRY["fetch_topobathy"].fn(
        bbox=list(_EXHIBIT_BBOX), fallback=("etopo_bathy_base",)
    )
    # cudem_nearshore painted NOTHING, so it carries no row at all (a 0% row is
    # not a claim); ETOPO's row is the measured 100%.
    rows = {r.rung: r.coverage for r in res.fallbacks}
    assert rows == pytest.approx({"etopo_bathy_base": 1.0})
    assert res.cudem_tile_count == 0
    assert res.bathymetry_present is True
    assert res.rung_coverage == {"cudem_nearshore": 0.0, "etopo_bathy_base": 1.0}


def test_the_land_legs_flat_ocean_fill_never_reaches_the_served_bed(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """N5's evidence: the 3DEP leg is staged at its real 0 m OCEAN fill, sits at
    higher precedence than ETOPO, and must be masked out of the composite. Every
    served cell is the ETOPO bed (-30 m), not sea-level land fill."""
    _patch_total_cudem_loss(monkeypatch, tmp_path)
    seen: dict[str, Any] = {}
    real = tb._composite_sources_to_array

    def _spy(sources, target_crs, bbox, **kw):
        out = real(sources, target_crs, bbox, **kw)
        seen["arr"] = out[0]
        return out

    monkeypatch.setattr(tb, "_composite_sources_to_array", _spy)
    TOOL_REGISTRY["fetch_topobathy"].fn(
        bbox=list(_EXHIBIT_BBOX), fallback=("etopo_bathy_base",)
    )
    arr = seen["arr"]
    assert float(np.nanmax(arr)) < 0.0, "the land leg's 0 m ocean fill survived"
    assert float(np.nanmin(arr)) == pytest.approx(-30.0, abs=1.0)


# --------------------------------------------------------------------------- #
# A GAP + a faulted filling rung, end to end: what the composers actually see.
# --------------------------------------------------------------------------- #


class _Transient(Exception):
    """A transport hiccup (MinIO/S3), the shape that must stay retryable."""

    retryable = True


def _fault_the_rungs_fetch(monkeypatch) -> None:
    """CUDEM's footprint gap is measured PRE-fetch; the rung's own read then
    faults on the cache/transport edge."""
    from trid3nt_server.tools.fetchers._router import router as router_mod

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise _Transient("EndpointConnectionError: could not connect to MinIO")

    monkeypatch.setattr(router_mod, "read_through", _boom)


def test_a_transport_fault_on_the_filling_rung_is_not_a_bathymetry_verdict(
    monkeypatch, tmp_path, fake_s3
) -> None:
    _patch_partial_cudem(monkeypatch, tmp_path)
    _fault_the_rungs_fetch(monkeypatch)
    with pytest.raises(LadderRefused) as ei:
        TOOL_REGISTRY["fetch_topobathy"].fn(
            bbox=list(_EXHIBIT_BBOX), fallback=("etopo_bathy_base",)
        )
    assert ei.value.error_code == LADDER_ERROR_CODE != "TOPOBATHY_COVERAGE_GAP"
    assert ei.value.retryable is True
    assert "89% of AOI" in str(ei.value)             # the gap context
    assert "MinIO" in str(ei.value)                  # AND the cause
    assert isinstance(ei.value.__cause__, _Transient)


def test_geoclaw_propagates_a_ladder_fault_instead_of_calling_the_coast_bedless(
    monkeypatch, tmp_path, fake_s3
) -> None:
    from trid3nt_server.workflows.geoclaw.inundation import inundation as gi

    _patch_partial_cudem(monkeypatch, tmp_path)
    _fault_the_rungs_fetch(monkeypatch)
    with pytest.raises(LadderRefused) as ei:
        gi._fetch_topo_for_geoclaw(_EXHIBIT_BBOX)
    assert ei.value.error_code == LADDER_ERROR_CODE
    assert ei.value.retryable is True


def test_geoclaw_still_refuses_terminally_on_a_REAL_coverage_gap(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """The other side of the branch: a coverage-coded refusal is still fatal --
    the land-only fetch_dem leg is never reached."""
    from trid3nt_server.workflows.geoclaw.inundation import inundation as gi

    _patch_partial_cudem(monkeypatch, tmp_path)
    monkeypatch.setattr(tb, "_select_etopo_tiles", lambda *_a, **_k: [])
    with pytest.raises(gi.GeoClawComposerError) as ei:
        gi._fetch_topo_for_geoclaw(_EXHIBIT_BBOX)
    assert ei.value.error_code == "GEOCLAW_NO_BATHYMETRY"


def test_schism_propagates_a_ladder_fault_with_its_retryability(
    monkeypatch, tmp_path, fake_s3
) -> None:
    import asyncio

    from trid3nt_server.workflows.schism.tidal_hydro import tidal_hydro as sc

    _patch_partial_cudem(monkeypatch, tmp_path)
    _fault_the_rungs_fetch(monkeypatch)
    with pytest.raises(LadderRefused) as ei:
        asyncio.run(sc._fetch_bathymetry_cog(list(_EXHIBIT_BBOX)))
    assert ei.value.error_code == LADDER_ERROR_CODE
    assert ei.value.retryable is True


# --------------------------------------------------------------------------- #
# MEASURED paint: a partial ETOPO base may not claim the whole remainder.
# --------------------------------------------------------------------------- #


def _patch_total_cudem_loss_with_half_an_etopo(monkeypatch, tmp_path) -> None:
    """Every CUDEM tile drops AND the ETOPO base reaches only the west half (the
    AOI straddles a 15-degree ETOPO tile boundary and one tile is unreadable)."""
    _patch_total_cudem_loss(monkeypatch, tmp_path)
    half = str(tmp_path / "etopo-half.tif")
    _synth_raster(half, (-85.55, 29.70, -85.475, 29.85), -30.0)
    monkeypatch.setattr(tb, "_select_etopo_tiles", lambda *_a, **_k: [half])


def test_a_half_reaching_etopo_base_refuses_rather_than_claiming_a_bed_everywhere(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """The ETOPO leg painted ~48% of the AOI. Stamping etopo/1.0 and a 'REAL
    below-waterline bed everywhere' warning over a half-NaN raster is the exact
    lie the coverage gate exists to prevent."""
    _patch_total_cudem_loss_with_half_an_etopo(monkeypatch, tmp_path)
    with pytest.raises(tb.TopobathyCoverageGapError) as ei:
        TOOL_REGISTRY["fetch_topobathy"].fn(
            bbox=list(_EXHIBIT_BBOX), fallback=("etopo_bathy_base",)
        )
    exc = ei.value
    assert "painted by nothing" in str(exc)
    # ... and it does not advertise a remedy that was already tried and failed.
    assert "force_bathy_base=true" not in str(exc)
    assert "no param that makes this request honest" in str(exc)


def test_a_half_reaching_etopo_base_refuses_under_force_bathy_base_too(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """An exemption buys a COARSER bed, never a bed with holes in it."""
    _patch_total_cudem_loss_with_half_an_etopo(monkeypatch, tmp_path)
    with pytest.raises(tb.TopobathyCoverageGapError):
        TOOL_REGISTRY["fetch_topobathy"].fn(
            bbox=list(_EXHIBIT_BBOX), force_bathy_base=True
        )


def test_a_partial_bed_with_no_cudem_gap_is_still_a_gap(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """CUDEM never intersected this AOI (no CUDEM gap to have), and the ETOPO base
    that auto-engaged reaches half of it. The old code returned SUCCESS with
    bathymetry_present=True over a 50%-NaN raster."""
    etopo = str(tmp_path / "etopo-half.tif")
    land = str(tmp_path / "land.tif")
    _synth_raster(etopo, (-85.55, 29.70, -85.475, 29.85), -30.0)
    _synth_raster(land, _EXHIBIT_BBOX, 12.0)
    monkeypatch.setattr(tb, "_select_cudem_tiles", lambda *_a, **_k: [])
    monkeypatch.setattr(tb, "_select_etopo_tiles", lambda *_a, **_k: [etopo])
    monkeypatch.setattr(tb, "_fetch_3dep_land_to_file", lambda *_a, **_k: land)
    real = tb._composite_sources_to_array
    monkeypatch.setattr(
        tb, "_composite_sources_to_array",
        lambda s, c, b, **kw: real(
            [(x[len("/vsicurl/"):] if x.startswith("/vsicurl/") else x) for x in s],
            c, b, **kw,
        ),
    )
    with pytest.raises(tb.TopobathyCoverageGapError) as ei:
        TOOL_REGISTRY["fetch_topobathy"].fn(bbox=list(_EXHIBIT_BBOX))
    assert "PAINTS a real below-waterline bed over only" in str(ei.value)
    assert ei.value.covered_fraction < 0.6


# --------------------------------------------------------------------------- #
# The NCEI regional FINE leg fills the hole -- and is not ignored.
# --------------------------------------------------------------------------- #


def _patch_partial_cudem_plus_regional_fine(monkeypatch, tmp_path) -> None:
    """CUDEM covers 89%; the FINER NCEI regional coastal DEM covers the AOI. No
    ETOPO leg engages at all (include_regional_fine never forces the base on)."""
    cudem = str(tmp_path / "cudem.tif")
    regional = str(tmp_path / "regional.tif")
    land = str(tmp_path / "land.tif")
    _synth_raster(cudem, _EXHIBIT_BBOX, -5.0)
    _synth_raster(regional, _EXHIBIT_BBOX, -7.0)
    _synth_raster(land, _EXHIBIT_BBOX, 12.0)
    monkeypatch.setattr(tb, "_select_cudem_tiles", lambda *_a, **_k: list(_EXHIBIT_TILES))
    monkeypatch.setattr(tb, "_select_etopo_tiles", lambda *_a, **_k: [])
    monkeypatch.setattr(tb, "_assert_navd88", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(tb, "_fetch_3dep_land_to_file", lambda *_a, **_k: land)
    monkeypatch.setattr(
        tb, "_select_regional_coastal_dem_tiles",
        lambda *_a, **_k: (["https://x/regional_ncei.tif"], ["CoNED_test"]),
    )
    real = tb._composite_sources_to_array
    remap = {t: cudem for t in _EXHIBIT_TILES}
    remap["https://x/regional_ncei.tif"] = regional

    def _strip(sources, target_crs, bbox, **kw):
        out = []
        for s in sources:
            bare = s[len("/vsicurl/"):] if s.startswith("/vsicurl/") else s
            out.append(remap.get(bare, bare))
        return real(out, target_crs, bbox, **kw)

    monkeypatch.setattr(tb, "_composite_sources_to_array", _strip)


def test_a_finer_regional_bed_that_fills_the_hole_serves_instead_of_refusing(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """The false refusal: the merge required an ETOPO base to accept an exempted
    partial-CUDEM AOI, but include_regional_fine never engages ETOPO -- so a FINER
    bed that fully painted the hole was refused as 'NO nearshore source'."""
    _patch_partial_cudem_plus_regional_fine(monkeypatch, tmp_path)
    res = TOOL_REGISTRY["fetch_topobathy"].fn(
        bbox=list(_EXHIBIT_BBOX), include_regional_fine=True
    )
    assert isinstance(res, TopobathyResult)
    assert res.regional_tile_count == 1
    assert res.bathymetry_present is True
    warning = res.fallback_warning or ""
    assert "PARTIAL-CUDEM BATHYMETRY" in warning
    assert "NCEI REGIONAL fine coastal DEM" in warning
    assert "89%" in warning and "11%" in warning


def test_the_regional_share_is_measured_not_credited_to_etopo(
    monkeypatch, tmp_path
) -> None:
    """The share map is per-source paint: ETOPO gets 0 because ETOPO painted
    nothing, even though 11% of the AOI is not CUDEM."""
    _patch_partial_cudem_plus_regional_fine(monkeypatch, tmp_path)
    _arr, _t, _c, prov = tb._select_and_merge(
        _EXHIBIT_BBOX, 10, tb.TARGET_CRS, None, 30.0,
        False, True, None, False, False,
    )
    coverage = prov["rung_coverage"]
    assert coverage["cudem_nearshore"] == pytest.approx(8.0 / 9.0, abs=1e-6)
    assert coverage["etopo_bathy_base"] == 0.0
    assert coverage["regional_fine"] == pytest.approx(1.0 / 9.0, abs=1e-6)


def test_the_finer_contributor_is_a_declared_row_not_a_warning(
    monkeypatch, tmp_path, caplog
) -> None:
    """regional_fine is an ``enhancement`` rung: it lands on the contract as a
    named row, with no unknown-key warning and no GATE-UNSEEN mark. A model
    reading only the declared rungs can now account for the whole raster."""
    _patch_partial_cudem_plus_regional_fine(monkeypatch, tmp_path)
    ladder = get_ladder("fetch_topobathy")

    class _Result:
        rung_coverage = {
            "cudem_nearshore": 8.0 / 9.0, "regional_fine": 1.0 / 9.0,
            "etopo_bathy_base": 0.0,
        }

    with caplog.at_level("WARNING", logger="trid3nt_server.fallbacks.walker"):
        _res, act = walk_ladder(ladder, params={}, attempt=lambda _r, _p: _Result(),
                                gate=lambda **_k: True)
    assert "declares no rung" not in caplog.text
    assert "sum to" not in caplog.text
    rows = {r.rung: r.coverage for r in act.to_contract()}
    assert rows == pytest.approx(
        {"cudem_nearshore": 8.0 / 9.0, "regional_fine": 1.0 / 9.0}
    )
    assert act.ungated == []
    assert not act.degraded
    fine = next(r for r in act.records if r.rung == "regional_fine")
    assert fine.consequence == "enhancement"
    assert "FINER" in (fine.note or "")


def test_shares_that_do_not_sum_to_one_are_said_out_loud(caplog) -> None:
    class _Result:
        rung_coverage = {"primary": 0.2}

    with caplog.at_level("WARNING", logger="trid3nt_server.fallbacks.walker"):
        walk_ladder(_ladder(), params={}, attempt=lambda _r, _p: _Result(),
                    gate=lambda **_k: True)
    assert "sum to 0.2000" in caplog.text
    assert "painted by a source outside the ladder or by nothing at all" in caplog.text


# --------------------------------------------------------------------------- #
# The enhancement layer degrades LOUDLY.
# --------------------------------------------------------------------------- #


def test_the_geoclaw_fine_nearshore_layer_never_vanishes_silently(
    monkeypatch, tmp_path, fake_s3, caplog
) -> None:
    """The nested ~10 m shore topo is an enhancement, so its loss degrades the run
    rather than stopping it -- but it returns the note that says WHY, and the note
    rides the answer layer's fallback_note."""
    from trid3nt_server.workflows.geoclaw.inundation import inundation as gi

    _patch_partial_cudem(monkeypatch, tmp_path)
    _fault_the_rungs_fetch(monkeypatch)
    with caplog.at_level("WARNING"):
        uri, note = gi._fetch_fine_nearshore_for_geoclaw(_EXHIBIT_BBOX)
    assert uri is None
    assert note and "LABELED DEGRADE (fine nearshore topo)" in note
    assert "COARSE primary topo" in note
    assert "LABELED DEGRADE (fine nearshore topo)" in caplog.text


def test_the_fine_nearshore_note_names_an_uncovered_aoi_too(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """The other silent path: the fetch SUCCEEDS but no fine source covers the AOI.
    Returning None with no note left the model unable to say the run-up was coarse."""
    from trid3nt_server.workflows.geoclaw.inundation import inundation as gi

    _patch_total_cudem_loss(monkeypatch, tmp_path)
    uri, note = gi._fetch_fine_nearshore_for_geoclaw(_EXHIBIT_BBOX)
    assert uri is None
    assert note and "no genuinely-fine nearshore source" in note


# --------------------------------------------------------------------------- #
# Decline semantics: a LATER gap may not retro-justify an EARLIER decline.
# --------------------------------------------------------------------------- #


def test_a_later_gap_never_retro_justifies_an_earlier_decline() -> None:
    """Two alternatives. alt1 is declined while the primary's failure is a plain
    retryable upstream error (no gap outstanding); alt2 then reports a gap. The
    refusal must still be the PRIMARY's typed error, retryability intact -- not a
    coverage refusal blamed on the decline."""
    class _Upstream(Exception):
        error_code = "CAP_UPSTREAM"
        retryable = True

    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise _Upstream("CUDEM 503")
        if rung.name == "alt1":
            raise AssertionError("a declined rung must never be invoked")
        raise LadderGap("part", covered_fraction=0.4, gap_note="alt2 covers 40%")

    ladder = _ladder(_rung("alt1", "cross_dataset"), _rung("alt2", "same_data"))
    with pytest.raises(_Upstream) as ei:
        walk_ladder(ladder, params={}, attempt=_attempt, allow=("alt1", "alt2"),
                    gate=lambda **kw: kw["rung"].name != "alt1")
    assert ei.value.error_code == "CAP_UPSTREAM"
    assert ei.value.retryable is True
    rows = {r.rung: r for r in ei.value.fallback_activation.records}
    assert rows["alt1"].declined is True   # the decline still leaves its trace


def test_a_decline_in_front_of_an_outstanding_gap_still_owns_the_refusal() -> None:
    """The control: the gap came FIRST, so the decline really is why nothing
    filled it, and the refusal wears the capability's coverage code."""
    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise LadderGap("gap", covered_fraction=0.5, gap_note="half the AOI")
        raise AssertionError("a declined rung must never be invoked")

    with pytest.raises(LadderRefused) as ei:
        walk_ladder(_ladder(_rung("alt", "cross_dataset")), params={},
                    attempt=_attempt, allow=("alt",), gate=lambda **_k: False)
    assert ei.value.error_code == "TEST_REFUSED"
    assert "declined at the fallback gate" in str(ei.value)
    assert "half the AOI" in str(ei.value)


def test_a_transport_fault_after_a_decline_still_beats_the_decline_verdict(
) -> None:
    """F1e/R1: primary gaps -> alt1 is DECLINED while that gap is outstanding ->
    alt2 is PERMITTED but transport-faults. The decline fired first in plan
    order, but it is NOT why the gap went unfilled -- alt2's fault is. The
    refusal must wear FALLBACK_LADDER_ERROR with alt2's own retryability, never
    the coverage code with the decline's non-retryable verdict."""
    class _Transient(Exception):
        retryable = True

    def _attempt(rung: Any, _p: Any) -> Any:
        if rung.name == "primary":
            raise LadderGap("gap", covered_fraction=0.5, gap_note="primary 50%")
        if rung.name == "alt1":
            raise AssertionError("a declined rung must never be invoked")
        raise _Transient("EndpointConnectionError: MinIO unreachable")

    with pytest.raises(LadderRefused) as ei:
        walk_ladder(_ladder(_rung("alt1", "cross_dataset"), _rung("alt2", "same_data")),
                    params={}, attempt=_attempt, allow=("alt1", "alt2"),
                    gate=lambda **kw: kw["rung"].name != "alt1")
    assert ei.value.error_code == LADDER_ERROR_CODE != "TEST_REFUSED"
    assert ei.value.retryable is True
    assert "MinIO unreachable" in str(ei.value)
    rows = {r.rung: r for r in ei.value.activation.records}
    assert rows["alt1"].declined is True  # the decline still leaves its trace


# --------------------------------------------------------------------------- #
# An UNMEASURED serve carries no numbers anywhere on the envelope.
# --------------------------------------------------------------------------- #


def test_an_exempted_envelope_carries_no_numeric_shares(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """The note says the per-rung share is UNMEASURED; a rung_coverage map beside
    it would make the envelope contradict itself."""
    _patch_partial_cudem(monkeypatch, tmp_path)
    res = TOOL_REGISTRY["fetch_topobathy"].fn(
        bbox=list(_EXHIBIT_BBOX), force_bathy_base=True
    )
    assert list(res.fallbacks or []) == []
    assert "UNMEASURED" in (res.fallback_note or "")
    assert res.rung_coverage is None


def test_a_rung_the_gate_never_saw_says_so_on_the_row(
    monkeypatch, tmp_path, fake_s3
) -> None:
    """The capability auto-engages its ETOPO base when CUDEM does not intersect at
    all -- no gap, no walk, no gate. The row is stamped because the data served,
    and it says the gate never saw it rather than implying approval."""
    etopo = str(tmp_path / "etopo.tif")
    land = str(tmp_path / "land.tif")
    _synth_raster(etopo, _EXHIBIT_BBOX, -30.0)
    _synth_raster(land, _EXHIBIT_BBOX, 12.0)
    monkeypatch.setattr(tb, "_select_cudem_tiles", lambda *_a, **_k: [])
    monkeypatch.setattr(tb, "_select_etopo_tiles", lambda *_a, **_k: [etopo])
    monkeypatch.setattr(tb, "_fetch_3dep_land_to_file", lambda *_a, **_k: land)
    real = tb._composite_sources_to_array
    monkeypatch.setattr(
        tb, "_composite_sources_to_array",
        lambda s, c, b, **kw: real(
            [(x[len("/vsicurl/"):] if x.startswith("/vsicurl/") else x) for x in s],
            c, b, **kw,
        ),
    )
    # NO fallback= is declared: the caller permitted nothing.
    res = TOOL_REGISTRY["fetch_topobathy"].fn(bbox=list(_EXHIBIT_BBOX))
    rows = {r.rung: r for r in (res.fallbacks or [])}
    assert rows["etopo_bathy_base"].coverage == pytest.approx(1.0)
    assert "the fallback gate never saw this rung" in (rows["etopo_bathy_base"].note or "")
    assert "GATE-UNSEEN" in (res.fallback_note or "")


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


def test_a_user_supplied_rung_still_surfaces_its_input_layer(
    monkeypatch, tmp_path
) -> None:
    """A rung with its own ``call`` never reaches _route_once, so nothing records
    the emit-on-fetch arguments -- the user's own bed would be the one input that
    never appears on the map."""
    from trid3nt_server.tools.fetchers._router import emit_on_fetch

    surfaced: list = []
    monkeypatch.setattr(
        emit_on_fetch, "maybe_emit_input_on_fetch",
        lambda spec, params, layer, **kw: surfaced.append((layer.uri, kw)),
    )
    _patch_partial_cudem(monkeypatch, tmp_path)
    TOOL_REGISTRY["fetch_topobathy"].fn(
        bbox=list(_EXHIBIT_BBOX), dem_uri="s3://mine/survey.tif",
        purpose="survey bed",
    )
    assert surfaced == [("s3://mine/survey.tif", {"visualize": None,
                                                  "purpose": "survey bed"})]


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


def test_sfincs_says_a_retryable_ladder_fault_is_retryable() -> None:
    """``_build_failed_envelope`` threads an error_code but has NO retryable field,
    and this composer's contract is an envelope rather than a raise. So the code
    separates the two verdicts and the detail says the retryability out loud --
    otherwise a MinIO hiccup reads to the model as a terminal modeling failure."""
    import inspect

    from trid3nt_server.workflows.sfincs.flood import flood as fl
    from trid3nt_server.workflows.sfincs.run_sfincs import _build_failed_envelope

    assert "retryable" not in inspect.signature(_build_failed_envelope).parameters
    handler = inspect.getsource(fl.model_flood_scenario).split(
        "except (TopobathyError, LadderRefused) as exc:"
    )[1].split("except Exception")[0]
    assert "LADDER_ERROR_CODE" in handler
    assert "RETRY the same request" in handler
    assert "error_detail=f\"{exc}{ladder_detail}\"" in handler


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


# --------------------------------------------------------------------------- #
# F1e: the REGISTERED TOOL entrypoint's typed-exception tuple. A LadderRefused
# escaping the composer (the retryable FALLBACK_LADDER_ERROR truth) must not
# fall to the generic catch-all -- that reads it as GEOCLAW/SCHISM_INTERNAL_ERROR
# and loses both the code and the retryability. A genuine coverage gap (already
# wrapped into GeoClawComposerError/SchismScenarioError upstream) must keep
# landing on the SAME terminal code as before this fix.
# --------------------------------------------------------------------------- #


def _stub_activation() -> Any:
    return type("A", (), {"records": [], "to_contract": lambda s: [],
                          "narration": lambda s: None, "capability": "c"})()


def test_geoclaw_tool_entrypoint_threads_a_retryable_ladder_fault(monkeypatch) -> None:
    import asyncio

    from trid3nt_server.workflows.geoclaw.inundation import inundation as gi

    fault = LadderRefused(
        f"{LADDER_ERROR_CODE}: primary 89%, and the rung permitted to fill it "
        "failed for an unrelated reason -- EndpointConnectionError: MinIO "
        "unreachable.",
        error_code=LADDER_ERROR_CODE, activation=_stub_activation(), retryable=True,
    )

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise fault

    monkeypatch.setattr(gi, "model_geoclaw_inundation", _boom)
    out = asyncio.run(TOOL_REGISTRY["geoclaw_inundation"].fn(
        bbox=list(_EXHIBIT_BBOX), scenario="tsunami", sim_duration_s=60.0))
    assert out["status"] == "error"
    assert out["error_code"] == LADDER_ERROR_CODE != "GEOCLAW_INTERNAL_ERROR"
    assert "MinIO unreachable" in out["error_message"]
    assert "RETRY the same request" in out["error_message"]


def test_geoclaw_tool_entrypoint_keeps_the_terminal_coverage_code_unchanged(
    monkeypatch,
) -> None:
    """A genuine gap is already wrapped into GeoClawComposerError upstream --
    this fix must not touch that path."""
    import asyncio

    from trid3nt_server.workflows.geoclaw.inundation import inundation as gi

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise gi.GeoClawComposerError(
            "GEOCLAW_NO_BATHYMETRY", "the topo-bathymetry ladder refused: gap")

    monkeypatch.setattr(gi, "model_geoclaw_inundation", _boom)
    out = asyncio.run(TOOL_REGISTRY["geoclaw_inundation"].fn(
        bbox=list(_EXHIBIT_BBOX), scenario="tsunami", sim_duration_s=60.0))
    assert out == {
        "status": "error", "error_code": "GEOCLAW_NO_BATHYMETRY",
        "error_message": "the topo-bathymetry ladder refused: gap",
    }


def test_schism_tool_entrypoint_threads_a_retryable_ladder_fault(monkeypatch) -> None:
    import asyncio

    from trid3nt_server.workflows.schism.tidal_hydro import tidal_hydro as sc

    fault = LadderRefused(
        f"{LADDER_ERROR_CODE}: primary 89%, and the rung permitted to fill it "
        "failed for an unrelated reason -- EndpointConnectionError: MinIO "
        "unreachable.",
        error_code=LADDER_ERROR_CODE, activation=_stub_activation(), retryable=True,
    )

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise fault

    monkeypatch.setattr(sc, "model_schism_tidal_hydro", _boom)
    out = asyncio.run(sc.schism_tidal_hydro(
        mesh_source="coastal_tin", bbox=list(_EXHIBIT_BBOX)))
    assert out["status"] == "error"
    assert out["error_code"] == LADDER_ERROR_CODE != "SCHISM_INTERNAL_ERROR"
    assert "MinIO unreachable" in out["error_message"]
    assert "RETRY the same request" in out["error_message"]


def test_schism_tool_entrypoint_keeps_the_terminal_coverage_code_unchanged(
    monkeypatch,
) -> None:
    """A genuine gap is already wrapped into SchismScenarioError upstream --
    this fix must not touch that path."""
    import asyncio

    from trid3nt_server.workflows.schism.tidal_hydro import tidal_hydro as sc

    async def _boom(*_a: Any, **_k: Any) -> Any:
        raise sc.SchismScenarioError(
            "SCHISM_BATHYMETRY_UNAVAILABLE", "no real bathymetry")

    monkeypatch.setattr(sc, "model_schism_tidal_hydro", _boom)
    out = asyncio.run(sc.schism_tidal_hydro(
        mesh_source="coastal_tin", bbox=list(_EXHIBIT_BBOX)))
    assert out == {
        "status": "error", "error_code": "SCHISM_BATHYMETRY_UNAVAILABLE",
        "error_message": "no real bathymetry",
    }


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
    assert "11% etopo_bathy_base [cross_dataset]" in (out.fallback_note or "")
    # Idempotent through the shared seam: a re-stamp duplicates nothing.
    again = wf._stamp_swan_provenance(out, SwanRunArgs(bbox=_SMOKE_BBOX), rows)
    assert [r.rung for r in again.fallbacks] == ["cudem_nearshore", "etopo_bathy_base"]
    assert again.fallback_note == out.fallback_note
