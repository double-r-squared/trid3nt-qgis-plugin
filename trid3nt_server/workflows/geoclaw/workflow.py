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
from trid3nt_server.workflows.runtime.primitives import Primitive, field, max_over_time, publish_reads, series, unanchored
from trid3nt_server.workflows.runtime.review import _seat_on, fill_sheet
from trid3nt_server.workflows.runtime.sheet import Keyword, Sheet, reference_time
from trid3nt_server.workflows.runtime.sheet import run as run_sheet_
from trid3nt_server.workflows.runtime.workflow import EVENT_TIME
from trid3nt_server.workflows.solver.staging import discard_run

from .authoring import check_staged_run, grid_keywords, serialize
from .errors import GeoclawError
from .modules import GEO
from .modules.outputs import Solved

if TYPE_CHECKING:
    from trid3nt_server.workflows.runtime.fill import Fill

logger = logging.getLogger("trid3nt_server.workflows.geoclaw.workflow")

__all__ = ["GeoclawWorkflow", "fill_sheet", "publish_outputs", "run_sheet", "stage_sheet"]

#: The dispatch a staged run goes to, by the path it is resolved at CALL time.
_DISPATCH = "trid3nt_server.workflows.geoclaw.engine.solve_case"

#: What the worker uploads from the run directory: the fixed grids' products, the gauges and its verdict.
_OUTPUTS = ["fgmax*", "fgout*", "gauge*", "geoclaw_metrics.json"]

#: The names the workflow writes into the run itself, as the data rows it declares: composites read them, no caller states them.
_PRODUCED = ("deep_bed_offset", "sift_unit_sources")

_FRAME, = BED.states


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


async def stage_sheet(*, sheet: Sheet, run_tag: str, prefix: str, name: str) -> dict[str, Any]:
    """A filled sheet written as the run directory, checked against itself and staged -> the staged run.
    The worker reads only a case's results, so the case section is just those."""
    from ..solver.staging import new_rundir, stage_run

    rundir = new_rundir(run_tag, stem="geoclaw")
    written = await asyncio.to_thread(serialize, sheet, rundir)
    results = _results(sheet)

    def check(directory: Any, **_: Any) -> Any:
        return check_staged_run(directory, sheet=sheet, written=written["written"])

    staged = await stage_run(
        rundir, run_tag, module="geoclaw", steering="claw.data", results=results, outputs=_OUTPUTS,
        mesh_inputs=[], prefix=prefix, sheet=sheet.state(), server_facts={"name": name},
        result_basename=results[0], check=check, case={"results": results})
    return {**staged, "name": name}


async def run_sheet(*, sheet: Sheet, staged: Mapping[str, Any], mesh: Mapping[str, Any], name: str,
                    moment: str | None = None, frame: str | None = None) -> dict[str, Any]:
    """A staged run handed to the box -> the run handle.
    Its ``reference_time`` is the instant every published time counts from: the stated ``moment`` (a scenario's clock
    starts at its rupture), else the solve's own start, said on the record as defaulted;
    its ``vertical_frame`` is the zero the bed, and so every depth, is counted from."""
    module, _, attribute = _DISPATCH.rpartition(".")
    handle = await run_sheet_(staged, dispatch=getattr(import_module(module), attribute))
    return {**handle, "mesh": mesh, "module": sheet.module, "name": name, "reference_time": reference_time(moment, handle),
            "vertical_frame": frame}


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


async def publish_outputs(*, run: Mapping[str, Any], sheet: Sheet, outputs: Sequence[Primitive],
                          captions: Mapping[str, str], params: Mapping[str, Any],
                          anchors: Sequence[Mapping[str, Any]] = ()) -> LayerURI:
    """Read what the run saved and publish every row of the module table, the template's own reads beside them.
    A solved run is opened over the sheet it solved, which the handle carries only as its record."""
    opened = {**run, "sheet": sheet}
    return await publish_reads(
        engine="geoclaw", run=run, outputs=outputs, captions=captions, params=params, anchors=anchors,
        solved=lambda module: Solved(opened, GEO), table=lambda solved: _table(solved(str(run["module"])), sheet),
        record=_record)


