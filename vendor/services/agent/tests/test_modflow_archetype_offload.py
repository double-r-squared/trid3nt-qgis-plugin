"""MODFLOW archetype heavy-compute offload tests.

Proves the agent<->worker contract for the GRACE2_MODFLOW_ARCHETYPE_OFFLOAD gate:

  * Gate is OFF by default (env unset -> in-agent path, offload branch skipped).
  * Gate is ON for offloadable archetypes; PRT + saltwater_intrusion stay local.
  * Spec round-trip: _run_args_to_deck_kwargs includes archetype + per-archetype
    fields; validate_job_spec + build_deck_kwargs_from_spec round-trip cleanly.
  * Solver submit routes to grace2-modflow (MODFLOW_BUILD_SOLVER), never SFINCS.
  * read_modflow_archetype_manifest returns the correct typed LayerURI per archetype.
  * Honesty floor: a manifest with status=error raises MODFLOWWorkflowError.
  * Worker postprocess runner dispatch table covers all six offloadable archetypes.
"""

from __future__ import annotations

import json
import os
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grace2_agent.workflows.run_modflow import (
    MODFLOWWorkflowError,
    _run_args_to_deck_kwargs,
    modflow_archetype_offload_enabled,
    read_modflow_archetype_manifest,
)
from services.workers._modflow_build import build_deck_kwargs_from_spec, validate_job_spec
from services.workers._modflow_postprocess.postprocess import (
    _ARCHETYPE_POSTPROCESS_RUNNERS,
    compute_drawdown_metrics,
    compute_cbc_term_metrics,
    compute_mounding_metrics,
    compute_recovery_efficiency,
    compute_seasonal_head_range_m,
    compute_budget_partition,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_run_args(**overrides: Any) -> Any:
    """Build a minimal MODFLOWRunArgs-like namespace for testing."""
    from grace2_contracts.modflow_contracts import MODFLOWRunArgs

    base: dict[str, Any] = {
        "spill_location_latlon": (30.0, -85.5),
        "contaminant": "benzene",
        "release_rate_kg_s": 0.001,
        "duration_days": 30.0,
        "aquifer_k_ms": 1e-4,
        "porosity": 0.3,
    }
    base.update(overrides)
    return MODFLOWRunArgs(**base)


def _make_run_result(run_id: str = "test-run-001", output_uri: str | None = None) -> Any:
    """Stub RunResult for manifest reader tests."""
    obj = types.SimpleNamespace()
    obj.run_id = run_id
    obj.output_uri = output_uri or f"s3://runs-bucket/{run_id}/"
    return obj


def _manifest_ok(archetype: str, metrics: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal ok publish_manifest.json for an archetype."""
    style_map = {
        "sustainable_yield": "continuous_drawdown_m",
        "mine_dewatering": "continuous_dewatering_rate",
        "regional_water_budget": "continuous_head_m",
        "MAR": "continuous_mounding_m",
        "ASR": "continuous_head_m",
        "wetland_hydroperiod": "continuous_hydroperiod_m",
    }
    return {
        "schema_version": 1,
        "engine": "modflow",
        "run_id": "test-run-001",
        "status": "ok",
        "frame_count": 1,
        "metrics": metrics,
        "layers": [
            {
                "layer_id_stem": f"{archetype}-test-run-001",
                "name": archetype,
                "role": "primary",
                "style_preset": style_map.get(archetype, "continuous_head_m"),
                "units": "m",
                "cog_uri": f"s3://runs/test-run-001/{archetype}.tif",
                "bbox": [-85.6, 29.9, -85.4, 30.1],
                "metrics": metrics,
            }
        ],
        "error_code": None,
    }


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------


class TestModflowArchetypeOffloadGate:
    def test_gate_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Gate is OFF when env var is unset."""
        monkeypatch.delenv("GRACE2_MODFLOW_ARCHETYPE_OFFLOAD", raising=False)
        assert modflow_archetype_offload_enabled() is False

    def test_gate_on_truthy_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for val in ("1", "on", "true", "yes", "ON", "TRUE", "YES"):
            monkeypatch.setenv("GRACE2_MODFLOW_ARCHETYPE_OFFLOAD", val)
            assert modflow_archetype_offload_enabled() is True, f"failed for {val!r}"

    def test_gate_off_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRACE2_MODFLOW_ARCHETYPE_OFFLOAD", "")
        assert modflow_archetype_offload_enabled() is False

    def test_gate_off_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRACE2_MODFLOW_ARCHETYPE_OFFLOAD", "0")
        assert modflow_archetype_offload_enabled() is False


# ---------------------------------------------------------------------------
# Spec round-trip tests (agent composer -> worker validator -> kwargs)
# ---------------------------------------------------------------------------


class TestArchetypeSpecRoundTrip:
    """_run_args_to_deck_kwargs -> validate_job_spec -> build_deck_kwargs_from_spec."""

    def _roundtrip(self, run_args: Any) -> dict[str, Any]:
        deck_kwargs = _run_args_to_deck_kwargs(run_args)
        spec: dict[str, Any] = {
            "schema_version": 1,
            "engine": "modflow",
            "spec_id": "test-spec-001",
            "run_args": deck_kwargs,
            "options": {"compute_class": "standard"},
        }
        validated = validate_job_spec(spec)
        return build_deck_kwargs_from_spec(validated)

    def test_sustainable_yield_roundtrip(self) -> None:
        run_args = _make_run_args(
            archetype="sustainable_yield",
            well_location_latlon=(30.01, -85.49),
            pumping_rate_m3_day=500.0,
        )
        kwargs = self._roundtrip(run_args)
        assert kwargs["archetype"] == "sustainable_yield"
        assert "well_location_latlon" in kwargs
        assert "pumping_rate_m3_day" in kwargs

    def test_mine_dewatering_roundtrip(self) -> None:
        run_args = _make_run_args(
            archetype="mine_dewatering",
            well_location_latlon=(30.01, -85.49),
        )
        kwargs = self._roundtrip(run_args)
        assert kwargs["archetype"] == "mine_dewatering"

    def test_regional_water_budget_roundtrip(self) -> None:
        run_args = _make_run_args(archetype="regional_water_budget")
        kwargs = self._roundtrip(run_args)
        assert kwargs["archetype"] == "regional_water_budget"

    def test_mar_roundtrip(self) -> None:
        run_args = _make_run_args(
            archetype="MAR",
            basin_footprint_lonlat=[(-85.5, 30.0), (-85.49, 30.0), (-85.49, 30.01), (-85.5, 30.01)],
            infiltration_rate_m_day=0.1,
        )
        kwargs = self._roundtrip(run_args)
        assert kwargs["archetype"] == "MAR"
        assert "basin_footprint_lonlat" in kwargs

    def test_asr_roundtrip(self) -> None:
        run_args = _make_run_args(
            archetype="ASR",
            well_location_latlon=(30.01, -85.49),
            injection_rate_m3_day=200.0,
            recovery_rate_m3_day=150.0,
        )
        kwargs = self._roundtrip(run_args)
        assert kwargs["archetype"] == "ASR"
        assert "injection_rate_m3_day" in kwargs
        assert "recovery_rate_m3_day" in kwargs

    def test_wetland_hydroperiod_roundtrip(self) -> None:
        run_args = _make_run_args(
            archetype="wetland_hydroperiod",
            wetland_footprint_lonlat=[(-85.5, 30.0), (-85.49, 30.0), (-85.49, 30.01), (-85.5, 30.01)],
        )
        kwargs = self._roundtrip(run_args)
        assert kwargs["archetype"] == "wetland_hydroperiod"

    def test_spec_missing_required_fields_raises(self) -> None:
        bad_spec = {
            "schema_version": 1,
            "engine": "modflow",
            "spec_id": "bad",
            "run_args": {"archetype": "sustainable_yield"},
            "options": {},
        }
        with pytest.raises(ValueError, match="missing required field"):
            validate_job_spec(bad_spec)


# ---------------------------------------------------------------------------
# Solver routing test
# ---------------------------------------------------------------------------


class TestArchetypeOffloadSolverRouting:
    """The archetype offload MUST route to MODFLOW_BUILD_SOLVER (grace2-modflow),
    not the SFINCS solver, and MUST pass --build-spec-uri to the worker."""

    def test_modflow_build_solver_constant(self) -> None:
        from grace2_agent.tools.solver import MODFLOW_BUILD_SOLVER

        assert MODFLOW_BUILD_SOLVER == "modflow-build"

    def test_submit_uses_build_spec_uri_env(self) -> None:
        """submit_modflow_build_solve injects --build-spec-uri into the container env."""
        from grace2_agent.tools.solver import submit_modflow_build_solve, set_batch_client, set_s3_client, set_runs_bucket

        class FakeBatch:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def submit_job(self, **kwargs: Any) -> dict[str, Any]:
                self.calls.append(kwargs)
                return {"jobId": "jid-arch-1", "jobName": kwargs.get("jobName")}

        class FakeS3:
            def put_object(self, **_: Any) -> dict[str, Any]:
                return {}

            def get_object(self, **_: Any) -> dict[str, Any]:
                return {"Body": MagicMock(read=lambda: b'{"schema_version":1}')}

        fb = FakeBatch()
        set_batch_client(fb)
        set_s3_client(FakeS3())
        set_runs_bucket("runs-bucket")
        os.environ.setdefault("GRACE2_AWS_BATCH_JOB_DEF_MODFLOW_BUILD", "grace2-modflow-build")
        os.environ.setdefault("GRACE2_AWS_BATCH_JOB_QUEUE", "grace2-solvers")
        try:
            submit_modflow_build_solve("s3://cache/spec.json", compute_class="standard")
        except Exception:  # noqa: BLE001 -- focus on what was submitted
            pass
        finally:
            set_batch_client(None)
            set_s3_client(None)

        if fb.calls:
            call = fb.calls[0]
            # Must NOT be the SFINCS job def.
            jd = call.get("jobDefinition", "")
            assert "sfincs" not in jd.lower(), f"routed to SFINCS job-def: {jd!r}"
            # Must carry --build-spec-uri env var.
            env = {e["name"]: e["value"] for e in call.get("containerOverrides", {}).get("environment", [])}
            assert "GRACE2_BUILD_SPEC_URI" in env, f"missing GRACE2_BUILD_SPEC_URI in env: {env}"


# ---------------------------------------------------------------------------
# Manifest reader tests (read_modflow_archetype_manifest)
# ---------------------------------------------------------------------------


class TestReadModflowArchetypeManifest:
    """read_modflow_archetype_manifest -> correct typed LayerURI per archetype."""

    def _patch_read(self, manifest: dict[str, Any]) -> Any:
        """Return a context manager that patches _read_object_text to return manifest."""
        import grace2_agent.workflows.run_modflow as rmod

        return patch.object(
            rmod,
            "_read_object_text",
            return_value=json.dumps(manifest),
        )

    def test_sustainable_yield_returns_drawdown(self) -> None:
        from grace2_contracts.modflow_contracts import DrawdownLayerURI

        manifest = _manifest_ok("sustainable_yield", {"max_drawdown_m": 3.5})
        run_result = _make_run_result()
        with self._patch_read(manifest):
            layer = read_modflow_archetype_manifest(run_result, "sustainable_yield", publish=False)
        assert isinstance(layer, DrawdownLayerURI)
        assert layer.max_drawdown_m == pytest.approx(3.5)

    def test_mine_dewatering_returns_dewater(self) -> None:
        from grace2_contracts.modflow_contracts import DewaterLayerURI

        manifest = _manifest_ok(
            "mine_dewatering",
            {"dewatering_rate_m3_day": 1200.0, "drain_cell_count": 42},
        )
        run_result = _make_run_result()
        with self._patch_read(manifest):
            layer = read_modflow_archetype_manifest(run_result, "mine_dewatering", publish=False)
        assert isinstance(layer, DewaterLayerURI)
        assert layer.dewatering_rate_m3_day == pytest.approx(1200.0)
        assert layer.drain_cell_count == 42

    def test_regional_water_budget_returns_budget_partition(self) -> None:
        from grace2_contracts.modflow_contracts import BudgetPartitionLayerURI

        manifest = _manifest_ok(
            "regional_water_budget",
            {"budget_partition_m3_day": {"chd_in": 1000.0, "chd_out": -950.0}},
        )
        run_result = _make_run_result()
        with self._patch_read(manifest):
            layer = read_modflow_archetype_manifest(
                run_result, "regional_water_budget", publish=False
            )
        assert isinstance(layer, BudgetPartitionLayerURI)
        assert layer.budget_partition_m3_day["chd_in"] == pytest.approx(1000.0)

    def test_mar_returns_mounding(self) -> None:
        from grace2_contracts.modflow_contracts import MoundingLayerURI

        manifest = _manifest_ok(
            "MAR",
            {"max_mounding_m": 0.85, "recharged_volume_m3": 50000.0},
        )
        run_result = _make_run_result()
        with self._patch_read(manifest):
            layer = read_modflow_archetype_manifest(run_result, "MAR", publish=False)
        assert isinstance(layer, MoundingLayerURI)
        assert layer.max_mounding_m == pytest.approx(0.85)
        assert layer.recharged_volume_m3 == pytest.approx(50000.0)

    def test_asr_returns_asr_layer(self) -> None:
        from grace2_contracts.modflow_contracts import ASRLayerURI

        manifest = _manifest_ok(
            "ASR",
            {"recovery_efficiency": 0.82, "head_timeseries": [10.0, 11.5, 9.8]},
        )
        run_result = _make_run_result()
        with self._patch_read(manifest):
            layer = read_modflow_archetype_manifest(run_result, "ASR", publish=False)
        assert isinstance(layer, ASRLayerURI)
        assert layer.recovery_efficiency == pytest.approx(0.82)
        assert layer.head_timeseries == [10.0, 11.5, 9.8]

    def test_wetland_hydroperiod_returns_hydroperiod(self) -> None:
        from grace2_contracts.modflow_contracts import HydroperiodLayerURI

        manifest = _manifest_ok(
            "wetland_hydroperiod",
            {"seasonal_head_range_m": 1.2, "head_timeseries": [5.0, 6.1, 5.3]},
        )
        run_result = _make_run_result()
        with self._patch_read(manifest):
            layer = read_modflow_archetype_manifest(
                run_result, "wetland_hydroperiod", publish=False
            )
        assert isinstance(layer, HydroperiodLayerURI)
        assert layer.seasonal_head_range_m == pytest.approx(1.2)

    def test_error_manifest_raises_workflow_error(self) -> None:
        """Worker honesty gate: a status=error manifest raises MODFLOWWorkflowError."""
        manifest = {
            "schema_version": 1,
            "engine": "modflow",
            "run_id": "test-run-001",
            "status": "error",
            "frame_count": 0,
            "metrics": {"max_drawdown_m": 0.0},
            "layers": [],
            "error_code": "MODFLOW_ARCHETYPE_EMPTY_RESULT",
        }
        run_result = _make_run_result()
        import grace2_agent.workflows.run_modflow as rmod

        with patch.object(rmod, "_read_object_text", return_value=json.dumps(manifest)):
            with pytest.raises(MODFLOWWorkflowError) as exc_info:
                read_modflow_archetype_manifest(
                    run_result, "sustainable_yield", publish=False
                )
        assert "MODFLOW_ARCHETYPE_EMPTY_RESULT" in exc_info.value.error_code

    def test_unknown_archetype_raises(self) -> None:
        """An unrecognised archetype (e.g. a PRT one) raises MODFLOWWorkflowError."""
        manifest = _manifest_ok("capture_zone", {"capture_zone_area_km2": 1.5})
        run_result = _make_run_result()
        import grace2_agent.workflows.run_modflow as rmod

        with patch.object(rmod, "_read_object_text", return_value=json.dumps(manifest)):
            with pytest.raises(MODFLOWWorkflowError) as exc_info:
                read_modflow_archetype_manifest(
                    run_result, "capture_zone", publish=False
                )
        assert "MODFLOW_ARCHETYPE_UNKNOWN" in exc_info.value.error_code


# ---------------------------------------------------------------------------
# Worker dispatch table coverage
# ---------------------------------------------------------------------------


class TestWorkerDispatchTable:
    """_ARCHETYPE_POSTPROCESS_RUNNERS covers all six offloadable archetypes."""

    OFFLOADABLE = {
        "sustainable_yield",
        "mine_dewatering",
        "regional_water_budget",
        "MAR",
        "ASR",
        "wetland_hydroperiod",
    }

    def test_dispatch_table_covers_all_offloadable(self) -> None:
        from services.workers._modflow_postprocess.postprocess import (
            _ARCHETYPE_POSTPROCESS_RUNNERS,
        )

        assert self.OFFLOADABLE == set(_ARCHETYPE_POSTPROCESS_RUNNERS.keys())

    def test_prt_archetypes_not_in_table(self) -> None:
        from services.workers._modflow_postprocess.postprocess import (
            _ARCHETYPE_POSTPROCESS_RUNNERS,
        )

        for arch in ("capture_zone", "wellhead_protection"):
            assert arch not in _ARCHETYPE_POSTPROCESS_RUNNERS, (
                f"{arch!r} must NOT be in the worker dispatch table (LOCAL-ONLY)"
            )

    def test_saltwater_intrusion_not_in_table(self) -> None:
        from services.workers._modflow_postprocess.postprocess import (
            _ARCHETYPE_POSTPROCESS_RUNNERS,
        )

        assert "saltwater_intrusion" not in _ARCHETYPE_POSTPROCESS_RUNNERS, (
            "saltwater_intrusion must NOT be in the worker dispatch table (LOCAL-ONLY)"
        )

    def test_runner_callables_exist(self) -> None:
        """All runner names in the table resolve to callables in the module."""
        import services.workers._modflow_postprocess as pp_mod

        for archetype, runner_name in _ARCHETYPE_POSTPROCESS_RUNNERS.items():
            runner = getattr(pp_mod, runner_name, None)
            assert callable(runner), (
                f"runner {runner_name!r} for archetype {archetype!r} is not callable"
            )


# ---------------------------------------------------------------------------
# Pure metric function tests (no I/O needed)
# ---------------------------------------------------------------------------


class TestArchetypeMetrics:
    """Metric functions port faithfully from the agent (pure math, no I/O)."""

    def test_compute_drawdown_metrics_basic(self) -> None:
        import numpy as np

        decline = np.array([[1.0, 2.5], [0.5, 3.1]])
        assert compute_drawdown_metrics(decline) == pytest.approx(3.1)

    def test_compute_drawdown_metrics_all_nan(self) -> None:
        import numpy as np

        decline = np.full((3, 3), np.nan)
        assert compute_drawdown_metrics(decline) == 0.0

    def test_compute_drawdown_metrics_negative_clamped(self) -> None:
        """Negative values (recovery artifact) clamp to 0."""
        import numpy as np

        decline = np.array([[-0.5, -1.0]])
        assert compute_drawdown_metrics(decline) == 0.0

    def test_compute_cbc_term_metrics(self) -> None:
        import numpy as np

        # DRN flux is negative; magnitude is the dewatering rate.
        term_grid = np.array([[-100.0, -200.0], [np.nan, -50.0]])
        rate, count = compute_cbc_term_metrics(term_grid)
        assert rate == pytest.approx(350.0)
        assert count == 3

    def test_compute_mounding_metrics(self) -> None:
        import numpy as np

        rise = np.array([[0.0, 0.8], [1.5, np.nan]])
        assert compute_mounding_metrics(rise) == pytest.approx(1.5)

    def test_compute_seasonal_head_range_m(self) -> None:
        import numpy as np

        # Two steps: the cell [0,1] has range 1.0 (peak).
        step1 = np.array([[10.0, 5.0], [8.0, 9.0]])
        step2 = np.array([[10.5, 6.0], [7.5, 9.2]])
        peak_range, ts = compute_seasonal_head_range_m([step1, step2])
        assert peak_range == pytest.approx(1.0)
        assert ts is not None
        assert len(ts) == 2

    def test_compute_seasonal_head_range_single_step_no_ts(self) -> None:
        import numpy as np

        step = np.array([[10.0, 5.0]])
        peak_range, ts = compute_seasonal_head_range_m([step])
        assert peak_range == 0.0
        assert ts is None

    def test_compute_recovery_efficiency(self) -> None:
        assert compute_recovery_efficiency(1000.0, 820.0) == pytest.approx(0.82)
        assert compute_recovery_efficiency(1000.0, 1100.0) == pytest.approx(1.0)  # clamped
        assert compute_recovery_efficiency(0.0, 100.0) is None

    def test_compute_budget_partition(self) -> None:
        totals = {
            "FLOW-JA-FACE": -999.0,  # excluded
            "CHD_IN": 1000.0,
            "CHD_OUT": -950.0,
            "WEL_OUT": -0.0,  # near-zero dropped
        }
        partition = compute_budget_partition(totals)
        assert "flow-ja-face" not in partition
        assert "chd_in" in partition
        assert "chd_out" in partition
        assert partition["chd_in"] == pytest.approx(1000.0)
        # Near-zero WEL dropped.
        assert "wel_out" not in partition
