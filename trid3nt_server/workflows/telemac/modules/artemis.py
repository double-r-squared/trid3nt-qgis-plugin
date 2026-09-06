"""The ARTEMIS wrapper: its catalog, its composite, and its output.

The wrapper asserts NO value of its own. The engine's default is its whole
position, and every opinion above it lives in a template.

What it holds beyond the catalog is the ONE keyword group an elliptic mild-slope
run cannot state a keyword at a time: the incident wave. ARTEMIS reads its forcing
out of the BOUNDARY CONDITIONS FILE rather than out of the steering file - the
same 13-column TELEMAC row, read under other names, columns 4 to 7 carrying HB,
TETAP, ALFAP and RP - so the wave height and the reflection coefficient are a FILE
this composite writes, and the steering file names it.

The composite RESTAMPS the pair the mesh recipe already wrote. It never walks the
boundary and never renumbers it: the rank and the node on every row are the
mesh's own, and what changes is the boundary TYPE and the four forcing columns.
A boundary walk authored twice is two boundaries that happen to agree.
"""

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


def IncidentWave(*, cli_text: Any, open_nodes: Any,  # noqa: N802 - a value constructor
                 structure_nodes: Any, height_m: Any,
                 reflection_coef: Any) -> Mapping[str, Any]:
    """The wave the domain is forced with, and which faces it enters through.

    ``cli_text`` is the boundary file the mesh recipe wrote from this geometry's
    own IPOBO. ``open_nodes`` is the stretch the mesh designated liquid, and
    ``structure_nodes`` the boundary the declared structure runs along - both
    measured against the accepted mesh, because which node is which is a fact
    about the domain rather than about the wave.

    A MAPPING and not an object, because the sheet's one ref walk descends
    mappings: a late-bound read inside it is bound by fill before the composite
    ever expands it.
    """
    return MappingProxyType({"cli_text": cli_text, "open_nodes": open_nodes,
                             "structure_nodes": structure_nodes,
                             "height_m": height_m,
                             "reflection_coef": reflection_coef})


def _incident_wave(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                      Mapping[str, Any]]:
    """The incident wave -> the CONTENT of the boundary file that carries it.

    ARTEMIS reads the forcing out of the boundary file rather than out of the
    deck, so what this value stands for is that file's rows. The keyword that
    NAMES the file is the template's own statement, beside the geometry it is
    the boundary of - a file slot a composite filled would read as an open
    mandatory slot everywhere the declaration is shown.
    """
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

    The rank and the node on every row are read back and written back unchanged -
    the last two columns ARE the walk, and rewriting them would describe a
    boundary no geometry produced. What is restamped is the type and the four
    forcing columns:

      * the designated liquid stretch becomes KINC with HB = the incident height,
        which is the edge the wave enters through and the scattered field leaves
        through;
      * a solid face the declared structure runs along becomes KLOG with the
        structure's own RP, so the barrier reflects the fraction it was declared
        to;
      * every other solid face is KLOG with RP 0 - the absorbing shore, which is
        what keeps a complex coastline from ringing the whole basin.

    TETAP is the BOUNDARY TANGENT and not the wave direction; the incident
    direction lives only in DIRECTION OF WAVE PROPAGATION, so it is 0 everywhere,
    which is what the proven supplied-mesh arm wrote.

    The structure wins a contested node: a barrier face that imposed the incident
    wave would radiate the sheltering away from inside the lee.
    """
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
    for rank, node in rows:
        if node - 1 in solid:
            lihbor, hb, rp = KLOG, 0.0, float(reflection_coef)
        elif node - 1 in liquid:
            lihbor, hb, rp = KINC, float(height_m), 0.0
        else:
            lihbor, hb, rp = KLOG, 0.0, 0.0
        lines.append(
            f"{lihbor} {_LIQUID_VELOCITY_CODE} {_LIQUID_VELOCITY_CODE}  "
            f"{hb:.4f} 0.000 0.000 {rp:.3f}  {_TRACER_CODE}  "
            f"0.000 0.000 0.000  {node:>11d} {rank:>11d}")
    return "\n".join(lines) + "\n"


ART = Module("artemis")
ART.composites(incident_wave=_incident_wave)
ART.outputs(agitation=publish_agitation_products)