class GeoclawWorkflow(Workflow):
    """GeoClaw: the template module, read by its own names.
    A template DECLARES STEERING (a body over the one module), INPUTS, the MESH ask and the datum and sources
    its data comes from; this builds every stage off them."""

    engine = "geoclaw"
    solve_step = "solve"
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
        return (tuple(row for row in asks.INPUTS if row.name in named) + (EVENT_TIME,) + super().seated())

    def input(self, name: str) -> Any:
        """The template's own inputs first, then the keyword a name spells on the module."""
        declared = next((row for row in self.inputs if row.name == name), None)
        if declared is not None:
            return declared
        if name in self._fetches():
            return Input(Producer(runner=self._fetches()[name], row=name), name=name)
        body, identifier = identify_on([self.steering], name)
        return Keyword(body, identifier, True)

    def _fetches(self) -> Mapping[str, str]:
        """The data rows this workflow declares for itself, by row name -> the tool each reads."""
        from trid3nt_server.inputs.vertical_datum import OFFSET_FETCH

        return {"deep_bed_fetch": self.template.DEEP_BED, "deep_bed_offset": OFFSET_FETCH,
                "sea_level_offset": OFFSET_FETCH, "sift_unit_sources_fetch": "fetch_sift_unit_sources"}

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

    async def launch(self, state: Fill) -> RunResult:
        """The run, in order: the grid, the rows, the data, the sheet written and staged under its review, the solve, the outputs."""
        from trid3nt_server.render.pipeline_emitter import begin_substeps, current_emitter, substep
        from trid3nt_server.tools.mesh.step import build_declared_mesh, keep_mesh
        from trid3nt_server.workflows.runtime.fill import call, production, read

        env = production(state)
        run = env.run
        slots = self._slots()
        domain = slots[DOMAIN]
        keywords = dict(state.keywords)
        own, _ = _floor(self.steering, keywords)
        emitter = current_emitter()
        begin_substeps(emitter, 4 + ("mesh" not in run))

        async def given(value: Any) -> Any:
            return await read(env, value) if isinstance(value, str) else value

        async def stage(label: str, fn: Any, /, **kwargs: Any) -> Any:
            async with substep(emitter, label):
                run[label] = await call(fn, kwargs, label)
            return run[label]

        if "mesh" not in run:
            await stage("mesh", build_declared_mesh, mesh=await self._recipe(env, self.template.MESH, set()),
                        name=await read(env, domain), input_mode=env.input_mode,
                        fresh=bool(state.carried.get("restart_clean")))
        await keep_mesh(run["mesh"])
        for name in self.named_rows(run):
            await read(env, name)
        art = run["mesh"]["artifact"]
        grid = grid_keywords(art, set(own) | set(self.steering.ASSERTED))
        name = str(getattr(await read(env, domain), "name", None) or "domain")
        async with substep(emitter, "data"):
            await self._data(env, own)
        declared = {row.name for row in self.inputs if not row.is_row}
        staged: dict[str, Any] = {}
        run_tag = new_ulid()
        prefix = self.engine

        async def write(sheet: Sheet) -> None:
            # A card edit fills the sheet again, so the staged run is the one the last fill wrote.
            staged.clear()
            staged.update(await stage_sheet(sheet=sheet, run_tag=run_tag, prefix=prefix, name=name))

        try:
            await stage("sheet", fill_sheet, steering=self.steering,
                        produced={**run, **{slot.name: run[held] for slot, held in slots.items() if held in run}},
                        params={key: run[key] for key in declared}, workflow=self.name,
                        title=self._states("REVIEW_TITLE", ""), keywords={**keywords, **grid},
                        input_mode=env.input_mode, seat=_seat_on(env), write=write,
                        user=[key for key, verdict in env.inputs.items() if verdict.origin is Origin.USER])
        except BaseException:
            # A run that does not reach the box leaves no staged copy behind.
            await asyncio.to_thread(discard_run, run_tag, prefix, stem=self.engine)
            raise
        solved = await stage("solve", run_sheet, sheet=run["sheet"], staged=staged, mesh=run["mesh"], name=name,
                             moment=run.get(EVENT_TIME.name), frame=run.get(_FRAME.name) or _FRAME.default)
        await keep_mesh(run["mesh"], solved.get("run_id"))
        from trid3nt_server.inputs.run_notes import journal_note

        mass = (solved.get("metrics") or {}).get("initial_mass")
        journal_note(f"Total mass at initial time {mass}" if mass is not None else
                     "the engine printed no Total mass at initial time")
        outputs, captions = self._outputs()
        # A primitive's point, line and band name run inputs; each is read here and rejoins its primitive at publish.
        anchors = [{"at": await given(p.at), "along": await given(p.along), "within": await given(p.within)} for p in outputs]
        value = await stage("outputs", publish_outputs, run=solved, sheet=run["sheet"],
                            outputs=[unanchored(p) for p in outputs], captions=captions, anchors=anchors,
                            params={key: run[key] for key in declared})
        return RunResult(value=value, results=dict(run))

    async def _data(self, env: Any, own: Mapping[str, Any]) -> None:
        """The data the template's statements ask the world for, each a declared fetch row the run reads and journals.

        The deep rung of the bed (the coarse surface under the fine one, over the whole grid) unless the run
        states one; the sea level, as the offset of the template's tidal datum on the run's frame at the seed,
        unless the run states GEO_SEA_LEVEL; the unit sources of a SIFT rupture the sheet will state."""
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
        if "GEO_SEA_LEVEL" not in own and self.steering.ASSERTED.get("GEO_SEA_LEVEL") is None:
            if seed is None:
                raise GeoclawError(
                    f"GEO_SEA_LEVEL is stated by nothing, and {self.template.SEA_LEVEL_DATUM} on {frame} is measured at the "
                    "seed, the point on the water the question is about: state seed, or state GEO_SEA_LEVEL.",
                    error_code="GEOCLAW_SEA_LEVEL_UNSTATED")
            record = await _fetch(env, "sea_level_offset", OFFSET_FETCH, point=list(seed),
                                  from_frame=self.template.SEA_LEVEL_DATUM, to_frame=frame)
            run["sea_level"] = record["offset_m"]
        rupture = own["SiftFault"] if "SiftFault" in own else self.steering.ASSERTED.get("SiftFault")
        if rupture is not None and rupture["slips"] and run.get("sift_unit_sources") is None:
            run["sift_unit_sources"] = await _fetch(env, "sift_unit_sources_fetch", "fetch_sift_unit_sources",
                                                    names=list(rupture["slips"]))


async def _fetch(env: Any, row: str, runner: str, **kwargs: Any) -> Any:
    """One fetch the run declares as a data row of its own, produced like any other row (journalled, its layer surfaced)."""
    from trid3nt_server.workflows.runtime.fill import read

    env.data[row] = Input(Producer(runner=runner, kwargs=kwargs, row=row), name=row)
    return await read(env, row)


def _box(value: Any) -> Any:
    """A rectangle or a domain read off the run -> its (min_lon, min_lat, max_lon, max_lat); anything else as stated."""
    return tuple(getattr(value, "bbox", value))
