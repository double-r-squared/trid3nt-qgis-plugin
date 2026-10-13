"""Declarative library v1: value types, params, validator, interpreter, ledger.

Offline only - every runner here is a local stub; no solve, no network.
"""
from __future__ import annotations
from trid3nt_server.workflows.telemac.authoring.selafin_io import (
    BOUNDARY_CONDITIONS_FILE,
    GEOMETRY_FILE,
)

import collections
import contextlib
import dataclasses
import importlib
import warnings
from types import MappingProxyType, SimpleNamespace

import pytest
from pydantic import BaseModel

from trid3nt_server.inputs.input import Input
from trid3nt_server.workflows.runtime import (
    DeclarationError,
    StepFailedError,
    SuppliedGeometryError,
    Workflow,
    tool,
    merge_provenance,
    provenance_entries,
    render_docstring,
)
from trid3nt_server.inputs.bound_domain import Domain
from seated import resolve_params

_HERE = "tests.runtime.test_declarative_library"


@contextlib.contextmanager
def _patched(target, name, value):
    """Patch and restore by hand.

    NOT monkeypatch: the fixture and the test share one instance, so a mid-test
    ``undo()`` would revert the fixture's persistence-dir env as well."""
    original = target.__dict__[name]
    setattr(target, name, value)
    try:
        yield
    finally:
        setattr(target, name, original)


# --- stub runners the plans below name by dotted path ----------------------- #
_CALLS: list[str] = []
_FAIL_AT: set[str] = set()
#: artifact URIs the fake object store does NOT hold (the dead-URI probe).
_MISSING_ARTIFACTS: set[str] = set()


class _RetryableGate(RuntimeError):
    """Stands in for the engines' retryable typed gates (banks/reach)."""

    retryable = True
    error_code = "STUB_GATE"

    def __init__(self, message="retry me"):
        super().__init__(message)
        self.suggestions = ["widen the reach", "pass banks explicitly"]


async def stub_step(**kwargs):
    _CALLS.append("stub_step")
    if "stub_step" in _FAIL_AT:
        raise RuntimeError("boom")
    return {"uri": "s3://b/k.tif", "value": kwargs}


async def stub_second(**kwargs):
    _CALLS.append("stub_second")
    if "stub_second" in _FAIL_AT:
        raise RuntimeError("boom-2")
    return {"uri": "s3://b/k2.tif", "seen": kwargs}


async def stub_producer(**kwargs):
    _CALLS.append("stub_producer")
    if "stub_producer" in _FAIL_AT:
        raise RuntimeError("producer down")
    return "s3://b/produced.tif"


async def stub_noting(**kwargs):
    """A step that MEASURES something the reader has to know and has no result
    field to say it in - the note channel's whole reason to exist."""
    from trid3nt_server.inputs.run_notes import journal_note

    _CALLS.append("stub_noting")
    journal_note("measured 42.0% coverage")
    if "stub_noting" in _FAIL_AT:
        raise RuntimeError("noted, then failed")
    return {"uri": "s3://b/noted.tif"}


async def stub_mesh_step(**kwargs):
    """A mesh step's own result shape: the ARTIFACT beside the fields read off it.

    Its consumers read the artifact by attribute, so what the replay hands back
    has to be the artifact and not a mapping that merely holds its fields.
    """
    from trid3nt_server.tools.mesh.artifact import MeshArtifact

    _CALLS.append("stub_mesh_step")
    art = MeshArtifact(
        mesh_id="m-1", name="reach mesh", mode="om2d",
        display_uri="s3://b/mesh.2dm",
        engine_files={GEOMETRY_FILE: "s3://b/mesh.slf"},
        crs_authid="EPSG:32610", has_bathymetry=True,
        node_count=7, element_count=6,
        bbox=(-124.1, 40.4, -124.0, 40.5), utm_epsg=32610,
        probes={"min_edge_m": 40.5, "edge_length_m": {"min": 40.5, "max": 400.0}},
        provenance={"recipe": {"extent": {"type": "Polygon", "coordinates": [
            [[-124.1, 40.4], [-124.0, 40.4], [-124.0, 40.5], [-124.1, 40.4]]]}}},
    )
    return {"uri": "s3://b/mesh.slf", "artifact": art, "mesh_id": art.mesh_id,
            "min_edge_m": 40.5, "element_count": art.element_count}


