"""The workflow SKELETON itself: the template method, the facade, the factory.

Offline, nothing here solves. Hooks default silently; the factory synthesizes the
wire signature from the declared params and renders the docstring from that same
narrowed set; slot and signature are checked both ways; the facade's four MUST be
filled; a coercion failure is triaged; a guessable-wrong wire type refuses."""

from __future__ import annotations

import inspect
from types import MappingProxyType
from typing import Any

import pytest

from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import DeclarationError, Workflow


# --- (2) hooks: silent by default, effective when filled --------------------- #
def _metadata(name: str) -> AtomicToolMetadata:
    return AtomicToolMetadata(name=name, ttl_class="live-no-cache",
                              source_class="workflow_dispatch", cacheable=False,
                              engine="telemac", tier="template")


class _Layer:
    """The minimum a published result has to look like to the publish stage."""

    def __init__(self) -> None:
        self.uri = "s3://b/k.tif"
        self.layer_id = "L"
        self.notes: list[str] = []
        self.synthetic_inputs: list[Any] = []
        self.depth_max_m = 1.5

    def model_copy(self, *, update: dict[str, Any]) -> "_Layer":
        for key, value in update.items():
            setattr(self, key, value)
        return self


class _Stub(Workflow):
    """A workflow whose template states its steps under STEPS."""

    def steps(self):
        return self.template.STEPS(self)


def _module(plan, **names):
    from types import SimpleNamespace

    return SimpleNamespace(STEPS=plan, **names)


def _workflow(cls=_Stub, **kw):
    return cls(metadata=_metadata("skeleton_probe"), inputs=(),
               template=_module(lambda o: ()), **kw)


def test_the_hooks_are_silent_by_default():
    wf = _workflow()
    from trid3nt_server.workflows.runtime.workflow import RunResult

    assert wf.checks(RunResult(value=_Layer()), {}) == ()


@pytest.mark.asyncio
async def test_a_filled_check_hook_reaches_the_result_as_a_note(monkeypatch):
    from trid3nt_server.workflows.runtime import RunResult
    from trid3nt_server.workflows.runtime import run_output

    class Checked(_Stub):
        def checks(self, run, inputs):
            return (f"depth {run.value.depth_max_m} m is a screening figure",)

    async def _no_persist(run_id, *, charts):
        return []

    monkeypatch.setattr(run_output, "persist_run_products", _no_persist)
    wf = _workflow(Checked)
    out = await wf._publish(RunResult(value=_Layer()))
    assert "depth 1.5 m is a screening figure" in out.notes


# --- (3) the registration factory synthesizes the wire ---------------------- #
def test_the_generated_signature_is_the_declaration_plus_controls():
    from trid3nt_server.workflows.runtime.workflow import _wire_signature

    params = (
        Input(name="location", desc="a place"),
        Input(name="depth_m", default=1.0, bounds=(0.0, 9.0),
              desc="a bounded value"),
        Input(name="armed", type=bool, desc="a flag"),
        Input(name="solver_dt_s", default=1.0, bounds=(0.1, 60.0),
              desc="a solver setting"),
    )
    sig, annotations = _wire_signature(params)
    assert list(sig.parameters) == ["location", "depth_m", "armed",
                                    "solver_dt_s", "input_mode", "restart_clean", "keywords",
                                    "picks", "_extra_ignored"]
    assert annotations["depth_m"] == (float | None)      # bounded -> float
    assert annotations["armed"] == bool                  # declared type wins, and it is required
    assert annotations["location"] == str                # inferred, and required
    assert sig.parameters["restart_clean"].default is False
    assert all(sig.parameters[n].default is None
               for n in ("location", "depth_m", "armed", "input_mode",
                         "keywords"))
    # The absorber is the channel, not a tolerance: what it carries is refused.
    assert sig.parameters["_extra_ignored"].kind is inspect.Parameter.VAR_KEYWORD


