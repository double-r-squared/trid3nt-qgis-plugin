"""RERUN-WITH-OVERRIDES: the skeleton's recalibration interface. Offline.

The setter family's capability, reproduced as skeleton machinery. What
``set_telemac_parameters`` did for one deck and one engine - copy the parent,
change named values, leave the parent byte-identical, refuse the law-inversion
that makes a coefficient mean something else - the primitive does for any
declared workflow, through the plan rather than through a text editor.

  1.  the REACH of an override is read off the plan: where reuse stops and which
      declared Data survives it;
  2.  the derivation refuses what it cannot honestly do - an unknown parent, an
      undeclared name, an override that moves nothing, a template whose
      declaration has changed;
  3.  a derived run REUSES its parent's records for the untouched prefix and
      re-executes from the cut, and the reused artifacts are the parent's OWN
      (identical URIs, never re-fetched);
  4.  the child records its parent and its overrides - in provenance, in the
      narrated note and in the journal line, so a what-if fan and a calibration
      loop are both readable as chains;
  5.  COUPLED VALIDITY: a declared cross-param rule refuses the friction
      law-inversion in BOTH lanes (a fresh invocation and a derivation), and an
      atypical-but-right-quantity value still proceeds;
  6.  a coupled rule that reads an undeclared param refuses at REGISTRATION - a
      guard that can never fire is worse than none.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.workflows.lib import (
    CoupledValidityError,
    DataDecl,
    tool,
    Param,
    ParamRef,
    PlanValidationError,
    Ref,
    Step,
    Validity,
    Workflow,
    check_validity,
    doors,
    resolve_params,
)
from trid3nt_server.workflows.lib import snapshot as snap_mod
from trid3nt_server.workflows.lib.rerun import RerunRefused, reuse_plan
from trid3nt_server.workflows.lib.rerun.derive import rerun


@pytest.fixture(autouse=True)
def _tmp_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "persistence"))
    yield


# --- (1) the reach of an override, read off the plan ------------------------ #
def test_the_cut_is_the_first_node_the_override_reaches():
    """A rate the physics step reads leaves the whole acquire prefix inheritable."""
    from trid3nt_server.tools import TOOL_REGISTRY

    wf = TOOL_REGISTRY["telemac_do_sag"].fn.workflow
    labels = [n.label for n in _nodes(wf)]

    cut, keep = reuse_plan(wf.plan, wf.data, ("k1_per_day",))
    assert labels[cut] == "waqtel"
    # the meshed river, the mid-reach seed and the National Water Model discharge
    # are all upstream of the physics block, so a rate override inherits them
    assert labels[:cut] == ["draw", "reach", "seed", "carrier_discharge"]
    # the whole domain CHAIN is upstream of the physics too - the navigated
    # mainstem, its ends, the mapped water, the reach cut between them and the
    # terrain the mesh's bed is painted from
    assert keep == frozenset({"rivers", "centerline", "ends", "window", "water",
                              "mapped_water", "reach_polygon", "dem"})


def test_a_mesh_override_cuts_later_than_a_physics_one():
    from trid3nt_server.tools import TOOL_REGISTRY

    wf = TOOL_REGISTRY["telemac_do_sag"].fn.workflow
    physics, _ = reuse_plan(wf.plan, wf.data, ("k1_per_day",))
    mesh, _ = reuse_plan(wf.plan, wf.data, ("mesh_resolution_m",))
    assert mesh > physics
    # the edge length is the MESH's ask, so the cut lands on the mesh step: the
    # mesh is rebuilt and the deck follows it, rather than the deck alone being
    # re-authored around a mesh nobody re-cut.
    assert [n.label for n in _nodes(wf)][mesh] == "mesh"


def test_an_override_the_plan_never_reads_has_no_cut():
    wf = _probe_workflow()
    assert reuse_plan(wf.plan, wf.data, ("unread",)) == (None, frozenset({"world"}))


def test_data_whose_producer_reads_the_override_is_not_inherited():
    wf = _probe_workflow()
    cut, keep = reuse_plan(wf.plan, wf.data, ("where",))
    assert cut == 0 and keep == frozenset()


# --- (2) what a derivation refuses ------------------------------------------ #
@pytest.mark.asyncio
async def test_a_run_with_no_snapshot_refuses_by_name():
    with pytest.raises(RerunRefused) as ei:
        await rerun("NOPE", {"rate": 2.0})
    assert ei.value.error_code == "RERUN_PARENT_UNKNOWN"


@pytest.mark.asyncio
async def test_no_overrides_refuses_rather_than_reproducing_the_parent():
    with pytest.raises(RerunRefused) as ei:
        await rerun("RUN1", {})
    assert ei.value.error_code == "RERUN_OVERRIDES_EMPTY"


@pytest.mark.asyncio
async def test_an_undeclared_override_name_refuses_and_lists_what_is_declared(
        monkeypatch):
    wf = _probe_workflow()
    await _seed_parent(monkeypatch, wf, "RUN1")
    with pytest.raises(RerunRefused) as ei:
        await rerun("RUN1", {"rait": 2.0})
    assert ei.value.error_code == "RERUN_OVERRIDE_UNKNOWN"
    assert "rait" in str(ei.value) and "rate" in str(ei.value)


@pytest.mark.asyncio
async def test_an_override_equal_to_the_parents_value_refuses(monkeypatch):
    wf = _probe_workflow()
    await _seed_parent(monkeypatch, wf, "RUN1")
    with pytest.raises(RerunRefused) as ei:
        await rerun("RUN1", {"rate": 1.0})
    assert ei.value.error_code == "RERUN_OVERRIDE_INERT"


@pytest.mark.asyncio
async def test_a_template_whose_params_have_changed_refuses(monkeypatch):
    wf = _probe_workflow()
    await _seed_parent(monkeypatch, wf, "RUN1")
    grown = _probe_workflow(extra=(Param("added", door=doors.SCENARIO, default="x",
                                         desc="declared after that run"),))
    monkeypatch.setattr(_registry_module(), "TOOL_REGISTRY",
                        {wf.name: _entry(grown)}, raising=False)
    with pytest.raises(RerunRefused) as ei:
        await rerun("RUN1", {"rate": 2.0})
    assert ei.value.error_code == "RERUN_TEMPLATE_CHANGED"
    assert "added" in str(ei.value)


# --- (3) + (4) the derivation itself ---------------------------------------- #
@pytest.mark.asyncio
async def test_a_derived_run_inherits_the_prefix_and_re_executes_from_the_cut(
        monkeypatch):
    wf = _probe_workflow()
    parent = await _run_probe(monkeypatch, wf, run_id="RUN1")
    assert parent.executed == ["locate", "measure", "publish"]

    child = await _rerun_probe(monkeypatch, wf, "RUN1", {"rate": 2.0})
    # `locate` reads only `where`, which did not move, so it is the parent's to
    # hand down - and the artifact handed down is the parent's own object.
    assert child.replayed == ["locate"]
    assert child.executed == ["measure", "publish"]
    assert child.results["locate"]["uri"] == parent.results["locate"]["uri"]
    assert child.results["measure"]["value"] == 2.0 * parent.results["measure"]["value"]


@pytest.mark.asyncio
async def test_the_child_records_its_parent_in_provenance_and_in_the_journal(
        monkeypatch):
    from trid3nt_server.workflows.lib import journal

    wf = _probe_workflow()
    await _run_probe(monkeypatch, wf, run_id="RUN1")
    await _rerun_probe(monkeypatch, wf, "RUN1", {"rate": 2.0})

    lines = journal.read_records()
    parent_line, child_line = lines[-2], lines[-1]
    assert parent_line["parent_run_id"] is None and parent_line["overrides"] == []
    assert child_line["parent_run_id"] == "RUN1"
    assert child_line["overrides"] == ["rate"]
    assert child_line["replayed"] == ["locate"]
    # the override is seated through the USER door, labelled with the run it
    # came from, so the sheet says WHY this value is what it is
    row = next(r for r in child_line["sheet"] if r["name"] == "rate")
    assert row["door"] == "user" and row["basis"] == "user"
    assert "override of run RUN1" in row["note"]
    assert any("derived from run RUN1 by overriding rate" in n
               for n in child_line["notes"])


@pytest.mark.asyncio
async def test_two_overrides_on_one_parent_make_two_independent_children(
        monkeypatch):
    """The what-if consumer: one parent, a fan of children, one chain each."""
    from trid3nt_server.workflows.lib import journal

    wf = _probe_workflow()
    await _run_probe(monkeypatch, wf, run_id="RUN1")
    a = await _rerun_probe(monkeypatch, wf, "RUN1", {"rate": 2.0})
    b = await _rerun_probe(monkeypatch, wf, "RUN1", {"rate": 4.0})

    assert a.results["measure"]["value"] != b.results["measure"]["value"]
    assert a.replayed == b.replayed == ["locate"]
    chained = [ln for ln in journal.read_records() if ln["parent_run_id"] == "RUN1"]
    assert len(chained) == 2


@pytest.mark.asyncio
async def test_a_child_is_itself_derivable_from(monkeypatch):
    """A calibration loop is this, walked: every child is the next parent."""
    wf = _probe_workflow()
    await _run_probe(monkeypatch, wf, run_id="RUN1")
    await _rerun_probe(monkeypatch, wf, "RUN1", {"rate": 2.0}, run_id="RUN2")
    grandchild = await _rerun_probe(monkeypatch, wf, "RUN2", {"rate": 3.0})
    assert grandchild.replayed == ["locate"]
    assert grandchild.results["measure"]["value"] == pytest.approx(15.0)


@pytest.mark.asyncio
async def test_a_failed_run_is_recorded_and_reruns_with_the_bad_value_corrected(
        monkeypatch):
    """The failure-recovery consumer: the good work survives the bad value."""
    wf = _probe_workflow()
    _no_persist(monkeypatch)
    monkeypatch.setattr(_registry_module(), "TOOL_REGISTRY",
                        {wf.name: _entry(wf)}, raising=False)
    monkeypatch.setattr(_probe_module(), "_MEASURE_REFUSES_ABOVE", 3.0)

    failed = await wf.run({"rate": 9.0, "input_mode": "auto"})
    assert failed["status"] == "error"
    # the envelope names the attempt AND the work it finished, so the caller can
    # act on it without reading a log
    attempt = failed["run_id"]
    assert "locate" in failed["error_message"] and attempt in failed["error_message"]

    captured: dict[str, Any] = {}
    _pin_run_id(monkeypatch, wf, "RECOVERED", captured)
    await rerun(attempt, {"rate": 2.0})
    assert captured["run"].replayed == ["locate"]
    assert captured["run"].results["measure"]["value"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_a_failure_with_nothing_finished_records_no_attempt(monkeypatch):
    """No inheritable work means no handle to offer - the envelope stays plain."""
    wf = _probe_workflow()
    _no_persist(monkeypatch)
    monkeypatch.setattr(_probe_module(), "_LOCATE_REFUSES", True)
    failed = await wf.run({"rate": 1.0, "input_mode": "auto"})
    assert failed["status"] == "error" and "run_id" not in failed


# --- (5) coupled validity, both lanes --------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("supplied,ok", [
    ({}, True),                                              # the declared pair
    ({"friction_law": 4, "friction_coefficient": 0.033}, True),   # Manning, honest
    ({"friction_coefficient": 120.0}, True),   # atypical Ks, still a Ks - proceeds
    ({"friction_law": 4}, False),              # Manning with a Strickler number
    ({"friction_law": 2, "friction_coefficient": 0.03}, False),   # and the inverse
])
async def test_the_friction_law_inversion_is_refused_at_resolve_time(supplied, ok):
    wf = _coastal_probe()
    resolved = await resolve_params(wf.params, supplied)
    if ok:
        check_validity(wf.validity, resolved, workflow=wf.name)
        return
    with pytest.raises(CoupledValidityError) as ei:
        check_validity(wf.validity, resolved, workflow=wf.name)
    assert ei.value.error_code == "COUPLED_VALIDITY_REFUSED"
    assert "reciprocals" in str(ei.value)


@pytest.mark.asyncio
async def test_a_law_override_that_leaves_the_coefficient_refuses_the_derivation(
        monkeypatch):
    """The setter's classic trap, now on the rerun lane: the run does not start."""
    wf = _coastal_probe()
    await _seed_parent(monkeypatch, wf, "RUNC")
    with pytest.raises(CoupledValidityError):
        await rerun("RUNC", {"friction_law": 4})
    # naming BOTH is what gets past it - which is the re-confirmation demanded
    out = await rerun("RUNC", {"friction_law": 4, "friction_coefficient": 0.033})
    assert out.law == 4.0 and out.value == pytest.approx(0.033)


