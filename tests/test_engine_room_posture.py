"""The ENGINE-ROOM posture: staged inputs in, no network, and a code stamp on the run.

Three seams landed together because they are one idea (ADR 0317): a worker that is
handed everything it needs can be handed nothing else, and a run that records which
code produced it can be read honestly later. Each is tested where it can actually
fail - the flag on the launch line, the refusal when a bed is missing, the warning
when the tree has moved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trid3nt_server.workflows.solver.solver import (
    LOCAL_DOCKER_WORKFLOW_NAME,
    LocalSolverSpec,
    SolverDispatchError,
    _with_declared_network,
)


def _spec(**over):
    """A minimal docker spec whose argv is the family's volume-mount line."""
    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        return ["docker", "run", "--rm", "--name", run_id,
                "-v", f"{rundir}:/data", "-w", "/data", "img:latest", *args]

    fields = dict(
        solver="t", workflow_name=LOCAL_DOCKER_WORKFLOW_NAME, args_key="a",
        build_argv=build_argv, stdout_name="o", stderr_name="e",
        stdout_uri_field="ou", stderr_uri_field="eu", exec_kind="docker",
    )
    fields.update(over)
    return LocalSolverSpec(**fields)


# --------------------------------------------------------------------------- #
# --network none: declared per spec, applied by the launcher
# --------------------------------------------------------------------------- #


def test_declared_network_lands_immediately_after_docker_run():
    """The flag is a docker-run option, so it must precede every other argument.

    Placed after ``--rm`` or after the image it is either a different option or an
    argument to the container, and the container would silently keep its network.
    """
    spec = _spec(network="none")
    cmd = _with_declared_network(spec, spec.build_argv("RID", Path("/tmp/r"), []))
    assert cmd[:4] == ["docker", "run", "--network", "none"]
    assert cmd[-1] == "img:latest"


def test_no_declared_network_leaves_the_launch_line_untouched():
    """An engine that has not migrated its fetches keeps the default bridge."""
    spec = _spec()
    argv = spec.build_argv("RID", Path("/tmp/r"), [])
    assert _with_declared_network(spec, argv) == argv
    assert "--network" not in argv


def test_a_spec_that_writes_its_own_network_and_declares_one_is_refused():
    """Two ``--network`` flags on one line is a launch failure, so it fails HERE.

    The self-S3 build+solve specs write ``--network host`` in their own closure;
    declaring the field as well would produce a command docker rejects at run
    time, when the run is already minted and the failure reads as the solver's.
    """
    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        return ["docker", "run", "--network", "host", "img:latest"]

    spec = _spec(network="none", build_argv=build_argv)
    with pytest.raises(SolverDispatchError, match="declare it in one place"):
        _with_declared_network(spec, spec.build_argv("RID", Path("/tmp/r"), []))


def test_every_telemac_spec_declares_no_network():
    """The FAMILY DoD, asserted: the whole image runs with the network denied.

    The reach spec is the one that carried the exception, because the reach
    pipeline navigated NLDI, re-seeded off two flowline queries, queried NHDArea
    and walked its own DEM ladder from inside the container. All four are staged
    now, so the posture is no longer per-leg - it is the image's.

    Note the reach spec also serves the rain-on-grid catchment, so this one line
    is what puts BOTH remaining legs behind the denied network.
    """
    import trid3nt_server.workflows.telemac.run_telemac  # noqa: F401
    from trid3nt_server.workflows.solver.solver import LOCAL_SOLVER_SPEC_REGISTRY

    for name in ("artemis_agitation", "telemac3d_strat", "telemac_river_dye"):
        assert LOCAL_SOLVER_SPEC_REGISTRY[name]().network == "none", name


# --------------------------------------------------------------------------- #
# Staged bed: a real domain with no bed refuses rather than solving on nothing
# --------------------------------------------------------------------------- #


