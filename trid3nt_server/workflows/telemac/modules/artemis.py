"""The ARTEMIS wrapper: its catalog, its composite, and its output.

The wrapper asserts NO value of its own. ARTEMIS reads its forcing out of the
BOUNDARY CONDITIONS FILE rather than out of the steering file, so the incident
wave is a FILE this composite writes and the steering file names."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..products.agitation import publish_agitation_products
from .module import Module

__all__ = ["ART", "BOUNDARY_FILENAME", "IncidentWave", "stamp_boundary_rows"]

#: What the restamped boundary file is called in the run directory. The deck's own
#: BOUNDARY CONDITIONS FILE statement, so the steering file reads as the record of
#: the run it is.
BOUNDARY_FILENAME = "artemis.cli"

#: ARTEMIS's own boundary types in the first column: an INCIDENT wave enters the
#: domain here and the scattered field radiates out through the same face; a SOLID
#: face reflects whatever fraction its RP states.
KINC, KLOG = 1, 2

#: LIUBOR / LIVBOR under both types. The mild-slope solve reads no velocity
#: condition; the pair writer's own rows carry 5 there and ARTEMIS is indifferent.
_LIQUID_VELOCITY_CODE = 5
#: LITBOR. ARTEMIS carries no tracer, so the tracer code and its three columns are
#: written as the wall value the pair writer already uses.
_TRACER_CODE = 2


# ``cli_text`` is the boundary file the mesh recipe wrote from this geometry's own
# IPOBO; ``open_nodes`` and ``structure_nodes`` are measured against the accepted
# mesh, because which node is which is a fact about the domain, not the wave.
def IncidentWave(*, cli_text: Any, open_nodes: Any,  # noqa: N802 - a value constructor
                 structure_nodes: Any, height_m: Any,
                 reflection_coef: Any) -> Mapping[str, Any]:
    """The wave the domain is forced with, and which faces it enters through.

    A MAPPING, not an object: the sheet's one ref walk descends mappings."""
    return MappingProxyType({"cli_text": cli_text, "open_nodes": open_nodes,
                             "structure_nodes": structure_nodes,
                             "height_m": height_m,
                             "reflection_coef": reflection_coef})


def _incident_wave(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                      Mapping[str, Any]]:
    """The incident wave -> the CONTENT of the boundary file that carries it.

    The keyword that NAMES the file stays the template's own statement."""
    return ({},
            {BOUNDARY_FILENAME: stamp_boundary_rows(
                str(value["cli_text"]),
                open_nodes=value["open_nodes"],
                structure_nodes=value["structure_nodes"],
                height_m=float(value["height_m"]),
                reflection_coef=float(value["reflection_coef"]))})


def stamp_boundary_rows(cli_text: str, *, open_nodes: Sequence[int],
                        structure_nodes: Sequence[int], height_m: float,
                        reflection_coef: float) -> str:
    """The mesh's boundary file, restamped as ARTEMIS reads it.

    Rank and node are written back unchanged: the last two columns ARE the walk."""
    liquid = {int(n) for n in (open_nodes or ())}
    solid = {int(n) for n in (structure_nodes or ())}
    rows: list[tuple[int, int]] = []
    for line in cli_text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        rows.append((int(parts[-1]), int(parts[-2])))
    if not rows:
        raise ValueError(
            "the mesh's boundary file holds no rows, so there is no boundary to "
            "force an incident wave through.")
    rows.sort()
    ranks = [rank for rank, _ in rows]
    if ranks != list(range(1, len(rows) + 1)):
        raise ValueError(
            f"the mesh's boundary file ranks {ranks[:5]}... are not a permutation "
            f"of 1..{len(rows)}; TELEMAC numbers its boundary once.")
    lines: list[str] = []
    # The structure WINS a contested node: a barrier face that imposed the
    # incident wave would radiate the sheltering away from inside the lee. Every
    # other solid face is KLOG with RP 0 - the absorbing shore, which is what
    # keeps a complex coastline from ringing the whole basin.
    for rank, node in rows:
        if node - 1 in solid:
            lihbor, hb, rp = KLOG, 0.0, float(reflection_coef)
        elif node - 1 in liquid:
            lihbor, hb, rp = KINC, float(height_m), 0.0
        else:
            lihbor, hb, rp = KLOG, 0.0, 0.0
        # Columns 4 to 7 are HB, TETAP, ALFAP and RP. TETAP is the BOUNDARY
        # TANGENT and not the wave direction - the incident direction lives only
        # in DIRECTION OF WAVE PROPAGATION - so it is 0 on every row.
        lines.append(
            f"{lihbor} {_LIQUID_VELOCITY_CODE} {_LIQUID_VELOCITY_CODE}  "
            f"{hb:.4f} 0.000 0.000 {rp:.3f}  {_TRACER_CODE}  "
            f"0.000 0.000 0.000  {node:>11d} {rank:>11d}")
    return "\n".join(lines) + "\n"


ART = Module("artemis")
ART.composites(incident_wave=_incident_wave)
ART.outputs(agitation=publish_agitation_products)
