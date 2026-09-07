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
    import trid3nt_server.workflows.telemac.solving.run_telemac  # noqa: F401
    from trid3nt_server.workflows.solver.solver import LOCAL_SOLVER_SPEC_REGISTRY

    for name in ("artemis_agitation", "telemac3d_strat", "telemac_river_dye"):
        assert LOCAL_SOLVER_SPEC_REGISTRY[name]().network == "none", name


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


def test_the_bed_spec_is_registered_and_pins_one_product_of_the_mosaic():
    """DEM_all serves every NCEI DEM under one endpoint - coastal tiles on NAVD88,
    the same tiles on MHW, the ETOPO bases on EGM2008 - so the row that reads it
    as a bed pins the ONE product it means and states that product's datum."""
    from trid3nt_server.tools.fetchers._router.registration import get_spec

    spec = get_spec("fetch_greatlakes_bathymetry")
    assert spec is not None
    assert spec.ingest["access"] == "imageserver_export"
    assert spec.ingest["imageserver"]["service"] == "DEM_all"
    assert "greatlakes_lakedatum" in \
        spec.ingest["imageserver"]["export_query"]["mosaicRule"]
    assert "Low Water Datum" in (spec.vertical_datum or "")
    assert spec.output.role == "input"
    assert spec.output.style == {"kind": "continuous", "ramp": "gray", "units": "m", "label": "Elevation"}
