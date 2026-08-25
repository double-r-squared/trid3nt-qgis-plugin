"""``TelemacWorkflow`` - the TELEMAC engine facade.

Five operations, and nothing else. Behind them sit the shared TELEMAC step family
(``workflows/telemac/steps``): the geocode/seed acquisition, the corridor mesher
that currently lives inside the deck writer, the deck serializer, the solver
dispatch and the per-deliverable readers. The facade is what stays still while
those move: when the shared mesh front replaces the private corridor mesher,
``build_mesh`` keeps its shape and no template changes.

Named by ENGINE only. A domain qualifier here would weld a domain assumption into
the engine; the domain arrives through ``acquire_domain``'s slots.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Mapping

from trid3nt_server.workflows.lib import (
    Forcing,
    MeshPolicy,
    Physics,
    PlanValidationError,
    Ref,
    Step,
    Workflow,
)
from trid3nt_server.workflows.telemac.steps import (
    CarrierDischarge,
    Geocode,
    Products,
    ReachSeed,
    Solve,
    WriteDeck,
    write_reach_deck,
)

__all__ = ["CorridorPolicy", "MeshHandle", "TelemacWorkflow"]

#: Which deliverable family reads each declared physics process. A process the
#: facade does not know REFUSES at plan construction rather than solving into a
#: reader that cannot describe it.
_READERS: dict[str, str] = {
    "tracer": "dye",
    "morphodynamics": "dye",
    "waqtel_o2": "dissolved_oxygen",
}

#: The universal SIZING ask, translated into the deck fields the corridor mesher
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

#: The forcing slot, translated into the deck's own forcing keywords.
_FORCING_FIELDS: dict[str, str] = {"carrier": "carrier_discharge", "rain": "rain"}


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

    A HANDLE, not a mesh: TELEMAC's corridor mesher runs inside the deck writer
    and the worker, so the mesh comes into being during ``author``. The handle is
    what keeps the interface honest while that stays true - a shared-front mesh
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
        fields = {deck: getattr(self.policy, slot)
                  for slot, deck in _SIZING_FIELDS.items()}
        fields.update({deck: getattr(self.corridor, slot)
                       for slot, deck in _CORRIDOR_FIELDS.items()})
        return fields


class TelemacWorkflow(Workflow):
    """TELEMAC-2D: the reach pipeline behind five operations."""

    engine = "telemac2d"
    solve_step = "solve"

    # -- 1. acquire -------------------------------------------------------- #

    def acquire_domain(self, *, location: Any, bbox: Any, rivers: Any,
                       discharge: Any = None,
                       event_time: Any = None) -> tuple[Step, ...]:
        """Place -> reach -> mid-reach seed -> the carrier discharge at that seed.

        Three steps because the modeled world is not established until the flow
        that carries everything through it is: the seed is the point the discharge
        is read AT, so it cannot be declared independently of the reach.
        """
        return (
            Geocode.reach(location, bbox).named("reach"),
            ReachSeed(reach=Ref("reach"), rivers=rivers).named("seed"),
            CarrierDischarge(seed=Ref("seed"), explicit=discharge,
                             event_time=event_time).named("carrier_discharge"),
        )

    # -- 2. mesh ----------------------------------------------------------- #

    def build_mesh(self, domain: Any, policy: MeshPolicy,
                   *, corridor: CorridorPolicy | None = None) -> MeshHandle:
        """The reach mesh for an acquired domain: neutral sizing + corridor shape."""
        return MeshHandle(domain, policy,
                          CorridorPolicy() if corridor is None else corridor)

    # -- 3. author --------------------------------------------------------- #

    def author(self, *, mesh: MeshHandle, physics: Physics,
               forcing: Forcing) -> Step:
        """Serialize mesh + physics + forcing into the TELEMAC-2D reach deck."""
        if not isinstance(mesh, MeshHandle):
            raise PlanValidationError(
                f"author needs the mesh build_mesh produced, got {type(mesh).__name__}.")
        if not isinstance(physics, Physics):
            raise PlanValidationError(
                f"author needs a Physics slot, got {type(physics).__name__}.")
        if not isinstance(forcing, Forcing):
            raise PlanValidationError(
                f"author needs a Forcing slot, got {type(forcing).__name__}.")
        if physics.process not in _READERS:
            raise PlanValidationError(
                f"TELEMAC authors no physics process {physics.process!r} "
                f"(known: {sorted(_READERS)}).")
        fields: dict[str, Any] = {"reach": mesh.domain, "seed": Ref("seed")}
        fields.update(mesh.deck_fields())
        fields.update(_translate(forcing.values, _FORCING_FIELDS))
        fields.update(physics.values)
        _refuse_uncovered_deck_fields(fields, physics.process)
        return WriteDeck.telemac(**fields).named("deck")

    # -- 4. solve ---------------------------------------------------------- #

    def solver_spec(self, *, compute_class: Any) -> Step:
        """Dispatch the staged deck to the TELEMAC worker under the shared supervisor."""
        return Solve.telemac(deck=Ref("deck"), compute_class=compute_class) \
                    .named("solve")

    # -- 5. read ----------------------------------------------------------- #

    def read_results(self, run: Any, *, physics: Physics,
                     forcing: Forcing) -> Step:
        """The published deliverable the declared physics process asks for."""
        reader = _READERS.get(getattr(physics, "process", ""))
        if reader is None:
            raise PlanValidationError(
                f"TELEMAC reads no results for physics process "
                f"{getattr(physics, 'process', None)!r}.")
        carrier = forcing.values.get("carrier")
        if reader == "dissolved_oxygen":
            return Products.dissolved_oxygen(
                deck=Ref("deck"), solve=run,
                process=physics.values.get("do_sag_config"),
                carrier_discharge=carrier).named("do_field")
        return Products.dye(deck=Ref("deck"), solve=run,
                            carrier_discharge=carrier).named("plume")


def _translate(values: Mapping[str, Any], table: Mapping[str, str]) -> dict[str, Any]:
    """Rename slot members onto the deck's own keyword names."""
    return {table.get(k, k): v for k, v in values.items()}


def _refuse_uncovered_deck_fields(fields: Mapping[str, Any], process: str) -> None:
    """The slots and the deck writer's signature must AGREE, in both directions.

    The signature check has to run both ways or it only catches half the disease.
    An UNKNOWN key vanishes - the deck is written without it and the run answers a
    different question than the template declared. A MISSING required key is the
    mirror image and costs more: the plan builds, the acquire stage geocodes and
    fetches, and only then does ``write_reach_deck`` die on a TypeError, several
    minutes and three network round-trips after the declaration that was already
    wrong. Both are refused HERE, while the plan value is being built.
    """
    signature = inspect.signature(write_reach_deck).parameters
    accepted = set(signature)
    unknown = sorted(set(fields) - accepted)
    if unknown:
        raise PlanValidationError(
            f"the {process!r} declaration names {unknown}, which the TELEMAC deck "
            f"writer does not accept (accepted: {sorted(accepted)})."
        )
    required = sorted(
        name for name, prm in signature.items()
        if prm.kind is inspect.Parameter.KEYWORD_ONLY
        and prm.default is inspect.Parameter.empty
    )
    missing = [name for name in required if name not in fields]
    if missing:
        raise PlanValidationError(
            f"the {process!r} declaration covers no {missing}, which the TELEMAC "
            f"deck writer requires (required: {required}). Declare the slot member "
            "that carries it - a Forcing without a `carrier`, for instance, leaves "
            "`carrier_discharge` unfilled."
        )
