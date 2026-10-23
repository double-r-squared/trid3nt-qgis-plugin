"""The MODFLOW workflow: a template over one module's sheet, meshed, reviewed, solved and read.

``launch`` runs one order - the mesh, the sheet written, validated and staged under its
one review, the solve, the outputs. The publish is the runtime's; this hands it what is
the engine's own: how a solved run is opened, the module table of what its output
control saved, and the run's record."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime
from functools import partial
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Collection, Mapping, Sequence

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import Origin
from trid3nt_contracts.execution import LayerURI

from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.inputs.slots import BED, DOMAIN, MESH
from trid3nt_server.workflows.runtime import DeclarationError, RunResult, Workflow
from trid3nt_server.workflows.runtime.module import (MOMENT, SlotRefused,
                                                     identify_on,
                                                     keyword_floor)
from trid3nt_server.workflows.runtime.primitives import (Primitive, field, publish_reads,
                                                         unanchored)
from trid3nt_server.workflows.runtime.review import _seat_on, fill_sheet
from trid3nt_server.workflows.runtime.sheet import Filled, Keyword, Sheet
from trid3nt_server.workflows.runtime.sheet import fill_coupled, reference_time
from trid3nt_server.workflows.runtime.sheet import run as run_sheet_
from trid3nt_server.workflows.runtime.workflow import EVENT_TIME
from trid3nt_server.workflows.solver.staging import discard_run

from .authoring import DISV_FILE, MFSIM_FILE
from .modules import COUPLED_BODIES, wrapper_for
from .modules.outputs import Solved, filerecord, group_stem
from .modules.ugrid import display_file

if TYPE_CHECKING:
    from trid3nt_server.workflows.runtime.fill import Fill

logger = logging.getLogger("trid3nt_server.workflows.modflow.workflow")

__all__ = ["CONCENTRATION_UNITS", "ModflowWorkflow", "fill_sheet", "publish_outputs", "run_bodies",
           "run_sheet", "stage_sheet"]

#: The dispatch a staged run goes to, by the path it is resolved at CALL time.
_DISPATCH = "trid3nt_server.workflows.modflow.engine.solve_case"

#: The metrics file the worker writes, read back off the run prefix.
_METRICS = "modflow_metrics.json"

#: The simulation's own listing, which mf6 writes beside the model's.
_SIM_LISTING = "mfsim.lst"

_FRAME, = BED.states

#: mf6 carries no unit system, so a run carrying a concentration states the unit it is counted in.
CONCENTRATION_UNITS = Input(
    name="concentration_units",
    desc="The unit every concentration this run carries is counted in - the "
         "unit its source and starting concentrations are stated in, such as "
         "mg/L. A measured concentration is converted into it, and a reading "
         "no conversion reaches refuses")

# The kinds the FloPy meshers lay; a supplied mesh of another kind refuses at its row.
_SUPPLIED_MESH = Input(MESH, name="mesh", coercion=MappingProxyType(
    {"kinds": ("unstructured_tri", "voronoi", "quadtree")})).stated()


def _floor(steering: type, keywords: Mapping[str, Any]
           ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The raw keyword floor routed onto this run's bodies -> the carrier's values and each coupled module's, by identifier."""
    names = list(keywords) if isinstance(keywords, Mapping) else []
    return keyword_floor(run_bodies(steering, names), keywords)


def run_bodies(steering: type, names: Sequence[str] = ()) -> list[type]:
    """This run's bodies in SIMULATION ORDER: the carrier, then each module its Transport/Energy states, then each body a qualified name of this run names.
    A qualifier may add only a body the carrier couples (gwt/gwe); an unknown module word is identify_on's to refuse."""
    bodies = [steering, *(wrapper_for(module)
                          for name, module in COUPLED_BODIES.items()
                          if steering.ASSERTED.get(name) is not None)]
    for name in names:
        module = str(name).partition(":")[0].strip().lower()
        if ":" in str(name) and module in COUPLED_BODIES.values() \
                and all(body.MODULE != module for body in bodies):
            bodies.append(wrapper_for(module))
    return bodies


