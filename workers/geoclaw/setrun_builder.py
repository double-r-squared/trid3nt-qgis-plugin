"""GeoClaw ``setrun.py`` authoring — the build_spec -> Clawpack deck adapter.

The GeoClaw analogue of ``workers/modflow/gwt_adapter.py`` (which authors
a FloPy deck from typed args). This module is the DETERMINISTIC, UNIT-TESTABLE
core of the GeoClaw worker: it parses the agent-staged ``build_spec`` JSON and
emits a canonical Clawpack/GeoClaw ``setrun.py`` (plus, for a tsunami scenario, a
``maketopo`` helper that synthesizes a dtopo) over the AOI + topo DEM + a driver
scenario.

It deliberately does NOT import clawpack or run the solver — it only WRITES the
deck files (a ``setrun.py`` Python module, a per-application ``Makefile`` that
provides the ``.output`` target, and a small ``qinit`` / ``dtopo`` data file when
the scenario needs one). The entrypoint then invokes the Clawpack ``runclaw`` /
``make .output`` machinery against the authored deck. Splitting the authoring out
(mirroring gwt_adapter) is what makes the worker testable with NO Fortran
toolchain present.

Canonical real-world pipeline (mirrored, not invented):
    GeoClaw modellers write a ``setrun.py`` that returns a ``clawpack.clawutil
    .data.ClawRunData`` object. The load-bearing blocks, in the order GeoClaw's
    own examples use them, are:
      - clawdata: domain (lower/upper x,y), base grid (num_cells), t span +
        evenly-spaced output_times (the fort.q frames), CFL, bc (boundary
        conditions).
      - geo_data: gravity, coordinate_system=2 (lat/lon), earth_radius,
        sea_level, friction (manning_coefficient), dry_tolerance.
      - topo_data.topofiles: the topography file(s) over the AOI.
      - amrdata: amr_levels_max + refinement_ratios (adaptive mesh refinement).
      - qinit_data (dam_break): a raised-column perturbation file.
      - dtopo_data.dtopofiles (tsunami): the seafloor-deformation source.
      - surge_data (surge): parametric Holland-1980 wind + pressure forcing
        (geoclaw.surge) driven by a storm-track file (t, lon, lat, max wind
        speed/radius, central pressure, storm radius). A drag_law selector
        (none | Garratt | Powell) sets the wind-stress law; the run window opens
        BEFORE landfall (clawdata.t0 = t0_s, negative) so the storm spins up.

The build_spec schema (authored agent-side by ``workflows/run_geoclaw.py``):
    {
      "scenario": "dam_break" | "tsunami" | "surge",
      "bbox": [min_lon, min_lat, max_lon, max_lat],   # EPSG:4326 (the AOI)
      "domain_bbox": [min_lon, min_lat, max_lon, max_lat],  # optional; the
          # COMPUTATIONAL DOMAIN (clawdata bounds). For a tsunami the composer
          # sets this to an OFFSHORE-EXTENDED box that spans the Okada source ->
          # the AOI coast; absent -> the domain is the AOI ``bbox``.
      "topo_file": "topo.asc",        # staged DEM (topotype-3 ESRI ASCII)
      "sim_duration_s": 3600.0,
      "output_frames": 24,
      "amr_levels": 2,
      "manning_n": 0.025,
      "sea_level_m": 0.0,
      "base_num_cells": [40, 40],     # optional; base grid resolution
      # dam_break:
      "dam_break_depth_m": 10.0,
      "source_lonlat": [lon, lat],    # optional; AOI centroid otherwise
      # tsunami:
      "dtopo_file": "dtopo.tt3",      # optional staged dtopo; else synthesize
      "source_magnitude": 8.0,
      # surge:
      "surge_forcing_file": "surge.csv",  # optional staged hydrograph (unused
          # by the parametric path; reserved for a gridded-wind upgrade)
      "storm_track": [                # OPTIONAL parametric-Holland storm track;
          # each point is [t_s, lon, lat, max_wind_speed_ms, max_wind_radius_m,
          # central_pressure_pa, storm_radius_m] with t_s SECONDS RELATIVE TO
          # LANDFALL (negative before). Absent -> a synthetic demo track making
          # landfall at the AOI centroid.
          [-43200.0, -95.0, 26.0, 45.0, 46000.0, 96000.0, 500000.0],
          [0.0, -94.7, 29.3, 49.0, 46000.0, 95000.0, 500000.0],
      ],
      "wind_drag_law": "garratt",     # none | garratt | powell (the wind-stress law)
      "t0_s": -43200.0,               # run start, s from landfall (surge: < 0)
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "GeoClawDeckError",
    "GeoClawBuildSpec",
    "DeckManifest",
    "parse_build_spec",
    "render_setrun_py",
    "render_qinit_data",
    "render_maketopo_dtopo",
    "render_storm_file",
    "resolve_storm_track",
    "render_makefile",
    "build_geoclaw_deck",
]


class GeoClawDeckError(RuntimeError):
    """Raised on a malformed build_spec / unsupported scenario.

    Carries an open-set ``error_code`` so the entrypoint records a typed failure.
    """

    error_code: str = "GEOCLAW_DECK_BUILD_FAILED"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_VALID_SCENARIOS = {"dam_break", "tsunami", "surge", "thacker"}

#: PARSER VERSION -- bump whenever a build_spec top-level field is added,
#: renamed, or retired. Named in the strict-field error so a stale worker
#: image (which silently dropped unknown fields before ADR 0158) is
#: distinguishable from a genuinely-malformed caller.
_PARSER_VERSION = "geoclaw-spec-7"

#: Gravitational acceleration (m/s^2) the Thacker deck uses. Kept in lockstep with
#: ``trid3nt_contracts.geoclaw_thacker.THACKER_GRAVITY`` (the agent-side analytic
#: reference) -- duplicated here (not imported) so the worker deck-author stays
#: dependency-free (no trid3nt_contracts in the worker image).
_THACKER_GRAVITY = 9.81

#: GeoClaw ``surge_data.drag_law`` codes (storm_module.f90): the wind-stress
#: coefficient law selected for the surge wind forcing. The knob MUST land a
#: distinct integer (an unknown name raises) so a drag-law choice measurably
#: changes the run rather than silently no-opping (the ADR 0143 lesson).
_DRAG_LAW_CODES: dict[str, int] = {"none": 0, "garratt": 1, "powell": 2}

#: Every top-level build_spec field ``parse_build_spec`` reads. No legacy /
#: envelope-only fields exist here -- ``manifest.get("build_spec")`` is the
#: pure spec dict (run_id / inputs / outputs live as SIBLING manifest keys,
#: never inside build_spec), so an unknown key here is always a genuine typo
#: or a stale/dropped composer field, never a deliberately-ignored envelope key.
_KNOWN_SPEC_FIELDS = frozenset(
    {
        "scenario",
        "bbox",
        "domain_bbox",
        "topo_file",
        "sim_duration_s",
        "output_frames",
        "amr_levels",
        "manning_n",
        "sea_level_m",
        "base_num_cells",
        "dam_break_depth_m",
        "source_lonlat",
        "dtopo_file",
        "finite_fault_file",
        "source_magnitude",
        "fault_strike_deg",
        "fault_dip_deg",
        "fault_rake_deg",
        "fault_depth_km",
        "surge_forcing_file",
        "storm_track",
        "wind_drag_law",
        "t0_s",
        "extra_topo_files",
        "fgmax_arrival_tol_m",
        "coastal_gauge_lonlat",
        "amr_regions",
        "manning_coefficients",
        "manning_break",
        "lagrangian_particles",
        "fgmax_mask",
        "fgout_frames",
        "bowl_a_m",
        "bowl_h0_m",
        "bowl_eta_amp",
        "bouss_equations",
        "bouss_min_depth",
        "bouss_min_level",
        "bouss_max_level",
    }
)


def _reject_unknown_spec_fields(raw: dict[str, Any]) -> None:
    """Raise loudly if ``raw`` carries a top-level key ``parse_build_spec`` never
    reads (ADR 0158 -- the ADR 0148 lesson: a stale image silently dropped
    unknown build_spec fields and two registered knob templates ran as no-ops).
    """
    unknown = sorted(set(raw) - _KNOWN_SPEC_FIELDS)
    if unknown:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_UNKNOWN_FIELDS",
            f"build_spec carries unknown field(s) {unknown} that parser "
            f"{_PARSER_VERSION} does not read -- this SILENTLY no-ops the "
            f"intended knob(s) rather than applying them. Either the caller has "
            f"a typo, or the worker image is stale (rebuild it -- ADR 0148). "
            f"Known fields: {sorted(_KNOWN_SPEC_FIELDS)}.",
        )


@dataclass
class GeoClawBuildSpec:
    """The typed, validated build_spec the deck author consumes.

    A plain dataclass (no pydantic dep in the worker image) holding exactly the
    fields ``render_setrun_py`` needs. ``parse_build_spec`` validates + fills
    defaults from the raw manifest dict.
    """

    scenario: str
    bbox: tuple[float, float, float, float]
    topo_file: str
    # The COMPUTATIONAL DOMAIN (clawdata lower/upper). When None the domain is the
    # ``bbox`` (the AOI) -- the back-compat default. For an OFFSHORE-source scenario
    # (tsunami) the composer passes a domain that EXTENDS offshore so it SPANS the
    # Okada source -> the AOI coast: an Okada deformation must sit inside the domain
    # over a deep-water column, and the deep-to-shallow propagation path must be
    # resolved, before the wave can run up the AOI. The AOI (``bbox``) still drives
    # the fine-AMR region + fgmax monitor + gauge (the run-up is observed there).
    domain_bbox: tuple[float, float, float, float] | None = None
    sim_duration_s: float = 3600.0
    output_frames: int = 24
    amr_levels: int = 2
    manning_n: float = 0.025
    sea_level_m: float = 0.0
    base_num_cells: tuple[int, int] = (40, 40)
    # dam_break.
    dam_break_depth_m: float = 10.0
    source_lonlat: tuple[float, float] | None = None
    # tsunami.
    dtopo_file: str | None = None
    # Staged finite-fault subfault table (clawpack CSVFault CSV) -> the worker
    # assembles a MULTI-subfault Okada dtopo (a real inverted slip distribution)
    # instead of the single-subfault scaling synthesis. Absent -> single subfault.
    finite_fault_file: str | None = None
    source_magnitude: float = 8.0
    # tsunami Okada fault geometry (user-gated; synthetic defaults when omitted).
    fault_strike_deg: float | None = None
    fault_dip_deg: float | None = None
    fault_rake_deg: float | None = None
    fault_depth_km: float | None = None
    # surge.
    surge_forcing_file: str | None = None
    # Parametric-Holland storm track: each point is
    # (t_s, lon, lat, max_wind_speed_ms, max_wind_radius_m, central_pressure_pa,
    # storm_radius_m) with t_s SECONDS RELATIVE TO LANDFALL (negative before).
    # Empty -> a synthetic demo track (landfall at the AOI centroid) is generated.
    storm_track: list[tuple[float, float, float, float, float, float, float]] = (
        field(default_factory=list)
    )
    # Wind-stress drag law for the surge wind forcing: none | garratt | powell.
    wind_drag_law: str = "garratt"
    # Run START time, seconds from landfall. Surge opens the window BEFORE
    # landfall (< 0) so the storm spins up; the run spans [t0_s, t0_s +
    # sim_duration_s]. Defaults 0.0 (dam_break/tsunami keep t0 == 0, byte-identical).
    t0_s: float = 0.0
    # Nested DEM(s), ordered coarse->fine, appended after the primary topo.
    extra_topo_files: list[str] = field(default_factory=list)
    # fgmax (max water depth / speed / arrival time) monitoring.
    fgmax_arrival_tol_m: float = 0.01
    # Coastal gauge (lon, lat); deterministic seaward-edge fallback if None.
    coastal_gauge_lonlat: tuple[float, float] | None = None
    # Explicit AMR refinement windows, each an 8-tuple in GeoClaw regiondata order
    # (min_level, max_level, t_start_s, t_end_s, min_lon, max_lon, min_lat, max_lat)
    # appended AFTER the engine default region tiers.
    amr_regions: list[tuple[float, float, float, float, float, float, float, float]] = (
        field(default_factory=list)
    )
    # Spatially-varying Manning bottom-friction. manning_coefficients is a list of n
    # values for topography bands split by manning_break (ascending, length =
    # len(coefficients) - 1). None -> the single global manning_n is used.
    manning_coefficients: list[float] | None = None
    manning_break: list[float] = field(default_factory=list)
    # Lagrangian (particle-tracking) gauges: each (lon, lat) is added as a gauge
    # advected by the flow (gtype='lagrangian'), tracing the depth-averaged
    # velocity. Empty -> only the stationary coastal gauge is emitted.
    lagrangian_particles: list[tuple[float, float]] = field(default_factory=list)
    # fgmax point set: "full" (uniform grid, point_style=2) or "onshore" (the DEM
    # onshore cells only, point_style=4 with a topotype-3 mask). "full" is the
    # byte-identical default; "onshore" needs the entrypoint mask-gen step.
    fgmax_mask: str = "full"
    # fgout smooth-animation frame count. When > 0 (tsunami / surge) an fgout
    # fixed-grid monitor is emitted: a uniform grid over the AOI at the AOI ambient
    # cell size, dumped at ``fgout_frames`` EVENLY-SPACED times (decoupled from the
    # AMR fort.q cadence) -> SMOOTH single-resolution animation frames. 0 (default)
    # emits no fgout block -> byte-identical to a pre-fgout deck.
    fgout_frames: int = 0
    # Thacker paraboloid-basin V&V (scenario="thacker"). The basin radius a (m),
    # central still-water depth h0 (m), and dimensionless oscillation amplitude A
    # in (0,1). Consumed ONLY for scenario="thacker" (the worker generates the
    # bowl topo + analytic still-surface qinit from them); None otherwise.
    bowl_a_m: float | None = None
    bowl_h0_m: float | None = None
    bowl_eta_amp: float | None = None
    # Boussinesq (SGN) dispersive solver (num_eqn=5, implicit PETSc/MPI solve).
    # bouss_equations: 0=SWE (non-dispersive, the byte-identical default), 1=Madsen-
    # Sorensen, 2=SGN (Serre-Green-Naghdi, recommended). >0 selects the num_eqn=5
    # bouss executable (Makefile.bouss + PETSc) so deep-water tsunami propagation
    # carries the dispersive trailing wave train SWE cannot resolve; the correction
    # is applied only in water deeper than bouss_min_depth (shallow/run-up cells stay
    # on the robust SWE solver) and only on AMR levels in [bouss_min_level,
    # bouss_max_level]. bouss_min_depth in METERS. Requires the PETSc-enabled image.
    bouss_equations: int = 0
    bouss_min_depth: float = 10.0
    bouss_min_level: int = 1
    bouss_max_level: int = 10


@dataclass
class DeckManifest:
    """Provenance the deck author returns (echoed into completion for narration).

    Mirrors ``gwt_adapter.DeckManifest``: a small typed record the entrypoint /
    postprocess can read to narrate typed numbers about what was built (domain,
    grid, driver) without re-parsing the setrun.py.
    """

    scenario: str
    bbox: tuple[float, float, float, float]
    base_num_cells: tuple[int, int]
    amr_levels: int
    output_frames: int
    sim_duration_s: float
    files_written: list[str] = field(default_factory=list)
    driver_descriptor: str = ""
    # >0 when the SGN/MS Boussinesq (num_eqn=5) executable was authored -- the
    # entrypoint reads this to inject the PETSc/MPI env into the make subprocess.
    bouss_equations: int = 0


def parse_build_spec(raw: dict[str, Any]) -> GeoClawBuildSpec:
    """Validate the raw manifest ``build_spec`` dict -> a typed ``GeoClawBuildSpec``.

    Raises ``GeoClawDeckError`` (typed code) on a missing/invalid field so the
    entrypoint records an honest terminal error rather than crashing mid-deck.
    """
    if not isinstance(raw, dict):
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID", f"build_spec must be a JSON object, got {type(raw)}"
        )
    _reject_unknown_spec_fields(raw)

    scenario = str(raw.get("scenario") or "dam_break").strip().lower()
    if scenario not in _VALID_SCENARIOS:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            f"scenario must be one of {sorted(_VALID_SCENARIOS)}, got {scenario!r}",
        )

    bbox_raw = raw.get("bbox")
    if not isinstance(bbox_raw, (list, tuple)) or len(bbox_raw) != 4:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            f"bbox must be [min_lon, min_lat, max_lon, max_lat], got {bbox_raw!r}",
        )
    try:
        bbox = tuple(float(v) for v in bbox_raw)  # type: ignore[assignment]
    except (TypeError, ValueError) as exc:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID", f"bbox values must be numeric: {bbox_raw!r}"
        ) from exc
    min_lon, min_lat, max_lon, max_lat = bbox
    if not (min_lon < max_lon and min_lat < max_lat):
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            f"bbox must satisfy min_lon<max_lon and min_lat<max_lat, got {bbox}",
        )

    topo_file = str(raw.get("topo_file") or "").strip()
    if not topo_file:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID", "build_spec.topo_file is required (the staged DEM)"
        )

    def _num(key: str, default: float) -> float:
        v = raw.get(key)
        return float(v) if v is not None else float(default)

    def _int(key: str, default: int) -> int:
        v = raw.get(key)
        return int(v) if v is not None else int(default)

    base_cells_raw = raw.get("base_num_cells") or [40, 40]
    if not isinstance(base_cells_raw, (list, tuple)) or len(base_cells_raw) != 2:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            f"base_num_cells must be [nx, ny], got {base_cells_raw!r}",
        )
    base_num_cells = (int(base_cells_raw[0]), int(base_cells_raw[1]))
    if base_num_cells[0] < 2 or base_num_cells[1] < 2:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID", f"base_num_cells must each be >= 2, got {base_num_cells}"
        )

    src = raw.get("source_lonlat")
    source_lonlat: tuple[float, float] | None = None
    if isinstance(src, (list, tuple)) and len(src) == 2:
        source_lonlat = (float(src[0]), float(src[1]))

    # Optional computational domain (clawdata bounds). Validated like ``bbox``;
    # None -> the deck uses ``bbox`` (back-compat). The composer supplies an
    # offshore-extended domain for the tsunami (Okada) scenario.
    dom_raw = raw.get("domain_bbox")
    domain_bbox: tuple[float, float, float, float] | None = None
    if dom_raw is not None:
        if not isinstance(dom_raw, (list, tuple)) or len(dom_raw) != 4:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"domain_bbox must be [min_lon, min_lat, max_lon, max_lat], got {dom_raw!r}",
            )
        try:
            domain_bbox = tuple(float(v) for v in dom_raw)  # type: ignore[assignment]
        except (TypeError, ValueError) as exc:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"domain_bbox values must be numeric: {dom_raw!r}",
            ) from exc
        d0, d1, d2, d3 = domain_bbox  # type: ignore[misc]
        if not (d0 < d2 and d1 < d3):
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"domain_bbox must satisfy min_lon<max_lon and min_lat<max_lat, got {domain_bbox}",
            )

    gauge = raw.get("coastal_gauge_lonlat")
    coastal_gauge_lonlat: tuple[float, float] | None = None
    if isinstance(gauge, (list, tuple)) and len(gauge) == 2:
        coastal_gauge_lonlat = (float(gauge[0]), float(gauge[1]))

    extra_topo_raw = raw.get("extra_topo_files")
    if extra_topo_raw is None:
        extra_topo_files: list[str] = []
    elif isinstance(extra_topo_raw, (list, tuple)):
        extra_topo_files = [str(f).strip() for f in extra_topo_raw if str(f).strip()]
    else:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            f"extra_topo_files must be a list of file names, got {extra_topo_raw!r}",
        )

    def _opt_num(key: str) -> float | None:
        v = raw.get(key)
        return float(v) if v is not None else None

    # Explicit AMR refinement windows. Accept either the 8-tuple regiondata order
    # or a dict with named keys; normalize to the 8-tuple the setrun emits.
    amr_regions_raw = raw.get("amr_regions") or []
    if not isinstance(amr_regions_raw, (list, tuple)):
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            f"amr_regions must be a list of region windows, got {amr_regions_raw!r}",
        )
    amr_regions: list[
        tuple[float, float, float, float, float, float, float, float]
    ] = []
    for r in amr_regions_raw:
        if isinstance(r, dict):
            try:
                tup = (
                    float(r["min_level"]),
                    float(r["max_level"]),
                    float(r["t_start_s"]),
                    float(r["t_end_s"]),
                    float(r["min_lon"]),
                    float(r["max_lon"]),
                    float(r["min_lat"]),
                    float(r["max_lat"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise GeoClawDeckError(
                    "GEOCLAW_SPEC_INVALID",
                    f"amr_regions window missing/invalid field: {r!r} ({exc})",
                ) from exc
        elif isinstance(r, (list, tuple)) and len(r) == 8:
            try:
                tup = tuple(float(v) for v in r)  # type: ignore[assignment]
            except (TypeError, ValueError) as exc:
                raise GeoClawDeckError(
                    "GEOCLAW_SPEC_INVALID",
                    f"amr_regions window values must be numeric: {r!r}",
                ) from exc
        else:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"amr_regions window must be an 8-tuple or dict, got {r!r}",
            )
        if tup[1] < tup[0]:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"amr_regions max_level < min_level: {tup}",
            )
        amr_regions.append(tup)  # type: ignore[arg-type]

    # Spatially-varying Manning friction (coefficients + elevation breakpoints).
    mc_raw = raw.get("manning_coefficients")
    manning_coefficients: list[float] | None = None
    manning_break: list[float] = []
    if mc_raw is not None:
        if not isinstance(mc_raw, (list, tuple)) or not mc_raw:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"manning_coefficients must be a non-empty list, got {mc_raw!r}",
            )
        try:
            manning_coefficients = [float(v) for v in mc_raw]
        except (TypeError, ValueError) as exc:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"manning_coefficients must be numeric: {mc_raw!r}",
            ) from exc
        if any(n <= 0.0 for n in manning_coefficients):
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"manning coefficients must be > 0: {manning_coefficients}",
            )
        mb_raw = raw.get("manning_break") or []
        try:
            manning_break = [float(v) for v in mb_raw]
        except (TypeError, ValueError) as exc:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID", f"manning_break must be numeric: {mb_raw!r}"
            ) from exc
        if len(manning_break) != len(manning_coefficients) - 1:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"manning_break length ({len(manning_break)}) must equal "
                f"len(manning_coefficients) - 1 ({len(manning_coefficients) - 1})",
            )
        if any(
            manning_break[i] >= manning_break[i + 1]
            for i in range(len(manning_break) - 1)
        ):
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"manning_break must be strictly ascending: {manning_break}",
            )

    # Lagrangian particle seed points (each an (lon, lat) 2-tuple).
    lp_raw = raw.get("lagrangian_particles")
    lagrangian_particles: list[tuple[float, float]] = []
    if lp_raw is not None:
        if not isinstance(lp_raw, (list, tuple)):
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"lagrangian_particles must be a list of (lon, lat), got {lp_raw!r}",
            )
        for pt in lp_raw:
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise GeoClawDeckError(
                    "GEOCLAW_SPEC_INVALID",
                    f"lagrangian_particles entry must be (lon, lat), got {pt!r}",
                )
            try:
                lagrangian_particles.append((float(pt[0]), float(pt[1])))
            except (TypeError, ValueError) as exc:
                raise GeoClawDeckError(
                    "GEOCLAW_SPEC_INVALID",
                    f"lagrangian_particles values must be numeric: {pt!r}",
                ) from exc

    # Parametric-Holland storm track (surge). Accept either the 7-tuple order or
    # a dict with named keys; normalize to the 7-tuple the storm file emits.
    st_raw = raw.get("storm_track") or []
    if not isinstance(st_raw, (list, tuple)):
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            f"storm_track must be a list of track points, got {st_raw!r}",
        )
    storm_track: list[tuple[float, float, float, float, float, float, float]] = []
    for pt in st_raw:
        if isinstance(pt, dict):
            try:
                tup = (
                    float(pt["t_s"]),
                    float(pt["lon"]),
                    float(pt["lat"]),
                    float(pt["max_wind_speed_ms"]),
                    float(pt["max_wind_radius_m"]),
                    float(pt["central_pressure_pa"]),
                    float(pt.get("storm_radius_m", 500000.0)),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise GeoClawDeckError(
                    "GEOCLAW_SPEC_INVALID",
                    f"storm_track point missing/invalid field: {pt!r} ({exc})",
                ) from exc
        elif isinstance(pt, (list, tuple)) and len(pt) in (6, 7):
            try:
                vals = [float(v) for v in pt]
            except (TypeError, ValueError) as exc:
                raise GeoClawDeckError(
                    "GEOCLAW_SPEC_INVALID",
                    f"storm_track point values must be numeric: {pt!r}",
                ) from exc
            if len(vals) == 6:  # storm_radius omitted -> 500 km fill (GeoClaw default)
                vals.append(500000.0)
            tup = tuple(vals)  # type: ignore[assignment]
        else:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"storm_track point must be a 6/7-tuple or dict, got {pt!r}",
            )
        if tup[5] <= 0.0:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"storm_track central_pressure_pa must be > 0 (Pa): {tup!r}",
            )
        storm_track.append(tup)  # type: ignore[arg-type]
    # Track times must be strictly ascending (GeoClaw interpolates the storm in
    # time; a non-monotone file corrupts the interpolation).
    if any(
        storm_track[i][0] >= storm_track[i + 1][0]
        for i in range(len(storm_track) - 1)
    ):
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            f"storm_track times (t_s) must be strictly ascending: "
            f"{[p[0] for p in storm_track]}",
        )

    wind_drag_law = str(raw.get("wind_drag_law") or "garratt").strip().lower()
    if wind_drag_law not in _DRAG_LAW_CODES:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            f"wind_drag_law must be one of {sorted(_DRAG_LAW_CODES)}, "
            f"got {wind_drag_law!r}",
        )

    # fgmax point set selector.
    fgmax_mask = str(raw.get("fgmax_mask") or "full").strip().lower()
    if fgmax_mask not in ("full", "onshore"):
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            f"fgmax_mask must be 'full' or 'onshore', got {fgmax_mask!r}",
        )

    sim_duration_s = _num("sim_duration_s", 3600.0)
    if sim_duration_s <= 0:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID", f"sim_duration_s must be > 0, got {sim_duration_s}"
        )
    output_frames = _int("output_frames", 24)
    if output_frames < 1:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID", f"output_frames must be >= 1, got {output_frames}"
        )
    amr_levels = _int("amr_levels", 2)
    if amr_levels < 1:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID", f"amr_levels must be >= 1, got {amr_levels}"
        )
    fgout_frames = _int("fgout_frames", 0)
    if fgout_frames < 0:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID", f"fgout_frames must be >= 0, got {fgout_frames}"
        )

    # --- Thacker bowl parameters (mutually exclusive with a geographic scenario).
    bowl_a_m = _opt_num("bowl_a_m")
    bowl_h0_m = _opt_num("bowl_h0_m")
    bowl_eta_amp = _opt_num("bowl_eta_amp")
    if scenario == "thacker":
        if bowl_a_m is None or bowl_h0_m is None or bowl_eta_amp is None:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                "scenario='thacker' requires bowl_a_m, bowl_h0_m, bowl_eta_amp",
            )
        if bowl_a_m <= 0.0 or bowl_h0_m <= 0.0:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"bowl_a_m and bowl_h0_m must be > 0, got a={bowl_a_m}, h0={bowl_h0_m}",
            )
        if not (0.0 < bowl_eta_amp < 1.0):
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"bowl_eta_amp must be in (0, 1), got {bowl_eta_amp}",
            )
    elif any(v is not None for v in (bowl_a_m, bowl_h0_m, bowl_eta_amp)):
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            "bowl_a_m / bowl_h0_m / bowl_eta_amp are only valid for "
            f"scenario='thacker', not scenario={scenario!r}",
        )

    # --- Boussinesq (SGN) dispersive solver knobs. bouss_equations selects the
    # num_eqn=5 executable when > 0; the depth/level knobs are only meaningful then.
    bouss_equations = _int("bouss_equations", 0)
    if bouss_equations not in (0, 1, 2):
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            "bouss_equations must be 0 (SWE), 1 (Madsen-Sorensen) or 2 (SGN), "
            f"got {bouss_equations}",
        )
    bouss_min_depth = _num("bouss_min_depth", 10.0)
    bouss_min_level = _int("bouss_min_level", 1)
    bouss_max_level = _int("bouss_max_level", 10)
    if bouss_equations != 0:
        if scenario == "thacker":
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                "bouss_equations > 0 is not supported for scenario='thacker' "
                "(the closed-basin planar V&V deck has its own renderer)",
            )
        if bouss_min_depth <= 0.0:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                f"bouss_min_depth must be > 0, got {bouss_min_depth}",
            )
        if bouss_min_level < 1 or bouss_max_level < bouss_min_level:
            raise GeoClawDeckError(
                "GEOCLAW_SPEC_INVALID",
                "require 1 <= bouss_min_level <= bouss_max_level, got "
                f"min={bouss_min_level}, max={bouss_max_level}",
            )

    return GeoClawBuildSpec(
        scenario=scenario,
        bbox=bbox,  # type: ignore[arg-type]
        topo_file=topo_file,
        domain_bbox=domain_bbox,
        sim_duration_s=sim_duration_s,
        output_frames=output_frames,
        amr_levels=amr_levels,
        manning_n=_num("manning_n", 0.025),
        sea_level_m=_num("sea_level_m", 0.0),
        base_num_cells=base_num_cells,
        dam_break_depth_m=_num("dam_break_depth_m", 10.0),
        source_lonlat=source_lonlat,
        dtopo_file=(str(raw["dtopo_file"]).strip() if raw.get("dtopo_file") else None),
        finite_fault_file=(
            str(raw["finite_fault_file"]).strip() if raw.get("finite_fault_file") else None
        ),
        source_magnitude=_num("source_magnitude", 8.0),
        fault_strike_deg=_opt_num("fault_strike_deg"),
        fault_dip_deg=_opt_num("fault_dip_deg"),
        fault_rake_deg=_opt_num("fault_rake_deg"),
        fault_depth_km=_opt_num("fault_depth_km"),
        surge_forcing_file=(
            str(raw["surge_forcing_file"]).strip()
            if raw.get("surge_forcing_file")
            else None
        ),
        storm_track=storm_track,
        wind_drag_law=wind_drag_law,
        t0_s=_num("t0_s", 0.0),
        extra_topo_files=extra_topo_files,
        fgmax_arrival_tol_m=_num("fgmax_arrival_tol_m", 0.01),
        coastal_gauge_lonlat=coastal_gauge_lonlat,
        amr_regions=amr_regions,
        manning_coefficients=manning_coefficients,
        manning_break=manning_break,
        lagrangian_particles=lagrangian_particles,
        fgmax_mask=fgmax_mask,
        fgout_frames=fgout_frames,
        bowl_a_m=bowl_a_m,
        bowl_h0_m=bowl_h0_m,
        bowl_eta_amp=bowl_eta_amp,
        bouss_equations=bouss_equations,
        bouss_min_depth=bouss_min_depth,
        bouss_min_level=bouss_min_level,
        bouss_max_level=bouss_max_level,
    )


def _centroid(spec: GeoClawBuildSpec) -> tuple[float, float]:
    """The driver source point — explicit ``source_lonlat`` or the AOI centroid.

    For a tsunami the composer resolves ``source_lonlat`` to an OFFSHORE,
    over-deep-water point (and extends ``domain_bbox`` to span it) BEFORE the deck
    is authored, so this honors that point verbatim. The AOI-centroid fallback is
    retained only for the dam_break qinit column (an onshore release) and as a
    last-resort when no source was resolved.
    """
    if spec.source_lonlat is not None:
        return spec.source_lonlat
    min_lon, min_lat, max_lon, max_lat = spec.bbox
    return (0.5 * (min_lon + max_lon), 0.5 * (min_lat + max_lat))


def _domain(spec: GeoClawBuildSpec) -> tuple[float, float, float, float]:
    """The COMPUTATIONAL DOMAIN bounds (clawdata lower/upper).

    The explicit ``domain_bbox`` when the composer supplied one (the offshore-
    extended domain that spans the Okada source -> the AOI coast), else the AOI
    ``bbox`` (back-compat: a domain == AOI run). The base grid spans THIS extent;
    the AOI (``bbox``) drives the fine-AMR region + fgmax + gauge.
    """
    if spec.domain_bbox is not None:
        return spec.domain_bbox
    return spec.bbox


def _coastal_gauge(spec: GeoClawBuildSpec) -> tuple[float, float]:
    """The coastal time-series gauge point.

    Explicit ``coastal_gauge_lonlat`` when supplied, else a deterministic
    seaward-edge fallback: the mid-point of the AOI's SOUTHERN edge, inset a
    small fraction off the boundary so the gauge sits just inside the domain
    (the southern edge is the conventional seaward edge for these northern-
    hemisphere coastal demos; this is a deterministic fallback, not a claim about
    the true coastline).
    """
    if spec.coastal_gauge_lonlat is not None:
        return spec.coastal_gauge_lonlat
    min_lon, min_lat, max_lon, max_lat = spec.bbox
    gx = 0.5 * (min_lon + max_lon)
    gy = min_lat + 0.05 * (max_lat - min_lat)
    return (gx, gy)


#: Filename of the topotype-3 fgmax point mask (point_style=4) the entrypoint
#: generates from the staged DEM and the setrun references via ``fg.xy_fname``.
FGMAX_MASK_FILENAME: str = "fgmax_mask.tt3"


def _refinement_ratios(amr_levels: int) -> list[int]:
    """Per-level AMR refinement ratios - INCREASING toward the finest level.

    GeoClaw's ``refinement_ratios_{x,y,t}`` lists carry one entry per level
    transition (``amr_levels - 1`` entries). A flat all-2s list under-refines the
    inundation front; the canonical examples step the ratio up (e.g. ``[4, 3]``).
    We mirror that intent deterministically: the first transition is 2x, the
    middle transitions are 4x (coarse levels stay cheap while the inland front is
    resolved), and for a DEEP nest (>= 4 transitions, i.e. amr_levels >= 5) the
    FINAL transition steps back down to 2x. That gentle last step gives a finer
    coastal run-up resolution (a denser inundation sheet) WITHOUT the 4x
    cell-count + timestep cliff a 4x final transition would add -- e.g. a 5-level
    nest cumulates 2*4*4*2 = 64x (a town AOI run-up ~20 m) instead of 128x (~9 m,
    which blows the per-AOI cost budget). The <= 4-level schedules are UNCHANGED
    ([2], [2,4], [2,4,4]) so existing decks are byte-identical. ``amr_levels=1``
    -> ``[1]`` (GeoClaw wants a non-empty list of length >= mxnest-1, and 1 is a
    harmless self-ratio).
    """
    n = max(amr_levels - 1, 1)
    ratios: list[int] = []
    for i in range(n):
        if i == 0:
            ratios.append(2)
        elif i == n - 1 and n >= 4:
            ratios.append(2)  # gentle final step for a deep (>= 5-level) nest
        else:
            ratios.append(4)
    return ratios


#: Levels above the coarse base the INTERMEDIATE offshore PROPAGATION tier sits at.
#: 2 levels above the base grid == level 3 (the base is level 1). For the ~1.8 km
#: base grid that is ~230 m -- the ~200-500 m mid-resolution propagation grid the
#: canonical tsunami nesting (coarse deep ocean + intermediate shelf/propagation +
#: fine shore) uses so the shoaling wave is resolved as it travels.
_PROPAGATION_LEVELS_ABOVE_BASE = 2


def _propagation_level(amr_levels: int) -> int:
    """The INTERMEDIATE propagation/shelf refinement level for an OFFSHORE tsunami.

    The offshore-extended computational domain (the source -> coast corridor + the
    continental shelf) is FORCED to AT LEAST this level so the wave is resolved on
    a genuine mid-resolution grid as it propagates + shoals -- not numerically
    damped/dispersed on the coarse ~1.5 km base grid before it reaches the AOI
    coast. Set to ``_PROPAGATION_LEVELS_ABOVE_BASE`` levels above the base (level
    3), CAPPED at one-below-finest (``amr_levels - 1``) so it never collides with
    the finest AOI tier, and floored at the base (1) so a shallow nest is a no-op.

    Geometry-free (a pure function of ``amr_levels``) and MIRRORED EXACTLY by
    ``run_geoclaw._geoclaw_propagation_level`` so the agent cost/cell estimate
    matches the deck the worker authors (the agent <-> worker cross-check).
    """
    base_plus = 1 + _PROPAGATION_LEVELS_ABOVE_BASE
    return min(base_plus, max(int(amr_levels) - 1, 1))


# Synthetic (NON-SITE-SPECIFIC) Okada fault defaults - used ONLY when the
# user did not supply the matching geometry field. Mirrored from the v0.1
# render_maketopo_dtopo synthetic source so the banner / honesty story is
# consistent.
_SYNTHETIC_FAULT_STRIKE_DEG = 0.0
_SYNTHETIC_FAULT_DIP_DEG = 15.0
_SYNTHETIC_FAULT_RAKE_DEG = 90.0
_SYNTHETIC_FAULT_DEPTH_KM = 10.0


def fgmax_grid_geom(spec: GeoClawBuildSpec) -> dict[str, Any]:
    """The fgmax monitor grid geometry over the AOI (shared source of truth).

    Returns ``{x1, y1, x2, y2, nx, ny, dx, aoi_level}`` for the uniform fgmax
    sample grid the setrun emits for a tsunami/surge run: the AOI extent inset by
    a half AOI-ambient-level cell, at cell size ``dx`` (the base cell divided by
    the refinement product up to the AOI ambient level). ``render_setrun_py``
    emits the point_style=2 grid from EXACTLY this ``dx`` (locked by a unit test),
    and the entrypoint builds the point_style=4 ONSHORE mask on THIS grid so the
    masked fgmax points are a strict subset of the full-grid points (their common
    cells match cell-for-cell). Pure arithmetic -- no clawpack import.
    """
    min_lon, min_lat, max_lon, max_lat = spec.bbox
    dom_min_lon, dom_min_lat, dom_max_lon, dom_max_lat = _domain(spec)
    nx, ny = spec.base_num_cells
    amr_levels = int(spec.amr_levels)
    has_amr_windows = bool(spec.amr_regions)
    window_max_level = max((int(r[1]) for r in spec.amr_regions), default=0)
    max_levels = max(amr_levels, window_max_level)
    aoi_level = max(amr_levels - 1, 1) if has_amr_windows else amr_levels
    ratios = _refinement_ratios(max_levels)
    base_dx = (dom_max_lon - dom_min_lon) / float(nx)
    aoi_product = 1
    for r in ratios[: aoi_level - 1]:
        aoi_product *= int(r)
    dx = base_dx / float(aoi_product)
    x1 = min_lon + dx / 2.0
    x2 = max_lon - dx / 2.0
    y1 = min_lat + dx / 2.0
    y2 = max_lat - dx / 2.0
    gnx = int(round((x2 - x1) / dx)) + 1
    gny = int(round((y2 - y1) / dx)) + 1
    return {
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "nx": gnx,
        "ny": gny,
        "dx": dx,
        "aoi_level": aoi_level,
    }


def render_qinit_data(spec: GeoClawBuildSpec) -> str:
    """Render a ``qinit.xyz`` raised-column perturbation for the dam_break scenario.

    A TOPOTYPE-1 file: a circular raised water column of height
    ``dam_break_depth_m`` centred on the source, radius scaled to ~1/8 of the
    domain. GeoClaw's ``qinit`` module (``read_qinit`` in qinit_module.f90) reads
    ONLY a bare ``x y z`` topotype-1 file -- it has NO ESRI/topotype-3 header
    branch (it reads the first line as ``x_low y_hi`` then sweeps ``x y`` to infer
    the grid, then re-reads ``x y q``). The perturbation is added to the initial
    water surface and released at t=0 (qinit_type=4). It is referenced as the
    SINGLE-element ``qinitfiles.append(['qinit.xyz'])`` (QinitData.write accepts
    only a len-1 [fname] or the deprecated len-3 form; a len-2 list raises
    ValueError at rundata.write()).

    TOPOTYPE-1 layout (matches clawpack ``Topography.write(topo_type=1)``, format
    verified against a real write): one ``x y z`` line per point, ordered
    NORTH-FIRST (rows of decreasing latitude) and x-fastest (west->east) within
    each row, so the first line is ``xlower yupper z`` exactly as read_qinit
    expects. No header.

    Pure string render -- unit-testable with no clawpack import.
    """
    min_lon, min_lat, max_lon, max_lat = spec.bbox
    cx, cy = _centroid(spec)
    span = min(max_lon - min_lon, max_lat - min_lat)
    radius = max(span / 8.0, 1e-4)
    h = float(spec.dam_break_depth_m)
    # A small (16x16) perturbation grid covering the source disc. GeoClaw
    # bilinearly interpolates the qinit file onto the computational grid.
    n = 16
    x0 = cx - radius
    y0 = cy - radius
    cellsize = (2.0 * radius) / (n - 1)

    def _z(i: int, j: int) -> float:
        x = x0 + cellsize * i
        y = y0 + cellsize * j
        r = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        return h if r <= radius else 0.0

    # NORTH-FIRST rows (j = n-1 down to 0), x-fastest within each row; bare x y z.
    lines = []
    for j in range(n - 1, -1, -1):
        y = y0 + cellsize * j
        for i in range(n):
            x = x0 + cellsize * i
            lines.append(f"{x:.8f} {y:.8f} {_z(i, j):.6f}")
    return "\n".join(lines) + "\n"


def _render_finite_fault_construction(spec: GeoClawBuildSpec) -> str:
    """The maketopo.py FAULT-CONSTRUCTION block for a REAL finite-fault inversion.

    Reads the staged clawpack ``CSVFault`` subfault table (a published inversion's
    N patches: lon/lat/depth/strike/dip/rake/slip/length/width) with the NATIVE
    ``dtopotools.CSVFault`` reader (``coordinate_specification="centroid"`` -- the
    FSP convention). The resulting ``fault`` has MANY subfaults, so the Okada
    superposition is a real, concentrated, asymmetric slip field -- NOT one
    idealized rectangle. ``dx`` / ``buffer_size`` are tuned to the fault footprint
    (a small buffer -- the fault IS the footprint)."""
    csv = str(spec.finite_fault_file)
    return f'''# REAL finite-fault inversion: a MULTI-subfault Okada source (basis =
# measured-inversion). The staged CSV is a published USGS finite-fault product's
# subfault table; the deformation is the Okada superposition of every patch's slip.
print("*** MEASURED finite-fault inversion source: N-subfault Okada from a "
      "published USGS finite-fault product (real inverted slip distribution).")
fault = dtopotools.CSVFault()
fault.read({csv!r}, coordinate_specification="centroid")
_nsub = len(fault.subfaults)
_slips = [float(_s.slip) for _s in fault.subfaults]
print("finite-fault: %d subfaults, slip %.2f-%.2f m, Mw=%.2f"
      % (_nsub, min(_slips), max(_slips), float(fault.Mw())))

# dtopo grid: a modest buffer around the fault footprint (the fault IS the
# footprint for a finite-fault model, unlike a point-scaled single subfault).
_DTOPO_DX = 1/60.
_DTOPO_BUFFER = 0.5
'''


def _render_single_subfault_construction(spec: GeoClawBuildSpec) -> str:
    """The maketopo.py FAULT-CONSTRUCTION block for the DEGRADE rung: a single
    rectangular subfault scaled from ``source_magnitude`` (Wells & Coppersmith).

    This is the DERIVED fallback (no finite-fault product): one idealized rectangle,
    LOUDLY banner-flagged as non-site-specific whenever the geometry defaulted."""
    cx, cy = _centroid(spec)
    mw = float(spec.source_magnitude)

    strike = spec.fault_strike_deg if spec.fault_strike_deg is not None else _SYNTHETIC_FAULT_STRIKE_DEG
    dip = spec.fault_dip_deg if spec.fault_dip_deg is not None else _SYNTHETIC_FAULT_DIP_DEG
    rake = spec.fault_rake_deg if spec.fault_rake_deg is not None else _SYNTHETIC_FAULT_RAKE_DEG
    depth_km = spec.fault_depth_km if spec.fault_depth_km is not None else _SYNTHETIC_FAULT_DEPTH_KM
    depth_m = float(depth_km) * 1000.0

    defaulted = [
        name
        for name, supplied in (
            ("strike", spec.fault_strike_deg is not None),
            ("dip", spec.fault_dip_deg is not None),
            ("rake", spec.fault_rake_deg is not None),
            ("depth", spec.fault_depth_km is not None),
        )
        if not supplied
    ]
    # Honesty banner: emitted (and printed at runtime) whenever ANY geometry
    # field fell back to a synthetic, NON-SITE-SPECIFIC default.
    if defaulted:
        banner = (
            "NON-SITE-SPECIFIC synthetic (DERIVED, scaling-law) source: fault "
            "geometry field(s) " + ", ".join(defaulted)
            + " were NOT user-supplied and use generic synthetic defaults; this "
            "dtopo is a single idealized rectangle, NOT a site-specific inversion."
        )
    else:
        banner = ""

    return f'''# DERIVED single-subfault source (the degrade rung: no finite-fault product).
BANNER = {banner!r}
if BANNER:
    print("*** " + BANNER)

# Scale a single rectangular subfault from the moment magnitude (Mw).
# Wells & Coppersmith (1994) style log-scaling for length/width; mu = 4e10 Pa.
mw = {mw!r}
M0 = 10.0 ** (1.5 * mw + 9.05)            # seismic moment (N m)
length = 10.0 ** (-2.44 + 0.59 * mw) * 1000.0   # m
width = 10.0 ** (-1.01 + 0.32 * mw) * 1000.0    # m
mu = 4.0e10
slip = M0 / (mu * length * width)

subfault = dtopotools.SubFault()
subfault.strike = {float(strike)!r}
subfault.dip = {float(dip)!r}
subfault.rake = {float(rake)!r}
subfault.length = length
subfault.width = width
subfault.depth = {depth_m!r}
subfault.slip = slip
subfault.longitude = {cx!r}
subfault.latitude = {cy!r}
# coordinate_specification is REQUIRED by Okada (empty/wrong -> ValueError).
subfault.coordinate_specification = "centroid"

fault = dtopotools.Fault()
fault.subfaults = [subfault]
print("single-subfault: Mw=%s slip=%.2f m strike=%s dip=%s rake=%s depth_m=%s"
      % (mw, slip, {float(strike)!r}, {float(dip)!r}, {float(rake)!r}, {depth_m!r}))

# dtopo grid: a LARGE buffer around the point-scaled subfault so the deformation
# footprint of the single rectangle is captured.
_DTOPO_DX = 1/60.
_DTOPO_BUFFER = 2.0
'''


def render_maketopo_dtopo(spec: GeoClawBuildSpec) -> str:
    """Render a ``maketopo.py`` that synthesizes an Okada dtopo for the tsunami
    scenario (when no dtopo file was staged).

    Fallback ladder (ADR 0226 finite-fault upgrade -- the data-source fallback
    norm):
      * ``finite_fault_file`` staged -> a MULTI-subfault Okada from a published USGS
        finite-fault inversion (``_render_finite_fault_construction``): a real,
        concentrated, asymmetric slip field (basis=measured-inversion); else
      * the DEGRADE rung -- a single rectangular subfault scaled from
        ``source_magnitude`` (``_render_single_subfault_construction``), LOUDLY
        banner-flagged non-site-specific.

    Both paths build a ``fault`` object + set ``_DTOPO_DX`` / ``_DTOPO_BUFFER``, then
    share the SAME tail: ``create_dtopography`` -> ``dtopo.tt3`` -> the final-time
    vertical deformation ``deformation_dz.asc`` PRODUCT. ``coordinate_specification
    ="centroid"`` throughout (required by Okada). Emitted as a SEPARATE Python helper
    the entrypoint runs BEFORE the solve (it imports clawpack, so this authoring
    module must not). Pure string render here.
    """
    if spec.finite_fault_file:
        construction = _render_finite_fault_construction(spec)
    else:
        construction = _render_single_subfault_construction(spec)

    return f'''"""Auto-generated by the GeoClaw worker — synthesize an Okada dtopo."""
import numpy as _np
from clawpack.geoclaw import dtopotools

{construction}
# Build the dtopo grid with the canonical GeoClaw helper (auto-sizes a box around
# the fault with a buffer), not a hand-rolled linspace box.
x, y = fault.create_dtopo_xy(dx=_DTOPO_DX, buffer_size=_DTOPO_BUFFER)
fault.create_dtopography(x, y, times=[0.0, 1.0])
fault.dtopo.write("dtopo.tt3", dtopo_type=3)
print("wrote dtopo.tt3 (%d subfault(s))" % len(fault.subfaults))

# Emit the FINAL-time vertical seafloor deformation dZ (m) as an ESRI-ASCII grid
# (EPSG:4326, the regular create_dtopo_xy lon/lat box) so the agent-side / worker
# postprocess can rasterize the Okada uplift(+)/subsidence(-) field as a
# first-class PRODUCT -- the direct answer to "what seafloor deformation does this
# earthquake drive". Pure text write (no gdal in this helper); dZ is (ntime, ny, nx)
# indexed [lat, lon] with ascending x/y (verified against clawpack dtopotools).
_dz = _np.asarray(fault.dtopo.dZ)[-1]
_xs = _np.asarray(fault.dtopo.x, dtype=float)
_ys = _np.asarray(fault.dtopo.y, dtype=float)
_dx = float(_xs[1] - _xs[0]) if _xs.size > 1 else 1.0 / 60.0
_dy = float(_ys[1] - _ys[0]) if _ys.size > 1 else _dx
with open("deformation_dz.asc", "w") as _fh:
    _fh.write("ncols %d\\n" % _xs.size)
    _fh.write("nrows %d\\n" % _ys.size)
    _fh.write("xllcorner %.10f\\n" % (float(_xs.min()) - _dx / 2.0))
    _fh.write("yllcorner %.10f\\n" % (float(_ys.min()) - _dy / 2.0))
    _fh.write("cellsize %.12f\\n" % _dx)
    _fh.write("NODATA_value -9999.0\\n")
    # NORTH-first rows (highest latitude first), west->east within each row.
    for _j in range(_ys.size - 1, -1, -1):
        _fh.write(" ".join("%.6f" % float(_v) for _v in _dz[_j, :]) + "\\n")
print("wrote deformation_dz.asc dZ_min=%.4f m dZ_max=%.4f m ncols=%d nrows=%d"
      % (float(_dz.min()), float(_dz.max()), _xs.size, _ys.size))
'''


# Synthetic (NON-SITE-SPECIFIC) demo storm defaults for a surge run when the
# caller supplies NO storm_track. A generic Category-2-class Holland storm that
# approaches from offshore and makes landfall at the AOI centroid at t=0.
_DEMO_STORM_MAX_WIND_MS = 45.0          # ~Cat-2 (~87 kt)
_DEMO_STORM_MAX_WIND_RADIUS_M = 46000.0  # ~25 nm eyewall radius
_DEMO_STORM_CENTRAL_PRESSURE_PA = 96000.0  # 960 hPa
_DEMO_STORM_RADIUS_M = 400000.0          # ~400 km outer radius (a large storm)
#: Demo track approach vector: the storm centre travels this many degrees of
#: latitude from offshore (south) to the coast over the pre-landfall window.
_DEMO_STORM_APPROACH_DEG = 3.0


def _synthetic_demo_track(
    spec: GeoClawBuildSpec,
) -> list[tuple[float, float, float, float, float, float, float]]:
    """A NON-SITE-SPECIFIC demo storm track making landfall at the AOI centroid.

    A generic Category-2-class Holland storm that tracks due north from
    ``_DEMO_STORM_APPROACH_DEG`` degrees south of the AOI centroid (offshore) to
    the centroid at t=0 (landfall), then continues inland. The intensity peaks at
    landfall (deepest central pressure) and decays after. Used ONLY when the caller
    supplied no ``storm_track`` - illustrative, not a real historical storm. Times
    span ``[t0_s, t0_s + sim_duration_s]`` (t0_s < 0 opens the window pre-landfall)
    with a little pad on each end so the run window stays inside the track.
    """
    clon, clat = _centroid(spec)
    t0 = float(spec.t0_s)
    tfinal = t0 + float(spec.sim_duration_s)
    # Pad the track window so [t0, tfinal] is strictly interior (GeoClaw
    # interpolates within the file; a window at the very edge risks extrapolation).
    pad = max(0.25 * (tfinal - t0), 3600.0)
    tstart = t0 - pad
    tend = tfinal + pad
    n = 9
    track: list[tuple[float, float, float, float, float, float, float]] = []
    for i in range(n):
        frac = i / (n - 1)
        t = tstart + frac * (tend - tstart)
        # Latitude: south (offshore) -> centroid at landfall (t=0) -> inland.
        # Linear in time relative to the landfall instant.
        span = tend - tstart
        # normalized time in [-1, +1] with 0 at landfall (t=0).
        tau = t / max(abs(tstart), abs(tend), 1.0)
        lat = clat + _DEMO_STORM_APPROACH_DEG * tau
        lon = clon
        # Intensity: deepest (lowest pressure / highest wind) at landfall, decaying
        # with |tau|. A gentle triangular profile.
        decay = min(abs(tau), 1.0)
        pc = _DEMO_STORM_CENTRAL_PRESSURE_PA + 4000.0 * decay  # 960 -> up to 1000 hPa
        vmax = _DEMO_STORM_MAX_WIND_MS * (1.0 - 0.35 * decay)
        track.append(
            (
                float(t),
                float(lon),
                float(lat),
                float(vmax),
                float(_DEMO_STORM_MAX_WIND_RADIUS_M),
                float(pc),
                float(_DEMO_STORM_RADIUS_M),
            )
        )
    return track


def resolve_storm_track(
    spec: GeoClawBuildSpec,
) -> tuple[list[tuple[float, float, float, float, float, float, float]], bool]:
    """The surge storm track + whether it is the SYNTHETIC demo (not user-supplied).

    Returns ``(track, is_synthetic)``: the explicit ``spec.storm_track`` when the
    caller supplied one (``is_synthetic=False``), else a generated demo track
    (``is_synthetic=True``). The bool drives the honesty banner in the storm file
    so a demo run never reads as a real historical storm.
    """
    if spec.storm_track:
        return list(spec.storm_track), False
    return _synthetic_demo_track(spec), True


def render_storm_file(spec: GeoClawBuildSpec) -> str:
    """Render the GeoClaw-format storm file (``storm.storm``) for a surge run.

    The GeoClaw Fortran storm reader (``surge/storm.py`` write_geoclaw, read by
    ``model_storm_module``) consumes a fixed 3-line header + one row per forecast:

        <num_casts>
        <time_offset>            # 0.0 -> track times are seconds from landfall
        <blank>
        t  lon  lat  max_wind_speed  max_wind_radius  central_pressure  storm_radius
        ...                          # 7 columns, {:19,.8e}; np.loadtxt(skiprows=3)

    Units (GeoClaw): time s, lon/lat deg, max_wind_speed m/s, max_wind_radius m,
    central_pressure Pa, storm_radius m. ``time_offset = 0.0`` (a float) tells the
    reader the row times are ALREADY seconds relative to landfall (t=0). This is a
    PURE string render of exactly the bytes ``Storm.write(file_format='geoclaw')``
    emits - so the worker never imports the pandas-backed ``Storm`` class to author
    it (the deck-author stays clawpack-free + unit-testable).

    A leading ``#``-comment honesty banner is NOT written (the Fortran reader is
    strict: header is exactly 3 lines); the synthetic-vs-real provenance rides the
    deck manifest / driver descriptor instead.
    """
    track, _is_synth = resolve_storm_track(spec)
    if not track:
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID",
            "surge scenario resolved an EMPTY storm track (no user track and the "
            "demo synthesizer produced nothing) - refusing a wind-free surge deck",
        )
    fmt = ("{:19,.8e} " * 7)[:-1]
    lines = [str(len(track)), "0.0", ""]
    for (t, lon, lat, vmax, rmw, pc, rstorm) in track:
        lines.append(fmt.format(t, lon, lat, vmax, rmw, pc, rstorm))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Thacker (1981) paraboloid-basin V&V deck (scenario="thacker").
#
# An IDEALIZED, frictionless, closed-wall bowl in PLANAR Cartesian metres
# (coordinate_system=1) with a worker-GENERATED paraboloid topo + an analytic
# still-surface qinit -- NO fetched DEM. Mirrors the clawpack Cartesian bowl
# examples' conventions (num_aux=1, capa_index=0, topotype qinit_type=4) but uses
# the EXACT radially-symmetric Thacker solution (omega = sqrt(8 g h0)/a) as the
# t=0 initial condition so the numerical run can be graded against the closed form
# (period / central amplitude / shoreline excursion / mass conservation).
# See trid3nt_contracts.geoclaw_thacker for the shared analytic definition.
# --------------------------------------------------------------------------- #
#: Grid resolution (points per side) of the worker-written bowl topo + qinit
#: topotype-1 files. 201 matches the clawpack bowl examples (well below solve
#: cost; GeoClaw samples them onto the coarser computational grid).
_THACKER_TOPO_N = 201


def _thacker_k(amp_A: float) -> float:
    """Thacker amplitude ratio k = (1+A)/(1-A) (> 1)."""
    return (1.0 + amp_A) / (1.0 - amp_A)


def _render_topotype1_grid(
    x0: float, x1: float, y0: float, y1: float, n: int, zfunc
) -> str:
    """Render an ``n x n`` bare ``x y z`` topotype-1 ASCII over [x0,x1]x[y0,y1].

    NORTH-FIRST rows (decreasing y), x-fastest (west->east) within each row -- the
    layout clawpack ``Topography.write(topo_type=1)`` emits and GeoClaw's readers
    expect (first line = ``x0 y1 z``). Pure string render (no numpy / clawpack)."""
    dx = (x1 - x0) / (n - 1)
    dy = (y1 - y0) / (n - 1)
    lines: list[str] = []
    for j in range(n - 1, -1, -1):
        y = y0 + dy * j
        for i in range(n):
            x = x0 + dx * i
            lines.append(f"{x:.8f} {y:.8f} {zfunc(x, y):.8f}")
    return "\n".join(lines) + "\n"


def render_thacker_topo(spec: GeoClawBuildSpec) -> str:
    """Render the paraboloid bed ``B(x,y) = h0 (r^2/a^2 - 1)`` as topotype-1 ASCII."""
    x0, y0, x1, y1 = spec.bbox
    a = float(spec.bowl_a_m)  # type: ignore[arg-type]
    h0 = float(spec.bowl_h0_m)  # type: ignore[arg-type]

    def _b(x: float, y: float) -> float:
        return h0 * ((x * x + y * y) / (a * a) - 1.0)

    return _render_topotype1_grid(x0, x1, y0, y1, _THACKER_TOPO_N, _b)


def render_thacker_qinit(spec: GeoClawBuildSpec) -> str:
    """Render the analytic t=0 Thacker surface as a topotype-1 qinit perturbation.

    ``eta(r,0) = h0 [ sqrt(k) - 1 - (r^2/a^2)(k-1) ]``, ``k = (1+A)/(1-A)`` -- the
    still-surface extremum of the radially-symmetric oscillation (zero velocity at
    t=0). qinit_type=4 adds this to eta (sea_level=0), so the initial water surface
    IS this analytic surface and h = max(0, eta - B)."""
    x0, y0, x1, y1 = spec.bbox
    a = float(spec.bowl_a_m)  # type: ignore[arg-type]
    h0 = float(spec.bowl_h0_m)  # type: ignore[arg-type]
    amp_A = float(spec.bowl_eta_amp)  # type: ignore[arg-type]
    k = _thacker_k(amp_A)
    sqrt_k = k ** 0.5

    def _eta0(x: float, y: float) -> float:
        r2 = (x * x + y * y) / (a * a)
        return h0 * (sqrt_k - 1.0 - r2 * (k - 1.0))

    return _render_topotype1_grid(x0, x1, y0, y1, _THACKER_TOPO_N, _eta0)


def render_thacker_setrun_py(spec: GeoClawBuildSpec) -> str:
    """Render the Cartesian, frictionless, closed-wall Thacker ``setrun.py``.

    PLANAR ``coordinate_system=1`` (metres), ``capa_index=0``, ``num_aux=1``,
    ``friction_forcing=False`` (the exact solution is inviscid), ``bc_*='wall'``
    (a closed basin so total mass is conserved -- the V&V gate). Reads the
    worker-written ``topo.asc`` (topotype-1 paraboloid) + ``qinit.xyz``
    (topotype-1 analytic surface, qinit_type=4). Records a CENTER gauge (eta(0,t)
    -> period + central amplitude) plus a dense +x-axis gauge line
    (depth(r,t) -> shoreline excursion). PURE string render -- no clawpack."""
    x0, y0, x1, y1 = spec.bbox
    nx, ny = spec.base_num_cells
    amr_levels = int(spec.amr_levels)
    ratios = _refinement_ratios(amr_levels)
    amr_ratios = ", ".join(str(r) for r in ratios)
    tfinal = float(spec.sim_duration_s)
    num_output_times = int(spec.output_frames)
    a = float(spec.bowl_a_m)  # type: ignore[arg-type]

    # Gauges: id 1 at the basin centre (0,0); a dense +x-axis line (ids 100+) from
    # r=0 to 1.5a so the V&V finds the moving shoreline (largest wet radius).
    gauge_lines = "    rundata.gaugedata.gauges.append([1, 0.0, 0.0, 0., 1.e10])\n"
    gid = 100
    steps = 30  # 0 .. 1.5a in 0.05a steps
    for s in range(steps + 1):
        xr = (1.5 * a) * (s / steps)
        gauge_lines += (
            f"    rundata.gaugedata.gauges.append([{gid}, {xr!r}, 0.0, 0., 1.e10])\n"
        )
        gid += 1

    return f'''"""Auto-generated by the GeoClaw worker (setrun_builder) -- Thacker V&V.