def test_a_real_domain_with_no_staged_bed_refuses():
    """The worker holds no fetcher any more, so a missing bed is a STAGING fault.

    Left unchecked it would surface as whatever the builder does with an absent
    file, several minutes later, wearing the solver's name.
    """
    from trid3nt_server.workflows.telemac.authoring.open_water import (
        OpenWaterError,
        staged_bed_inputs,
    )

    with pytest.raises(OpenWaterError, match="no bed raster was staged"):
        staged_bed_inputs(None, real=True, section="agitation")
    with pytest.raises(OpenWaterError):
        staged_bed_inputs({"uri": None}, real=True, section="stratified")


def test_an_idealized_domain_stages_nothing():
    """A Berkhoff shoal samples nothing, so it must not demand a raster."""
    from trid3nt_server.workflows.telemac.authoring.open_water import staged_bed_inputs

    assert staged_bed_inputs(None, real=False, section="agitation") == []


def test_a_staged_bed_becomes_one_manifest_input_row():
    from trid3nt_server.workflows.telemac.authoring.open_water import (
        STAGED_BED_DEST,
        staged_bed_inputs,
    )

    rows = staged_bed_inputs({"uri": "s3://c/bed.tif"}, real=True, section="agitation")
    assert rows == [{"gs_uri": "s3://c/bed.tif", "dest": STAGED_BED_DEST}]


# --------------------------------------------------------------------------- #
# Which domains solve on a fetched bed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode,expected", [("diffraction", True),
                                           ("resonance", False),
                                           ("shoal", False)])
def test_only_a_real_geography_mode_takes_the_fetched_bed(mode, expected):
    """A verification domain's bed is authored by the physics, whatever is asked."""
    from trid3nt_server.workflows.telemac.authoring.open_water import solves_on_real_bed

    assert solves_on_real_bed("noaa_greatlakes",
                              lon=-87.38, lat=46.54, mode=mode,
                              real_bed_modes=("diffraction",)) is expected


def test_an_auto_lake_bed_is_real_only_inside_the_covered_lakes():
    from trid3nt_server.workflows.telemac.authoring.open_water import solves_on_real_bed

    assert solves_on_real_bed("auto",
                              lon=-87.1, lat=46.95) is True      # Superior
    assert solves_on_real_bed("auto",
                              lon=-95.0, lat=39.0) is False      # Kansas


# --------------------------------------------------------------------------- #
# Code provenance: which code made this run, and has it moved
# --------------------------------------------------------------------------- #


def test_an_unrecorded_code_identity_reads_as_unknown_not_as_clean():
    """A run with no stamp must not read as "nothing changed"."""
    from trid3nt_server.workflows.solver.code_provenance import staleness

    warning = staleness(code_sha=None, engine="telemac")
    assert warning is not None
    assert warning["kind"] == "code_identity_unknown"


def test_an_engine_with_no_declared_paths_says_so():
    from trid3nt_server.workflows.solver.code_provenance import staleness

    warning = staleness(code_sha="0" * 40, engine="not_an_engine")
    assert warning is not None
    assert warning["kind"] == "engine_paths_unknown"


def test_a_solver_identifier_resolves_to_its_engine():
    """A run record carries the SOLVER name, which is not always the engine's."""
    from trid3nt_server.workflows.solver.code_provenance import (
        engine_paths,
        resolve_engine,
    )

    assert resolve_engine("artemis_agitation") == "telemac"
    assert resolve_engine("telemac3d_strat") == "telemac"
    assert engine_paths("artemis_agitation") == engine_paths("telemac")


def test_code_identity_stamps_a_sha_and_a_dirty_flag():
    """A sha alone claims a run came from a commit; an edit makes that false."""
    from trid3nt_server.workflows.solver.code_provenance import code_identity

    identity = code_identity()
    assert set(identity) == {"code_sha", "code_dirty"}
    if identity["code_sha"] is not None:
        assert len(identity["code_sha"]) == 40
        assert isinstance(identity["code_dirty"], bool)


