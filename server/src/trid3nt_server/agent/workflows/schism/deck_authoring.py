"""SCHISM case-deck authoring for the ``tidal_hydro`` archetype (ADR 0118).

Two mesh sources, one barotropic tidal deck:

  * ``bundled_quarterannulus`` -- STAGE the bundled Test_QuarterAnnulus fixture
    deck verbatim (the verification case whose green gate the spike proved,
    ADR 0115): hgrid.gr3 + vgrid.in + param.nml + bctides.in + drag.gr3 +
    station.in + the analytical reference ForPlot_ana_elev.dat.
  * ``coastal_tin`` -- AUTHOR a deck for a supplied oceanmesh TIN: the
    ``tin_to_hgrid`` bridge (the worker's proven pure-numpy converter) turns
    lon/lat nodes + triangles + per-node bathymetry (sampled from a fetched
    DEM/topobathy COG) into hgrid.gr3; the QA param.nml + vgrid.in (2D
    barotropic, nvrt=2, nchi=0) are reused as the proven hydro-core template with
    rnday/dt/ihfskip substituted; bctides.in is authored analytically from the
    requested tidal constituents (a spatially-uniform amplitude/phase boundary --
    a screening tidal forcing); drag.gr3 carries a uniform coastal Cd; station.in
    sits at the mesh centroid.

The gr3 bridge lives in the WORKER tree (``services/workers/schism/schism_gr3.py``,
pure numpy, no server imports) so it stays offline-suite-neutral; this module
loads it BY FILE PATH (importlib) rather than duplicating it -- the worker module
is the single source of truth for the format bridge.

ASCII only. No heavy imports at module load (numpy/rasterio are lazy).
"""

from __future__ import annotations

import importlib.util
import logging
import math
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger("trid3nt_server.agent.workflows.schism.deck_authoring")

__all__ = [
    "SchismDeckError",
    "quarterannulus_fixture_dir",
    "stage_quarterannulus_deck",
    "wwm_duck_fixture_dir",
    "stage_wwm_duck_deck",
    "load_gr3_bridge",
    "sample_bathymetry_on_nodes",
    "author_coastal_tin_deck",
    "CONSTITUENT_ANGULAR_FREQ_RAD_S",
]


class SchismDeckError(RuntimeError):
    """Raised when deck authoring/staging fails before dispatch."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: The QuarterAnnulus fixture deck files staged verbatim for the verification
#: archetype (the analytical reference rides along for the RMSE gate).
_QA_DECK_FILES: tuple[str, ...] = (
    "hgrid.gr3",
    "vgrid.in",
    "param.nml",
    "bctides.in",
    "drag.gr3",
    "station.in",
    "ForPlot_ana_elev.dat",
)

#: Major-constituent angular frequencies (rad/s) for the analytical bctides
#: boundary. M2 matches the QA fixture (1.405257e-4). Values are the standard
#: astronomical tidal frequencies.
CONSTITUENT_ANGULAR_FREQ_RAD_S: dict[str, float] = {
    "M2": 1.405257e-4,
    "S2": 1.454441e-4,
    "N2": 1.378797e-4,
    "K2": 1.458423e-4,
    "K1": 7.292117e-5,
    "O1": 6.759774e-5,
    "P1": 7.252295e-5,
    "Q1": 6.495854e-5,
}


def quarterannulus_fixture_dir() -> Path:
    """Resolve the repo's bundled QuarterAnnulus fixture directory.

    Walks up from this module to the trid3nt-local root (the dir holding
    ``services/``) and returns ``services/workers/schism/fixtures/quarterannulus``.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "services" / "workers" / "schism" / "fixtures" / "quarterannulus"
        if cand.is_dir():
            return cand
    raise SchismDeckError(
        "SCHISM_INPUT_INVALID",
        "could not locate the bundled QuarterAnnulus fixture deck under services/workers/schism/fixtures",
    )


