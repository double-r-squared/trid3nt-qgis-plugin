"""The GeoClaw workflow: a template over the one module's sheet, gridded, reviewed, solved and read.

``launch`` runs one order - the grid recipe, the rows the sheet reads, the data the template's
statements ask the world for, the sheet written, checked and staged under its one review, the solve,
the outputs. The publish is the runtime's; this hands it what is the engine's own: how a solved run
is opened, the module table of what its fixed grids and gauges saved, and the run's record."""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from importlib import import_module
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import Origin
from trid3nt_contracts.execution import LayerURI

from trid3nt_server.inputs.input import Input
from trid3nt_server.inputs.instant import event_time
from trid3nt_server.inputs.slots import BED, DOMAIN
from trid3nt_server.workflows.runtime import DeclarationError, RunResult, Workflow
from trid3nt_server.workflows.runtime.data import Producer
from trid3nt_server.workflows.runtime.module import identify_on, keyword_floor
from trid3nt_server.workflows.runtime.primitives import Primitive, field, max_over_time, outline, peak, publish_reads, series, unanchored
from trid3nt_server.workflows.runtime.review import _seat_on, fill_sheet
from trid3nt_server.workflows.runtime.sheet import Keyword, Sheet, reference_time
from trid3nt_server.workflows.runtime.sheet import run as run_sheet_
from trid3nt_server.workflows.solver.cores import docker_cores
from trid3nt_server.workflows.solver.stop import SolveStopped
from trid3nt_server.workflows.runtime.workflow import EVENT_TIME
from trid3nt_server.workflows.solver.staging import discard_run

from .authoring import check_staged_run, grid_keywords, serialize
from .authoring.restart import Previous, check_matches, checkpoint_outputs, previous_run
from .errors import GeoclawError
from .modules import GEO, SlotRefused, body_on
from .modules.module import rupture_end
from .modules.outputs import Solved, refuse_dry_start

if TYPE_CHECKING:
    from trid3nt_server.workflows.runtime.fill import Fill

logger = logging.getLogger("trid3nt_server.workflows.geoclaw.workflow")

__all__ = ["GeoclawWorkflow", "fill_sheet", "publish_outputs", "run_sheet", "stage_sheet"]

#: The dispatch a staged run goes to, by the path it is resolved at CALL time.
_DISPATCH = "trid3nt_server.workflows.geoclaw.engine.solve_case"

#: What the worker uploads from the run directory: the fixed grids' products, the gauges and its verdict.
_OUTPUTS = ["fgmax*", "fgout*", "gauge*", "geoclaw_metrics.json"]

#: The keyword whose value names the checkpoint a run restarts from, by the URI of its file in the runs bucket.
_RESTART = "CLAW_RESTART_FILE"

#: The names the workflow writes into the run itself, as the data rows it declares: composites read them, no caller states them.
_PRODUCED = ("deep_bed_offset", "finite_fault", "sift_unit_sources", "storm_tracks")

_FRAME, = BED.states

#: Which GeoClaw body the run solves, and over how many MPI processes: the template's values run on the module a run names, and the
#: Boussinesq executable runs under mpiexec at the count a run states. Unstated, the template's own module and one process.
_MODULE = Input(name="module", type=str, desc=(
    "The GeoClaw module the run solves: 'geoclaw' (shallow water) or 'geoclaw_bouss' (the Boussinesq SGN / Madsen-Sorensen "
    "equations, the same keywords plus Clawpack's BOUSS_ ones); unstated, the template's own.")).stated()
_CORES = Input(name="cores", type=int, desc=(
    "The MPI process count the Boussinesq executable runs under (PETSc's linear solves use them); a whole number from 1 "
    "to the docker host's cores, unstated the CONTAINER_CORES setting divided by the threads. Shallow-water runs take none.")).stated()
_THREADS = Input(name="threads", type=int, desc=(
    "The OpenMP thread count of each process of the Boussinesq executable (OMP_NUM_THREADS); a whole number of at least 1 "
    "whose product with cores is at most the docker host's cores, unstated 1, so the run holds the cores it declares. "
    "Shallow-water runs take none.")).stated()


#: The keyword a run states its own refinement under, in the engine's record shape; it replaces the recipe's flagregion ops.
_FLAGREGIONS = "FLAGREGION_FLAGREGIONS"


