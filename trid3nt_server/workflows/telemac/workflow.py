"""``TelemacWorkflow`` - the TELEMAC engine facade.

Four operations, and nothing else. Behind them sit the TELEMAC step families
(``workflows/telemac/steps``): the reach front (geocode, flowline, seed, corridor
deck, reach solve, per-deliverable readers) and the open-water front (an AOI, a
regular grid over it, one worker section, one result SELAFIN). The facade is what
stays still while those move.

The MESH is not one of the four: a template declares its mesh ask as a
``tool.build_mesh`` block beside PHYSICS and FORCING, and ``author`` reads the
fields the deck writers know by their own names.

WHAT VARIES BETWEEN TEMPLATES is the declared PHYSICS PROCESS, and one table
(:data:`_PROCESSES`) says what each process means end to end - which deck writer
serializes it, which solve dispatches it, which reader publishes it. A process
the facade does not know REFUSES at plan construction rather than solving into a
reader that cannot describe the result.

Named by ENGINE only. A domain qualifier here would weld a domain assumption into
the engine; the domain arrives through ``acquire_domain``'s slots.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from trid3nt_server.workflows.lib import (
    DataRef,
    Forcing,
    Physics,
    PlanValidationError,
    Ref,
    Step,
    Workflow,
)
from trid3nt_server.workflows.shared.aoi import AcquireAoi
from trid3nt_server.workflows.telemac.steps import (
    AcquireCatchment,
    Agitation,
    CarrierDischarge,
    Geocode,
    Products,
    RainOnGrid,
    ReachSeed,
    Solve,
    SolveOpenWater,
    SolveRainOnGrid,
    Stratified,
    WriteDeck,
    write_agitation_deck,
    write_rain_on_grid_deck,
    write_reach_deck,
    write_stratified_deck,
)

__all__ = ["TelemacWorkflow", "mesh_deck_fields"]


@dataclass(frozen=True, slots=True)
class _Process:
    """One declared physics process, end to end.

    The facade routes on ``Physics.process``, and this is what it routes TO: the
    deck step that serializes it, the writer whose real signature the slots are
    checked against in both directions, the solve that dispatches it, and the
    reader that turns the solved file into the question's deliverable. Adding a
    TELEMAC domain is a row here plus its two runners - never a branch in the four
    operations.
    """

    #: What the deck writer calls the acquired domain (``reach`` / ``aoi`` /
    #: ``catchment``).
    domain_kw: str
    #: The plan value that domain keyword takes. It is the PROCESS's, not the
    #: template's: a reach deck reads the acquired reach, an open-water deck its
    #: AOI, a rain deck the built catchment mesh, and no template chooses otherwise.
    domain_ref: Any
    deck: Callable[..., Step]
    writer: Callable[..., Any]
    solve: Callable[..., Step]
    read: Callable[..., Step]
    #: Forcing slot member -> the deck writer's own keyword for it.
    forcing_fields: Mapping[str, str]
    #: Extra plan-value deck fields this process's writer always takes.
    extra_fields: Mapping[str, Any] = None  # type: ignore[assignment]


def _reach_solve(*, compute_class: Any) -> Step:
    return Solve.telemac(deck=Ref("deck"), compute_class=compute_class)


def _open_water_solve(*, compute_class: Any) -> Step:
    return SolveOpenWater.telemac(deck=Ref("deck"), compute_class=compute_class)


def _rain_on_grid_solve(*, compute_class: Any) -> Step:
    return SolveRainOnGrid.telemac(deck=Ref("deck"), compute_class=compute_class)


def _read_rain_on_grid(*, solve: Any, physics: Physics, forcing: Forcing) -> Step:
    return RainOnGrid.products(deck=Ref("deck"), solve=solve).named("flood_depth")


def _read_dye(*, solve: Any, physics: Physics, forcing: Forcing) -> Step:
    return Products.dye(deck=Ref("deck"), solve=solve,
                        carrier_discharge=forcing.values.get("carrier")).named("plume")


def _read_dissolved_oxygen(*, solve: Any, physics: Physics, forcing: Forcing) -> Step:
    return Products.dissolved_oxygen(
        deck=Ref("deck"), solve=solve,
        process=physics.values.get("do_sag_config"),
        carrier_discharge=forcing.values.get("carrier")).named("do_field")


def _read_agitation(*, solve: Any, physics: Physics, forcing: Forcing) -> Step:
    return Agitation.products(deck=Ref("deck"), solve=solve).named("agitation")


def _read_stratified(*, solve: Any, physics: Physics, forcing: Forcing) -> Step:
    return Stratified.products(deck=Ref("deck"), solve=solve).named("column")


#: Plan-value deck fields every reach writer takes: the seed the one centerline
#: was navigated from, that CENTERLINE itself, the ACCEPTED mesh the solve runs
#: on, and the mapped water the reach was cut from - which a dredge field is cut
#: out of. All four are chain products rather than sheet values, so none can be
#: declared. The centerline is named here so the deck reads the SAME line the
#: section was cut between and the mesh was built over.
_REACH_EXTRA: Mapping[str, Any] = {"seed": Ref("seed"), "mesh": Ref("mesh"),
                                   "centerline": Ref("centerline"),
                                   "reach_polygon": Ref("reach_polygon")}

#: The agitation deck always reads the run's MESH slot: the domain a phase-
#: resolving solve runs on is the caller's to author, and a deck that read the
#: slot only sometimes would answer the grid question on the runs that filled it.
_AGITATION_EXTRA: Mapping[str, Any] = {"supplied_mesh": DataRef("mesh")}

_REACH_FORCING: Mapping[str, str] = {"carrier": "carrier_discharge", "rain": "rain"}
_RAIN_FORCING: Mapping[str, str] = {"rain": "rain"}

#: The known TELEMAC physics processes. See :class:`_Process`.
_PROCESSES: dict[str, _Process] = {
    "tracer": _Process(
        domain_kw="reach", domain_ref=Ref("reach"),
        deck=WriteDeck.telemac, writer=write_reach_deck,
        solve=_reach_solve, read=_read_dye, forcing_fields=_REACH_FORCING,
        extra_fields=_REACH_EXTRA),
    "morphodynamics": _Process(
        domain_kw="reach", domain_ref=Ref("reach"),
        deck=WriteDeck.telemac, writer=write_reach_deck,
        solve=_reach_solve, read=_read_dye, forcing_fields=_REACH_FORCING,
        extra_fields=_REACH_EXTRA),
    "waqtel_o2": _Process(
        domain_kw="reach", domain_ref=Ref("reach"),
        deck=WriteDeck.telemac, writer=write_reach_deck,
        solve=_reach_solve, read=_read_dissolved_oxygen,
        forcing_fields=_REACH_FORCING, extra_fields=_REACH_EXTRA),
    "harbor_agitation": _Process(
        domain_kw="aoi", domain_ref=Ref("aoi"),
        deck=Agitation.deck, writer=write_agitation_deck,
        solve=_open_water_solve, read=_read_agitation, forcing_fields={},
        extra_fields=_AGITATION_EXTRA),
    "stratified_3d": _Process(
        domain_kw="aoi", domain_ref=Ref("aoi"),
        deck=Stratified.deck, writer=write_stratified_deck,
        solve=_open_water_solve, read=_read_stratified, forcing_fields={}),
    "rainfall_runoff": _Process(
        domain_kw="catchment", domain_ref=Ref("mesh"),
        deck=RainOnGrid.deck, writer=write_rain_on_grid_deck,
        solve=_rain_on_grid_solve, read=_read_rain_on_grid,
        forcing_fields=_RAIN_FORCING,
        # The infiltration SURFACE is a mesh-node field, so it is a step result the
        # deck reads rather than a value the sheet carries - the same shape as the
        # reach family's mid-reach seed.
        extra_fields={"infiltration": Ref("infiltration")}),
}

#: Mesh SPEC fields a TELEMAC deck writer reads, under the name that writer knows
#: each by. The mesher's vocabulary is its library's and the deck's is TELEMAC's,
#: so the two are stated apart rather than forced to agree. A declared field
#: ABSENT from this table shapes the mesh and no deck: the deck records what the
#: mesh ask asked FOR, and the mesher answers for what it built.
_MESH_DECK_FIELDS: Mapping[str, str] = {
    "resolution_m": "mesh_resolution_m",
    "min_edge_length_m": "mesh_resolution_m",
}

#: The ``refine`` knobs a deck reads, by the same rule.
_MESH_DECK_REFINE: Mapping[str, str] = {
    "resolution_m": "mesh_resolution_m",
}

#: Which domain SHAPES ``acquire_domain`` knows, and what each one is for. A
#: reach is a corridor along a flowline; an open-water domain is a grid over an
#: AOI; a catchment is the terrain that drains to one point. The shape is the
#: template's declaration, never inferred from which slots happen to be filled.
_SHAPES = ("reach", "open_water", "catchment")


class TelemacWorkflow(Workflow):
    """TELEMAC: the reach and open-water pipelines behind five operations."""

    engine = "telemac2d"
    solve_step = "solve"

    # -- 1. acquire -------------------------------------------------------- #

    def acquire_domain(self, *, location: Any, bbox: Any, shape: str = "reach",
                       rivers: Any = None, discharge: Any = None,
                       event_time: Any = None, pour_point: Any = None,
                       seed_coords: Any = None,
                       aoi_half_deg: float | tuple[float, float] = 0.06,
                       aoi_name: str = "aoi",
                       code_prefix: str = "TELEMAC") -> tuple[Step, ...]:
        """The steps that establish the modeled world and its resolved state.

        Three domain SHAPES, declared rather than inferred:

        * ``reach`` - place -> reach centre -> the seed -> the carrier discharge
          AT that seed. Three steps because the modeled world is not established
          until the flow that carries everything through it is: the seed is the
          point the discharge is read at, so it cannot be declared independently
          of the reach. ``seed_coords`` pins that seed - a template whose ask
          names where the substance enters the water is naming which stretch to
          model, and the one centerline is navigated from there.
        * ``open_water`` - place or extent -> the AOI, and nothing else. A lake
          fetch and a harbour basin are bounded by the ask itself; there is no
          flowline to find and no carrier to resolve.
        * ``catchment`` - the OUTLET first, then the analysis window around it.
          The basin's shape is the terrain's answer rather than the geocoder's, so
          a place bbox cannot bound it: the AOI is derived from the pour point
          unless the caller drew one.

        New acquisition inputs arrive as keywords WITH DEFAULTS, so a template
        that does not want one is untouched.
        """
        if shape not in _SHAPES:
            raise PlanValidationError(
                f"acquire_domain shape {shape!r} is not a TELEMAC domain shape "
                f"(known: {list(_SHAPES)}).")
        if shape == "catchment":
            if isinstance(aoi_half_deg, (list, tuple)):
                raise PlanValidationError(
                    "a catchment AOI is a SQUARE buffer around the outlet, so "
                    "aoi_half_deg is one number; a (dlon, dlat) pair describes a "
                    "domain whose shape the ask decides, which a catchment's is not.")
            return (AcquireCatchment(location=location, bbox=bbox,
                                     pour_point=pour_point, half_deg=aoi_half_deg,
                                     default_name=aoi_name,
                                     code_prefix=code_prefix).named("aoi"),)
        if shape == "open_water":
            return (AcquireAoi(location=location, bbox=bbox, half_deg=aoi_half_deg,
                               default_name=aoi_name,
                               code_prefix=code_prefix).named("aoi"),)
        return (
            Geocode.reach(location, bbox).named("reach"),
            ReachSeed(reach=Ref("reach"), rivers=rivers,
                      supplied=seed_coords).named("seed"),
            CarrierDischarge(seed=Ref("seed"), explicit=discharge,
                             event_time=event_time).named("carrier_discharge"),
        )

    # -- 2. author --------------------------------------------------------- #

    def author(self, *, mesh: Any, physics: Physics, forcing: Forcing) -> Step:
        """Serialize the mesh ask + physics + forcing into the process's deck."""
        if not _is_mesh_declaration(mesh):
            raise PlanValidationError(
                "author needs the template's MESH declaration (tool.build_mesh(...)), "
                f"got {type(mesh).__name__}.")
        if not isinstance(physics, Physics):
            raise PlanValidationError(
                f"author needs a Physics slot, got {type(physics).__name__}.")
        if not isinstance(forcing, Forcing):
            raise PlanValidationError(
                f"author needs a Forcing slot, got {type(forcing).__name__}.")
        process = self._process(physics)
        fields: dict[str, Any] = {process.domain_kw: process.domain_ref}
        fields.update(dict(process.extra_fields or {}))
        fields.update(mesh_deck_fields(mesh))
        fields.update(_translate(forcing.values, process.forcing_fields))
        fields.update(physics.values)
        _refuse_uncovered_deck_fields(fields, process.writer, physics.process)
        return process.deck(**fields).named("deck")

    # -- 3. solve ---------------------------------------------------------- #

    def solve(self, *, compute_class: Any, physics: Physics) -> Step:
        """Dispatch the staged deck to the worker the declared process runs on.

        ``physics`` is the process SELECTOR and is required: it must never default,
        because a template that forgot to name its physics would otherwise
        dispatch an agitation or stratified deck to the wrong solver silently. A missing
        selector must raise, not fall back.
        """
        if physics is None:
            raise PlanValidationError(
                "solve needs the physics process that selects the worker; "
                f"none was named (known: {sorted(_PROCESSES)}).")
        return self._process(physics).solve(compute_class=compute_class).named("solve")

    # -- 4. read ----------------------------------------------------------- #

    def read(self, run: Any, *, physics: Physics, forcing: Forcing) -> Step:
        """The published deliverable the declared physics process asks for."""
        return self._process(physics).read(solve=run, physics=physics, forcing=forcing)

    # -- routing ----------------------------------------------------------- #

    @staticmethod
    def _process(physics: Any) -> _Process:
        name = getattr(physics, "process", "")
        found = _PROCESSES.get(name)
        if found is None:
            raise PlanValidationError(
                f"TELEMAC models no physics process {name!r} "
                f"(known: {sorted(_PROCESSES)}).")
        return found


