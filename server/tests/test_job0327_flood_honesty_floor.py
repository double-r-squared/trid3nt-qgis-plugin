"""job-0327 — SFINCS Toutle silent-failure fix: honesty floor + Atlas-2 fallback.

Two distinct mechanisms compounded into NATE's symptom ("flood returned in 3s,
no layer, agent said ok"):

  WHY-IT-FAILS (Toutle-specific): the Pacific Northwest is NOT in NOAA Atlas 14
  (Atlas-14 PFDS answers "Error 3.0: ... not within a project area"), and the
  MEMORY-noted "Atlas-14 -> Atlas-2 fallback" was doc-only. PART A adds the
  fallback so a small novel Western-US AOI like Toutle gets a design-storm depth.

  WHY-IT-LIES (AOI-agnostic): ``_build_failed_envelope`` returns a structurally
  valid "modeled" envelope with ``layers=[]`` + ``solver_run_ids=[]``, threading
  the error code only into the buried ``flood.metrics.solver_version`` string.
  ``summarize_tool_result`` stamped that dict ``status="ok"`` (no failed-envelope
  detector), so the LLM honestly narrated "done". PART B (the HONESTY FLOOR) adds
  a failed-modeled-envelope classifier at the single summarize chokepoint so a
  non-run can NEVER reach the LLM as ``status="ok"``.

The first test in each part is a regression-lock: it FAILS on the pre-fix code
(proving the bug) and PASSES after the fix.
"""

from __future__ import annotations

import pytest

from trid3nt_server.adapter import (
    _failed_modeled_envelope_error_code,
    summarize_tool_result,
)
from trid3nt_server.tools.data_fetch import (
    PrecipForcingUnavailableError,
    _ATLAS14_ARI_YEARS,
    _fetch_atlas2_precip_bytes,
    _parse_atlas14_csv,
)
from trid3nt_server.workflows.model_flood_scenario import _build_failed_envelope
from trid3nt_contracts import new_ulid


def _patch_read_through_passthrough(monkeypatch):
    """Bypass the object-store cache: invoke fetch_fn directly, no GCS/S3.

    ``lookup_precip_return_period`` routes its fetch through ``read_through``;
    in tests we don't want a real cache client, so replace it with a thin shim
    that just runs ``fetch_fn`` and wraps the bytes in a ReadThroughResult.
    Exceptions from ``fetch_fn`` propagate unchanged (the real shim re-raises).
    """
    import trid3nt_server.tools.cache as cache_mod
    import trid3nt_server.tools.data_fetch as df

    def _passthrough(*, metadata, params, ext, fetch_fn, **_kw):  # noqa: ANN001
        return cache_mod.ReadThroughResult(uri=None, data=fetch_fn(), hit=False)

    monkeypatch.setattr(df, "read_through", _passthrough)


# --------------------------------------------------------------------------- #
# PART B — HONESTY FLOOR (the load-bearing change; root-cause-agnostic)
# --------------------------------------------------------------------------- #


def _toutle_failed_envelope(error_code: str = "FETCHER_FAILED"):
    """A real ``_build_failed_envelope`` for the Toutle bbox, model_dump'd."""
    env = _build_failed_envelope(
        bbox=(-122.85, 46.20, -122.60, 46.45),
        project_id=new_ulid(),
        session_id=new_ulid(),
        error_code=error_code,
        error_detail="NOAA Atlas 14 PFDS returned no precip-frequency data",
        workflow_name="model_flood_scenario",
        data_sources=[],
        forcing=None,
        solver_run_ids=[],
        return_period_years=100,
        duration_hours=24.0,
        grid_resolution_m=30.0,
    )
    return env.model_dump(mode="json")