def test_an_argument_the_tool_does_not_declare_is_refused_by_name():
    """Never run as if unstated: a misspelling names its nearest input, a name
    nothing is close to says so, and nothing is launched."""
    import asyncio

    from trid3nt_server.tools import TOOL_REGISTRY

    out = asyncio.run(TOOL_REGISTRY["telemac_dye_release"].fn(
        spill_duraton_s=600.0, cores=8, description="a dye run"))
    said = out["error_message"]
    assert out["status"] == "error" and out["error_code"] == "INPUT_UNNAMED"
    assert "'spill_duraton_s' is not an input of telemac_dye_release " \
        "(nearest: spill_duration_s" in said
    assert "'cores' is not an input of telemac_dye_release (none is close)." in said
    # A field the model invented resembles no input, so none is suggested for it.
    assert "'description' is not an input of telemac_dye_release " \
        "(none is close)." in said
    assert said.count("inside keywords") == 1


def test_a_required_param_is_required_on_the_schema_and_says_so_on_its_line():
    """What the fill refuses unstated is what the model is told it must state:
    the schema's ``required`` and the docstring line read the same predicate."""
    from trid3nt_server.model.adapters.adapter import _tool_schema
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["telemac_rain_on_grid"].fn
    wired = [p for p in fn.workflow.inputs if p.fills_from_user]
    required = [p.name for p in wired if p.required]
    assert "pour_point" in required
    assert _tool_schema(fn)["required"] == required
    lines = [line for line in fn.__doc__.splitlines() if line.startswith("    ")]
    params = lines[:len(wired)]
    assert [line.endswith(", required.") for line in params] == \
        [index < len(required) for index in range(len(params))]
    assert [line.split(":")[0].strip() for line in params[:len(required)]] == required


def test_values_and_rows_are_documented_in_one_section_in_wire_order():
    """One body, one section: required values, defaulted ones, then the optional
    values and the rows in the order the wire carries them, each row's line still
    saying what a supplied layer is and how the run fills it otherwise."""
    from trid3nt_server.tools import TOOL_REGISTRY

    fn = TOOL_REGISTRY["telemac_rain_on_grid"].fn
    doc = fn.__doc__
    assert "Context layers:" not in doc and doc.count("\nParams:\n") == 1
    block = doc[doc.index("\nParams:\n"):doc.index("\nSheet: ")]
    named = [line.split(":")[0].strip() for line in block.splitlines()
             if line.startswith("    ")]
    wired = [p for p in fn.workflow.inputs if p.fills_from_user]
    order = sorted(wired, key=lambda p: 0 if p.required else
                   1 if p.default is not None else 2)
    assert named == [p.name for p in order]
    assert [p.is_row for p in order] == sorted(p.is_row for p in order)
    bed = fn.workflow.input("bed")
    assert f"    bed: {bed.doc_line}" in block
    assert "unfilled, the run matches a source of terrain" in bed.doc_line


def test_every_declared_param_is_on_the_real_tools_wire_and_docstring():
    from trid3nt_server.tools import TOOL_REGISTRY

    for name in ("telemac_do_sag", "telemac_dye_release"):
        fn = TOOL_REGISTRY[name].fn
        declared = {p.name for p in fn.workflow.inputs}
        assert {"event_time", "vertical_frame"} <= declared
        assert declared <= set(inspect.signature(fn).parameters)
        assert all(f"    {p}:" in fn.__doc__ for p in declared)


def test_a_stated_seated_input_reaches_the_sheet_as_the_users():
    import asyncio

    from trid3nt_server.tools import TOOL_REGISTRY
    from seated import resolve_params

    wf = TOOL_REGISTRY["telemac_do_sag"].fn.workflow
    from trid3nt_server.workflows.runtime.fill import ACCEPTED, Fill, fill

    state = asyncio.run(fill(Fill(workflow=wf), {"vertical_frame": "IGLD85",
                                                  "mesh_resolution_m": 30.0}))
    supplied = {n: v.value for n, v in state.inputs.items() if v.state == ACCEPTED}
    assert supplied["vertical_frame"] == "IGLD85"
    assert supplied["mesh_resolution_m"] == 30.0
    sheet = asyncio.run(resolve_params(wf.inputs, supplied))
    assert sheet["vertical_frame"].value == "IGLD85"
    assert sheet["vertical_frame"].origin == "user"


