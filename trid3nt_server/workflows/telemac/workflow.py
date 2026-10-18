"""The TELEMAC workflow: fill, run, then read.

``fill`` sets slots, expands composites and binds producers; it is repeatable and decides nothing.
``run`` serializes the sheet, stages the run directory and hands it to the box; it is explicit and consequential.
``publish_outputs`` reads the primitives a template listed off the solved run and publishes each as asked."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from importlib import import_module
from typing import TYPE_CHECKING, Any, Callable, Collection, Mapping, Sequence

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import Origin
from trid3nt_contracts.execution import LayerURI

from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.inputs.slots import BED, DISCHARGE, DOMAIN, LEVEL, MESH
from trid3nt_server.workflows.runtime import (
    DeclarationError,
    RunResult,
    Workflow,
)
from trid3nt_server.workflows.runtime.workflow import EVENT_TIME, _names_untaken
from trid3nt_server.workflows.solver.staging import discard_run
from trid3nt_server.workflows.telemac.errors import TelemacError
from trid3nt_server.workflows.telemac.modules import wrapper_for
from trid3nt_server.workflows.runtime.module import (SlotRefused,
                                                     identify_on)
from trid3nt_server.workflows.runtime.module import MOMENT
from trid3nt_server.workflows.runtime.primitives import (
    OutputEmpty,
    Primitive,
    publish_reads,
    unanchored,
)
from trid3nt_server.workflows.telemac.modules.outputs import Solved
from trid3nt_server.workflows.runtime.review import _review, _seat_on
from trid3nt_server.workflows.runtime.sheet import (
    Keyword,
    SheetIncomplete,
    complete,
)
from trid3nt_server.workflows.runtime.sheet import fill as fill_slots
from trid3nt_server.workflows.runtime.sheet import fill_coupled
from trid3nt_server.workflows.runtime.sheet import run as run_sheet_
from trid3nt_server.workflows.telemac.modules.sheet import (
    CONTINUATION,
    TelemacSheet,
    stage as stage_sheet_,
)

if TYPE_CHECKING:
    from trid3nt_server.workflows.runtime.fill import Fill

logger = logging.getLogger("trid3nt_server.workflows.telemac.workflow")

__all__ = ["Measured", "Placed", "TelemacWorkflow",
           "fill_sheet", "publish_outputs", "run_bodies", "run_sheet", "stage_sheet", "stated"]

_TELEMAC = "trid3nt_server.workflows.telemac"

#: What a reader may open on any run, past the files the engine is required to
#: write: the mesh it solved on, the deck it read, the listing and the metrics.
_ALWAYS_READABLE = ("full_listing.log", "telemac_metrics.json")

#: What the workflow meshes a domain with, and the element it lays, where the
#: template declares no recipe of its own.
_MESHER, _MESH_KIND = "om2d", "unstructured_tri"

#: The dispatch a staged run goes to, by the path it is resolved at CALL time.
_DISPATCH = f"{_TELEMAC}.engine.solve_case"

# The kind is stated here once: every module of this engine solves on triangles, so a supplied mesh of another kind refuses at its row.
_SUPPLIED_MESH = Input(MESH, name="mesh", coercion=MappingProxyType({"kinds": (_MESH_KIND,)})).stated()

#: What the settle is called: what the deck and every later stage read it as.
_SETTLED = "settled"

#: The measurements the workflow takes of this question's world, by the KIND a composite asks for: the runner and the stage
#: it is taken at (before meshing, before the settle, as the settle, or against the settled run).
_MEASURES: Mapping[str, tuple[str, str, str]] = MappingProxyType({
    "footprint": ("trid3nt_server.inputs.structure.structure", "prep", "world"),
    "transect": ("trid3nt_server.inputs.structure.transect", "prep", "world"),
    "rating": (f"{_TELEMAC}.authoring.rating_curve.settle_outlet_rating",
               "author", "produce"),
    "harbour": (f"{_TELEMAC}.authoring.walked_boundary.settle_harbour",
                "author", "settle"),
    "dredge": (f"{_TELEMAC}.authoring.reference_surface.settle_dredge",
               "author", "derive")})

#: The mesher's own clean passes every domain gets first. They change the TOPOLOGY, so they run ahead of the bed and roles
#: (a renumbering after a primitive painted node values is refused). Smoothing is NOT among them: it folds elements beside
#: a rim locked at one spacing, and the boundary walk meets the folded pair's unpaired edges as a second rim.
def _clean_ops() -> list[Any]:
    from trid3nt_server.tools.mesh.tool import mesh_op

    return [mesh_op("delete_boundary_faces"),
            mesh_op("delete_faces_connected_to_one_face"),
            mesh_op("make_mesh_boundaries_traversable"),
            mesh_op("fix_mesh", delete_unused=True)]


@dataclass(frozen=True, slots=True)
class Placed:
    """A point the run checks against the accepted mesh, under ``name``.

    ``point`` names the run input holding it; an unstated one places nothing, and nothing read at it is composed."""

    name: str
    point: str
    #: What the marker published before the solve is called on the map.
    label: str = "Release point"
    #: Read the initial wet state off the run this one carries on from: a source
    #: has to enter water, and a continued run opens at another run's surface.
    continues: bool = False


@dataclass(frozen=True, slots=True)
class Measured:
    """A measurement the run takes against its own world, under ``name``.

    ``kind`` picks the runner; ``reads`` maps its arguments to run inputs by name; ``asked`` states the literals only this question can state."""

    name: str
    kind: str = ""
    reads: Mapping[str, Any] = field(default_factory=dict)
    asked: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _MEASURES:
            raise DeclarationError(
                f"{self.name}: there is no {self.kind!r} measurement; the "
                f"workflow takes {tuple(_MEASURES)}.")
        object.__setattr__(self, "reads", MappingProxyType(dict(self.reads)))
        object.__setattr__(self, "asked", MappingProxyType(dict(self.asked)))


def _names(value: Any) -> list[str]:
    """Every input name a ``reads`` mapping names, nested mappings included."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [name for item in value.values() for name in _names(item)]
    return []


async def _read_named(env: Any, value: Any) -> Any:
    """A ``reads`` mapping with every name replaced by what the run holds."""
    from trid3nt_server.workflows.runtime.fill import read

    if isinstance(value, str):
        return await read(env, value)
    if isinstance(value, Mapping):
        return {key: await _read_named(env, item) for key, item in value.items()}
    return value


def _painted(row: Mapping[str, Any]) -> list[Primitive]:
    """One table row -> the ONE layer the run publishes of it: the temporal layer where the row varies in time, the final frame where it does not.
    A still beside a time series would copy a frame the temporal layer already carries."""
    from trid3nt_server.workflows.runtime.primitives import field

    token, module, style = row["token"], row["module"], row.get("style")
    if row.get("varies"):
        return [field(token, t="every", module=module).animate(style=style)]
    return [field(token, t=-1, module=module).layer(style=style)]


@dataclass(frozen=True, slots=True)
class _Written:
    """One variable a result file carries, and the layer it is published as.
    ``spelling`` is the file's own name (empty for a row the result lacks); ``first`` names the file whose layer carries a repeated spelling;
    ``rowed`` whether a module row names it; ``withheld`` the module's mark on a slot its engine never writes."""

    primitive: Primitive
    file: str
    spelling: str
    rowed: bool
    first: str | None = None
    withheld: str = ""


def _written(run: Mapping[str, Any],
             solved: Callable[[str], Solved]) -> list[_Written]:
    """EVERY variable this run wrote -> the one layer each is published under.
    Module rows come first in table order, then every variable a result file carries that no row resolved; a spelling two files carry is ONE layer.
    A table decides how a variable is drawn, never whether; a slot marked never-written is carried with that mark and published by no layer."""
    from trid3nt_server.workflows.runtime.primitives import field

    written: list[_Written] = []
    claimed: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()

    def _claim(primitives: list[Primitive], file: str, spelling: str,
               rowed: bool) -> None:
        seen.add((file, spelling.upper()))
        first = claimed.get(spelling.upper())
        if first is not None:
            written.append(_Written(primitives[0], file, spelling, rowed, first))
            return
        claimed[spelling.upper()] = file
        written.extend(_Written(p, file, spelling, rowed) for p in primitives)

    rows = list(run.get("module_output") or ())
    for row in rows:
        read = solved(str(row["module"]))
        try:
            variable, _ = read.variable(str(row["token"]))
        except OutputEmpty:
            # A row the result does not carry claims no spelling and is skipped downstream.
            written.extend(_Written(p, read.result_file, "", True)
                           for p in _painted(row))
            continue
        _claim(_painted(row), read.result_file, variable.strip(), True)
    walked: set[str] = set()
    for module in dict.fromkeys([str(run["module"]),
                                 *(str(row["module"]) for row in rows)]):
        read = solved(module)
        if read.result_file in walked:
            continue
        walked.add(read.result_file)
        marked = {name.upper(): entry.cited
                  for entry in read.body.UNWRITTEN.values()
                  for name in (entry.spelling, entry.french) if name}
        for variable in read.result["varnames"]:
            spelling = str(variable).strip()
            if (read.result_file, spelling.upper()) in seen:
                continue
            if spelling.upper() in marked:
                seen.add((read.result_file, spelling.upper()))
                written.append(_Written(
                    field(spelling, t="every", module=module), read.result_file,
                    spelling, False, withheld=(
                        f"{read.body.MODULE} never writes it, by its own source "
                        f"- {marked[spelling.upper()]}")))
                continue
            _claim([field(spelling, t="every", module=module).animate()],
                   read.result_file, spelling, False)
    return written


def _account(written: Sequence[_Written], layers: Sequence[LayerURI],
             why: Mapping[Primitive, str]) -> None:
    """Journal what became of every variable a result file carries, read off the layers the publish SURFACED."""
    from trid3nt_server.render.formats import quantity_of
    from trid3nt_server.inputs.run_notes import journal_note

    landed = {layer.quantity for layer in layers}
    for entry in written:
        if not entry.spelling:
            continue
        where = f"{entry.file} wrote {entry.spelling!r}"
        if entry.withheld:
            journal_note(f"{where} and no layer carries it: {entry.withheld}")
        elif quantity_of(entry.spelling.lower()) not in landed:
            journal_note(f"{where} and no layer carries it: "
                         f"{why.get(entry.primitive.key, 'the publish surfaced none')}")
        elif entry.first is not None:
            journal_note(f"{where}, which {entry.first} wrote as well; the one "
                         f"layer of it is read off {entry.first}")
        elif not entry.rowed:
            journal_note(f"{where} and no module row names it; it is published "
                         "under the spelling the result file carries")


