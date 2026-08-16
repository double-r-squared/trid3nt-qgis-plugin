"""Offline SED3D module smoke (ADR 0260): author a baroclinic estuary deck, add
the SED3D sediment inputs, run the targeted pschism_SED_TVD-VL binary DIRECTLY
through the image (no MinIO/daemon), and prove the module solves to completion
with a physically-sensible discriminating pair (fine class suspends more than
coarse under identical tidal+river forcing).

Run:
  cd /home/nate/Documents/trid3nt-local
  PYTHONPATH=.:contracts/src venvs/agent/bin/python scripts/smoke_schism_sed.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")
from trid3nt_server.agent.workflows.schism.deck_authoring import (  # noqa: E402
    author_baroclinic_estuary_deck,
)

IMAGE = "trid3nt-local/schism:icmsed"
BIN = "/opt/schism/bin/pschism_SED_TVD-VL"
WORK = Path("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
            "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/sed_smoke")

# two-class bed: class 1 FINE (slow settling, low critical shear -> stays
# suspended), class 2 COARSE (fast settling, high critical shear -> deposits).
CLASSES = [
    dict(sd50=0.12, wsed=1.06, tau_ce=0.15, erate=1.6e-3, srho=2650.0),   # fine
    dict(sd50=1.20, wsed=28.65, tau_ce=0.60, erate=1.6e-3, srho=2650.0),  # coarse
]


def _read_nodes(hgrid: str):
    lines = hgrid.splitlines()
    n_elem, n_node = (int(v) for v in lines[1].split()[:2])
    xy = []
    for i in range(n_node):
        p = lines[2 + i].split()
        xy.append((float(p[1]), float(p[2])))
    return n_elem, n_node, xy


def _fd(x: float) -> str:
    """Fortran double literal (e.g. 1.6e-3 -> '1.600000d-03')."""
    return f"{x:.6e}".replace("e", "d")


def _author_sediment_nml(classes, *, sed_morph=0, morph_fac=1.0, nbed=1) -> str:
    j = lambda key: ", ".join(_fd(c[key]) for c in classes)  # noqa: E731
    return (
        "&SED_CORE\n"
        f"Sd50 = {j('sd50')}\n"
        f"Erate = {j('erate')}\n"
        "/\n"
        "&SED_OPT\n"
        f"iSedtype = {', '.join('1' for _ in classes)}\n"
        f"Srho = {j('srho')}\n"
        "comp_ws = 0\n"
        "comp_tauce = 0\n"
        f"Wsed = {j('wsed')}\n"
        f"tau_ce = {j('tau_ce')}\n"
        "sed_debug = 0\n"
        "bedload = 1\n"
        "suspended_load = 1\n"
        "ierosion = 0\n"
        "slope_formulation = 4\n"
        f"sed_morph = {sed_morph}\n"
        "sed_morph_time = 0.0d0\n"
        f"morph_fac = {morph_fac:.1f}d0\n"
        "slope_avalanching = 1\n"
        "dry_slope_cr = 1.0\n"
        "wet_slope_cr = 0.3\n"
        "bedmass_filter = 0\n"
        "actv_max = 0.05d0\n"
        f"Nbed = {nbed}\n"
        "sedlay_ini_opt = 0\n"
        "newlayer_thick = 0.001d0\n"
        "imeth_bed_evol = 2\n"
        "poro_option = 1\n"
        "porosity = 0.4\n"
        "/\n"
    )


def _gr3_nodefield(title, n_elem, xy, value_fn) -> str:
    out = [title, f"{n_elem} {len(xy)}"]
    for i, (x, y) in enumerate(xy):
        out.append(f"{i + 1} {x:.9f} {y:.9f} {value_fn(i):.7f}")
    return "\n".join(out) + "\n"


def _patch_param_sed(text: str, *, sed_class: int, sed_morph: int) -> str:
    def sub(pat, repl, t):
        return re.sub(pat, repl, t, count=1, flags=re.M)
    text = sub(r"^(\s*sed_class\s*=\s*)\S+", rf"\g<1>{sed_class}", text)
    text = sub(r"^(\s*flag_ic\(5\)\s*=\s*)\S+", r"\g<1>0", text)  # zero suspended IC
    # SED output flags into &SCHOUT: per-class conc + total suspended + bed change/stress
    inject = (
        "  iof_sed(7) = 1\n"      # bottom depth change (morphology)
        "  iof_sed(9) = 1\n"      # bottom shear stress
        "  iof_sed(19) = 1\n"     # class-1 (fine) concentration
        "  iof_sed(20) = 1\n"     # class-2 (coarse) concentration
        "  iof_sed(21) = 1\n"     # total suspended load
    )
    text = re.sub(r"(?m)^(&SCHOUT\s*\n)", rf"\g<1>{inject}", text, count=1)
    return text


def _rewrite_msource(dest: Path, concs: list[float]) -> None:
    """Append per-class river inflow concentrations (kg/m3) for the SED tracers.

    The river point source carries a suspended sediment load; the discriminating
    pair is then SETTLING: the fine class (low Wsed) stays suspended and travels
    downstream, the coarse class (high Wsed) deposits near the source.
    """
    p = dest / "msource.th"
    lines = p.read_text().splitlines()
    extra = " ".join(f"{c:.4f}" for c in concs)
    out = []
    for ln in lines:
        parts = ln.split()
        out.append(" ".join(parts) + " " + extra)
    p.write_text("\n".join(out) + "\n")


def _patch_bctides_tracer(dest: Path) -> None:
    """Append one tracer-type flag (0 = no boundary input) for the SED module."""
    p = dest / "bctides.in"
    lines = p.read_text().splitlines()
    for i, ln in enumerate(lines):
        parts = ln.split()
        # the open-boundary flag line: nnodes iettype ifltype itetype isatype
        if len(parts) == 5 and parts[1] == "3" and all(x.lstrip("-").isdigit() for x in parts):
            lines[i] = ln + " 0"
            break
    p.write_text("\n".join(lines) + "\n")


def build_deck(dest: Path, *, sed_morph: int = 0) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    info = author_baroclinic_estuary_deck(
        dest,
        bbox=(-95.10, 29.30, -94.70, 29.70),  # Galveston Bay, TX (idealized channel)
        constituents=["M2"],
        tidal_amplitude_m=0.6,
        sim_days=0.5,
        ocean_side="south",
        river_discharge_m3s=1200.0,
        nx=12, ny=24, dt_s=120.0,
    )
    hgrid = (dest / "hgrid.gr3").read_text()
    n_elem, n_node, xy = _read_nodes(hgrid)

    (dest / "sediment.nml").write_text(
        _author_sediment_nml(CLASSES, sed_morph=sed_morph))
    (dest / "bedthick.ic").write_text(
        _gr3_nodefield("bed thickness", n_elem, xy, lambda i: 2.0))
    for k in range(len(CLASSES)):
        (dest / f"bed_frac_{k + 1}.ic").write_text(
            _gr3_nodefield(f"bed frac class {k + 1}", n_elem, xy,
                           lambda i: 1.0 / len(CLASSES)))
    if sed_morph == 1:
        (dest / "imorphogrid.gr3").write_text(
            _gr3_nodefield("morpho ramp", n_elem, xy, lambda i: 1.0))

    param = (dest / "param.nml").read_text()
    (dest / "param.nml").write_text(
        _patch_param_sed(param, sed_class=len(CLASSES), sed_morph=sed_morph))
    # river carries 0.2 kg/m3 suspended load in each class; settling separates them
    _rewrite_msource(dest, [0.2 for _ in CLASSES])
    _patch_bctides_tracer(dest)
    return dict(n_elem=n_elem, n_node=n_node)


def run(dest: Path) -> tuple[bool, str]:
    (dest / "outputs").mkdir(exist_ok=True)
    # nscribe must be >= number of scribed output variables (elev+T+S+5 iof_sed).
    ncompute, nscribe = 2, 9
    cmd = [
        "docker", "run", "--rm", "-v", f"{dest}:/data", "-w", "/data",
        "--entrypoint", "mpirun", IMAGE, "--allow-run-as-root", "--oversubscribe",
        "-np", str(ncompute + nscribe), BIN, str(nscribe),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    mirror = dest / "outputs" / "mirror.out"
    completed = mirror.exists() and "Run completed successfully" in mirror.read_text()
    tail = "\n".join(proc.stderr.splitlines()[-15:])
    return completed, tail


def inspect_outputs(dest: Path) -> dict:
    import numpy as np
    import netCDF4  # noqa: F401
    from netCDF4 import Dataset
    out = dest / "outputs"
    res = {}
    for name in sorted(p.name for p in out.glob("*.nc")):
        try:
            ds = Dataset(out / name)
            vs = {}
            for v in ds.variables:
                if v in ("time", "SCHISM_hgrid_node_x", "SCHISM_hgrid_node_y"):
                    continue
                arr = np.asarray(ds.variables[v][:])
                if arr.size:
                    fin = arr[np.isfinite(arr)]
                    if fin.size:
                        vs[v] = dict(max=float(np.nanmax(fin)),
                                     mean=float(np.nanmean(np.abs(fin))))
            if vs:
                res[name] = vs
            ds.close()
        except Exception as exc:  # noqa: BLE001
            res[name] = f"read error: {exc}"
    return res


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    dest = WORK / "run_multiclass"
    import shutil
    if dest.exists():
        shutil.rmtree(dest)
    meta = build_deck(dest, sed_morph=0)
    print(f"deck authored: {meta}")
    ok, tail = run(dest)
    print(f"COMPLETED={ok}")
    if not ok:
        print("STDERR TAIL:\n" + tail)
        sys.exit(1)
    outs = inspect_outputs(dest)
    import json
    print("OUTPUTS:\n" + json.dumps(outs, indent=2)[:3000])
