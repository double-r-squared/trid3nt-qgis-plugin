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
    "stage_transport_scheme_deck",
    "wwm_duck_fixture_dir",
    "stage_wwm_duck_deck",
    "load_gr3_bridge",
    "sample_bathymetry_on_nodes",
    "author_coastal_tin_deck",
    "author_baroclinic_estuary_deck",
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


#: JONSWAP spectral shape selector for WWM's parametric boundary (WBSS=2 -> the
#: Jonswap peak-enhanced spectrum; the sign of WBSS decides mean(-)/peak(+) period,
#: we prescribe a PEAK period so WBSS is positive).
_WWM_JONSWAP_WBSS: int = 2


def _transform_wwm_input_parametric(
    wwm_text: str,
    *,
    hs_m: float,
    tp_s: float,
    dir_deg: float,
    spread_deg: float,
) -> str:
    """Rewrite the Duck wwminput.nml &BOUC block to a PARAMETRIC JONSWAP boundary.

    The bundled Duck case forces WWM from a non-parametric observed spectrum
    (``LBCSP=T``, ``IBOUNDFORMAT=6``, the ``DUCK94_wave_spectra_8m_array.nc`` file).
    This switches the boundary to a PRESCRIBED parametric JONSWAP spectrum uniform
    along the offshore boundary (``LBCWA=T``, ``LBCSP=F``, ``LINHOM=F``,
    ``IBOUNDFORMAT=1``, steady in time ``LBCSE=F``) driven by the four physical
    knobs -- the offshore Hs (``WBHS``), the peak period (``WBTP`` with ``WBSS=2``
    JONSWAP), the mean wave direction (``WBDM``, nautical degrees), and the
    directional spread in degrees (``WBDS`` with ``WBDSMS=1``). Deterministic +
    line-oriented (a test asserts the toggles + values land). Only the &BOUC forcing
    lines change; the spectral discretisation (MSC/MDC) and physics stay the
    published values.
    """
    import re

    # (key, new-value, trailing-comment-preserving) substitutions on the &BOUC lines.
    scalar_subs: dict[str, str] = {
        "LBCSE": "F",  # steady parametric boundary (no time interpolation needed)
        "LBINTER": "F",
        "LBCWA": "T",  # parametric wave spectra ON
        "LBCSP": "F",  # non-parametric (file) spectra OFF
        "LINHOM": "F",  # spatially uniform along the boundary
        "IBOUNDFORMAT": "1",  # native WWM parametric
        "WBSS": str(_WWM_JONSWAP_WBSS),  # JONSWAP, peak period (positive sign)
        "WBHS": f"{float(hs_m):.4f}",
        "WBTP": f"{float(tp_s):.4f}",
        "WBDM": f"{float(dir_deg):.4f}",
        "WBDSMS": "1",  # spread specified in degrees
        "WBDS": f"{float(spread_deg):.4f}",
    }
    out_lines: list[str] = []
    for line in wwm_text.splitlines():
        m = re.match(r"(\s*)([A-Za-z_]\w*)(\s*=\s*)(\S+)(.*)$", line)
        if m and m.group(2).upper() in scalar_subs:
            key = m.group(2)
            val = scalar_subs[m.group(2).upper()]
            out_lines.append(f"{m.group(1)}{key}{m.group(3)}{val}{m.group(5)}")
            continue
        out_lines.append(line)
    return "\n".join(out_lines) + ("\n" if wwm_text.endswith("\n") else "")


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
    dest_dir: str | Path,
    *,
    sim_hours: float = 4.0,
    wave_forcing: dict[str, float] | None = None,
) -> tuple[list[Path], int, int]:
    """Stage the bundled Duck deck (transformed) into ``dest_dir`` for the coupled run.

    Copies the pristine fixture verbatim, then applies the in-code ADR 0126
    transforms (``_transform_wwm_param`` + the two file-name reconciliations GOTM/
    WWM hardcode) so the deck runs on ``pschism_WWM_GOTM_TVD-VL``. Returns
    ``(deck_files, ncompute, nscribe)`` -- 4 compute + 4 scribe (the proven spike
    layout; >= the 3 trimmed output variables).

    ``wave_forcing`` (ADR 0189, the parametric-spectrum row) switches the WWM open
    boundary from the bundled non-parametric observed spectrum to a PRESCRIBED
    parametric JONSWAP spectrum. When provided it must carry
    ``{hs_m, tp_s, dir_deg, spread_deg}`` -- the four offshore-spectrum knobs -- and
    ``wwminput.nml`` is rewritten (``_transform_wwm_input_parametric``); the bundled
    ``DUCK94_wave_spectra_8m_array.nc`` is still staged but goes unread
    (``LBCSP=F``). ``None`` keeps the published observed-spectrum forcing (the
    validation case).
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
        elif name == "wwminput.nml" and wave_forcing:
            d.write_text(
                _transform_wwm_input_parametric(
                    s.read_text(encoding="utf-8"),
                    hs_m=float(wave_forcing["hs_m"]),
                    tp_s=float(wave_forcing["tp_s"]),
                    dir_deg=float(wave_forcing["dir_deg"]),
                    spread_deg=float(wave_forcing["spread_deg"]),
                ),
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


#: The transport-validation deck reuses the QA mesh + boundary but adds a live
#: baroclinic tracer (temperature) so the transport solver actually runs. The
#: hydro-core binary freezes T/S in barotropic mode (ibc=1), so the front needs
#: ibc=0 + a 3D vgrid; the scheme is toggled by tvd.prop alone (identical flow).
_TRANSPORT_VGRID_3D: str = (
    "2 !ivcor\n5 1 1.e6 !nvrt, kz, h_s\nZ levels\n1 -1.e6\nS levels\n"
    "40. 1. 1.e-4 !h_c, theta_b, theta_f\n"
    "1 -1.\n2 -0.75\n3 -0.5\n4 -0.25\n5 0.\n"
)
#: SCHISM output vars the transport deck scribes (elevation, depth-avg vel,
#: surface T, T@prism, salinity) -> nscribe must be >= this count.
TRANSPORT_SCHEME_NSCRIBE: int = 6
TRANSPORT_SCHEME_NLAYERS: int = 5


def _read_gr3_nodes(gr3_text: str) -> tuple[int, int, list[tuple[float, float]]]:
    """Parse ``(n_elem, n_node, [(x, y), ...])`` from an hgrid.gr3 string."""
    lines = gr3_text.splitlines()
    n_elem, n_node = (int(v) for v in lines[1].split()[:2])
    nodes = [
        (float(lines[2 + i].split()[1]), float(lines[2 + i].split()[2]))
        for i in range(n_node)
    ]
    return n_elem, n_node, nodes


def _patch_transport_param(
    qa_param_text: str, *, sim_days: float, dt_s: float
) -> str:
    """Reuse the QA param.nml, switching to a baroclinic tracer-transport run.

    ibc=0 (baroclinic -> temperature is a LIVE transported tracer, not frozen),
    itr_met=3 (horizontal TVD; the per-element tvd.prop toggles TVD vs upwind),
    h_tvd=5 (TVD active where flagged), temperature + salinity initialized from
    the staged temp.ic / salt.ic (flag_ic(1:2)=1), and elevation + T outputs on.
    One output stack (ihfskip = nsteps). No forcing legs beyond the baked M2
    boundary that drives the advecting current.
    """
    import re

    nsteps = int(math.ceil(sim_days * 86400.0 / dt_s))
    hourly = max(1, int(round(3600.0 / dt_s)))

    def sub(pat: str, repl: str, t: str) -> str:
        return re.sub(pat, repl, t, count=1, flags=re.M)

    t = qa_param_text
    t = sub(r"^(\s*rnday\s*=\s*)\S+", rf"\g<1>{sim_days:g}", t)
    t = sub(r"^(\s*dt\s*=\s*)\S+", rf"\g<1>{dt_s:g}.", t)
    t = sub(r"^(\s*ibc\s*=\s*)\S+", r"\g<1>0", t)  # baroclinic: live tracer
    t = sub(r"^(\s*dramp\s*=\s*)\S+", r"\g<1>0.1", t)
    t = sub(r"^(\s*drampbc\s*=\s*)\S+", r"\g<1>0.", t)
    t = sub(r"^(\s*itr_met\s*=\s*)\S+", r"\g<1>3", t)
    t = sub(r"^(\s*h_tvd\s*=\s*)\S+", r"\g<1>5", t)
    t = sub(r"^(\s*ihfskip\s*=\s*)\S+", rf"\g<1>{nsteps}", t)
    t = sub(r"^(\s*nspool\s*=\s*)\S+", rf"\g<1>{hourly}", t)
    t = sub(r"^(\s*nspool_sta\s*=\s*)\S+", rf"\g<1>{hourly}", t)
    t = sub(r"^(\s*flag_ic\(1\)\s*=\s*)\S+", r"\g<1>1", t)
    t = sub(r"^(\s*flag_ic\(2\)\s*=\s*)\S+", r"\g<1>1", t)
    t = sub(r"^(\s*iof_hydro\(18\)\s*=\s*)\S+", r"\g<1>1", t)  # surface T
    t = sub(r"^(\s*iof_hydro\(29\)\s*=\s*)\S+", r"\g<1>1", t)  # T @ prism
    t = sub(r"^(\s*iof_hydro\(19\)\s*=\s*)\S+", r"\g<1>1", t)  # salinity
    return t


def stage_transport_scheme_deck(
    dest_dir: str | Path,
    *,
    scheme: str,
    sim_days: float = 2.0,
    dt_s: float = 300.0,
    t_hot: float = 20.0,
    t_cold: float = 10.0,
) -> dict[str, Any]:
    """Author a heat-front transport deck on the QA mesh for ONE transport scheme.

    Stages the QuarterAnnulus mesh + M2 boundary + drag + station verbatim, adds a
    3D pure-sigma vgrid (so the baroclinic transport solver runs), a temperature
    FRONT initial condition (temp.ic: ``t_hot`` where x < x_mid, ``t_cold``
    otherwise), a uniform salt.ic, and a per-element ``tvd.prop`` that toggles the
    scheme: ``scheme="tvd"`` -> all 1 (TVD^2 limiter active), ``scheme="upwind"``
    -> all 0 (first-order upwind everywhere). Both runs share the identical mesh /
    boundary / flow, so the scheme is the ONLY difference. Returns
    ``{"files": [...], "n_nodes", "n_elements", "n_layers", "x_mid", "t_hot",
    "t_cold", "nscribe"}``.
    """
    from trid3nt_contracts.schism_contracts import SCHISM_TRANSPORT_SCHEMES

    if scheme not in SCHISM_TRANSPORT_SCHEMES:
        raise SchismDeckError(
            "SCHISM_INPUT_INVALID",
            f"scheme must be one of {SCHISM_TRANSPORT_SCHEMES}, got {scheme!r}",
        )
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    qa = quarterannulus_fixture_dir()

    for name in ("hgrid.gr3", "bctides.in", "drag.gr3", "station.in"):
        shutil.copy(qa / name, dest_dir / name)

    gr3_text = (qa / "hgrid.gr3").read_text(encoding="utf-8")
    n_elem, n_node, nodes = _read_gr3_nodes(gr3_text)
    xs = [x for x, _ in nodes]
    x_mid = 0.5 * (min(xs) + max(xs))

    (dest_dir / "vgrid.in").write_text(_TRANSPORT_VGRID_3D, encoding="utf-8")

    tvd_flag = 1 if scheme == "tvd" else 0
    (dest_dir / "tvd.prop").write_text(
        "".join(f"{e + 1} {tvd_flag}\n" for e in range(n_elem)), encoding="utf-8"
    )

    temp_lines = [f"temp front IC ({scheme})", f"{n_elem} {n_node}"]
    salt_lines = [f"salt IC ({scheme}, uniform)", f"{n_elem} {n_node}"]
    for i, (x, y) in enumerate(nodes):
        tval = t_hot if x < x_mid else t_cold
        temp_lines.append(f"{i + 1} {x:.6f} {y:.6f} {tval:.4f}")
        salt_lines.append(f"{i + 1} {x:.6f} {y:.6f} 0.0000")
    (dest_dir / "temp.ic").write_text("\n".join(temp_lines) + "\n", encoding="utf-8")
    (dest_dir / "salt.ic").write_text("\n".join(salt_lines) + "\n", encoding="utf-8")

    param_text = _patch_transport_param(
        (qa / "param.nml").read_text(encoding="utf-8"), sim_days=sim_days, dt_s=dt_s
    )
    (dest_dir / "param.nml").write_text(param_text, encoding="utf-8")

    files = [
        dest_dir / n
        for n in (
            "hgrid.gr3", "vgrid.in", "param.nml", "bctides.in", "drag.gr3",
            "station.in", "temp.ic", "salt.ic", "tvd.prop",
        )
    ]
    return {
        "files": files, "n_nodes": n_node, "n_elements": n_elem,
        "n_layers": TRANSPORT_SCHEME_NLAYERS, "x_mid": x_mid,
        "t_hot": t_hot, "t_cold": t_cold, "nscribe": TRANSPORT_SCHEME_NSCRIBE,
    }


# --------------------------------------------------------------------------- #
# baroclinic_circulation archetype (ADR 0189): density-driven 3D estuary.
# --------------------------------------------------------------------------- #
#: SCHISM output vars the baroclinic estuary deck scribes: elevation (2D) +
#: salinity + temperature + depth-avg velocity (3D) -> nscribe must be >= this.
BAROCLINIC_NSCRIBE: int = 5
#: The default vertical-grid layer count for the coarse baroclinic smoke (pure-S
#: SZ grid; enough layers to resolve a two-layer estuarine stratification).
BAROCLINIC_NVRT: int = 10


def _author_sz_vgrid(nvrt: int, *, theta_b: float = 1.0, theta_f: float = 4.0,
                     h_c: float = 5.0) -> str:
    """Author a pure-S (SZ with one Z level) vgrid.in with ``nvrt`` sigma layers.

    ``ivcor=2`` (SZ), one degenerate Z level at the bottom, ``nvrt`` S levels from
    the surface (sigma 0) to the bed (sigma -1). ``theta_f`` > 0 stretches
    resolution toward the surface + bed (the estuarine pycnocline). This is the
    3D vertical discretisation the baroclinic solver needs (the barotropic tidal
    deck runs a 2-level vgrid; a density-driven run must resolve the water column).
    """
    if nvrt < 3:
        raise SchismDeckError("SCHISM_INPUT_INVALID", "baroclinic nvrt must be >= 3")
    lines = [
        "2 !ivcor",
        f"{nvrt} 1 1.e6 !nvrt, kz, h_s",
        "Z levels",
        "1 -1.e6",
        "S levels",
        f"{h_c:g}. {theta_b:g} {theta_f:g} !h_c, theta_b, theta_f",
    ]
    for k in range(nvrt):
        sigma = -1.0 + k / (nvrt - 1)  # -1 (bed) .. 0 (surface)
        lines.append(f"{k + 1} {sigma:.6f}")
    return "\n".join(lines) + "\n"


def _patch_baroclinic_param(
    qa_param_text: str, *, sim_days: float, dt_s: float,
) -> str:
    """Reuse the QA param.nml for a density-driven 3D BAROCLINIC estuary run.

    ibc=0 (baroclinic -> the T/S density field drives the flow), flag_ic(1:2)=1
    (T/S read from temp.ic/salt.ic), if_source=1 (the river freshwater point
    source), itr_met=3 + h_tvd=5 (TVD tracer transport), and the salinity +
    temperature + depth-avg velocity outputs on so the stratification is scribed.
    dramp/drampbc ramp the tidal + baroclinic forcing. One output stack.
    """
    import re

    nsteps = int(math.ceil(sim_days * 86400.0 / dt_s))
    hourly = max(1, int(round(3600.0 / dt_s)))

    def sub(pat: str, repl: str, t: str) -> str:
        return re.sub(pat, repl, t, count=1, flags=re.M)

    t = qa_param_text
    # ics=2 (lat/lon spherical): the estuary mesh is in geographic degrees, so
    # SCHISM must compute great-circle distances -- with the QA fixture's ics=1
    # (Cartesian) it would read degrees AS metres (a ~0.5 m domain) and the tracer
    # backtracking overflows (nbtrk > mxnbt).
    t = sub(r"^(\s*ics\s*=\s*)\S+", r"\g<1>2", t)
    t = sub(r"^(\s*rnday\s*=\s*)\S+", rf"\g<1>{sim_days:g}", t)
    t = sub(r"^(\s*dt\s*=\s*)\S+", rf"\g<1>{dt_s:g}.", t)
    t = sub(r"^(\s*ibc\s*=\s*)\S+", r"\g<1>0", t)       # baroclinic
    t = sub(r"^(\s*ibtp\s*=\s*)\S+", r"\g<1>0", t)
    t = sub(r"^(\s*dramp\s*=\s*)\S+", r"\g<1>0.5", t)
    t = sub(r"^(\s*drampbc\s*=\s*)\S+", r"\g<1>0.5", t)
    t = sub(r"^(\s*itr_met\s*=\s*)\S+", r"\g<1>3", t)
    t = sub(r"^(\s*h_tvd\s*=\s*)\S+", r"\g<1>5", t)
    t = sub(r"^(\s*if_source\s*=\s*)\S+", r"\g<1>1", t)  # river freshwater source
    t = sub(r"^(\s*dramp_ss\s*=\s*)\S+", r"\g<1>0.5", t)
    t = sub(r"^(\s*ihfskip\s*=\s*)\S+", rf"\g<1>{nsteps}", t)
    t = sub(r"^(\s*nspool\s*=\s*)\S+", rf"\g<1>{hourly}", t)
    t = sub(r"^(\s*nspool_sta\s*=\s*)\S+", rf"\g<1>{hourly}", t)
    t = sub(r"^(\s*flag_ic\(1\)\s*=\s*)\S+", r"\g<1>1", t)
    t = sub(r"^(\s*flag_ic\(2\)\s*=\s*)\S+", r"\g<1>1", t)
    t = sub(r"^(\s*iof_hydro\(18\)\s*=\s*)\S+", r"\g<1>1", t)  # temperature
    t = sub(r"^(\s*iof_hydro\(19\)\s*=\s*)\S+", r"\g<1>1", t)  # salinity
    return t


#: Minimum wet lattice nodes required to clip to a shoreline-following mesh; below
#: this the water mask covered too little of the AOI to build a domain, so the deck
#: author falls back to the full rectangle (loudly noted) rather than a sliver.
_MIN_CLIP_NODES: int = 40


def _largest_connected_component(pts: Any, tris: Any) -> Any:
    """Return the subset of ``tris`` in the largest node-connected component.

    A centroid-clipped Delaunay can leave small water pockets disconnected from
    the main estuary body; SCHISM's open-boundary extraction wants one connected
    domain. Union-find over triangle edges, keep the component with the most
    triangles.
    """
    import numpy as np

    n = int(pts.shape[0])
    parent = np.arange(n)

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for tri in tris:
        r = find(int(tri[0]))
        for k in (1, 2):
            parent[find(int(tri[k]))] = r
    roots = np.array([find(int(t[0])) for t in tris])
    if roots.size == 0:
        return tris
    labels, counts = np.unique(roots, return_counts=True)
    keep_root = labels[int(np.argmax(counts))]
    return tris[roots == keep_root]


def _build_estuary_mesh(
    bbox: tuple[float, float, float, float],
    *,
    nx: int,
    ny: int,
    ocean_side: str,
    depth_ocean_m: float,
    depth_river_m: float,
    water_mask_fn: Any = None,
) -> tuple[Any, Any, Any, bool]:
    """Build a coarse triangulated estuary channel over ``bbox`` (lon/lat).

    Returns ``(points(N,2), tris(M,3), depths(N,), clipped)``. A regular ``nx x ny``
    lon/lat lattice; bathymetry ramps linearly from ``depth_river_m`` at the
    landward edge to ``depth_ocean_m`` at ``ocean_side`` (positive-down SCHISM
    depth) -- an IDEALIZED coarse demonstration BATHYMETRY (the honesty floor).

    When ``water_mask_fn(lon_arr, lat_arr) -> bool_arr`` is supplied the lattice is
    CLIPPED TO WATER: land nodes are dropped, the wet nodes are Delaunay-meshed,
    triangles whose centroid is land are removed (so the mesh follows the real
    SHORELINE instead of painting the rectangle over land), and only the largest
    connected water body is kept. ``clipped`` reports whether a shoreline mesh was
    built (False -> the mask covered too little water and the full rectangle was
    kept, a loud caller-side note). ``water_mask_fn`` is INJECTED (a real coastline
    / DEM-sign classifier live; a synthetic one in the offline tests), so this
    function stays pure + network-free.
    """
    import numpy as np
    from scipy.spatial import Delaunay

    lon0, lat0, lon1, lat1 = bbox
    xs = np.linspace(lon0, lon1, nx)
    ys = np.linspace(lat0, lat1, ny)
    gx, gy = np.meshgrid(xs, ys)
    pts = np.column_stack([gx.ravel(), gy.ravel()])

    tris = Delaunay(pts).simplices
    clipped = False
    if water_mask_fn is not None:
        wet = np.asarray(water_mask_fn(pts[:, 0], pts[:, 1]), dtype=bool)
        if int(wet.sum()) >= _MIN_CLIP_NODES:
            # Keep the STRUCTURED lattice triangulation (clean 2-manifold topology)
            # and drop only cells whose centroid is land -- a staircase shoreline
            # boundary that tin_to_hgrid can walk, unlike a re-Delaunay of the water
            # subset (which bridges concavities with slivers and breaks the boundary
            # extraction). Then keep the largest connected water body + re-index.
            cent = pts[tris].mean(axis=1)
            water_cell = np.asarray(
                water_mask_fn(cent[:, 0], cent[:, 1]), dtype=bool)
            wtris = tris[water_cell]
            wtris = _largest_connected_component(pts, wtris)
            used = np.unique(wtris)
            if used.size >= _MIN_CLIP_NODES and wtris.shape[0] > 0:
                remap = np.full(pts.shape[0], -1, dtype=np.int64)
                remap[used] = np.arange(used.size)
                pts = pts[used]
                tris = remap[wtris]
                clipped = True

    # normalized 0(landward/river) .. 1(ocean) coordinate along the forcing axis
    if ocean_side in ("south", "north"):
        frac = (pts[:, 1] - lat0) / (lat1 - lat0)  # 0 at south .. 1 at north
        if ocean_side == "south":
            frac = 1.0 - frac
    else:  # east / west
        frac = (pts[:, 0] - lon0) / (lon1 - lon0)  # 0 at west .. 1 at east
        if ocean_side == "west":
            frac = 1.0 - frac
    depths = depth_river_m + (depth_ocean_m - depth_river_m) * frac
    return pts, tris, depths.astype(float), clipped


def _river_source_element(hgrid_text: str, ocean_side: str) -> int:
    """Pick the 1-based element id nearest the RIVER (landward) edge of the mesh.

    Parses the written hgrid.gr3 element table (so the id matches SCHISM's own
    numbering after any bridge re-index) and returns the element whose centroid is
    farthest from the ocean boundary along the forcing axis -- the freshwater point
    source location.
    """
    lines = hgrid_text.splitlines()
    n_elem, n_node = (int(v) for v in lines[1].split()[:2])
    xy = {}
    for i in range(n_node):
        p = lines[2 + i].split()
        xy[int(p[0])] = (float(p[1]), float(p[2]))
    ebase = 2 + n_node
    best_e, best_key = 1, -1.0e30
    xs = [c[0] for c in xy.values()]
    ys = [c[1] for c in xy.values()]
    lon0, lon1 = min(xs), max(xs)
    lat0, lat1 = min(ys), max(ys)
    for e in range(n_elem):
        p = lines[ebase + e].split()
        eid = int(p[0])
        nn = [int(p[2]), int(p[3]), int(p[4])]
        cx = sum(xy[k][0] for k in nn) / 3.0
        cy = sum(xy[k][1] for k in nn) / 3.0
        if ocean_side == "south":
            key = cy  # farthest north = river
        elif ocean_side == "north":
            key = -cy
        elif ocean_side == "west":
            key = cx
        else:  # east
            key = -cx
        if key > best_key:
            best_key, best_e = key, eid
    return best_e


def author_baroclinic_estuary_deck(
    dest_dir: str | Path,
    *,
    bbox: tuple[float, float, float, float],
    constituents: list[str],
    tidal_amplitude_m: float,
    sim_days: float,
    ocean_side: str = "south",
    river_discharge_m3s: float = 500.0,
    ocean_salinity_psu: float = 33.0,
    river_salinity_psu: float = 0.0,
    river_temp_c: float = 14.0,
    ocean_temp_c: float = 14.0,
    nvrt: int = BAROCLINIC_NVRT,
    nx: int = 20,
    ny: int = 40,
    dt_s: float = 120.0,
    coastal_drag_cd: float = 0.0025,
    water_mask_fn: Any = None,
    supplied_mesh: tuple[Any, Any, Any] | None = None,
) -> dict[str, Any]:
    """Author a coarse density-driven 3D BAROCLINIC estuary deck into ``dest_dir``.

    A coarse structured channel over ``bbox`` (a real US estuary footprint), a 3D
    pure-S vgrid (``nvrt`` layers), ibc=0 baroclinic physics, an initial salinity
    GRADIENT (fresh at the landward/river edge -> ``ocean_salinity_psu`` at the
    ocean edge), a sustained freshwater river point SOURCE at the landward edge
    (``river_discharge_m3s`` at S=0), and a tidal-elevation ocean open boundary --
    so the density gradient drives a gravitational (estuarine) circulation and the
    water column stratifies. Returns ``{"files": [...], "n_nodes", "n_elements",
    "n_layers", "open_node_count", "source_elem", "bbox", "centroid"}``. The mesh +
    bathymetry are an IDEALIZED coarse demonstration geometry (surfaced in the
    template's synthetic_inputs), NOT a surveyed estuary -- the calibrated
    Columbia-River CORIE 28-day 3D run is the NATE-gated validation.

    ``supplied_mesh`` (ADR 0208 precondition gate): when given as
    ``(points_lonlat (N,2), tris (M,3) 0-based, depths_positive_down (N,))`` -- a
    user mesh accepted by the precondition gate -- it REPLACES the idealized lattice
    (real shoreline + real sampled bathymetry). The salinity IC gradient, river
    source, and tidal open boundary are still authored keyed to ``ocean_side`` (the
    forcing remains idealized; only the geometry is real).
    """
    import numpy as np

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if supplied_mesh is not None:
        # A user mesh accepted by the precondition gate: real shoreline + real
        # bathymetry replace the idealized lattice. The forcing (salinity IC
        # gradient, river source, tidal boundary) is still authored below keyed to
        # ocean_side -- only the domain geometry is real.
        pts = np.asarray(supplied_mesh[0], dtype=float)
        tris = np.asarray(supplied_mesh[1], dtype=np.int64)
        depths = np.asarray(supplied_mesh[2], dtype=float)
        clipped = True
        bbox = (float(pts[:, 0].min()), float(pts[:, 1].min()),
                float(pts[:, 0].max()), float(pts[:, 1].max()))
    else:
        pts, tris, depths, clipped = _build_estuary_mesh(
            bbox, nx=nx, ny=ny, ocean_side=ocean_side,
            depth_ocean_m=15.0, depth_river_m=3.0, water_mask_fn=water_mask_fn,
        )
        if water_mask_fn is not None and not clipped:
            logger.warning(
                "baroclinic estuary: water mask covered too little of the AOI to clip "
                "to a shoreline mesh -- meshing the full rectangle (salinity may render "
                "over land). Supply a wetter AOI / a coastline mask."
            )
    bridge = load_gr3_bridge()
    try:
        gr3_text = bridge.tin_to_hgrid(
            pts, tris, depth=depths, grid_name="trid3nt_baroclinic_estuary",
            open_boundary_side=ocean_side, clean_boundary=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise SchismDeckError("SCHISM_MESH_INVALID", f"tin_to_hgrid failed: {exc}") from exc

    (dest_dir / "hgrid.gr3").write_text(gr3_text, encoding="utf-8")
    open_node_count = _open_boundary_node_count(gr3_text)
    header = gr3_text.splitlines()[1].split()
    n_elem, n_nodes = int(header[0]), int(header[1])

    # Re-parse the FINAL node coords (the bridge may re-index) for the IC gradient.
    glines = gr3_text.splitlines()
    node_xy = np.array(
        [[float(glines[2 + i].split()[1]), float(glines[2 + i].split()[2])]
         for i in range(n_nodes)], dtype=float,
    )
    lon0, lat0, lon1, lat1 = bbox
    if ocean_side in ("south", "north"):
        frac = (node_xy[:, 1] - lat0) / (lat1 - lat0)
        if ocean_side == "south":
            frac = 1.0 - frac
    else:
        frac = (node_xy[:, 0] - lon0) / (lon1 - lon0)
        if ocean_side == "west":
            frac = 1.0 - frac
    frac = np.clip(frac, 0.0, 1.0)
    salt_ic = river_salinity_psu + (ocean_salinity_psu - river_salinity_psu) * frac
    temp_ic = np.full(n_nodes, 0.5 * (river_temp_c + ocean_temp_c))

    # vgrid.in: 3D pure-S SZ grid.
    (dest_dir / "vgrid.in").write_text(_author_sz_vgrid(nvrt), encoding="utf-8")

    # param.nml: QA template patched to baroclinic + source.
    qa = quarterannulus_fixture_dir()
    (dest_dir / "param.nml").write_text(
        _patch_baroclinic_param(
            (qa / "param.nml").read_text(encoding="utf-8"), sim_days=sim_days, dt_s=dt_s,
        ),
        encoding="utf-8",
    )

    # bctides.in: tidal-elevation ocean boundary (zero-gradient T/S).
    (dest_dir / "bctides.in").write_text(
        _author_bctides(open_node_count, constituents, tidal_amplitude_m),
        encoding="utf-8",
    )

    # drag.gr3: uniform coastal Cd.
    drag_lines = ["0", f"{n_elem} {n_nodes}"]
    for i in range(n_nodes):
        drag_lines.append(
            f"{i + 1} {node_xy[i, 0]:.9f} {node_xy[i, 1]:.9f} {coastal_drag_cd:.7e}"
        )
    (dest_dir / "drag.gr3").write_text("\n".join(drag_lines) + "\n", encoding="utf-8")

    # temp.ic / salt.ic: horizontally-varying (the estuarine salinity gradient).
    temp_lines = ["temp IC (baroclinic estuary)", f"{n_elem} {n_nodes}"]
    salt_lines = ["salt IC (baroclinic estuary gradient)", f"{n_elem} {n_nodes}"]
    for i in range(n_nodes):
        temp_lines.append(
            f"{i + 1} {node_xy[i, 0]:.6f} {node_xy[i, 1]:.6f} {temp_ic[i]:.4f}"
        )
        salt_lines.append(
            f"{i + 1} {node_xy[i, 0]:.6f} {node_xy[i, 1]:.6f} {salt_ic[i]:.4f}"
        )
    (dest_dir / "temp.ic").write_text("\n".join(temp_lines) + "\n", encoding="utf-8")
    (dest_dir / "salt.ic").write_text("\n".join(salt_lines) + "\n", encoding="utf-8")

    # tvd.prop: per-element TVD^2 flag (itr_met=3 requires it) -- TVD everywhere.
    (dest_dir / "tvd.prop").write_text(
        "".join(f"{e + 1} 1\n" for e in range(n_elem)), encoding="utf-8"
    )

    # River freshwater point source (source_sink.in + vsource.th + msource.th).
    source_elem = _river_source_element(gr3_text, ocean_side)
    (dest_dir / "source_sink.in").write_text(
        f"1 !# of elements with sources\n{source_elem}\n\n0 !# of elements with sinks\n",
        encoding="utf-8",
    )
    t_end = sim_days * 86400.0
    (dest_dir / "vsource.th").write_text(
        f"0. {river_discharge_m3s:.3f}\n{t_end:.1f} {river_discharge_m3s:.3f}\n",
        encoding="utf-8",
    )
    # msource.th columns: time, T@source, S@source (ntracer=2: T then S).
    (dest_dir / "msource.th").write_text(
        f"0. {river_temp_c:.3f} {river_salinity_psu:.3f}\n"
        f"{t_end:.1f} {river_temp_c:.3f} {river_salinity_psu:.3f}\n",
        encoding="utf-8",
    )

    # station.in: one station at the mesh centroid.
    lon_c = float(node_xy[:, 0].mean())
    lat_c = float(node_xy[:, 1].mean())
    (dest_dir / "station.in").write_text(_author_station_in(lon_c, lat_c), encoding="utf-8")

    files = [
        dest_dir / n for n in (
            "hgrid.gr3", "vgrid.in", "param.nml", "bctides.in", "drag.gr3",
            "station.in", "temp.ic", "salt.ic", "tvd.prop",
            "source_sink.in", "vsource.th", "msource.th",
        )
    ]
    return {
        "files": files,
        "n_nodes": n_nodes,
        "n_elements": n_elem,
        "n_layers": nvrt,
        "open_node_count": open_node_count,
        "source_elem": source_elem,
        "bbox": tuple(bbox),
        "centroid": (lon_c, lat_c),
        "shoreline_clipped": bool(clipped),
    }


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
    points: Any = None,
    cells: Any = None,
    depths: Any = None,
    constituents: list[str],
    tidal_amplitude_m: float,
    sim_days: float,
    open_boundary_side: str,
    dt_s: float = 120.0,
    coastal_drag_cd: float = 0.0025,
    supplied_mesh: tuple[Any, Any, Any] | None = None,
) -> dict[str, Any]:
    """Author a full coastal_tin SCHISM deck into ``dest_dir``.

    Returns ``{"files": [Path, ...], "n_nodes": int, "n_elements": int,
    "open_node_count": int, "centroid": (lon, lat)}``. Raises SchismDeckError on a
    mesh/boundary fault (the honest-failure surface).

    Geometry source: either ``points``/``cells``/``depths`` (the oceanmesh TIN +
    node-sampled bathymetry) OR ``supplied_mesh`` (ADR 0212 precondition gate) as
    ``(points_lonlat (N,2), tris (M,3) 0-based, depths_positive_down (N,))`` -- a
    user mesh accepted by the gate REPLACES the internal TIN (real shoreline + real
    sampled bathymetry). The tidal open boundary is re-keyed to ``open_boundary_side``
    and the forcing (uniform constituent amplitude) stays as authored -- only the
    domain geometry becomes real.
    """
    import numpy as np

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if supplied_mesh is not None:
        pts = np.asarray(supplied_mesh[0], dtype=float)
        tris = np.asarray(supplied_mesh[1], dtype=np.int64)
        depth_arr = np.asarray(supplied_mesh[2], dtype=float)
    else:
        if points is None or cells is None or depths is None:
            raise SchismDeckError(
                "SCHISM_MESH_INVALID",
                "author_coastal_tin_deck needs points+cells+depths or supplied_mesh")
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