def test_a_param_edited_on_the_review_card_is_what_the_run_records():
    """The record and the provenance are read after the launch, so a value a
    person changed on the card is written as the user's and never as the
    template's starting one."""
    import asyncio

    from trid3nt_server.workflows.runtime import RunResult
    from trid3nt_server.workflows.runtime.fill import Fill, fill
    from trid3nt_server.workflows.telemac.workflow import _seat_on

    class Edited(_Stub):
        async def launch(self, state):
            _seat_on(state.env)({"rate": 9.0})
            return RunResult(value=_Layer())

    wf = Edited(metadata=_metadata("skeleton_probe"), template=_module(lambda o: ()),
                inputs=(Input(name="rate", desc="a rate", default=6.0, bounds=(0.0, 60.0)),))

    async def _run():
        return await wf._launched(await fill(Fill(workflow=wf), {}))

    run = asyncio.run(_run())
    row, entry = run.inputs["rate"], run.entries[0]
    assert (row.value, row.origin, entry.value, entry.basis) == (9.0, "user", 9.0, "user")


# --- (4) a chart builder is the function, with no string fallback ----------- #


# --- (5) an unknown slot member is refused at plan construction ------------- #


def _reach_mesh(**params):
    """The MESH recipe a reach template writes, with test values for its ask."""
    from trid3nt_server.tools.mesh.tool import tool

    params.setdefault("extent", "reach_polygon")
    return tool.build_mesh(mesher="om2d", kind="unstructured_tri", **params)


# --- (7) a coercion's failure is triaged, never flattened ------------------- #
class _Retryable(Exception):
    """What a gate raises: the adapter harvests .suggestions off the RAISED object."""

    retryable = True
    suggestions = ("send location='Eel River, California'",)


@pytest.mark.asyncio
async def test_a_retryable_coercion_failure_propagates_with_its_suggestions():
    def _gate(args):
        raise _Retryable("pick one")

    wf = _workflow(coerce=(_gate,))
    with pytest.raises(_Retryable) as ei:
        await wf.run({})
    assert ei.value.suggestions  # the channel survived; nothing flattened it


@pytest.mark.asyncio
async def test_a_typed_coercion_refusal_keeps_its_own_error_code():
    from trid3nt_server.inputs.user_input import WireArgsError

    def _typed(args):
        raise WireArgsError("needs a location", error_code="TELEMAC_PARAMS_INCOMPLETE")

    out = await _workflow(coerce=(_typed,)).run({})
    assert out["error_code"] == "TELEMAC_PARAMS_INCOMPLETE"


@pytest.mark.asyncio
async def test_a_BUG_in_a_coercion_reads_as_internal_never_as_the_caller_s_fault():
    """PARAMS_INVALID blames the caller for our own crash and sends the model off
    to 'fix' arguments that were never wrong."""
    def _buggy(args):
        return {"x": 1 / 0}

    out = await _workflow(coerce=(_buggy,)).run({})
    assert out["error_code"] == "TELEMAC_INTERNAL_ERROR"
    assert out["status"] == "error"


# --- (8) a declaration whose wire type would be guessed wrong refuses -------- #
def test_an_unbounded_numeric_default_refuses_rather_than_advertising_a_string():
    with pytest.raises(DeclarationError) as ei:
        Input(name="k_per_day", default=0.3, desc="a rate")
    assert "STRING" in str(ei.value)
    # ... and both offered fixes are accepted
    assert Input(name="k_per_day", default=0.3,
                 bounds=(0.01, 20.0), desc="a rate").wire_type is float
    assert Input(name="k_per_day", default=0.3,
                 type=float, desc="a rate").wire_type is float


def test_a_bool_default_is_not_a_numeric_default():
    """bool IS an int in Python; a flag infers bool correctly and must not refuse."""
    assert Input(name="armed", default=False,
                 desc="a flag").wire_type is bool