def _stated_regions(keywords: dict[str, Any], steering: type) -> list[dict[str, Any]] | None:
    """The run's FLAGREGION_FLAGREGIONS taken off its keywords -> the flagregion ops that replace the recipe's; None when unstated.
    Each record is Clawpack's (name, minlevel, maxlevel, t1, t2, spatial_region_type, spatial_region): type 1 a box
    [x1, x2, y1, y2], type 2 a ruled rectangle by its fields; the grid checks each as it checks the recipe's own."""
    raw = next((raw for raw in keywords if identify_on([steering], raw) == (steering, _FLAGREGIONS)), None)
    if raw is None:
        return None
    records = keywords.pop(raw)
    if not isinstance(records, (list, tuple)):
        raise GeoclawError(f"{_FLAGREGIONS} is a list of flagregion records, not {records!r}.", error_code="GEOCLAW_FLAGREGION_RECORD")
    ops = []
    for number, record in enumerate(records, 1):
        kind = record[5] if isinstance(record, (list, tuple)) and len(record) == 7 else None
        spatial = record[6] if kind is not None else None
        box = kind == 1 and isinstance(spatial, (list, tuple)) and len(spatial) == 4
        if not (box or (kind == 2 and isinstance(spatial, Mapping))):
            raise GeoclawError(f"{_FLAGREGIONS} record {number} is not (name, minlevel, maxlevel, t1, t2, spatial_region_type, "
                               "spatial_region) with type 1 and a box [x1, x2, y1, y2] or type 2 and a ruled rectangle's fields: "
                               f"{record!r}.", error_code="GEOCLAW_FLAGREGION_RECORD")
        where = {"extent": [spatial[0], spatial[2], spatial[1], spatial[3]]} if box else {"ruled": dict(spatial)}
        ops.append({"op": "flagregion", "kwargs": dict(zip(("name", "minlevel", "maxlevel", "t1", "t2"), record[:5]), **where)})
    return ops


def _threads(run: Mapping[str, Any], module: str) -> int | None:
    """The OpenMP thread count each Boussinesq process is staged with: the run's own, else one; none for shallow water."""
    if module != "geoclaw_bouss":
        return None
    return int(run[_THREADS.name]) if run.get(_THREADS.name) is not None else 1