async def stub_no_bbox(**kwargs):
    """A layer result whose ``bbox`` field is THERE and empty - the None-missing
    tail the binder must refuse rather than carry forward."""
    _CALLS.append("stub_no_bbox")
    return {"uri": "s3://b/line.fgb", "bbox": None}


async def stub_rung(**kwargs):
    _CALLS.append("stub_rung")
    if "stub_rung" in _FAIL_AT:
        raise RuntimeError("rung down")
    return "s3://b/rung.tif"


async def stub_self_gating(**kwargs):
    _CALLS.append("stub_self_gating")
    return {"uri": "s3://b/sg.tif", "seen": kwargs}


async def stub_gate_raiser(**kwargs):
    _CALLS.append("stub_gate_raiser")
    raise _RetryableGate()


def stub_chart(*, result, params):
    _CALLS.append("stub_chart")
    if "stub_chart" in _FAIL_AT:
        raise RuntimeError("chart-boom")
    return {"chart_id": "c1", "title": "t"}


def stub_chart_empty(*, result, params):
    """A builder with nothing to plot - the curve the result should carry is absent."""
    _CALLS.append("stub_chart_empty")
    return None


def stub_chart_leaks(*, result, params):
    """A builder that puts a DESCRIPTION in the title - the f-string leak, one call
    later, where only the emitted payload can catch it."""
    _CALLS.append("stub_chart_leaks")
    return {"chart_id": "c1", "title": "base"}


class _StubProduct(BaseModel):
    """A pydantic result, because the skeleton's publish stage ``model_copy``s one."""

    uri: str = "s3://b/product.tif"
    layer_id: str = "stub-product"
    run_id: str | None = None
    synthetic_inputs: list = []
    notes: list[str] = []


async def stub_product(**kwargs):
    _CALLS.append("stub_product")
    return _StubProduct()


async def stub_deep(**kwargs):
    """A clean result deep enough to exhaust a shrunken scan budget."""
    _CALLS.append("stub_deep")
    node = {"leaf": 1.0}
    for i in range(40):
        node = {"n": node, "i": i}
    return {"uri": "s3://b/k.tif", "deep": node}


def derive_double(params):
    return float(params.base) * 2.0


def derive_broken(params):
    _ = params.base
    return "nope".missing_attribute        # a real bug, not a dependency wait


# --- Param declarations ------------------------------------------------------ #
def test_inverted_bounds_refused():
    with pytest.raises(DeclarationError):
        Input(name="x", desc="d", default=1.0, bounds=(9.0, 1.0))


@pytest.mark.asyncio
async def test_a_value_outside_its_bounds_refuses_by_name_and_never_clamps():
    """The lever is the user's: a value moved onto the bound would run a question
    nobody asked and say nothing while it did it."""
    declared = [Input(name="a", desc="d", default=1.0,
                      bounds=(0.0, 5.0), units="m")]
    with pytest.raises(Exception) as exc:
        await resolve_params(declared, {"a": 99.0})
    assert "a=99 m is outside the declared range 0 to 5 m" in str(exc.value)
    p = await resolve_params(declared, {"a": 5.0})
    assert p["a"].value == 5.0


@pytest.mark.asyncio
async def test_the_bound_compares_numbers_and_does_not_retype_the_param():
    """A row that declares int must not resolve to a float.

    The bound reads its ends as numbers, which is right; handing back what it
    read makes an engine keyword refuse later and name the KEYWORD, not the
    declaration."""
    declared = Input(name="levels", desc="d", default=13,
                     bounds=(5.0, 30.0), type=int)
    for supplied, expected in (({"levels": 13}, 13), ({}, 13),
                               ({"levels": 30}, 30), ({"levels": 5}, 5)):
        resolved = await resolve_params([declared], supplied)
        value = resolved["levels"].value
        assert value == expected and isinstance(value, int), (supplied, value)


@pytest.mark.asyncio
async def test_non_numeric_bounded_value_refuses_never_defaults():
    with pytest.raises(Exception) as exc:
        await resolve_params(
            [Input(name="a", desc="d", default=1.0, bounds=(0.0, 5.0))],
            {"a": "nonsense"},
        )
    assert "not a number" in str(exc.value)


@pytest.mark.asyncio
async def test_a_bool_is_refused_for_a_bounded_param():
    """bool IS an int, so True would coerce to 1.0 - a flag is not a measurement."""
    for flag in (True, False):
        with pytest.raises(Exception, match="not a number"):
            await resolve_params(
                [Input(name="a", desc="d", default=1.0,
                       bounds=(0.0, 5.0))],
                {"a": flag},
            )


@pytest.mark.asyncio
async def test_optional_absent_param_resolves_to_none():
    p = await resolve_params([Input(name="a", desc="d").optional()], {})
    assert "a" not in p


@pytest.mark.asyncio
async def test_a_default_is_stamped_as_the_templates_not_as_the_user():
    decl = [Input(name="a", desc="d", default=3.0, type=float)]
    p = await resolve_params(decl, {})
    assert p["a"].value == 3.0 and p["a"].origin == "template"
    p2 = await resolve_params(decl, {"a": 4.0})
    assert p2["a"].origin == "user"


@pytest.mark.asyncio
async def test_an_absent_param_with_a_derived_stand_in_still_leaves_a_row():
    decl = [Input(name="outfall", desc="where it enters").optional(
        "seeded at the derived reach point")]
    p = await resolve_params(decl, {})
    rows = provenance_entries(p, decl)
    assert [r.param for r in rows] == ["outfall"]
    assert rows[0].basis == "derived"
    assert "derived reach point" in rows[0].note


def test_the_composites_own_provenance_row_wins():
    from trid3nt_contracts.common import SyntheticInput

    own = SyntheticInput(param="bed_source", value="nhd_area", basis="producer",
                         note="real NHDArea banks")
    declared = SyntheticInput(param="bed_source", value="nhd_area",
                              basis="template", note="declared default")
    other = SyntheticInput(param="k1", value=0.3, basis="template")
    merged = merge_provenance([own], [declared, other])
    assert [r.param for r in merged] == ["bed_source", "k1"]
    assert merged[0].basis == "producer"


# --- modifier legality ------------------------------------------------------- #
def test_one_author_word_declares_every_producer():
    """There is no fetch/build role on the declaration: what a runner does to the
    world is the REGISTRY's knowledge, so both runners are declared the same way
    and both carry the same modifiers."""
    from trid3nt_server.workflows.runtime import Producer

    fetcher, builder = tool("fetch_dem"), tool("build_mesh")
    assert type(fetcher) is type(builder) is Producer


def _params():
    return [Input(name="base", desc="d", default=1.0, type=float),
            Input(name="pt", desc="d").optional()]


def test_the_wire_refuses_an_input_declared_twice():
    from trid3nt_server.inputs.user_input import WireArgsError
    from trid3nt_server.workflows.runtime.workflow import _wire_signature

    decl = [Input(name="base", desc="d", default=1.0, type=float),
            Input(name="base", desc="other", default=2.0, type=float)]
    with pytest.raises(WireArgsError, match="twice"):
        _wire_signature(decl)


# --- a ref tail is refused at BINDING, never bound to a silent None ----------- #


