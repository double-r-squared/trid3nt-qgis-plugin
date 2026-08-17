"""Construct a ``File Type="HEC-RAS Results"`` plan-HDF skeleton around a seeded
GEOMETRY-only HDF, by Muncie-diff transplant.

RasGeomPreprocess's modern two-argument CLI (``<plan_hdf> <geom_suffix>``) engages
ONLY when its first argument is an HDF whose root attribute ``File Type`` reads
``"HEC-RAS Results"`` (top-level ``Plan Data`` + ``Event Conditions`` + ``Geometry``
groups). A bare ``File Type="HEC-RAS Geometry"`` HDF -- what every public HEC-RAS
example project ships -- is discarded into the legacy ``io.x`` Fortran path
(``Htabopen.for``). This builder produces the Results-typed wrapper the seeded
fixtures lack: it copies HEC's shipped Muncie plan HDF (the only in-distribution
``File Type="HEC-RAS Results"`` reference), replaces its ``/Geometry`` subtree with
the seeded fixture's REAL, GUI-computed geometry, and repoints ``Plan Information``
at the fixture's project. The 1D cross-section subtree (Beaver Creek) or the 2D
flow area + ``Type="Connection"`` structures + storage areas (Bald Eagle) transplant
verbatim as an h5py group copy.

The product is engine-RECOGNIZED: RasGeomPreprocess advances past the ``io.x``
fallback into ``READ_SIZ`` (proven -- a raw geometry-only HDF falls into ``io.x``;
this skeleton reaches the geometry reader). It is NOT yet solvable: RasGeomPreprocess
reads the network geometry from a Muncie-format ``.xNN`` preprocessor file (the
``Section - Arrays Sizes`` / ``FORMAT 50`` reader), and the seeded fixtures ship only
the GUI ``.gNN`` text, not the ``.xNN``. Authoring that ``.xNN`` is the remaining
per-front lift. This builder discharges the plan-HDF half of ADR
0172's recipe and is the reference the ``.xNN`` author builds beside.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import h5py
import numpy as np

_HERE = Path(__file__).resolve().parent

#: The only shipped ``File Type="HEC-RAS Results"`` plan HDF in the distribution.
MUNCIE_PLAN = _HERE / "muncie_smoke" / "wrk_source" / "Muncie.p04.tmp.hdf"


def build_skeleton(
    geometry_hdf: Path,
    out_path: Path,
    *,
    plan_template: Path = MUNCIE_PLAN,
    geometry_filename: str | None = None,
    flow_filename: str | None = None,
    project_title: str | None = None,
) -> dict:
    """Write a Results-typed plan HDF at ``out_path`` around ``geometry_hdf``.

    ``geometry_hdf`` is a seeded fixture's ``File Type="HEC-RAS Geometry"`` HDF; its
    ``/Geometry`` subtree (and that group's attributes) replaces the template's.
    ``geometry_filename`` / ``flow_filename`` / ``project_title`` repoint the copied
    ``Plan Information`` at the fixture's project when given. Returns a provenance
    dict (transplanted geometry children, any structure types, 2D area names).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plan_template, out_path)

    prov: dict = {"geometry_children": [], "structure_types": [], "flow_areas": []}
    with h5py.File(out_path, "r+") as f, h5py.File(geometry_hdf, "r") as g:
        del f["Geometry"]
        g.copy(g["Geometry"], f, name="Geometry")
        for k, v in g["Geometry"].attrs.items():
            f["Geometry"].attrs[k] = v
        f.attrs["File Type"] = np.bytes_(b"HEC-RAS Results")
        try:
            proj = g["/"].attrs["Projection"]
            f.attrs["Projection"] = proj
        except KeyError:
            pass

        pi = f["Plan Data/Plan Information"]
        if geometry_filename is not None:
            pi.attrs["Geometry Filename"] = np.bytes_(geometry_filename.encode())
        if flow_filename is not None:
            pi.attrs["Flow Filename"] = np.bytes_(flow_filename.encode())
        if project_title is not None:
            pi.attrs["Project Title"] = np.bytes_(project_title.encode())

        prov["geometry_children"] = sorted(f["Geometry"].keys())
        st = f.get("Geometry/Structures/Attributes")
        if st is not None:
            prov["structure_types"] = sorted(
                {r["Type"].decode(errors="replace").strip() for r in st[()]}
            )
        fa = f.get("Geometry/2D Flow Areas")
        if fa is not None:
            prov["flow_areas"] = sorted(
                k for k in fa.keys() if isinstance(fa[k], h5py.Group)
            )
    prov["out_path"] = str(out_path)
    return prov