def test_failed_flood_envelope_summarizes_as_error_not_ok():
    """REGRESSION LOCK (the bug): a failed flood envelope MUST NOT be status=ok.

    On the pre-fix code summarize_tool_result returned ``status="ok"`` for this
    dict (no failed-envelope detector) — exactly NATE's silent "done". After
    job-0327 B1 it returns ``status="error"`` carrying the threaded code.
    """
    dumped = _toutle_failed_envelope("FETCHER_FAILED")
    summary = summarize_tool_result("run_model_flood_scenario", dumped)

    assert summary["status"] == "error", (
        "failed flood envelope (no layers, no solver run) must surface as "
        f"status=error, got {summary['status']!r} — the model did NOT run"
    )
    assert summary["error_code"] == "FETCHER_FAILED"
    assert summary["error_type"] == "FailedModelEnvelope"
    assert summary["retryable"] is False
    assert "did NOT run successfully" in summary["message"]


def test_failed_envelope_error_code_threaded_through_all_exit_codes():
    """Every _build_failed_envelope exit code survives to the summary."""
    for code in (
        "FETCHER_FAILED",
        "PRECIP_FORCING_UNAVAILABLE",
        "LULC_MAPPING_MISMATCH",
        "SOLVER_DISPATCH_FAILED",
        "SOLVER_FAILED",
        "POSTPROCESS_FAILED",
    ):
        dumped = _toutle_failed_envelope(code)
        summary = summarize_tool_result("run_model_flood_scenario", dumped)
        assert summary["status"] == "error"
        assert summary["error_code"] == code, (
            f"threaded code {code!r} lost; got {summary['error_code']!r}"
        )


def test_failed_envelope_promotes_code_to_workflow_name_depth0():
    """B2: the error code is promoted onto the depth-0 workflow_name string."""
    dumped = _toutle_failed_envelope("FETCHER_FAILED")
    assert dumped["workflow_name"] == "model_flood_scenario:FAILED:FETCHER_FAILED"
    # ...and the legacy solver_version threading is preserved.
    assert dumped["flood"]["metrics"]["solver_version"] == "failed:FETCHER_FAILED"


def test_error_code_extractor_prefers_workflow_name_then_solver_version():
    # depth-0 workflow_name seam wins.
    assert (
        _failed_modeled_envelope_error_code(
            {"workflow_name": "x:FAILED:CODE_A", "flood": {}}
        )
        == "CODE_A"
    )
    # fall back to the buried solver_version when workflow_name lacks the infix.
    assert (
        _failed_modeled_envelope_error_code(
            {
                "workflow_name": "model_flood_scenario",
                "flood": {"metrics": {"solver_version": "failed:CODE_B"}},
            }
        )
        == "CODE_B"
    )
    # neither seam present -> generic code.
    assert (
        _failed_modeled_envelope_error_code({"workflow_name": "x", "flood": {}})
        == "MODEL_RUN_PRODUCED_NO_LAYERS"
    )


def test_honesty_floor_root_cause_agnostic_minimal_dict():
    """The classifier keys off STRUCTURE, not on which root cause fired.

    A bare modeled dict with empty layers (a future composer that swallowed its
    own exception) still surfaces as error. job-0327 R2: an UNTAGGED modeled
    envelope with no layers is the publish/render-drop sub-case, so the code is
    ``NO_RENDERABLE_LAYER`` (regardless of solver_run_ids — the R1 "no
    solver_run_ids" gate was a hole).
    """
    summary = summarize_tool_result(
        "run_some_future_modeled_tool",
        {"envelope_type": "modeled", "layers": [], "solver_run_ids": []},
    )
    assert summary["status"] == "error"
    assert summary["error_code"] == "NO_RENDERABLE_LAYER"

    # Same shape but WITH an already-appended solver_run_id (the R1 hole): an
    # untagged modeled run that dispatched a solver but produced no layer is
    # STILL non-ok — the empty layers list is the trigger, not solver_run_ids.
    summary2 = summarize_tool_result(
        "run_some_future_modeled_tool",
        {"envelope_type": "modeled", "layers": [], "solver_run_ids": ["run_xyz"]},
    )
    assert summary2["status"] == "error"
    assert summary2["error_code"] == "NO_RENDERABLE_LAYER"


