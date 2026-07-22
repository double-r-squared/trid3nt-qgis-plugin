"""Parametric hurricane wind + pressure forcing for SFINCS via a Delft3D spiderweb.

The COASTAL SFINCS wind track: turn an IBTrACS best track (the existing
``fetch_storm_tracks`` tool, extended to carry RMW / R34 / POCI per fix) into a
Holland-1980/2010 polar wind + pressure-drop field, written as a native Delft3D
``meteo_on_spiderweb_grid`` ASCII ``.spw`` file, re-anchored onto the deck
``SFINCS_TREF`` window. SFINCS reads the spw via the ``spwfile`` + ``utmzone``
sfincs.inp keywords (first-class ``SfincsInput`` attrs in hydromt_sfincs 1.2.2)
and converts the lon/lat eye coordinates to the AOI UTM grid internally.

DE-RISK (2026-07-19, recorded in the job smoke): the exact sfincs.inp lines the
``deltares/sfincs-cpu:sfincs-v2.3.3`` binary accepted, byte-for-byte, are::

    spwfile          = sfincs.spw
    utmzone          = 16n
    baro             = 1

and the container log confirmed uptake with::

    Info    : reading spiderweb file sfincs.spw
    Info    : converting spiderweb coordinates to UTM  zone 16n
    Info    : turning on wind
    Info    : turning on atmospheric pressure

with a nonzero water response (zsmax ~0.5 m over a 40 km / 4 h wind-only smoke).
The emitter (``sfincs_builder._emit_spiderweb_config``) MUST reproduce those
lines and copy the ``.spw`` into the deck under the bare relative name.

Design authority: ``reports/design/module-wave-scoping-2026-07-19.md`` (##
Parametric hurricane wind / spiderweb (SFINCS)).

Zero new runtime deps: the Holland profile + spw writer are hand-rolled pure
python (math + numpy for the resample); ``cht_cyclones`` is a DEV-ONLY
cross-check, never imported at runtime.

HONESTY (Invariant 7): IBTrACS RMW / R34 are frequently blank for older or
weaker fixes. When blank we fall back to the Knaff-Zehr (2007) RMW parametric
and a Holland-2008 B estimate; every fallback is surfaced in the returned
``provenance`` dict (never fabricated as observed). The 1-min -> 10-min wind
averaging factor (0.93) is applied EXPLICITLY and logged; USA_WIND is 1-min
sustained and feeding it unconverted biases the field high ~7-10 percent.
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any

# SFINCS_TREF is the deck reference instant (2026-01-01) that
# ``_generate_hydromt_yaml_config`` writes as the deck ``tref``/``tstart``. The
# spw TIME lines MUST be minutes-since-this-instant or the wind silently clips
# out of the simulation window (job-0248 class). Import the ONE canonical value.
from .sfincs_forcing_adapter import SFINCS_TREF

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Physical constants
# --------------------------------------------------------------------------- #

#: knots -> m/s.
KT_TO_MS: float = 0.514444
#: nautical mile -> m.
NM_TO_M: float = 1852.0
#: 1-min sustained -> 10-min sustained averaging factor (WMO/Harper 2010). The
#: IBTrACS USA_WIND column is a 1-min maximum sustained wind; SFINCS/Holland
#: expect a ~10-min-averaged surface wind. Applied EXPLICITLY + logged.
ONE_MIN_TO_TEN_MIN: float = 0.93
#: near-surface air density (kg/m3) used in the Holland B estimate + profile.
RHO_AIR: float = 1.15
#: standard sea-level pressure (Pa) used as the ambient pn when USA_POCI blank.
PN_DEFAULT_PA: float = 101300.0
#: earth angular velocity (rad/s) for the Coriolis parameter.
_OMEGA: float = 7.2921e-5

# --------------------------------------------------------------------------- #
# spw grid geometry (Delft3D meteo_on_spiderweb_grid)
# --------------------------------------------------------------------------- #

#: directional spokes (columns) around the eye.
DEFAULT_N_SPOKES: int = 36
#: radial rings (rows) out to spw_radius.
DEFAULT_N_RINGS: int = 100
#: spiderweb radius (m) - out past the 34 kt wind field for a shelf-scale storm.
DEFAULT_SPW_RADIUS_M: float = 500_000.0
#: spw resample step (minutes) - 30 min matches the coastal output cadence.
DEFAULT_STEP_MIN: float = 30.0
#: inflow angle over water (deg) rotated into the tangential wind (Sobey/Bretschneider).
DEFAULT_INFLOW_DEG: float = 20.0
#: storm-translation asymmetry weight (adds the forward-motion vector on the
#: right of the track, scaled by local wind fraction). ~0.6 is the standard
#: fraction (Lin & Chavas 2012) that produces the right-of-eye surge signature.
DEFAULT_ASYM_WEIGHT: float = 0.6
#: FALLBACK landfall window (hours) used ONLY when the caller passes neither an
#: explicit ``window_hr`` NOR a positive ``deck_sim_hours``. By default the
#: window tracks ``deck_sim_hours`` so landfall (anchored ~``landfall_frac``
#: through) stays INSIDE the simulated deck window - a 48 h window over a 24 h
#: deck pushes landfall (~1728 min) past the deck end (1440 min) and the peak
#: surge / right-of-eye asymmetry never gets simulated (silent underestimate).
DEFAULT_WINDOW_HR: float = 48.0
#: bare relative filename the spwfile keyword references (deck-local; must sit
#: in the rundir alongside sfincs.inp). Proven byte-for-byte in the docker smoke.
SPW_FILENAME: str = "sfincs.spw"


class SpiderwebError(RuntimeError):
    """Raised on an unrecoverable spiderweb build failure (no usable fixes,
    a deck-window / spw-window mismatch, or a write error). Surfaced by
    ``model_flood_scenario`` as a typed failed envelope (never silent)."""


@dataclass(frozen=True)
class SpiderwebResult:
    """The built spiderweb: local spw path + the sfincs.inp knobs + provenance.

    - ``spw_path`` - the ASCII .spw on the local filesystem (copied into the
      deck dir by ``build_sfincs_model`` before the manifest glob).
    - ``spw_filename`` - the bare relative name the ``spwfile`` keyword uses.
    - ``utmzone`` - the SFINCS ``utmzone`` value (e.g. ``"16n"``) derived from
      the AOI centroid; the grid MUST be built in the matching UTM CRS.
    - ``utm_epsg`` - the matching projected EPSG (e.g. ``32616``) the spiderweb
      branch passes as the ``BuildOptions.crs`` override.
    - ``provenance`` - free-form dict echoed into narration; carries the storm
      name / landfall time / peak intensity AND which values were fallback
      (``rmw_source``, ``pn_source``, ``b_source``) so honesty is preserved.
    """

    spw_path: str
    utmzone: str
    utm_epsg: int
    spw_filename: str = SPW_FILENAME
    provenance: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Holland 1980/2010 parametric profile
# --------------------------------------------------------------------------- #


def _coriolis_f(lat_deg: float) -> float:
    """Coriolis parameter (1/s) at latitude (abs, so both hemispheres work)."""
    return 2.0 * _OMEGA * math.sin(math.radians(abs(lat_deg)))


def knaff_zehr_rmw_km(vmax_ms: float, lat_deg: float) -> float:
    """Knaff-Zehr (2007) radius-of-maximum-wind parametric fallback (km).

    Used when USA_RMW is blank. ``vmax_ms`` is the 10-min surface wind; the
    published regression uses kt, so convert back. Floored at 10 km to avoid a
    pathological sub-grid RMW on an intense small-core storm.
    """
    v_kt = vmax_ms / KT_TO_MS
    rmw = (
        218.3784
        - 1.2014 * v_kt
        + (v_kt / 10.9844) ** 2
        - (v_kt / 35.3052) ** 3
        - 145.509 * math.cos(math.radians(lat_deg))
    )
    return max(rmw, 10.0)


def holland_b(vmax_ms: float, pn_pa: float, pc_pa: float) -> float:
    """Holland (2008) B shape-parameter estimate.

    ``B = rho_air * e * vmax^2 / dp`` (dp = pn - pc). Clamped to the physical
    [0.5, 2.5] band. Used when R34 radii are blank (a per-quadrant R34 fit is
    explicitly deferred to v2 - see the design Risks).
    """
    dp = max(pn_pa - pc_pa, 100.0)
    b = RHO_AIR * math.e * (vmax_ms ** 2) / dp
    return min(max(b, 0.5), 2.5)


def holland_profile(
    r_m: float,
    rmw_m: float,
    b: float,
    dp_pa: float,
    pc_pa: float,
    lat_deg: float,
) -> tuple[float, float]:
    """Holland-1980 gradient wind + pressure at radius ``r_m`` (m).

    Returns ``(wind_speed_ms, pressure_pa)``. The gradient-wind form includes
    the Coriolis correction term; at the RMW the exponential factor peaks and
    the wind maximises, decaying monotonically outward - the falsifiable shape
    the smoke checks (peak at ~RMW, decays to near-zero at spw_radius).
    """
    r = max(r_m, 1.0)
    f = _coriolis_f(lat_deg)
    rr = (rmw_m / r) ** b
    p = pc_pa + dp_pa * math.exp(-rr)
    inside = (b * dp_pa / RHO_AIR) * rr * math.exp(-rr) + (r * f / 2.0) ** 2
    v = math.sqrt(max(inside, 0.0)) - (r * f / 2.0)
    return max(v, 0.0), p


def _build_polar_field(
    vmax_ms: float,
    rmw_m: float,
    b: float,
    dp_pa: float,
    pc_pa: float,
    lat_deg: float,
    translation: tuple[float, float],
    *,
    n_spokes: int,
    n_rings: int,
    spw_radius_m: float,
    inflow_deg: float,
    asym_weight: float,
) -> tuple[list[list[float]], list[list[float]], list[list[float]], float]:
    """One time-slice polar field on the spiderweb grid.

    Returns ``(wind_speed, wind_from_direction, p_drop, dr)`` where each field
    is indexed ``[ring][spoke]`` (ring 0 = eye, ring n_rings = spw_radius) and
    ``dr`` is the radial step (m). Spoke ``j`` points at bearing
    ``j*360/n_spokes`` degrees clockwise from north. The tangential wind is
    cyclonic (NH counter-clockwise) with an inflow angle over water; the
    storm-translation vector is added on the right of the track scaled by the
    local wind fraction (the asymmetry uniform wind cannot reproduce).
    """
    tu, tv = translation
    dr = spw_radius_m / n_rings
    speed = [[0.0] * n_spokes for _ in range(n_rings + 1)]
    fromdir = [[0.0] * n_spokes for _ in range(n_rings + 1)]
    pdrop = [[0.0] * n_spokes for _ in range(n_rings + 1)]
    for i in range(n_rings + 1):
        r = i * dr
        v_sym, p = holland_profile(max(r, 1.0), rmw_m, b, dp_pa, pc_pa, lat_deg)
        wind_frac = v_sym / max(vmax_ms, 1e-6)
        for j in range(n_spokes):
            bearing = j * (360.0 / n_spokes)
            th = math.radians(bearing)
            # Northern-Hemisphere cyclonic (COUNTER-clockwise) tangential wind +
            # inward inflow angle over water. At bearing ``th`` (clockwise from
            # north) the outward radial is (sin th, cos th) in (E, N); the CCW
            # tangential is 90 deg to its LEFT, i.e. rotate by ``th - 90``. A
            # ``th + 90`` here would spin the storm CLOCKWISE (Southern-Hemi) and
            # silently invert the right-of-track asymmetry (surge on the wrong
            # side). Subtracting the inflow angle bends the wind inward (toward
            # the low) on every quadrant.
            ang = th - math.radians(90.0 + inflow_deg)
            we = v_sym * math.sin(ang)
            wn = v_sym * math.cos(ang)
            # storm-translation asymmetry (adds forward motion, weighted).
            we += asym_weight * tu * wind_frac
            wn += asym_weight * tv * wind_frac
            spd = math.hypot(we, wn)
            # meteorological FROM-direction: the compass bearing the wind blows
            # from (deg), = the TO-direction rotated 180.
            to_dir = math.degrees(math.atan2(we, wn)) % 360.0
            from_dir = (to_dir + 180.0) % 360.0
            speed[i][j] = spd
            fromdir[i][j] = from_dir
            # p_drop = pn - p(r) = dp * (1 - exp(-rr)); 0 at eye, dp at far field.
            pdrop[i][j] = dp_pa - (p - pc_pa)
    return speed, fromdir, pdrop, dr


# --------------------------------------------------------------------------- #
# Delft3D meteo_on_spiderweb_grid ASCII writer (FileVersion 1.03)
# --------------------------------------------------------------------------- #


def write_spw(
    path: str,
    *,
    eye_lonlat: list[tuple[float, float]],
    times_min: list[float],
    fields: list[tuple[list[list[float]], list[list[float]], list[list[float]]]],
    spw_radius_m: float,
    ref_time: _dt.datetime,
    n_spokes: int,
    n_rings: int,
) -> None:
    """Write the ASCII spw (FileVersion 1.03, n_quantity 3).

    quantity1 = wind_speed (m s-1), quantity2 = wind_from_direction (degree),
    quantity3 = p_drop (Pa). Each TIME block carries the eye lon/lat and the
    three ``n_rings x n_spokes`` quantity blocks (ring 0 / the eye is excluded
    per the Delft3D convention - the first data ring is ring 1). ``times_min``
    are minutes since ``ref_time`` (the deck SFINCS_TREF).
    """
    lines: list[str] = []
    A = lines.append
    A("FileVersion      =    1.03")
    A("Filetype         =    meteo_on_spiderweb_grid")
    A("NODATA_value     =    -999.000")
    A("n_cols           =    %d" % n_spokes)
    A("n_rows           =    %d" % n_rings)
    A("grid_unit        =    m")
    A("spw_radius       =    %.1f" % spw_radius_m)
    A("spw_rad_unit     =    m")
    A("n_quantity       =    3")
    A("quantity1        =    wind_speed")
    A("quantity2        =    wind_from_direction")
    A("quantity3        =    p_drop")
    A("unit1            =    m s-1")
    A("unit2            =    degree")
    A("unit3            =    Pa")
    ref = ref_time.strftime("%Y-%m-%d %H:%M:%S")
    for t_min, (lon, lat), (speed, fromdir, pdrop) in zip(
        times_min, eye_lonlat, fields
    ):
        A("TIME             = %.6f minutes since %s +00:00" % (t_min, ref))
        A("x_spw_eye        =    %.4f" % lon)
        A("y_spw_eye        =    %.4f" % lat)
        A("p_drop_spw_eye   =    %.1f" % 0.0)
        for i in range(1, n_rings + 1):
            A(" ".join("%.4f" % speed[i][j] for j in range(n_spokes)))
        for i in range(1, n_rings + 1):
            A(" ".join("%.4f" % fromdir[i][j] for j in range(n_spokes)))
        for i in range(1, n_rings + 1):
            A(" ".join("%.4f" % pdrop[i][j] for j in range(n_spokes)))
    with open(path, "w", encoding="ascii") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Fix parsing + landfall-window selection + resample
# --------------------------------------------------------------------------- #


def _to_float(v: Any) -> float | None:
    """Blank-tolerant float coercion (mirrors fetch_storm_tracks _blank_to_none_float)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def _parse_iso(s: str | None) -> _dt.datetime | None:
    """Parse an IBTrACS ISO_TIME (``YYYY-MM-DD HH:MM:SS``) to a UTC datetime."""
    if not s:
        return None
    txt = str(s).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(txt, fmt).replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
    return None