#: How long a stated value is printed before the doc names its shape: a whole tracer array crowds the keywords off the page.
_VALUE_CHARS = 48


def _stated_value(value: Any) -> str:
    """One asserted value, as the docstring prints it."""
    if value is None:
        return "nothing (the engine's default stands)"
    if isinstance(value, (list, tuple)):
        text = ", ".join(_stated_value(item) for item in value)
        return f"[{text}]" if len(text) <= _VALUE_CHARS else f"{len(value)} values"
    return f"{value:g}" if isinstance(value, float) else str(value)


async def publish_outputs(*, run: Mapping[str, Any], outputs: Sequence[Primitive],
                          captions: Mapping[str, str],
                          params: Mapping[str, Any],
                          anchors: Sequence[Mapping[str, Any]] = ()
                          ) -> LayerURI:
    """Read what the run wrote and publish every variable, ONE layer each (temporal where the row varies in time, else the final frame).
    Module rows style the layer; a variable no row names goes under the file's own spelling and one reaching no layer is journalled by name.
    The engine's half: how a module's result is opened, the table of what it wrote, and the run's record."""

    def _table(solved: Callable[[str], Solved]) -> tuple[list[Primitive], Any]:
        written = _written(run, solved)
        return ([entry.primitive for entry in written
                 if entry.first is None and not entry.withheld],
                lambda layers, why: _account(written, layers, why))

    return await publish_reads(
        engine="telemac", run=run, outputs=outputs, captions=captions,
        params=params, anchors=anchors,
        solved=lambda module: Solved(run, wrapper_for(module)), table=_table,
        record=lambda solved, name: _record(solved, name=name))


def _record(solved: Solved, *, name: str) -> LayerURI:
    """The run's own record: the mesh every published group rides; it binds no group, so it is DRAWN rather than measured."""
    from trid3nt_server.store import objects as storage

    return LayerURI(
        layer_id=f"telemac-{solved.run_id}", name=name, layer_type="mesh",
        uri=f"s3://{storage.runs_bucket()}/{solved.run_id}/{solved.display_file}",
        style={"kind": "reference"}, bbox=solved.bbox,
        crs_authid=f"EPSG:{solved.utm_epsg}",
        reference_time=solved.run.get("reference_time"))


def stated(*, steering: type, keywords: Mapping[str, Any]) -> dict[str, Any]:
    """The run's own keyword values by identifier, resolved ONCE before any stage.
    The template's assertions under the floor that overrides them; a composite has no number of its own and is absent."""
    out = {name: value for name, value in steering.ASSERTED.items()
           if value is not None and name in steering.MODULE_INPUT}
    return {**out, **_floor(steering, keywords)[0]}