def test_honesty_floor_does_not_fire_on_successful_modeled_envelope():
    """A modeled envelope WITH a layer is a real success — must stay status=ok."""
    summary = summarize_tool_result(
        "run_model_flood_scenario",
        {
            "envelope_type": "modeled",
            "layers": [{"layer_id": "flood-depth-peak-x", "uri": "s3://b/x.tif"}],
            "solver_run_ids": ["run_abc"],
            "flood": {"metrics": {"max_depth_m": 1.2}},
        },
    )
    assert summary["status"] == "ok"


def test_honesty_floor_does_not_fire_on_non_modeled_tools():
    """Observed/fetched tools legitimately return non-layer data — stay status=ok."""
    # A fetched point query (e.g. precip lookup) — no envelope_type, no layers.
    summary = summarize_tool_result(
        "lookup_precip_return_period",
        {"precip_inches": 5.9, "units": "inches", "source": "noaa-atlas2"},
    )
    assert summary["status"] == "ok"
    # An explicitly non-modeled envelope with no layers (e.g. a fetched vector
    # summary) must NOT be misclassified.
    summary2 = summarize_tool_result(
        "fetch_administrative_boundaries",
        {"envelope_type": "fetched", "layers": [], "feature_count": 0},
    )
    assert summary2["status"] == "ok"


# --------------------------------------------------------------------------- #
# job-0327 ROUND 2 — dispatched-then-failed + publish-drop holes
# --------------------------------------------------------------------------- #


def test_dispatched_then_failed_tagged_with_run_id_surfaces_error():
    """MUST-FIX 1: SOLVER_FAILED/SOLVER_TIMEOUT/POSTPROCESS_FAILED append a
    solver_run_id BEFORE failing, so the R1 ``not solver_run_ids`` gate let
    them slip through as status=ok. A ":FAILED:"-tagged modeled envelope with
    a NON-empty solver_run_ids list and layers==[] MUST surface as error with
    the threaded code, regardless of the run id.
    """
    for code in ("SOLVER_FAILED", "SOLVER_TIMEOUT", "POSTPROCESS_FAILED"):
        dumped = {
            "envelope_type": "modeled",
            "workflow_name": f"model_flood_scenario:FAILED:{code}",
            "layers": [],
            "solver_run_ids": ["run_dispatched_before_failure"],
            "flood": {"metrics": {"solver_version": f"failed:{code}"}},
        }
        summary = summarize_tool_result("run_model_flood_scenario", dumped)
        assert summary["status"] == "error", (
            f"dispatched-then-failed {code} with a run_id must be error, got "
            f"{summary['status']!r}"
        )
        assert summary["error_code"] == code
        assert summary["error_type"] == "FailedModelEnvelope"
        assert "did NOT run successfully" in summary["message"]


def test_dispatched_then_failed_solver_version_only_with_run_id():
    """The legacy solver_version seam alone (no ":FAILED:" workflow_name infix)
    still classifies a run-id-bearing modeled envelope as a tagged failure."""
    dumped = {
        "envelope_type": "modeled",
        "workflow_name": "model_flood_scenario",  # NO :FAILED: infix
        "layers": [],
        "solver_run_ids": ["run_abc"],
        "flood": {"metrics": {"solver_version": "failed:SOLVER_FAILED"}},
    }
    summary = summarize_tool_result("run_model_flood_scenario", dumped)
    assert summary["status"] == "error"
    assert summary["error_code"] == "SOLVER_FAILED"
    assert summary["error_type"] == "FailedModelEnvelope"