Idealized paraboloid-basin (Thacker 1981) frictionless closed-wall bowl.
Domain (planar metres): {spec.bbox}
Do NOT hand-edit -- regenerate from the build_spec.
"""
from clawpack.clawutil import data


def setrun(claw_pkg="geoclaw"):
    assert claw_pkg.lower() == "geoclaw", "setrun expects claw_pkg='geoclaw'"
    num_dim = 2
    rundata = data.ClawRunData(claw_pkg, num_dim)
    rundata = setgeo(rundata)

    clawdata = rundata.clawdata
    clawdata.num_dim = num_dim

    # --- Planar Cartesian domain (metres) ---
    clawdata.lower[0] = {x0!r}
    clawdata.upper[0] = {x1!r}
    clawdata.lower[1] = {y0!r}
    clawdata.upper[1] = {y1!r}

    clawdata.num_cells[0] = {nx!r}
    clawdata.num_cells[1] = {ny!r}

    clawdata.num_eqn = 3
    clawdata.num_aux = 1  # Cartesian: topo aux only (no capacity/yleft metric)
    clawdata.capa_index = 0  # uniform Cartesian capacity (no lat/lon metric)

    # --- Time domain + evenly-spaced output frames ---
    clawdata.t0 = 0.0
    clawdata.output_style = 1
    clawdata.num_output_times = {num_output_times!r}
    clawdata.tfinal = {tfinal!r}
    clawdata.output_t0 = True
    clawdata.output_format = "ascii"
    clawdata.output_q_components = "all"
    clawdata.output_aux_components = "none"

    # --- Numerics ---
    clawdata.dt_initial = 0.01
    clawdata.dt_variable = True
    clawdata.dt_max = 1.0e99
    clawdata.cfl_desired = 0.75
    clawdata.cfl_max = 1.0
    clawdata.steps_max = 100000
    clawdata.order = 2
    clawdata.dimensional_split = "unsplit"
    clawdata.transverse_waves = 2
    clawdata.num_waves = 3
    clawdata.limiter = ["mc", "mc", "mc"]
    clawdata.use_fwaves = True
    clawdata.source_split = "godunov"

    # --- Boundary conditions: CLOSED WALLS (a closed basin -> mass conserved) ---
    clawdata.num_ghost = 2
    clawdata.bc_lower[0] = "wall"
    clawdata.bc_upper[0] = "wall"
    clawdata.bc_lower[1] = "wall"
    clawdata.bc_upper[1] = "wall"

    # --- AMR ---
    amrdata = rundata.amrdata
    amrdata.amr_levels_max = {amr_levels!r}
    amrdata.refinement_ratios_x = [{amr_ratios}]
    amrdata.refinement_ratios_y = [{amr_ratios}]
    amrdata.refinement_ratios_t = [{amr_ratios}]
    amrdata.aux_type = ["center"]
    amrdata.flag_richardson = False
    amrdata.flag2refine = True
    amrdata.regrid_interval = 3
    amrdata.regrid_buffer_width = 2
    amrdata.verbosity_regrid = 0

    # --- Gauges: centre (period + amplitude) + a +x-axis line (shoreline) ---
{gauge_lines}
    # --- qinit: the analytic t=0 Thacker surface (perturbation to eta) ---
    qinit_data = rundata.qinit_data
    qinit_data.qinit_type = 4  # perturbation to eta (water surface)
    qinit_data.qinitfiles = []
    qinit_data.qinitfiles.append(["qinit.xyz"])
    return rundata


def setgeo(rundata):
    try:
        geo_data = rundata.geo_data
    except AttributeError:
        raise AttributeError("Missing geo_data; rundata must be a GeoClaw run.")

    geo_data.gravity = {_THACKER_GRAVITY!r}
    geo_data.coordinate_system = 1  # 1 = Cartesian (metres), planar bowl
    geo_data.earth_radius = 6367500.0
    geo_data.dry_tolerance = 1.0e-3
    geo_data.friction_forcing = False  # inviscid -- the exact solution is frictionless
    geo_data.sea_level = 0.0

    refine_data = rundata.refinement_data
    refine_data.wave_tolerance = 1.0e-3
    refine_data.variable_dt_refinement_ratios = True

    topo_data = rundata.topo_data
    topo_data.topofiles = []
    # topotype 1 = bare x y z; the paraboloid bed the worker wrote as topo.asc.
    topo_data.topofiles.append([1, "topo.asc"])

    return rundata


if __name__ == "__main__":
    rundata = setrun()
    rundata.write()
'''


def render_setrun_py(spec: GeoClawBuildSpec) -> str:
    """Render the canonical GeoClaw ``setrun.py`` for the build_spec.

    Emits a ``setrun(claw_pkg='geoclaw')`` function returning a
    ``ClawRunData`` with the load-bearing clawdata / geo_data / topo_data /
    amrdata / (qinit|dtopo) blocks wired from ``spec``. The output_times list is
    ``output_frames`` evenly-spaced dumps across ``[0, sim_duration_s]`` so the
    postprocess gets exactly that many fort.q frames for the animation group.

    PURE string render — unit-testable with NO clawpack import. The clawpack
    import lives INSIDE the generated module (executed only when the entrypoint
    runs it), never in this authoring module.
    """
    # Thacker is a wholly-different (planar, frictionless, closed-wall) deck; it
    # has its own renderer rather than threading Cartesian branches through every
    # geographic block below (which stays byte-identical for the other scenarios).
    if spec.scenario == "thacker":
        return render_thacker_setrun_py(spec)

    # The AOI (fine-AMR region + fgmax monitor + gauge + rasterize extent).
    min_lon, min_lat, max_lon, max_lat = spec.bbox
    # The COMPUTATIONAL DOMAIN (clawdata lower/upper + base grid). Extends offshore
    # to span the Okada source for a tsunami; == AOI otherwise.
    dom_min_lon, dom_min_lat, dom_max_lon, dom_max_lat = _domain(spec)
    nx, ny = spec.base_num_cells
    amr_levels = int(spec.amr_levels)
    # Explicit AMR windows make the AOI a REGION-BASED refinement surface: the AOI
    # is pinned to an AMBIENT level ONE BELOW the finest, and each window pins a
    # finer level over its box -- so an in-AOI window is demonstrably finer than its
    # surroundings (a window pinned at the AOI-wide finest would otherwise be
    # subsumed by the default AOI region). ``aoi_level`` is the AOI ambient; the
    # finest available level (``max_levels``) is raised to cover any window that asks
    # for a level beyond ``amr_levels`` (a window is a bounded sub-box, so its
    # extra-fine cells scale with the window area, not the whole AOI). With NO
    # windows both collapse to ``amr_levels`` -- every non-window deck is unchanged.
    has_amr_windows = bool(spec.amr_regions)
    window_max_level = max((int(r[1]) for r in spec.amr_regions), default=0)
    max_levels = max(amr_levels, window_max_level)
    aoi_level = max(amr_levels - 1, 1) if has_amr_windows else amr_levels
    ratios = _refinement_ratios(max_levels)
    amr_ratios = ", ".join(str(r) for r in ratios)

    # Evenly-spaced output frames including the final time (exclude t=0 dump:
    # GeoClaw always writes frame 0 at t=0, so we request output_frames AFTER it
    # via output_style=1 with num_output_times = output_frames and tfinal set).
    num_output_times = int(spec.output_frames)
    # Run window [t0, tfinal]. Surge opens BEFORE landfall (t0_s < 0) so the storm
    # spins up; dam_break/tsunami keep t0_s == 0 so tfinal == sim_duration_s and the
    # emitted deck is byte-identical. ``t0_zero_repr`` keeps the legacy ``0.``
    # literal in the shared region/gauge blocks when t0 == 0 (byte-identical),
    # switching to the real (negative) value only for a surge.
    t0_val = float(spec.t0_s)
    tfinal = t0_val + float(spec.sim_duration_s)
    t0_zero_repr = "0." if t0_val == 0.0 else repr(t0_val)
    is_surge = spec.scenario == "surge"

    # fgmax sample spacing: the base cell size divided by the product of the
    # refinement ratios up to the AOI AMBIENT level. The BASE grid spans the
    # COMPUTATIONAL DOMAIN, so base_dx is measured across the domain (NOT the AOI)
    # -- otherwise the fgmax sample points would be mis-aligned with the FV cell
    # centers whenever the domain extends offshore beyond the AOI. fgmax samples at
    # ``aoi_level`` (not the absolute finest) so the whole AOI depth field is
    # captured regardless of the window/ambient split; a window's finer cells are
    # picked up by interpolation. With no windows ``aoi_level == max_levels`` so the
    # spacing is the finest-level cell size (the unchanged deck).
    base_dx = (dom_max_lon - dom_min_lon) / float(nx)
    aoi_product = 1
    for r in ratios[: aoi_level - 1]:
        aoi_product *= int(r)
    dx_fgmax = base_dx / float(aoi_product)

    # Scenario-specific source blocks.
    qinit_block = ""
    dtopo_block = ""
    if spec.scenario == "dam_break":
        qinit_block = (
            "    qinit_data = rundata.qinit_data\n"
            "    qinit_data.qinit_type = 4  # perturbation to eta (water surface)\n"
            "    qinit_data.qinitfiles = []\n"
            "    qinit_data.qinitfiles.append(['qinit.xyz'])\n"
        )
    elif spec.scenario == "tsunami":
        dtopo_file = spec.dtopo_file or "dtopo.tt3"
        dtopo_block = (
            "    dtopo_data = rundata.dtopo_data\n"
            "    dtopo_data.dtopofiles = []\n"
            f"    dtopo_data.dtopofiles.append([3, {dtopo_file!r}])\n"
            "    dtopo_data.dt_max_dtopo = 1.0\n"
        )
    # surge: parametric Holland-1980 wind + pressure forcing driven by the storm
    # track file (storm.storm, authored by render_storm_file). The storm-field aux
    # layout (num_aux = 3 shallow + 1 friction + 3 storm) + the surge geo constants
    # (rho / rho_air / ambient_pressure / coriolis) are wired in the num_aux / setgeo
    # blocks below; the surge_data block here turns the forcing on + selects the
    # wind-stress drag law.
    surge_block = ""
    surge_import = ""
    surge_geo_block = ""
    if is_surge:
        surge_import = "import os\nimport numpy as np\n"
        drag_code = _DRAG_LAW_CODES[spec.wind_drag_law]
        surge_block = (
            "    # --- Storm surge: parametric Holland 1980 wind + pressure ---\n"
            "    surge_data = rundata.surge_data\n"
            "    surge_data.wind_forcing = True\n"
            "    surge_data.pressure_forcing = True\n"
            f"    surge_data.drag_law = {drag_code!r}  # 0=none 1=Garratt 2=Powell "
            f"({spec.wind_drag_law})\n"
            "    surge_data.display_landfall_time = True\n"
            "    surge_data.wind_refine = [20.0, 40.0, 60.0]\n"
            "    surge_data.R_refine = [60.0e3, 40.0e3, 20.0e3]\n"
            "    surge_data.storm_specification_type = 'holland80'\n"
            "    surge_data.storm_file = os.path.join(os.getcwd(), 'storm.storm')\n"
        )
        # Surge geo physics constants + Coriolis (a rotating storm) + the
        # variable-friction field the storm aux layout reserves (aux index 4).
        surge_geo_block = (
            "    geo_data.rho = 1025.0  # seawater density, kg/m^3\n"
            "    geo_data.rho_air = 1.15  # air density, kg/m^3\n"
            "    geo_data.ambient_pressure = 101.3e3  # background MSL pressure, Pa\n"
            "    geo_data.coriolis_forcing = True  # a rotating (cyclonic) storm\n"
        )

    # Aux array layout. A surge run adds the storm fields to the shallow-water aux:
    # 3 shallow (h/capacity/yleft) + 1 friction + 3 storm (wind_x, wind_y, pressure)
    # = 7 (the canonical GeoClaw storm-surge aux, matching surge_data's default
    # wind_index=4/pressure_index=6 -> Fortran 5/6/7). dam_break/tsunami keep the
    # 3-aux shallow layout (byte-identical). aux_type is emitted with the SAME
    # double-quoted literal formatting the original deck used.
    aux_type_list = (
        ["center", "capacity", "yleft", "center", "center", "center", "center"]
        if is_surge
        else ["center", "capacity", "yleft"]
    )
    num_aux_val = len(aux_type_list)
    aux_type_str = "[" + ", ".join(f'"{t}"' for t in aux_type_list) + "]"

    # --- Boussinesq (SGN) dispersive solver block --------------------------------
    # bouss_equations > 0 lifts num_eqn 3 -> 5 and emits a BoussData block: the
    # implicit SGN/MS dispersive correction, applied only where water is deeper than
    # bouss_min_depth (shallow/run-up cells stay on SWE) and only on AMR levels in
    # [bouss_min_level, bouss_max_level]. bouss_solver=3 selects PETSc (the image's
    # MPI sparse solve). SWE (the default) emits NO block -> byte-identical deck.
    is_bouss = int(spec.bouss_equations) > 0
    num_eqn_val = 5 if is_bouss else 3
    bouss_block = ""
    if is_bouss:
        bouss_block = (
            "    # --- Boussinesq (SGN/MS) dispersive solver (num_eqn=5, PETSc) ---\n"
            "    from clawpack.geoclaw.data import BoussData\n"
            "    rundata.add_data(BoussData(), 'bouss_data')\n"
            f"    rundata.bouss_data.bouss_equations = {int(spec.bouss_equations)!r}"
            "    # 0=SWE, 1=Madsen-Sorensen, 2=SGN\n"
            f"    rundata.bouss_data.bouss_min_depth = {float(spec.bouss_min_depth)!r}"
            "    # switch to SWE in shallower water\n"
            f"    rundata.bouss_data.bouss_min_level = {int(spec.bouss_min_level)!r}\n"
            f"    rundata.bouss_data.bouss_max_level = {int(spec.bouss_max_level)!r}\n"
            "    rundata.bouss_data.bouss_solver = 3       # 1=GMRES, 2=Pardiso, 3=PETSc\n"
            "    rundata.bouss_data.bouss_tstart = 0.\n\n"
        )

    # --- GAP1 fgmax: monitor max depth + speed + arrival time over the AOI ---
    # Emitted for tsunami and surge (the inundation scenarios) - the fgmax output
    # backs the max-inundation depth layer + the arrival_time_s narration. NOT
    # emitted for dam_break (no coastal arrival concept there).
    fgmax_block = ""
    fgmax_import = ""
    if spec.scenario in ("tsunami", "surge"):
        fgmax_import = "from clawpack.geoclaw import fgmax_tools\n"
        # A sane fgmax check cadence: ~50 checks across the run, floored at 1 s.
        dt_check = max(tfinal / 50.0, 1.0)
        arrival_tol = float(spec.fgmax_arrival_tol_m)
        if spec.fgmax_mask == "onshore":
            # point_style=4: the fgmax points are the ONSHORE cells of a topotype-3
            # mask (fgmax_mask.tt3) the entrypoint generates from the DEM. The mask
            # grid (built on fgmax_grid_geom) supplies x/y/dx, so here we only
            # reference it + the shared monitor settings -- a strict onshore subset
            # of the full point_style=2 grid (fewer points -> smaller fgmax output).
            fgmax_block = (
                "    # --- fgmax: max depth/speed/arrival on the ONSHORE DEM cells ---\n"
                "    rundata.fgmax_data.num_fgmax_val = 2  # save max depth + speed\n"
                "    fgmax_grids = rundata.fgmax_data.fgmax_grids\n"
                "    fg = fgmax_tools.FGmaxGrid()\n"
                "    fg.point_style = 4  # points = onshore cells of a topotype-3 mask\n"
                f"    fg.xy_fname = {FGMAX_MASK_FILENAME!r}  # 0/1 mask (1 = monitored cell)\n"
                f"    fg.tstart_max = {t0_zero_repr}  # monitor max values from t0\n"
                "    fg.tend_max = 1.e10\n"
                f"    fg.dt_check = {dt_check!r}\n"
                f"    fg.min_level_check = {aoi_level!r}  # monitor at the AOI ambient level\n"
                f"    fg.arrival_tol = {arrival_tol!r}  # wet-cell threshold for arrival\n"
                "    fg.interp_method = 0  # 0 ==> pw const in cells, recommended\n"
                "    fgmax_grids.append(fg)\n"
            )
        else:
            fgmax_block = (
                "    # --- fgmax: max depth/speed/arrival monitored over the AOI ---\n"
                "    rundata.fgmax_data.num_fgmax_val = 2  # save max depth + speed\n"
                "    fgmax_grids = rundata.fgmax_data.fgmax_grids\n"
                f"    dx_fine = {dx_fgmax!r}  # AOI ambient-level cell size (fgmax spacing)\n"
                "    fg = fgmax_tools.FGmaxGrid()\n"
                "    fg.point_style = 2  # uniform rectangular x-y grid\n"
                "    # align sample pts with AOI ambient-level FV cell centers (half-cell inset):\n"
                f"    fg.x1 = {min_lon!r} + dx_fine / 2.0\n"
                f"    fg.x2 = {max_lon!r} - dx_fine / 2.0\n"
                f"    fg.y1 = {min_lat!r} + dx_fine / 2.0\n"
                f"    fg.y2 = {max_lat!r} - dx_fine / 2.0\n"
                "    fg.dx = dx_fine\n"
                f"    fg.tstart_max = {t0_zero_repr}  # monitor max values from t0\n"
                "    fg.tend_max = 1.e10\n"
                f"    fg.dt_check = {dt_check!r}\n"
                f"    fg.min_level_check = {aoi_level!r}  # monitor at the AOI ambient level\n"
                f"    fg.arrival_tol = {arrival_tol!r}  # wet-cell threshold for arrival\n"
                "    fg.interp_method = 0  # 0 ==> pw const in cells, recommended\n"
                "    fgmax_grids.append(fg)\n"
            )

    # --- fgout: SMOOTH fixed-grid animation frames (decoupled from AMR cadence) --
    # fgout = a fixed uniform grid interpolated at REGULAR time intervals, so the
    # animation frames are SMOOTH (single resolution, evenly-spaced times) rather
    # than the coarse/variable fort.q AMR-patch cadence the scrubber uses by
    # default. Emitted for tsunami / surge ONLY when ``fgout_frames`` > 0 -- a
    # 0-frame spec emits NO fgout block (byte-identical to a pre-fgout deck). The
    # grid spans the AOI at the AOI-ambient cell size (SAME dx as the fgmax grid,
    # so the two monitors share a resolution) and dumps ``fgout_frames`` frames
    # evenly across [t0, tfinal]. Output is ASCII (topotype-analogue fort.q layout)
    # so the postprocess reads each frame with the same uniform-grid parser it uses
    # for fort.q -- no AMR flatten, no clawpack import agent-side.
    fgout_block = ""
    fgout_import = ""
    if spec.scenario in ("tsunami", "surge") and int(spec.fgout_frames) > 0:
        fgout_import = "from clawpack.geoclaw import fgout_tools\n"
        fgout_nout = int(spec.fgout_frames)
        gnx = int(round((max_lon - min_lon) / dx_fgmax))
        gny = int(round((max_lat - min_lat) / dx_fgmax))
        gnx = max(gnx, 2)
        gny = max(gny, 2)
        fgout_block = (
            "    # --- fgout: uniform-grid SMOOTH animation frames over the AOI ---\n"
            "    fgout_grids = rundata.fgout_data.fgout_grids\n"
            "    fgout = fgout_tools.FGoutGrid()\n"
            "    fgout.fgno = 1\n"
            "    fgout.point_style = 2  # uniform rectangular x-y grid\n"
            "    fgout.output_format = 'ascii'  # fort.q-layout frames (uniform-grid parse)\n"
            f"    fgout.nx = {gnx!r}\n"
            f"    fgout.ny = {gny!r}\n"
            f"    fgout.x1 = {min_lon!r}\n"
            f"    fgout.x2 = {max_lon!r}\n"
            f"    fgout.y1 = {min_lat!r}\n"
            f"    fgout.y2 = {max_lat!r}\n"
            f"    fgout.tstart = {t0_val!r}\n"
            f"    fgout.tend = {tfinal!r}\n"
            f"    fgout.nout = {fgout_nout!r}  # evenly-spaced frames (smooth cadence)\n"
            "    fgout_grids.append(fgout)\n"
        )

    # --- GAP3 regions: the canonical multi-scale tsunami setup ---------------
    # COARSE deep ocean + INTERMEDIATE shelf/propagation + FINE AOI. GeoClaw
    # combines overlapping regions by taking the MAX of the covering regions'
    # min/max levels (amrclaw flagregions2.f90), so:
    #   (1) a whole-DOMAIN region FORCES the offshore-extended propagation domain
    #       (the source -> coast corridor + the continental shelf) to an
    #       INTERMEDIATE mid-resolution level (``_propagation_level``, ~230 m), and
    #       caps it at one-below-finest -- the shoaling wave is well-resolved as it
    #       TRAVELS (not damped on the coarse base grid), while the costly finest
    #       mesh is still NOT created across the whole ocean;
    #   (2) an AOI region FORCES the finest level over the coastal AOI for the whole
    #       run -- where the run-up is computed + monitored.
    # OFFSHORE-ONLY: the intermediate propagation tier applies only to a tsunami
    # whose domain extends offshore (domain_bbox present). dam_break/surge (domain
    # == AOI, no propagation corridor) keep min level 1, so those decks are
    # byte-identical. With amr_levels == 1 everything collapses to a uniform grid.
    offshore_max = max(amr_levels - 1, 1)
    _offshore = spec.scenario == "tsunami" and spec.domain_bbox is not None
    prop_min = _propagation_level(amr_levels) if _offshore else 1
    regions_block = (
        "    # --- Regions: intermediate propagation tier over the offshore domain\n"
        "    #     (force the source->coast corridor + shelf to mid-resolution so\n"
        "    #     the wave is resolved as it shoals; cap at one-below-finest) ---\n"
        f"    rundata.regiondata.regions.append([{prop_min!r}, {offshore_max!r}, "
        f"{t0_zero_repr}, {tfinal!r}, {dom_min_lon!r}, {dom_max_lon!r}, {dom_min_lat!r}, {dom_max_lat!r}])\n"
        "    # --- Regions: pin the AOI ambient AMR level for the run (one below the\n"
        "    #     finest when explicit windows are present, so a window refines above\n"
        "    #     it; the requested finest when there are no windows) ---\n"
        f"    rundata.regiondata.regions.append([{aoi_level!r}, {aoi_level!r}, "
        f"{t0_zero_repr}, {tfinal!r}, {min_lon!r}, {max_lon!r}, {min_lat!r}, {max_lat!r}])\n"
    )
    # Explicit user-supplied AMR windows (region-based flagging): each forces a
    # lat/lon/time box to [min_level, max_level]. Appended AFTER the default tiers
    # (GeoClaw combines overlapping regions by MAX of covering min/max levels).
    for reg in spec.amr_regions:
        ml, xl, t0r, t1r, x0r, x1r, y0r, y1r = reg
        regions_block += (
            "    # --- Region: explicit user AMR window ---\n"
            f"    rundata.regiondata.regions.append([{int(ml)!r}, {int(xl)!r}, "
            f"{float(t0r)!r}, {float(t1r)!r}, {float(x0r)!r}, {float(x1r)!r}, "
            f"{float(y0r)!r}, {float(y1r)!r}])\n"
        )

    # --- GAP4 gauges: one coastal gauge (explicit or seaward-edge fallback) ---
    gx, gy = _coastal_gauge(spec)
    gauges_block = (
        "    # --- Gauges: one coastal time-series gauge ---\n"
        f"    rundata.gaugedata.gauges.append([1, {gx!r}, {gy!r}, {t0_zero_repr}, 1.e10])\n"
    )
    # Lagrangian particle gauges: each seeded point is added as a gauge advected by
    # the flow (gtype='lagrangian'), its recorded columns q[2,3] replaced by the
    # particle position (x(t), y(t)). The stationary coastal gauge (id 1) stays
    # 'stationary' via the per-gauge gtype dict (unlisted -> default). Empty ->
    # byte-identical (no gtype dict, no extra gauges).
    if spec.lagrangian_particles:
        gauges_block += "    # --- Lagrangian particle gauges (advected by the flow) ---\n"
        _gtype_entries: list[str] = []
        for _i, (plon, plat) in enumerate(spec.lagrangian_particles):
            _gid = 100 + _i
            gauges_block += (
                f"    rundata.gaugedata.gauges.append([{_gid}, "
                f"{float(plon)!r}, {float(plat)!r}, {t0_zero_repr}, 1.e10])\n"
            )
            _gtype_entries.append(f"{_gid}: 'lagrangian'")
        gauges_block += (
            f"    rundata.gaugedata.gtype = {{{', '.join(_gtype_entries)}}}\n"
        )

    # --- GAP7 nested DEM: primary topo + any extra (coarse->fine) topo files ---
    topo_lines = [f"    topo_data.topofiles.append([3, {spec.topo_file!r}])\n"]
    for f in spec.extra_topo_files:
        topo_lines.append(f"    topo_data.topofiles.append([3, {f!r}])\n")
    topo_block = "".join(topo_lines)

    # --- Friction: single global n, or a spatially-varying (banded) Manning n ---
    # GeoClaw selects the friction coefficient per cell from ``manning_coefficient``
    # (a list) split by ``manning_break`` (topography breakpoints): band k applies
    # where manning_break[k-1] <= B < manning_break[k]. A single-element list with
    # an empty break list is the uniform case (byte-identical to the scalar path).
    if spec.manning_coefficients is not None:
        mc = ", ".join(repr(float(n)) for n in spec.manning_coefficients)
        mb = ", ".join(repr(float(b)) for b in spec.manning_break)
        friction_block = (
            f"    geo_data.manning_coefficient = [{mc}]\n"
            f"    geo_data.manning_break = [{mb}]\n"
        )
    else:
        friction_block = (
            f"    geo_data.manning_coefficient = {float(spec.manning_n)!r}\n"
        )

    return f'''"""Auto-generated by the GeoClaw worker (setrun_builder).

