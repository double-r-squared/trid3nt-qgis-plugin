"""Author the geometry-HDF ``.../<2D area>/Infiltration`` SCS Curve Number layer.

The rain-on-grid (RoG) infiltration link: HEC-RAS 2D loses rainfall to the ground
through a per-cell SCS Curve Number loss method stored in the geometry, under
``Geometry/2D Flow Areas/<area>/Infiltration``. Without it a rain-on-grid solve
runs ZERO-loss (all rain becomes runoff) -- the same double-count trap the TELEMAC
side avoids (ADR 0195). This authors the layer our own composer stamps into the
plan HDF's Geometry, so the engine applies genuine SCS-CN abstraction.

Structure DECODED byte-exact from the shipped public-domain Bald Eagle Creek dam-
break geometry HDF (``BaldEagleDamBrk.g09.hdf``, an HEC example with a real SCS-CN
infiltration layer -- schema facts only, nothing vendored):

  Geometry/2D Flow Areas/<area>/Infiltration        @Infiltration Filename,
                                                    @Infiltration Layername,
                                                    @Infiltration Date Last Modified,
                                                    @Infiltration File Date
    Curve Number                (Nc,) f4   per-cell CN (AMC-adjusted here)
    Abstraction Ratio           (Nc,) f4   per-cell Ia/S ratio (0.2 std / 0.05 rev)
    Minimum Infiltration Rate   (Nc,) f4   per-cell fc floor (in/hr; 0 = pure SCS-CN)
    Cell Center Classifications (Nc,) i4   soil-class index per cell
    Face Center Classifications (Nf,) i4   soil-class index per face
    Properties                  (1,) compound[('Name','S27'),('Value','<f4')]
                                           ('SCS Initial Loss Reset Time', <hr>)

The same four Infiltration attrs are ALSO mirrored on the parent 2D-area group
(the geometry cross-references the layer there). ``Curve Number`` /
``Abstraction Ratio`` / ``Minimum Infiltration Rate`` are the fields the engine
reads per computational cell; the Classifications index a soil legend (uniform =
class 1 for a single-CN authored layer). Cell arrays are sized to the TOTAL cell
count (incl. ghost cells) so they index 1:1 with ``Cells Center Manning's n``.

The AMC conversion is the canonical NRCS NEH-630 formula (byte-parity with the
TELEMAC ``runoff_scs_cn.f`` branch, ADR 0195), kept self-contained so the worker
tree imports nothing from the server package.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_AREA_ROOT = "Geometry/2D Flow Areas"

#: HEC's default initial-abstraction reset time (hours) -- the Bald Eagle value.
DEFAULT_INITIAL_LOSS_RESET_HR = 24.0
#: Standard SCS initial-abstraction ratio Ia/S (0.2 standard; 0.05 revised, Woodward
#: 2003). Mirrors the TELEMAC ``OPTION FOR INITIAL ABSTRACTION RATIO`` knob.
IA_RATIO_STANDARD = 0.2
IA_RATIO_REVISED = 0.05

#: AMC label -> integer (1 dry / 2 normal / 3 wet), the paper's condition selector.
_AMC_MAP = {"dry": 1, "normal": 2, "wet": 3, "i": 1, "ii": 2, "iii": 3,
            "1": 1, "2": 2, "3": 3}


class HecrasInfiltrationError(ValueError):
    """Invalid infiltration input (out-of-range CN, unknown AMC, size mismatch)."""


def amc_to_int(amc) -> int:
    """Coerce an AMC label/int to 1 (dry) / 2 (normal) / 3 (wet)."""
    if isinstance(amc, (int, np.integer)) and int(amc) in (1, 2, 3):
        return int(amc)
    key = str(amc).strip().lower()
    if key not in _AMC_MAP:
        raise HecrasInfiltrationError(
            f"antecedent moisture must be dry/normal/wet or 1/2/3; got {amc!r}")
    return _AMC_MAP[key]


def amc_convert_cn(cn2: float, amc: int) -> float:
    """Convert a normal-condition CN2 to the AMC dry (I) / normal (II) / wet (III).

    NRCS NEH-630 (Chow 1988):
      AMC I (dry):  CN1 = 4.2*CN2 / (10 - 0.058*CN2)
      AMC III (wet):CN3 = 23*CN2 / (10 + 0.13*CN2)
    Byte-parity with the TELEMAC ``runoff_scs_cn.f`` branch (ADR 0195).
    """
    cn2 = float(cn2)
    if not (0.0 < cn2 <= 100.0):
        raise HecrasInfiltrationError(f"CN2 must be in (0, 100]; got {cn2}")
    if amc == 2:
        return cn2
    if amc == 1:
        return 4.2 * cn2 / (10.0 - 0.058 * cn2)
    if amc == 3:
        return 23.0 * cn2 / (10.0 + 0.13 * cn2)
    raise HecrasInfiltrationError(f"AMC must be 1/2/3; got {amc}")


@dataclass
class InfiltrationLayer:
    """The per-cell SCS-CN arrays authored into the geometry Infiltration group."""

    curve_number: np.ndarray          # (Nc,) f4
    abstraction_ratio: np.ndarray     # (Nc,) f4
    min_infiltration_rate: np.ndarray  # (Nc,) f4
    initial_loss_reset_hr: float = DEFAULT_INITIAL_LOSS_RESET_HR


def build_infiltration_layer(
    n_cells: int,
    *,
    curve_number: float | None = None,
    per_cell_cn2: np.ndarray | None = None,
    amc: int | str = 2,
    ia_ratio: float = IA_RATIO_STANDARD,
    min_infiltration_rate_in_hr: float = 0.0,
    initial_loss_reset_hr: float = DEFAULT_INITIAL_LOSS_RESET_HR,
) -> InfiltrationLayer:
    """Assemble the per-cell SCS-CN arrays for ``n_cells`` computational cells.

    ``curve_number`` gives a UNIFORM CN2 over the whole area (the ``curve_number``
    knob, analog to the TELEMAC uniform path); ``per_cell_cn2`` supplies a
    distributed CN2 field (e.g. NLCD-derived, Table-1 pattern). Exactly one must be
    given. The normal-condition CN2 is AMC-adjusted here (dry/normal/wet), so the
    per-cell ``Curve Number`` written to the geometry is the effective CN the engine
    abstracts with. ``ia_ratio`` is the initial-abstraction ratio (0.2 standard /
    0.05 revised); ``min_infiltration_rate_in_hr`` is the constant-loss floor once
    the CN capacity is exhausted (0 = pure SCS-CN)."""
    n = int(n_cells)
    amc_i = amc_to_int(amc)
    if (curve_number is None) == (per_cell_cn2 is None):
        raise HecrasInfiltrationError(
            "give exactly one of curve_number (uniform) or per_cell_cn2 (distributed)")
    if curve_number is not None:
        cn_adj = amc_convert_cn(float(curve_number), amc_i)
        cn = np.full(n, cn_adj, np.float32)
    else:
        arr = np.asarray(per_cell_cn2, np.float64).reshape(-1)
        if arr.size != n:
            raise HecrasInfiltrationError(
                f"per_cell_cn2 has {arr.size} values but the mesh has {n} cells")
        cn = np.asarray([amc_convert_cn(v, amc_i) for v in arr], np.float32)
    if not (0.0 < float(ia_ratio) < 1.0):
        raise HecrasInfiltrationError(f"ia_ratio must be in (0,1); got {ia_ratio}")
    if float(min_infiltration_rate_in_hr) < 0.0:
        raise HecrasInfiltrationError("min_infiltration_rate must be >= 0")
    return InfiltrationLayer(
        curve_number=cn,
        abstraction_ratio=np.full(n, float(ia_ratio), np.float32),
        min_infiltration_rate=np.full(n, float(min_infiltration_rate_in_hr), np.float32),
        initial_loss_reset_hr=float(initial_loss_reset_hr),
    )


def write_infiltration_layer(
    f, area_name: str, layer: InfiltrationLayer, *,
    n_faces: int,
    filename: str = ".\\Soils Data\\Infiltration.hdf",
    layername: str = "Infiltration",
    date_stamp: str = "01JAN2026 00:00:00",
) -> dict:
    """Write ``layer`` into the plan/geometry HDF under the 2D area's Infiltration.

    ``f`` is an open ``h5py.File`` positioned on a deck that already carries
    ``Geometry/2D Flow Areas/<area_name>`` (our composer writes the 2D area first).
    The per-cell arrays must be sized to the geometry's TOTAL cell count; the caller
    passes ``n_faces`` for the face-classification array. Returns a provenance dict.
    """
    area_path = f"{_AREA_ROOT}/{area_name}"
    if area_path not in f:
        raise HecrasInfiltrationError(
            f"2D area {area_name!r} absent -- write the 2D flow area before infiltration")
    area = f[area_path]
    nc = int(area["Cells Center Manning's n"].shape[0])
    if layer.curve_number.shape[0] != nc:
        raise HecrasInfiltrationError(
            f"infiltration arrays sized {layer.curve_number.shape[0]} but the geometry "
            f"has {nc} cells -- they must index 1:1 with Cells Center Manning's n")

    grp_path = f"{area_path}/Infiltration"
    if "Infiltration" in area:
        del area["Infiltration"]
    g = area.create_group("Infiltration")
    g.create_dataset("Curve Number", data=layer.curve_number.astype(np.float32))
    g.create_dataset("Abstraction Ratio", data=layer.abstraction_ratio.astype(np.float32))
    g.create_dataset("Minimum Infiltration Rate",
                     data=layer.min_infiltration_rate.astype(np.float32))
    g.create_dataset("Cell Center Classifications", data=np.ones(nc, np.int32))
    g.create_dataset("Face Center Classifications", data=np.ones(int(n_faces), np.int32))
    props = np.array(
        [(b"SCS Initial Loss Reset Time", np.float32(layer.initial_loss_reset_hr))],
        dtype=np.dtype([("Name", "S27"), ("Value", "<f4")]))
    g.create_dataset("Properties", data=props)

    meta = {
        "Infiltration Filename": np.bytes_(filename),
        "Infiltration Layername": np.bytes_(layername),
        "Infiltration Date Last Modified": np.bytes_(date_stamp),
        "Infiltration File Date": np.bytes_(date_stamp),
    }
    for k, v in meta.items():
        g.attrs[k] = v
        area.attrs[k] = v  # mirror on the 2D-area group (the geometry cross-ref)

    return {
        "area": area_name,
        "cells": nc,
        "faces": int(n_faces),
        "cn_min": round(float(layer.curve_number.min()), 3),
        "cn_max": round(float(layer.curve_number.max()), 3),
        "ia_ratio": round(float(layer.abstraction_ratio[0]), 4),
        "min_infil_in_hr": round(float(layer.min_infiltration_rate[0]), 4),
    }


def write_percent_impervious(
    f, area_name: str, *, n_faces: int, percent: float = 0.0,
    filename: str = ".\\Land Classification\\LandCover.hdf",
    layername: str = "LandCover",
    date_stamp: str = "01JAN2026 00:00:00",
) -> dict:
    """Write the sibling ``.../<area>/Percent Impervious`` group the 2D hydrology
    reader (``READ_UN_HYDROLOGY2D`` -> ``surfacemodule.setsurfacepercentimpervious``)
    requires WHENEVER an Infiltration layer is present.

    DECODED live (ADR 0205): with the precip interpolation folder in place the engine
    reads past MetInterp into ``READ_UN_HYDROLOGY2D``, which reads Curve Number +
    Abstraction Ratio + Minimum Infiltration Rate AND ``Percent Impervious``. Absent,
    the surface-module lookup faults (``H5Gcreate2: invalid location``). Structure
    byte-exact from the shipped ``BaldEagleDamBrk.g09.hdf`` sibling group:

      Geometry/2D Flow Areas/<area>/Percent Impervious   @Percent Impervious Filename,
                                                         @...Layername, @...File Date,
                                                         @...Date Last Modified
        Percent Impervious           (Nc,) f4   per-cell impervious fraction (0..100)
        Cell Center Classifications  (Nc,) i4   class index per cell
        Face Center Classifications  (Nf,) i4   class index per face

    A rain-on-grid catchment with no impervious surfaces is ``percent = 0.0`` (the
    reference values are 0.0). Cell arrays index 1:1 with ``Cells Center Manning's n``;
    ``n_faces`` sizes the face array. Returns a provenance dict."""
    area_path = f"{_AREA_ROOT}/{area_name}"
    if area_path not in f:
        raise HecrasInfiltrationError(
            f"2D area {area_name!r} absent -- write the 2D flow area before impervious")
    area = f[area_path]
    nc = int(area["Cells Center Manning's n"].shape[0])
    if not (0.0 <= float(percent) <= 100.0):
        raise HecrasInfiltrationError(
            f"percent impervious must be in [0, 100]; got {percent}")

    if "Percent Impervious" in area:
        del area["Percent Impervious"]
    g = area.create_group("Percent Impervious")
    g.create_dataset("Percent Impervious", data=np.full(nc, float(percent), np.float32))
    g.create_dataset("Cell Center Classifications", data=np.ones(nc, np.int32))
    g.create_dataset("Face Center Classifications", data=np.ones(int(n_faces), np.int32))

    meta = {
        "Percent Impervious Filename": np.bytes_(filename),
        "Percent Impervious Layername": np.bytes_(layername),
        "Percent Impervious Date Last Modified": np.bytes_(date_stamp),
        "Percent Impervious File Date": np.bytes_(date_stamp),
    }
    for k, v in meta.items():
        g.attrs[k] = v
        area.attrs[k] = v  # mirror on the 2D-area group (the geometry cross-ref)

    return {"area": area_name, "cells": nc, "faces": int(n_faces),
            "percent_impervious": float(percent)}
