"""Tests for ``tools.solver.solve_progress_vcpus`` (fingerprint audit A6).

The single deployment-aware seam the workflow live solve-progress call sites
use instead of reading ``AWS_BATCH_COMPUTE_CLASS_SIZING`` directly:

- **local-docker** lane -> ``os.cpu_count()`` (the host CPUs actually doing
  the solve; the web renders the local deployment with "CPU" wording). Never
  the AWS Batch tier count, whatever compute_class / cloud_vcpus says.
- **aws-batch** lane (set OR unset env -- the default) -> byte-identical to
  the callers' prior logic: ``cloud_vcpus`` passthrough when the caller
  already resolved a count (SFINCS autoscale provenance), else the sizing
  table lookup, ``None`` for an unknown/missing class.

Wording/telemetry only: a companion test pins that the DISPATCH sizing table
itself is untouched.
"""

from __future__ import annotations

import os

import pytest

from grace2_agent.tools.solver import (
    AWS_BATCH_COMPUTE_CLASS_SIZING,
    solve_progress_vcpus,
)


# --------------------------------------------------------------------------- #
# Cloud lane (aws-batch, set or unset) -- byte-identical to the prior logic
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("backend_env", [None, "aws-batch"])
@pytest.mark.parametrize(
    ("compute_class", "expected"),
    [
        ("small", 4),
        ("standard", 8),
        ("large", 16),
        ("xlarge", 48),
        ("gpu", 32),
        ("no-such-tier", None),  # unknown class -> None (old .get().get())
        (None, None),  # no class -> None
    ],
)
def test_cloud_lane_tier_lookup(monkeypatch, backend_env, compute_class, expected):
    if backend_env is None:
        monkeypatch.delenv("GRACE2_SOLVER_BACKEND", raising=False)
    else:
        monkeypatch.setenv("GRACE2_SOLVER_BACKEND", backend_env)
    assert solve_progress_vcpus(compute_class) == expected


@pytest.mark.parametrize("backend_env", [None, "aws-batch"])
def test_cloud_lane_cloud_vcpus_passthrough(monkeypatch, backend_env):
    """The SFINCS autoscale-provenance path: caller-resolved count wins."""
    if backend_env is None:
        monkeypatch.delenv("GRACE2_SOLVER_BACKEND", raising=False)
    else:
        monkeypatch.setenv("GRACE2_SOLVER_BACKEND", backend_env)
    assert solve_progress_vcpus(cloud_vcpus=8) == 8
    # Passthrough beats the tier lookup when both are supplied.
    assert solve_progress_vcpus("large", cloud_vcpus=8) == 8
    # No caller count -> falls back to the tier lookup / None.
    assert solve_progress_vcpus(cloud_vcpus=None) is None


# --------------------------------------------------------------------------- #
# Local lane (local-docker) -- host CPU count, never the AWS tier
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "compute_class", [None, "small", "standard", "large", "xlarge", "no-such-tier"]
)
def test_local_lane_reports_host_cpu_count(monkeypatch, compute_class):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    assert solve_progress_vcpus(compute_class) == os.cpu_count()


def test_local_lane_ignores_cloud_vcpus(monkeypatch):
    """The autoscale provenance carries the perf model's CLOUD vCPU anchor --
    the local card must never show it."""
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    assert solve_progress_vcpus(cloud_vcpus=8) == os.cpu_count()
    assert solve_progress_vcpus("xlarge", cloud_vcpus=48) == os.cpu_count()


def test_local_lane_none_when_cpu_count_indeterminate(monkeypatch):
    """os.cpu_count() can return None -- the card then omits the segment."""
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert solve_progress_vcpus("standard") is None


# --------------------------------------------------------------------------- #
# Dispatch invariant -- the helper is wording-only
# --------------------------------------------------------------------------- #


def test_dispatch_sizing_table_untouched(monkeypatch):
    """The Batch resourceRequirements table is never mutated by the seam."""
    before = {k: dict(v) for k, v in AWS_BATCH_COMPUTE_CLASS_SIZING.items()}
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    solve_progress_vcpus("standard")
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    solve_progress_vcpus("standard", cloud_vcpus=8)
    assert AWS_BATCH_COMPUTE_CLASS_SIZING == before
