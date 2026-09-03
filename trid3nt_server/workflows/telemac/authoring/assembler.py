"""The approved sheet + the accepted mesh -> the run directory the box receives.

ONE flow for every TELEMAC simulation the server authors. The sheet is
serialized into the engine's own steering files, every field those files name is
written beside them, the whole directory is uploaded, and the manifest that says
which engine reads which file is written last. What travels to the worker is
therefore the mesh, the authored steering files and the files they name; nothing
the container receives is a knob it has to interpret.

What differs between families is DATA, not a branch: :data:`_RECIPES` keys each
family to its engine class, the names its run directory holds its files under,
and the writer that fills it. The two entry points below prepare their own sheet
- a reach settles a release point and classifies a substance, a catchment
summons an infiltration surface and a storm - and hand it to the same
:func:`_assemble`, which is where every run becomes a staged one.

Every optional block is threaded ONLY when it was asked for, so a run that does
not use a module leaves the steering file byte-identical to the historical one.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.lib import Step
from trid3nt_server.workflows.mesh.shared.nodes import (
    read_accepted_mesh_nodes,
    read_centerline_utm,
)
from trid3nt_server.workflows.mesh.topology import read_topology

from . import author
from .open_water import OpenWaterError, case_section, stage_telemac_manifest
from ..helpers.catchment import mesh_nodes
from ..helpers.errors import (
    RainOnGridError,
    TelemacDyeScenarioError,
    TelemacDyeScenarioInputError,
)
from ..helpers.reach import MESH_H_FLOOR_M, coerce_lonlat_point, suggest_time_step_s
from ..helpers.substance import (
    arm_sediment_modules,
    classify_substance,
    resolve_decay_law,
    resolve_grain,
    sanitize_substance,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.authoring.assembler")

__all__ = ["Assemble", "assemble_rain_on_grid", "assemble_reach"]

_AUTHORING = "trid3nt_server.workflows.telemac.authoring"

#: The names the run directory holds a REACH's files under. They are the
#: ``.cas``'s own GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements, so the
#: steering file reads as the record of the run it is.
_REACH_GEOMETRY = "river.slf"
_REACH_BOUNDARY = "river.cli"
_REACH_STEERING = "t2d_river.cas"
_REACH_RESULT = "r2d_river.slf"
#: The engine's perfect-restart record - the full state at the last time step in
#: double precision - which is what a continuation reads. The graphic results
#: file is a picture of the run; this is the run's last instant.
_RESTART = "restart_river.slf"
#: What a continued run's PREVIOUS COMPUTATION FILE is called in the run
#: directory. The engine reads a file, not a URI, so the previous run's restart
#: record is staged under one name and the steering file names that.
_PREVIOUS_DEST = "previous.slf"

#: The same four names for a CATCHMENT.
_ROG_GEOMETRY = "rog.slf"
_ROG_BOUNDARY = "rog.cli"
_ROG_STEERING = "t2d_rog.cas"
_ROG_RESULT = "r2d_rog.slf"

#: The mesh boundary ROLE a catchment's outlet carries. The quad prescribes a
#: LEVEL, and this run writes no value at its number, so the level is the boundary
#: file's own zero - a zero-depth clamp, measured holding the outlet nodes at
#: 0.0000 m for every frame of a design storm. The recipe that paints it says so
#: out loud and names why the free exit is not the swap: an all-KSORT quad is
#: ill-posed the moment flow enters, and the engine refuses it by name. The
#: hydrograph is the flux across the nodes that took the role either way.
_OUTLET_ROLE = "outflow"

#: The bed-load transport laws GAIA can run with suspension off. Anything else
#: (Engelund-Hansen total load etc.) falls back to the default rather than
#: wedging the solve.
_BEDLOAD_FORMULAE = (1, 2, 7)
#: The friction laws the steering file interprets ``friction_coefficient`` under:
#: 2 = Chezy, 3 = Strickler, 4 = Manning.
_FRICTION_LAWS = (2, 3, 4)
_DREDGE_MODES = ("scheduled", "criterion")

#: Which TELEMAC module the class COUPLES the solve with. The author writes the
#: ``COUPLING WITH`` line off this same class, and the word rides in the manifest
#: because the worker's runner choice turns on it.
_CLASS_COUPLING = {"decay": "waqtel", "do_sag": "waqtel", "sediment": "gaia"}

#: How far down its own reach a DO-sag outfall sits. The reach was navigated
#: downstream FROM the outfall, so the discharge belongs at the top; the fraction
#: is what holds the source node off the inflow face rather than on it.
_DO_SAG_OUTFALL_FRAC = 0.02


@dataclass(frozen=True, slots=True)
class _Recipe:
    """One family's run directory: which engine, which files, where it stages.

    The per-family AUTHOR is the only code :func:`_assemble` does not hold, and
    it is a value here rather than a branch there: adding a family is a row plus
    a writer, never a fork in the flow.
    """

    #: The telapy engine class the worker dispatches this run to.
    module: str
    #: The GEOMETRY FILE, BOUNDARY CONDITIONS FILE and RESULTS FILE the steering
    #: file names, under the names the run directory holds them by.
    geometry: str
    boundary: str
    result: str
    #: The steering file itself.
    steering: str
    #: Where the manifest and the authored files are staged in the cache bucket.
    prefix: str
    #: ``(rundir, sheet, geometry, boundary, results, steering, ...) -> report``.
    author: Callable[..., Mapping[str, Any]]


_RECIPES: dict[str, _Recipe] = {
    "reach": _Recipe(
        module="telemac2d", geometry=_REACH_GEOMETRY, boundary=_REACH_BOUNDARY,
        result=_REACH_RESULT, steering=_REACH_STEERING, prefix="telemac",
        author=author.author_reach),
    "rain_on_grid": _Recipe(
        module="telemac2d", geometry=_ROG_GEOMETRY, boundary=_ROG_BOUNDARY,
        result=_ROG_RESULT, steering=_ROG_STEERING, prefix="telemac_rog",
        author=author.author_rain_on_grid),
}


# --------------------------------------------------------------------------- #
# The one flow.
# --------------------------------------------------------------------------- #
def _run_directory(run_tag: str) -> Path:
    rundir = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"telemac-{run_tag}"
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _cache_bucket() -> str:
    bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not bucket:
        raise TelemacDyeScenarioError(
            "TELEMAC_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage an authored run.")
    return bucket


def _upload_authored(rundir: Path, run_tag: str, names: Sequence[str],
                     prefix: str) -> list[dict[str, str]]:
    """Upload every file this authoring wrote -> the manifest rows staging them."""
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    bucket = _cache_bucket()
    s3 = _get_s3_client()
    rows: list[dict[str, str]] = []
    for name in names:
        key = f"{prefix}/{run_tag}/{name}"
        s3.put_object(Bucket=bucket, Key=key, Body=(rundir / name).read_bytes())
        rows.append({"gs_uri": f"s3://{bucket}/{key}", "dest": name})
    return rows


def _write_manifest(case: Mapping[str, Any], run_tag: str, *, outputs: list[str],
                    inputs: list[dict[str, str]], prefix: str) -> str:
    """Write the worker manifest for an authored case -> its ``s3://`` URI.

    The document itself is written by the ONE manifest writer, under the ``case``
    key the worker dispatches on.
    """
    try:
        return stage_telemac_manifest(
            section="case", config=case, run_tag=run_tag, outputs=outputs,
            inputs=inputs, prefix=prefix)
    except OpenWaterError as exc:
        raise TelemacDyeScenarioError("TELEMAC_STAGING_FAILED", str(exc)) from exc


async def _assemble(family: str, *, sheet: Mapping[str, Any],
                    server_facts: Mapping[str, Any], results: list[str],
                    outputs: list[str], mesh_inputs: list[dict[str, str]],
                    author_kwargs: Mapping[str, Any],
                    coupling: str | None = None,
                    continue_from: str | None = None) -> dict[str, Any]:
    """Sheet in, staged run directory out. The flow every family runs.

    Mint the run's tag, let the family's own writer fill a directory under it,
    upload everything it wrote beside the mesh the solve runs on, and write the
    manifest that names the case last - so a manifest exists only for a run whose
    every file is already where the launcher will look for it.

    ``coupling`` and ``continue_from`` are the two facts a steering file cannot
    state to the WORKER: which module's launcher can drive the case, and which
    staged file it restarts from. Both are absent on a run that has neither.
    """
    recipe = _RECIPES[family]
    run_tag = new_ulid()
    rundir = _run_directory(run_tag)
    written = await asyncio.to_thread(partial(
        recipe.author, rundir, sheet=sheet, geometry=recipe.geometry,
        boundary=recipe.boundary, results=recipe.result,
        steering=recipe.steering, **dict(author_kwargs)))
    # Every file the author wrote, under its path INSIDE the run directory: the
    # oil module's user fortran is a directory the engine compiles, so the walk
    # is recursive and the manifest dest carries the same relative path.
    authored = sorted(str(p.relative_to(rundir))
                      for p in rundir.rglob("*") if p.is_file())
    inputs = [*mesh_inputs,
              *await asyncio.to_thread(_upload_authored, rundir, run_tag,
                                       authored, recipe.prefix)]
    case = case_section(
        module=recipe.module, steering=recipe.steering, results=results,
        # The engine compiles the DIRECTORY the steering file names, so the
        # manifest channel carries the same directory the author wrote into.
        user_fortran=written.get("user_fortran"),
        coupling=coupling, continue_from=continue_from,
        server_facts=server_facts)
    manifest_uri = await asyncio.to_thread(
        _write_manifest, case, run_tag, outputs=outputs, inputs=inputs,
        prefix=recipe.prefix)
    logger.info("telemac %s staged run_tag=%s steering=%s results=%s authored=%s "
                "-> %s", family, run_tag, recipe.steering, results, authored,
                manifest_uri)
    return {"run_tag": run_tag, "rundir": str(rundir), "sheet": dict(sheet),
            "case": case, "manifest_uri": manifest_uri, "authored": authored,
            "written": dict(written), "outputs": outputs, "inputs": inputs,
            "result_basename": recipe.result}


def _mesh_field(mesh: Mapping[str, Any], name: str, *,
                missing: Callable[[str], Exception]) -> str:
    """One field of the ACCEPTED mesh's record, or the refusal that names it.

    A mesh record missing any of them refuses: falling through would solve on a
    mesh nobody accepted, under the accepted mesh's name.
    """
    uri = (mesh or {}).get(name)
    if not uri:
        raise missing(
            f"the mesh for this run carries no {name}, so the accepted mesh "
            f"cannot be staged (mesh record: {sorted((mesh or {}))}).")
    return str(uri)


def _reach_mesh_missing(message: str) -> Exception:
    return TelemacDyeScenarioError("TELEMAC_MESH_NOT_ACCEPTED", message)


def _catchment_mesh_missing(message: str) -> Exception:
    return RainOnGridError(message, error_code="TELEMAC_ROG_MESH_NOT_ACCEPTED")


# --------------------------------------------------------------------------- #
# The reach: what a spill, a scour or a sag is solved as.
# --------------------------------------------------------------------------- #
def _class_files(substance_class: str, *,
                 dredging: bool) -> tuple[list[str], list[str]]:
    """What a class MUST produce, and everything the supervisor brings back.

    The two lists answer two questions. ``results`` is the success convention -
    a solver that exits clean without writing these has not solved anything - so
    it names only files the ENGINE writes. ``outputs`` is what a reader may later
    open, so it also names the inputs the run was handed, which is how a solved
    run stays readable from its own prefix.
    """
    results = [_REACH_RESULT]
    outputs = [_REACH_RESULT, _REACH_GEOMETRY, _REACH_BOUNDARY, _REACH_STEERING,
               "full_listing.log", "telemac_metrics.json"]
    if substance_class not in _CLASS_COUPLING:
        # Every run on the stepped arm leaves the state a continuation reads,
        # so re-entry is a property of the family rather than of foresight. A
        # coupled class runs the module's own launcher whole and cannot be
        # continued, so it is not asked to write one.
        results.append(_RESTART)
        outputs.append(_RESTART)
    if substance_class == "sediment":
        # the GAIA deposition SELAFIN + its steering file, which the postprocess
        # reads to build the bed-evolution COG.
        results.append(author.GAIA_RESULT_FILENAME)
        outputs += [author.GAIA_RESULT_FILENAME, author.GAIA_STEERING_FILENAME]
        if dredging:
            # the NESTOR dig/dump rule, its zones, and the design grade.
            outputs += ["nestor.act", "nestor.pol", "nestor.ref"]
    elif substance_class == "oil":
        # the raw particle track the slick reader parses, and the oil steering
        # file the run used. The slick and the particle snapshots are built from
        # that track on the server, so neither is a file the worker writes.
        results.append("drogues.txt")
        outputs += ["drogues.txt", "oil_spill.txt"]
    elif substance_class in ("decay", "do_sag"):
        # the WAQTEL steering file: the forcing this run actually applied.
        outputs.append(author.WAQTEL_FILENAME)
    return results, outputs


def _face_section(nodes: Sequence[int], node_xy: Any,
                  bed: Any) -> list[list[float]]:
    """The channel the outflow face cuts, as ``(offset, bed)`` pairs.

    A role is a contiguous RUN of the boundary walk, so the nodes arrive in the
    order they lie along the face and the offset is the running chord distance
    between them. That makes the section a real transect of the painted bed
    rather than a scatter that has to be re-ordered by a rule of its own.

    A node the bed left unpainted drops out of the section and takes no offset
    with it: the survey has a hole in it, and closing the hole by shifting the
    nodes past it would narrow a channel nobody re-measured.
    """
    import numpy as np

    xy = None if node_xy is None else np.asarray(node_xy, dtype=float)
    if xy is None or len(nodes) < 2 or max(nodes) >= xy.shape[0]:
        raise TelemacDyeScenarioError(
            "TELEMAC_MESH_SECTION_UNMEASURED",
            f"the outflow stage is a normal depth over the channel the outflow "
            f"face cuts, and that face names {len(nodes)} node(s) against "
            f"{0 if xy is None else xy.shape[0]} mesh coordinates; a reach mesh "
            "recipe names its faces with set_boundary_roles.")
    points = xy[list(nodes)]
    steps = np.hypot(*(points[1:] - points[:-1]).T)
    offsets = np.concatenate([[0.0], np.cumsum(steps)])
    section = [[round(float(o), 3), round(float(z), 3)]
               for o, z in zip(offsets, bed[list(nodes)]) if np.isfinite(z)]
    if len(section) < 2:
        raise TelemacDyeScenarioError(
            "TELEMAC_MESH_SECTION_UNMEASURED",
            f"the outflow face carries {len(section)} painted node(s), which is "
            "no section to derive a normal depth over.")
    return section


def _measured_reach(roles: Mapping[str, Any], node_xy: Any, node_bed: Any,
                    centerline_utm: Any) -> dict[str, Any]:
    """What the accepted mesh says about the reach the outflow stage rests on.

    Three measurements, every one off the artifact itself: the bed at the two
    declared roles, the SECTION the outflow face cuts through that bed, and the
    length of the line the mesh was built over. A profile derived beside the mesh
    could disagree with the bed the geometry file holds, and a stage derived from
    it would be prescribed against ground the solver never sees.

    The two bed numbers are medians over the nodes each role names - the reach's
    top, and the fall from there to its outflow. That fall over that length is
    the friction slope the normal depth is computed at.
    """
    import numpy as np

    bed = None if node_bed is None else np.asarray(node_bed, dtype=float)
    medians: dict[str, float] = {}
    role_nodes: dict[str, list[int]] = {}
    for role in ("inflow", "outflow"):
        nodes = [int(n) for n in ((roles or {}).get(role) or ())]
        if bed is None or not nodes or max(nodes) >= bed.shape[0]:
            raise TelemacDyeScenarioError(
                "TELEMAC_MESH_BED_UNMEASURED",
                f"the outflow stage is derived over the painted bed at the "
                f"{role!r} role's own nodes, and the accepted mesh carries "
                f"{0 if bed is None else bed.shape[0]} bed values under roles "
                f"{sorted(roles or {})}; a reach mesh recipe paints its bed with "
                "set_bed and names its faces with set_boundary_roles.")
        median = float(np.nanmedian(bed[nodes]))
        if not np.isfinite(median):
            raise TelemacDyeScenarioError(
                "TELEMAC_MESH_BED_UNMEASURED",
                f"every node the {role!r} role names carries an unpainted bed, so "
                "the outflow stage has no ground to be measured from.")
        medians[role] = median
        role_nodes[role] = nodes
    line = np.asarray(centerline_utm, dtype=float)
    length = (float(np.hypot(*(line[1:] - line[:-1]).T).sum())
              if line.ndim == 2 and len(line) > 1 else 0.0)
    return {"bed_top_m": medians["inflow"],
            "bed_drop_m": medians["inflow"] - medians["outflow"],
            "reach_length_m": round(length, 3),
            "outflow_section": _face_section(role_nodes["outflow"], node_xy, bed)}


def _continuation_start_s(uri: str) -> float:
    """The clock time the restart record stands at - read off the file itself.

    A continued run is the SAME declared scenario over an extended horizon, and
    the horizon begins where the leg being continued stopped. Only that file can
    say when: the engine writes the restart at its own last time step, which is
    not the graphic period, not the asked duration, and not something the server
    can compute from the ask.
    """
    import tempfile

    from trid3nt_server.workflows.solver.solver import _download_object
    from trid3nt_server.workflows.telemac.result_reader import read_selafin

    with tempfile.TemporaryDirectory(prefix="telemac-continue-") as tmp:
        path = Path(tmp) / _PREVIOUS_DEST
        _download_object(str(uri), path)
        times = read_selafin(path)["times"]
    if len(times) == 0:
        raise TelemacDyeScenarioError(
            "TELEMAC_CONTINUATION_UNREADABLE",
            f"{uri} holds no time record, so there is no state to continue from "
            "and no instant to continue the scenario at. Point continue_from at "
            f"a completed run's {_RESTART}.")
    return float(times[-1])


def _to_utm(source: Any, utm_epsg: int) -> Any:
    """A lon/lat geometry source -> its shapely geometry in the mesh's metres."""
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import transform as _transform, unary_union

    from trid3nt_server.tools.processing._geometry_common import (
        flatten_geometries, read_geometry_doc,
    )

    geometry = unary_union([_shape(g)
                            for g in flatten_geometries(read_geometry_doc(source))])
    tr = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    return _transform(tr.transform, geometry)


def _to_utm_point(lonlat: tuple[float, float],
                  utm_epsg: int) -> tuple[float, float]:
    """One lon/lat point in the mesh's own metres."""
    from pyproj import Transformer

    x, y = Transformer.from_crs(4326, int(utm_epsg), always_xy=True).transform(
        float(lonlat[0]), float(lonlat[1]))
    return (float(x), float(y))


def _mesh_nodes(mesh: Mapping[str, Any]) -> tuple[Any, Any]:
    """The accepted mesh's node coordinates and bed, read off its display face.

    The ``.2dm`` is the one readable record of the node numbering the geometry
    file carries, so the bed read here is the bed the solve starts from - which
    is what the outflow stage is measured over, and what a NESTOR design grade
    digs back to.
    """
    points, _cells, z, _lonlat = read_accepted_mesh_nodes(
        _mesh_field(mesh, "display_uri", missing=_reach_mesh_missing))
    return points, z


def _resolved_physics(friction_coefficient: float | None, friction_law: Any,
                      velocity_diffusivity: float | None,
                      tracer_diffusivity: float | None) -> dict[str, Any]:
    """Only the constitutive overrides the user actually set, range-checked.

    Anything unset is ABSENT from the sheet, so the author's own default table
    stands and the steering file emits the historical literal.
    """
    from trid3nt_server.workflows.shared.physics_registry import (
        PhysicsRegistryError,
        applied_physics_delta,
        validate_and_resolve_physics,
    )

    overrides: dict[str, Any] = {}
    if friction_coefficient is not None:
        overrides["friction_coefficient"] = float(friction_coefficient)
    if friction_law is not None and int(friction_law) in _FRICTION_LAWS:
        overrides["friction_law"] = int(friction_law)
    if velocity_diffusivity is not None:
        overrides["velocity_diffusivity"] = float(velocity_diffusivity)
    if tracer_diffusivity is not None:
        overrides["tracer_diffusivity"] = float(tracer_diffusivity)
    if not overrides:
        return {}
    try:
        resolved = validate_and_resolve_physics("telemac", overrides)
    except PhysicsRegistryError as exc:
        raise TelemacDyeScenarioInputError(
            f"invalid TELEMAC advanced physics: {exc}") from exc
    logger.info("telemac advanced physics applied (user-provided): %s",
                applied_physics_delta("telemac", resolved))
    return resolved


