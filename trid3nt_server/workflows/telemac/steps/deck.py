"""The DECK step: params + forcing -> the worker ReachConfig manifest.

One serialization hook for the TELEMAC reach family. Everything the deck writes
is either a declared param, a produced artifact, or a class the substance module
resolved - so what the solver reads is exactly what the approved sheet said.

Every optional block is threaded ONLY when it was asked for, so a run that does
not use a module leaves the deck byte-identical to the historical one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Mapping

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.lib import Step

from .errors import TelemacDyeScenarioError, TelemacDyeScenarioInputError
from .reach import (
    coerce_lonlat_point,
    resolve_reach_river,
    suggest_mesh_size_m,
    suggest_time_step_s,
)
from .substance import (
    arm_sediment_modules,
    classify_substance,
    resolve_decay_law,
    resolve_grain,
    sanitize_substance,
)

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.deck")

__all__ = ["WriteDeck", "normalize_bank_source", "stage_manifest", "write_reach_deck"]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: The bed-load transport laws GAIA can run with suspension off. Anything else
#: (Engelund-Hansen total load etc.) falls back to the default rather than
#: wedging the solve.
_BEDLOAD_FORMULAE = (1, 2, 7)
#: The friction laws the deck interprets ``friction_coefficient`` under:
#: 2 = Chezy, 3 = Strickler, 4 = Manning.
_FRICTION_LAWS = (2, 3, 4)
_DREDGE_MODES = ("scheduled", "criterion")


#: The ONE bank source, plus the spellings it answers to. A reach domain is the
#: REAL mapped water polygon or it is a typed refusal; there is no assumed-width
#: rung to name, so anything else REFUSES rather than canonicalizing.
_BANK_SOURCES: dict[str, tuple[str, ...]] = {
    "nhd_area": ("nhd_area", "nhdarea", "nhd", "auto"),
}


def normalize_bank_source(value: Any) -> str:
    """Coerce a bank_source to the closed set {nhd_area}.

    Absent takes the declared default; a known synonym canonicalizes; anything
    else is a typed refusal. A width is not a bank: a reach whose banks nothing
    maps has no domain, and the refusal names the supply paths instead.
    """
    if value is None or not str(value).strip():
        return "nhd_area"
    v = str(value).strip().lower().replace("-", "_")
    for canonical, spellings in _BANK_SOURCES.items():
        if v in spellings:
            return canonical
    raise TelemacDyeScenarioInputError(
        f"bank_source {value!r} is not the one source a reach domain is cut from: "
        "'nhd_area' (the real mapped NHDArea water polygon). A reach whose banks "
        "nothing maps has no domain - supply one, or name a covered reach.")


def stage_manifest(reach: dict[str, Any], run_tag: str, *,
                   mesh_only: bool = False,
                   inputs: list[dict[str, str]] | None = None) -> str:
    """Write the worker manifest to the cache bucket and return its ``s3://`` URI.

    ``mesh_only`` flags the fast mesh-preview mode: build the mesh, write the
    wireframe + gate stats, skip the solve.

    ``inputs`` is what the launcher stages into the run directory before the
    container starts, ``{gs_uri, dest}`` per entry. It carries the centerline,
    the banks and the bed this pipeline used to fetch for itself, which is why
    the worker needs no network.
    """
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise TelemacDyeScenarioError(
            "TELEMAC_DYE_STAGING_FAILED",
            "TRID3NT_CACHE_BUCKET must be set to stage the TELEMAC manifest.")
    outputs = ["r2d_river.slf", "river.slf", "river.cli", "t2d_river.cas",
               "full_listing.log", "telemac_metrics.json"]
    # The GAIA deposition SELAFIN + its steering file ship for a sediment run so
    # the postprocess can build the bed-evolution COG. A non-sediment run never
    # produces them, so the supervisor's output glob simply skips them.
    if str((reach or {}).get("substance_class") or "") == "sediment":
        outputs += ["gaia_river.slf", "gaia_river.cas"]
    if mesh_only:
        # river_mesh.npz is the accepted topology a later solve adopts, so it
        # comes back with the geometry rather than dying with the run directory.
        outputs = ["river.slf", "river.cli", "river_mesh.npz",
                   "mesh_preview.geojson", "telemac_metrics.json"]
    manifest: dict[str, Any] = {
        "reach": reach,
        "run_id": run_tag,
        "inputs": list(inputs or []),
        "telemac_args": [],  # the image CMD drives the entrypoint
        "outputs": outputs,
    }
    if mesh_only:
        manifest["mesh_only"] = True
    key = f"telemac/{run_tag}/manifest.json"
    _get_s3_client().put_object(
        Bucket=cache_bucket, Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


def _resolved_physics(friction_coefficient: float | None, friction_law: Any,
                      velocity_diffusivity: float | None,
                      tracer_diffusivity: float | None) -> dict[str, Any]:
    """Only the constitutive overrides the user actually set, range-checked.

    Anything unset is ABSENT from the manifest, so the worker ReachConfig field
    stays None and the deck author emits the historical literal.
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
                    dredge_dig_depth_m: float | None) -> dict[str, Any]:
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
    # The erodible-bed tuning rides ONLY when armed AND set; unset lets the worker
    # ReachConfig defaults apply, which keeps a non-erodible run byte-identical.
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
            dredge_dig_depth_m=dredge_dig_depth_m)
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
    river: dict[str, Any],
) -> tuple[tuple[float, float] | None, str | None]:
    """A supplied release point, settled against the domain -> ``(point, note)``.

    PRE-FLIGHT: the domain polygon the accepted mesh was cut from decides whether
    the point can be a source at all, and the fetched flowline decides where on
    the river it sits. A point the domain does not hold raises through, because
    the only alternatives are releasing somewhere the user did not choose or
    solving a source outside the water.

    ONE path, and it always has a polygon: a mesh that states no domain polygon
    refuses at the read rather than letting a point through untested.

    A run that placed no point asks nothing of the geometry and reads none of it.
    """
    if release_pair is None:
        return None, None
    from trid3nt_server.workflows.telemac.release_point import (
        contain_release_point, domain_polygon_of,
    )

    domain = domain_polygon_of(mesh.get("artifact"))
    contained = await asyncio.to_thread(
        contain_release_point, point=release_pair, domain=domain,
        flowline=river["provenance"]["centerline_uri"])
    return (contained.lon, contained.lat), contained.note