#: Which coupling composite on the carrier states each coupled body, by module word.
_COMPOSITE_FOR = {module: name for name, module in COUPLED_BODIES.items()}


def _compose_coupled(sheet: Sheet, stated: Mapping[str, Mapping[str, Any]],
                     held: Mapping[str, Any]) -> Sheet:
    """Run-stated coupled keywords -> the sheet: composed through the carrier's own coupling composite (the slot checks, the stress expansion and the simulation's-own refusals a template statement takes) and merged onto that body's deck - a body this run names and no template stated is created here."""
    for module, slots in stated.items():
        _, named = sheet.body.COMPOSITES[_COMPOSITE_FOR[module]].apply(
            {"slots": slots}, held)
        if any(content["module"] == module for content in sheet.coupled):
            sheet = fill_coupled(sheet, {module: named[module]["slots"]})
        else:
            body = {**named[module], "stated": sorted(named[module]["slots"])}
            files = {**sheet.files, module: body}
            sheet = replace(sheet, files=MappingProxyType(files))
    return sheet


fill_sheet = partial(fill_sheet, engine="modflow", floor=_floor, compose=_compose_coupled)


def _results(sheet: Sheet) -> list[str]:
    """The files every output control of this run names: what the worker must find written."""
    found: list[str] = []
    for source in (sheet.filled, *(body.get("slots") or {}
                                   for body in sheet.coupled)):
        for identifier, row in source.items():
            if identifier.startswith("OC_") and identifier.endswith("_FILERECORD"):
                value = row.value if isinstance(row, Filled) else row
                if (name := filerecord(value)):
                    found.append(name)
    return found


def _listings(sheet: Sheet) -> list[str]:
    """Every model's listing, and the simulation's: what a failed solve prints its stop into."""
    return [str(sheet.filled["NAM_LIST"].value if "NAM_LIST" in sheet.filled
                else f"{sheet.module}.lst")] + [
        str((body.get("slots") or {}).get("NAM_LIST") or f"{body['module']}.lst")
        for body in sheet.coupled]


def _validated(rundir: Path) -> None:
    """mf6's own validate over the directory, leaving nothing of its own behind: a
    listing the parse writes is not an input the solve is handed."""
    from .authoring.validate import validate_authored_simulation

    before = set(rundir.iterdir())
    try:
        validate_authored_simulation(rundir)
    finally:
        for left in set(rundir.iterdir()) - before:
            left.unlink()


def _beside(uri: str, dest: Path) -> None:
    from trid3nt_server.tools.cache import read_object_bytes_s3

    dest.write_bytes(read_object_bytes_s3(uri))


async def stage_sheet(*, sheet: Sheet, run_tag: str, mesh: Mapping[str, Any],
                           prefix: str, server_facts: Mapping[str, Any]
                           ) -> dict[str, Any]:
    """A filled sheet written as the simulation directory, validated by mf6 and staged with the directory checked -> the staged run.
    The DISV is the accepted mesh's own artifact for this layering, staged as its input and copied beside the files
    that name it, so mf6 validates the bytes the solve reads and the staged check digests them."""
    from .authoring.mesh_files import modflow_mesh_files
    from .authoring.simulation import serialize
    from .authoring.staged_check import check_staged_run
    from ..solver.staging import new_rundir, stage_run

    rundir = new_rundir(run_tag, stem="modflow")
    disv = (await modflow_mesh_files(sheet=sheet, mesh=mesh))["disv"]
    await asyncio.to_thread(serialize, sheet, rundir, mesh)
    await asyncio.to_thread(_beside, disv, rundir / DISV_FILE)
    # The parse is a container round trip, so it runs off the loop.
    await asyncio.to_thread(_validated, rundir)
    results = _results(sheet)
    listings = _listings(sheet)
    # The manifest's module is the body answering the question: a coupled run is its transport.
    module = str(sheet.coupled[0]["module"]) if sheet.coupled else sheet.module
    return await stage_run(
        rundir, run_tag, module=module, steering=MFSIM_FILE,
        results=results, outputs=[*results, *listings, _SIM_LISTING, _METRICS],
        mesh_inputs=[{"gs_uri": disv, "dest": DISV_FILE}], prefix=prefix,
        sheet=sheet.state(), server_facts=server_facts,
        result_basename=(results or listings)[0], check=check_staged_run)