def test_a_moved_engine_names_the_commits_that_moved_it():
    """The warning is the point: a reader gets a verdict, not a diff to run."""
    import subprocess

    from trid3nt_server.workflows.solver.code_provenance import _REPO_ROOT, staleness

    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "log", "--format=%H", "-40", "--",
         "workers/telemac/"],
        capture_output=True, text=True)
    shas = [s for s in out.stdout.split() if s]
    if len(shas) < 3:
        pytest.skip("not enough telemac history in this checkout")
    warning = staleness(code_sha=shas[2], engine="artemis_agitation")
    assert warning is not None
    assert warning["kind"] == "engine_code_moved"
    assert warning["engine"] == "telemac"
    assert warning["commit_count"] >= 2
    assert "STALE vs CODE" in warning["message"]


def test_an_engine_that_never_moved_is_not_reported_as_drift_unknown():
    """An EMPTY log is the clean answer, not a git failure.

    The two are opposite verdicts and the reader only ever sees one line, so a
    run whose engine has not been touched since it ran must read as unchanged
    however many unrelated commits have landed on top of it.
    """
    import subprocess

    from trid3nt_server.workflows.solver.code_provenance import _REPO_ROOT, staleness

    out = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True)
    head = out.stdout.strip()
    if not head:
        pytest.skip("not a git checkout")
    # Nothing has landed after HEAD, so the engine's own log since it is empty.
    assert staleness(code_sha=head, engine="telemac", code_dirty=False) is None


# --------------------------------------------------------------------------- #
# The bed spec's sample lattice
# --------------------------------------------------------------------------- #


def test_the_bed_spec_reproduces_each_builder_lattice_exactly():
    """px_per_deg is ANGULAR on both axes - a metric cell would not reproduce it.

    The three builders asked for 1200 / 1800 / 3000 px per degree with three
    different caps, and the sampled node values (and therefore the physics) follow
    the grid the request asks for.
    """
    import numpy as np

    from trid3nt_server.tools.fetchers._router.executors.raster_cog import (
        _imageserver_size,
    )

    for bbox, ppd, cap in (((-85.02, 29.69, -84.90, 29.80), 1800.0, 3000),
                           ((-87.60, 46.70, -86.60, 47.20), 1200.0, 2000),
                           ((-87.392, 46.528, -87.368, 46.550), 3000.0, 2500)):
        want = (int(np.clip(round((bbox[2] - bbox[0]) * ppd), 64, cap)),
                int(np.clip(round((bbox[3] - bbox[1]) * ppd), 64, cap)))
        got = _imageserver_size(bbox, {"px_per_deg": ppd, "px_min": 64,
                                       "px_max": cap})
        assert got == want, (bbox, ppd)


def test_the_bed_spec_is_registered_and_declares_a_fixed_service():
    """One mosaic, so the service is on the spec rather than a param nobody varies."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    spec = get_spec("fetch_ncei_dem_mosaic")
    assert spec is not None
    assert spec.ingest["access"] == "imageserver_export"
    assert spec.ingest["imageserver"]["service"] == "DEM_all"
    assert spec.output.role == "input"
    assert spec.output.style == {"kind": "continuous", "ramp": "gray", "units": "m", "label": "Elevation"}


# --------------------------------------------------------------------------- #
# ARTEMIS: the steering file is the only authority on a structure
# --------------------------------------------------------------------------- #


def test_the_artemis_worker_holds_no_schematic_breakwater():
    """The branch that meshed a barrier nobody asked for is GONE from the source.

    Asserted on the text because the branch's whole failure mode was being
    unreachable from any run a caller can write: no test could reach it either,
    which is how it survived so long.
    """
    source = (Path(__file__).resolve().parents[1]
              / "workers" / "telemac" / "artemis_build.py").read_text()
    assert "demo_bw" not in source
    assert "schematic demo breakwater" not in source
    # The idealized Sommerfeld domain keeps its DECLARED barrier params; what
    # died is inventing one on the real-bathymetry path.
    assert "breakwater_tip_x_m" in source