def _floor(steering: type, keywords: Mapping[str, Any]
           ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The raw keyword floor routed onto this run's bodies -> the carrier's values and each coupled module's, by identifier."""
    if keywords and not isinstance(keywords, Mapping):
        raise SlotRefused(
            f"keywords takes a mapping of the engine's own keyword names to "
            f"values, e.g. {{\"LAW OF BOTTOM FRICTION\": 4}}; got "
            f"{type(keywords).__name__}.")
    bodies = run_bodies(steering)
    own: dict[str, Any] = {}
    coupled: dict[str, dict[str, Any]] = {}
    for name, value in (keywords or {}).items():
        body, identifier = identify_on(bodies, name)
        if body is steering:
            own[identifier] = value
        else:
            coupled.setdefault(body.MODULE, {})[identifier] = value
    return own, coupled


def run_bodies(steering: type) -> list[type]:
    """This run's bodies in DECK ORDER: the carrier, then each module its coupling statement names.
    Read off the declaration, so the names a floor may qualify are known before any fill."""
    from trid3nt_server.workflows.telemac.modules import wrapper_for

    coupled = steering.ASSERTED.get("coupling") or ()
    return [steering] + [wrapper_for(body["module"]) for body in coupled
                         if isinstance(body, Mapping) and body.get("module")]


async def fill_sheet(*, steering: type, produced: Mapping[str, Any],
                     params: Mapping[str, Any],
                     settled: Mapping[str, Any] | None = None,
                     workflow: str, title: str, keywords: Mapping[str, Any],
                     input_mode: str | None, spent: Sequence[str] = (),
                     seat: Callable[[Mapping[str, Any]], None] | None = None,
                     write: Callable[[TelemacSheet], Any] | None = None,
                     user: Sequence[str] = ()) -> TelemacSheet:
    """Set the body's slots against what the run measured -> the sheet, HELD.

    The raw ``keywords`` floor is filled last and therefore beats a template
    value. ``seat`` takes the param values the person proceeded on; ``write``
    writes and checks the deck of every sheet a card is drawn from; ``user``
    names the params the call stated."""
    stated, coupled = _floor(steering, keywords)

    async def fill(values: Mapping[str, Any], edits: Mapping[str, Any]) -> TelemacSheet:
        # A composite may read fetched data at the fill, so the fill runs off the loop.
        sheet = await asyncio.to_thread(
            fill_slots, steering, template=workflow, produced=dict(produced),
            params=dict(values), settled=settled, **{**stated, **edits})
        # After the fill: the coupled decks are what the carrier's coupling composite wrote into the sheet's files.
        sheet = fill_coupled(sheet, coupled) if coupled else sheet
        if write is not None:
            # The deck is written and its staged directory checked before the card is drawn, so a proceed is followed by the solve alone.
            await write(sheet)
        return sheet

    sheet = await _review(fill, params, steering=steering, workflow=workflow,
                          title=title, input_mode=input_mode, spent=spent,
                          seat=seat, user=user)
    logger.info("telemac sheet filled: %s states %d keywords, %d open "
                "(%d required)", sheet.body.__name__, len(sheet.filled),
                len(sheet.open()), len(sheet.required()))
    return sheet


async def stage_sheet(*, sheet: TelemacSheet, run_tag: str, settled: Mapping[str, Any],
                      results: Sequence[str], steering: str,
                      prefix: str) -> dict[str, Any]:
    """A complete sheet, serialized and staged with the directory checked -> the staged run.
    The handle names every variable the deck asked for; what the run wrote is decided here, where the coupled bodies and declared tracers are in hand."""
    coupled = dict(sheet.resolved()).get("COUPLING WITH")
    outputs = [*results, *(row["dest"] for row in settled["mesh_inputs"]),
               steering, *_ALWAYS_READABLE, *sorted(sheet.files)]
    return await stage_sheet_(
        sheet, run_tag=run_tag, mesh_inputs=settled["mesh_inputs"],
        outputs=outputs, results=list(results), prefix=prefix,
        server_facts=settled["server_facts"], steering=steering,
        coupling=None if not coupled else str(coupled).split(";")[0].lower(),
        continue_from=settled.get("continue_from"))


async def run_sheet(*, sheet: TelemacSheet, staged: Mapping[str, Any],
                    settled: Mapping[str, Any], dispatch: str,
                    display: str = "",
                    moment: str | None = None) -> dict[str, Any]:
    """A staged run handed to the box -> the run handle.
    Its ``reference_time`` is the instant every published time counts from: the stated ``moment``, else the solve's own start."""
    module, _, attribute = str(dispatch).rpartition(".")
    to_the_box = getattr(import_module(module), attribute)
    handle = await run_sheet_(staged, dispatch=to_the_box)
    return {**settled, **handle, "module": sheet.module,
            "reference_time": moment or handle.get("started_at"),
            "display_basename": display or None,
            "module_output": [{"token": token, "module": module,
                               "name": row.name, "unit": row.unit,
                               "style": row.style, "varies": row.varies,
                               "has_edge": bool(row.has_edge),
                               "injected": bool(row.injected)}
                              for token, module, row in sheet.published()],
            "tracer_names": dict(sheet.resolved()).get("NAMES OF TRACERS")}


class TelemacWorkflow(Workflow):
    """TELEMAC: the template module, read by its own names.
    A template DECLARES STEERING (and its coupling), INPUTS, OUTPUTS, CAPTIONS, DOC and its run files; this builds every stage off them.
    Nothing here restates a template, and a name a template does not state takes the default beside it."""

    engine = "telemac"
    solve_step = "solve"
    moment = EVENT_TIME.name
    def __init__(self, *, coerce: Sequence[Any] = (), **declared: Any) -> None:
        super().__init__(coerce=(*coerce, event_time()), **declared)

    def seated(self) -> tuple[Input, ...]:
        """What the mesh ask names of the mesher's own inputs, the moment the run
        opens at, what the types of its rows state, then the mesh a caller hands in."""
        from trid3nt_server.tools.mesh import recipe as asks

        recipe = self._states("MESH", None)
        # The ask this workflow builds where a template declares none names the edge alone.
        named = ({asks.RESOLUTION.name} if recipe is None
                 else set(asks.input_names(recipe)))
        return (tuple(row for row in asks.INPUTS if row.name in named)
                + (EVENT_TIME,) + super().seated() + (_SUPPLIED_MESH,))

    def input(self, name: str) -> Any:
        """The template's and the engine's own inputs first, then the keyword a name
        spells on whichever of this run's bodies carries it."""
        declared = next((row for row in self.inputs if row.name == name), None)
        if declared is not None:
            return declared
        body, identifier = identify_on(run_bodies(self.steering), name)
        return Keyword(body, identifier, body is self.steering)

    @property
    def steering(self) -> type:
        """The STEERING body: the module wrapper, the slots this question asserts
        and the coupling it carries."""
        return self.template.STEERING

    def input_schema(self) -> tuple[Any, str] | None:
        """The module input dictionary this deck's STEERING drives: where this
        template's values read their schema off."""
        module = getattr(getattr(self.template, "STEERING", None), "MODULE", "")
        if not module:
            return None
        from trid3nt_server.workflows.telemac.modules.module import module_input_dir

        return (module_input_dir(), module)

    def slot_units(self) -> Mapping[Any, str]:
        """What the two series slots convert to: the unit of the PRESCRIBED list
        each one's value is written into, which the transform fixes."""
        from .modules.telemac2d import PRESCRIBED_UNITS

        return {DISCHARGE: PRESCRIBED_UNITS["flowrate"],
                LEVEL: PRESCRIBED_UNITS["elevation"]}

    def published_units(self) -> Mapping[str, str]:
        """The unit each variable this run publishes is written in, by the name the result file carries.
        Three statements, all the deck's own: the module's variable table, the rows every coupled module appends,
        and the 32-character tracer text the deck writes its tracers as."""
        from .modules import wrapper_for
        from .modules.sheet import tracer_text

        published = {row.name: row.unit
                     for row in self.steering.MODULE_OUTPUT.values()}
        for body in getattr(self.steering, "coupling", ()) or ():
            appends = wrapper_for(body["module"]).APPENDS
            for row in (list(appends(body)) if appends is not None else []):
                published[row.name] = row.unit
        for declared in getattr(self.steering, "NAMES_OF_TRACERS", ()) or ():
            name, unit = tracer_text(declared)
            published[name] = unit
        return published

    def run_window_s(self, keywords: Mapping[str, Any]) -> float | None:
        """How long this run's solve covers, as THIS module spells it, under the floor that may have moved it.
        A matched series source filters on the number the deck will write."""
        return self.steering.seconds(
            stated(steering=self.steering, keywords=keywords))

    def check(self) -> None:
        """Refuse at import what this template cannot run on: a domain, one bed,
        the deck's own clock, a function for every measurement it asks for and
        a caption for every output it lists."""
        slots = self._slots()
        self._clock()
        if DISCHARGE in slots:
            self._asserted(None, "LAW_OF_BOTTOM_FRICTION")
            self._asserted(None, "FRICTION_COEFFICIENT")
        for ask in self._declared(Measured):
            _signature(_MEASURES[ask.kind][0])
        self._outputs()

    async def launch(self, state: Fill) -> RunResult:
        """The run, in order: the world, the mesh and its files, what the run places and measures, the settle, the sheet written and checked under its review, the solve, the outputs.
        Every stage writes into the run's one mapping and reads off it by plain name."""
        from trid3nt_server.tools.mesh.step import build_declared_mesh, keep_mesh
        from trid3nt_server.render.pipeline_emitter import (begin_substeps,
                                                            current_emitter,
                                                            substep)
        from trid3nt_server.workflows.runtime.fill import (_load, _taken, call,
                                                           production, read)
        from .authoring.mesh_files import telemac_mesh_files
        from .authoring.opening import open_channel, open_water
        from .authoring.release_point import settle_release

        env = production(state)
        run = env.run
        slots = self._slots()
        domain = slots[DOMAIN]
        level, inflow = slots.get(LEVEL, ""), slots.get(DISCHARGE, "")
        keywords = dict(state.keywords)
        previous = _previous(self.steering, keywords)
        recipe = self._states("MESH", None)
        asks = self._declared(Measured)
        placed = self._declared(Placed)
        emitter = current_emitter()
        begin_substeps(emitter, 5 + ("mesh" not in run) + len(asks) + len(placed)
                       + bool(inflow))

        async def given(value: Any) -> Any:
            return await read(env, value) if isinstance(value, str) else value

        async def stage(label: str, fn: Any, /, **kwargs: Any) -> Any:
            async with substep(emitter, label):
                run[label] = await call(
                    fn if callable(fn) else _load(fn), kwargs, label)
            return run[label]

        # What nobody stated - a measurement of no value, a point nobody placed - and the op set on it or the read at or along it are not composed either.
        untaken: set[str] = set()

        async def measure(when: str) -> None:
            for ask in asks:
                runner, _stage, taken = _MEASURES[ask.kind]
                if taken == when:
                    reads = await _read_named(env, dict(ask.reads))
                    if "value" in reads and reads["value"] is None:
                        run[ask.name] = None
                        untaken.add(ask.name)
                        continue
                    world = await self._world(env, domain, _signature(runner))
                    await stage(_SETTLED if taken == "settle" else ask.name,
                                runner, **world, **reads, **ask.asked)

        run.update(stated(steering=self.steering, keywords=keywords))
        await measure("world")
        mesh = recipe if recipe is not None else self._mesh(domain, slots)
        # A supplied mesh is already the run's mesh: its row's ingestion wrote the record the build would have.
        if "mesh" not in run:
            await stage("mesh", build_declared_mesh,
                        mesh=await self._recipe(env, mesh, untaken),
                        name=await read(env, self._states("MESH_ON", "") or domain),
                        input_mode=env.input_mode,
                        fresh=bool(state.carried.get("restart_clean")))
        else:
            self._say_what_no_build_read(env, mesh)
        # The files this engine asks the accepted mesh for are written from it before anything reads one: its boundary numbering says which face carries what.
        await stage("mesh_files", telemac_mesh_files, mesh=run["mesh"])
        await keep_mesh(run["mesh"])
        if inflow:
            # THE OPEN-CHANNEL ADDITION: whether a run CARRIES a discharge is a run-time fact, decided off the carrier handed in; an absent one authors no channel.
            await stage("channel", open_channel, mesh=run["mesh"],
                        files=run["mesh_files"], carrier=await read(env, inflow),
                        stage=await read(env, level) if level else None,
                        friction_law=self._asserted(run, "LAW_OF_BOTTOM_FRICTION"),
                        friction_coefficient=self._asserted(
                            run, "FRICTION_COEFFICIENT"))
        for mark in placed:
            held = await given(mark.point)
            if held is None:
                run[mark.name] = None
                untaken.add(mark.name)
                continue
            await stage(mark.name, settle_release, point=held,
                        mesh=run["mesh"], domain=await read(env, domain),
                        label=mark.label,
                        **({"continue_from": previous} if mark.continues else {}))
        await measure("produce")
        settles = self._settled_by()
        if settles:
            await measure("settle")
        else:
            await stage(_SETTLED, open_water, **await self._opening(
                env, domain, level, inflow), continue_from=previous)
        await measure("derive")
        # A row asked near a point this run placed is produced here, once that point is a node of the mesh; the fill holds every other row,
        # so these two loops fetch nothing else.
        for name in self._fills():
            await read(env, name)
        if run.get(EVENT_TIME.name):
            # The deck's clock is UTC, so a module that reads the sun needs where on the earth the run is.
            lon, lat = (await read(env, domain)).centroid
            run[MOMENT] = {"at": datetime.fromisoformat(run[EVENT_TIME.name]),
                           "lon": lon, "lat": lat}
        declared = {row.name for row in self.inputs if not row.is_row}
        listed = tuple(self._states("RESULTS", ()))
        staged: dict[str, Any] = {}
        # One tag for the run: every card edit re-stages into the same directory and prefix.
        run_tag = new_ulid()
        prefix = self._states("PREFIX", "telemac")

        async def write(sheet: TelemacSheet) -> None:
            # A card edit fills the sheet again, so the staged run is the one the last fill wrote.
            staged.clear()
            staged.update(await stage_sheet(
                sheet=sheet, run_tag=run_tag, settled=run[_SETTLED],
                results=list(listed or (self._result(),)),
                steering=(self._states("STEERING_FILE", "") or f"{self.steering.MODULE}_{self.name}.cas"),
                prefix=prefix))

        try:
            await stage("sheet", fill_sheet, steering=self.steering,
                        # A module fills keywords from a row by the NAME of its type, so each type's row is held under that name beside its own.
                        produced={**run, **{slot.name: run[name] for slot, name
                                            in slots.items() if name in run}},
                        params={name: run[name] for name in declared},
                        settled=run[_SETTLED].get("keywords") or {},
                        workflow=self.name,
                        title=self._states("REVIEW_TITLE", ""), keywords=keywords,
                        input_mode=env.input_mode,
                        spent=sorted(mark.name for mark in placed),
                        seat=_seat_on(env), write=write,
                        user=[name for name, verdict in env.inputs.items()
                              if verdict.origin is Origin.USER])
        except BaseException:
            # A run that does not reach the box leaves no staged copy behind.
            await asyncio.to_thread(discard_run, run_tag, prefix,
                                    stem=self.engine)
            raise
        solved = await stage(
            "solve", run_sheet, sheet=run["sheet"], staged=staged,
            settled=run[_SETTLED], dispatch=_DISPATCH,
            display=self._states("DISPLAY_FILE", ""),
            moment=run.get(EVENT_TIME.name))
        await keep_mesh(run["mesh"], solved.get("run_id"))
        outputs, captions = self._outputs()
        outputs = [p for p in outputs
                   if not (_names_untaken(p.along, untaken) or _names_untaken(p.at, untaken))]
        # A primitive's point, line and band name run inputs; each is read here and rejoins its primitive at publish.
        anchors = [{"at": await given(p.at), "along": await given(p.along),
                    "within": await given(p.within)} for p in outputs]
        value = await stage(
            "outputs", publish_outputs, run=run["solve"],
            outputs=[unanchored(p) for p in outputs], captions=captions,
            anchors=anchors, params={name: run[name] for name in declared})
        return RunResult(value=value, results=dict(run))

    def _declared(self, kind: type) -> tuple[Any, ...]:
        """Every placement or measurement the template module declares, in declared order."""
        return tuple(value for value in vars(self.template).values()
                     if isinstance(value, kind))

    def _fills(self) -> list[str]:
        """The declared rows a module of this run fills keywords from itself."""
        wanted = set(self.steering.FILLED_BY)
        for body in self.steering.ASSERTED.get("coupling") or ():
            if isinstance(body, Mapping):
                wanted |= set(body.get("filled_by", ()))
        return [name for slot, name in self._slots().items()
                if slot.name in wanted]

    def judged(self, state: Fill) -> dict[str, Any]:
        """The session rule, then what this deck refuses before any mesh is built: a previous computation on a template whose settle
        opens on none, and a required file or the cadence neither the deck nor the call states."""
        from trid3nt_server.workflows.runtime.fill import MISSING, REJECTED, Verdict

        refused = super().judged(state)
        keywords = dict(state.keywords)
        settles = self._settled_by()
        if settles and _previous(self.steering, keywords):
            refused[CONTINUATION] = Verdict(
                REJECTED, code="TELEMAC_CONTINUATION_REFUSED",
                reason=f"{self.name} is settled by the {settles[0].kind} it measures, "
                "which opens on no previous computation: this template does not "
                "carry a run on.")
        try:
            complete(self.steering, stated(steering=self.steering, keywords=keywords))
        except SheetIncomplete as exc:
            refused[self.steering.MODULE] = Verdict(
                MISSING, reason=str(exc), code="TELEMAC_SHEET_INCOMPLETE")
        return refused

    def unnamed(self) -> tuple[str, ...]:
        """Every input the template names as a plain string - in a placement, a
        measurement, a row, an output, the mesh recipe or a composite - that no
        param, row, output or keyword of this run is called."""
        known = ({row.name for row in self.inputs} | self._marks()
                 | {"mesh", "mesh_files", "channel", _SETTLED, "line"}
                 | {name for body in run_bodies(self.steering)
                    for name in body.MODULE_INPUT})
        return tuple(dict.fromkeys(name for name in self._named()
                                   if isinstance(name, str) and name not in known))

    def named_rows(self, held: Collection[str] = ()) -> tuple[str, ...]:
        """Every declared row a stage of this run reads WITHOUT the mesh: the domain, what the template names, the mesh ask's rows where a mesh
        is still to be built, the level and the inflow, the line a measurement is taken against, and the rows a module fills from.
        One asked near a point the run places is the settle's: that point is a node of the mesh."""
        # A seated row is produced only when stated: a composite names the mesh stage's output by the same word.
        rows = {row.name: row for row in self.inputs
                if row.is_row and row is not _SUPPLIED_MESH}
        marks = tuple(self._marks())
        slots = self._slots()
        read = [slots[DOMAIN], *self._named(mesh="mesh" not in held), slots.get(LEVEL), slots.get(DISCHARGE),
                *("line" for ask in self._declared(Measured) if "line" in _signature(_MEASURES[ask.kind][0])),
                *self._fills()]
        return tuple(dict.fromkeys(
            name for name in read if isinstance(name, str)
            and name in rows and rows[name].coercion.get("near") not in marks))

    def _settled_by(self) -> list[Any]:
        """The measurements this template is SETTLED by, in declared order; none where the open water settles it."""
        return [ask for ask in self._declared(Measured) if _MEASURES[ask.kind][2] == "settle"]

    def _marks(self) -> set[str]:
        return ({mark.name for mark in self._declared(Placed)}
                | {ask.name for ask in self._declared(Measured)})

    def _say_what_no_build_read(self, env: Any, recipe: Any) -> None:
        """One note naming each input the caller stated that only the mesh build reads: a supplied mesh leaves it unused."""
        from trid3nt_server.inputs.run_notes import journal_note
        from trid3nt_server.tools.mesh.recipe import input_names

        slots = self._slots()
        # The settle reads the designating depth off the run as well as the build.
        elsewhere = {*self._named(mesh=False), *self._fills(), slots[DOMAIN],
                     slots.get(LEVEL), slots.get(DISCHARGE),
                     "open_depth_threshold_m"}
        stated = ({name for name, verdict in env.inputs.items()
                   if verdict.origin is Origin.USER}
                  | set(env.supplied) | set(env.picks))
        unused = [name for name in dict.fromkeys(input_names(recipe))
                  if name in stated and name not in elsewhere]
        if unused:
            journal_note(
                "a mesh was supplied, so this run built none and "
                f"{', '.join(unused)} went unused: only the mesh build reads "
                f"{'it' if len(unused) == 1 else 'them'}.")

    def _named(self, mesh: bool = True) -> list[Any]:
        from trid3nt_server.tools.mesh.recipe import input_names

        named = [mark.point for mark in self._declared(Placed)]
        named += [name for ask in self._declared(Measured)
                  for name in _names(dict(ask.reads))]
        named += [row.coercion.get("near") for row in self.inputs]
        named += [value for p in self._states("OUTPUTS", ())
                  for value in (p.at, p.along, p.within)]
        if mesh:
            slots = self._slots()
            recipe = self._states("MESH", None)
            named += input_names(recipe if recipe is not None else self._mesh(slots[DOMAIN], slots))
        named += self.steering.named()
        return named

    def _mesh(self, domain: str, slots: Mapping[str, str]) -> Any:
        """The mesh this workflow asks for where the template declares none."""
        from trid3nt_server.tools.mesh.tool import mesh_op, tool

        return tool.build_mesh(
            mesher=_MESHER, kind=_MESH_KIND, extent=domain,
            resolution_m="mesh_resolution_m",
            # THE RIM IS THE ASK'S TO SIZE: no library sizing function measures the domain's outline, so an undeclared rim
            # comes back an order of magnitude past the size word; granularity is the user's.
            ops=[mesh_op("set_rim_size"), *_clean_ops(),
                 mesh_op("set_bed", source=slots[BED]),
                 # The runs ride on the DOMAIN: the producer measured them where it cut the polygon; a drawn outline carries the canvas's.
                 mesh_op("set_boundary_roles", runs=domain)])

    async def _opening(self, env: Any, domain: str, level: str,
                       inflow: str) -> dict[str, Any]:
        """What the open water is settled from, read off this run."""
        from trid3nt_server.workflows.runtime.fill import read

        run = env.run
        declared = {row.name for row in self.inputs}
        return {
            "mesh": run["mesh"], "files": run["mesh_files"],
            # WHAT THE WATER STANDS AT: the channel's own measurement, else the level slot, else nothing; a bed stated as a depth needs nothing.
            "level": (run["channel"] if inflow
                      else await read(env, level) if level else None),
            "geometry": self._file("GEOMETRY_FILE", "geometry.slf"),
            "boundary": self._file("BOUNDARY_CONDITIONS_FILE", "boundary.cli"),
            "result": self._result(),
            "mesh_resolution_m": run["mesh_resolution_m"],
            # THE CLOCK IS THE MODULE'S: the settle reads the seconds the deck was written for, not a param restating them.
            "duration_s": self._clock(run),
            # A deck that STATES the depth an open edge is designated at prescribes the sea state across it, so a mesh where nothing reaches it is sealed.
            **({"deck": self.name,
                "open_depth_threshold_m": run["open_depth_threshold_m"]}
               if "open_depth_threshold_m" in declared else {}),
            **({"name": run["name"]} if "name" in declared else {})}

    def _result(self) -> str:
        """The deck's own RESULTS statement, else the first file this template
        says the run writes: a 3D deck names a 3D and a 2D result rather than
        one RESULTS FILE."""
        listed = tuple(self._states("RESULTS", ()))
        return self._file(self.steering.RESULT_KEYWORD,
                          listed[0] if listed else "results.slf")

    async def _world(self, env: Any, domain: str,
                     takes: frozenset[str]) -> dict[str, Any]:
        """What a measurement is taken against, read off this run, cut to what
        its function takes."""
        from trid3nt_server.workflows.runtime.fill import read

        run = env.run
        world: dict[str, Any] = {}
        for name in ("mesh", "files", "line", "domain", "settled"):
            if name not in takes:
                continue
            held = {"files": "mesh_files", "domain": domain}.get(name, name)
            world[name] = (run.get(held) if name in ("mesh", "files", "settled")
                           else await read(env, held))
        if "friction_law" in takes:
            world["friction_law"] = self._asserted(run, "LAW_OF_BOTTOM_FRICTION")
        return world

    def _clock(self, run: Mapping[str, Any] | None = None) -> Any:
        """How long the settle opens this run's water for, as the module spells it.
        One keyword where the module names the window, the step and count where it names those; the deck must state each (refused at import).
        A module that does not march in time spells none."""
        spelled = [self._asserted(run, keyword) for keyword in self.steering.CLOCK]
        return spelled[0] if len(spelled) == 1 else spelled or None

    def _asserted(self, run: Mapping[str, Any] | None, keyword: str) -> Any:
        """One value the DECK itself states, read at RUN time off the floor.
        The floor may move the number before the deck writes it, so the stage reads it rather than being built from it; the deck must state it (refused at import)."""
        if self.steering.ASSERTED.get(keyword) is None:
            raise DeclarationError(
                f"the workflow settles this question's run on the {keyword} its "
                f"deck states, and this deck states none.")
        return None if run is None else run[keyword]

    def _file(self, keyword: str, fallback: str) -> str:
        """One file the deck itself names, read off the body that names it.
        The deck's GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements ARE the run directory's names; restating them would let the two drift."""
        named = self.steering.ASSERTED.get(keyword)
        return str(named) if isinstance(named, str) and named else fallback

    def sheet_doc(self) -> str:
        """The ENGINE SURFACE line of this template's docstring.

        Read off the declaration itself, never claimed by prose."""
        body = self.steering
        asserts = set(body.ASSERTED)
        touched = sorted({body.MODULE_INPUT[name].rubrique[0] for name in asserts
                          if name in body.MODULE_INPUT
                          and body.MODULE_INPUT[name].rubrique})
        open_required = sorted(slot.keyword for name, slot in body.MODULE_INPUT.items()
                               if slot.is_required and name not in asserts)
        return (
            f"Sheet: {body.MODULE}, whose dictionary has {len(body.MODULE_INPUT)} "
            f"keywords. This template states {len(asserts)} of them, under "
            f"{', '.join(touched)}. Open mandatory slots: "
            f"{', '.join(open_required) if open_required else 'none'}. Every "
            f"other keyword is the engine's own default and is set on the call - "
            f"keywords={{\"LAW OF BOTTOM FRICTION\": 4}} - after "
            f"describe_keywords(module=\"{body.MODULE}\", query=...) names it "
            f"with its help, its choices, its unit, its range and that default. The solve is "
            f"partitioned across PARALLEL PROCESSORS, stated the same way; a "
            f"count past this box's cores is refused.\n"
            f"Stated by this deck (override any of them by name): "
            f"{self._stated_keywords()}.")

    def _stated_keywords(self) -> str:
        """The deck's own opinions, keyword by keyword, in dictionary order.
        Generated off ASSERTED so the doc cannot claim an opinion the deck does not hold; a value the run MEASURES is named as measured."""
        body = self.steering
        rows = []
        for name, slot in body.MODULE_INPUT.items():
            if name not in body.ASSERTED:
                continue
            rows.append(f"{slot.keyword} = {_stated_value(body.ASSERTED[name])}")
        return "; ".join(rows) if rows else "nothing"


def _previous(steering: type, keywords: dict[str, Any]) -> str | None:
    """The module's previous-computation file, taken off the keyword floor.
    The settle stages it and the sheet names the staged copy, so the floor never writes the caller's uri into the deck."""
    for name in list(keywords):
        body, identifier = identify_on(run_bodies(steering), name)
        if body is steering and identifier == CONTINUATION:
            return str(keywords.pop(name))
    return None


def _signature(runner: str) -> frozenset[str]:
    """The keyword names one measurement runner takes, by ONE attribute lookup.
    A name the module does not carry refuses here rather than at the call."""
    from inspect import signature

    module, _, name = runner.rpartition(".")
    op = getattr(import_module(module), name, None)
    if not callable(op):
        raise DeclarationError(
            f"{module} carries no {name!r} that takes a measurement; a "
            "measurement names the runner that takes it.")
    return frozenset(signature(op).parameters)