async def run_sheet(*, sheet: Sheet, staged: Mapping[str, Any],
                    mesh: Mapping[str, Any], name: str,
                    moment: str | None = None, frame: str | None = None,
                    units: Mapping[str, str] = MappingProxyType({})) -> dict[str, Any]:
    """A staged run handed to the box -> the run handle.
    Its ``reference_time`` is the instant every published time counts from: the stated ``moment``, else the solve's own start, said on the record as defaulted;
    its ``vertical_frame`` is the zero the bed, and so every head, is counted from; ``units`` what the run stated its outputs in."""
    module, _, attribute = _DISPATCH.rpartition(".")
    handle = await run_sheet_(staged, dispatch=getattr(import_module(module), attribute))
    return {**handle, "mesh": mesh, "module": sheet.module, "name": name,
            "reference_time": reference_time(moment, handle),
            "vertical_frame": frame, "published_units": dict(units)}


def _table(solved: Callable[[str], Solved], run_module: str, coupled: Sequence[str]
           ) -> tuple[list[Primitive], Callable[[Sequence[LayerURI], Mapping[Primitive, str]], None]]:
    """EVERY output row every module's output control saved -> one temporal layer per layer, and the account of the rest.
    A row a module did not save is journalled by the keyword that would have saved it, never refused."""
    from trid3nt_server.inputs.run_notes import journal_note

    rows: list[Primitive] = []
    unsaved: list[str] = []
    for module in (run_module, *coupled):
        read = solved(module)
        for token, row in read.body.MODULE_OUTPUT.items():
            refused = read.unsaved(token)
            if refused is not None:
                unsaved.append(f"no layer carries the {row.name}: {refused}")
                continue
            # The carrier's rows read off the run's own module; a coupled body's carry its name.
            rows += [field(token, t="every", plane=layer,
                           module=None if module == run_module else module)
                     .animate(style=row.style)
                     for layer in range(1, read.nlay + 1)]

    def account(layers: Sequence[LayerURI], why: Mapping[Primitive, str]) -> None:
        from trid3nt_server.inputs.observation import sited

        for line in unsaved:
            journal_note(line)
        for primitive, reason in why.items():
            # A data input's site miss was said at the read, in the reader's own words.
            if sited(primitive.at):
                continue
            journal_note(f"no layer carries the {primitive.variable} of layer "
                         f"{primitive.plane}: {reason}")

    return rows, account


def _record(solved: Solved, name: str) -> LayerURI:
    """The run's own record: the layer-1 head group's file, the mesh every published group rides; it binds no group, so it is DRAWN."""
    from trid3nt_server.store import objects as storage

    refused = solved.unsaved("HEAD")
    if refused is not None:
        raise refused
    head = solved.body.MODULE_OUTPUT["HEAD"]
    return LayerURI(
        layer_id=f"modflow-{solved.run_id}", name=name, layer_type="mesh",
        uri=(f"s3://{storage.runs_bucket()}/{solved.run_id}/"
             f"{display_file(group_stem(head, 1))}"),
        style={"kind": "reference"}, bbox=solved.bbox,
        crs_authid=f"EPSG:{solved.utm_epsg}",
        reference_time=solved.run.get("reference_time"))


async def publish_outputs(*, run: Mapping[str, Any], sheet: Sheet,
                          outputs: Sequence[Primitive], captions: Mapping[str, str],
                          params: Mapping[str, Any],
                          anchors: Sequence[Mapping[str, Any]] = ()) -> LayerURI:
    """Read what the run saved and publish every output row, one temporal layer per layer, the template's reads beside them.
    A solved run is opened over the sheet it solved, which the handle carries only as its record; a coupled body's
    keywords are its own slots."""
    from .authoring.simulation import coupled_sheet

    opened = {**run, "sheet": sheet}
    coupled = {str(body.get("module")): body for body in sheet.coupled}

    def _solved(module: str) -> Solved:
        body = coupled.get(module)
        return Solved(opened, wrapper_for(module), filled=(
            None if body is None else coupled_sheet(sheet, body).filled))

    return await publish_reads(
        engine="modflow", run=run, outputs=outputs, captions=captions,
        params=params, anchors=anchors, solved=_solved,
        table=lambda solved: _table(solved, str(run["module"]),
                                    [str(body["module"]) for body in sheet.coupled]),
        record=_record)


