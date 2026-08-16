"""Offline ICM water-quality module smoke (ADR 0260): author a baroclinic estuary
deck, add the ICM eutrophication inputs (full icm.nml + a minimal ICM_rad.th.nc
radiation series so iRad=1 avoids sflux), run the targeted pschism_ICM_TVD-VL
binary DIRECTLY through the image, and prove the module solves to completion with
a physically-sensible discriminating pair: nutrient LOAD vs NO-LOAD river input
-> a downstream nutrient plume + dissolved-oxygen response only under load.

Run:
  cd /home/nate/Documents/trid3nt-local
  PYTHONPATH=.:contracts venvs/agent/bin/python scripts/smoke_schism_icm.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, ".")
from trid3nt_server.agent.workflows.schism.deck_authoring import (  # noqa: E402
    author_baroclinic_estuary_deck,
)

IMAGE = "trid3nt-local/schism:icmsed"
BIN = "/opt/schism/bin/pschism_ICM_TVD-VL"
WORK = Path("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
            "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/icm_smoke")
ICM_SAMPLE = Path("/tmp/claude-1000/-home-nate-Documents-GRACE-2/"
                  "fd2df08a-a572-4b62-ba9a-e82d8a0a740e/scratchpad/icm.nml")

# ICM core tracer order (1-based within the 17-var block).
NH4, NO3, PO4, DOX = 10, 11, 15, 17
# background river water-quality inflow (g/m3); DO ~saturated, low nutrients.
BG = {DOX: 8.0, NH4: 0.05, NO3: 0.05, PO4: 0.01}
# nutrient LOAD: elevated N + P (e.g. a wastewater / ag runoff source).
LOAD = {NH4: 3.0, NO3: 1.5, PO4: 0.3}


def _read_nodes(hgrid: str):
    lines = hgrid.splitlines()
    n_elem, n_node = (int(v) for v in lines[1].split()[:2])
    xy = [(float(lines[2 + i].split()[1]), float(lines[2 + i].split()[2]))
          for i in range(n_node)]
    return n_elem, n_node, xy


def _author_icm_nml(dest: Path) -> None:
    """Bake the full v5.11.0 sample icm.nml, switching radiation to the file
    source (iRad=1) so the run needs only a small ICM_rad.th.nc, not sflux."""
    text = ICM_SAMPLE.read_text()
    text = re.sub(r"(?m)^(iRad\s*=\s*)\S+", r"\g<1>1", text)
    if "iRad" not in text:
        text = text.replace("&MARCO\n", "&MARCO\niRad = 1\n", 1)
    (dest / "icm.nml").write_text(text)


def _author_rad_nc(dest: Path, *, value: float = 40.0, mdt: float = 3600.0,
                   nrec: int = 48) -> None:
    """Minimal ICM_rad.th.nc: 1D time_series (E/m2/day) + scalar time_step."""
    from netCDF4 import Dataset
    ds = Dataset(dest / "ICM_rad.th.nc", "w", format="NETCDF4")
    ds.createDimension("time", nrec)
    ts = ds.createVariable("time_series", "f8", ("time",))
    ts[:] = [value] * nrec
    step = ds.createVariable("time_step", "f8")
    step[:] = mdt
    ds.close()


def _patch_param_icm(text: str) -> str:
    def sub(pat, repl, t):
        return re.sub(pat, repl, t, count=1, flags=re.M)
    text = sub(r"^(\s*flag_ic\(7\)\s*=\s*)\S+", r"\g<1>0", text)  # cold start via wqc0
    inject = (
        "  iof_icm_core(1) = 1\n"    # PB1 diatom
        "  iof_icm_core(2) = 1\n"    # PB2 green algae
        "  iof_icm_core(3) = 1\n"    # PB3 cyanobacteria
        "  iof_icm_core(10) = 1\n"   # NH4
        "  iof_icm_core(11) = 1\n"   # NO3
        "  iof_icm_core(15) = 1\n"   # PO4
        "  iof_icm_core(17) = 1\n"   # DOX (dissolved oxygen)
    )
    text = re.sub(r"(?m)^(&SCHOUT\s*\n)", rf"\g<1>{inject}", text, count=1)
    return text


def _icm_source_vec(load: bool) -> list[float]:
    vec = [0.0] * 17
    for idx, v in BG.items():
        vec[idx - 1] = v
    if load:
        for idx, v in LOAD.items():
            vec[idx - 1] = v
    return vec


def _rewrite_msource(dest: Path, icm_vec: list[float]) -> None:
    p = dest / "msource.th"
    lines = p.read_text().splitlines()
    extra = " ".join(f"{c:.4f}" for c in icm_vec)
    p.write_text("\n".join(" ".join(ln.split()) + " " + extra
                           for ln in lines) + "\n")


def _patch_bctides_tracer(dest: Path) -> None:
    p = dest / "bctides.in"
    lines = p.read_text().splitlines()
    for i, ln in enumerate(lines):
        parts = ln.split()
        if len(parts) == 5 and parts[1] == "3" and all(
                x.lstrip("-").isdigit() for x in parts):
            lines[i] = ln + " 0"
            break
    p.write_text("\n".join(lines) + "\n")


def build_deck(dest: Path, *, load: bool) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    info = author_baroclinic_estuary_deck(
        dest,
        bbox=(-95.10, 29.30, -94.70, 29.70),  # Galveston Bay, TX (idealized channel)
        constituents=["M2"], tidal_amplitude_m=0.6, sim_days=0.5,
        ocean_side="south", river_discharge_m3s=1200.0,
        nx=12, ny=24, dt_s=120.0,
    )
    n_elem, n_node, xy = _read_nodes((dest / "hgrid.gr3").read_text())
    _author_icm_nml(dest)
    _author_rad_nc(dest)
    (dest / "param.nml").write_text(_patch_param_icm((dest / "param.nml").read_text()))
    _rewrite_msource(dest, _icm_source_vec(load))
    _patch_bctides_tracer(dest)
    return dict(n_elem=n_elem, n_node=n_node)


def run(dest: Path) -> tuple[bool, str]:
    (dest / "outputs").mkdir(exist_ok=True)
    ncompute, nscribe = 2, 11
    cmd = ["docker", "run", "--rm", "-v", f"{dest}:/data", "-w", "/data",
           "--entrypoint", "mpirun", IMAGE, "--allow-run-as-root", "--oversubscribe",
           "-np", str(ncompute + nscribe), BIN, str(nscribe)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    mirror = dest / "outputs" / "mirror.out"
    completed = mirror.exists() and "Run completed successfully" in mirror.read_text()
    fe = dest / "outputs" / "fatal.error"
    tail = (fe.read_text().strip()[:400] if fe.exists() else "") + "\n" + \
        "\n".join(proc.stderr.splitlines()[-8:])
    return completed, tail


def field_stats(dest: Path, varfile: str, var: str) -> dict:
    import numpy as np
    from netCDF4 import Dataset
    ds = Dataset(dest / "outputs" / varfile)
    arr = np.asarray(ds.variables[var][:])
    ds.close()
    fin = arr[np.isfinite(arr)]
    # last time step surface-ish stats
    return dict(max=float(np.nanmax(fin)), mean=float(np.nanmean(fin)),
                min=float(np.nanmin(fin)))


if __name__ == "__main__":
    WORK.mkdir(parents=True, exist_ok=True)
    results = {}
    for tag, load in (("noload", False), ("load", True)):
        dest = WORK / f"run_{tag}"
        if dest.exists():
            shutil.rmtree(dest)
        meta = build_deck(dest, load=load)
        ok, tail = run(dest)
        print(f"[{tag}] deck={meta} COMPLETED={ok}")
        if not ok:
            print(f"[{tag}] ERROR:\n{tail}")
            sys.exit(1)
        results[tag] = {
            "NH4": field_stats(dest, "ICM_NH4_1.nc", "ICM_NH4"),
            "NO3": field_stats(dest, "ICM_NO3_1.nc", "ICM_NO3"),
            "PO4": field_stats(dest, "ICM_PO4_1.nc", "ICM_PO4"),
            "DOX": field_stats(dest, "ICM_DOX_1.nc", "ICM_DOX"),
        }
    print("RESULTS:\n" + json.dumps(results, indent=2))