Scenario: {spec.scenario}
Domain (EPSG:4326): {spec.bbox}
Do NOT hand-edit — regenerate from the build_spec.
"""
from clawpack.clawutil import data
{fgmax_import}{fgout_import}{surge_import}

def setrun(claw_pkg="geoclaw"):
    assert claw_pkg.lower() == "geoclaw", "setrun expects claw_pkg='geoclaw'"
    num_dim = 2
    rundata = data.ClawRunData(claw_pkg, num_dim)
    rundata = setgeo(rundata)

    clawdata = rundata.clawdata
    clawdata.num_dim = num_dim

    # --- Domain (lon/lat) --- the COMPUTATIONAL DOMAIN (spans the offshore
    # source -> the AOI coast for a tsunami); the AOI is refined via the region
    # block + monitored by fgmax/gauge below.
    clawdata.lower[0] = {dom_min_lon!r}
    clawdata.upper[0] = {dom_max_lon!r}
    clawdata.lower[1] = {dom_min_lat!r}
    clawdata.upper[1] = {dom_max_lat!r}

    # --- Base computational grid ---
    clawdata.num_cells[0] = {nx!r}
    clawdata.num_cells[1] = {ny!r}

    clawdata.num_eqn = {num_eqn_val!r}
    clawdata.num_aux = {num_aux_val!r}
    clawdata.capa_index = 2

    # --- Time domain + evenly-spaced output frames (the fort.q animation) ---
    clawdata.t0 = {t0_val!r}
    clawdata.output_style = 1
    clawdata.num_output_times = {num_output_times!r}
    clawdata.tfinal = {tfinal!r}
    clawdata.output_t0 = True
    clawdata.output_format = "ascii"
    clawdata.output_q_components = "all"
    clawdata.output_aux_components = "none"

    # --- Numerics ---
    clawdata.dt_initial = 1.0
    clawdata.dt_variable = True
    clawdata.dt_max = 1.0e99
    clawdata.cfl_desired = 0.75
    clawdata.cfl_max = 1.0
    clawdata.steps_max = 100000
    clawdata.order = 2
    clawdata.dimensional_split = "unsplit"
    clawdata.transverse_waves = 2
    clawdata.num_waves = 3
    clawdata.limiter = ["mc", "mc", "mc"]
    clawdata.use_fwaves = True
    clawdata.source_split = "godunov"

    # --- Boundary conditions (extrap = open / non-reflecting) ---
    clawdata.num_ghost = 2
    clawdata.bc_lower[0] = "extrap"
    clawdata.bc_upper[0] = "extrap"
    clawdata.bc_lower[1] = "extrap"
    clawdata.bc_upper[1] = "extrap"

    # --- AMR (adaptive mesh refinement) ---
    amrdata = rundata.amrdata
    amrdata.amr_levels_max = {max_levels!r}
    amrdata.refinement_ratios_x = [{amr_ratios}]
    amrdata.refinement_ratios_y = [{amr_ratios}]
    amrdata.refinement_ratios_t = [{amr_ratios}]
    amrdata.aux_type = {aux_type_str}
    amrdata.flag_richardson = False
    amrdata.flag2refine = True
    amrdata.regrid_interval = 3
    amrdata.regrid_buffer_width = 2
    amrdata.verbosity_regrid = 0

{regions_block}{gauges_block}{fgmax_block}{fgout_block}{qinit_block}{dtopo_block}{surge_block}{bouss_block}    return rundata


def setgeo(rundata):
    try:
        geo_data = rundata.geo_data
    except AttributeError:
        raise AttributeError("Missing geo_data; rundata must be a GeoClaw run.")

    geo_data.gravity = 9.81
    geo_data.coordinate_system = 2  # 2 = lat/lon (spherical)
    geo_data.earth_radius = 6367500.0
{surge_geo_block}
    geo_data.dry_tolerance = 1.0e-3
    geo_data.friction_forcing = True
{friction_block}    geo_data.friction_depth = 1.0e6

    geo_data.sea_level = {float(spec.sea_level_m)!r}

    refine_data = rundata.refinement_data
    refine_data.wave_tolerance = 0.05
    refine_data.speed_tolerance = [0.25, 0.5, 1.0, 2.0]
    refine_data.variable_dt_refinement_ratios = True

    topo_data = rundata.topo_data
    topo_data.topofiles = []
    # topotype 3 = ESRI/GeoClaw header ASCII; the entrypoint converts the staged
    # DEM to this form as {spec.topo_file!r}. Any nested DEM(s) follow,
    # ordered coarse->fine (GeoClaw prefers finer topo where it overlaps).
{topo_block}
    return rundata


if __name__ == "__main__":
    rundata = setrun()
    rundata.write()
'''


def render_makefile(spec: GeoClawBuildSpec) -> str:
    """Render the per-application GeoClaw ``Makefile`` for the deck.

    This is THE file that provides the ``.output`` target ``make .output``
    invokes: GeoClaw's ``.output`` rule lives in
    ``$(CLAW)/clawutil/src/Makefile.common``, and that common Makefile only
    becomes usable once a per-application Makefile sets the GeoClaw build vars
    (CLAW_PKG, EXE, SETRUN_FILE, OUTDIR, the module/source lists) and includes
    it. Without this file in the run cwd, ``make .output`` fails instantly with
    "No rule to make target '.output'".

    Mirrors the canonical clawpack/geoclaw example Makefile
    (clawpack/geoclaw/examples/*/Makefile): set the load-bearing vars and list the
    GeoClaw Riemann solvers in SOURCES (rpn2_geoclaw / rpt2_geoclaw /
    geoclaw_riemann_utils -- these are NOT supplied by Makefile.geoclaw and MUST be
    listed explicitly, exactly as every example Makefile does, or xgeoclaw fails to
    link with "undefined reference to rpn2_/rpt2_"), then include
    ``Makefile.geoclaw`` followed by ``$(CLAWMAKE)`` =
    ``$(CLAW)/clawutil/src/Makefile.common``. The result compiles ``xgeoclaw`` and
    runs it headless into ``_output/``.

    PURE string render -- unit-testable with NO clawpack import. $(CLAW) is
    resolved at run time from the image env (set in the Dockerfile).
    """
    if int(spec.bouss_equations) > 0:
        return _render_bouss_makefile()
    # The GeoClaw 2d shallow modules come from Makefile.geoclaw's COMMON_MODULES,
    # but the rpn2/rpt2 Riemann solvers are per-application SOURCES that the
    # canonical example Makefiles list explicitly (Makefile.geoclaw does NOT add
    # them). Omitting them is the "undefined reference to rpn2_/rpt2_" link bug.
    return '''# Auto-generated by the GeoClaw worker (setrun_builder.render_makefile).
# Per-application GeoClaw Makefile -- defines the build vars then includes the
# Clawpack machinery that provides the `.output` (headless solve) target.
# Do NOT hand-edit -- regenerate from the build_spec.

# CLAW must be exported in the runtime env (the clawpack install root) so the
# includes below resolve. Fail loudly if it is not.
ifndef CLAW
  $(error CLAW is not set -- export CLAW=<clawpack install root> before make)
endif

CLAW_PKG = geoclaw
EXE = xgeoclaw
SETRUN_FILE = setrun.py
OUTDIR = _output
SETPLOT_FILE = setplot.py
PLOTDIR = _plots

# Compiler flags (gfortran, optimized headless build).
FFLAGS ?= -O2 -fopenmp
FC ?= gfortran

# Custom per-application Fortran modules -- none (the GeoClaw 2d shallow modules
# come from Makefile.geoclaw's COMMON_MODULES below).
MODULES = \\

# The GeoClaw Riemann solvers MUST be listed here (Makefile.geoclaw does not add
# them); without them xgeoclaw fails to link (undefined reference to rpn2_/rpt2_).
SOURCES = \\
  $(CLAW)/riemann/src/rpn2_geoclaw.f \\
  $(CLAW)/riemann/src/rpt2_geoclaw.f \\
  $(CLAW)/riemann/src/geoclaw_riemann_utils.f \\

EXCLUDE_MODULES = \\

EXCLUDE_SOURCES = \\

# The standard Clawpack Makefile that resolves $(CLAW)/.../Makefile.common.
CLAWMAKE = $(CLAW)/clawutil/src/Makefile.common

# Pull in the GeoClaw 2d shallow module/source lists (COMMON_MODULES /
# COMMON_SOURCES) ...
include $(CLAW)/geoclaw/src/2d/shallow/Makefile.geoclaw

# ... then the common rules, which define the `.output` target make runs.
include $(CLAWMAKE)
'''


def _render_bouss_makefile() -> str:
    """Render the per-application Makefile for the Boussinesq (num_eqn=5) variant.

    Mirrors the canonical clawpack/geoclaw/examples/bouss/*/Makefile: instead of
    Makefile.geoclaw it includes ``Makefile.bouss`` (which lists the FULL bouss
    source set -- amrclaw + shallow + bouss + the rpn2/rpt2 Riemann solvers -- so
    SOURCES stays EMPTY here; re-listing riemann would double-compile it into a
    duplicate-symbol link error). FC is the MPI Fortran wrapper (CLAW_MPIFC), and
    the PETSc include/link flags come from pkg-config against $(PETSC_DIR). The
    hard ``ifndef`` guards match Makefile.bouss's: the entrypoint sets PETSC_DIR /
    PETSC_OPTIONS / CLAW_MPIEXEC / CLAW_MPIFC (from the image env) for the make
    subprocess, so a missing one fails loudly rather than silently building SWE.
    """
    return '''# Auto-generated by the GeoClaw worker (setrun_builder._render_bouss_makefile).
# Boussinesq (SGN/Madsen-Sorensen) variant -- num_eqn=5, implicit PETSc+MPI solve.
# Do NOT hand-edit -- regenerate from the build_spec.

ifndef CLAW
  $(error CLAW is not set -- export CLAW=<clawpack install root> before make)
endif
ifndef PETSC_DIR
  $(error PETSC_DIR not set -- the Boussinesq solver requires PETSc >= 3.20)
endif
ifndef PETSC_OPTIONS
  $(error PETSC_OPTIONS must be declared as an environment variable)
endif
ifndef CLAW_MPIEXEC
  $(error CLAW_MPIEXEC must be declared as an environment variable)
endif
ifndef CLAW_MPIFC
  $(error CLAW_MPIFC must be declared as an environment variable)
endif

CLAW_PKG = geoclaw
EXE = xgeoclaw
SETRUN_FILE = setrun.py
OUTDIR = _output
SETPLOT_FILE = setplot.py
PLOTDIR = _plots

# MPI Fortran wrapper (over-rules any FC in the env), and MPI-rank launch count.
FC = ${CLAW_MPIFC}
BOUSS_MPI_PROCS ?= 2
RUNEXE = "${CLAW_MPIEXEC} -n ${BOUSS_MPI_PROCS}"

# PETSc compile/link flags (as in the canonical bouss example Makefile): the
# includes + -DHAVE_PETSC gate petsc_driver.f90, and pkg-config resolves the
# solver library set. PETSC_ARCH is empty for a conda/package-manager PETSc.
PETSC_INCLUDE = $(PETSC_DIR)/include $(PETSC_DIR)/$(PETSC_ARCH)/include
INCLUDE += $(PETSC_INCLUDE)
PETSC_LFLAGS = $(shell PKG_CONFIG_PATH=$(PETSC_DIR)/$(PETSC_ARCH)/lib/pkgconfig pkg-config --libs-only-L --libs-only-l PETSc)

FFLAGS ?= -O2 -fopenmp -std=legacy -ffree-line-length-none
FFLAGS += -DHAVE_PETSC
LFLAGS += $(PETSC_LFLAGS) -fopenmp

# Makefile.bouss provides the FULL source set (amrclaw + shallow + bouss + the
# rpn2/rpt2 Riemann solvers) so no custom MODULES/SOURCES are listed here.
MODULES = \\

SOURCES = \\

EXCLUDE_MODULES = \\

EXCLUDE_SOURCES = \\

BOUSSLIB = $(CLAW)/geoclaw/src/2d/bouss
AMRLIB = $(CLAW)/amrclaw/src/2d
GEOLIB = $(CLAW)/geoclaw/src/2d/shallow

CLAWMAKE = $(CLAW)/clawutil/src/Makefile.common

# The bouss source list (COMMON_MODULES / COMMON_SOURCES) ...
include $(BOUSSLIB)/Makefile.bouss

# ... then the common rules, which define the `.output` target make runs.
include $(CLAWMAKE)
'''


def build_geoclaw_deck(build_spec_raw: dict[str, Any], deck_dir: Any) -> DeckManifest:
    """Author the full GeoClaw deck (setrun.py + scenario source files) into
    ``deck_dir`` from a raw build_spec dict. Returns a ``DeckManifest`` of what
    was written.

    The single entrypoint-facing call: parse -> render -> write. clawpack is NOT
    imported (the rendered ``maketopo.py`` imports it, executed later by the
    entrypoint). Pure file I/O + string render -> unit-testable with no Fortran.
    """
    from pathlib import Path

    deck = Path(deck_dir)
    deck.mkdir(parents=True, exist_ok=True)
    spec = parse_build_spec(build_spec_raw)

    written: list[str] = []

    setrun_text = render_setrun_py(spec)
    (deck / "setrun.py").write_text(setrun_text, encoding="utf-8")
    written.append("setrun.py")

    # The per-application Makefile -- THIS is what supplies the `.output` target
    # `make .output` runs (via the included Makefile.common). Without it the
    # solve fails instantly with "No rule to make target '.output'".
    (deck / "Makefile").write_text(render_makefile(spec), encoding="utf-8")
    written.append("Makefile")

    driver = ""
    if spec.scenario == "thacker":
        # Worker-GENERATED paraboloid bed + analytic still-surface qinit (NO DEM).
        (deck / "topo.asc").write_text(render_thacker_topo(spec), encoding="utf-8")
        written.append("topo.asc")
        (deck / "qinit.xyz").write_text(render_thacker_qinit(spec), encoding="utf-8")
        written.append("qinit.xyz")
        driver = (
            f"thacker paraboloid-basin V&V (a={spec.bowl_a_m} m, h0={spec.bowl_h0_m} m, "
            f"A={spec.bowl_eta_amp}), frictionless closed-wall planar bowl"
        )
    elif spec.scenario == "dam_break":
        (deck / "qinit.xyz").write_text(render_qinit_data(spec), encoding="utf-8")
        written.append("qinit.xyz")
        driver = f"dam_break raised column {spec.dam_break_depth_m:.1f} m at {_centroid(spec)}"
    elif spec.scenario == "tsunami":
        if spec.dtopo_file is None:
            (deck / "maketopo.py").write_text(
                render_maketopo_dtopo(spec), encoding="utf-8"
            )
            written.append("maketopo.py")
            _dom = _domain(spec)
            _dom_note = (
                f" domain={tuple(round(v, 4) for v in _dom)}"
                if spec.domain_bbox is not None
                else " domain=AOI"
            )
            driver = (
                f"tsunami synthetic Okada source Mw{spec.source_magnitude:.1f} "
                f"at {_centroid(spec)}{_dom_note}"
            )
        else:
            driver = f"tsunami staged dtopo {spec.dtopo_file}"
    else:  # surge
        (deck / "storm.storm").write_text(render_storm_file(spec), encoding="utf-8")
        written.append("storm.storm")
        track, is_synth = resolve_storm_track(spec)
        _t0, _tN = track[0][0], track[-1][0]
        _pc_min = min(p[5] for p in track) / 100.0  # Pa -> hPa
        _origin = "synthetic demo storm" if is_synth else "user-supplied track"
        driver = (
            f"surge parametric-Holland ({_origin}, {len(track)} track pts, "
            f"{spec.wind_drag_law} drag, min central pressure {_pc_min:.0f} hPa, "
            f"landfall at {_centroid(spec)}, window t0={spec.t0_s:.0f}s)"
        )

    if int(spec.bouss_equations) > 0:
        _bnames = {1: "Madsen-Sorensen", 2: "SGN"}
        driver += (
            f"; Boussinesq {_bnames.get(int(spec.bouss_equations), '?')} dispersive "
            f"(num_eqn=5, bouss_min_depth={spec.bouss_min_depth:.0f} m, "
            f"levels {spec.bouss_min_level}-{spec.bouss_max_level}, PETSc)"
        )

    manifest = DeckManifest(
        scenario=spec.scenario,
        bbox=spec.bbox,
        base_num_cells=spec.base_num_cells,
        amr_levels=spec.amr_levels,
        output_frames=spec.output_frames,
        sim_duration_s=spec.sim_duration_s,
        files_written=written,
        driver_descriptor=driver,
        bouss_equations=int(spec.bouss_equations),
    )
    # Persist the manifest alongside the deck for provenance / debugging.
    (deck / "deck_manifest.json").write_text(
        json.dumps(
            {
                "scenario": manifest.scenario,
                "bbox": list(manifest.bbox),
                "base_num_cells": list(manifest.base_num_cells),
                "amr_levels": manifest.amr_levels,
                "output_frames": manifest.output_frames,
                "sim_duration_s": manifest.sim_duration_s,
                "files_written": manifest.files_written,
                "driver_descriptor": manifest.driver_descriptor,
                "bouss_equations": manifest.bouss_equations,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest
