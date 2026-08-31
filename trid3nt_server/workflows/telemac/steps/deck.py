"""The DECK step: params + forcing -> the run's own record of what it solves.

One serialization hook for the TELEMAC reach family. Everything the deck writes
is either a declared param, a produced artifact, or a class the substance module
resolved - so what the solver reads is exactly what the approved sheet said.

The sheet is SERIALIZED HERE, into the engine's own steering files, against the
liquid-boundary order the accepted mesh measured. What travels to the worker is
therefore the mesh, the authored decks and the files they name: which engine to
run, which deck it reads and which results must exist for the run to have
happened. Nothing the container receives is a knob it has to interpret.

Every optional block is threaded ONLY when it was asked for, so a run that does
not use a module leaves the deck byte-identical to the historical one.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.lib import Step
from trid3nt_server.workflows.mesh.shared.nodes import (
    read_accepted_mesh_nodes,
    read_centerline_utm,
)
from trid3nt_server.workflows.mesh.topology import read_topology

from . import author
from .errors import TelemacDyeScenarioError, TelemacDyeScenarioInputError
from .reach import MESH_H_FLOOR_M, coerce_lonlat_point, suggest_time_step_s
from .substance import (
    arm_sediment_modules,
    classify_substance,
    resolve_decay_law,
    resolve_grain,
    sanitize_substance,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.deck")

__all__ = ["WriteDeck", "stage_manifest", "write_reach_deck"]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: The names the run directory holds the reach's files under. They are the
#: ``.cas``'s own GEOMETRY / BOUNDARY CONDITIONS / RESULTS statements, so the
#: steering file reads as the record of the run it is.
_GEOMETRY_DEST = "river.slf"
_BOUNDARY_DEST = "river.cli"
_STEERING = "t2d_river.cas"
_RESULT = "r2d_river.slf"

#: Which telapy engine class runs a reach, and the identity its row carries in a
#: run listing.
_MODULE = "telemac2d"
_FAMILY = "reach"

#: The bed-load transport laws GAIA can run with suspension off. Anything else
#: (Engelund-Hansen total load etc.) falls back to the default rather than
#: wedging the solve.
_BEDLOAD_FORMULAE = (1, 2, 7)
#: The friction laws the deck interprets ``friction_coefficient`` under:
#: 2 = Chezy, 3 = Strickler, 4 = Manning.
_FRICTION_LAWS = (2, 3, 4)
_DREDGE_MODES = ("scheduled", "criterion")


def stage_manifest(case: Mapping[str, Any], run_tag: str, *,
                   outputs: list[str],
                   inputs: list[dict[str, str]] | None = None) -> str:
    """Write the worker manifest for an authored case -> its ``s3://`` URI.

    ``inputs`` is what the launcher stages into the run directory before the
    container starts, ``{gs_uri, dest}`` per entry: the accepted mesh and the
    decks this step authored. The document itself is written by the ONE manifest
    writer, under the ``case`` key the worker dispatches on.
    """
    from .open_water import OpenWaterError, stage_telemac_manifest

    try:
        return stage_telemac_manifest(
            section="case", config=case, run_tag=run_tag, outputs=outputs,
            inputs=inputs, prefix="telemac")
    except OpenWaterError as exc:
        raise TelemacDyeScenarioError("TELEMAC_DYE_STAGING_FAILED",
                                      str(exc)) from exc


#: Which TELEMAC module the class's deck COUPLES the solve with. The author
#: writes the ``COUPLING WITH`` line off this same class, and the word rides in
#: the manifest because the worker's runner choice turns on it.
_CLASS_COUPLING = {"decay": "waqtel", "do_sag": "waqtel", "sediment": "gaia"}


def _class_files(substance_class: str, *,
                 dredging: bool) -> tuple[list[str], list[str]]:
    """What a class MUST produce, and everything the supervisor brings back.

    The two lists answer two questions. ``results`` is the success convention -
    a solver that exits clean without writing these has not solved anything - so
    it names only files the ENGINE writes. ``outputs`` is what a reader may later
    open, so it also names the inputs the run was handed, which is how a solved
    run stays readable from its own prefix.
    """
    results = [_RESULT]
    outputs = [_RESULT, _GEOMETRY_DEST, _BOUNDARY_DEST, _STEERING,
               "full_listing.log", "telemac_metrics.json"]
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


def _user_fortran_dir(written: Mapping[str, Any]) -> str | None:
    """The user-Fortran DIRECTORY the author reported, or ``None`` when it wrote none.

    A class whose deck compiles user Fortran states it twice by construction -
    the steering file's own keyword and the manifest field the runner reads - so
    both are taken from the ONE report the author hands back rather than from a
    second opinion here.
    """
    fortran = (written or {}).get("fortran")
    return str(Path(str(fortran)).parent) if fortran else None


def _authoring_dir(run_tag: str) -> Path:
    rundir = Path(os.environ.get("TRID3NT_RUNS_DIR", "/tmp")) / f"telemac-{run_tag}"
    rundir.mkdir(parents=True, exist_ok=True)
    return rundir


def _mesh_field(mesh: Mapping[str, Any], name: str) -> str:
    """One field of the ACCEPTED mesh's record, or the refusal that names it.

    A mesh record missing any of them refuses: falling through would solve on a
    mesh nobody accepted, under the accepted mesh's name.
    """
    uri = (mesh or {}).get(name)
    if not uri:
        raise TelemacDyeScenarioError(
            "TELEMAC_MESH_NOT_ACCEPTED",
            f"the mesh for this run carries no {name}, so the accepted mesh "
            f"cannot be staged (mesh record: {sorted((mesh or {}))}).")
    return str(uri)


def _fitted_bed(mesh: Mapping[str, Any]) -> dict[str, Any]:
    """The downstream bed the mesh was BUILT with, which the outflow stage reads.

    Measured on the mesh, never restated here: the stage the deck prescribes has
    to be the one the geometry file carries, and a second derivation of it could
    disagree with the bed the solve starts from.
    """
    probes = getattr((mesh or {}).get("artifact"), "probes", None) or {}
    fit = probes.get("bed_fit") or {}
    if not fit:
        raise TelemacDyeScenarioError(
            "TELEMAC_MESH_BED_UNFITTED",
            "the accepted mesh carries no fitted downstream bed, so the outflow "
            "stage has no ground to be measured from; the reach mesh ask declares "
            "its bed with a downstream_along channel.")
    return dict(fit)


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

    Only a NESTOR design grade needs them - the grade a maintenance dredge digs
    back TO is the channel that is there - so the parse happens on that path
    rather than on every reach run.
    """
    points, _cells, z, _lonlat = read_accepted_mesh_nodes(
        _mesh_field(mesh, "display_uri"))
    return points, z


