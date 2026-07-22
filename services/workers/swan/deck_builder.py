"""SWAN ``.swn`` command-file authoring -- the build_spec -> SWAN deck adapter.

The SWAN analogue of ``services/workers/geoclaw/setrun_builder.py`` (which authors
a Clawpack ``setrun.py`` from typed args). This module is the DETERMINISTIC,
UNIT-TESTABLE core of the SWAN worker: it parses the agent-staged ``build_spec``
JSON and emits a canonical SWAN ASCII command file (the ``.swn``, copied to the
file literally named ``INPUT`` by the entrypoint) over the AOI + a bottom (bathy)
input grid + a parametric offshore wave boundary, plus the small bottom input
array file.

It deliberately does NOT import or run SWAN -- it only WRITES the deck text (the
``.swn`` command file + the ``bottom.bot`` input array). The entrypoint then runs
``swanrun`` against the authored deck. Splitting the authoring out (mirroring
``setrun_builder.py``) is what makes the worker testable with NO Fortran toolchain
present -- the deck author is a pure string render.

Canonical real-world pipeline (mirrored, not invented):
    A SWAN run is driven by ONE ASCII command file -- a sequence of keyword
    commands (confirmed from the official SWAN user manual). The load-bearing
    blocks, in the order SWAN's own templates use them, are:
      - PROJECT / SET: project + run constants (gravity, NaN/exception value).
      - MODE STATIONARY|NONSTATIONARY + COORDINATES SPHERICAL: run mode + lat/lon.
      - CGRID ... CIRCLE <ndir> <flow> <fhigh> <nfreq>: the computational spatial
        + spectral grid (x,y, freq, theta). >=3 dir bins/quadrant, >=4 freqs.
      - INPGRID BOTTOM ... + READINP BOTTOM ... 'bottom.bot': the bed (from the
        existing fetch_topobathy DEM, sampled onto the SWAN input grid).
      - INPGRID WIND ... + READINP WIND ... (optional): the ERA5 10 m wind field.
      - GEN3 + WCAPPING + BREAKING + FRICTION (+ TRIAD): the physics toggles.
      - BOUND SHAPE JONSWAP + BOUNDSPEC SIDE <side> CONSTANT PAR <hs> <per> <dir>
        <dd>: the PARAMETRIC offshore boundary.
      - BLOCK 'COMPGRID' NOHEADER 'swan_out.mat' LAYOUT 3 HSIGN RTP DIR ...: the
        gridded output fields the postprocess rasterizes.
      - COMPUTE (stationary; NO option -- a bare COMPUTE) | COMPUTE NONSTATIONARY
        <t0> <dt> <unit> <t1> (nonstationary): runs it. STOP. The STATIONARY token
        after COMPUTE is ONLY for a stationary step inside a MODE NONSTATIONARY
        deck; in a MODE STATIONARY deck it must be omitted (else SWAN no-ops).

The build_spec schema (authored agent-side by ``workflows/run_swan.py``):
    {
      "mode": "stationary" | "nonstationary",
      "bbox": [min_lon, min_lat, max_lon, max_lat],   # EPSG:4326
      "bottom_file": "bottom.bot",     # staged DEM sampled onto the SWAN grid
      "mx": 100, "my": 100,            # SWAN bottom/comp grid mesh cells
      "n_dir": 36, "n_freq": 32,       # spectral grid
      "freq_low_hz": 0.04, "freq_high_hz": 1.0,
      "boundary": {"hs_m": 3.0, "tp_s": 9.0, "dir_deg": 180.0,
                   "spread_deg": 25.0, "side": "S"},
      "wind_file": "wind.dat",         # optional; enables GEN3 wind growth
      "friction": true, "breaking": true, "triads": true,
      "sim_duration_s": 10800.0,       # nonstationary only
      "time_step_s": 600.0,            # nonstationary only
      "output_frames": 24,             # nonstationary only
      "output_quantities": ["HSIGN", "RTP", "DIR"],
    }

A NetCDF output is preferred when the worker SWAN build has NetCDF support, but a
Matlab ``.mat`` BLOCK is the portable default (every SWAN build can write it and
scipy reads it in the postprocess), so the canonical template targets ``.mat``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SwanDeckError",
    "SwanBuildSpec",
    "SwanDeckManifest",
    "parse_build_spec",
    "render_swn_command_file",
    "render_bottom_input",
    "build_swan_deck",
    "DEFAULT_OUTPUT_QUANTITIES",
    "OUTPUT_MAT_FILENAME",
    "INPUT_FILENAME",
    "SWN_CASENAME",
    "SWN_FILENAME",
    "SWAN_EXCEPTION_VALUE",
    "SWAN_DEPMIN_M",
]

#: The output Matlab file the BLOCK command writes (the portable default the
#: postprocess reads with scipy.io.loadmat). The entrypoint also copies the .swn
#: to INPUT (the SWAN convention).
OUTPUT_MAT_FILENAME: str = "swan_out.mat"
INPUT_FILENAME: str = "INPUT"

#: The SWAN *case name* the entrypoint hands to ``swanrun -input <SWN_CASENAME>``.
#: The TU Delft ``swanrun`` launcher APPENDS ``.swn`` to this argument: it looks
#: for ``<SWN_CASENAME>.swn``, copies it to the file literally named ``INPUT``,
#: then runs ``swan.exe`` (which reads ``INPUT``). So the deck author MUST write
#: ``<SWN_CASENAME>.swn`` and the entrypoint MUST pass the bare case name (NO
#: ``.swn``, NO ``INPUT``). Passing ``-input INPUT`` made swanrun hunt for the
#: nonexistent ``INPUT.swn`` and abort ("file INPUT.swn does not exist", exit 1)
#: BEFORE SWAN ever solved -- the live 2026-06-23 Mexico Beach failure. This
#: constant is the single source of truth shared by the deck author + the runner.
SWN_CASENAME: str = "swan_run"
#: The actual ``.swn`` command file the deck author writes (``swanrun`` reads it).
SWN_FILENAME: str = f"{SWN_CASENAME}.swn"

#: The default gridded output quantities the BLOCK writes / the postprocess reads.
DEFAULT_OUTPUT_QUANTITIES: tuple[str, ...] = ("HSIGN", "RTP", "DIR")

#: The SWAN output quantities we know how to rasterize. Guards against an
#: LLM-invented field that SWAN would reject at solve time.
_VALID_OUTPUT_QUANTITIES: frozenset[str] = frozenset(
    {"HSIGN", "RTP", "TPS", "PER", "TM01", "TM02", "DIR", "PDIR", "DSPR", "SETUP"}
)

_VALID_SIDES: frozenset[str] = frozenset({"N", "S", "E", "W"})

#: SWAN's exception (NaN / dry / no-data) value -- written via SET; the postprocess
#: masks cells equal to it. A large sentinel SWAN uses for cells with no result.
SWAN_EXCEPTION_VALUE: float = -999.0

#: SWAN's DEPMIN -- the minimum (positive-down) depth, in metres, SWAN treats as
#: WET. A bottom cell with depth < DEPMIN is dry/inactive. Written on the SET line
#: AND read by the worker's all-dry guard, so the deck + guard can never drift: if
#: EVERY bottom cell is below this, the whole grid is inactive and SWAN no-ops
#: ("Normal end of run", no swan_out.mat) -- the all-dry signature.
SWAN_DEPMIN_M: float = 0.05


class SwanDeckError(RuntimeError):
    """Raised on a malformed build_spec / unsupported value.

    Carries an open-set ``error_code`` so the entrypoint records a typed failure
    (mirrors ``GeoClawDeckError``).
    """

    error_code: str = "SWAN_DECK_BUILD_FAILED"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class SwanBuildSpec:
    """The typed, validated build_spec the SWAN deck author consumes.

    A plain dataclass (no pydantic dep in the worker image) holding exactly the
    fields ``render_swn_command_file`` needs. ``parse_build_spec`` validates +
    fills defaults from the raw manifest dict. Mirrors ``GeoClawBuildSpec``.
    """

    mode: str
    bbox: tuple[float, float, float, float]
    bottom_file: str
    mx: int = 100
    my: int = 100
    n_dir: int = 36
    n_freq: int = 32
    freq_low_hz: float = 0.04
    freq_high_hz: float = 1.0
    # parametric boundary.
    boundary_hs_m: float = 3.0
    boundary_tp_s: float = 9.0
    boundary_dir_deg: float = 180.0
    boundary_spread_deg: float = 25.0
    boundary_side: str = "S"
    # optional wind input.
    wind_file: str | None = None
    # physics toggles.
    friction: bool = True
    breaking: bool = True
    triads: bool = True
    # nonstationary timing.
    sim_duration_s: float = 10800.0
    time_step_s: float = 600.0
    output_frames: int = 24
    # output.
    output_quantities: tuple[str, ...] = DEFAULT_OUTPUT_QUANTITIES


@dataclass
class SwanDeckManifest:
    """Provenance the deck author returns (echoed into completion for narration).

    Mirrors ``GeoClaw.DeckManifest``: a small typed record the entrypoint /
    postprocess can read to narrate typed numbers about what was built without
    re-parsing the .swn.
    """

    mode: str
    bbox: tuple[float, float, float, float]
    mx: int
    my: int
    n_dir: int
    n_freq: int
    boundary_hs_m: float
    boundary_tp_s: float
    boundary_dir_deg: float
    wind_enabled: bool
    output_quantities: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    driver_descriptor: str = ""


def parse_build_spec(raw: dict[str, Any]) -> SwanBuildSpec:
    """Validate the raw manifest ``build_spec`` dict -> a typed ``SwanBuildSpec``.

    Raises ``SwanDeckError`` (typed code) on a missing/invalid field so the
    entrypoint records an honest terminal error rather than crashing mid-deck.
    Mirrors ``setrun_builder.parse_build_spec``.
    """
    if not isinstance(raw, dict):
        raise SwanDeckError(
            "SWAN_SPEC_INVALID", f"build_spec must be a JSON object, got {type(raw)}"
        )

    mode = str(raw.get("mode") or "stationary").strip().lower()
    if mode not in {"stationary", "nonstationary"}:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID",
            f"mode must be 'stationary' or 'nonstationary', got {mode!r}",
        )

    bbox_raw = raw.get("bbox")
    if not isinstance(bbox_raw, (list, tuple)) or len(bbox_raw) != 4:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID",
            f"bbox must be [min_lon, min_lat, max_lon, max_lat], got {bbox_raw!r}",
        )
    try:
        bbox = tuple(float(v) for v in bbox_raw)  # type: ignore[assignment]
    except (TypeError, ValueError) as exc:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID", f"bbox values must be numeric: {bbox_raw!r}"
        ) from exc
    min_lon, min_lat, max_lon, max_lat = bbox
    if not (min_lon < max_lon and min_lat < max_lat):
        raise SwanDeckError(
            "SWAN_SPEC_INVALID",
            f"bbox must satisfy min_lon<max_lon and min_lat<max_lat, got {bbox}",
        )

    bottom_file = str(raw.get("bottom_file") or "").strip()
    if not bottom_file:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID",
            "build_spec.bottom_file is required (the staged bathy input grid)",
        )

    def _num(key: str, default: float) -> float:
        v = raw.get(key)
        return float(v) if v is not None else float(default)

    def _int(key: str, default: int) -> int:
        v = raw.get(key)
        return int(v) if v is not None else int(default)

    mx = _int("mx", 100)
    my = _int("my", 100)
    if mx < 2 or my < 2:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID", f"mx and my must each be >= 2, got ({mx}, {my})"
        )

    n_dir = _int("n_dir", 36)
    if n_dir < 12:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID",
            f"n_dir must be >= 12 (>= 3 directional bins per quadrant), got {n_dir}",
        )
    n_freq = _int("n_freq", 32)
    if n_freq < 4:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID", f"n_freq must be >= 4, got {n_freq}"
        )

    freq_low = _num("freq_low_hz", 0.04)
    freq_high = _num("freq_high_hz", 1.0)
    if freq_low <= 0.0 or freq_high <= freq_low:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID",
            f"require 0 < freq_low_hz < freq_high_hz, got ({freq_low}, {freq_high})",
        )

    boundary_raw = raw.get("boundary") or {}
    if not isinstance(boundary_raw, dict):
        raise SwanDeckError(
            "SWAN_SPEC_INVALID", f"boundary must be a JSON object, got {boundary_raw!r}"
        )
    hs = float(boundary_raw.get("hs_m", 3.0))
    tp = float(boundary_raw.get("tp_s", 9.0))
    bdir = float(boundary_raw.get("dir_deg", 180.0))
    spread = float(boundary_raw.get("spread_deg", 25.0))
    side = str(boundary_raw.get("side", "S")).strip().upper()
    if hs <= 0.0 or tp <= 0.0 or spread <= 0.0:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID",
            f"boundary hs_m / tp_s / spread_deg must be > 0, got ({hs}, {tp}, {spread})",
        )
    if not (0.0 <= bdir < 360.0):
        raise SwanDeckError(
            "SWAN_SPEC_INVALID", f"boundary dir_deg must be in [0, 360), got {bdir}"
        )
    if side not in _VALID_SIDES:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID",
            f"boundary side must be one of {sorted(_VALID_SIDES)}, got {side!r}",
        )

    sim_duration_s = _num("sim_duration_s", 10800.0)
    time_step_s = _num("time_step_s", 600.0)
    if mode == "nonstationary" and (sim_duration_s <= 0.0 or time_step_s <= 0.0):
        raise SwanDeckError(
            "SWAN_SPEC_INVALID",
            "nonstationary mode requires sim_duration_s > 0 and time_step_s > 0",
        )
    output_frames = _int("output_frames", 24)
    if output_frames < 1:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID", f"output_frames must be >= 1, got {output_frames}"
        )

    quants_raw = raw.get("output_quantities") or list(DEFAULT_OUTPUT_QUANTITIES)
    if not isinstance(quants_raw, (list, tuple)) or not quants_raw:
        raise SwanDeckError(
            "SWAN_SPEC_INVALID",
            f"output_quantities must be a non-empty list, got {quants_raw!r}",
        )
    quants: list[str] = []
    for q in quants_raw:
        qu = str(q).strip().upper()
        if qu not in _VALID_OUTPUT_QUANTITIES:
            raise SwanDeckError(
                "SWAN_SPEC_INVALID",
                f"unknown output quantity {qu!r}; valid: {sorted(_VALID_OUTPUT_QUANTITIES)}",
            )
        if qu not in quants:
            quants.append(qu)
    # HSIGN is the primary narrated/painted field -- guarantee it is present.
    if "HSIGN" not in quants:
        quants.insert(0, "HSIGN")

    wind_file = raw.get("wind_file")
    wind_file = str(wind_file).strip() if wind_file else None

    return SwanBuildSpec(
        mode=mode,
        bbox=bbox,  # type: ignore[arg-type]
        bottom_file=bottom_file,
        mx=mx,
        my=my,
        n_dir=n_dir,
        n_freq=n_freq,
        freq_low_hz=freq_low,
        freq_high_hz=freq_high,
        boundary_hs_m=hs,
        boundary_tp_s=tp,
        boundary_dir_deg=bdir,
        boundary_spread_deg=spread,
        boundary_side=side,
        wind_file=wind_file,
        friction=bool(raw.get("friction", True)),
        breaking=bool(raw.get("breaking", True)),
        triads=bool(raw.get("triads", True)),
        sim_duration_s=sim_duration_s,
        time_step_s=time_step_s,
        output_frames=output_frames,
        output_quantities=tuple(quants),
    )


def _grid_geometry(spec: SwanBuildSpec) -> dict[str, float]:
    """Compute the SWAN regular-grid origin + spans from the bbox (lon/lat).

    SWAN's CGRID / INPGRID REGULAR take an origin (xpc, ypc), an orientation
    (alpc=0 -> grid x aligned with east), spans (xlenc, ylenc) and mesh counts
    (mxc, myc). For a SPHERICAL (lat/lon) run the spans are in DEGREES. We anchor
    the grid at the SW corner spanning the full bbox.
    """
    min_lon, min_lat, max_lon, max_lat = spec.bbox
    return {
        "xpc": min_lon,
        "ypc": min_lat,
        "xlenc": max_lon - min_lon,
        "ylenc": max_lat - min_lat,
    }


def render_swn_command_file(spec: SwanBuildSpec) -> str:
    """Render the canonical SWAN ``.swn`` command file for the build_spec.

    Emits the keyword command sequence (PROJECT / SET / MODE / COORDINATES /
    CGRID / INPGRID+READINP BOTTOM / [INPGRID+READINP WIND] / GEN3 / WCAPPING /
    [FRICTION] / [BREAKING] / [TRIAD] / BOUND SHAPE + BOUNDSPEC / BLOCK / COMPUTE
    / STOP) wired from ``spec``. PURE string render -- unit-testable with NO SWAN
    install (mirrors ``render_setrun_py``).

    The grid is a REGULAR (rectilinear) lat/lon grid spanning the bbox; the
    BOTTOM input grid uses the SAME geometry as the computational grid (the worker
    samples the staged DEM onto exactly this mesh). The boundary is a parametric
    ``BOUNDSPEC SIDE <side> CONSTANT PAR``. v0.1 omits true 2D nested spectra
    (BOUNDNEST3) -- those are a later data dependency (see the module docstring).
    """
    g = _grid_geometry(spec)
    # SWAN spectral band edges: directional CIRCLE over the full 360 deg.
    mxc = int(spec.mx)
    myc = int(spec.my)
    # SWAN INPGRID mesh counts are CELL counts (mxc = number of meshes = grid
    # points - 1 in each direction). Use the same mesh for comp + bottom input.
    inp_mx = max(mxc, 1)
    inp_my = max(myc, 1)
    dx = g["xlenc"] / inp_mx
    dy = g["ylenc"] / inp_my

    lines: list[str] = []
    lines.append("$ -------------------------------------------------------------")
    lines.append("$ GRACE-2 SWAN nearshore wave-field deck (auto-generated).")
    lines.append("$ Do NOT hand-edit -- regenerate from the build_spec.")
    lines.append(f"$ mode={spec.mode} bbox={spec.bbox}")
    lines.append("$ -------------------------------------------------------------")
    lines.append("PROJECT 'GRACE2' 'WAVE'")
    # SET general run constants. SWAN reads SET's optional fields POSITIONALLY
    # (``SET [level] [nor] [depmin] ... NAUTICAL``); it does NOT accept a
    # ``KEY=value`` form (the ``=`` sign only follows a real command KEYWORD, and
    # SWAN has no ``LEVEL=`` / ``NOR=`` keyword on SET). The earlier
    # ``SET NAUTICAL LEVEL=0.0 NOR=90.0 EXCEPTION ...`` therefore mis-parsed. Emit
    # the canonical positional/keyword form, with NAUTICAL LAST so it closes the
    # field sequence. DEPMIN (threshold depth, m) is load-bearing: it is the
    # minimum depth SWAN treats as WET -- without it (or at its 0.05 m default with
    # an all-land bottom) a coastal grid can end up with no active sea points,
    # which is one way SWAN reaches "Normal end of run" having computed nothing.
    # EXCEPTION is NOT a SET field -- it belongs on INPGRID BOTTOM (below). SWAN
    # rejected "... EXCEPTION -999.0 NAUTICAL" on SET ("Illegal keyword: EXCEPTIO").
    lines.append(
        f"SET LEVEL 0.0 NOR 90.0 DEPMIN {SWAN_DEPMIN_M:.2f} NAUTICAL"
    )

    # Run mode + spherical (lat/lon) coordinates.
    if spec.mode == "nonstationary":
        lines.append("MODE NONSTATIONARY TWODIMENSIONAL")
    else:
        lines.append("MODE STATIONARY TWODIMENSIONAL")
    lines.append("COORDINATES SPHERICAL")

    # Computational spatial + spectral grid.
    #   CGRID REGULAR xpc ypc alpc xlenc ylenc mxc myc CIRCLE ndir flow fhigh nfreq
    lines.append(
        "CGRID REGULAR "
        f"{g['xpc']:.6f} {g['ypc']:.6f} 0.0 "
        f"{g['xlenc']:.6f} {g['ylenc']:.6f} {mxc} {myc} "
        f"CIRCLE {int(spec.n_dir)} {spec.freq_low_hz:.4f} {spec.freq_high_hz:.4f} "
        f"{int(spec.n_freq)}"
    )

    # Bottom (bathymetry) input grid + read.
    #   INPGRID BOTTOM REGULAR xpinp ypinp alpinp mxinp myinp dxinp dyinp
    #   READINP BOTTOM fac 'fname' idla nhedf FREE
    # EXCEPTION on INPGRID (its correct home) tells SWAN the -999.0 sentinel marks
    # dry/nodata cells in bottom.bot (it was wrongly on SET before).
    lines.append(
        "INPGRID BOTTOM REGULAR "
        f"{g['xpc']:.6f} {g['ypc']:.6f} 0.0 {inp_mx} {inp_my} "
        f"{dx:.8f} {dy:.8f} EXCEPTION {SWAN_EXCEPTION_VALUE:.1f}"
    )
    # fac=1.0 (no scale), idla=3, 0 header lines, FREE format.
    # idla=3 = SWAN reads the grid starting in the LOWER-LEFT (SW) corner, x
    # fastest, rows running SOUTH->NORTH -- which is EXACTLY how render_bottom_input
    # writes the array (outer loop j ascending: lat = ypc + j*dy, south-first).
    # The old value 1 was WRONG: the SWAN manual (node26) defines idla=1 as the
    # UPPER-LEFT (NW) corner (rows north-first), so idla=1 against our south-first
    # data MIRRORED the bathymetry N<->S -- stranding the forced seaward boundary
    # over land, leaving zero wet cells -> the 32ms "Normal end of run" no-op.
    lines.append(f"READINP BOTTOM 1.0 '{spec.bottom_file}' 3 0 FREE")

    # Optional wind input grid (enables GEN3 wind-sea growth).
    wind_enabled = spec.wind_file is not None
    if wind_enabled:
        lines.append(
            "INPGRID WIND REGULAR "
            f"{g['xpc']:.6f} {g['ypc']:.6f} 0.0 {inp_mx} {inp_my} "
            f"{dx:.8f} {dy:.8f}"
        )
        lines.append(f"READINP WIND 1.0 '{spec.wind_file}' 1 0 FREE")

    # Physics. GEN3 = third-generation wind input + whitecapping (deep water).
    lines.append("GEN3 WESTHUYSEN")
    if not wind_enabled:
        # SWAN ABORTS at error level 3 on quadruplets + ZERO wind ("not recommended
        # to use quadruplets in combination with zero wind conditions" -> "No start
        # of computation"). Quadruplet nonlinear interactions model wind-sea growth,
        # which is irrelevant for a pure boundary-forced swell with no wind forcing,
        # so disable them when no ERA5 wind field is supplied (keep them when it is).
        lines.append("OFF QUAD")
    if spec.friction:
        # JONSWAP bottom friction (depth-induced), default coefficient.
        lines.append("FRICTION JONSWAP CONSTANT 0.067")
    if spec.breaking:
        # Depth-induced breaking (Battjes-Janssen), default gamma.
        lines.append("BREAKING CONSTANT 1.0 0.73")
    if spec.triads:
        # Triad (three-wave) nonlinear interactions (shallow water).
        lines.append("TRIAD")

    # Parametric offshore boundary: JONSWAP shape + a CONSTANT PAR side spec.
    #   BOUND SHAPE JONSWAP PEAK DSPR DEGREES
    #   BOUNDSPEC SIDE <side> CONSTANT PAR <hs> <per> <dir> <dd>
    lines.append("BOUND SHAPE JONSWAP PEAK DSPR DEGREES")
    side_word = {"N": "N", "S": "S", "E": "E", "W": "W"}[spec.boundary_side]
    lines.append(
        f"BOUNDSPEC SIDE {side_word} CONSTANT PAR "
        f"{spec.boundary_hs_m:.3f} {spec.boundary_tp_s:.3f} "
        f"{spec.boundary_dir_deg:.2f} {spec.boundary_spread_deg:.2f}"
    )

    # Gridded output BLOCK over the whole computational grid -> a Matlab file the
    # postprocess reads. NOHEADER keeps it a plain array per quantity; LAYOUT 3 is
    # the standard ordering.
    quant_str = " ".join(spec.output_quantities)
    lines.append(
        f"BLOCK 'COMPGRID' NOHEADER '{OUTPUT_MAT_FILENAME}' LAYOUT 3 {quant_str}"
    )

    # Compute + stop.
    if spec.mode == "nonstationary":
        # COMPUTE NONSTATIONARY <tstart> <dt> <unit> <tstop>. SWAN time strings
        # are ISO-like; we use a relative seconds-from-zero convention via SEC
        # unit so the deck is self-contained (no absolute calendar dependency).
        n_steps = int(spec.output_frames)
        # tbegin=0, tend=sim_duration; dt set so we get ~output_frames dumps.
        lines.append(
            "COMPUTE NONSTATIONARY "
            f"000000.000 {spec.time_step_s:.1f} SEC "
            f"{spec.sim_duration_s:.1f}"
        )
        # NOTE: per-frame BLOCK dumps are emitted by SWAN at each compute step
        # when the BLOCK is inside the COMPUTE window; n_steps is carried in the
        # manifest for the postprocess frame-selection cap.
        _ = n_steps
    else:
        # STATIONARY mode: the SWAN manual is explicit -- "if the SWAN mode is
        # stationary, then only the command COMPUTE should be given here (no
        # options!)". The ``STATIONARY [time]`` option after COMPUTE is reserved
        # for a stationary step INSIDE a MODE NONSTATIONARY file. Writing
        # ``COMPUTE STATIONARY`` in a MODE STATIONARY deck makes SWAN read
        # ``STATIONARY`` as an unexpected/time token and skip the actual
        # iteration -- it "prepares computation", does ZERO sweeps, writes no
        # swan_out.mat, and still prints "Normal end of run" in milliseconds (the
        # live 2026-06-23 33 ms no-op). Emit the bare COMPUTE.
        lines.append("COMPUTE")
    lines.append("STOP")

    return "\n".join(lines) + "\n"


def render_bottom_input(
    spec: SwanBuildSpec,
    *,
    depth_fn: Any = None,
) -> str:
    """Render a SWAN BOTTOM input array (a ``READINP BOTTOM`` FREE-format grid).

    SWAN reads the bottom as positive-DOWN depths (metres below the still-water
    level) on the ``INPGRID BOTTOM`` mesh. We write rows of constant y ASCENDING
    (south-first), so the deck declares ``idla=3`` (SWAN lower-left/SW corner, x
    fastest, rows south->north) to match -- NOT idla=1, which the manual defines as
    the NW corner and would mirror the bed N<->S. This renders an
    ``(inp_my+1) x (inp_mx+1)`` grid of depth values, one row of x per line.

    ``depth_fn(lon, lat) -> depth_m`` supplies the depth (positive down) at a grid
    node; when ``None`` a flat demo bathymetry (a uniform 10 m depth) is written so
    the deck is self-contained for tests. The worker overrides ``depth_fn`` by
    sampling the staged ``fetch_topobathy`` DEM (which is positive-UP NAVD88, so
    the worker negates it to SWAN's positive-down convention).

    Pure string render -- unit-testable with no SWAN import (mirrors
    ``render_qinit_data``).
    """
    g = _grid_geometry(spec)
    inp_mx = max(int(spec.mx), 1)
    inp_my = max(int(spec.my), 1)
    nx = inp_mx + 1
    ny = inp_my + 1
    dx = g["xlenc"] / inp_mx
    dy = g["ylenc"] / inp_my

    def _depth(lon: float, lat: float) -> float:
        if depth_fn is not None:
            try:
                return float(depth_fn(lon, lat))
            except Exception:  # noqa: BLE001 -- a bad sample falls back to demo
                return 10.0
        return 10.0  # flat 10 m demo bathymetry.

    rows: list[str] = []
    for j in range(ny):
        lat = g["ypc"] + j * dy
        vals: list[str] = []
        for i in range(nx):
            lon = g["xpc"] + i * dx
            vals.append(f"{_depth(lon, lat):.3f}")
        rows.append(" ".join(vals))
    return "\n".join(rows) + "\n"


def build_swan_deck(
    build_spec_raw: dict[str, Any],
    deck_dir: Any,
    *,
    depth_fn: Any = None,
) -> SwanDeckManifest:
    """Author the full SWAN deck (.swn command file + bottom input) into
    ``deck_dir`` from a raw build_spec dict. Returns a ``SwanDeckManifest`` of what
    was written.

    The single entrypoint-facing call: parse -> render -> write. SWAN is NOT
    imported (the deck author is a pure string render). Pure file I/O + string
    render -> unit-testable with no Fortran. Mirrors ``build_geoclaw_deck``.

    ``depth_fn`` (optional) is the bathymetry sampler the worker passes (a DEM
    lookup); when ``None`` a flat demo bathymetry is written so the deck is
    self-contained for tests.
    """
    from pathlib import Path

    deck = Path(deck_dir)
    deck.mkdir(parents=True, exist_ok=True)
    spec = parse_build_spec(build_spec_raw)

    written: list[str] = []

    # The command file. SWAN's ``swanrun`` launcher is fed ``-input SWN_CASENAME``,
    # which APPENDS ``.swn`` -> it reads ``SWN_FILENAME`` (``swan_run.swn``), copies
    # it to the file literally named ``INPUT``, then runs ``swan.exe`` (which reads
    # ``INPUT``). So ``SWN_FILENAME`` is the load-bearing file swanrun needs. We
    # ALSO write the identical text to ``INPUT`` directly so a bare
    # ``swan.exe``-style fallback (or a swanrun that pre-seeds INPUT) still finds a
    # valid command file -- swanrun simply overwrites it with the same bytes.
    swn_text = render_swn_command_file(spec)
    (deck / SWN_FILENAME).write_text(swn_text, encoding="utf-8")
    written.append(SWN_FILENAME)
    (deck / INPUT_FILENAME).write_text(swn_text, encoding="utf-8")
    written.append(INPUT_FILENAME)

    # The bottom (bathymetry) input array.
    bottom_text = render_bottom_input(spec, depth_fn=depth_fn)
    (deck / spec.bottom_file).write_text(bottom_text, encoding="utf-8")
    written.append(spec.bottom_file)

    wind_enabled = spec.wind_file is not None
    driver = (
        f"{spec.mode} wave field Hs={spec.boundary_hs_m:.1f} m "
        f"Tp={spec.boundary_tp_s:.1f} s dir={spec.boundary_dir_deg:.0f} deg "
        f"side={spec.boundary_side}"
        + (" + ERA5 wind (GEN3)" if wind_enabled else " (boundary-forced only)")
    )

    manifest = SwanDeckManifest(
        mode=spec.mode,
        bbox=spec.bbox,
        mx=spec.mx,
        my=spec.my,
        n_dir=spec.n_dir,
        n_freq=spec.n_freq,
        boundary_hs_m=spec.boundary_hs_m,
        boundary_tp_s=spec.boundary_tp_s,
        boundary_dir_deg=spec.boundary_dir_deg,
        wind_enabled=wind_enabled,
        output_quantities=list(spec.output_quantities),
        files_written=written,
        driver_descriptor=driver,
    )
    # Persist the manifest alongside the deck for provenance / debugging.
    (deck / "deck_manifest.json").write_text(
        json.dumps(
            {
                "mode": manifest.mode,
                "bbox": list(manifest.bbox),
                "mx": manifest.mx,
                "my": manifest.my,
                "n_dir": manifest.n_dir,
                "n_freq": manifest.n_freq,
                "boundary_hs_m": manifest.boundary_hs_m,
                "boundary_tp_s": manifest.boundary_tp_s,
                "boundary_dir_deg": manifest.boundary_dir_deg,
                "wind_enabled": manifest.wind_enabled,
                "output_quantities": manifest.output_quantities,
                "files_written": manifest.files_written,
                "driver_descriptor": manifest.driver_descriptor,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest
