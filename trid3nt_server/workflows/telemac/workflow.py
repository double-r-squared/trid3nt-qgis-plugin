"""``TelemacWorkflow`` - the TELEMAC engine facade.

Five operations, and nothing else. Behind them sit the TELEMAC step families
(``workflows/telemac/steps``): the reach front (geocode, flowline, seed, corridor
deck, reach solve, per-deliverable readers) and the open-water front (an AOI, a
regular grid over it, one worker section, one result SELAFIN). The facade is what
stays still while those move: when the shared mesh front replaces the private
corridor mesher, ``build_mesh`` keeps its shape and no template changes.

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
    Forcing,
    MeshPolicy,
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
    Coastal,
    Geocode,
    Products,
    RainOnGrid,
    ReachSeed,
    Solve,
    SolveOpenWater,
    SolveRainOnGrid,
    Stratified,
    Wave,
    WriteDeck,
    write_agitation_deck,
    write_coastal_deck,
    write_rain_on_grid_deck,
    write_reach_deck,
    write_stratified_deck,
    write_wave_deck,
)

__all__ = ["CorridorPolicy", "MeshHandle", "TelemacWorkflow"]


@dataclass(frozen=True, slots=True)
class _Process:
    """One declared physics process, end to end.

    The facade routes on ``Physics.process``, and this is what it routes TO: the
    deck step that serializes it, the writer whose real signature the slots are
    checked against in both directions, the solve that dispatches it, and the
    reader that turns the solved file into the question's deliverable. Adding a
    TELEMAC domain is a row here plus its two runners - never a branch in the five
    operations.
    """

    #: What the deck writer calls the acquired domain (``reach`` / ``aoi``).
    domain_kw: str
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


def _read_coastal(*, solve: Any, physics: Physics, forcing: Forcing) -> Step:
    return Coastal.products(deck=Ref("deck"), solve=solve).named("inundation")


def _read_wave(*, solve: Any, physics: Physics, forcing: Forcing) -> Step:
    return Wave.products(deck=Ref("deck"), solve=solve).named("wave_field")


def _read_agitation(*, solve: Any, physics: Physics, forcing: Forcing) -> Step:
    return Agitation.products(deck=Ref("deck"), solve=solve).named("agitation")


def _read_stratified(*, solve: Any, physics: Physics, forcing: Forcing) -> Step:
    return Stratified.products(deck=Ref("deck"), solve=solve).named("column")


_REACH_FORCING: Mapping[str, str] = {"carrier": "carrier_discharge", "rain": "rain"}
_COASTAL_FORCING: Mapping[str, str] = {"water_level": "water_level"}
_RAIN_FORCING: Mapping[str, str] = {"rain": "rain"}

#: The known TELEMAC physics processes. See :class:`_Process`.
_PROCESSES: dict[str, _Process] = {
    "tracer": _Process(
        domain_kw="reach", deck=WriteDeck.telemac, writer=write_reach_deck,
        solve=_reach_solve, read=_read_dye, forcing_fields=_REACH_FORCING,
        extra_fields={"seed": Ref("seed")}),
    "morphodynamics": _Process(
        domain_kw="reach", deck=WriteDeck.telemac, writer=write_reach_deck,
        solve=_reach_solve, read=_read_dye, forcing_fields=_REACH_FORCING,
        extra_fields={"seed": Ref("seed")}),
    "waqtel_o2": _Process(
        domain_kw="reach", deck=WriteDeck.telemac, writer=write_reach_deck,
        solve=_reach_solve, read=_read_dissolved_oxygen,
        forcing_fields=_REACH_FORCING, extra_fields={"seed": Ref("seed")}),
    "coastal_surge": _Process(
        domain_kw="aoi", deck=Coastal.deck, writer=write_coastal_deck,
        solve=_open_water_solve, read=_read_coastal,
        forcing_fields=_COASTAL_FORCING),
    "wave_spectrum": _Process(
        domain_kw="aoi", deck=Wave.deck, writer=write_wave_deck,
        solve=_open_water_solve, read=_read_wave, forcing_fields={}),
    "harbor_agitation": _Process(
        domain_kw="aoi", deck=Agitation.deck, writer=write_agitation_deck,
        solve=_open_water_solve, read=_read_agitation, forcing_fields={}),
    "stratified_3d": _Process(
        domain_kw="aoi", deck=Stratified.deck, writer=write_stratified_deck,
        solve=_open_water_solve, read=_read_stratified, forcing_fields={}),
    "rainfall_runoff": _Process(
        domain_kw="catchment", deck=RainOnGrid.deck, writer=write_rain_on_grid_deck,
        solve=_rain_on_grid_solve, read=_read_rain_on_grid,
        forcing_fields=_RAIN_FORCING,
        # The infiltration SURFACE is a mesh-node field, so it is a step result the
        # deck reads rather than a value the sheet carries - the same shape as the
        # reach family's mid-reach seed.
        extra_fields={"infiltration": Ref("infiltration")}),
}

#: The universal SIZING ask, translated into the deck fields a TELEMAC writer
#: reads. The shared mesh front will consume the MeshPolicy directly and this
#: table goes.
_SIZING_FIELDS: dict[str, str] = {
    "resolution": "mesh_resolution",
    "target_edge_m": "mesh_resolution_m",
}

#: The corridor SHAPE ask, same translation, engine-owned rather than universal.
_CORRIDOR_FIELDS: dict[str, str] = {
    "extent_km": "reach_length_km",
    "width_m": "channel_width_m",
    "boundary_source": "bank_source",
}

#: Which domain SHAPES ``acquire_domain`` knows, and what each one is for. A
#: reach is a corridor along a flowline; an open-water domain is a grid over an
#: AOI; a catchment is the terrain that drains to one point. The shape is the
#: template's declaration, never inferred from which slots happen to be filled.
_SHAPES = ("reach", "open_water", "catchment")


@dataclass(frozen=True, slots=True)
class CorridorPolicy:
    """The CORRIDOR-shaped part of a mesh ask, owned by the facade that meshes one.

    A length along the flow axis, a cross-stream width and where the banks come
    from describe one domain SHAPE, not every shape - so by the placement rule
    they sit here, beside the only mesher that reads them, rather than in the
    universal :class:`MeshPolicy`. A future basin or coastal facade declares its
    own shape policy; neither has to carry the other's fields.
    """

    #: How far the modeled corridor runs, along its principal (flow) axis.
    extent_km: Any = None
    #: The modeled cross-stream width of the corridor.
    width_m: Any = None
    #: Where the corridor BOUNDARY comes from (real polygons vs an assumed ribbon).
    boundary_source: Any = None


class MeshHandle:
    """What ``build_mesh`` yields for TELEMAC: the domain, and the ask over it.

    A HANDLE, not a mesh: TELEMAC's meshers run inside the deck writer and the
    worker, so the mesh comes into being during ``author``. The handle is what
    keeps the interface honest while that stays true - a shared-front mesh
    artifact will arrive in the same slot without the template noticing. Named for
    what it IS rather than for the domain it happens to describe; the domain word
    lives on the :class:`CorridorPolicy` it carries.
    """

    __slots__ = ("domain", "policy", "corridor")

    def __init__(self, domain: Any, policy: MeshPolicy,
                 corridor: CorridorPolicy) -> None:
        if not isinstance(policy, MeshPolicy):
            raise PlanValidationError(
                f"build_mesh needs a MeshPolicy, got {type(policy).__name__}.")
        if not isinstance(corridor, CorridorPolicy):
            raise PlanValidationError(
                "TELEMAC's build_mesh needs a CorridorPolicy in its `corridor` slot, "
                f"got {type(corridor).__name__}.")
        self.domain = domain
        self.policy = policy
        self.corridor = corridor

    def deck_fields(self) -> dict[str, Any]:
        """The mesh ask as deck keywords - only the members the template DECLARED.

        An undeclared member is ABSENT rather than ``None``: passing None would
        override the deck writer's own default with nothing, and a grid domain
        that declares no corridor would silently null out a corridor writer's
        length and width. Absence lets each writer's declared default stand, which
        is what "the template did not ask" means.
        """
        fields = {deck: getattr(self.policy, slot)
                  for slot, deck in _SIZING_FIELDS.items()
                  if getattr(self.policy, slot) is not None}
        fields.update({deck: getattr(self.corridor, slot)
                       for slot, deck in _CORRIDOR_FIELDS.items()
                       if getattr(self.corridor, slot) is not None})
        return fields


class TelemacWorkflow(Workflow):
    """TELEMAC: the reach and open-water pipelines behind five operations."""

    engine = "telemac2d"
    solve_step = "solve"

    # -- 1. acquire -------------------------------------------------------- #

    def acquire_domain(self, *, location: Any, bbox: Any, shape: str = "reach",
                       rivers: Any = None, discharge: Any = None,
                       event_time: Any = None, pour_point: Any = None,
                       aoi_half_deg: float | tuple[float, float] = 0.06,
                       aoi_name: str = "aoi",
                       code_prefix: str = "TELEMAC") -> tuple[Step, ...]:
        """The steps that establish the modeled world and its resolved state.

        Three domain SHAPES, declared rather than inferred:

        * ``reach`` - place -> reach centre -> mid-reach seed -> the carrier
          discharge AT that seed. Three steps because the modeled world is not
          established until the flow that carries everything through it is: the
          seed is the point the discharge is read at, so it cannot be declared
          independently of the reach.
        * ``open_water`` - place or extent -> the AOI, and nothing else. A coastal
          strip, a lake fetch and a harbour basin are all bounded by the ask
          itself; there is no flowline to find and no carrier to resolve.
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
            ReachSeed(reach=Ref("reach"), rivers=rivers).named("seed"),
            CarrierDischarge(seed=Ref("seed"), explicit=discharge,
                             event_time=event_time).named("carrier_discharge"),
        )

    # -- 2. mesh ----------------------------------------------------------- #

    def build_mesh(self, domain: Any, policy: MeshPolicy,
                   *, corridor: CorridorPolicy | None = None) -> MeshHandle:
        """The mesh for an acquired domain: neutral sizing + optional domain shape."""
        return MeshHandle(domain, policy,
                          CorridorPolicy() if corridor is None else corridor)

    # -- 3. author --------------------------------------------------------- #

    def author(self, *, mesh: MeshHandle, physics: Physics,
               forcing: Forcing) -> Step:
        """Serialize mesh + physics + forcing into the deck the process asks for."""
        if not isinstance(mesh, MeshHandle):
            raise PlanValidationError(
                f"author needs the mesh build_mesh produced, got {type(mesh).__name__}.")
        if not isinstance(physics, Physics):
            raise PlanValidationError(
                f"author needs a Physics slot, got {type(physics).__name__}.")
        if not isinstance(forcing, Forcing):
            raise PlanValidationError(
                f"author needs a Forcing slot, got {type(forcing).__name__}.")
        process = self._process(physics)
        fields: dict[str, Any] = {process.domain_kw: mesh.domain}
        fields.update(dict(process.extra_fields or {}))
        fields.update(mesh.deck_fields())
        fields.update(_translate(forcing.values, process.forcing_fields))
        fields.update(physics.values)
        _refuse_uncovered_deck_fields(fields, process.writer, physics.process)
        return process.deck(**fields).named("deck")

    # -- 4. solve ---------------------------------------------------------- #

    def solver_spec(self, *, compute_class: Any, physics: Physics) -> Step:
        """Dispatch the staged deck to the worker the declared process runs on.

        ``physics`` is the process SELECTOR and is required. It was once defaulted
        to the tracer (reach) process for the templates that predate the open-water
        front, which meant a template that forgot to name its physics dispatched a
        coastal or wave deck to the reach solver - a wrong worker, silently, rather
        than a plan that refuses to build.
        """
        if physics is None:
            raise PlanValidationError(
                "solver_spec needs the physics process that selects the worker; "
                f"none was named (known: {sorted(_PROCESSES)}).")
        return self._process(physics).solve(compute_class=compute_class).named("solve")

    # -- 5. read ----------------------------------------------------------- #

    def read_results(self, run: Any, *, physics: Physics,
                     forcing: Forcing) -> Step:
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