async def write_reach_deck(
    *,
    reach: dict[str, Any],
    seed: dict[str, Any],
    mesh: dict[str, Any],
    carrier_discharge: dict[str, Any],
    rain: dict[str, Any] | None = None,
    release_coords: Any = None,
    reach_seed_coords: Any = None,
    substance: str = "dye",
    reach_length_km: float = 6.0,
    channel_width_m: float = 60.0,
    sim_duration_s: float = 3600.0,
    spill_fraction: float = 0.25,
    spill_duration_s: float = 300.0,
    dye_concentration_mgl: float = 100.0,
    source_q_m3s: float = 8.0,
    mesh_resolution: str = "auto",
    mesh_resolution_m: float | None = None,
    bank_source: str = "nhd_area",
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
    do_sag_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize the approved sheet into the worker ReachConfig + the run meta.

    The MESH is the accepted one: its geometry and its boundary roles are staged
    for the solve, so the run is solved on the triangulation that was presented
    rather than on an equivalent rebuild, and the timestep follows the edge that
    mesh was BUILT at rather than the edge that was asked for.

    The RELEASE POINT is settled here, BEFORE anything is staged: the river is
    resolved first because containment is a question about real geometry, a
    supplied point outside the domain polygon refuses while the user can still
    move it, and one inside it is put on the flowline. Only then does the marker
    go on the canvas - at the point the deck actually carries, so the map and the
    solve cannot disagree - saying out loud whether the user placed it or the
    pipeline derived it.
    """
    substance = sanitize_substance(substance)
    release_pair = coerce_lonlat_point(release_coords)
    seed_pair = coerce_lonlat_point(reach_seed_coords)
    seed_lon, seed_lat = float(seed["lon"]), float(seed["lat"])

    sizing = suggest_mesh_size_m(
        reach_length_km=reach_length_km, channel_width_m=channel_width_m,
        resolution=mesh_resolution, override_m=mesh_resolution_m)
    mesh_size_m = sizing.mesh_size_m
    mesh_node_estimate = sizing.node_estimate
    mesh_resolution_label = sizing.label
    time_step_s = suggest_time_step_s(mesh_size_m, mesh=mesh.get("artifact"))
    logger.info("telemac mesh granularity: %s -> h=%.3g m (~%d nodes, dt=%.3g s, "
                "reach=%.3g km x %.3g m)%s", mesh_resolution_label, mesh_size_m,
                mesh_node_estimate, time_step_s, reach_length_km, channel_width_m,
                f" [{sizing.cap_note}]" if sizing.cap_note else "")

    substance_class, _payload, erodible, class_block = _substance_block(
        substance, erodible_bed=erodible_bed, sediment_gradation=sediment_gradation,
        dredging=dredging, decay_half_life_hours=decay_half_life_hours,
        decay_rate_per_day=decay_rate_per_day,
        sediment_type=sediment_type, grain_size_um=grain_size_um,
        bed_thickness_m=bed_thickness_m, bedload_formula=bedload_formula,
        morphological_factor=morphological_factor, dredge_mode=dredge_mode,
        dredge_volume_m3=dredge_volume_m3, dredge_disposal=dredge_disposal,
        dredge_crit_depth_m=dredge_crit_depth_m, dredge_dig_depth_m=dredge_dig_depth_m)

    from trid3nt_server.workflows.telemac.release_layer import publish_release_point
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    # The river this reach is MESHED on, fetched here and staged into the run
    # directory. It runs BEFORE everything the release depends on, because the
    # flowline a supplied point is snapped to is the one this ladder resolved.
    run_tag = new_ulid()
    river = await resolve_reach_river(
        reach=reach, seed=seed, run_tag=run_tag,
        reach_length_km=float(reach_length_km),
        bank_source=normalize_bank_source(bank_source),
        release=seed_pair)
    logger.info("telemac reach river: seed=(%.5f,%.5f) rung=%s comids=%s "
                "centerline=%s bed=%s",
                river["provenance"]["seed_lon"], river["provenance"]["seed_lat"],
                river["provenance"]["seed_rung"],
                river["provenance"]["centerline_comids"],
                river["provenance"]["centerline_sha256"][:12],
                river["provenance"]["bed_source"])

    release_lonlat, release_note = await _settle_release(
        release_pair, mesh=mesh, river=river)

    # The marker rides BEFORE the solve, so the user sees the input against the
    # mesh rather than only in the results, and it carries the SETTLED point: the
    # containment test above refuses anything the domain does not hold, so no run
    # carries a user-placed marker the plume disagrees with.
    release_point = release_lonlat or seed_pair
    await publish_release_point(
        current_emitter(),
        lon=(release_point or (seed_lon, seed_lat))[0],
        lat=(release_point or (seed_lon, seed_lat))[1],
        user_supplied=release_point is not None,
        reach_name=reach["slug"],
        label="Outfall" if do_sag_config else "Release point")

    rain_mm_day = (rain or {}).get("mm_per_day")
    deck: dict[str, Any] = {
        "name": reach["slug"],
        # The seed the CENTERLINE was resolved from, not the one the ladder was
        # handed: the manifest is the run's record of what it meshed, and the two
        # differ whenever a re-seed rung fired.
        "seed_lon": round(float(river["provenance"]["seed_lon"]), 6),
        "seed_lat": round(float(river["provenance"]["seed_lat"]), 6),
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
        "channel_width_m": float(channel_width_m),
        "bank_source": normalize_bank_source(bank_source),
        # WHICH dataset the staged bed came from. The worker opens a file and
        # cannot know, so the label travels with the file - otherwise the run's
        # own metrics could not tell a GLO-30 bed from the 3DEP one the ladder
        # fell to, which is exactly the substitution the loudness floor exists
        # to keep visible.
        "bed_source": str(river["provenance"]["bed_source"] or "staged"),
        "mesh_size_m": mesh_size_m,
        "time_step_s": time_step_s,
        **({"output_interval_min": float(output_interval_min)}
           if output_interval_min is not None else {}),
        "dye_conc_mgl": float(dye_concentration_mgl),
        # A picked release point overrides spill_frac. It is the SETTLED point -
        # already inside the domain and already on the flowline - so the worker
        # places the source where it is told and tests nothing.
        **({"release_lon": round(release_lonlat[0], 6),
            "release_lat": round(release_lonlat[1], 6)}
           if release_lonlat is not None else {}),
        "spill_frac": float(min(max(spill_fraction, 0.0), 1.0)),
        "pulse_window_s": float(spill_duration_s),
        "source_q_m3s": float(source_q_m3s),
        "inflow_q_m3s": float(carrier_discharge["m3s"]),
        "duration_s": float(sim_duration_s),
    }
    return {
        "deck": deck,
        "run_tag": run_tag,
        "inputs": [*river["inputs"], *_accepted_mesh_inputs(mesh)],
        "river": river["provenance"],
        "mesh_id": mesh.get("mesh_id"),
        "substance": substance,
        "substance_class": substance_class,
        "erodible_bed": bool(erodible),
        "reach_name": reach["slug"],
        "location_name": reach["name"],
        "mesh_size_m": mesh_size_m,
        "mesh_node_estimate": mesh_node_estimate,
        "mesh_resolution_label": mesh_resolution_label,
        # Present ONLY when the user's own explicit mesh_resolution_m was moved by
        # a sizing rule; the products turn it into a provenance row so an
        # overridden lever is never silently overridden.
        "mesh_resolution_note": sizing.cap_note,
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


#: The accepted mesh's record -> the manifest ``dest`` the worker adopts it under.
#: The topology bundle is what makes adoption possible at all: a SELAFIN states
#: which nodes are on a boundary and never which stretch of it the flow enters by.
_ACCEPTED_MESH_DESTS: dict[str, str] = {"topology_uri": "river_mesh.npz"}


def _accepted_mesh_inputs(mesh: Mapping[str, Any]) -> list[dict[str, str]]:
    """Manifest rows staging the accepted mesh into the solve's run directory.

    A mesh record missing its topology refuses: silently falling back to a
    worker-side rebuild would solve on a mesh nobody accepted, under the accepted
    mesh's name.
    """
    rows: list[dict[str, str]] = []
    for field_name, dest in _ACCEPTED_MESH_DESTS.items():
        uri = (mesh or {}).get(field_name)
        if not uri:
            raise TelemacDyeScenarioError(
                "TELEMAC_MESH_NOT_ACCEPTED",
                f"the corridor mesh for this run carries no {field_name}, so the "
                "accepted topology cannot be staged and the solve would mesh one "
                f"of its own instead (mesh record: {sorted((mesh or {}))}).")
        rows.append({"gs_uri": str(uri), "dest": dest})
    return rows


class WriteDeck:
    """Per-engine deck serialization. One hook per engine, one shared skeleton."""

    @staticmethod
    def telemac(**kwargs: Any) -> Step:
        """The TELEMAC-2D reach deck."""
        return Step(runner=f"{_STEPS}.deck.write_reach_deck", stage="author",
                    kwargs=kwargs)
