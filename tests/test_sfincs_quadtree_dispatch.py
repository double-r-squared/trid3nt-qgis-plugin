"""ADR 0178: composer-side quadtree build+solve dispatch + coast-following knobs.

Offline unit coverage for the seam that makes ``sfincs_flood(quadtree=True)``
actually dispatch the worker build+solve (the ADR 0176 Finding A gap):

- ``compose_quadtree_build_spec`` produces a spec the strict worker parser
  (``validate_job_spec``) accepts, carrying the refinement knobs under
  ``options.quadtree`` and the design-surge forcing.
- the ``sfincs-quadtree`` solver is registered (workflow registry + local spec).
- the local spec's ``docker run`` line is the ``--network host`` build+solve
  form with the run-id rewritten to the launcher's.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in (REPO, REPO / "contracts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from trid3nt_server.workflows.sfincs.flood.quadtree_dispatch import (  # noqa: E402
    SFINCS_QUADTREE_SOLVER_NAME,
    compose_quadtree_build_spec,
    sfincs_quadtree_local_spec,
)

BBOX = (-85.5522, 29.6983, -85.3976, 29.8517)


def test_compose_build_spec_validates_and_carries_knobs():
    from workers._sfincs_build.spec import validate_job_spec

    spec = compose_quadtree_build_spec(
        run_id="SPEC01",
        dem_uri="s3://trid3nt-cache/x/dem.tif",
        bbox=BBOX,
        base_resolution_m=400.0,
        coast_refine_level=3,
        max_refine_level=3,
        coast_band_m=800.0,
        simulation_hours=12.0,
        output_interval_min=30.0,
        return_period_yr=100,
    )
    # The strict worker parser accepts it (no unknown top-level fields; quadtree
    # deck needs only dem_uri, not landcover).
    out = validate_job_spec(spec)
    qt = out["options"]["quadtree"]
    assert qt["base_resolution_m"] == 400.0
    assert qt["coast_refine_level"] == 3
    assert qt["max_refine_level"] == 3
    assert qt["coast_band_m"] == 800.0
    assert out["options"]["return_period_yr"] == 100
    # Design-surge boundary rides in via the deck's return-period fallback.
    assert out["forcing"] == {"forcing_type": "waterlevel"}
    assert out["inputs"]["dem_uri"] == "s3://trid3nt-cache/x/dem.tif"
    assert out.get("nlcd_vintage_year") is None


def test_compose_build_spec_omits_coast_band_when_none():
    spec = compose_quadtree_build_spec(
        run_id="SPEC02", dem_uri="s3://c/dem.tif", bbox=BBOX,
        base_resolution_m=200.0, coast_refine_level=2, max_refine_level=2,
        coast_band_m=None, simulation_hours=6.0, output_interval_min=None,
        return_period_yr=50,
    )
    assert "coast_band_m" not in spec["options"]["quadtree"]


def test_solver_registered():
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_SOLVER_SPEC_REGISTRY,
        SOLVER_WORKFLOW_REGISTRY,
    )

    assert SOLVER_WORKFLOW_REGISTRY.get(SFINCS_QUADTREE_SOLVER_NAME) == (
        "model_flood_scenario"
    )
    assert SFINCS_QUADTREE_SOLVER_NAME in LOCAL_SOLVER_SPEC_REGISTRY
    # The two SFINCS paths are DISTINCT registry entries. The regular one used to
    # be absent because the dispatcher special-cased it by name; it registers
    # itself now (ADR 0317), so what this asserts is that the quadtree spec did
    # not take its slot.
    assert LOCAL_SOLVER_SPEC_REGISTRY["sfincs"] is not (
        LOCAL_SOLVER_SPEC_REGISTRY[SFINCS_QUADTREE_SOLVER_NAME])


def test_build_argv_is_network_host_build_solve():
    spec = sfincs_quadtree_local_spec()
    assert spec.solver == SFINCS_QUADTREE_SOLVER_NAME
    assert spec.args_key == "sfincs_quadtree_args"
    argv = spec.build_argv(
        "RUNID", Path("/tmp/x"),
        ["--run-id", "STAGING", "--build-spec-uri", "s3://c/spec.json"],
    )
    line = " ".join(argv)
    assert "docker run" in line
    assert "--network host" in line
    assert "--build-spec-uri s3://c/spec.json" in line
    # run-id rewritten to the launcher's (STAGING -> RUNID) so outputs land under
    # the prefix wait_for_completion polls.
    assert "--run-id RUNID" in line
    assert "STAGING" not in line
    assert argv[argv.index("--name") + 1] == "RUNID"