def _sediment_block(substance: str, payload: Any, *, erodible: bool,
                    gradation: list[list[float]] | None, dredging: bool,
                    sediment_type: str | None, grain_size_um: float | None,
                    bed_thickness_m: float | None, bedload_formula: Any,
                    morphological_factor: float | None, dredge_mode: str,
                    dredge_volume_m3: float | None, dredge_disposal: bool,
                    dredge_crit_depth_m: float | None,
                    dredge_dig_depth_m: float | None,
                    dredge_bank_offset_m: float) -> dict[str, Any]:
    sed_type, sed_grain_um = resolve_grain(payload, sediment_type, grain_size_um)
    logger.info("substance %r -> sediment class (GAIA, type=%s d50=%.4gum): %s",
                substance, sed_type, sed_grain_um,
                "erodible-bed bedload morphodynamics (scour + deposition)" if erodible
                else "suspended settling + supply-limited deposition")
    block: dict[str, Any] = {
        "substance_class": "sediment", "sediment_type": sed_type,
        "grain_size_um": sed_grain_um, "sediment_density": 2650.0,
        "erodible_bed": bool(erodible),
    }
    # The erodible-bed tuning rides ONLY when armed AND set; unset lets the
    # author's own defaults apply, which keeps a non-erodible run byte-identical.
    if erodible and bed_thickness_m is not None:
        block["bed_thickness_m"] = float(bed_thickness_m)
    if erodible and bedload_formula is not None and int(bedload_formula) in _BEDLOAD_FORMULAE:
        block["bedload_formula"] = int(bedload_formula)
    if erodible and morphological_factor is not None:
        block["morphological_factor"] = float(morphological_factor)
    if gradation:
        block["sediment_gradation"] = gradation
    if dredging:
        mode = str(dredge_mode or "scheduled").strip().lower()
        block.update({
            "dredging": True,
            "dredge_mode": mode if mode in _DREDGE_MODES else "scheduled",
            "dredge_disposal": bool(dredge_disposal),
            # The BANK SETBACK the dig field is cut back from the mapped water,
            # and the same number that excludes a stretch too narrow to dredge.
            "dredge_bank_offset_m": float(dredge_bank_offset_m),
        })
        if dredge_volume_m3 is not None:
            block["dredge_volume_m3"] = float(dredge_volume_m3)
        if dredge_crit_depth_m is not None:
            block["dredge_crit_depth_m"] = float(dredge_crit_depth_m)
        if dredge_dig_depth_m is not None:
            block["dredge_dig_depth_m"] = float(dredge_dig_depth_m)
    return block