# --- the ONE class body -------------------------------------------------------- #
def test_one_body_names_values_and_rows_alike_and_keeps_their_order():
    """The attribute name IS the input's name and the body's order IS the order;
    a value and a row are one class, told apart by what each states."""
    from trid3nt_server.inputs.input import declared

    class INPUTS:
        depth_m = Input(default=1.0, bounds=(0.0, 9.0), desc="a depth")
        armed = Input(type=bool, desc="a flag").optional()
        dem = tool("fetch_dem", source="3dep")
        walls = Input().supplied(geometry="polyline").optional()

    rows = declared(INPUTS)
    assert all(type(row) is Input for row in rows)
    assert [r.name for r in rows] == ["depth_m", "armed", "dem", "walls"]
    assert [r.is_row for r in rows] == [False, False, True, True]
    assert rows[0].bounds == (0.0, 9.0) and not rows[0].required
    assert rows[2].producer.kwargs["source"] == "3dep"
    assert rows[3].producer is None and rows[3].geometry == "polyline"
    assert rows[3].is_optional is True
    with pytest.raises(AttributeError):
        INPUTS.deth_m


def test_the_old_declaration_names_are_gone():
    import trid3nt_server.workflows.runtime as runtime

    for gone in ("Param", "Data", "DataDecl", "param_rows", "data_rows",
                 "ResolvedParams"):
        assert not hasattr(runtime, gone)


def test_a_value_with_no_desc_refuses_the_declaration():
    from trid3nt_server.inputs.input import declared

    with pytest.raises(DeclarationError, match="declares no desc"):
        declared([Input(name="depth_m", default=1.0, bounds=(0.0, 9.0))])


def test_one_declaration_cannot_be_bound_to_two_names():
    """A row is one declaration in one body; sharing the object would make the
    second name silently the first."""
    shared = Input(default=1.0, bounds=(0.0, 9.0), desc="d")

    with pytest.raises(DeclarationError, match="declared again"):
        class PARAMS:
            first = shared
            second = shared


# --- a producer-less Data slot: context handed in, or labelled absence -------- #


def test_a_producer_backed_data_may_not_be_optional():
    """A producer either produces or fails typed, so there is no absence to describe."""
    with pytest.raises(DeclarationError, match="optional"):
        Input(name="mesh", producer=tool(f"{_HERE}.stub_producer")).optional()


def test_a_context_slot_declares_the_shape_it_accepts():
    """A slot names no source - the SHAPE is the only thing it can honestly say."""
    slot = Input(name="structure").supplied(geometry="polyline").optional()
    assert slot.producer is None
    assert slot.geometry == "polyline" and slot.is_optional is True


def test_a_context_slot_refuses_a_shape_nobody_declares():
    with pytest.raises(DeclarationError, match="not a declared shape"):
        Input(name="structure").supplied(geometry="squiggle")


def test_a_produced_row_is_not_also_supplied():
    with pytest.raises(DeclarationError, match="declares a producer AND"):
        Input(name="walls", producer=tool(f"{_HERE}.stub_producer")).supplied(geometry="polyline")


async def stub_refine(**kwargs):
    _CALLS.append("stub_refine")
    return {"bbox": [10.0, 10.0, 11.0, 11.0], "name": "refined"}


# --- the ledger: every terminal state tombstones ------------------------------ #


# --- generated docstring ------------------------------------------------------ #
def test_docstring_front_loads_routing_within_the_truncation_budget():
    doc = render_docstring(
        summary="S.", routing="R " * 100, inputs=_params(), returns="a layer",
    )
    assert doc.index("\nParams:") < 1000
    assert doc.startswith("S.")


def test_docstring_refuses_a_routing_block_over_the_budget():
    with pytest.raises(ValueError, match="truncation budget"):
        render_docstring(summary="S.", routing="x" * 1200, inputs=_params(),
                         returns="a layer")


def test_the_routing_view_stops_before_the_param_sheet():
    """Two views, one declaration: a surface that only helps someone CHOOSE the
    tool takes the routing block; the model filling the params takes the sheet."""
    kwargs = dict(summary="S.", routing="R.", inputs=_params(), returns="a layer",
                  not_for="something else")
    routing = render_docstring(**kwargs, view="routing")
    full = render_docstring(**kwargs)
    assert "Params:" not in routing and "Params:" in full
    assert routing.startswith("S.") and "Do NOT use this for" in routing
    assert "Returns: a layer" in routing
    assert len(routing) < len(full)


