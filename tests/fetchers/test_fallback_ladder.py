"""Fallback-ladder contract: the rung schema, the ONE walker, the loudness gate.

All offline, over ladders declared HERE. The contract is what the registry, the
walker and the gate do with a rung, which is a statement about them and not about
whichever capability happens to declare one.
"""

from __future__ import annotations

from typing import Any

import pytest

from trid3nt_contracts.common import render_fallback_line
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



def _rung(name: str, consequence: str, **kw: Any) -> Rung:
    return Rung(name=name, consequence=consequence, describes=f"{name} describes", **kw)


def _ladder(*alternatives: Rung, user: Rung | None = None) -> Ladder:
    rungs = ((user,) if user else ()) + (_rung("primary", "primary"),) + alternatives
    return Ladder(capability="test_cap", rungs=rungs, refuse_error_code="TEST_REFUSED")




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
    """AUTO means nobody is being asked, emitter or no emitter.

    Keying the ask on the presence of a channel stalled an auto run for the whole
    gate TTL; the labeled default applies immediately."""
    import asyncio as _asyncio

    from trid3nt_server.render import pipeline_emitter as pe

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
        warning_id=new_ulid(), tool_name="fetch_cudem",
        tool_args={}, estimated_mb=0.0, threshold_mb=0.0,
        recommendation="approve?", options=["proceed", "cancel"], ttl_seconds=1,
    )
    emitter = _Emitter()
    assert asyncio.run(gate_mod._present_and_wait(emitter, envelope)) is False
    assert emitter.sent and emitter.sent[0][0] == "tool-payload-warning"




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


class _Transient(Exception):
    """A transport hiccup (MinIO/S3), the shape that must stay retryable."""

    retryable = True


def test_shares_that_do_not_sum_to_one_are_said_out_loud(caplog) -> None:
    class _Result:
        rung_coverage = {"primary": 0.2}

    with caplog.at_level("WARNING", logger="trid3nt_server.fallbacks.walker"):
        walk_ladder(_ladder(), params={}, attempt=lambda _r, _p: _Result(),
                    gate=lambda **_k: True)
    assert "sum to 0.2000" in caplog.text
    assert "painted by a source outside the ladder or by nothing at all" in caplog.text




def test_a_later_gap_never_retro_justifies_an_earlier_decline() -> None:
    """A later gap never retro-justifies an earlier decline.

    An alternative declined while the primary's failure is a plain retryable upstream
    error leaves that primary error as the refusal, retryability intact."""
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
    """A transport fault after a decline still beats the decline verdict.

    The decline fired first in plan order but is not why the gap went unfilled, so
    the refusal wears the faulting rung's error and its retryability."""
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




def test_the_walker_still_stamps_a_rung_a_capability_lays_down_itself() -> None:
    """The detector stays: a capability that paints a degradation rung the walk
    never descended to has that row appended and marked, because the loudness gate
    never had the chance to ask about it."""
    from trid3nt_server.fallbacks.walker import Activation, RungRecord, _reconcile_to_paint

    ladder = _ladder(_rung("coarse", "cross_dataset"))
    activation = Activation(capability=ladder.capability)
    activation.records = [RungRecord("primary", "primary", 0.4, "the primary")]
    _reconcile_to_paint(ladder, activation, {"primary": 0.4, "coarse": 0.6})
    rows = {r.rung: r for r in activation.records}
    assert activation.ungated == ["coarse"]
    assert "the fallback gate never saw this rung" in (rows["coarse"].note or "")


def test_emit_seam_carries_activation_rows_onto_a_reemitted_layer() -> None:
    from trid3nt_contracts.common import FallbackActivation
    from trid3nt_contracts.execution import LayerURI
    from trid3nt_server.render.layer_uri_emit import emit_layer_uri

    layer = LayerURI(
        layer_id="input-bed-abc", name="Coastal bed", layer_type="raster",
        uri="s3://bucket/bed.tif", role="context",
    )
    rows = [
        FallbackActivation(capability="test_cap", rung="primary",
                           consequence="primary", coverage=0.889),
        FallbackActivation(capability="test_cap", rung="coarse_base",
                           consequence="cross_dataset", coverage=0.111),
    ]
    out = emit_layer_uri(layer, fallbacks=rows)
    assert out is not None
    assert [r.rung for r in out.fallbacks] == ["primary", "coarse_base"]
    assert "11% coarse_base [cross_dataset]" in (out.fallback_note or "")
    # Idempotent: a second pass neither duplicates rows nor the narration.
    again = emit_layer_uri(out, fallbacks=rows)
    assert len(again.fallbacks) == 2
    assert again.fallback_note == out.fallback_note

