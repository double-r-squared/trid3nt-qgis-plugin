"""The ARTEMIS wrapper: its dictionary, its composite, and its outputs.

ARTEMIS reads its forcing from the BOUNDARY CONDITIONS FILE, so the incident wave is a file this
composite writes and the steering file names; the coefficient KD is wave height over the stamped incident height.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence


from .module import Module, Output
from .outputs import PRIMITIVES

__all__ = ["ART", "BOUNDARY_FILENAME", "IncidentWave", "MODULE_OUTPUT",
           "incident_height", "stamp_boundary_rows"]

# What the module writes, by VARIABLES FOR GRAPHIC PRINTOUTS mnemonic. A mild-slope solve is steady (one frame); ``KD`` is derived over them and never asked of the engine.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "HS": Output("WAVE HEIGHT", "m", varies=False,
                 style={"kind": "mesh", "ramp": "ylgnbu", "units": "m",
                        "floor": 0}),
    "PHAS": Output("WAVE PHASE", "rad", varies=False,
                   style={"kind": "mesh", "ramp": "hsv", "units": "rad"}),
    "ZS": Output("FREE SURFACE", "m", varies=False,
                 style={"kind": "mesh", "ramp": "blues", "units": "m"}),
    "ZF": Output("BOTTOM", "m", varies=False,
                 style={"kind": "mesh", "ramp": "terrain", "units": "m"}),
    # KD is a dimensionless ratio; the legend caps at the 99.5th percentile because the standing wave at the open boundary sets the maximum and would paint the interior one colour.
    "KD": Output("KD", "Hs/H0", varies=False,
                 style={"kind": "mesh", "range": "p99.5"}),
})

# The restamped boundary file's run-directory name, the deck's BOUNDARY CONDITIONS FILE statement.
BOUNDARY_FILENAME = "artemis.cli"

# ARTEMIS boundary types in column 1: INCIDENT admits the wave and radiates the scattered field; SOLID reflects the fraction its RP states.
KINC, KLOG = 1, 2

# LIUBOR / LIVBOR under both types: the mild-slope solve reads no velocity condition; the pair writer's 5 is ignored.
_LIQUID_VELOCITY_CODE = 5
# LITBOR: ARTEMIS carries no tracer, so the wall value the pair writer uses is written.
_TRACER_CODE = 2


# ``open_nodes`` and ``structure_nodes`` are measured against the accepted mesh; ``cli_text`` is from this geometry's IPOBO.
def IncidentWave(*, measured: Any, height_m: Any,  # noqa: N802 - a value constructor
                 reflection_coef: Any) -> Mapping[str, Any]:
    """The wave the domain is forced with, and which faces it enters through.

    ``measured`` names what the workflow measures off the accepted harbour mesh.
    """
    return MappingProxyType({"measured": measured, "height_m": height_m,
                             "reflection_coef": reflection_coef})


def _incident_wave(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                      Mapping[str, Any]]:
    """The incident wave -> the content of the boundary file carrying it; the keyword naming the file is the template's."""
    harbour = value["measured"]
    return ({},
            {BOUNDARY_FILENAME: stamp_boundary_rows(
                str(harbour["cli_text"]),
                open_nodes=harbour["open_nodes"],
                structure_nodes=harbour["structure_nodes"],
                height_m=float(value["height_m"]),
                reflection_coef=float(value["reflection_coef"]))})


def stamp_boundary_rows(cli_text: str, *, open_nodes: Sequence[int],
                        structure_nodes: Sequence[int], height_m: float,
                        reflection_coef: float) -> str:
    """The mesh's boundary file, restamped as ARTEMIS reads it.

    Rank and node are written back unchanged: the last two columns are the walk.
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
    # The structure wins a contested node (an incident-wave barrier would radiate the sheltering away); every other solid face is KLOG with RP 0, the absorbing shore.
    for rank, node in rows:
        if node - 1 in solid:
            lihbor, hb, rp = KLOG, 0.0, float(reflection_coef)
        elif node - 1 in liquid:
            lihbor, hb, rp = KINC, float(height_m), 0.0
        else:
            lihbor, hb, rp = KLOG, 0.0, 0.0
        # Columns 4 to 7 are HB, TETAP, ALFAP, RP. TETAP is the boundary tangent, not the wave direction (that is DIRECTION OF WAVE PROPAGATION), so 0 on every row.
        lines.append(
            f"{lihbor} {_LIQUID_VELOCITY_CODE} {_LIQUID_VELOCITY_CODE}  "
            f"{hb:.4f} 0.000 0.000 {rp:.3f}  {_TRACER_CODE}  "
            f"0.000 0.000 0.000  {node:>11d} {rank:>11d}")
    return "\n".join(lines) + "\n"


def incident_height(cli_text: str) -> float:
    """The incident height the boundary file stamps on its KINC rows, in metres.

    One monochromatic wave: a file stamping several or none has no incident wave to measure KD against.
    """
    heights = set()
    for line in cli_text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and int(parts[0]) == KINC:
            heights.add(float(parts[3]))
    if len(heights) != 1:
        raise ValueError(
            f"the boundary file stamps {sorted(heights)} as incident heights; the "
            "coefficient KD is measured against exactly one.")
    return heights.pop()


def _kd(solved: Any) -> tuple[str, str, Any]:
    from trid3nt_server.workflows.solver.solver import download_result

    local = download_result(solved.run_id, BOUNDARY_FILENAME)
    try:
        h0 = incident_height(Path(local).read_text(errors="replace"))
    finally:
        Path(local).unlink(missing_ok=True)
    _name, _units, hs = solved.frames("HS", None)
    return "KD", "Hs/H0", hs / h0


ART = Module("artemis")
ART.MODULE_OUTPUT = MODULE_OUTPUT
ART.DERIVED = MappingProxyType({"KD": _kd})
ART.PRINTOUTS = "VARIABLES_FOR_GRAPHIC_PRINTOUTS"
ART.composites(reads={"incident_wave": ("measured", "height_m",
                                         "reflection_coef")},
               incident_wave=_incident_wave)
ART.reads(**PRIMITIVES)