def read_ibtracs_fixes_from_fgb(fgb_path: str) -> list[dict[str, Any]]:
    """Read a fetch_storm_tracks POINTS FlatGeobuf into a list of fix dicts.

    Each returned dict carries ``lon``/``lat``/``iso_time``/``wind_kt``/
    ``pres_mb`` plus the radii columns fetch_storm_tracks now emits
    (``rmw_nmi``/``poci_mb``/``roci_nmi``/``r34_*_nmi``). geopandas + pyogrio
    are the same read path the tool uses to WRITE the FGB.
    """
    try:
        import geopandas as gpd  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise SpiderwebError(
            f"geopandas unavailable for reading the storm-track FGB: {exc}"
        ) from exc
    gdf = gpd.read_file(fgb_path, engine="pyogrio")
    fixes: list[dict[str, Any]] = []
    for _, row in gdf.iterrows():
        geom = row.get("geometry")
        if geom is None:
            continue
        try:
            lon, lat = float(geom.x), float(geom.y)
        except Exception:  # noqa: BLE001 - non-point geometry
            continue
        fixes.append(
            {
                "lon": lon,
                "lat": lat,
                "iso_time": row.get("iso_time"),
                "name": row.get("name"),
                "sid": row.get("sid"),
                "wind_kt": _to_float(row.get("wind_kt")),
                "pres_mb": _to_float(row.get("pres_mb")),
                "rmw_nmi": _to_float(row.get("rmw_nmi")),
                "poci_mb": _to_float(row.get("poci_mb")),
                "roci_nmi": _to_float(row.get("roci_nmi")),
                "r34_ne_nmi": _to_float(row.get("r34_ne_nmi")),
                "r34_se_nmi": _to_float(row.get("r34_se_nmi")),
                "r34_sw_nmi": _to_float(row.get("r34_sw_nmi")),
                "r34_nw_nmi": _to_float(row.get("r34_nw_nmi")),
            }
        )
    return fixes