def test_publish_drop_success_form_surfaces_no_renderable_layer_with_metrics():
    """MUST-FIX 2b: solve SUCCEEDED (metrics present, NO ":FAILED:" tag,
    non-empty solver_run_ids) but the layer was dropped at publish/render.
    MUST surface status=error, error_code=NO_RENDERABLE_LAYER, with the
    available flood metrics in the message so the agent narrates honestly.
    """
    dumped = {
        "envelope_type": "modeled",
        "workflow_name": "model_flood_scenario",  # untagged — solve completed
        "layers": [],
        "solver_run_ids": ["run_completed_ok"],
        "flood": {
            "metrics": {
                "flooded_area_km2": 12.5,
                "max_depth_m": 3.4,
                "mean_depth_m": 0.9,
                "p95_depth_m": 2.1,
                "solver_version": "sfincs-v2.3.3",  # a REAL solve, not failed:
            }
        },
    }
    summary = summarize_tool_result("run_model_flood_scenario", dumped)
    assert summary["status"] == "error"
    assert summary["error_code"] == "NO_RENDERABLE_LAYER"
    assert summary["error_type"] == "NoRenderableLayer"
    # The numbers must appear so the agent can narrate them honestly.
    msg = summary["message"]
    assert "12.5" in msg, f"flooded_area missing from message: {msg!r}"
    assert "3.4" in msg, f"max_depth missing from message: {msg!r}"
    assert "not on the map" in msg


def test_publish_drop_with_no_metrics_degrades_gracefully():
    """Sub-case (b) with absent metrics: still NO_RENDERABLE_LAYER, message
    degrades to a metric-free honest statement (no crash, no fabricated nums)."""
    dumped = {
        "envelope_type": "modeled",
        "workflow_name": "model_flood_scenario",
        "layers": [],
        "solver_run_ids": ["run_x"],
    }
    summary = summarize_tool_result("run_model_flood_scenario", dumped)
    assert summary["status"] == "error"
    assert summary["error_code"] == "NO_RENDERABLE_LAYER"
    assert "no renderable layer" in summary["message"].lower()


def test_modeled_with_nonempty_layers_stays_ok_success_path_unregressed():
    """MUST-FIX 2 guard: a modeled envelope WITH a non-empty layers list reads
    as status=ok — even with a non-empty solver_run_ids list (success path)."""
    summary = summarize_tool_result(
        "run_model_flood_scenario",
        {
            "envelope_type": "modeled",
            "workflow_name": "model_flood_scenario",
            "layers": [{"layer_id": "flood-depth-peak-x", "uri": "wms://qgis/x"}],
            "solver_run_ids": ["run_abc"],
            "flood": {"metrics": {"max_depth_m": 1.2, "solver_version": "sfincs-v2.3.3"}},
        },
    )
    assert summary["status"] == "ok"


def test_telemetry_flag_derives_failure_from_returned_error_summary():
    """MUST-FIX 3 (unit): the server's telemetry derivation — success from
    summary.status, error_code from summary.error_code when no exception was
    raised — records a returned-failure envelope as a FAILURE with its code.

    This mirrors the server.py logic (no raised dispatch_error, but the summary
    carries status=error) without standing up the full WS loop.
    """
    dumped = _toutle_failed_envelope("SOLVER_FAILED")
    summary = summarize_tool_result("run_model_flood_scenario", dumped)

    # Replicate the server.py derivation (server.py ~:1531-1547).
    dispatch_error = None
    _tel_error_code = None
    _tel_success = dispatch_error is None
    if dispatch_error is not None:  # pragma: no cover — exercised by server path
        _tel_error_code = "RAISED"
    elif isinstance(summary, dict) and summary.get("status") == "error":
        _tel_success = False
        _code = summary.get("error_code")
        _tel_error_code = str(_code) if _code is not None else None

    assert _tel_success is False, "returned-failure envelope must record FAILURE"
    assert _tel_error_code == "SOLVER_FAILED"

    # And the success path: a real modeled success records success=True, code=None.
    ok_summary = summarize_tool_result(
        "run_model_flood_scenario",
        {
            "envelope_type": "modeled",
            "layers": [{"layer_id": "x", "uri": "wms://q/x"}],
            "solver_run_ids": ["r"],
            "flood": {"metrics": {"max_depth_m": 1.0}},
        },
    )
    _ok_success = True
    _ok_code = None
    if isinstance(ok_summary, dict) and ok_summary.get("status") == "error":
        _ok_success = False
        _ok_code = ok_summary.get("error_code")
    assert _ok_success is True
    assert _ok_code is None