def stage_quarterannulus_deck(dest_dir: str | Path) -> list[Path]:
    """Copy the bundled QuarterAnnulus deck into ``dest_dir``; return the file paths.

    The deck is proven-green (the spike's in-image gate). Staged verbatim -- no
    reparameterization (the verification archetype exercises the published case)."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = quarterannulus_fixture_dir()
    out: list[Path] = []
    for name in _QA_DECK_FILES:
        s = src / name
        if not s.exists():
            raise SchismDeckError(
                "SCHISM_INPUT_INVALID", f"QuarterAnnulus fixture missing: {name}"
            )
        d = dest_dir / name
        shutil.copy(s, d)
        out.append(d)
    return out


#: The Test_WWM_Duck deck files staged for the coupled_waves archetype (the
#: published V&V data under Data/ is NOT a worker deck file -- the composer reads
#: it server-side for the cross-shore chart).
_WWM_DUCK_DECK_FILES: tuple[str, ...] = (
    "hgrid.gr3", "hgrid.ll", "vgrid.in", "param.nml", "bctides.in",
    "wwminput.nml", "wwmbnd.gr3", "diffmax.gr3", "diffmin.gr3",
    "elev.ic", "elev.th", "rough.gr3", "u_prof.dat",
    "DUCK94_wave_spectra_8m_array.nc",
)

#: WWM output indices KEPT (all others zeroed at stage time -> a small scribe
#: count): sig. height (1) + discrete peak period (9) -- the postprocess targets.
_WWM_KEEP_IOF_WWM: frozenset[int] = frozenset({1, 9})
#: Hydro output indices KEPT: elevation (1) only.
_WWM_KEEP_IOF_HYDRO: frozenset[int] = frozenset({1})


def wwm_duck_fixture_dir() -> Path:
    """Resolve the repo's bundled Test_WWM_Duck fixture directory (ADR 0126/0129)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "services" / "workers" / "schism" / "fixtures" / "wwm_duck"
        if cand.is_dir():
            return cand
    raise SchismDeckError(
        "SCHISM_INPUT_INVALID",
        "could not locate the bundled Test_WWM_Duck fixture under services/workers/schism/fixtures",
    )


def _transform_wwm_param(param_text: str, *, sim_hours: float) -> str:
    """Apply the ADR 0126 1d staging transforms to the pristine Duck param.nml.

    Deterministic + line-oriented (a test asserts idempotence): strip the three
    master-only namelist vars the v5.11.0 binary does not declare
    (``nbins_veg_vert`` / ``nmarsh_types`` / ``RADFLAG``), substitute ``rnday`` from
    the requested window, and trim the output flags to elevation + Hs + Tp so a
    small scribe count runs on a modest core budget. ``itur=3`` is KEPT (faithful).
    """
    import re

    rnday = max(0.02, float(sim_hours) / 24.0)
    out_lines: list[str] = []
    for line in param_text.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        # 1. drop master-only vars (would abort the run: "Cannot match namelist object")
        if re.match(r"(nbins_veg_vert|nmarsh_types|radflag)\s*=", low):
            continue
        # 2. rnday substitution (preserve any trailing comment)
        m = re.match(r"(\s*rnday\s*=\s*)([0-9.eEdD+-]+)(.*)$", line)
        if m:
            out_lines.append(f"{m.group(1)}{rnday:.6f}{m.group(3)}")
            continue
        # 3. output trim: zero every iof_hydro/iof_wwm not in the keep sets
        mh = re.match(r"(\s*iof_hydro\((\d+)\)\s*=\s*)1(\b.*)$", line)
        if mh and int(mh.group(2)) not in _WWM_KEEP_IOF_HYDRO:
            out_lines.append(f"{mh.group(1)}0{mh.group(3)}")
            continue
        mw = re.match(r"(\s*iof_wwm\((\d+)\)\s*=\s*)1(\b.*)$", line)
        if mw and int(mw.group(2)) not in _WWM_KEEP_IOF_WWM:
            out_lines.append(f"{mw.group(1)}0{mw.group(3)}")
            continue
        out_lines.append(line)
    return "\n".join(out_lines) + ("\n" if param_text.endswith("\n") else "")


def stage_wwm_duck_deck(
    dest_dir: str | Path, *, sim_hours: float = 4.0
) -> tuple[list[Path], int, int]:
    """Stage the bundled Duck deck (transformed) into ``dest_dir`` for the coupled run.

    Copies the pristine fixture verbatim, then applies the in-code ADR 0126
    transforms (``_transform_wwm_param`` + the two file-name reconciliations GOTM/
    WWM hardcode) so the deck runs on ``pschism_WWM_GOTM_TVD-VL``. Returns
    ``(deck_files, ncompute, nscribe)`` -- 4 compute + 4 scribe (the proven spike
    layout; >= the 3 trimmed output variables).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    src = wwm_duck_fixture_dir()
    out: list[Path] = []
    for name in _WWM_DUCK_DECK_FILES:
        s = src / name
        if not s.exists():
            raise SchismDeckError("SCHISM_INPUT_INVALID", f"WWM_Duck fixture missing: {name}")
        d = dest_dir / name
        if name == "param.nml":
            d.write_text(
                _transform_wwm_param(s.read_text(encoding="utf-8"), sim_hours=sim_hours),
                encoding="utf-8",
            )
        else:
            shutil.copy(s, d)
        out.append(d)
    # GOTM's init_turbulence hardcodes 'gotmturb.nml'; WWM wants its own hgrid.
    gotm_nml = dest_dir / "gotmturb.nml"
    shutil.copy(src / "gotmturb.inp", gotm_nml)
    out.append(gotm_nml)
    hgrid_wwm = dest_dir / "hgrid_WWM.gr3"
    shutil.copy(src / "hgrid.gr3", hgrid_wwm)
    out.append(hgrid_wwm)
    return out, 4, 4


def load_gr3_bridge() -> Any:
    """Import the worker's ``schism_gr3`` module by file path (single source of truth).

    The bridge is pure numpy with no server/SCHISM imports (it flat-imports from
    the worker dir in the worker tests), so loading it here just needs its file
    path -- no duplication of ``tin_to_hgrid`` in the server tree.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "services" / "workers" / "schism" / "schism_gr3.py"
        if cand.exists():
            spec = importlib.util.spec_from_file_location("schism_gr3_bridge", cand)
            if spec is None or spec.loader is None:
                break
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise SchismDeckError(
        "SCHISM_MESH_INVALID",
        "could not load the schism_gr3 TIN->hgrid bridge from services/workers/schism",
    )


def sample_bathymetry_on_nodes(
    points: Any,
    dem_path: str | Path,
    *,
    min_wet_depth_m: float = 0.5,
) -> Any:
    """Sample a DEM/topobathy raster at each TIN node -> SCHISM depths (positive-down).

    ``points`` are (N,2) lon/lat (EPSG:4326). ``dem_path`` is a local
    DEM/topobathy COG (NAVD88-ish elevation, positive UP). SCHISM ``hgrid.gr3``
    depth is positive DOWN, so ``depth = -elevation``. Land nodes (elevation above
    the datum -> negative depth) are CLAMPED to ``min_wet_depth_m`` so a barotropic
    tidal SCREENING run keeps every node wet (a documented screening choice --
    surfaced in the template's synthetic_inputs). NaN samples (outside the raster)
    also clamp to the min depth. Returns an (N,) float array.
    """
    import numpy as np
    import rasterio

    pts = np.asarray(points, dtype=float)
    with rasterio.open(str(dem_path)) as ds:
        # rasterio.sample expects (lon, lat) in the raster CRS; our COGs are 4326.
        sampled = np.array(
            [v[0] for v in ds.sample([(float(x), float(y)) for x, y in pts[:, :2]])],
            dtype=float,
        )
        nodata = ds.nodata
    elev = sampled.astype(float)
    if nodata is not None:
        elev = np.where(elev == nodata, np.nan, elev)
    depth = -elev  # positive-down bathymetry
    depth = np.where(np.isfinite(depth), depth, min_wet_depth_m)
    depth = np.where(depth < min_wet_depth_m, min_wet_depth_m, depth)
    return depth


def _open_boundary_node_count(gr3_text: str) -> int:
    """Parse the open-boundary node count from an hgrid.gr3 string."""
    for line in gr3_text.splitlines():
        if "Total number of open boundary nodes" in line:
            try:
                return int(line.split("=")[0].strip().split()[0])
            except (ValueError, IndexError):
                return 0
    return 0


def _author_bctides(
    open_node_count: int,
    constituents: list[str],
    amplitude_m: float,
    *,
    phase_deg: float = 0.0,
) -> str:
    """Author a bctides.in for a spatially-uniform harmonic-elevation open boundary.

    iettype=3 (harmonic elevation from the listed constituents), ifltype=0 (no
    normal-velocity forcing -- a pure tidal elevation boundary). Every open node
    gets the same amplitude/phase (a screening tidal forcing; a per-node
    FES2014/TPXO field is the sign-off candidates' upgrade, ADR 0115 4a). Mirrors
    the QA fixture's block layout.
    """
    if open_node_count <= 0:
        raise SchismDeckError(
            "SCHISM_MESH_INVALID",
            "the coastal TIN has no open-boundary nodes; cannot force a tidal boundary "
            "(check open_boundary_side)",
        )
    lines: list[str] = []
    lines.append("01/01/2000 00:00:00 PST")
    lines.append("0 40. ntip")  # earth tidal potential OFF
    lines.append(f"{len(constituents)}  nbfr")
    for c in constituents:
        amig = CONSTITUENT_ANGULAR_FREQ_RAD_S[c]
        lines.append(c)
        lines.append(f"{amig:.15f} 1.0 0.0")  # amig, nodal factor ff, nodal arg face
    lines.append("1 nope")
    # nnodes iettype ifltype itetype isatype ; 3=harmonic elev, 0=no vel forcing
    lines.append(f"{open_node_count} 3 0 0 0")
    for c in constituents:
        lines.append(f"  {c} !elevation")
        for _ in range(open_node_count):
            lines.append(f"  {amplitude_m:.6f}  {phase_deg:.2f}")
    return "\n".join(lines) + "\n"


def _author_station_in(lon_c: float, lat_c: float) -> str:
    """One elevation station at the mesh centroid (the timeseries-chart point)."""
    return (
        "1 0 0 0 0 0 0 0 0 !on/off: elev,air_pressure,windx,windy,T,S,u,v,w\n"
        "1\n"
        f"1 {lon_c:.6f} {lat_c:.6f} 0.\n"
    )


def _substitute_param_nml(qa_param_text: str, *, sim_days: float, dt_s: float) -> str:
    """Reuse the proven QA param.nml, substituting the coastal run knobs.

    Substitutes rnday (sim length), dt (time step), and ihfskip (stack spool) so
    the whole run lands in ONE output stack (out2d_1.nc). nspool (map cadence) and
    nspool_sta (station cadence) are set to ~hourly. Everything else (barotropic
    ibc=1, nchi=0, nvrt=2 via vgrid, iof_hydro(1)/(16) elevation+vel output,
    iout_sta=1) is inherited verbatim from the green fixture.
    """
    import re

    nsteps = int(math.ceil(sim_days * 86400.0 / dt_s))
    ihfskip = nsteps  # one stack for the whole run
    hourly = max(1, int(round(3600.0 / dt_s)))  # ~1 output/hour

    text = qa_param_text
    text = re.sub(r"(?m)^(\s*rnday\s*=\s*)\S+", rf"\g<1>{sim_days:g}", text, count=1)
    text = re.sub(r"(?m)^(\s*dt\s*=\s*)\S+", rf"\g<1>{dt_s:g}.", text, count=1)
    text = re.sub(r"(?m)^(\s*ihfskip\s*=\s*)\S+", rf"\g<1>{ihfskip}", text, count=1)
    text = re.sub(r"(?m)^(\s*nspool\s*=\s*)\S+", rf"\g<1>{hourly}", text, count=1)
    text = re.sub(r"(?m)^(\s*nspool_sta\s*=\s*)\S+", rf"\g<1>{hourly}", text, count=1)
    return text


