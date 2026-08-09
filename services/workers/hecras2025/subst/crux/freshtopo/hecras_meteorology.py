"""Author the plan-HDF ``/Event Conditions/Meteorology/Precipitation`` rain-on-grid
forcing (spatially-uniform constant-intensity design storm).

The rain-on-grid (RoG) forcing link: instead of an inflow hydrograph on a BC line
(``hecras_event_conditions``), a RoG run rains uniformly over the whole 2D area.
HEC-RAS stores spatially-and-temporally uniform precipitation as a CONSTANT-mode
Meteorology record in the plan HDF's Event Conditions -- the same group the engine
reads the 2D-BC forcing from (ADR 0136-0138), so a composed pure-2D deck honours it
with no DSS file and no image rebuild for the forcing itself.

Structure DECODED from the HEC-RAS 6.x ``RasUnsteady.set_constant_precipitation`` /
``_update_constant_precipitation_hdf`` / ``_ensure_meteorology_attributes_dataset``
writers (ras-commander, riding in the solver image -- authoritative, matches what
the 6.x GUI writes):

  /Event Conditions/Meteorology
    Attributes                      compound[('Variable','S32'),('Group','S42')]
                                    one row: ('Precipitation',
                                              'Event Conditions/Meteorology/Precipitation')
    /Precipitation                  @Enabled=uint8(1), @Mode='Constant',
                                    @Constant Value=float64(rate),
                                    @Constant Units='mm/hr' | 'in/hr'

The matching ASCII switch is written into the unsteady/boundary text file:
  ``Precipitation Mode=Enable``
  ``Met BC=Precipitation|Mode=Constant``
  ``Met BC=Precipitation|Constant Value=<g>``
  ``Met BC=Precipitation|Constant Units=<units>``

CAVEAT (bake into the template docstring): constant mode rains at ONE rate for the
whole computation window -- a single-storm design event, ``depth = rate * duration``
(mass-balance hand-checkable). It has no falling limb WITHIN the storm; a true
time-varying hyetograph (rain that stops mid-run so drainage/recession is captured)
needs a DSS or ASCII ``Precipitation Hydrograph=`` record and is the RoG residual --
the exact analog of the TELEMAC constant-rain ``RAINDEF=1`` limit (ADR 0195/0196).
"""
from __future__ import annotations

import re

import numpy as np

MET_ROOT = "Event Conditions/Meteorology"
PRECIP_PATH = f"{MET_ROOT}/Precipitation"
_PRECIP_UNITS = ("mm/hr", "in/hr")

#: mm per US-survey inch (HEC-RAS depths are inches on a ftUS deck).
_MM_PER_IN = 25.4


class HecrasMeteorologyError(ValueError):
    """Invalid precipitation forcing (negative rate, bad units)."""


def _fmt_g(v: float) -> str:
    """HEC-RAS writes constant values compactly (e.g. '25', '0.25')."""
    return f"{float(v):g}"