def _clean_sort_fixes(fixes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop fixes with no parseable time/position and sort chronologically."""
    out = []
    for f in fixes:
        t = _parse_iso(f.get("iso_time"))
        lon = _to_float(f.get("lon"))
        lat = _to_float(f.get("lat"))
        if t is None or lon is None or lat is None:
            continue
        g = dict(f)
        g["_t"] = t
        g["lon"] = lon
        g["lat"] = lat
        out.append(g)
    out.sort(key=lambda g: g["_t"])
    return out


def _landfall_index(fixes: list[dict[str, Any]]) -> int:
    """Index of the landfall-proxy fix = MIN central pressure (peak intensity).

    Central pressure is the more reliable intensity proxy than USA_WIND (which
    is more often blank). Falls back to MAX wind, then the track midpoint.
    """
    press = [(_to_float(f.get("pres_mb")), i) for i, f in enumerate(fixes)]
    press = [(p, i) for p, i in press if p is not None]
    if press:
        return min(press)[1]
    winds = [(_to_float(f.get("wind_kt")), i) for i, f in enumerate(fixes)]
    winds = [(w, i) for w, i in winds if w is not None]
    if winds:
        return max(winds)[1]
    return len(fixes) // 2


def _select_window(
    fixes: list[dict[str, Any]],
    *,
    window_hr: float,
    landfall_frac: float,
) -> list[dict[str, Any]]:
    """Select the fixes inside the landfall window (landfall ~``landfall_frac``
    through). Returns >= 2 fixes (raises if the track is too short)."""
    if len(fixes) < 2:
        raise SpiderwebError(
            "storm track has fewer than 2 usable fixes - cannot build a "
            "time-varying spiderweb (widen the year range / bbox)."
        )
    lf = _landfall_index(fixes)
    lf_time = fixes[lf]["_t"]
    start = lf_time - _dt.timedelta(hours=window_hr * landfall_frac)
    end = lf_time + _dt.timedelta(hours=window_hr * (1.0 - landfall_frac))
    win = [f for f in fixes if start <= f["_t"] <= end]
    if len(win) < 2:
        # Window too tight for the fix cadence - fall back to the full track.
        win = list(fixes)
    return win


def _resample_30min(
    win: list[dict[str, Any]], *, step_min: float
) -> list[dict[str, Any]]:
    """Resample the window to ``step_min`` cadence: LINEAR interp on eye lon/lat
    and on intensity (wind_kt / pres_mb). Radii are carried from the nearest
    fix (they change slowly). numpy does the interpolation."""
    import numpy as np

    t0 = win[0]["_t"]
    tsec = np.array([(f["_t"] - t0).total_seconds() for f in win], dtype=float)
    lon = np.array([f["lon"] for f in win], dtype=float)
    lat = np.array([f["lat"] for f in win], dtype=float)

    def _series(key: str, fallback: float) -> "np.ndarray":
        vals = [_to_float(f.get(key)) for f in win]
        # forward/back-fill blanks so interp never sees NaN.
        last = fallback
        filled = []
        for v in vals:
            if v is not None:
                last = v
            filled.append(last)
        # back-fill the leading Nones
        nxt = fallback
        for k in range(len(filled) - 1, -1, -1):
            if vals[k] is not None:
                nxt = vals[k]
            elif filled[k] == fallback:
                filled[k] = nxt
        return np.array(filled, dtype=float)

    wind = _series("wind_kt", 35.0)
    pres = _series("pres_mb", 1005.0)

    step_s = step_min * 60.0
    n = int(math.floor(tsec[-1] / step_s)) + 1
    grid = np.arange(n) * step_s
    out: list[dict[str, Any]] = []
    for gs in grid:
        lo = float(np.interp(gs, tsec, lon))
        la = float(np.interp(gs, tsec, lat))
        wk = float(np.interp(gs, tsec, wind))
        pm = float(np.interp(gs, tsec, pres))
        # nearest source fix for the slowly-varying radii.
        near = win[int(np.argmin(np.abs(tsec - gs)))]
        out.append(
            {
                "_t": t0 + _dt.timedelta(seconds=float(gs)),
                "minutes": gs / 60.0,
                "lon": lo,
                "lat": la,
                "wind_kt": wk,
                "pres_mb": pm,
                "rmw_nmi": _to_float(near.get("rmw_nmi")),
                "poci_mb": _to_float(near.get("poci_mb")),
            }
        )
    return out


def _translation_vector(
    prev: dict[str, Any], cur: dict[str, Any]
) -> tuple[float, float]:
    """Storm forward-motion vector (m/s, east/north) between two 30-min fixes."""
    dt_s = (cur["_t"] - prev["_t"]).total_seconds()
    if dt_s <= 0:
        return (0.0, 0.0)
    lat0 = math.radians(0.5 * (prev["lat"] + cur["lat"]))
    de = (cur["lon"] - prev["lon"]) * 111_320.0 * math.cos(lat0)
    dn = (cur["lat"] - prev["lat"]) * 110_540.0
    return (de / dt_s, dn / dt_s)


def _utm_from_lon_lat(lon: float, lat: float) -> tuple[str, int]:
    """AOI-centroid UTM zone -> (utmzone string e.g. '16n', EPSG e.g. 32616).

    The spiderweb eye coords are lon/lat; SFINCS converts them to the grid UTM.
    The ``utmzone`` value the v2.3.3 binary accepted is ``<zone><hemi>`` with a
    LOWERCASE hemisphere letter (proven ``16n`` in the docker smoke).
    """
    zone = int((lon + 180.0) / 6.0) + 1
    zone = max(1, min(60, zone))
    hemi = "n" if lat >= 0 else "s"
    epsg = (32600 if lat >= 0 else 32700) + zone
    return f"{zone}{hemi}", epsg


# --------------------------------------------------------------------------- #
# Public build entry points
# --------------------------------------------------------------------------- #


def build_spiderweb_from_fixes(
    fixes: list[dict[str, Any]],
    aoi_bbox: tuple[float, float, float, float],
    *,
    out_dir: str,
    deck_sim_hours: float,
    window_hr: float | None = None,
    landfall_frac: float = 0.6,
    step_min: float = DEFAULT_STEP_MIN,
    n_spokes: int = DEFAULT_N_SPOKES,
    n_rings: int = DEFAULT_N_RINGS,
    spw_radius_m: float = DEFAULT_SPW_RADIUS_M,
    tref: _dt.datetime = SFINCS_TREF,
    storm_name: str | None = None,
) -> SpiderwebResult:
    """Build a spiderweb .spw from a list of IBTrACS fix dicts (offline-testable).

    Steps: clean/sort -> landfall-window select (landfall ~``landfall_frac``
    through a ``window_hr`` window) -> 30-min resample -> Holland field per
    step -> re-anchor TIME to ``tref`` (first window fix -> minute 0) -> write
    the ASCII spw into ``out_dir``. The utm zone comes from the AOI centroid.

    WINDOW SIZING (the design's #1 hazard - silent surge underestimate): by
    default ``window_hr`` TRACKS ``deck_sim_hours`` so the landfall (anchored
    ~``landfall_frac`` through the window) always falls INSIDE the simulated
    ``[0, deck_sim_hours*60]`` deck window. If the window were fixed larger than
    the deck (the old 48 h default over a 24 h deck) landfall re-anchors to
    ~``landfall_frac*window_hr*60`` min - PAST the deck end - so the pre-landfall
    ramp overlaps (the overlap assert passes, false confidence) but the peak
    surge + right-of-eye asymmetry (the entire falsifiable observable) is never
    simulated. An explicit ``window_hr`` is still honoured, but then the landfall
    assert below hard-fails if it would push landfall out of the deck window.

    DECK-WINDOW ASSERTS (job-0248 class): the re-anchored spw spans
    ``[0, span_min]`` and the deck runs ``[0, deck_sim_hours*60]``. We raise a
    typed ``SpiderwebError`` (rather than emit a dead deck) if (a) the spw does
    not overlap the deck window at all, OR (b) the landfall minute falls outside
    the deck window - i.e. peak intensity is never actually simulated.
    """
    # Default the window to the deck length so landfall stays inside the deck
    # window; fall back to the fixed reference window only when neither is usable.
    if window_hr is None:
        window_hr = (
            float(deck_sim_hours)
            if deck_sim_hours and float(deck_sim_hours) > 0.0
            else DEFAULT_WINDOW_HR
        )
    fixes = _clean_sort_fixes(fixes)
    if len(fixes) < 2:
        raise SpiderwebError(
            "no usable IBTrACS fixes (need >= 2 with time + position) to build "
            "a spiderweb."
        )
    win = _select_window(fixes, window_hr=window_hr, landfall_frac=landfall_frac)
    samples = _resample_30min(win, step_min=step_min)

    # --- deck-window overlap assert -------------------------------------------
    span_min = samples[-1]["minutes"]
    deck_min = float(deck_sim_hours) * 60.0
    if span_min <= 0.0 or deck_min <= 0.0:
        raise SpiderwebError(
            f"degenerate spw/deck window (spw span {span_min:.1f} min, deck "
            f"{deck_min:.1f} min)."
        )
    # The spw starts at minute 0 (== tref == deck tstart); require it to reach
    # into the deck window (overlap) - a spw that ends before the deck starts,
    # or a deck shorter than a token slice, means the wind never forces.
    overlap = min(span_min, deck_min) - 0.0
    if overlap <= step_min:
        raise SpiderwebError(
            f"spw window [{0.0:.0f},{span_min:.0f}] min does not overlap the "
            f"deck window [0,{deck_min:.0f}] min by more than one step "
            f"({step_min:.0f} min) - wind would clip out (job-0248 class). "
            f"Widen duration_hr or the landfall window."
        )
    # --- landfall-inside-deck assert (the #1 hazard) --------------------------
    # Re-anchored minute 0 == win[0] time (== deck tstart); the landfall fix must
    # fall INSIDE the deck window or peak surge + right-of-eye asymmetry (the
    # falsifiable observable) is never simulated even though the ramp overlaps.
    landfall_min = (
        win[_landfall_index(win)]["_t"] - win[0]["_t"]
    ).total_seconds() / 60.0
    if not (0.0 <= landfall_min <= deck_min):
        raise SpiderwebError(
            f"landfall at {landfall_min:.0f} min falls OUTSIDE the deck window "
            f"[0,{deck_min:.0f}] min - peak surge / right-of-eye asymmetry would "
            f"never be simulated (silent underestimate). Extend duration_hr to "
            f">= {landfall_min / 60.0:.1f} h or shrink window_hr so landfall "
            f"(~{landfall_frac:.0%} through the window) lands inside the deck."
        )
    if span_min < deck_min:
        logger.warning(
            "sfincs_spiderweb: spw covers %.0f min but the deck runs %.0f min; "
            "wind holds at the last fix past the spw end (SFINCS clamps).",
            span_min,
            deck_min,
        )

    # --- AOI centroid -> utm zone + intensity provenance ----------------------
    cx = 0.5 * (aoi_bbox[0] + aoi_bbox[2])
    cy = 0.5 * (aoi_bbox[1] + aoi_bbox[3])
    utmzone, utm_epsg = _utm_from_lon_lat(cx, cy)

    rmw_source = "USA_RMW"
    pn_source = "USA_POCI"
    peak_wind_ms = 0.0
    min_pres_mb = 9999.0

    times_min: list[float] = []
    eye: list[tuple[float, float]] = []
    field_slices: list[
        tuple[list[list[float]], list[list[float]], list[list[float]]]
    ] = []
    for k, s in enumerate(samples):
        lat = s["lat"]
        # 1-min USA_WIND (kt) -> 10-min surface wind (m/s) with the EXPLICIT 0.93.
        vmax = (s["wind_kt"] or 35.0) * KT_TO_MS * ONE_MIN_TO_TEN_MIN
        peak_wind_ms = max(peak_wind_ms, vmax)
        pc = (s["pres_mb"] or 1005.0) * 100.0
        min_pres_mb = min(min_pres_mb, s["pres_mb"] or 1005.0)
        # ambient pn: USA_POCI when present else standard atmosphere.
        if s.get("poci_mb"):
            pn = float(s["poci_mb"]) * 100.0
        else:
            pn = PN_DEFAULT_PA
            pn_source = "standard-atmosphere (USA_POCI blank)"
        dp = max(pn - pc, 100.0)
        # RMW: USA_RMW (n mi) else Knaff-Zehr fallback.
        if s.get("rmw_nmi"):
            rmw_m = float(s["rmw_nmi"]) * NM_TO_M
        else:
            rmw_m = knaff_zehr_rmw_km(vmax, lat) * 1000.0
            rmw_source = "Knaff-Zehr (USA_RMW blank)"
        b = holland_b(vmax, pn, pc)
        # translation vector from the neighbouring resampled fix.
        if k + 1 < len(samples):
            trans = _translation_vector(s, samples[k + 1])
        elif k > 0:
            trans = _translation_vector(samples[k - 1], s)
        else:
            trans = (0.0, 0.0)
        speed, fromdir, pdrop, _dr = _build_polar_field(
            vmax, rmw_m, b, dp, pc, lat, trans,
            n_spokes=n_spokes, n_rings=n_rings, spw_radius_m=spw_radius_m,
            inflow_deg=DEFAULT_INFLOW_DEG, asym_weight=DEFAULT_ASYM_WEIGHT,
        )
        times_min.append(s["minutes"])
        eye.append((s["lon"], lat))
        field_slices.append((speed, fromdir, pdrop))

    os.makedirs(out_dir, exist_ok=True)
    spw_path = os.path.join(out_dir, SPW_FILENAME)
    write_spw(
        spw_path,
        eye_lonlat=eye,
        times_min=times_min,
        fields=field_slices,
        spw_radius_m=spw_radius_m,
        ref_time=tref,
        n_spokes=n_spokes,
        n_rings=n_rings,
    )

    provenance = {
        "storm_name": storm_name,
        "landfall_iso": win[_landfall_index(win)]["_t"].strftime("%Y-%m-%dT%H:%MZ"),
        "peak_wind_10min_ms": round(peak_wind_ms, 1),
        "min_central_pressure_mb": round(min_pres_mb, 1),
        "rmw_source": rmw_source,
        "pn_source": pn_source,
        "b_source": "Holland-2008 estimate",
        "averaging_factor_1min_to_10min": ONE_MIN_TO_TEN_MIN,
        "n_steps": len(times_min),
        "step_min": step_min,
        "spw_radius_m": spw_radius_m,
        "utmzone": utmzone,
        "utm_epsg": utm_epsg,
        "spw_span_min": span_min,
        "deck_window_min": deck_min,
        "landfall_min": round(landfall_min, 1),
        "window_hr": window_hr,
    }
    logger.info(
        "sfincs_spiderweb: built %s (%d steps @ %.0f min, peak 10-min wind "
        "%.1f m/s, min pc %.1f mb, RMW=%s, pn=%s, utmzone=%s/EPSG:%d, span "
        "%.0f min vs deck %.0f min) [0.93 1-min->10-min applied]",
        spw_path, len(times_min), step_min, peak_wind_ms, min_pres_mb,
        rmw_source, pn_source, utmzone, utm_epsg, span_min, deck_min,
    )
    return SpiderwebResult(
        spw_path=spw_path,
        utmzone=utmzone,
        utm_epsg=utm_epsg,
        provenance=provenance,
    )


def build_spiderweb_for_storm(
    track_fgb_path: str,
    aoi_bbox: tuple[float, float, float, float],
    *,
    out_dir: str,
    deck_sim_hours: float,
    storm_name: str | None = None,
    **kwargs: Any,
) -> SpiderwebResult:
    """Convenience: read a fetch_storm_tracks POINTS FGB then build the spw.

    ``model_flood_scenario`` uses this after resolving the track (storm_name +
    storm_season via fetch_storm_tracks, or a verbatim storm_track_uri).
    """
    fixes = read_ibtracs_fixes_from_fgb(track_fgb_path)
    return build_spiderweb_from_fixes(
        fixes,
        aoi_bbox,
        out_dir=out_dir,
        deck_sim_hours=deck_sim_hours,
        storm_name=storm_name,
        **kwargs,
    )