# --------------------------------------------------------------------------- #
# PART A — Atlas-2 (Western US) design-storm fallback (the WHY-IT-FAILS fix)
# --------------------------------------------------------------------------- #

# Toutle / Mount St. Helens, Washington — the exact AOI from the live evidence.
_TOUTLE_LAT = 46.325
_TOUTLE_LON = -122.733


def test_atlas2_fallback_produces_positive_depth_for_toutle():
    """REGRESSION LOCK (the Toutle die): Atlas-2 yields a real depth for the PNW.

    Pre-fix there was NO Atlas-2 code path; the Toutle precip lookup hard-died.
    """
    body = _fetch_atlas2_precip_bytes(_TOUTLE_LAT, _TOUTLE_LON, 100, 24.0)
    parsed = _parse_atlas14_csv(body.decode("utf-8"))
    depth = parsed["matrix"]["24-hr"][100]
    assert depth > 0.0
    # Maritime PNW 100-yr / 24-hr is a few inches — sanity bound, not exact.
    assert 3.0 < depth < 10.0


def test_atlas2_anchors_reproduce_mapped_values_exactly():
    """The directly-mapped Atlas-2 2-yr and 100-yr anchors are reproduced exactly."""
    parsed = _parse_atlas14_csv(
        _fetch_atlas2_precip_bytes(_TOUTLE_LAT, _TOUTLE_LON, 2, 24.0).decode("utf-8")
    )
    assert parsed["matrix"]["24-hr"][2] == pytest.approx(2.6, abs=0.01)
    parsed100 = _parse_atlas14_csv(
        _fetch_atlas2_precip_bytes(_TOUTLE_LAT, _TOUTLE_LON, 100, 24.0).decode("utf-8")
    )
    assert parsed100["matrix"]["24-hr"][100] == pytest.approx(5.9, abs=0.01)


def test_atlas2_full_ari_row_is_monotonic_and_complete():
    """The synthesized row has one depth PER ARI column and is non-decreasing."""
    parsed = _parse_atlas14_csv(
        _fetch_atlas2_precip_bytes(_TOUTLE_LAT, _TOUTLE_LON, 100, 24.0).decode("utf-8")
    )
    row = parsed["matrix"]["24-hr"]
    assert sorted(row.keys()) == sorted(_ATLAS14_ARI_YEARS)
    depths = [row[ari] for ari in _ATLAS14_ARI_YEARS]
    assert depths == sorted(depths), "depth must be non-decreasing in ARI"


def test_atlas2_interior_west_is_drier_than_maritime_pnw():
    """The Cascade-crest split keeps interior points on the drier curve."""
    pnw = _parse_atlas14_csv(
        _fetch_atlas2_precip_bytes(_TOUTLE_LAT, _TOUTLE_LON, 100, 24.0).decode("utf-8")
    )["matrix"]["24-hr"][100]
    interior = _parse_atlas14_csv(
        _fetch_atlas2_precip_bytes(44.0, -117.0, 100, 24.0).decode("utf-8")
    )["matrix"]["24-hr"][100]
    assert interior < pnw


def test_atlas2_out_of_coverage_raises_typed_unavailable():
    """A point outside the Western-US envelope (e.g. the Southeast) raises a typed
    PrecipForcingUnavailableError — an HONEST miss, never an empty success."""
    with pytest.raises(PrecipForcingUnavailableError):
        _fetch_atlas2_precip_bytes(34.0, -85.0, 100, 24.0)


def test_precip_forcing_unavailable_is_not_retryable_with_code():
    """The typed final-fallback error carries the actionable error_code."""
    err = PrecipForcingUnavailableError("no coverage")
    assert err.error_code == "PRECIP_FORCING_UNAVAILABLE"
    assert err.retryable is False


def test_lookup_precip_falls_back_to_atlas2_for_toutle(monkeypatch):
    """End-to-end fallback: Atlas-14 out-of-area -> Atlas-2 answers for Toutle.

    Patches the Atlas-14 fetch to raise the out-of-project-area UpstreamAPIError
    (the live Toutle behavior) and asserts lookup_precip_return_period returns a
    real depth tagged source="noaa-atlas2" instead of dying.
    """
    import trid3nt_server.tools.data_fetch as df

    def _fake_atlas14(lat, lon):  # noqa: ANN001 — test double
        raise df.UpstreamAPIError(
            f"NOAA Atlas 14 PFDS returned no precip-frequency data for "
            f"(lat={lat}, lon={lon}) — point may be outside the Atlas 14 "
            f"project areas."
        )

    monkeypatch.setattr(df, "_fetch_atlas14_pfds_bytes", _fake_atlas14)
    _patch_read_through_passthrough(monkeypatch)

    result = df.lookup_precip_return_period(
        location=(_TOUTLE_LAT, _TOUTLE_LON),
        return_period_years=100,
        duration_hours=24.0,
    )
    assert result["source"] == "noaa-atlas2"
    assert result["vintage_volume"] == "NOAA Atlas 2 (Western US)"
    assert result["precip_inches"] > 0.0


def test_lookup_precip_raises_unavailable_when_both_atlases_miss(monkeypatch):
    """Both atlases miss (out-of-coverage point) -> typed, NOT-retryable error
    with the actionable observed-precip remediation in the message."""
    import trid3nt_server.tools.data_fetch as df

    def _fake_atlas14(lat, lon):  # noqa: ANN001
        raise df.UpstreamAPIError("not within a project area")

    monkeypatch.setattr(df, "_fetch_atlas14_pfds_bytes", _fake_atlas14)
    _patch_read_through_passthrough(monkeypatch)

    # A point in the open Atlantic — outside BOTH Atlas 14 and Atlas 2.
    with pytest.raises(df.PrecipForcingUnavailableError) as ei:
        df.lookup_precip_return_period(
            location=(30.0, -50.0),
            return_period_years=100,
            duration_hours=24.0,
        )
    msg = str(ei.value)
    assert "REMEDIATION" in msg
    assert "forcing_raster_uri" in msg
    assert ei.value.error_code == "PRECIP_FORCING_UNAVAILABLE"


def test_lookup_precip_atlas14_path_unregressed(monkeypatch):
    """REGRESSION GUARD: an Atlas-14-covered point still uses Atlas 14 (the
    fallback try/except did not break the primary design-storm path)."""
    import trid3nt_server.tools.data_fetch as df

    # A minimal valid Atlas-14 CSV body for the 24-hr row across all 10 ARIs.
    depths = ",".join(str(round(2.0 + i * 0.5, 3)) for i in range(len(_ATLAS14_ARI_YEARS)))
    fake_body = (
        "NOAA Atlas 14 Volume 9 Version 2\n"
        "Project area: Southeastern States\n"
        f"24-hr:, {depths}\n"
    ).encode("utf-8")

    monkeypatch.setattr(df, "_fetch_atlas14_pfds_bytes", lambda lat, lon: fake_body)
    _patch_read_through_passthrough(monkeypatch)

    # Fort Myers, FL — Atlas-14 country.
    result = df.lookup_precip_return_period(
        location=(26.64, -81.87),
        return_period_years=100,
        duration_hours=24.0,
    )
    assert result["source"] == "noaa-atlas14-pfds"
    assert "Atlas 14" in result["vintage_volume"]
    assert result["precip_inches"] > 0.0