# --- (6) a rule that could never fire is an authoring error ------------------ #
def test_a_validity_rule_reading_an_undeclared_param_refuses_at_registration():
    with pytest.raises(PlanValidationError) as ei:
        _probe_workflow(validity=(Validity(
            name="nonsense", reads=("not_declared",),
            holds=lambda v: True, message="never"),))
    assert "not_declared" in str(ei.value)


@pytest.mark.parametrize("kwargs", [
    dict(name="no reads", reads=(), holds=lambda v: True, message="m"),
    dict(name="no_message", reads=("a",), holds=lambda v: True, message=""),
    dict(name="not_callable", reads=("a",), holds="nope", message="m"),
])
def test_a_malformed_validity_declaration_refuses(kwargs):
    with pytest.raises(PlanValidationError):
        Validity(**kwargs)


# --- the probe workflow ------------------------------------------------------ #
_PROBE = "tests.test_rerun_with_overrides"

_STAGED: dict[str, Any] = {}

#: Failure seams, so a refusal can be aimed at a chosen node.
_MEASURE_REFUSES_ABOVE: float | None = None
_LOCATE_REFUSES = False


def locate(*, where: str) -> dict[str, Any]:
    """A step whose product is an ARTIFACT, so replay has a URI to point at."""
    if _LOCATE_REFUSES:
        raise ValueError(f"cannot locate {where!r}")
    _STAGED.setdefault(where, 0)
    _STAGED[where] += 1
    return {"uri": f"file:///staged/{where}/{_STAGED[where]}", "where": where}