def test_do_sag_publishes_both_docstring_views():
    from trid3nt_server.workflows.telemac.templates.do_sag.do_sag import telemac_do_sag

    assert "Params:" in (telemac_do_sag.__doc__ or "")
    assert "Params:" not in telemac_do_sag.routing_doc
    assert len(telemac_do_sag.routing_doc) < 1400


def test_docstring_reports_bounds_units_and_labeled_defaults():
    doc = render_docstring(
        summary="S.", routing="R.",
        inputs=[Input(name="a", desc="the knob", default=2.0,
                      bounds=(1.0, 3.0), units="mg/L")],
        returns="a layer")
    assert "mg/L" in doc and "range 1-3" in doc
    assert "default 2.0 (the template's starting value, not a site measurement)" in doc


# --- late binding: a plan DESCRIBES, the interpreter SUBSTITUTES -------------- #


# --- the form gate's revision REACHES the run -------------------------------- #


# --- the completion tombstone: three ghost paths ----------------------------- #


# --- aux notes survive a later failure ---------------------------------------- #


# --- eager Data producer errors are typed too --------------------------------- #


class _DataDown(RuntimeError):
    error_code = "MESH_SOURCE_DOWN"

    def __init__(self):
        super().__init__("the mesh source is down")


# --- the ledger under concurrency --------------------------------------------- #


def test_the_store_cycle_is_locked_across_processes(tmp_path):
    """The flock + read-inside-the-lock rule, exercised by real parallel writers:
    every writer's document survives, because none writes a stale whole store."""
    import threading

    from trid3nt_server.store.cases import FileMCPClient

    client = FileMCPClient(base_dir=tmp_path)
    path = client._collection_path("db", "coll")

    def _writer(i):
        def _apply(store):
            store[f"doc{i}"] = {"_id": f"doc{i}"}
            return None, True
        client._cycle(path, _apply)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(FileMCPClient._read_store(path)) == sorted(
        f"doc{i}" for i in range(12))


# --- R3-2: a revision re-derives what depends on it -------------------------- #
# --- R3-3: a revision invalidates the data produced from the old values ------ #
async def stub_dem(**kwargs):
    _CALLS.append("stub_dem")
    return {"uri": f"s3://b/dem-{kwargs['res']}.tif", "dem": f"dem@{kwargs['res']}"}


# --- observation 6: a binding fault is a typed plan error -------------------- #
_Point = collections.namedtuple("_Point", "lon lat")


class _HostileTuple(tuple):
    """A tuple subclass whose constructor is NOT ``type(x)(iterable)``."""

    def __new__(cls, a, b):
        return super().__new__(cls, (a, b))


# --- R4-1: the leak guard never passes on an exhausted budget ---------------- #
def _deep_clean(depth):
    """A clean nested structure whose node count the scan budget can be set under."""
    node = {"leaf": 1.0}
    for i in range(depth):
        node = {"n": node, "i": i}
    return node


# --- the chart PAYLOAD is the surface, not the node's return dict ------------ #


# --- a context slot's declared SHAPE is checked at the front door ------------- #


# --- the run's own NOTES ------------------------------------------------------ #


# --- presentation is not declaration vocabulary ------------------------------- #

#: The words a picture is described with. A declaration that carried one would
#: be describing the render instead of the run, and would freeze it at authoring
#: time, where the reader who wants it different is not.
_PRESENTATION_WORDS = ("style", "ramp", "colormap", "colourmap", "legend",
                       "preset", "palette", "vmin", "vmax", "opacity")


def _presentation_named_by(cls) -> list[str]:
    names = [f.name for f in dataclasses.fields(cls)] if dataclasses.is_dataclass(cls) else []
    names += [n for n in dir(cls) if not n.startswith("_")]
    return sorted({n for n in names
                   if any(w in n.lower() for w in _PRESENTATION_WORDS)})


# --- a continuation opens only on the mesh the journal recorded --------------- #
