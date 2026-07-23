"""Tests for ``tools.simulation.solver.solve_progress_vcpus`` (fingerprint audit A6).

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

from trid3nt_server.tools.simulation.solver import (
    AWS_BATCH_COMPUTE_CLASS_SIZING,
    solve_progress_vcpus,
)


# --------------------------------------------------------------------------- #
# Cloud lane (aws-batch) -- REMOVED (local-only slim, 2026-07 pass).
#
# ``solver_backend()`` now unconditionally returns ``SOLVER_BACKEND_LOCAL_DOCKER``
# (the AWS Batch arm was pulled from this build; see its docstring). That makes
# ``TRID3NT_SOLVER_BACKEND=aws-batch`` inert: ``solve_progress_vcpus`` never
# reaches the tier-lookup / cloud_vcpus-passthrough branches below the
# local-docker short-circuit, so a "cloud lane" test pinning tier vcpus or
# passthrough values byte-for-byte duplicates the local-lane assertions
# further down this file while asserting a code path this build cannot take.
# The former ``test_cloud_lane_tier_lookup`` and
# ``test_cloud_lane_cloud_vcpus_passthrough`` (which asserted AWS_BATCH tier
# vcpus / cloud_vcpus passthrough regardless of the "aws-batch" env value)
# are deleted rather than re-pinned: ``test_local_lane_reports_host_cpu_count``
# and ``test_local_lane_ignores_cloud_vcpus`` already cover "env value does
# not change the answer, host CPU count always wins" for the live behavior.
# If the Batch arm is ever re-woven from git history, restore these from VCS
# alongside it.
# --------------------------------------------------------------------------- #
# Local lane (local-docker) -- host CPU count, never the AWS tier
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "compute_class", [None, "small", "standard", "large", "xlarge", "no-such-tier"]
)
def test_local_lane_reports_host_cpu_count(monkeypatch, compute_class):
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
    assert solve_progress_vcpus(compute_class) == os.cpu_count()


def test_local_lane_ignores_cloud_vcpus(monkeypatch):
    """The autoscale provenance carries the perf model's CLOUD vCPU anchor --
    the local card must never show it."""
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
    assert solve_progress_vcpus(cloud_vcpus=8) == os.cpu_count()
    assert solve_progress_vcpus("xlarge", cloud_vcpus=48) == os.cpu_count()


def test_local_lane_none_when_cpu_count_indeterminate(monkeypatch):
    """os.cpu_count() can return None -- the card then omits the segment."""
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    assert solve_progress_vcpus("standard") is None


# --------------------------------------------------------------------------- #
# Dispatch invariant -- the helper is wording-only
# --------------------------------------------------------------------------- #


def test_dispatch_sizing_table_untouched(monkeypatch):
    """The Batch resourceRequirements table is never mutated by the seam."""
    before = {k: dict(v) for k, v in AWS_BATCH_COMPUTE_CLASS_SIZING.items()}
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
    solve_progress_vcpus("standard")
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "aws-batch")
    solve_progress_vcpus("standard", cloud_vcpus=8)
    assert AWS_BATCH_COMPUTE_CLASS_SIZING == before