def _substance_block(substance: str, *, erodible_bed: bool | None,
                     sediment_gradation: Any, dredging: bool | None,
                     decay_half_life_hours: float | None,
                     decay_rate_per_day: float | None,
                     sediment_type: str | None, grain_size_um: float | None,
                     bed_thickness_m: float | None, bedload_formula: Any,
                     morphological_factor: float | None, dredge_mode: str,
                     dredge_volume_m3: float | None, dredge_disposal: bool,
                     dredge_crit_depth_m: float | None,
                     dredge_dig_depth_m: float | None,
                     dredge_bank_offset_m: float,
                     ) -> tuple[str, Any, bool, dict[str, Any]]:
    """The class, its payload, whether the bed is erodible, and the sheet block."""
    substance_class, payload = classify_substance(substance)
    erodible, gradation, dredge = arm_sediment_modules(
        substance, erodible_bed=erodible_bed,
        sediment_gradation=sediment_gradation, dredging=dredging)

    # SINGLE SOURCE OF TRUTH: an armed erodible bed IS a GAIA morphodynamics run,
    # so it MUST route through the sediment class. Otherwise the flag and the
    # class gate diverge - erodible_bed reads True while nothing is coupled, and
    # the run only LOOKS morphodynamic.
    if erodible and substance_class != "sediment":
        logger.info("telemac: erodible bed armed but classify(%r)=%s - forcing the "
                    "sediment class (GAIA morphodynamics)", substance, substance_class)
        substance_class, payload = "sediment", {"type": "sand", "grain_size": 200.0}

    if substance_class == "oil":
        logger.info("substance %r -> oil class (preset %s): slick particles + "
                    "dissolved tracer", substance, payload)
        return substance_class, payload, False, {
            "substance_class": "oil", "oil_preset": payload}
    if substance_class == "decay":
        law, coef = resolve_decay_law(payload, decay_half_life_hours,
                                      decay_rate_per_day)
        logger.info("substance %r -> decay class (WAQTEL process 17, law=%d "
                    "coef=%.4g): first-order sink on the dye tracer, no new tracer",
                    substance, law, coef)
        return substance_class, payload, False, {
            "substance_class": "decay", "decay_law": law, "decay_coef": coef}
    if substance_class == "sediment":
        return substance_class, payload, erodible, _sediment_block(
            substance, payload, erodible=erodible, gradation=gradation,
            dredging=dredge, sediment_type=sediment_type,
            grain_size_um=grain_size_um, bed_thickness_m=bed_thickness_m,
            bedload_formula=bedload_formula,
            morphological_factor=morphological_factor, dredge_mode=dredge_mode,
            dredge_volume_m3=dredge_volume_m3, dredge_disposal=dredge_disposal,
            dredge_crit_depth_m=dredge_crit_depth_m,
            dredge_dig_depth_m=dredge_dig_depth_m,
            dredge_bank_offset_m=dredge_bank_offset_m)
    return substance_class, payload, False, {}