def _is_mesh_declaration(value: Any) -> bool:
    """Is ``value`` a frozen ``tool.build_mesh`` ask?

    Imported where it is asked rather than at module scope: the mesh tool registers
    itself into the tool registry, which imports the templates that import this
    facade, and a module-level import would close that circle.
    """
    from trid3nt_server.workflows.mesh.tool import MeshDeclaration

    return isinstance(value, MeshDeclaration)


def mesh_deck_fields(mesh: Any) -> dict[str, Any]:
    """The declared mesh ask, as the deck keywords a TELEMAC writer reads.

    A field the template did not declare is ABSENT rather than ``None``: passing
    None would override the deck writer's own default with nothing, and "the
    template did not ask" is what absence means.
    """
    fields: dict[str, Any] = {}
    for name, value in mesh.spec.fields.items():
        if name == "refine":
            fields.update({_MESH_DECK_REFINE[knob]: knob_value
                           for knob, knob_value in dict(value or {}).items()
                           if knob in _MESH_DECK_REFINE and knob_value is not None})
        elif name in _MESH_DECK_FIELDS and value is not None:
            fields[_MESH_DECK_FIELDS[name]] = value
    return fields


def _translate(values: Mapping[str, Any], table: Mapping[str, str]) -> dict[str, Any]:
    """Rename slot members onto the deck's own keyword names."""
    return {table.get(k, k): v for k, v in values.items()}


def _refuse_uncovered_deck_fields(fields: Mapping[str, Any], writer: Callable[..., Any],
                                  process: str) -> None:
    """The slots and the deck writer's signature must AGREE, in both directions.

    The signature check has to run both ways or it only catches half the disease.
    An UNKNOWN key vanishes - the deck is written without it and the run answers a
    different question than the template declared. A MISSING required key is the
    mirror image and costs more: the plan builds, the acquire stage geocodes and
    fetches, and only then does the writer die on a TypeError, several minutes and
    three network round-trips after the declaration that was already wrong. Both
    are refused HERE, while the plan value is being built.
    """
    signature = inspect.signature(writer).parameters
    accepted = set(signature)
    unknown = sorted(set(fields) - accepted)
    if unknown:
        raise PlanValidationError(
            f"the {process!r} declaration names {unknown}, which "
            f"{writer.__name__} does not accept (accepted: {sorted(accepted)})."
        )
    required = sorted(
        name for name, prm in signature.items()
        if prm.kind is inspect.Parameter.KEYWORD_ONLY
        and prm.default is inspect.Parameter.empty
    )
    missing = [name for name in required if name not in fields]
    if missing:
        raise PlanValidationError(
            f"the {process!r} declaration covers no {missing}, which "
            f"{writer.__name__} requires (required: {required}). Declare the slot "
            "member that carries it - a Forcing without a `carrier`, for instance, "
            "leaves `carrier_discharge` unfilled."
        )
