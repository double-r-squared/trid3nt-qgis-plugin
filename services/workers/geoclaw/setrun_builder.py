"""GeoClaw ``setrun.py`` authoring — the build_spec -> Clawpack deck adapter.

The GeoClaw analogue of ``services/workers/modflow/gwt_adapter.py`` (which authors
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
      - the surge scenario reuses a sea-surface boundary forcing (a fixed-grid
        sea_level offset for the v0.1 single-pulse fallback).

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
      "surge_forcing_file": "surge.csv",  # optional staged hydrograph
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


_VALID_SCENARIOS = {"dam_break", "tsunami", "surge"}


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
    source_magnitude: float = 8.0
    # tsunami Okada fault geometry (user-gated; synthetic defaults when omitted).
    fault_strike_deg: float | None = None
    fault_dip_deg: float | None = None
    fault_rake_deg: float | None = None
    fault_depth_km: float | None = None
    # surge.
    surge_forcing_file: str | None = None
    # Nested DEM(s), ordered coarse->fine, appended after the primary topo.
    extra_topo_files: list[str] = field(default_factory=list)
    # fgmax (max water depth / speed / arrival time) monitoring.
    fgmax_arrival_tol_m: float = 0.01
    # Coastal gauge (lon, lat); deterministic seaward-edge fallback if None.
    coastal_gauge_lonlat: tuple[float, float] | None = None


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


def parse_build_spec(raw: dict[str, Any]) -> GeoClawBuildSpec:
    """Validate the raw manifest ``build_spec`` dict -> a typed ``GeoClawBuildSpec``.

    Raises ``GeoClawDeckError`` (typed code) on a missing/invalid field so the
    entrypoint records an honest terminal error rather than crashing mid-deck.
    """
    if not isinstance(raw, dict):
        raise GeoClawDeckError(
            "GEOCLAW_SPEC_INVALID", f"build_spec must be a JSON object, got {type(raw)}"
        )

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
        extra_topo_files=extra_topo_files,
        fgmax_arrival_tol_m=_num("fgmax_arrival_tol_m", 0.01),
        coastal_gauge_lonlat=coastal_gauge_lonlat,
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


def render_maketopo_dtopo(spec: GeoClawBuildSpec) -> str:
    """Render a ``maketopo.py`` that synthesizes an Okada dtopo for the tsunami
    scenario (when no dtopo file was staged).

    Uses ``clawpack.geoclaw.dtopotools`` to build a single-subfault Okada source
    scaled from ``source_magnitude`` at the source point and write ``dtopo.tt3``.
    The fault GEOMETRY (strike / dip / rake / depth) is taken from the build_spec
    when the user supplied it, else a NON-SITE-SPECIFIC synthetic default - and
    the generated helper PRINTS a loud banner for every defaulted field so the
    run NEVER silently fabricates a site-specific source.

    The dtopo grid is built with ``fault.create_dtopo_xy(dx=1/60., buffer_size=
    2.0)`` (the canonical GeoClaw helper) rather than a hand-rolled
    ``np.linspace`` box, and ``coordinate_specification="centroid"`` is kept
    (required by Okada - a wrong/empty value raises ValueError).

    This is emitted as a SEPARATE Python helper the entrypoint runs BEFORE the
    solve (it imports clawpack, so it must not be imported by this authoring
    module). Pure string render here.
    """
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
            "NON-SITE-SPECIFIC synthetic source: fault geometry field(s) "
            + ", ".join(defaulted)
            + " were NOT user-supplied and use generic synthetic defaults; "
            "this dtopo is illustrative, NOT a site-specific seismic source."
        )
    else:
        banner = ""

    return f'''"""Auto-generated by the GeoClaw worker — synthesize an Okada dtopo."""
from clawpack.geoclaw import dtopotools

# Honesty banner: loudly flag a non-site-specific synthetic source so the run
# never silently fabricates a site-specific fault geometry.
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

# Build the dtopo grid with the canonical GeoClaw helper (auto-sizes a box
# around the fault with a buffer), not a hand-rolled linspace box.
x, y = fault.create_dtopo_xy(dx=1/60., buffer_size=2.0)
fault.create_dtopography(x, y, times=[0.0, 1.0])
fault.dtopo.write("dtopo.tt3", dtopo_type=3)
print("wrote dtopo.tt3 mw=%s slip=%.2f m strike=%s dip=%s rake=%s depth_m=%s"
      % (mw, slip, {float(strike)!r}, {float(dip)!r}, {float(rake)!r}, {depth_m!r}))
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
    # The AOI (fine-AMR region + fgmax monitor + gauge + rasterize extent).
    min_lon, min_lat, max_lon, max_lat = spec.bbox
    # The COMPUTATIONAL DOMAIN (clawdata lower/upper + base grid). Extends offshore
    # to span the Okada source for a tsunami; == AOI otherwise.
    dom_min_lon, dom_min_lat, dom_max_lon, dom_max_lat = _domain(spec)
    nx, ny = spec.base_num_cells
    amr_levels = int(spec.amr_levels)
    ratios = _refinement_ratios(amr_levels)
    amr_ratios = ", ".join(str(r) for r in ratios)

    # Evenly-spaced output frames including the final time (exclude t=0 dump:
    # GeoClaw always writes frame 0 at t=0, so we request output_frames AFTER it
    # via output_style=1 with num_output_times = output_frames and tfinal set).
    num_output_times = int(spec.output_frames)
    tfinal = float(spec.sim_duration_s)

    # Finest-level cell size (dx_fine): the base cell size divided by the product
    # of the refinement ratios. The BASE grid spans the COMPUTATIONAL DOMAIN, so
    # base_dx is measured across the domain (NOT the AOI) -- otherwise the fgmax
    # sample points would be mis-aligned with the finest-level FV cell centers
    # whenever the domain extends offshore beyond the AOI.
    base_dx = (dom_max_lon - dom_min_lon) / float(nx)
    refine_product = 1
    for r in ratios:
        refine_product *= int(r)
    dx_fine = base_dx / float(refine_product)

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
    # surge: the v0.1 fallback applies the sea_level offset only (a uniform
    # raised sea surface as a single-pulse surge); a staged hydrograph upgrade
    # plugs in here via a fgmax/boundary forcing in a later phase.

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
        fgmax_block = (
            "    # --- fgmax: max depth/speed/arrival monitored over the AOI ---\n"
            "    rundata.fgmax_data.num_fgmax_val = 2  # save max depth + speed\n"
            "    fgmax_grids = rundata.fgmax_data.fgmax_grids\n"
            f"    dx_fine = {dx_fine!r}  # finest-level cell size over the AOI\n"
            "    fg = fgmax_tools.FGmaxGrid()\n"
            "    fg.point_style = 2  # uniform rectangular x-y grid\n"
            "    # align sample pts with finest-level FV cell centers (half-cell inset):\n"
            f"    fg.x1 = {min_lon!r} + dx_fine / 2.0\n"
            f"    fg.x2 = {max_lon!r} - dx_fine / 2.0\n"
            f"    fg.y1 = {min_lat!r} + dx_fine / 2.0\n"
            f"    fg.y2 = {max_lat!r} - dx_fine / 2.0\n"
            "    fg.dx = dx_fine\n"
            "    fg.tstart_max = 0.0  # monitor max values from t0\n"
            "    fg.tend_max = 1.e10\n"
            f"    fg.dt_check = {dt_check!r}\n"
            f"    fg.min_level_check = {amr_levels!r}  # monitor on the finest level\n"
            f"    fg.arrival_tol = {arrival_tol!r}  # wet-cell threshold for arrival\n"
            "    fg.interp_method = 0  # 0 ==> pw const in cells, recommended\n"
            "    fgmax_grids.append(fg)\n"
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
        f"0., {tfinal!r}, {dom_min_lon!r}, {dom_max_lon!r}, {dom_min_lat!r}, {dom_max_lat!r}])\n"
        "    # --- Regions: pin the finest AMR level over the AOI for the run ---\n"
        f"    rundata.regiondata.regions.append([{amr_levels!r}, {amr_levels!r}, "
        f"0., {tfinal!r}, {min_lon!r}, {max_lon!r}, {min_lat!r}, {max_lat!r}])\n"
    )

    # --- GAP4 gauges: one coastal gauge (explicit or seaward-edge fallback) ---
    gx, gy = _coastal_gauge(spec)
    gauges_block = (
        "    # --- Gauges: one coastal time-series gauge ---\n"
        f"    rundata.gaugedata.gauges.append([1, {gx!r}, {gy!r}, 0., 1.e10])\n"
    )

    # --- GAP7 nested DEM: primary topo + any extra (coarse->fine) topo files ---
    topo_lines = [f"    topo_data.topofiles.append([3, {spec.topo_file!r}])\n"]
    for f in spec.extra_topo_files:
        topo_lines.append(f"    topo_data.topofiles.append([3, {f!r}])\n")
    topo_block = "".join(topo_lines)

    return f'''"""Auto-generated by the GeoClaw worker (setrun_builder).

Scenario: {spec.scenario}
Domain (EPSG:4326): {spec.bbox}
Do NOT hand-edit — regenerate from the build_spec.
"""
from clawpack.clawutil import data
{fgmax_import}

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

    clawdata.num_eqn = 3
    clawdata.num_aux = 3
    clawdata.capa_index = 2

    # --- Time domain + evenly-spaced output frames (the fort.q animation) ---
    clawdata.t0 = 0.0
    clawdata.output_style = 1
    clawdata.num_output_times = {num_output_times!r}
    clawdata.tfinal = {float(spec.sim_duration_s)!r}
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
    amrdata.amr_levels_max = {int(spec.amr_levels)!r}
    amrdata.refinement_ratios_x = [{amr_ratios}]
    amrdata.refinement_ratios_y = [{amr_ratios}]
    amrdata.refinement_ratios_t = [{amr_ratios}]
    amrdata.aux_type = ["center", "capacity", "yleft"]
    amrdata.flag_richardson = False
    amrdata.flag2refine = True
    amrdata.regrid_interval = 3
    amrdata.regrid_buffer_width = 2
    amrdata.verbosity_regrid = 0

{regions_block}{gauges_block}{fgmax_block}{qinit_block}{dtopo_block}    return rundata


def setgeo(rundata):
    try:
        geo_data = rundata.geo_data
    except AttributeError:
        raise AttributeError("Missing geo_data; rundata must be a GeoClaw run.")

    geo_data.gravity = 9.81
    geo_data.coordinate_system = 2  # 2 = lat/lon (spherical)
    geo_data.earth_radius = 6367500.0

    geo_data.dry_tolerance = 1.0e-3
    geo_data.friction_forcing = True
    geo_data.manning_coefficient = {float(spec.manning_n)!r}
    geo_data.friction_depth = 1.0e6

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
    if spec.scenario == "dam_break":
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
        driver = f"surge sea_level offset {spec.sea_level_m:.2f} m (v0.1 fallback)"

    manifest = DeckManifest(
        scenario=spec.scenario,
        bbox=spec.bbox,
        base_num_cells=spec.base_num_cells,
        amr_levels=spec.amr_levels,
        output_frames=spec.output_frames,
        sim_duration_s=spec.sim_duration_s,
        files_written=written,
        driver_descriptor=driver,
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
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest
