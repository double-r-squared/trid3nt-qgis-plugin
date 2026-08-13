"""Parametric Holland-1980 hurricane wind/pressure fields as SCHISM sflux inputs.

The honest, feasible PaHM route for OUR worker image (ADR 0217). The baked
full-monty binary (``pschism_WWM_COSINE_..._PAHM_...``) carries ``USE_PAHM``, but a
full-monty run unconditionally initializes EVERY compiled tracer module and so
demands every module namelist (icm.nml, sediment.nml, cosine.nml, fib.nml,
marsh, ...) -- the documented ADR 0115 friction that led to the targeted binaries.
There is no targeted PaHM-only binary in the image. Rather than rebuild a fourth
binary, this authors STANDALONE parametric wind/pressure fields (Holland 1980) as
the ``sflux/`` atmospheric-forcing inputs the CLEAN hydro-core binary
(``pschism_TVD-VL``) consumes via ``nws=2`` -- a SCHISM CORE feature (not a compiled
module), so the existing image runs it with NO rebuild.

Physics (Holland, G.J., 1980, "An Analytic Model of the Wind and Pressure Profiles
in Hurricanes", Mon. Wea. Rev. 108, 1212-1218):

  radial surface pressure  P(r) = Pc + dP * exp( -(Rmw/r)^B )
  gradient wind            Vg(r) = sqrt( (Rmw/r)^B * (B/rho) * dP * exp(-(Rmw/r)^B)
                                         + (r f / 2)^2 ) - r f / 2
  Holland B (from Vmax/dP) B = rho * e * Vmax^2 / dP   (clamped [1.0, 2.5])

with ``dP = Pn - Pc`` the pressure deficit, ``f = 2 Omega sin(lat)`` the Coriolis
parameter, ``rho`` the air density. The gradient wind is reduced to a 10-m wind by
a surface factor and rotated cyclonically with an inflow angle; the field is a
SYMMETRIC vortex (no forward-motion asymmetry -- the honest screening scope,
GAHM's asymmetry is the native-PaHM upgrade). Track center / Pc / Vmax / Rmw are
interpolated in time along the best track.

sflux format: SCHISM ``nws=2`` reads a structured lon/lat time series from
``sflux/sflux_air_1.NNNN.nc`` with ``uwind`` / ``vwind`` (m/s), ``prmsl`` (Pa),
``stmp`` (K), ``spfh`` (kg/kg), a 2D ``lon`` / ``lat`` grid, and a ``time``
(days-since-base_date) axis whose ``base_date`` attribute is the run start
(schism-dev sflux docs: schism master docs, "Atmospheric flux"). This module is
PURE (numpy + netCDF4, no server imports) so it is offline-testable and the deck
authoring imports it.

ASCII only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "TrackFix",
    "HollandField",
    "holland_profile",
    "holland_B",
    "wind_pressure_on_grid",
    "build_sflux_grid",
    "interpolate_track",
    "write_sflux_air",
    "SFLUX_INPUTS_TXT",
]

#: Air density at the sea surface (kg/m3) -- the standard Holland value.
_RHO_AIR: float = 1.15
#: Ambient (environmental) sea-level pressure fallback when USA_POCI is absent (Pa).
_PN_DEFAULT_PA: float = 101300.0
#: Surface reduction factor: gradient wind -> 10-m wind (standard 0.9).
_SURFACE_REDUCTION: float = 0.9
#: Inflow angle (radians): near-surface flow spirals inward ~20 deg.
_INFLOW_ANGLE_RAD: float = math.radians(20.0)
#: Earth angular rate (rad/s) for the Coriolis parameter.
_OMEGA: float = 7.2921159e-5
#: Radius of maximum wind fallback when USA_RMW is absent (m).
_RMW_DEFAULT_M: float = 40_000.0
#: Metres per degree of latitude (mean) and the per-degree-longitude base.
_M_PER_DEG_LAT: float = 110_540.0
_M_PER_DEG_LON: float = 111_320.0
#: Holland B clamp bounds (physical range).
_B_MIN, _B_MAX = 1.0, 2.5

#: The sflux_inputs.txt namelist for a single air-source (air_1) parametric field.
#: air_2 is declared but marked non-fatal-if-missing so SCHISM tolerates its
#: absence (the standard single-source pattern).
SFLUX_INPUTS_TXT: str = (
    "&sflux_inputs\n"
    "  air_1_relative_weight=1.,\n"
    "  air_2_relative_weight=99.,\n"
    "  air_1_max_window_hours=120.,\n"
    "  air_1_fail_if_missing=.true.,\n"
    "  air_2_fail_if_missing=.false.,\n"
    "  air_1_file='sflux_air_1',\n"
    "  air_2_file='sflux_air_2',\n"
    "  uwind_name='uwind',\n"
    "  vwind_name='vwind',\n"
    "  prmsl_name='prmsl',\n"
    "  stmp_name='stmp',\n"
    "  spfh_name='spfh'\n"
    "/\n"
)


@dataclass(frozen=True)
class TrackFix:
    """One best-track fix (IBTrACS row) after unit normalisation.

    time_hr: hours since the run start (a monotone axis the interpolation walks).
    lon / lat: storm center (deg).
    pc_pa: central pressure (Pa).
    vmax_ms: maximum sustained 10-m wind (m/s).
    rmw_m: radius of maximum wind (m).
    pn_pa: ambient/environmental pressure (Pa).
    """

    time_hr: float
    lon: float
    lat: float
    pc_pa: float
    vmax_ms: float
    rmw_m: float
    pn_pa: float


@dataclass(frozen=True)
class HollandField:
    """A rendered sflux field + its provenance scalars (the numbers the agent cites)."""

    peak_wind_ms: float
    min_pressure_pa: float
    n_times: int
    n_lon: int
    n_lat: int
    grid_bbox: tuple[float, float, float, float]
    holland_b_mean: float


def holland_B(vmax_ms: float, dp_pa: float) -> float:
    """Holland B shape parameter from Vmax and the pressure deficit (clamped)."""
    if dp_pa <= 0.0 or vmax_ms <= 0.0:
        return 1.3
    b = _RHO_AIR * math.e * vmax_ms * vmax_ms / dp_pa
    return max(_B_MIN, min(_B_MAX, b))


def holland_profile(
    r_m: float, pc_pa: float, pn_pa: float, rmw_m: float, b: float, lat_deg: float
) -> tuple[float, float]:
    """Return ``(gradient_wind_ms, pressure_pa)`` at radius ``r_m`` (Holland 1980)."""
    dp = max(1.0, pn_pa - pc_pa)
    r = max(1.0, r_m)
    ratio = (rmw_m / r) ** b
    exp_term = math.exp(-ratio)
    pressure = pc_pa + dp * exp_term
    f = 2.0 * _OMEGA * math.sin(math.radians(abs(lat_deg)))
    inner = ratio * (b / _RHO_AIR) * dp * exp_term + (r * f / 2.0) ** 2
    vg = math.sqrt(max(0.0, inner)) - r * f / 2.0
    return max(0.0, vg), pressure


def build_sflux_grid(
    bbox: tuple[float, float, float, float], pad_deg: float, target_res_deg: float
) -> tuple[Any, Any]:
    """A regular lon/lat grid (2D lon, lat arrays) covering ``bbox`` padded by
    ``pad_deg``. Resolution is coarsened so neither axis exceeds ~120 points (an
    sflux forcing grid, not the solve mesh)."""
    import numpy as np

    west, south, east, north = bbox
    west -= pad_deg
    south -= pad_deg
    east += pad_deg
    north += pad_deg
    nx = int(max(8, min(120, round((east - west) / max(1e-6, target_res_deg)) + 1)))
    ny = int(max(8, min(120, round((north - south) / max(1e-6, target_res_deg)) + 1)))
    lon1d = np.linspace(west, east, nx)
    lat1d = np.linspace(south, north, ny)
    lon2d, lat2d = np.meshgrid(lon1d, lat1d)  # (ny, nx)
    return lon2d, lat2d


def wind_pressure_on_grid(
    lon2d: Any, lat2d: Any, fix: TrackFix
) -> tuple[Any, Any, Any]:
    """Render ``(uwind, vwind, prmsl)`` (each (ny,nx)) for one interpolated fix.

    Symmetric Holland vortex: gradient wind reduced to 10 m, rotated cyclonically
    (counter-clockwise, N. hemisphere) with an inflow angle toward the center.
    """
    import numpy as np

    dp = max(1.0, fix.pn_pa - fix.pc_pa)
    b = holland_B(fix.vmax_ms, dp)
    coslat0 = math.cos(math.radians(fix.lat))
    dx = (lon2d - fix.lon) * _M_PER_DEG_LON * coslat0  # east (m)
    dy = (lat2d - fix.lat) * _M_PER_DEG_LAT  # north (m)
    r = np.sqrt(dx * dx + dy * dy)
    r_safe = np.maximum(r, 1.0)
    ratio = (fix.rmw_m / r_safe) ** b
    exp_term = np.exp(-ratio)
    prmsl = fix.pc_pa + dp * exp_term
    f = 2.0 * _OMEGA * math.sin(math.radians(abs(fix.lat)))
    inner = ratio * (b / _RHO_AIR) * dp * exp_term + (r_safe * f / 2.0) ** 2
    vg = np.sqrt(np.maximum(0.0, inner)) - r_safe * f / 2.0
    v10 = np.maximum(0.0, vg) * _SURFACE_REDUCTION
    # Cyclonic tangential (CCW): (-dy, dx)/r ; inflow toward center: (-dx,-dy)/r.
    ca, sa = math.cos(_INFLOW_ANGLE_RAD), math.sin(_INFLOW_ANGLE_RAD)
    ux = (-dy / r_safe) * ca + (-dx / r_safe) * sa
    uy = (dx / r_safe) * ca + (-dy / r_safe) * sa
    uwind = v10 * ux
    vwind = v10 * uy
    # Calm at the very center (r -> 0): zero wind where r is sub-grid.
    calm = r < 1.0
    uwind = np.where(calm, 0.0, uwind)
    vwind = np.where(calm, 0.0, vwind)
    return uwind, vwind, prmsl


def interpolate_track(track: list[TrackFix], times_hr: Any) -> list[TrackFix]:
    """Linearly interpolate the track to each sflux time (hours since start)."""
    import numpy as np

    t = np.array([f.time_hr for f in track], dtype=float)
    out: list[TrackFix] = []
    lon = np.array([f.lon for f in track])
    lat = np.array([f.lat for f in track])
    pc = np.array([f.pc_pa for f in track])
    vmax = np.array([f.vmax_ms for f in track])
    rmw = np.array([f.rmw_m for f in track])
    pn = np.array([f.pn_pa for f in track])
    for th in np.asarray(times_hr, dtype=float):
        out.append(
            TrackFix(
                time_hr=float(th),
                lon=float(np.interp(th, t, lon)),
                lat=float(np.interp(th, t, lat)),
                pc_pa=float(np.interp(th, t, pc)),
                vmax_ms=float(np.interp(th, t, vmax)),
                rmw_m=float(np.interp(th, t, rmw)),
                pn_pa=float(np.interp(th, t, pn)),
            )
        )
    return out


def write_sflux_air(
    sflux_dir: str | Path,
    track: list[TrackFix],
    mesh_bbox: tuple[float, float, float, float],
    *,
    base_date: tuple[int, int, int, int],
    sim_days: float,
    cadence_hr: float = 1.0,
    pad_deg: float = 2.0,
    target_res_deg: float = 0.08,
    tail_hours: float = 6.0,
) -> HollandField:
    """Author ``sflux/sflux_air_1.0001.nc`` + ``sflux/sflux_inputs.txt``.

    ``base_date`` is ``(year, month, day, hour)`` == the run start (param.nml
    start_*); the file ``time`` axis is days-since-base_date. Returns the rendered
    field's provenance scalars. Raises ``ValueError`` on an empty/degenerate track.
    """
    import netCDF4
    import numpy as np

    if len(track) < 2:
        raise ValueError("Holland sflux needs at least 2 track fixes to interpolate")
    sflux_dir = Path(sflux_dir)
    sflux_dir.mkdir(parents=True, exist_ok=True)

    lon2d, lat2d = build_sflux_grid(mesh_bbox, pad_deg, target_res_deg)
    ny, nx = lon2d.shape
    # Cover [0, sim_days*24 + tail_hours]: sflux MUST extend PAST the run end so
    # SCHISM can bracket-interpolate the wind at the final model step (a sflux
    # ending exactly at the run end aborts the last step -- no forward record).
    end_hr = sim_days * 24.0 + tail_hours
    n_times = max(2, int(round(end_hr / cadence_hr)) + 1)
    times_hr = np.linspace(0.0, end_hr, n_times)
    fixes = interpolate_track(track, times_hr)

    uwind = np.empty((n_times, ny, nx), dtype="f4")
    vwind = np.empty((n_times, ny, nx), dtype="f4")
    prmsl = np.empty((n_times, ny, nx), dtype="f4")
    peak_wind = 0.0
    min_pres = float("inf")
    b_vals: list[float] = []
    for i, fx in enumerate(fixes):
        u, v, p = wind_pressure_on_grid(lon2d, lat2d, fx)
        uwind[i], vwind[i], prmsl[i] = u, v, p
        peak_wind = max(peak_wind, float(np.sqrt(u * u + v * v).max()))
        min_pres = min(min_pres, float(p.min()))
        b_vals.append(holland_B(fx.vmax_ms, max(1.0, fx.pn_pa - fx.pc_pa)))

    path = sflux_dir / "sflux_air_1.0001.nc"
    with netCDF4.Dataset(path, "w", format="NETCDF3_CLASSIC") as ds:
        ds.createDimension("nx_grid", nx)
        ds.createDimension("ny_grid", ny)
        ds.createDimension("time", None)  # UNLIMITED record dim (SCHISM sflux reader)
        v_lon = ds.createVariable("lon", "f4", ("ny_grid", "nx_grid"))
        v_lat = ds.createVariable("lat", "f4", ("ny_grid", "nx_grid"))
        v_time = ds.createVariable("time", "f8", ("time",))
        v_time.units = (
            f"days since {base_date[0]:04d}-{base_date[1]:02d}-{base_date[2]:02d} "
            f"{base_date[3]:02d}:00:00"
        )
        v_time.base_date = np.array(base_date, dtype="i4")
        v_u = ds.createVariable("uwind", "f4", ("time", "ny_grid", "nx_grid"))
        v_v = ds.createVariable("vwind", "f4", ("time", "ny_grid", "nx_grid"))
        v_p = ds.createVariable("prmsl", "f4", ("time", "ny_grid", "nx_grid"))
        v_t = ds.createVariable("stmp", "f4", ("time", "ny_grid", "nx_grid"))
        v_q = ds.createVariable("spfh", "f4", ("time", "ny_grid", "nx_grid"))
        v_lon[:] = lon2d.astype("f4")
        v_lat[:] = lat2d.astype("f4")
        # SCHISM's sflux reader IGNORES the base_date HOUR -- it references file
        # times to MIDNIGHT of the base_date day. The model, however, starts at
        # start_hour == base_date[3]. Shift the written time axis by that hour so
        # sflux-from-midnight aligns with the model clock (else the run walks off
        # the end of the sflux coverage near the end -- an "no appropriate time"
        # abort). times_hr stays the Holland forcing clock (0 == first fix).
        v_time[:] = ((base_date[3] + times_hr) / 24.0).astype("f8")
        v_u[:] = uwind
        v_v[:] = vwind
        v_p[:] = prmsl
        v_t[:] = np.full((n_times, ny, nx), 300.0, dtype="f4")  # 300 K surface air
        v_q[:] = np.full((n_times, ny, nx), 0.018, dtype="f4")  # ~humid tropical
        ds.setncattr("Conventions", "CF-1.0")

    (sflux_dir / "sflux_inputs.txt").write_text(SFLUX_INPUTS_TXT, encoding="utf-8")

    west, south, east, north = (
        float(lon2d.min()), float(lat2d.min()), float(lon2d.max()), float(lat2d.max())
    )
    return HollandField(
        peak_wind_ms=round(peak_wind, 3),
        min_pressure_pa=round(min_pres, 1),
        n_times=n_times,
        n_lon=nx,
        n_lat=ny,
        grid_bbox=(west, south, east, north),
        holland_b_mean=round(float(sum(b_vals) / len(b_vals)), 4),
    )