def _do_sag_block(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """The WAQTEL O2 scenario: clean river in at the face, the OUTFALL as a source.

    Threaded only when a DO-sag config was supplied, so every other run is
    byte-identical.
    """
    if cfg is None:
        return {}
    # NO fallbacks. Every one of these is a declared Param on telemac_do_sag with
    # its own labeled default, and the waqtel step that builds ``cfg`` resolves
    # all of them before this runs - so a ``.get(k, 20.0)`` here was a SECOND
    # copy of the contract's number, free to drift from the one on the form.
    # A missing key is now a KeyError at the seam that lost it.
    return {
        "substance_class": "do_sag",
        "do_sag_effluent_bod_mgl": float(cfg["effluent_bod_mgl"]),
        "do_sag_effluent_q_m3s": float(cfg["effluent_q_m3s"]),
        "do_sag_effluent_do_mgl": float(cfg["effluent_do_mgl"]),
        "do_sag_upstream_do_mgl": float(cfg["upstream_do_mgl"]),
        "do_sat_mgl": float(cfg["saturation_mgl"]),
        "do_water_temp_c": float(cfg["water_temp_c"]),
        "do_k1_per_day": float(cfg["k1_per_day"]),
        "do_k2_per_day": float(cfg["k2_per_day"]),
        "do_k2_formula": int(cfg["k2_formula"]),
        "do_standard_mgl": float(cfg["standard_mgl"]),
    }


async def _settle_release(
    release_pair: tuple[float, float] | None, *, mesh: dict[str, Any],
    centerline: Any, centerline_utm: Any, utm_epsg: int, spill_fraction: float,
) -> tuple[tuple[float, float], str | None]:
    """WHERE the source enters the water -> ``(lon, lat)`` and how it was decided.

    ONE seam, because there is one centerline. A SUPPLIED point is settled against
    real geometry: the domain polygon the accepted mesh was cut from decides
    whether it can be a source at all, and the declared centerline decides where on
    the river it sits. A point the domain does not hold raises through, because
    the only alternatives are releasing somewhere the user did not choose or
    solving a source outside the water.

    With none placed the source sits at ``spill_fraction`` along that SAME
    centerline - the line the section was cut between and the mesh was built over -
    walked downstream to the first station the ACCEPTED MESH holds. The centerline
    is the whole navigated stretch and the mesh is only the part of it the mapped
    banks left, so "on the line" and "in the domain" are two different claims and
    only the second one solves.
    """
    from trid3nt_server.workflows.telemac.release_point import (
        contain_release_point, derive_release_on_mesh, domain_polygon_of,
    )

    if release_pair is None:
        return await asyncio.to_thread(
            derive_release_on_mesh, centerline_utm=centerline_utm, mesh=mesh,
            fraction=spill_fraction)

    contained = await asyncio.to_thread(
        contain_release_point, point=release_pair,
        domain=domain_polygon_of(mesh.get("artifact")),
        flowline=centerline)
    return (contained.lon, contained.lat), contained.note


async def assemble_reach(
    *,
    reach: dict[str, Any],
    seed: dict[str, Any],
    mesh: dict[str, Any],
    centerline: Any,
    carrier_discharge: dict[str, Any],
    reach_polygon: Any = None,
    rain: dict[str, Any] | None = None,
    release_coords: Any = None,
    substance: str = "dye",
    reach_length_km: float = 6.0,
    sim_duration_s: float = 3600.0,
    spill_fraction: float = 0.25,
    spill_duration_s: float = 300.0,
    dye_concentration_mgl: float = 100.0,
    source_q_m3s: float = 8.0,
    mesh_resolution_m: float = 14.0,
    output_interval_min: float | None = None,
    wind_speed_mps: float = 0.0,
    wind_direction_deg: float = 0.0,
    friction_coefficient: float | None = None,
    friction_law: Any = None,
    velocity_diffusivity: float | None = None,
    tracer_diffusivity: float | None = None,
    erodible_bed: bool | None = None,
    sediment_gradation: Any = None,
    dredging: bool | None = None,
    decay_half_life_hours: float | None = None,
    decay_rate_per_day: float | None = None,
    sediment_type: str | None = None,
    grain_size_um: float | None = None,
    bed_thickness_m: float | None = None,
    bedload_formula: Any = None,
    morphological_factor: float | None = None,
    dredge_mode: str = "scheduled",
    dredge_volume_m3: float | None = None,
    dredge_disposal: bool = False,
    dredge_crit_depth_m: float | None = None,
    dredge_dig_depth_m: float | None = None,
    dredge_bank_offset_m: float = 5.0,
    do_sag_config: dict[str, Any] | None = None,
    continue_from: str | None = None,
) -> dict[str, Any]:
    """Serialize the approved sheet into the reach's staged run + the run meta.

    The MESH is the accepted one: its geometry and its boundary roles are staged
    for the solve, so the run is solved on the triangulation that was presented
    rather than on an equivalent rebuild, and the timestep follows the edge that
    mesh was BUILT at rather than the edge that was asked for.

    The CENTERLINE is the chain's own declared row - the line the section was cut
    between, the mesh was built over and the canvas shows. There is exactly one,
    so the release the run carries, the marker on the map and the domain the
    solver integrates cannot describe three different rivers.

    The RELEASE POINT is settled against that centerline BEFORE anything is
    staged: a supplied point outside the domain polygon refuses while the user can
    still move it, one inside it is put on the flowline, and an unplaced one walks
    ``spill_fraction`` along the same line. Only then does the marker go on the
    canvas - at the point the steering file actually carries - saying out loud
    whether the user placed it or the pipeline derived it.

    ``continue_from`` is a previous run's RESTART record. It is staged like any
    other input and the steering file names it as the engine's PREVIOUS
    COMPUTATION FILE, so a continued run is an ordinary run whose initial state
    came out of another one - there is no resident solver anywhere for it to
    re-enter. The scenario it solves is the SAME one, re-authored over the
    extended horizon on the same absolute clock, which is why the instant that
    file stands at is read here.
    """
    substance = sanitize_substance(substance)
    release_pair = coerce_lonlat_point(release_coords)
    seed_lon, seed_lat = float(seed["lon"]), float(seed["lat"])

    # The granularity the sheet records is the one the ACCEPTED mesh was built at,
    # measured on its own cells; the asked edge stands only until a mesh exists to
    # measure. Nothing here re-derives an edge from a channel width nobody surveyed.
    measured = mesh.get("min_edge_m")
    mesh_size_m = round(max(float(measured if measured is not None
                                  else mesh_resolution_m), MESH_H_FLOOR_M), 3)
    mesh_resolution_label = (
        f"{mesh_size_m:.3g} m measured minimum edge over "
        f"{mesh.get('element_count') or 0} elements" if measured is not None
        else f"{mesh_size_m:.3g} m asked edge (mesh unmeasured)")
    time_step_s = suggest_time_step_s(mesh_size_m, mesh=mesh.get("artifact"))
    logger.info("telemac mesh granularity: %s -> h=%.3g m (dt=%.3g s, reach=%.3g km)",
                mesh_resolution_label, mesh_size_m, time_step_s, reach_length_km)

    substance_class, _payload, erodible, class_block = _substance_block(
        substance, erodible_bed=erodible_bed, sediment_gradation=sediment_gradation,
        dredging=dredging, decay_half_life_hours=decay_half_life_hours,
        decay_rate_per_day=decay_rate_per_day,
        sediment_type=sediment_type, grain_size_um=grain_size_um,
        bed_thickness_m=bed_thickness_m, bedload_formula=bedload_formula,
        morphological_factor=morphological_factor, dredge_mode=dredge_mode,
        dredge_volume_m3=dredge_volume_m3, dredge_disposal=dredge_disposal,
        dredge_crit_depth_m=dredge_crit_depth_m, dredge_dig_depth_m=dredge_dig_depth_m,
        dredge_bank_offset_m=dredge_bank_offset_m)

    # The class the SHEET states - the one the author branches on, and the one a
    # continuation is refused on. Reading it off the substance word would name a
    # run "tracer" that the author wrote a WAQTEL coupling into.
    run_class = ("do_sag" if do_sag_config
                 else str(class_block.get("substance_class") or "tracer"))
    coupled_with = _CLASS_COUPLING.get(run_class)
    if continue_from and coupled_with:
        raise TelemacDyeScenarioInputError(
            f"a {run_class} reach couples the solve with {coupled_with.upper()}, "
            "which runs the module's own launcher rather than the stepped arm; "
            "continuing one has never been run and this refuses rather than "
            "report it as a run that was. Drop continue_from, or run the "
            "uncoupled class.")

    from trid3nt_server.workflows.telemac.release_layer import publish_release_point
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    artifact = mesh.get("artifact")
    utm_epsg = int(getattr(artifact, "utm_epsg", 0) or 0)
    # The centerline is read head-to-tail from the seed the navigate was walked
    # downstream FROM, so ``spill_fraction`` counts from upstream and the bed the
    # mesh carries slopes the same way.
    centerline_utm = await asyncio.to_thread(
        read_centerline_utm, centerline, utm_epsg,
        start_lonlat=(seed_lon, seed_lat))
    # A DO-sag reach was NAVIGATED downstream from its outfall, so the outfall is
    # this reach's chainage zero and the whole modeled stretch is below it. The
    # source is placed just inside that top rather than on it: a source node
    # sitting on the prescribed-flowrate face would compete with the boundary
    # condition for the same node.
    if run_class == "do_sag":
        spill_fraction = _DO_SAG_OUTFALL_FRAC
    release_lonlat, release_note = await _settle_release(
        release_pair, mesh=mesh, centerline=centerline, centerline_utm=centerline_utm,
        utm_epsg=utm_epsg, spill_fraction=spill_fraction)

    # The marker rides BEFORE the solve, so the user sees the input against the
    # mesh rather than only in the results, and it carries the SETTLED point: the
    # containment test above refuses anything the domain does not hold, so no run
    # carries a user-placed marker the plume disagrees with.
    await publish_release_point(
        current_emitter(),
        lon=release_lonlat[0], lat=release_lonlat[1],
        user_supplied=release_pair is not None,
        reach_name=reach["slug"],
        label="Outfall" if do_sag_config else "Release point")

    # WHICH dataset painted the mesh's nodes. It is the mesher's own record of
    # the bed the geometry file carries - the ONE bed this run has - so the label
    # the metrics report cannot describe a raster the solve never read.
    bed_source = str((mesh.get("provenance") or {}).get("bed_source") or "staged")
    rain_mm_day = (rain or {}).get("mm_per_day")
    sheet: dict[str, Any] = {
        "name": reach["slug"],
        # The seed the one centerline was navigated from - the manifest's record
        # of which stretch this run meshed.
        "seed_lon": round(seed_lon, 6),
        "seed_lat": round(seed_lat, 6),
        **_resolved_physics(friction_coefficient, friction_law,
                            velocity_diffusivity, tracer_diffusivity),
        **class_block,
        **_do_sag_block(do_sag_config),
        # Wind rides ONLY when a positive speed was asked for; absent otherwise, so
        # the author emits no wind block.
        **({"wind_speed_mps": float(wind_speed_mps),
            "wind_dir_from_deg": float(wind_direction_deg)}
           if wind_speed_mps and float(wind_speed_mps) > 0.0 else {}),
        **({"rain_or_evap_mm_per_day": float(rain_mm_day)}
           if rain_mm_day is not None else {}),
        "nav_direction": "DM",
        "distance_km": float(reach_length_km),
        # WHICH dataset the staged bed came from. The worker opens a file and
        # cannot know, so the label travels with the file - otherwise the run's
        # own metrics could not tell a GLO-30 bed from the 3DEP one the ladder
        # fell to, which is exactly the substitution the loudness floor exists
        # to keep visible.
        "bed_source": bed_source,
        "mesh_size_m": mesh_size_m,
        "time_step_s": time_step_s,
        **({"output_interval_min": float(output_interval_min)}
           if output_interval_min is not None else {}),
        "dye_conc_mgl": float(dye_concentration_mgl),
        # A PICKED release point overrides spill_frac, and only a picked one is
        # written: the row is the run's record of what the USER placed, so a
        # derived source stays described by the fraction that produced it.
        **({"release_lon": round(release_lonlat[0], 6),
            "release_lat": round(release_lonlat[1], 6)}
           if release_pair is not None else {}),
        "spill_frac": float(min(max(spill_fraction, 0.0), 1.0)),
        "pulse_window_s": float(spill_duration_s),
        "source_q_m3s": float(source_q_m3s),
        "inflow_q_m3s": float(carrier_discharge["m3s"]),
        "duration_s": float(sim_duration_s),
        # WHERE this run picks up from, and WHEN. The sheet records the staged
        # NAME because the engine reads a file: the URI it was staged from is
        # the ask, and the ask is the run's inputs list. The instant comes off
        # that file, and every forcing series is written over the horizon it
        # opens.
        **({"continue_from": _PREVIOUS_DEST,
            "start_time_s": await asyncio.to_thread(
                _continuation_start_s, str(continue_from))}
           if continue_from else {}),
    }

    topology = await asyncio.to_thread(
        read_topology, _mesh_field(mesh, "topology_uri",
                                   missing=_reach_mesh_missing))
    node_xy, node_bed = await asyncio.to_thread(_mesh_nodes, mesh)
    results, outputs = _class_files(run_class,
                                    dredging=bool(class_block.get("dredging")))
    staged = await _assemble(
        "reach", sheet=sheet,
        server_facts={
            "utm_epsg": utm_epsg,
            "bbox": [round(float(v), 6)
                     for v in (getattr(artifact, "bbox", None) or ())],
            "npoin": int(mesh.get("node_count") or 0),
            "nelem": int(mesh.get("element_count") or 0),
            "mesh_size_m": mesh_size_m,
            # WHICH file carries the time series. The author wrote the RESULTS
            # FILE statement, so the name is the server's; the worker copies it
            # and measures the file it names.
            "result_slf": _REACH_RESULT,
            "bed_source": bed_source},
        results=results, outputs=outputs,
        mesh_inputs=[
            {"gs_uri": _mesh_field(mesh, "slf_uri", missing=_reach_mesh_missing),
             "dest": _REACH_GEOMETRY},
            {"gs_uri": _mesh_field(mesh, "cli_uri", missing=_reach_mesh_missing),
             "dest": _REACH_BOUNDARY},
            *([{"gs_uri": str(continue_from), "dest": _PREVIOUS_DEST}]
              if continue_from else [])],
        coupling=coupled_with,
        continue_from=_PREVIOUS_DEST if continue_from else None,
        author_kwargs={
            "restart": None if coupled_with else _RESTART,
            "liquid_boundary_order": topology["liquid_boundary_order"],
            "liquid_boundary_prescribes": topology["liquid_boundary_prescribes"],
            "bed": _measured_reach(topology["roles"], node_xy, node_bed,
                                   centerline_utm),
            "source_utm": _to_utm_point(release_lonlat, utm_epsg),
            "centerline_utm": centerline_utm,
            "reach_polygon_utm": (
                await asyncio.to_thread(_to_utm, reach_polygon, utm_epsg)
                if class_block.get("dredging") else None),
            "node_xy": node_xy, "node_bed": node_bed})
    return {
        **staged,
        "mesh_id": mesh.get("mesh_id"),
        "substance": substance,
        "substance_class": substance_class,
        "erodible_bed": bool(erodible),
        "reach_name": reach["slug"],
        "location_name": reach["name"],
        "mesh_size_m": mesh_size_m,
        "mesh_resolution_label": mesh_resolution_label,
        "mesh_resolution_asked_m": mesh_resolution_m,
        "time_step_s": time_step_s,
        "seed_source": seed.get("source"),
        # The pre-flight's record of what it did to a supplied release point.
        # It rides the run META rather than the sheet: the worker is handed the
        # settled coordinates and no account of how they were settled.
        "release_note": release_note,
        "discharge_note": carrier_discharge.get("note"),
        "rain_note": (rain or {}).get("note"),
        "rain_mm_per_day": rain_mm_day,
        "rain_rung": (rain or {}).get("rung"),
    }


# --------------------------------------------------------------------------- #
# The catchment: what a storm over a basin is solved as.
# --------------------------------------------------------------------------- #
def _outlet_boundary(mesh: Mapping[str, Any]) -> tuple[int, str]:
    """The declared OUTLET as ``(number, what its own .cli quad prescribes)``.

    The solver numbers its liquid boundaries by walking the geometry, the accepted
    topology recorded that numbering when the ``.cli`` was written, and the solver
    prints one flux per number in its own volume balance. So the number is what
    turns "the outlet" into the series the hydrograph reads.

    The prescription rides with it because this run writes NO value at that
    number: the outlet is solved on its code alone, and the steering file states
    which code that is rather than claiming a boundary condition nobody measured.
    """
    topology = read_topology(_mesh_field(mesh, "topology_uri",
                                         missing=_catchment_mesh_missing))
    order = list(topology["liquid_boundary_order"])
    if _OUTLET_ROLE not in topology["roles"] or _OUTLET_ROLE not in order:
        raise RainOnGridError(
            f"no boundary node of the catchment mesh took the {_OUTLET_ROLE!r} "
            "role, so the basin has no outlet to drain through and no hydrograph "
            "to measure. Move the pour point onto the basin's own outlet, or mesh "
            "it finer so a boundary node reaches it.",
            error_code="TELEMAC_ROG_NO_OUTLET_NODES")
    number = order.index(_OUTLET_ROLE) + 1
    return number, str(topology["liquid_boundary_prescribes"][number - 1])


async def assemble_rain_on_grid(
    *,
    catchment: dict[str, Any],
    infiltration: dict[str, Any],
    rain: dict[str, Any],
    mesh_resolution_m: float | None = None,
    time_step_s: float,
    output_interval_min: float | None = None,
) -> dict[str, Any]:
    """Serialize the approved sheet into the catchment's staged run + the run meta.

    ``catchment`` is the ACCEPTED mesh: its geometry, its boundary conditions and
    the outlet role its pour point matched are what the solve runs on, so the
    steering file is authored against the triangulation that was presented rather
    than an equivalent rebuild.

    The output CADENCE rides the sheet in MINUTES and converts to a count of
    solver steps at the author, beside the keyword it is written into.
    """
    from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (
        select_runoff_path,
    )

    decision = (select_runoff_path(hyetograph_mm=rain["series"])
                if rain["kind"] == "hyetograph"
                else select_runoff_path(
                    constant_intensity_mm_per_hr=rain["intensity_mm_per_hr"]))

    artifact = catchment.get("artifact")
    utm_epsg = int(getattr(artifact, "utm_epsg", 0) or 0)
    probes = dict(getattr(artifact, "probes", None) or {})
    provenance = dict(catchment.get("provenance") or {})
    outlet_boundary, outlet_prescribes = _outlet_boundary(catchment)
    mesh_size_m = float(catchment.get("min_edge_m") or mesh_resolution_m or 0.0)
    name = str(getattr(artifact, "name", None) or "watershed")
    bed_source = str(provenance.get("bed_source") or "staged")

    sheet: dict[str, Any] = {
        "name": name,
        "amc_condition": int(infiltration["amc_condition"]),
        "duration_s": float(rain["duration_s"]),
        "time_step_s": float(time_step_s),
        **({"output_interval_min": float(output_interval_min)}
           if output_interval_min is not None else {}),
        **({"rain_duration_s": float(rain["rain_duration_s"])}
           if rain.get("rain_duration_s") is not None else {}),
    }

    points_utm, _cells, _bed, _lonlat = await asyncio.to_thread(
        mesh_nodes, catchment)
    staged = await _assemble(
        "rain_on_grid", sheet=sheet,
        server_facts={
            "utm_epsg": utm_epsg,
            "bbox": [round(float(v), 6)
                     for v in (getattr(artifact, "bbox", None) or ())],
            "npoin": int(catchment.get("node_count") or 0),
            "nelem": int(catchment.get("element_count") or 0),
            "mesh_size_m": mesh_size_m,
            # WHICH file carries the time series. The author wrote the RESULTS
            # FILE statement, so the name is the server's; the worker copies it
            # and measures the file it names.
            "result_slf": _ROG_RESULT,
            "bed_source": bed_source},
        results=[_ROG_RESULT],
        # What a reader may later open: what the ENGINE writes, plus every file
        # this authoring hands it. The hyetograph is the one that is conditional,
        # and the same condition decides whether it is written at all.
        outputs=[_ROG_RESULT, _ROG_GEOMETRY, _ROG_BOUNDARY, "full_listing.log",
                 "telemac_metrics.json", _ROG_STEERING, author.ROG_CN_MAP,
                 author.ROG_FRICTION_LAWS, author.ROG_ZONES,
                 *([author.ROG_HYETOGRAPH] if decision.time_varying else [])],
        mesh_inputs=[
            {"gs_uri": _mesh_field(catchment, "slf_uri",
                                   missing=_catchment_mesh_missing),
             "dest": _ROG_GEOMETRY},
            {"gs_uri": _mesh_field(catchment, "cli_uri",
                                   missing=_catchment_mesh_missing),
             "dest": _ROG_BOUNDARY}],
        author_kwargs={
            "node_xy": points_utm,
            "node_cn2": infiltration["node_cn2"],
            "node_manning": infiltration["node_manning"],
            "rain_mm_per_day": float(rain["intensity_mm_per_hr"]) * 24.0,
            "outlet_boundary": outlet_boundary,
            "outlet_prescribes": outlet_prescribes,
            "hyetograph_blocks": (rain["blocks"] if decision.time_varying
                                  else None)})
    return {
        **staged,
        "catchment": catchment,
        "infiltration": infiltration,
        "rain": rain,
        "outlet_boundary": outlet_boundary,
        "runoff_path": decision.path,
        "runoff_reason": decision.reason,
        "hyetograph_total_mm": staged["written"].get("hyetograph_total_mm"),
        "mesh_size_m": mesh_size_m,
        "mesh_max_edge_m": float((probes.get("edge_length_m") or {}).get("max") or 0.0),
        "area_km2": float(probes.get("area_km2") or 0.0),
        "lonlat_bounds": [float(v) for v in (getattr(artifact, "bbox", None) or ())],
        "mesh_resolution_asked_m": mesh_resolution_m,
        "domain_name": name,
        "utm_epsg": utm_epsg,
        "bed_source": bed_source,
        "bed_note": str(provenance.get("bed_fallback_note") or ""),
        "sizing_source": str(provenance.get("sizing_source") or ""),
        "domain_source": str(provenance.get("domain_source") or ""),
    }


class Assemble:
    """The staged run, as the facade binds it. One entry per family."""

    @staticmethod
    def reach(**kwargs: Any) -> Step:
        """A spill, a scour or a sag in a river reach."""
        return Step(runner=f"{_AUTHORING}.assembler.assemble_reach", stage="author",
                    kwargs=kwargs)

    @staticmethod
    def rain_on_grid(**kwargs: Any) -> Step:
        """A storm over a delineated catchment."""
        return Step(runner=f"{_AUTHORING}.assembler.assemble_rain_on_grid",
                    stage="author", kwargs=kwargs)