def _stage_authored(rundir: Path, run_tag: str,
                    names: list[str]) -> list[dict[str, str]]:
    """Upload every deck this step authored -> the manifest rows staging them."""
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not bucket:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage the authored reach decks.")
    s3 = _get_s3_client()
    rows: list[dict[str, str]] = []
    for name in names:
        key = f"telemac/{run_tag}/{name}"
        s3.put_object(Bucket=bucket, Key=key, Body=(rundir / name).read_bytes())
        rows.append({"gs_uri": f"s3://{bucket}/{key}", "dest": name})
    return rows


def _resolved_physics(friction_coefficient: float | None, friction_law: Any,
                      velocity_diffusivity: float | None,
                      tracer_diffusivity: float | None) -> dict[str, Any]:
    """Only the constitutive overrides the user actually set, range-checked.

    Anything unset is ABSENT from the deck, so the author's own default table
    stands and the deck emits the historical literal.
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
    # The erodible-bed tuning rides ONLY when armed AND set; unset lets the deck
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
    """The class, its payload, whether the bed is erodible, and the deck block."""
    substance_class, payload = classify_substance(substance)
    erodible, gradation, dredge = arm_sediment_modules(
        substance, erodible_bed=erodible_bed,
        sediment_gradation=sediment_gradation, dredging=dredging)

    # SINGLE SOURCE OF TRUTH: an armed erodible bed IS a GAIA morphodynamics run,
    # so it MUST route through the sediment class. Otherwise the flag and the
    # class gate diverge - erodible_bed reads True while the deck couples nothing,
    # and the run only LOOKS morphodynamic.
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
    """The WAQTEL O2 inflow condition: the fully-mixed discharge rides in at the top.

    Threaded only when a DO-sag config was supplied, so every other run is
    byte-identical (the deck's O2 branch omits the dye point source entirely).
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
        "do_sag_bod_mgl": float(cfg["bod_mgl"]),
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
    so a derived release is inside the meshed domain by construction rather than by
    luck. A second navigate resolved beside this one is what put it 350 m outside.
    """
    if release_pair is None:
        from pyproj import Transformer
        from shapely.geometry import LineString

        point = LineString(centerline_utm).interpolate(
            min(max(float(spill_fraction), 0.0), 1.0), normalized=True)
        lon, lat = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True).transform(
            point.x, point.y)
        return (float(lon), float(lat)), None

    from trid3nt_server.workflows.telemac.release_point import (
        contain_release_point, domain_polygon_of,
    )

    contained = await asyncio.to_thread(
        contain_release_point, point=release_pair,
        domain=domain_polygon_of(mesh.get("artifact")),
        flowline=centerline)
    return (contained.lon, contained.lat), contained.note


async def write_reach_deck(
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
) -> dict[str, Any]:
    """Serialize the approved sheet into the run's deck + the run meta.

    The MESH is the accepted one: its geometry and its boundary roles are staged
    for the solve, so the run is solved on the triangulation that was presented
    rather than on an equivalent rebuild, and the timestep follows the edge that
    mesh was BUILT at rather than the edge that was asked for.

    The CENTERLINE is the chain's own declared row - the line the section was cut
    between, the mesh was built over and the canvas shows. There is exactly one,
    so the release the deck carries, the marker on the map and the domain the
    solver integrates cannot describe three different rivers.

    The RELEASE POINT is settled against that centerline BEFORE anything is
    staged: a supplied point outside the domain polygon refuses while the user can
    still move it, one inside it is put on the flowline, and an unplaced one walks
    ``spill_fraction`` along the same line. Only then does the marker go on the
    canvas - at the point the deck actually carries - saying out loud whether the
    user placed it or the pipeline derived it.
    """
    substance = sanitize_substance(substance)
    release_pair = coerce_lonlat_point(release_coords)
    seed_lon, seed_lat = float(seed["lon"]), float(seed["lat"])

    # The granularity the deck records is the one the ACCEPTED mesh was built at,
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

    from trid3nt_server.workflows.telemac.release_layer import publish_release_point
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    run_tag = new_ulid()
    artifact = mesh.get("artifact")
    utm_epsg = int(getattr(artifact, "utm_epsg", 0) or 0)
    # The centerline is read head-to-tail from the seed the navigate was walked
    # downstream FROM, so ``spill_fraction`` counts from upstream and the bed the
    # mesh carries slopes the same way.
    centerline_utm = await asyncio.to_thread(
        read_centerline_utm, centerline, utm_epsg,
        start_lonlat=(seed_lon, seed_lat))
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
    bed_source = str((mesh.get("provenance") or {}).get("dem_source") or "staged")
    rain_mm_day = (rain or {}).get("mm_per_day")
    deck: dict[str, Any] = {
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
        # the deck author emits no wind block.
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
    }

    node_xy, node_bed = (await asyncio.to_thread(_mesh_nodes, mesh)
                         if class_block.get("dredging") else (None, None))
    rundir = _authoring_dir(run_tag)
    written = await asyncio.to_thread(
        author.author_reach_deck, rundir, deck=deck,
        geometry=_GEOMETRY_DEST, boundary=_BOUNDARY_DEST, results=_RESULT,
        cas_name=_STEERING,
        liquid_boundary_order=read_topology(
            _mesh_field(mesh, "topology_uri"))["liquid_boundary_order"],
        bed=_fitted_bed(mesh),
        source_utm=_to_utm_point(release_lonlat, utm_epsg),
        centerline_utm=centerline_utm,
        reach_polygon_utm=(await asyncio.to_thread(_to_utm, reach_polygon, utm_epsg)
                           if class_block.get("dredging") else None),
        node_xy=node_xy, node_bed=node_bed)
    from .open_water import case_section

    # The class the DECK states, which is the one the author branches on. The
    # do_sag condition is threaded onto the deck rather than classified out of
    # the substance word, so reading it off the substance would name a run
    # "tracer" that the author wrote a WAQTEL coupling into.
    deck_class = str(deck.get("substance_class") or "tracer")
    results, outputs = _class_files(deck_class,
                                    dredging=bool(class_block.get("dredging")))
    # Every file the author wrote, under its path INSIDE the run directory: the
    # oil module's user fortran is a directory the engine compiles, so the walk
    # is recursive and the manifest dest carries the same relative path.
    authored = sorted(str(p.relative_to(rundir))
                      for p in rundir.rglob("*") if p.is_file())
    return {
        "deck": deck,
        "run_tag": run_tag,
        "case": case_section(
            module=_MODULE, steering=_STEERING, results=results, family=_FAMILY,
            # The engine compiles the DIRECTORY the steering file names, so the
            # manifest channel carries the same directory the author wrote into.
            user_fortran=_user_fortran_dir(written),
            coupling=_CLASS_COUPLING.get(deck_class),
            # What the SERVER measured and the container cannot learn from the
            # files it is handed. The worker copies it into its metrics verbatim.
            echo={"utm_epsg": utm_epsg,
                  "bbox": [round(float(v), 6)
                           for v in (getattr(artifact, "bbox", None) or ())],
                  "npoin": int(mesh.get("node_count") or 0),
                  "nelem": int(mesh.get("element_count") or 0),
                  "mesh_size_m": mesh_size_m,
                  "bed_source": bed_source}),
        "outputs": outputs,
        "inputs": [
            {"gs_uri": _mesh_field(mesh, "slf_uri"), "dest": _GEOMETRY_DEST},
            {"gs_uri": _mesh_field(mesh, "cli_uri"), "dest": _BOUNDARY_DEST},
            *await asyncio.to_thread(_stage_authored, rundir, run_tag, authored)],
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
        # It rides the run META rather than the deck: the worker is handed the
        # settled coordinates and no account of how they were settled.
        "release_note": release_note,
        "discharge_note": carrier_discharge.get("note"),
        "rain_note": (rain or {}).get("note"),
        "rain_mm_per_day": rain_mm_day,
        "rain_rung": (rain or {}).get("rung"),
    }


class WriteDeck:
    """Per-engine deck serialization. One hook per engine, one shared skeleton."""

    @staticmethod
    def telemac(**kwargs: Any) -> Step:
        """The TELEMAC-2D reach deck."""
        return Step(runner=f"{_STEPS}.deck.write_reach_deck", stage="author",
                    kwargs=kwargs)