def write_uniform_precipitation(
    f, *, rate_mm_per_hr: float, duration_hr: float,
    extents_xy: tuple[float, float, float, float], projection_wkt: str,
    start: str = "1900-01-02 00:00:00", step_hr: float = 1.0,
) -> dict:
    """Author a SPATIALLY-UNIFORM design storm as a gridded Meteorology record.

    The structure below is LIVE-DECODED against the 6.6 Linux compute engine (a
    chain of iterative solves, ADR 0199), NOT guessed -- the engine's meteorology
    readers open, in order:

      1. ``READ_UN_MET_PRECIP_DATA`` -> ``Precipitation/Values`` DIRECTLY (a
         gridded cumulative series; constant-mode attributes alone are a GUI
         convenience the SOLVER ignores -- proven: attrs-only errors "values not
         found"). Location is the group child, NOT ``Imported Raster Data/Values``
         (the GUI-import path), also proven live.
      2. ``Precipitation/Timestamp`` -- HEC ``DDMonYYYY HH:MM:SS`` FIXED strings
         (proven: float or ISO-8601 -> "input conversion error" in the engine's
         internal formatted read).
      3. ``READ_UN_M2D_PRECIP_INTERP`` (``MetInterp.f90``) -> a per-2D-area
         ``Precipitation/2D Flow Areas/<area>`` interpolation folder.

    Links 1-2 are authored here and pass the engine's readers; link 3 (the GUI-
    precomputed raster->cell interpolation folder) is the RESIDUAL that segfaults
    when authored blind -- the classic "needs a reference RoG deck" wall (ADR 0137).
    This writer authors the fully-decoded 1-2 structure; the composer stamps an
    empty per-area folder placeholder. ``depth = rate * duration`` (mm) is
    mass-balance-checkable. Cumulative amounts (mm), grid-extent attrs on ``Values``,
    ``Meteorology/Attributes`` index row. Returns a provenance dict."""
    rate = float(rate_mm_per_hr)
    dur = float(duration_hr)
    if not np.isfinite(rate) or rate < 0.0:
        raise HecrasMeteorologyError(f"precip rate must be finite and >= 0; got {rate}")
    if not np.isfinite(dur) or dur <= 0.0:
        raise HecrasMeteorologyError(f"duration must be finite and > 0; got {dur}")

    import datetime as _dt
    import uuid as _uuid

    x_min, y_min, x_max, y_max = (float(v) for v in extents_xy)
    # A single cell spanning the whole AOI (edge-aligned; centre irrelevant for 1x1).
    cellsize = float(max(x_max - x_min, y_max - y_min)) or 1.0
    raster_left = x_min
    raster_top = y_max

    # Hourly (or step_hr) cumulative series over the storm; first row 0, integrating
    # the constant rate by each interval.
    step = float(step_hr)
    n_steps = max(int(round(dur / step)), 1)
    t_hours = np.arange(0, n_steps + 1) * step               # 0 .. duration
    cumulative = (rate * t_hours).astype(np.float32).reshape(-1, 1)  # (n_times, 1) mm

    start_dt = _dt.datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    times = [(start_dt + _dt.timedelta(hours=float(h))).strftime("%Y-%m-%d %H:%M:%S")
             for h in t_hours]
    # The engine's Timestamp reader parses HEC ``DDMonYYYY HH:MM:SS`` fixed strings.
    _MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    hec_times = [
        (lambda d: f"{d.day:02d}{_MON[d.month - 1]}{d.year} "
                   f"{d.hour:02d}:{d.minute:02d}:{d.second:02d}")(
            start_dt + _dt.timedelta(hours=float(h)))
        for h in t_hours]

    f.require_group("Event Conditions")
    f.require_group(MET_ROOT)
    if "Precipitation" in f[MET_ROOT]:
        del f[MET_ROOT]["Precipitation"]
    precip = f.require_group(PRECIP_PATH)
    precip.attrs["Enabled"] = np.uint8(1)
    precip.attrs["Mode"] = np.bytes_("Gridded")
    precip.attrs["Source"] = np.bytes_("GDAL Raster File(s)")
    precip.attrs["GDAL Filename"] = np.bytes_(".\\Precipitation\\uniform.tif")
    precip.attrs["GDAL Datasetname"] = np.bytes_("precip")
    precip.attrs["GDAL Filter"] = np.bytes_("")
    precip.attrs["GDAL Folder"] = np.bytes_("")
    precip.attrs["Interpolation Method"] = np.bytes_("Nearest")

    # The 6.x Linux compute engine (READ_UN_MET_PRECIP_DATA) reads the gridded
    # series from ``Precipitation/Values`` DIRECTLY (verified live -- the GUI-import
    # ``Imported Raster Data/Values`` location is not what the solver opens).
    values_attrs = {
        "Data Type": np.bytes_("cumulative"),
        "GUID": np.bytes_(str(_uuid.uuid4())),
        "NoData": np.float32(-9999.0),
        "Projection": np.bytes_(projection_wkt.encode("ascii", "replace")
                                if isinstance(projection_wkt, str) else projection_wkt),
        "Raster Cellsize": np.float64(cellsize),
        "Raster Cols": np.int32(1),
        "Raster Left": np.float64(raster_left),
        "Raster Rows": np.int32(1),
        "Raster Top": np.float64(raster_top),
        "Rate Time Units": np.bytes_("Hour"),
        "Storage Configuration": np.bytes_("Sequential"),
        "Time Series Data Type": np.bytes_("Amount"),
        "Times": np.array(times, dtype="S19"),
        "Units": np.bytes_("mm"),
        "Version": np.bytes_("1.0"),
    }
    for name in ("Values", "Values (Vertical)"):
        if name in precip:
            del precip[name]
        ds = precip.create_dataset(
            name, data=cumulative, dtype=np.float32,
            chunks=(cumulative.shape[0], 1), compression="gzip",
            compression_opts=1, fillvalue=np.nan)
        for k, v in values_attrs.items():
            ds.attrs[k] = v

    # Link 2: the engine parses Timestamp as HEC DDMonYYYY fixed strings (live-proven).
    if "Timestamp" in precip:
        del precip["Timestamp"]
    precip.create_dataset("Timestamp", data=np.array(hec_times, dtype="S18"))

    _ensure_meteorology_attributes(f)
    return {"mode": "Gridded (uniform)", "rate_mm_per_hr": rate,
            "duration_hr": dur, "total_mm": round(rate * dur, 2),
            "n_times": int(cumulative.shape[0])}


def _ensure_meteorology_attributes(f) -> None:
    """Ensure ``Meteorology/Attributes`` indexes the Precipitation group (6.x form)."""
    attrs_path = f"{MET_ROOT}/Attributes"
    dt = np.dtype([("Variable", "S32"), ("Group", "S42")])
    row = np.array([(b"Precipitation", PRECIP_PATH.encode("ascii"))], dtype=dt)
    if attrs_path in f:
        existing = f[attrs_path][...]
        for r in existing:
            if bytes(r["Variable"]).rstrip(b"\x00") == b"Precipitation":
                return
        row = np.concatenate([existing.astype(dt), row])
        del f[attrs_path]
    f.create_dataset(attrs_path, data=row, chunks=(len(row),), maxshape=(None,),
                     compression="gzip", compression_opts=1)


_PRECIP_ASCII_KEYS = (
    "Precipitation Mode",
    "Met BC=Precipitation|Mode",
    "Met BC=Precipitation|Constant Value",
    "Met BC=Precipitation|Constant Units",
)


def constant_precipitation_ascii(rate: float, units: str = "mm/hr") -> str:
    """The ``.bNN`` ASCII precipitation switch block for constant-mode RoG.

    Appended to the fake-reach boundary/unsteady text so the ASCII switch agrees
    with the plan-HDF Meteorology group (the engine reads the HDF; the ASCII is the
    persisted GUI switch, kept consistent)."""
    if units not in _PRECIP_UNITS:
        raise HecrasMeteorologyError(f"units must be one of {_PRECIP_UNITS}; got {units!r}")
    return (
        "Precipitation Mode=Enable\n"
        "Met BC=Precipitation|Mode=Constant\n"
        f"Met BC=Precipitation|Constant Value={_fmt_g(rate)}\n"
        f"Met BC=Precipitation|Constant Units={units}\n"
    )


def inject_precipitation_ascii(bnn_text: str, rate: float, units: str = "mm/hr") -> str:
    """Return ``bnn_text`` with any stale precip keys stripped and the constant
    block appended (idempotent -- re-injecting replaces, never duplicates)."""
    lines = [ln for ln in bnn_text.splitlines()
             if not any(ln.strip().startswith(k + "=") for k in _PRECIP_ASCII_KEYS)]
    body = "\n".join(lines).rstrip("\n") + "\n"
    return body + constant_precipitation_ascii(rate, units)


def design_storm_units_and_rate(design_storm_mm_per_hr: float) -> tuple[float, str]:
    """Return the (rate, units) pair for a design storm given in mm/hr.

    HEC-RAS accepts 'mm/hr' directly, so the paper's metric intensity passes
    through unchanged (no conversion loss)."""
    r = float(design_storm_mm_per_hr)
    if not np.isfinite(r) or r < 0.0:
        raise HecrasMeteorologyError(f"design storm must be finite >= 0; got {r}")
    return r, "mm/hr"