def _processes(run: Mapping[str, Any], module: str) -> int:
    """The MPI process count the run is staged with: the run's own, else CONTAINER_CORES shared across the threads of each process; one for shallow water."""
    from trid3nt_server.inputs.run_notes import journal_note
    from trid3nt_server.settings import setting

    if run.get(_CORES.name) is not None:
        return int(run[_CORES.name])
    if module != "geoclaw_bouss":
        return 1
    count = max(1, int(setting("CONTAINER_CORES")) // int(_threads(run, module)))
    if count > 1:
        journal_note(f"{_CORES.name} {count} from CONTAINER_CORES: the run states none, so the Boussinesq executable runs on them.")
    return count


#: Whether the real event's catalogue epicentre is drawn beside the rupture: 'fetch' reads it, unstated or 'none' draws none.
_CATALOGUE = Input(name="catalogue_epicentre", type=str, desc=(
    "'fetch' marks the real event's USGS catalogue epicentre (its preferred origin, read by fetch_earthquake_epicentre for the "
    "ComCat event the run's FiniteFault or MomentTensorFault names) beside the rupture; unstated or 'none', no catalogue "
    "point is drawn.")).stated()


def _floor(steering: type, keywords: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """The raw keyword floor -> the values by identifier and each coupled module's, none here."""
    return keyword_floor([steering], keywords)


fill_sheet = partial(fill_sheet, engine="geoclaw", floor=_floor)


def _results(sheet: Sheet) -> list[str]:
    """The files the worker must find written: each fgmax table, each gauge and each fgout grid's last frame."""
    stated = sheet.stated()
    found = [f"fgmax{int(g[0]):04d}.txt" for g in stated.get("FGMAX_FGMAX_GRIDS") or ()]
    found += [f"gauge{int(g[0]):05d}.txt" for g in stated.get("GAUGE_GAUGES") or ()]
    for grid in stated.get("FGOUT_FGOUT_GRIDS") or ():
        suffix = "q" if grid[6] == "ascii" else "b"
        found.append(f"fgout{int(grid[0]):04d}.{suffix}{int(grid[2]):04d}")
    return found


async def stage_sheet(*, sheet: Sheet, run_id: str, prefix: str, name: str,
                      previous: Previous | None = None, cores: int = 1,
                      threads: int | None = None) -> dict[str, Any]:
    """A filled sheet written as the run directory, checked against itself and staged -> the staged run.
    The case section names the module whose executable the worker runs, the process count where it is more than one, and the
    results the worker must find. A run restarting from a checkpoint
    is held to the set that checkpoint was cut on, and the checkpoint and the records it appends to are staged with it."""
    from ..solver.staging import new_rundir, stage_run

    rundir = new_rundir(run_id, stem="geoclaw")
    written = await asyncio.to_thread(serialize, sheet, rundir)
    results = _results(sheet)
    if previous is not None:
        await asyncio.to_thread(check_matches, previous, rundir, float(sheet.stated()["CLAW_TFINAL"]))

    def check(directory: Any, **_: Any) -> Any:
        return check_staged_run(directory, sheet=sheet, written=written["written"])

    staged = await stage_run(
        rundir, run_id, module=sheet.module, steering="claw.data", results=results,
        outputs=[*_OUTPUTS, *checkpoint_outputs(sheet.stated())],
        mesh_inputs=list(previous.inputs) if previous else [], prefix=prefix, sheet=sheet.state(), server_facts={"name": name},
        result_basename=results[0], check=check,
        case={"module": sheet.module, **({"cores": int(cores)} if int(cores) > 1 else {}),
              **({"threads": int(threads)} if threads is not None else {}), "results": results})
    return {**staged, "name": name}


async def run_sheet(*, sheet: Sheet, staged: Mapping[str, Any], mesh: Mapping[str, Any], name: str,
                    moment: str | None = None, frame: str | None = None) -> dict[str, Any]:
    """A staged run handed to the box -> the run handle.
    Its ``reference_time`` is the instant every published time counts from: the stated ``moment`` (a scenario's clock
    starts at its rupture), else the solve's own start, said on the record as defaulted;
    its ``vertical_frame`` is the zero the bed, and so every depth, is counted from."""
    module, _, attribute = _DISPATCH.rpartition(".")
    def shaped(handle: Mapping[str, Any]) -> dict[str, Any]:
        return {**handle, "mesh": mesh, "module": sheet.module, "name": name,
                "reference_time": reference_time(moment, handle), "vertical_frame": frame}

    try:
        return shaped(await run_sheet_(staged, dispatch=getattr(import_module(module), attribute)))
    except SolveStopped as stop:
        stop.solved = shaped(stop.solved)
        raise


def _table(solved: Solved, sheet: Sheet) -> tuple[list[Primitive], Callable[[Sequence[LayerURI], Mapping[Primitive, str]], None]]:
    """EVERY output row the run's fixed grids and gauges saved -> one read per row, and the account of the rest.
    A row the run did not save is journalled by the keyword that would have saved it, never refused."""
    from trid3nt_server.inputs.run_notes import journal_note

    rows: list[Primitive] = []
    unsaved: list[str] = []
    gauges = [int(g[0]) for g in sheet.stated().get("GAUGE_GAUGES") or ()]
    for token, row in GEO.MODULE_OUTPUT.items():
        refused = solved.unsaved(token)
        if refused is not None:
            unsaved.append(f"no layer carries the {row.name}: {refused}")
            continue
        if token.startswith("GAUGE_"):
            rows += [series(token, at=number).chart() for number in gauges]
        elif token == "INUNDATION_LINE":
            rows.append(outline(token).layer(style=row.style))
        elif token == "MAX_RUNUP":
            rows.append(peak(token).layer(style=row.style))
        elif token in ("DEPTH", "SURFACE", "OVERLAND_DEPTH"):
            rows.append(field(token, t="every").animate(style=row.style))
        else:
            rows.append(max_over_time(token).layer(style=row.style))

    def account(layers: Sequence[LayerURI], why: Mapping[Primitive, str]) -> None:
        for line in unsaved:
            journal_note(line)
        for primitive, reason in why.items():
            journal_note(f"no layer carries the {primitive.variable}: {reason}")

    return rows, account


def _record(solved: Solved, name: str) -> LayerURI:
    """The run's own record: the outline of the fixed grids every product is read on; it binds no group, so it is DRAWN."""
    import json

    from trid3nt_server.store import objects as storage

    refused = solved.unsaved("MAX_DEPTH")
    if refused is not None:
        raise refused
    grid = solved.record("MAX_DEPTH")[8]
    dx, dy = (grid["x2"] - grid["x1"]) / (grid["nx"] - 1), (grid["y2"] - grid["y1"]) / (grid["ny"] - 1)
    west, south = grid["x1"] - dx / 2, grid["y1"] - dy / 2
    east, north = grid["x2"] + dx / 2, grid["y2"] + dy / 2
    outline = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": name}, "geometry": {
        "type": "Polygon", "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]]}}]}
    key = f"{solved.run_id}/record.geojson"
    storage.client().put_object(Bucket=storage.runs_bucket(), Key=key, Body=json.dumps(outline).encode("utf-8"),
                                ContentType="application/geo+json")
    return LayerURI(
        layer_id=f"geoclaw-{solved.run_id}", name=name, layer_type="vector",
        uri=f"s3://{storage.runs_bucket()}/{key}", style={"kind": "reference", "geometry": "polygon"},
        bbox=(west, south, east, north), crs_authid="EPSG:4326", reference_time=solved.run.get("reference_time"))


def _domain_features(facts: Mapping[str, Any], name: str) -> dict[str, Any]:
    """The level-1 box and one polygon per flagregion of the built grid, as a lon/lat FeatureCollection."""
    from shapely.geometry import Polygon, box, mapping

    def feature(properties: Mapping[str, Any], shape: Any) -> dict[str, Any]:
        return {"type": "Feature", "properties": dict(properties), "geometry": mapping(shape)}

    features = [feature({"name": name, "level": 1, "levels": facts["levels"]}, box(*facts["lower"], *facts["upper"]))]
    for region in facts["flagregions"]:
        shape = Polygon(region["polygon"]) if "polygon" in region else box(*region["extent"])
        features.append(feature({"name": region["name"], "minlevel": region["minlevel"], "maxlevel": region["maxlevel"],
                                 "t1_s": region["t1"], "t2_s": region["t2"]}, shape))
    return {"type": "FeatureCollection", "features": features}


async def publish_grid(*, art: Any, run_id: str, name: str) -> None:
    """The level-1 box and the refinement regions of the built grid, surfaced as a run input beside the others."""
    from trid3nt_server.render.formats import Deliverable, Vector, publish

    features = await asyncio.to_thread(_domain_features, art.probes["claw_grid"], name)
    await publish(run_id=run_id, engine="geoclaw", name=name, items=[Deliverable(
        product=Vector(features=features, stem="domain"), caption="domain and refinement regions", role="input",
        style={"kind": "reference", "geometry": "polygon"})])


def _point_item(feature: Mapping[str, Any], stem: str, caption: str) -> Any:
    """One labelled point of the rupture as a context deliverable."""
    from trid3nt_server.render.formats import Deliverable, Vector

    return Deliverable(product=Vector(features={"type": "FeatureCollection", "features": [dict(feature)]}, stem=stem),
                       caption=caption, role="context", style={"kind": "reference", "geometry": "point"})


def _rupture_items(shown: Mapping[str, Any], run_id: str, name: str) -> list[Any]:
    """Each dtopo's subfault outlines, the one point its source states (labelled by kind) and its final uplift as deliverables,
    the uplift on a diverging ramp centred on 0."""
    from dataclasses import replace
    from types import SimpleNamespace

    import numpy as np

    from trid3nt_server.render.formats import Deliverable, Vector
    from trid3nt_server.workflows.runtime.primitives import Primitive, Rasters, deliver

    style = {"kind": "continuous", "ramp": "rdbu", "center": 0.0}
    items: list[Any] = []
    for number, drawn in enumerate(shown.values(), 1):
        suffix = "" if len(shown) == 1 else f" {number}"
        items.append(Deliverable(product=Vector(features=drawn["features"], stem=f"rupture-subfaults{number}"),
                                 caption=f"rupture subfaults{suffix}", role="context",
                                 style={"kind": "reference", "geometry": "polygon"}))
        if drawn.get("point"):
            kind = drawn["point"]["properties"]["kind"]
            items.append(_point_item(drawn["point"], f"rupture-{kind}{number}", f"rupture {kind}{suffix}"))
        read = Rasters(name="vertical seafloor displacement", units="m", values=np.stack([drawn["uplift"]]),
                       bounds=drawn["bounds"])
        items.append(replace(deliver(Primitive("max_over_time", style=style), read, SimpleNamespace(run_id=run_id, run={}),
                                     caption=f"rupture uplift{suffix}", name=name, where=name), role="context"))
    return items


async def publish_rupture(*, sheet: Sheet, rundir: Any, run_id: str, name: str, catalogue: Mapping[str, Any] | None = None) -> None:
    """The rupture the run solves: its subfault outlines, the point its source states and its final uplift, surfaced as context
    beside the products, with the real event's ``catalogue`` epicentre when the run fetched it."""
    from pathlib import Path

    from trid3nt_server.render.formats import publish

    from .authoring.dtopo import rupture_on_map

    shown = await asyncio.to_thread(rupture_on_map, sheet, Path(rundir))
    items = await asyncio.to_thread(_rupture_items, shown, run_id, name) if shown else []
    if catalogue is not None:
        items.append(_point_item(catalogue, "catalogue-epicentre", "catalogue epicentre"))
    if items:
        await publish(run_id=run_id, engine="geoclaw", name=name, items=items)


async def publish_outputs(*, run: Mapping[str, Any], sheet: Sheet, outputs: Sequence[Primitive],
                          captions: Mapping[str, str], params: Mapping[str, Any],
                          anchors: Sequence[Mapping[str, Any]] = (), partial: str | None = None) -> LayerURI:
    """Read what the run saved and publish every row of the module table, the template's own reads beside them.
    A solved run is opened over the sheet it solved, which the handle carries only as its record."""
    opened = {**run, "sheet": sheet}
    return await publish_reads(
        engine="geoclaw", run=run, outputs=outputs, captions=captions, params=params, anchors=anchors,
        solved=lambda module: Solved(opened, GEO), table=lambda solved: _table(solved(str(run["module"])), sheet),
        record=_record, partial=partial)


class GeoclawWorkflow(Workflow):
    """GeoClaw: the template module, read by its own names.
    A template DECLARES STEERING (a body over the one module), INPUTS, the MESH ask and the datum and sources
    its data comes from; this builds every stage off them."""

    engine = "geoclaw"
    solve_step = "solve"
    lays_own_bed = True
    moment = EVENT_TIME.name

    def __init__(self, *, coerce: Sequence[Any] = (), **declared: Any) -> None:
        super().__init__(coerce=(*coerce, event_time()), **declared)

    @property
    def steering(self) -> type:
        """The STEERING body: the module wrapper and the slots this question asserts."""
        return self.template.STEERING

    def seated(self) -> tuple[Input, ...]:
        """What the mesh ask names of the mesher's own inputs, the moment the run opens at, and what the types of its rows state."""
        from trid3nt_server.tools.mesh import recipe as asks

        named = set(asks.input_names(self.template.MESH))
        return (tuple(row for row in asks.INPUTS if row.name in named) + (EVENT_TIME, _MODULE, _CORES, _THREADS, _CATALOGUE) + super().seated())

    def input(self, name: str) -> Any:
        """The template's own inputs first, then the keyword a name spells on the module."""
        declared = next((row for row in self.inputs if row.name == name), None)
        if declared is not None:
            return declared
        if name in self._fetches():
            return Input(Producer(runner=self._fetches()[name], row=name), name=name)
        # A name is held to the widest body (the Boussinesq one has every keyword the shallow-water one has); the run's own module refuses what it does not spell, at launch.
        body, identifier = identify_on([body_on(self.steering, "geoclaw_bouss")], name)
        return Keyword(body, identifier, True)

    def _fetches(self) -> Mapping[str, str]:
        """The data rows this workflow declares for itself, by row name -> the tool each reads."""
        from trid3nt_server.inputs.vertical_datum import OFFSET_FETCH

        return {"deep_bed_fetch": self.template.DEEP_BED, "deep_bed_offset": OFFSET_FETCH,
                "sea_level_offset": OFFSET_FETCH, "sift_unit_sources_fetch": "fetch_sift_unit_sources",
                "finite_fault_fetch": "fetch_finite_fault", "storm_tracks_fetch": "fetch_storm_tracks",
                "catalogue_epicentre_fetch": "fetch_earthquake_epicentre"}

    def check(self) -> None:
        """Refuse at import what this template cannot run on: one domain, one bed, a mesh ask."""
        self._slots()
        if self._states("MESH", None) is None:
            raise DeclarationError(f"{self.name} declares no MESH: a GeoClaw run solves the grid recipe its template asks for.")
        self._outputs()

    def unnamed(self) -> tuple[str, ...]:
        """Every input the template names as a plain string - in a composite or the mesh ask - that no input of this run is called."""
        from trid3nt_server.tools.mesh.recipe import input_names

        known = {row.name for row in self.inputs}
        named = [*self.steering.named(), *input_names(self.template.MESH)]
        return tuple(dict.fromkeys(name for name in named if isinstance(name, str) and name not in known | set(_PRODUCED)))

    def named_rows(self, held: Any = ()) -> tuple[str, ...]:
        """Every declared row: each is read without the grid recipe."""
        return tuple(row.name for row in self.inputs if row.is_row)

    async def _recipe(self, env: Any, recipe: Any, untaken: set[str]) -> dict[str, Any]:
        """The grid ask with every input it names read off the run; a rectangle or a domain stands as its box."""
        asked = await super()._recipe(env, recipe, untaken)
        asked["extent"] = _box(asked["extent"])
        for entry in asked["ops"]:
            entry["kwargs"] = {key: _box(value) if key == "extent" else value
                               for key, value in entry["kwargs"].items()}
        return asked

    def _steered(self, run: Mapping[str, Any]) -> type:
        """The body the run solves: the template's, rebased onto the module the run names, whose cores are checked here.
        A module nothing exposes, or a process count the module's executable cannot take, refuses by name."""
        module = str(run.get(_MODULE.name) or self.steering.MODULE)
        try:
            steering = body_on(self.steering, module)
        except SlotRefused as refused:
            raise GeoclawError(f"{_MODULE.name} {module!r} is no GeoClaw module: {refused}", error_code="GEOCLAW_MODULE_UNKNOWN") from refused
        stated = run.get(_CORES.name)
        if stated is not None:
            if module != "geoclaw_bouss":
                raise GeoclawError(f"{_CORES.name} is stated for the module {module}, whose executable runs one process: only "
                                   "geoclaw_bouss runs under mpiexec.", error_code="GEOCLAW_CORES_UNUSED")
            if not 1 <= int(stated) <= docker_cores():
                raise GeoclawError(f"{_CORES.name} {stated!r} is outside 1 to the docker host's {docker_cores()} cores.",
                                   error_code="GEOCLAW_CORES_OUT_OF_RANGE")
        threads = run.get(_THREADS.name)
        if threads is not None:
            if module != "geoclaw_bouss":
                raise GeoclawError(f"{_THREADS.name} is stated for the module {module}, whose executable is not compiled for "
                                   "OpenMP: only geoclaw_bouss takes threads.", error_code="GEOCLAW_THREADS_UNUSED")
            processes = int(stated or 1)
            if int(threads) < 1 or int(threads) * processes > docker_cores():
                raise GeoclawError(f"{_THREADS.name} {threads!r} on {processes} process(es) is outside 1 thread to the docker host's "
                                   f"{docker_cores()} cores in all.", error_code="GEOCLAW_THREADS_OUT_OF_RANGE")
        return steering

    def _restarted(self, keywords: dict[str, Any], steering: type) -> Previous | None:
        """The checkpoint the run names, taken off the keywords and replaced by the engine's own pair: CLAW_RESTART true and
        CLAW_RESTART_FILE the staged file's bare name. A run that states CLAW_RESTART itself is refused: naming the file is the restart."""
        for raw in list(keywords):
            body, identifier = identify_on([steering], raw)
            if body is steering and identifier == "CLAW_RESTART":
                raise GeoclawError(f"{raw} is stated: name the checkpoint with {_RESTART} (the URI of its file in the runs "
                                   "bucket) and the run restarts from it.", error_code="GEOCLAW_RESTART_STATED")
        for raw in list(keywords):
            body, identifier = identify_on([steering], raw)
            if body is steering and identifier == _RESTART:
                previous = previous_run(str(keywords.pop(raw)))
                keywords.update({"CLAW_RESTART": True, _RESTART: previous.name})
                return previous
        return None

    def _rise_shaped(self, keywords: dict[str, Any], steering: type) -> dict[str, Any]:
        """The run's keywords, a kinematic Fault or FiniteFault that states no rise_shape taking the template's RISE_SHAPE."""
        shape = self._states("RISE_SHAPE", None)
        for raw, value in keywords.items():
            _, identifier = identify_on([steering], raw)
            if shape and identifier in ("Fault", "FiniteFault") and isinstance(value, Mapping) \
                    and value.get("rupture_type") == "kinematic" and value.get("rise_shape") is None:
                keywords[raw] = {**value, "rise_shape": shape}
        return keywords

    async def launch(self, state: Fill) -> RunResult:
        """The run, in order: the grid, the rows, the data, the sheet written and staged under its review, the solve, the outputs."""
        from trid3nt_server.render.pipeline_emitter import begin_substeps, current_emitter, substep
        from trid3nt_server.tools.mesh.step import build_declared_mesh, keep_mesh
        from trid3nt_server.workflows.runtime.fill import anchors_of, call, production, read

        env = production(state)
        run = env.run
        slots = self._slots()
        domain = slots[DOMAIN]
        steering = self._steered(run)
        keywords = self._rise_shaped(dict(state.keywords), steering)
        previous = await asyncio.to_thread(self._restarted, keywords, steering)
        regions = _stated_regions(keywords, steering)
        own, _ = _floor(steering, keywords)
        emitter = current_emitter()
        begin_substeps(emitter, 4 + ("mesh" not in run))

        async def stage(label: str, fn: Any, /, **kwargs: Any) -> Any:
            async with substep(emitter, label):
                run[label] = await call(fn, kwargs, label)
            return run[label]

        if "mesh" not in run:
            asked = await self._recipe(env, self.template.MESH, set())
            await stage("mesh", build_declared_mesh, mesh={**asked, "ops": asked["ops"] if regions is None else regions},
                        name=await read(env, domain),
                        fresh=bool(state.carried.get("restart_clean")))
        await keep_mesh(run["mesh"])
        run_id = new_ulid()
        await publish_grid(art=run["mesh"]["artifact"], run_id=run_id, name=str(getattr(await read(env, domain), "name", None) or "domain"))
        for name in self.named_rows(run):
            await read(env, name)
        art = run["mesh"]["artifact"]
        name = str(getattr(await read(env, domain), "name", None) or "domain")
        async with substep(emitter, "data"):
            await self._data(env, own, steering)
            catalogue = await self._catalogue(env, own, steering)
        ends = [rupture_end(composite, own[composite] if composite in own else steering.ASSERTED.get(composite), run)
                for composite in ("Fault", "SiftFault", "FiniteFault", "SlabFault", "MomentTensorFault")]
        grid = grid_keywords(art, set(own) | set(steering.ASSERTED), max((end for end in ends if end is not None), default=None))
        declared = {row.name for row in self.inputs if not row.is_row}
        staged: dict[str, Any] = {}
        prefix = self.engine

        async def write(sheet: Sheet) -> None:
            # A card edit fills the sheet again, so the staged run is the one the last fill wrote.
            staged.clear()
            staged.update(await stage_sheet(sheet=sheet, run_id=run_id, prefix=prefix, name=name, previous=previous,
                                            cores=_processes(run, steering.MODULE),
                                            threads=_threads(run, steering.MODULE)))

        try:
            await stage("sheet", fill_sheet, steering=steering,
                        produced={**run, **{slot.name: run[held] for slot, held in slots.items() if held in run}},
                        params={key: run[key] for key in declared}, workflow=self.name,
                        title=self._states("REVIEW_TITLE", ""), keywords={**keywords, **grid},
                        seat=_seat_on(env), write=write,
                        user=[key for key, verdict in env.inputs.items() if verdict.origin is Origin.USER])
        except BaseException:
            # A run that does not reach the box leaves no staged copy behind.
            await asyncio.to_thread(discard_run, run_id, prefix, stem=self.engine)
            raise
        await publish_rupture(sheet=run["sheet"], rundir=staged["rundir"], run_id=run_id, name=name, catalogue=catalogue)
        async def products(handle: Mapping[str, Any], partial: str | None) -> Any:
            outputs, captions = self._outputs()
            # A primitive's point, line and band name run inputs; each is read here and rejoins its primitive at publish.
            anchors = await anchors_of(env, outputs)
            return await stage("outputs", publish_outputs, run=handle, sheet=run["sheet"],
                               outputs=[unanchored(p) for p in outputs], captions=captions, anchors=anchors,
                               params={key: run[key] for key in declared}, partial=partial)

        try:
            solved = await stage("solve", run_sheet, sheet=run["sheet"], staged=staged, mesh=run["mesh"], name=name,
                                 moment=run.get(EVENT_TIME.name), frame=run.get(_FRAME.name) or _FRAME.default)
        except SolveStopped as stop:
            await self.stopped(stop, run, products)
        await keep_mesh(run["mesh"], solved.get("run_id"))
        from trid3nt_server.inputs.run_notes import journal_note

        metrics = solved.get("metrics") or {}
        mass, zeta = metrics.get("initial_mass"), metrics.get("initial_zeta")
        if previous is not None:
            journal_note(f"restarted from {previous.uri} at t={previous.time:g} s; the engine prints no initial mass on a restart")
        elif zeta is not None:
            journal_note(f"Total zeta at initial time {zeta}")
        else:
            journal_note(f"Total mass at initial time {mass}" if mass is not None else
                         "the engine printed no Total mass at initial time")
        if str(solved.get("module")) == "geoclaw_bouss":
            await asyncio.to_thread(refuse_dry_start, Solved({**solved, "sheet": run["sheet"]}, GEO))
        return RunResult(value=await products(solved, None), results=dict(run))

    async def _data(self, env: Any, own: Mapping[str, Any], steering: type) -> None:
        """The data the template's statements ask the world for, each a declared fetch row the run reads and journals.

        The deep rung of the bed (the coarse surface under the fine one, over the whole grid) unless the run
        states one; the sea level, as the offset of the template's tidal datum on the run's frame at the seed,
        unless the run states GEO_SEA_LEVEL; the unit sources of a SIFT rupture the sheet will state; the record of the earthquake a FiniteFault statement
        names by ``event_id``, unless the statement already holds a record or the run holds one under its name."""
        from trid3nt_server.inputs.point import lonlat_of
        from trid3nt_server.inputs.vertical_datum import OFFSET_FETCH, offset_ask

        run = env.run
        frame = str(run.get(_FRAME.name) or _FRAME.default)
        seed = lonlat_of(run.get("seed"))
        if run.get("deep_bed") is None:
            facts = run["mesh"]["artifact"].probes["claw_grid"]
            cell_x, cell_y = facts["level1_cell_deg"]
            west, south = facts["lower"]
            east, north = facts["upper"]
            layer = await _fetch(env, "deep_bed_fetch", self.template.DEEP_BED,
                                 bbox=[west - cell_x, south - cell_y, east + cell_x, north + cell_y],
                                 purpose="deep bed")
            ask = await asyncio.to_thread(offset_ask, layer, frame, at=seed)
            run["deep_bed"] = layer
            run["deep_bed_offset"] = await _fetch(env, "deep_bed_offset", OFFSET_FETCH, **ask) if ask else None
        if "GEO_SEA_LEVEL" not in own and steering.ASSERTED.get("GEO_SEA_LEVEL") is None:
            if seed is None:
                raise GeoclawError(
                    f"GEO_SEA_LEVEL is stated by nothing, and {self.template.SEA_LEVEL_DATUM} on {frame} is measured at the "
                    "seed, the point on the water the question is about: state seed, or state GEO_SEA_LEVEL.",
                    error_code="GEOCLAW_SEA_LEVEL_UNSTATED")
            record = await _fetch(env, "sea_level_offset", OFFSET_FETCH, point=list(seed),
                                  from_frame=self.template.SEA_LEVEL_DATUM, to_frame=frame)
            run["sea_level"] = record["offset_m"]
        rupture = own["SiftFault"] if "SiftFault" in own else steering.ASSERTED.get("SiftFault")
        if rupture is not None and rupture["slips"] and run.get("sift_unit_sources") is None:
            run["sift_unit_sources"] = await _fetch(env, "sift_unit_sources_fetch", "fetch_sift_unit_sources",
                                                    names=list(rupture["slips"]))
        track = own["Storm"] if "Storm" in own else steering.ASSERTED.get("Storm")
        if isinstance(track, Mapping) and isinstance(track.get("storm_tracks"), str) and run.get(track["storm_tracks"]) is None:
            await self._storm(env, track)
        finite = own["FiniteFault"] if "FiniteFault" in own else steering.ASSERTED.get("FiniteFault")
        if isinstance(finite, Mapping) and finite.get("event_id") and isinstance(finite.get("finite_fault"), str) \
                and run.get(finite["finite_fault"]) is None:
            run[finite["finite_fault"]] = await _fetch(env, "finite_fault_fetch", "fetch_finite_fault",
                                                       event_id=finite["event_id"])

    async def _catalogue(self, env: Any, own: Mapping[str, Any], steering: type) -> dict[str, Any] | None:
        """The real event's catalogue epicentre as a point feature when the run asks for it with 'fetch'; None for 'none' or unstated.
        The event is the one the run's FiniteFault or MomentTensorFault names; a run that names none refuses rather than guess one."""
        choice = env.run.get(_CATALOGUE.name) or "none"
        if choice not in ("none", "fetch"):
            raise GeoclawError(f"{_CATALOGUE.name} {choice!r} is neither 'none' nor 'fetch'.", error_code="GEOCLAW_EPICENTRE_CHOICE")
        if choice == "none":
            return None
        event = _event_of(env.run, own, steering)
        if event is None:
            raise GeoclawError(f"{_CATALOGUE.name} 'fetch' reads the epicentre of a real event, and the run's rupture names none: "
                               "state a FiniteFault or MomentTensorFault of a ComCat event, or catalogue_epicentre 'none'.",
                               error_code="GEOCLAW_EPICENTRE_NO_EVENT")
        record = await _fetch(env, "catalogue_epicentre_fetch", "fetch_earthquake_epicentre", event_id=event)
        return {"type": "Feature", "properties": {
                    "kind": "catalogue epicentre", "event_id": record["event_id"], "depth_m": record["depth_km"] * 1000.0,
                    "time": record["time"], "magnitude": record["magnitude"], "magnitude_type": record["magnitude_type"]},
                "geometry": {"type": "Point", "coordinates": [record["longitude"], record["latitude"]]}}

    async def _storm(self, env: Any, statement: Mapping[str, Any]) -> None:
        """The points of the storm a Storm statement names (its name and season, over the level-1 grid), read into the run.

        A run that states no event_time counts its seconds from the storm's last landfall fix."""
        from trid3nt_server.inputs.geometry import read_geometry_doc
        from trid3nt_server.inputs.run_notes import journal_note

        from .authoring.storm import landfall_instant

        run = env.run
        name, season = run.get(statement["storm_name"]), run.get(statement["season"])
        if name is None or season is None:
            raise GeoclawError("Storm names no storm: state storm_name and season, the name and the year of the one whose "
                               "track the run is driven by.", error_code="GEOCLAW_STORM_UNSTATED")
        facts = run["mesh"]["artifact"].probes["claw_grid"]
        (west, south), (east, north) = facts["lower"], facts["upper"]
        layer = await _fetch(env, "storm_tracks_fetch", "fetch_storm_tracks", bbox=[west, south, east, north], storm_name=name,
                             start_year=int(season), end_year=int(season), geometry="points", purpose="storm track")
        run[statement["storm_tracks"]] = layer
        if run.get(EVENT_TIME.name) is None:
            moment = landfall_instant(await asyncio.to_thread(read_geometry_doc, layer))
            run[EVENT_TIME.name] = moment
            journal_note(f"reference time is the storm's last landfall fix, {moment} (IBTrACS USA_RECORD L): the run states no "
                         "event_time its seconds are counted from.")


def _event_of(run: Mapping[str, Any], own: Mapping[str, Any], steering: type) -> str | None:
    """The ComCat event the run's rupture names: a FiniteFault's event_id, else the event of the record it or a MomentTensorFault reads."""
    for composite, field in (("FiniteFault", "finite_fault"), ("MomentTensorFault", "moment_tensor")):
        value = own[composite] if composite in own else steering.ASSERTED.get(composite)
        if not isinstance(value, Mapping):
            continue
        if value.get("event_id"):
            return str(value["event_id"])
        record = run.get(value[field]) if isinstance(value.get(field), str) else value.get(field)
        if isinstance(record, Mapping) and record.get("event_id"):
            return str(record["event_id"])
    return None


async def _fetch(env: Any, row: str, runner: str, **kwargs: Any) -> Any:
    """One fetch the run declares as a data row of its own, produced like any other row (journalled, its layer surfaced)."""
    from trid3nt_server.workflows.runtime.fill import read

    env.data[row] = Input(Producer(runner=runner, kwargs=kwargs, row=row), name=row)
    return await read(env, row)


def _box(value: Any) -> Any:
    """A rectangle or a domain read off the run -> its (min_lon, min_lat, max_lon, max_lat); anything else as stated."""
    return tuple(getattr(value, "bbox", value))