def author_coastal_tin_deck(
    dest_dir: str | Path,
    *,
    points: Any,
    cells: Any,
    depths: Any,
    constituents: list[str],
    tidal_amplitude_m: float,
    sim_days: float,
    open_boundary_side: str,
    dt_s: float = 120.0,
    coastal_drag_cd: float = 0.0025,
) -> dict[str, Any]:
    """Author a full coastal_tin SCHISM deck into ``dest_dir``.

    Returns ``{"files": [Path, ...], "n_nodes": int, "n_elements": int,
    "open_node_count": int, "centroid": (lon, lat)}``. Raises SchismDeckError on a
    mesh/boundary fault (the honest-failure surface).
    """
    import numpy as np

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    pts = np.asarray(points, dtype=float)
    tris = np.asarray(cells, dtype=np.int64)
    depth_arr = np.asarray(depths, dtype=float)

    bridge = load_gr3_bridge()
    try:
        gr3_text = bridge.tin_to_hgrid(
            pts,
            tris,
            depth=depth_arr,
            grid_name="trid3nt_coastal_tin",
            open_boundary_side=open_boundary_side,
            clean_boundary=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise SchismDeckError(
            "SCHISM_MESH_INVALID", f"tin_to_hgrid failed: {exc}"
        ) from exc

    open_node_count = _open_boundary_node_count(gr3_text)
    # Re-parse node count from the header (the bridge may have re-indexed after
    # pinch-cleaning) for the honest n_nodes/n_elements.
    header = gr3_text.splitlines()[1].split()
    n_elem, n_nodes = int(header[0]), int(header[1])

    lon_c = float(pts[:, 0].mean())
    lat_c = float(pts[:, 1].mean())

    (dest_dir / "hgrid.gr3").write_text(gr3_text, encoding="utf-8")

    # vgrid.in: reuse the QA 2D barotropic vgrid (ivcor=2, nvrt=2).
    qa = quarterannulus_fixture_dir()
    shutil.copy(qa / "vgrid.in", dest_dir / "vgrid.in")

    # param.nml: QA template with coastal knobs substituted.
    param_text = _substitute_param_nml(
        (qa / "param.nml").read_text(encoding="utf-8"), sim_days=sim_days, dt_s=dt_s
    )
    (dest_dir / "param.nml").write_text(param_text, encoding="utf-8")

    # bctides.in: analytical harmonic-elevation boundary.
    bctides_text = _author_bctides(open_node_count, constituents, tidal_amplitude_m)
    (dest_dir / "bctides.in").write_text(bctides_text, encoding="utf-8")

    # drag.gr3: uniform coastal Cd (nchi=0 convention: value = drag coefficient).
    drag_lines = ["0", f"{n_elem} {n_nodes}"]
    for i in range(n_nodes):
        drag_lines.append(f"{i + 1} {pts[i, 0]:.9f} {pts[i, 1]:.9f} {coastal_drag_cd:.7e}")
    (dest_dir / "drag.gr3").write_text("\n".join(drag_lines) + "\n", encoding="utf-8")

    # station.in: elevation station at the mesh centroid.
    (dest_dir / "station.in").write_text(_author_station_in(lon_c, lat_c), encoding="utf-8")

    files = [
        dest_dir / n
        for n in ("hgrid.gr3", "vgrid.in", "param.nml", "bctides.in", "drag.gr3", "station.in")
    ]
    return {
        "files": files,
        "n_nodes": n_nodes,
        "n_elements": n_elem,
        "open_node_count": open_node_count,
        "centroid": (lon_c, lat_c),
    }