class ModflowWorkflow(Workflow):
    """MODFLOW: the template module, read by its own names.
    A template DECLARES STEERING (a body over one model), INPUTS, the MESH ask, OUTPUTS and CAPTIONS; this builds every stage off them."""

    engine = "modflow"
    solve_step = "solve"
    moment = EVENT_TIME.name

    def __init__(self, *, coerce: Sequence[Any] = (), **declared: Any) -> None:
        super().__init__(coerce=(*coerce, event_time()), **declared)

    @property
    def steering(self) -> type:
        """The STEERING body: the module wrapper and the slots this question asserts."""
        return self.template.STEERING

    def input_schema(self) -> tuple[Any, str] | None:
        """The module input dictionary this template's STEERING drives: where its values read their schema off."""
        from .modules.module import module_input_dir

        return (module_input_dir(), self.template.STEERING.MODULE)

    def seated(self) -> tuple[Input, ...]:
        """What the mesh ask names of the mesher's own inputs, the moment the run
        opens at, what the types of its rows state, the unit a concentration it
        carries is counted in, then the mesh a caller hands in."""
        from trid3nt_server.tools.mesh import recipe as asks

        named = set(asks.input_names(self.template.MESH))
        carries = "Transport" in self.template.STEERING.ASSERTED
        return (tuple(row for row in asks.INPUTS if row.name in named)
                + (EVENT_TIME,) + super().seated()
                + ((CONCENTRATION_UNITS,) if carries else ()) + (_SUPPLIED_MESH,))

    def published_units(self, stated: Mapping[str, Any] = MappingProxyType({})
                        ) -> Mapping[str, str]:
        """A concentration is published in the unit the run states it in; mf6 states no unit of its own."""
        unit = stated.get(CONCENTRATION_UNITS.name)
        return {"CONCENTRATION": str(unit)} if unit else {}

    def input(self, name: str) -> Any:
        """The template's and the engine's own inputs first, then the keyword a name spells on whichever of this run's bodies carries it."""
        declared = next((row for row in self.inputs if row.name == name), None)
        if declared is not None:
            return declared
        body, identifier = identify_on(run_bodies(self.steering, (name,)), name)
        return Keyword(body, identifier, body is self.steering)

    def check(self) -> None:
        """Refuse at import what this template cannot run on: one domain, one bed,
        a mesh ask and a caption for every output it lists."""
        self._slots()
        if self._states("MESH", None) is None:
            raise DeclarationError(
                f"{self.name} declares no MESH: a MODFLOW run solves the mesh its "
                "template asks for, or one the caller supplies in its place.")
        self._outputs()

    def unnamed(self) -> tuple[str, ...]:
        """Every input the template names as a plain string - in a composite, the
        mesh ask or an output - that no input of this run is called."""
        from trid3nt_server.tools.mesh.recipe import input_names

        known = {row.name for row in self.inputs}
        named = [*self.steering.named(), *input_names(self.template.MESH),
                 *(value for p in self._states("OUTPUTS", ())
                   for value in (p.at, p.along, p.within))]
        return tuple(dict.fromkeys(name for name in named
                                   if isinstance(name, str) and name not in known))

    def named_rows(self, held: Collection[str] = ()) -> tuple[str, ...]:
        """Every declared row: each is read without the mesh - the domain and bed the
        mesh is built over, and every row a composite of the sheet reads."""
        return tuple(row.name for row in self.inputs
                     if row.is_row and row is not _SUPPLIED_MESH)

    async def launch(self, state: Fill) -> RunResult:
        """The run, in order: the mesh, the sheet written, validated and staged under its review, the solve, the outputs.
        Every stage writes into the run's one mapping and reads off it by plain name."""
        from trid3nt_server.render.pipeline_emitter import (begin_substeps,
                                                            current_emitter,
                                                            substep)
        from trid3nt_server.tools.mesh.step import build_declared_mesh, keep_mesh
        from trid3nt_server.workflows.runtime.fill import call, production, read

        env = production(state)
        run = env.run
        slots = self._slots()
        domain = slots[DOMAIN]
        keywords = dict(state.keywords)
        emitter = current_emitter()
        begin_substeps(emitter, 3 + ("mesh" not in run))

        async def given(value: Any) -> Any:
            return await read(env, value) if isinstance(value, str) else value

        async def stage(label: str, fn: Any, /, **kwargs: Any) -> Any:
            async with substep(emitter, label):
                run[label] = await call(fn, kwargs, label)
            return run[label]

        # A supplied mesh is already the run's mesh: its row's ingestion wrote the record the build would have.
        if "mesh" not in run:
            await stage("mesh", build_declared_mesh,
                        mesh=await self._recipe(env, self.template.MESH, set()),
                        name=await read(env, domain), input_mode=env.input_mode,
                        fresh=bool(state.carried.get("restart_clean")))
        await keep_mesh(run["mesh"])
        for name in self.named_rows(run):
            await read(env, name)
        if run.get(EVENT_TIME.name):
            # TDIS states its start in UTC, so the run keeps where on the earth it is beside the instant.
            lon, lat = (await read(env, domain)).centroid
            run[MOMENT] = {"at": datetime.fromisoformat(run[EVENT_TIME.name]),
                           "lon": lon, "lat": lat}
        art = run["mesh"].get("artifact")
        if not getattr(art, "utm_epsg", None):
            from .authoring.accepted_mesh import mesh_missing

            raise mesh_missing("the accepted mesh names no projected zone, so "
                               "nothing solved on it can be placed on the map.")
        name = str(getattr(art, "name", None) or "domain")
        declared = {row.name for row in self.inputs if not row.is_row}
        staged: dict[str, Any] = {}
        # One tag for the run: every card edit re-stages into the same directory and prefix.
        run_tag = new_ulid()
        prefix = self.engine

        async def write(sheet: Sheet) -> None:
            # A card edit fills the sheet again, so the staged run is the one the last fill wrote.
            staged.clear()
            staged.update(await stage_sheet(
                sheet=sheet, run_tag=run_tag, mesh=run["mesh"], prefix=prefix,
                server_facts={"name": name, "utm_epsg": int(art.utm_epsg),
                              "mesh_size_m": run["mesh"].get("min_edge_m")}))

        try:
            await stage("sheet", fill_sheet, steering=self.steering,
                        # A module fills keywords from a row by the NAME of its type, so each type's row is held under that name beside its own.
                        produced={**run, **{slot.name: run[held] for slot, held
                                            in slots.items() if held in run}},
                        params={key: run[key] for key in declared},
                        workflow=self.name,
                        title=self._states("REVIEW_TITLE", ""), keywords=keywords,
                        input_mode=env.input_mode, seat=_seat_on(env), write=write,
                        user=[key for key, verdict in env.inputs.items()
                              if verdict.origin is Origin.USER])
        except BaseException:
            # A run that does not reach the box leaves no staged copy behind.
            await asyncio.to_thread(discard_run, run_tag, prefix, stem=self.engine)
            raise
        solved = await stage("solve", run_sheet, sheet=run["sheet"], staged=staged,
                             mesh=run["mesh"], name=name,
                             moment=run.get(EVENT_TIME.name),
                             frame=run.get(_FRAME.name) or _FRAME.default,
                             units=self.published_units(run))
        await keep_mesh(run["mesh"], solved.get("run_id"))
        outputs, captions = self._outputs()
        # A primitive's point, line and band name run inputs; each is read here and rejoins its primitive at publish.
        anchors = [{"at": await given(p.at), "along": await given(p.along),
                    "within": await given(p.within)} for p in outputs]
        value = await stage(
            "outputs", publish_outputs, run=solved, sheet=run["sheet"],
            outputs=[unanchored(p) for p in outputs], captions=captions,
            anchors=anchors, params={key: run[key] for key in declared})
        return RunResult(value=value, results=dict(run))
