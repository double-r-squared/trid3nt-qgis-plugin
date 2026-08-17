"""Author the plan-HDF ``/Event Conditions`` 2D-BC-line schema (OI-FT1).

This is the LAST forcing link the chain named: the engine's
``read_un_q2d_bc_`` (Read_UN_Q2D_BC.for) reads each 2D-BC-line flow hydrograph /
normal depth from ``/Event Conditions/Unsteady/Boundary Conditions`` -- NOT from
the ``.bNN`` fake-reach header (which is an inert required-1D placeholder) NOR
from the geometry BC line. Populating this group with a 2D-BC-keyed flow
hydrograph is what directs moving water onto the carved 2D area and WETS it.

Schema DECODED (schema facts only -- nothing vendored) from shipped HEC-RAS 6.6
pure-2D plan HDFs, and proven byte-exact against them (the per-BC ``Face
Indexes`` / ``Face Point Indexes`` / ``Face Fraction`` reproduce every shipped
reference; see ``docs/decisions/0138`` + ``scratchpad/oift1_probe_proofs``):

  /Event Conditions                              @Completed Successfully='True'
                                                 @Date Processed='<M/D/Y h:m:s>'
    /Unsteady
      /Boundary Conditions
        /Flow Hydrographs
          '2D: <area> BCLine: <name>'  (N,2) f4  interleaved [time, flow]
             @2D Flow Area, @BC Line, @Check TW Stage='False',
             @Data Type='INST-VAL', @EG Slope For Distributing Flow (f4),
             @Start Date, @End Date, @Interval, @Node Index=1 (i4),
             @Face Indexes (i4[k]), @Face Point Indexes (i4[k+1]),
             @Face Fraction (f4[k])
        /Normal Depths
          '2D: <area> BCLine: <name>'  (1,)  f4  friction slope
             @... (as above, minus the hydrograph-only attrs) + @BC Line WS='Multiple'
      /Initial Conditions                        @Startup Mode='Computed'

The keying is DERIVED from the geometry: a BC line's faces are the
``Geometry/Boundary Condition Lines/External Faces`` rows for that line, CLIPPED
to the line's ``[0, Length]`` station span; ``Face Fraction`` is each face's
clipped overlap fraction (partial at the ends where the polyline starts/stops
mid-face). Our own geometry writer lays the polyline exactly on the face
endpoints, so every fraction is 1.0 -- but the clip is implemented generally so
the author is correct for any BC line.
"""
from __future__ import annotations

import numpy as np

EC_ROOT = "Event Conditions"
BC_ROOT = "Event Conditions/Unsteady/Boundary Conditions"
IC_ROOT = "Event Conditions/Unsteady/Initial Conditions"
GEOM_BC = "Geometry/Boundary Condition Lines"


def _b(s: str) -> np.bytes_:
    return np.bytes_(s)


def derive_bc_faces(f, bc_name: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Return (Face Indexes i4[k], Face Point Indexes i4[k+1], Face Fraction
    f4[k], length_ft) for ``bc_name`` from the geometry BC-lines External Faces.

    Clips each face's ``[Station Start, Station End]`` to ``[0, Length]``; a face
    is kept iff it overlaps, and its fraction is the clipped overlap / face span.
    This exactly reproduces the shipped-HDF enumeration (proven offline).
    """
    attrs = f[f"{GEOM_BC}/Attributes"][()]
    ef = f[f"{GEOM_BC}/External Faces"][()]
    names = [r["Name"].decode(errors="replace").strip() for r in attrs]
    if bc_name not in names:
        raise KeyError(f"BC line {bc_name!r} not in geometry {names}")
    bc_id = names.index(bc_name)
    length = float(attrs[bc_id]["Length"])

    keep_fi: list[int] = []
    keep_fp: list[int] = []
    keep_frac: list[float] = []
    for row in ef:
        if int(row["BC Line ID"]) != bc_id:
            continue
        s0, s1 = float(row["Station Start"]), float(row["Station End"])
        lo, hi = max(s0, 0.0), min(s1, length)
        if hi <= lo:
            continue
        frac = (hi - lo) / (s1 - s0) if s1 > s0 else 1.0
        keep_fi.append(int(row["Face Index"]))
        keep_frac.append(frac)
        if not keep_fp:
            keep_fp.append(int(row["FP Start Index"]))
        keep_fp.append(int(row["FP End Index"]))
    if not keep_fi:
        raise ValueError(f"BC line {bc_name!r} clipped to zero faces (length={length})")
    return (np.asarray(keep_fi, np.int32), np.asarray(keep_fp, np.int32),
            np.asarray(keep_frac, np.float32), length)


def _common_bc_attrs(d, area: str, bc_name: str,
                     fi: np.ndarray, fp: np.ndarray, frac: np.ndarray) -> None:
    d.attrs["2D Flow Area"] = _b(area)
    d.attrs["BC Line"] = _b(bc_name)
    d.attrs["Check TW Stage"] = _b("False")
    d.attrs["Face Fraction"] = frac.astype(np.float32)
    d.attrs["Face Indexes"] = fi.astype(np.int32)
    d.attrs["Face Point Indexes"] = fp.astype(np.int32)
    d.attrs["Node Index"] = np.int32(1)


def write_flow_hydrograph_2d_bc(
    f, area: str, bc_name: str, times, flows, *,
    start_date: str, end_date: str, interval: str = "Days",
    eg_slope: float = 0.001,
) -> dict:
    """Write a 2D-BC-line FLOW hydrograph into ``/Event Conditions``.

    ``times``/``flows`` are 1D arrays (time in ``interval`` units, flow in cfs);
    stored interleaved as an ``(N,2) float32`` dataset. The BC faces are derived
    from geometry via ``derive_bc_faces``.
    """
    fi, fp, frac, length = derive_bc_faces(f, bc_name)
    times = np.asarray(times, np.float32).reshape(-1)
    flows = np.asarray(flows, np.float32).reshape(-1)
    if times.shape != flows.shape:
        raise ValueError("times and flows must be the same length")
    data = np.column_stack([times, flows]).astype(np.float32)
    grp = f.require_group(f"{BC_ROOT}/Flow Hydrographs")
    key = f"2D: {area} BCLine: {bc_name}"
    if key in grp:
        del grp[key]
    d = grp.create_dataset(key, data=data)
    _common_bc_attrs(d, area, bc_name, fi, fp, frac)
    d.attrs["Data Type"] = _b("INST-VAL")
    d.attrs["EG Slope For Distributing Flow"] = np.float32(eg_slope)
    d.attrs["Start Date"] = _b(start_date)
    d.attrs["End Date"] = _b(end_date)
    d.attrs["Interval"] = _b(interval)
    return {"bc": bc_name, "faces": int(fi.size), "length_ft": round(length, 2),
            "n_ordinates": int(data.shape[0]), "peak_cfs": float(flows.max())}


def write_normal_depth_2d_bc(f, area: str, bc_name: str, *, slope: float = 0.001) -> dict:
    """Write a 2D-BC-line NORMAL DEPTH (friction-slope outflow) into ``/Event Conditions``."""
    fi, fp, frac, length = derive_bc_faces(f, bc_name)
    grp = f.require_group(f"{BC_ROOT}/Normal Depths")
    key = f"2D: {area} BCLine: {bc_name}"
    if key in grp:
        del grp[key]
    d = grp.create_dataset(key, data=np.asarray([slope], np.float32))
    _common_bc_attrs(d, area, bc_name, fi, fp, frac)
    d.attrs["BC Line WS"] = _b("Multiple")
    return {"bc": bc_name, "faces": int(fi.size), "length_ft": round(length, 2),
            "slope": slope}


def strip_1d_reach_bcs(f) -> int:
    """Delete any 1D-reach-keyed EC entries (``River: ... Reach: ... RS: ...``).

    The carve copies a combined-1D/2D Muncie plan whose EC still names the White
    River reach; those references have no reach in the pure-2D fake-reach deck.
    Removing them leaves ONLY the authored 2D-BC entries. Returns the count.
    """
    removed = 0
    for sub in ("Flow Hydrographs", "Normal Depths", "Precipitation Hydrographs"):
        grp_path = f"{BC_ROOT}/{sub}"
        if grp_path not in f:
            continue
        grp = f[grp_path]
        for key in list(grp.keys()):
            if key.startswith("River:") or key.startswith("2D:") is False and "Reach:" in key:
                del grp[key]
                removed += 1
    return removed


def finalize_event_conditions(f, *, date_processed: str = "1/1/2026 12:00:00 AM") -> None:
    """Set the EC-root completion attrs + the Computed Initial-Conditions group."""
    ec = f.require_group(EC_ROOT)
    ec.attrs["Completed Successfully"] = _b("True")
    ec.attrs["Date Processed"] = _b(date_processed)
    ic = f.require_group(IC_ROOT)
    ic.attrs["Startup Mode"] = _b("Computed")