def measure(*, source: str, rate: float) -> dict[str, Any]:
    if _MEASURE_REFUSES_ABOVE is not None and float(rate) > _MEASURE_REFUSES_ABOVE:
        raise ValueError(f"rate {rate} is past what this probe measures")
    return {"value": 5.0 * float(rate), "source": source}


def publish(*, measured: dict) -> "_Result":
    return _Result(measured["value"])


def world(*, where: str) -> dict[str, Any]:
    return {"uri": f"file:///world/{where}", "where": where}


class _Result:
    """What the publish stage needs a result to look like."""

    def __init__(self, value: float) -> None:
        self.uri = "file:///out.tif"
        self.layer_id = "L"
        self.run_id = None
        self.value = value
        self.fallback_note = None
        self.synthetic_inputs: list[Any] = []

    def model_copy(self, *, update: dict[str, Any]) -> "_Result":
        for key, val in update.items():
            setattr(self, key, val)
        return self


def _probe_workflow(*, extra: tuple = (), validity: tuple = ()) -> Workflow:
    params = (
        Param("where", door=doors.USER, default="here", desc="the place"),
        Param("rate", door=doors.SCENARIO, default=1.0, bounds=(0.1, 100.0),
              desc="the value a rerun moves"),
        Param("unread", door=doors.SCENARIO, default="x",
              desc="declared but read by nothing in the plan"),
    ) + extra

    class Probe(Workflow):
        engine = "probe"
        solve_step = ""

        def acquire_domain(self, **slots):
            return ()

        def author(self, *, mesh, physics, forcing):
            return Step(runner=f"{_PROBE}.locate")

        def solve(self, **slots):
            return Step(runner=f"{_PROBE}.locate")

        def read(self, run, **slots):
            return Step(runner=f"{_PROBE}.locate")

    def plan(ops):
        return [
            Step(runner=f"{_PROBE}.locate", kwargs={"where": ParamRef("where")}).named("locate"),
            Step(runner=f"{_PROBE}.measure",
                 kwargs={"source": Ref("locate.where"), "rate": ParamRef("rate")}).named("measure"),
            Step(runner=f"{_PROBE}.publish",
                 kwargs={"measured": Ref("measure")}).named("publish"),
        ]

    return Probe(
        metadata=AtomicToolMetadata(name="probe_rerun", ttl_class="live-no-cache",
                                    source_class="workflow_dispatch",
                                    cacheable=False, engine="probe",
                                    tier="template"),
        params=params,
        data=(DataDecl("world", tool(f"{_PROBE}.world", where=ParamRef("where"))),),
        plan=plan, answer=("value",), validity=validity)


#: Laws this rule speaks about, and the Strickler/Manning crossover that tells
#: the two coefficient quantities apart: laws 2 and 3 take a Ks around 15-90, law
#: 4 takes a Manning n around 0.011-0.1, so a value below the crossover is an n.
_FRICTION_LAWS = (2, 3, 4)
_FRICTION_CROSSOVER = 1.0


def _friction_matches_law(v) -> bool:  # noqa: ANN001 - a ParamValues view
    """Is the coefficient the quantity this law reads it as?"""
    if int(v.friction_law) not in _FRICTION_LAWS:
        return True             # a law this rule says nothing about
    manning = int(v.friction_law) == 4
    return (float(v.friction_coefficient) < _FRICTION_CROSSOVER) is manning


_FRICTION_VALIDITY = (
    Validity(
        name="friction_coefficient_matches_law",
        reads=("friction_law", "friction_coefficient"),
        holds=_friction_matches_law,
        message=(
            "friction_law={friction_law} with friction_coefficient="
            "{friction_coefficient} reads the coefficient as the WRONG quantity. "
            "The two are reciprocals of each other."),
    ),
)


def _coastal_probe() -> Workflow:
    """A friction law/coefficient pair on a plan that needs no solver."""
    params = (
        Param("friction_law", door=doors.CONSTANT, default=3, type=int,
              bounds=(1.0, 7.0), consequence="physics", desc="the law"),
        Param("friction_coefficient", door=doors.SCENARIO, default=40.0,
              bounds=(0.001, 200.0), consequence="physics", desc="the coefficient"),
    )

    class Probe(Workflow):
        engine = "probe"

        def acquire_domain(self, **slots):
            return ()

        def author(self, *, mesh, physics, forcing):
            return Step(runner=f"{_PROBE}.friction_ok")

        def solve(self, **slots):
            return Step(runner=f"{_PROBE}.friction_ok")

        def read(self, run, **slots):
            return Step(runner=f"{_PROBE}.friction_ok")

    def plan(ops):
        return [Step(runner=f"{_PROBE}.friction_ok",
                     kwargs={"law": ParamRef("friction_law"),
                             "coefficient": ParamRef("friction_coefficient")}).named("check")]

    return Probe(metadata=AtomicToolMetadata(
        name="probe_friction", ttl_class="live-no-cache",
        source_class="workflow_dispatch", cacheable=False, engine="probe",
        tier="template"), params=params, plan=plan, validity=_FRICTION_VALIDITY)


def friction_ok(*, law: float, coefficient: float) -> "_Result":
    out = _Result(float(coefficient))
    out.law = float(law)
    return out


def _nodes(wf):
    from trid3nt_server.workflows.lib import expand_plan

    return expand_plan(wf.plan)


def _registry_module():
    import trid3nt_server.tools as mod

    return mod


def _probe_module():
    return sys.modules[__name__]


class _Entry:
    def __init__(self, fn) -> None:
        self.fn = fn


def _entry(workflow):
    def fn():  # pragma: no cover - only its .workflow attribute is read
        raise AssertionError("the registry entry is a handle, not a callable here")

    fn.workflow = workflow
    return _Entry(fn)


async def _run_probe(monkeypatch, wf, *, run_id: str, **wire) -> Any:
    """Run the probe once through the real spine, pinning the run id it publishes."""
    captured: dict[str, Any] = {}
    _pin_run_id(monkeypatch, wf, run_id, captured)
    _no_persist(monkeypatch)
    await wf.run(dict(wire, input_mode="auto"))
    return captured["run"]


async def _rerun_probe(monkeypatch, wf, parent: str, overrides: dict,
                       *, run_id: str | None = None) -> Any:
    captured: dict[str, Any] = {}
    _pin_run_id(monkeypatch, wf, run_id or f"CHILD-{parent}-{sorted(overrides)}",
                captured)
    _no_persist(monkeypatch)
    monkeypatch.setattr(_registry_module(), "TOOL_REGISTRY",
                        {wf.name: _entry(wf)}, raising=False)
    out = await rerun(parent, overrides)
    return captured.get("run", out)


def _pin_run_id(monkeypatch, wf, run_id, captured):
    """The probe declares no solve step, so the run id is pinned here instead."""
    monkeypatch.setattr(type(wf), "_run_id",
                        lambda self, result, run: run_id, raising=False)
    real = type(wf)._publish

    async def _capture(self, run, wall_seconds=0.0, **kw):
        captured["run"] = run
        return await real(self, run, wall_seconds, **kw)

    monkeypatch.setattr(type(wf), "_publish", _capture, raising=False)


def _no_persist(monkeypatch):
    from trid3nt_server.workflows.shared import run_products

    async def _skip(run_id, *, charts, metrics):
        return []

    monkeypatch.setattr(run_products, "persist_run_products", _skip)


async def _seed_parent(monkeypatch, wf, run_id: str) -> None:
    """A parent snapshot with no run behind it - for the refusal cases."""
    resolved = await resolve_params(wf.params, {})
    monkeypatch.setattr(_registry_module(), "TOOL_REGISTRY",
                        {wf.name: _entry(wf)}, raising=False)
    _no_persist(monkeypatch)
    await snap_mod.write_snapshot(
        run_id=run_id, workflow=wf.name, input_mode="auto",
        sheet=resolved.rows(), records=(), data_records=(), supplied={})


def test_the_probe_workflow_is_not_registered():
    """It is a fixture, not a product: nothing here may reach the tool registry."""
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "probe_rerun" not in TOOL_REGISTRY
    assert "probe_friction" not in TOOL_REGISTRY


def test_the_registered_surface_replaced_the_setter():
    """The setter's registration is GONE and the primitive stands in its place."""
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "set_telemac_parameters" not in TOOL_REGISTRY
    assert "rerun_workflow" in TOOL_REGISTRY
    meta = TOOL_REGISTRY["rerun_workflow"].metadata
    assert meta.ttl_class == "live-no-cache" and meta.cacheable is False
    doc = TOOL_REGISTRY["rerun_workflow"].fn.__doc__ or ""
    # the constant-door carve-out is DELIBERATE and has to be stated where the
    # caller reads: recalibration is the sanctioned door for a fixed quantity
    assert "own schema" in doc and "sanctioned" in doc


def test_rerun_workflow_is_retrievable_for_its_own_phrasings():
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    for prompt in ("rerun the last simulation but change the friction coefficient",
                   "run that again with a rougher bed",
                   "the run failed on that parameter, run it again with a "
                   "corrected value"):
        assert "rerun_workflow" in retrieve_visible_tools(prompt, None, 8), prompt
