"""Build a SCENARIO earthquake source from the USGS Slab2 subduction-interface
geometry -- the SCENARIO rung of the earthquake-source ladder.

The ladder (established the top two rungs):

  1. MEASURED finite-fault inversion   -- a REAL named ComCat event -> its published
     USGS finite-fault product (``finite_fault.py``). ``basis="measured_inversion"``.
  2. DERIVED single-subfault scaling    -- a real event with no finite-fault product
     -> one Wells & Coppersmith rectangle from Mw. ``basis="derived"``.
  3. SCENARIO Slab2 interface (THIS)    -- a "what if <zone> ruptures at M<x>" ask
     that is NOT a real event. There is no measured slip to fetch, so the geometry
     is taken from the REAL published Slab2 subduction-interface model (depth /
     strike / dip grids) and a target-Mw slip is DISTRIBUTED over it with published
     scaling + taper. ``basis="scenario_slab2"`` -- LOUDLY labeled a scenario so it
     is never confusable with a real event.

Why Slab2 and not the single rectangle: a subduction megathrust follows the CURVED
trench; a single strike-constant rectangle renders as a straight bar (NATE's catch on
the 0226 single-subfault proof). Tiling the real curved interface into many subfaults,
each carrying the Slab2-sampled strike/dip at its own location, produces a deformation
dipole that visibly tracks the trench curve.

The output is a ``FiniteFaultModel`` (the SAME normalized N-subfault table
``finite_fault.py`` produces), so the whole downstream seam is reused verbatim:
``to_csvfault_text`` -> ``stage_finite_fault_csv`` -> the worker's
``dtopotools.CSVFault`` multi-subfault Okada dtopo. NO worker/image change, NO
GeoClawRunArgs contract change -- a scenario is just another way to fill
``finite_fault_uri`` + ``finite_fault_footprint``.

Source truths (the URLs are basis-verified against the Slab2
data release DOI 10.5066/F7PV6JNV -- Hayes et al. 2018, "Slab2, a comprehensive
subduction zone geometry model", Science 362:58-61):
  * ScienceBase parent item ``5aa1b00ee4b0b1c392e86467`` carries one CHILD item per
    subduction zone; each child holds the zone's ``<code>_slab2_dep/str/dip_*.grd``
    GMT-NetCDF grids (depth to slab top [km, negative down], strike [deg], dip [deg]).
  * Zone codes: Cascadia = ``cas``, Alaska-Aleutians = ``alu`` (the two US zones this
    landing exercises). The full Slab2 roster carries ~27 zones on the same pattern.

``parse_slab2_grids`` is a PURE parser (unit-testable on a small NetCDF fixture in the
exact Slab2 layout). ``fetch_slab2_grids`` is the I/O boundary (ScienceBase children
API + file download, ``_http_get`` monkeypatchable). No MinIO / FDSN here.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from trid3nt_server.agent.workflows.geoclaw.finite_fault import (
    FiniteFaultModel,
    FiniteFaultPatch,
)

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.geoclaw.scenario_slab2")

__all__ = [
    "Slab2Grids",
    "Slab2Zone",
    "ScenarioSlab2Error",
    "SLAB2_ZONES",
    "SLAB2_PARENT_ITEM_ID",
    "SLAB2_CHILDREN_URL",
    "RIGIDITY_PA",
    "parse_slab2_grids",
    "fetch_slab2_grids",
    "strasser_interface_dimensions",
    "target_moment_nm",
    "moment_to_mw",
    "resolve_slab2_scenario",
]

#: Slab2 data release ScienceBase parent item (DOI 10.5066/F7PV6JNV). Its children
#: are the per-zone items carrying the grid files. Resolving children by API (rather
#: than hardcoding volatile per-file "file/get" ids) is robust to ScienceBase id
#: churn -- the child TITLE + the ``<code>_slab2_<param>_*.grd`` file-name pattern are
#: the stable keys.
SLAB2_PARENT_ITEM_ID = "5aa1b00ee4b0b1c392e86467"
SLAB2_CHILDREN_URL = (
    "https://www.sciencebase.gov/catalog/items?parentId={pid}"
    "&format=json&max=200&fields=title,files"
)

#: Subduction-interface rigidity (shear modulus) for the moment budget M0 = mu*A*D.
#: 30 GPa is the standard shallow-interface value (Bilek & Lay 1999 note lower mu in
#: the shallowest few km; 30 GPa is the conventional scenario-source choice). Declared
#: so the average-slip the taper normalizes to is auditable.
RIGIDITY_PA: float = 3.0e10


class ScenarioSlab2Error(RuntimeError):
    """A typed Slab2-scenario failure (never a silent dead-end)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class Slab2Zone:
    """A Slab2 subduction zone: its file code + the friendly names that resolve it."""

    code: str
    display: str
    aliases: tuple[str, ...]


#: The US subduction zones this landing exercises (the Slab2 roster is larger; add a
#: row to extend). ``code`` is the ``<code>_slab2_*`` file prefix; ``aliases`` are the
#: lowercased substrings a natural scenario ask ("Cascadia", "the Aleutians") matches.
SLAB2_ZONES: tuple[Slab2Zone, ...] = (
    Slab2Zone("cas", "Cascadia", ("cascadia", "cas", "pacific northwest",
                                  "juan de fuca", "csz")),
    Slab2Zone("alu", "Alaska-Aleutians", ("alaska", "aleutian", "aleutians", "alu",
                                          "alaska peninsula", "alaska-aleutians")),
)


def resolve_zone(name: str) -> Slab2Zone:
    """Resolve a natural zone name/code to a ``Slab2Zone`` (raises if unknown)."""
    key = str(name).strip().lower()
    for z in SLAB2_ZONES:
        if key == z.code or key == z.display.lower() or any(a in key for a in z.aliases):
            return z
    raise ScenarioSlab2Error(
        "SLAB2_ZONE_UNKNOWN",
        f"unknown Slab2 subduction zone {name!r}; supported: "
        + ", ".join(f"{z.display} ({z.code})" for z in SLAB2_ZONES),
    )


@dataclass
class Slab2Grids:
    """The three Slab2 interface grids for one zone on a shared lon/lat mesh.

    ``lon`` / ``lat`` are 1D ascending EPSG:4326 axes (lon normalized to -180..180);
    ``depth_km`` (NEGATIVE down, depth to slab top), ``strike_deg`` (0-360, CW from N),
    and ``dip_deg`` are 2D ``[lat, lon]`` arrays carrying ``NaN`` OUTSIDE the modeled
    slab (Slab2 grids are NaN-padded to a rectangle -- the ragged real edges)."""

    lon: np.ndarray
    lat: np.ndarray
    depth_km: np.ndarray
    strike_deg: np.ndarray
    dip_deg: np.ndarray
    zone_code: str = ""

    def __post_init__(self) -> None:
        for nm, a in (("depth_km", self.depth_km), ("strike_deg", self.strike_deg),
                      ("dip_deg", self.dip_deg)):
            if a.shape != (self.lat.size, self.lon.size):
                raise ScenarioSlab2Error(
                    "SLAB2_GRID_SHAPE_MISMATCH",
                    f"Slab2 {nm} shape {a.shape} != (lat={self.lat.size}, "
                    f"lon={self.lon.size})",
                )

    @property
    def finite_lat_span(self) -> tuple[float, float]:
        """(min_lat, max_lat) of rows that carry ANY finite depth -- the real
        along-trench extent of the modeled interface (NaN edges excluded)."""
        rows = np.where(np.isfinite(self.depth_km).any(axis=1))[0]
        if rows.size == 0:
            raise ScenarioSlab2Error(
                "SLAB2_GRID_ALL_NAN", "Slab2 depth grid is entirely NaN")
        return float(self.lat[rows.min()]), float(self.lat[rows.max()])

    def interface_lon_at(self, lat: float, target_depth_km: float) -> float | None:
        """Trace the interface: the lon where the slab-top depth crosses
        ``target_depth_km`` (negative down) at latitude ``lat``.

        Interpolates the depth-vs-lon profile of the nearest latitude row across only
        its finite (on-slab) cells. Returns ``None`` when that depth is not spanned at
        this latitude (off the modeled interface). This is what makes the tiled fault
        FOLLOW THE CURVE -- the returned lon migrates with both lat and depth."""
        j = int(np.argmin(np.abs(self.lat - lat)))
        prof = self.depth_km[j, :]
        good = np.isfinite(prof)
        if good.sum() < 2:
            return None
        lons = self.lon[good]
        deps = prof[good]  # negative, decreasing (deeper) eastward
        order = np.argsort(deps)  # ascending depth value == deepest-first
        deps_s = deps[order]
        lons_s = lons[order]
        td = float(target_depth_km)
        if td < deps_s[0] or td > deps_s[-1]:
            return None
        return float(np.interp(td, deps_s, lons_s))

    def sample(self, lon: float, lat: float) -> tuple[float, float, float] | None:
        """Nearest-cell (depth_km, strike_deg, dip_deg) at (lon, lat), or ``None``
        when the nearest cell is off-slab (NaN). Nearest-cell (not bilinear) keeps
        NaN edges from bleeding into an averaged value."""
        i = int(np.argmin(np.abs(self.lon - lon)))
        j = int(np.argmin(np.abs(self.lat - lat)))
        d = self.depth_km[j, i]
        s = self.strike_deg[j, i]
        p = self.dip_deg[j, i]
        if not (np.isfinite(d) and np.isfinite(s) and np.isfinite(p)):
            return None
        return float(d), float(s), float(p)


def _open_grd_z(raw: bytes | str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Read one Slab2 GMT-NetCDF ``.grd`` -> (lon_1d, lat_1d, z_2d[lat,lon]).

    Tolerates the coordinate-name variants GMT emits (``x``/``y``/``z`` COARDS or
    ``lon``/``lat``). Longitude is normalized to -180..180 ascending and the columns
    reordered to match. ``raw`` is a path or the file bytes."""
    import xarray as xr

    tmp_path: str | None = None
    if isinstance(raw, (bytes, bytearray)):
        fd, tmp_path = tempfile.mkstemp(prefix="slab2-", suffix=".grd")
        os.close(fd)
        with open(tmp_path, "wb") as fh:
            fh.write(raw)
        ds = xr.open_dataset(tmp_path)
    else:
        ds = xr.open_dataset(raw)
    try:
        lon_name = next((n for n in ("x", "lon", "longitude") if n in ds.variables), None)
        lat_name = next((n for n in ("y", "lat", "latitude") if n in ds.variables), None)
        z_name = next((n for n in ("z", "Band1", "depth", "elevation")
                       if n in ds.variables), None)
        if lon_name is None or lat_name is None or z_name is None:
            raise ScenarioSlab2Error(
                "SLAB2_GRD_NO_VARS",
                f"Slab2 .grd missing lon/lat/z vars (have {list(ds.variables)})",
            )
        lon = np.asarray(ds[lon_name].values, dtype=float)
        lat = np.asarray(ds[lat_name].values, dtype=float)
        z = np.asarray(ds[z_name].values, dtype=float)
    finally:
        ds.close()
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    # z is [lat, lon] in COARDS; if it came transposed, fix by axis length.
    if z.shape == (lon.size, lat.size) and lon.size != lat.size:
        z = z.T
    # Slab2 stores lon 0..360; normalize to -180..180 and reorder ascending.
    lon = np.where(lon > 180.0, lon - 360.0, lon)
    lon_order = np.argsort(lon)
    lon = lon[lon_order]
    z = z[:, lon_order]
    if lat[0] > lat[-1]:  # ensure ascending lat
        lat = lat[::-1]
        z = z[::-1, :]
    return lon, lat, z


def parse_slab2_grids(
    dep: bytes | str, strike: bytes | str, dip: bytes | str, *, zone_code: str = ""
) -> Slab2Grids:
    """Parse the three Slab2 ``.grd`` grids (depth/strike/dip) into a ``Slab2Grids``.

    Each is read on its own lon/lat mesh, then strike/dip are nearest-resampled onto
    the depth grid's mesh (the grids share Slab2's native mesh, so this is a no-op in
    practice, but guards a mismatched pair). Pure -- no I/O. Raises
    ``ScenarioSlab2Error`` on an unreadable grid."""
    lon, lat, dep_z = _open_grd_z(dep)
    _slon, _slat, str_z = _open_grd_z(strike)
    _plon, _plat, dip_z = _open_grd_z(dip)

    def _regrid(src_lon: np.ndarray, src_lat: np.ndarray, src_z: np.ndarray) -> np.ndarray:
        if src_lon.shape == lon.shape and src_lat.shape == lat.shape \
                and np.allclose(src_lon, lon) and np.allclose(src_lat, lat):
            return src_z
        ii = np.clip(np.searchsorted(src_lon, lon), 0, src_lon.size - 1)
        jj = np.clip(np.searchsorted(src_lat, lat), 0, src_lat.size - 1)
        return src_z[np.ix_(jj, ii)]

    return Slab2Grids(
        lon=lon, lat=lat,
        depth_km=dep_z,
        strike_deg=_regrid(_slon, _slat, str_z),
        dip_deg=_regrid(_plon, _plat, dip_z),
        zone_code=zone_code,
    )


def _http_get(url: str) -> bytes:
    """Fetch a URL's bytes (stdlib urllib). Isolated so tests monkeypatch it. The
    ScienceBase children API + the .grd file downloads are public static content
    (no repo catalog driver -- Slab2 is not an FDSN feed)."""
    req = urllib.request.Request(url, headers={"User-Agent": "trid3nt-geoclaw/1"})
    with urllib.request.urlopen(req, timeout=60.0) as resp:  # noqa: S310
        return resp.read()


def _resolve_zone_grid_urls(zone: Slab2Zone, http_get: Any) -> dict[str, str]:
    """Query the ScienceBase children API for the zone's child item and return the
    ``{dep,str,dip}`` .grd download URLs, matched by the ``<code>_slab2_<param>_``
    file-name pattern. Raises ``ScenarioSlab2Error`` when the zone item or a required
    grid is absent."""
    try:
        detail = json.loads(
            http_get(SLAB2_CHILDREN_URL.format(pid=SLAB2_PARENT_ITEM_ID))
            .decode("utf-8", errors="replace")
        )
    except Exception as exc:  # noqa: BLE001 - children API unreachable
        raise ScenarioSlab2Error(
            "SLAB2_CHILDREN_UNREACHABLE",
            f"could not reach the Slab2 ScienceBase children API "
            f"({SLAB2_CHILDREN_URL.format(pid=SLAB2_PARENT_ITEM_ID)}): {exc}",
        ) from exc
    items = detail.get("items") or []
    prefix = f"{zone.code}_slab2_"
    want = {"dep": f"{prefix}dep", "str": f"{prefix}str", "dip": f"{prefix}dip"}
    urls: dict[str, str] = {}
    for it in items:
        for f in it.get("files") or []:
            name = str(f.get("name") or "").lower()
            url = f.get("downloadUri") or f.get("url")
            if not (name.endswith(".grd") and url):
                continue
            for key, pat in want.items():
                if key not in urls and name.startswith(pat):
                    urls[key] = str(url)
    missing = [k for k in ("dep", "str", "dip") if k not in urls]
    if missing:
        raise ScenarioSlab2Error(
            "SLAB2_GRIDS_NOT_FOUND",
            f"Slab2 zone {zone.display} ({zone.code}): missing grid(s) {missing} "
            f"under parent item {SLAB2_PARENT_ITEM_ID}",
        )
    return urls


def fetch_slab2_grids(zone_name: str, *, _http_get_fn: Any = None) -> Slab2Grids:
    """Fetch + parse the Slab2 depth/strike/dip grids for a subduction zone.

    Resolves the zone's ScienceBase child item, downloads its three ``.grd`` grids,
    and parses them. The download is cached under the geoclaw setup cache
    (``TRID3NT_CACHE_DIR``/slab2) so a re-run of the scenario reuses the grids.
    Raises ``ScenarioSlab2Error`` on an unknown zone / unreachable API / missing or
    unparseable grid. ``_http_get_fn`` overrides the URL fetch for offline tests."""
    zone = resolve_zone(zone_name)
    http_get = _http_get_fn or _http_get

    cache_root = os.environ.get("TRID3NT_CACHE_DIR") or tempfile.gettempdir()
    cache_dir = os.path.join(cache_root, "slab2", zone.code)
    os.makedirs(cache_dir, exist_ok=True)
    cached = {p: os.path.join(cache_dir, f"{zone.code}_slab2_{p}.grd")
              for p in ("dep", "str", "dip")}

    def _read(path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    # Cache short-circuit: when all three grids are already cached (or pre-seeded),
    # parse them directly -- NO ScienceBase children-API round-trip. This is both the
    # re-run fast path and the seam that lets a pre-seeded grid drive the run offline.
    if all(os.path.exists(cached[p]) and os.path.getsize(cached[p]) > 0
           for p in ("dep", "str", "dip")):
        grids = parse_slab2_grids(
            _read(cached["dep"]), _read(cached["str"]), _read(cached["dip"]),
            zone_code=zone.code,
        )
        logger.info("fetch_slab2_grids zone=%s served from cache %s",
                    zone.code, cache_dir)
        return grids

    def _grid_bytes(param: str, url: str) -> bytes:
        try:
            data = http_get(url)
        except Exception as exc:  # noqa: BLE001
            raise ScenarioSlab2Error(
                "SLAB2_GRID_FETCH_FAILED",
                f"could not download Slab2 {zone.code}_{param} grid ({url}): {exc}",
            ) from exc
        try:
            with open(cached[param], "wb") as fh:
                fh.write(data)
        except OSError:
            pass
        return data

    urls = _resolve_zone_grid_urls(zone, http_get)
    dep = _grid_bytes("dep", urls["dep"])
    strike = _grid_bytes("str", urls["str"])
    dip = _grid_bytes("dip", urls["dip"])
    grids = parse_slab2_grids(dep, strike, dip, zone_code=zone.code)
    logger.info(
        "fetch_slab2_grids zone=%s lon=[%.2f,%.2f] lat=[%.2f,%.2f] finite_lat=%s",
        zone.code, float(grids.lon.min()), float(grids.lon.max()),
        float(grids.lat.min()), float(grids.lat.max()), grids.finite_lat_span,
    )
    return grids


# ---------------------------------------------------------------------------
# Scaling laws + moment budget (all cited; see module docstring)
# ---------------------------------------------------------------------------

def strasser_interface_dimensions(mw: float) -> tuple[float, float, float]:
    """Rupture (area_km2, length_km, width_km) for a subduction-INTERFACE event of
    moment magnitude ``mw``.

    Strasser, Arango & Bommer (2010), "Scaling of the source dimensions of interface
    and intraslab subduction-zone earthquakes with moment magnitude", SRL 81(6):941-950,
    interface regressions:
        log10(A) = -3.476 + 0.952 Mw   (A in km^2)
        log10(L) = -2.477 + 0.585 Mw   (L in km)
        log10(W) = -0.882 + 0.351 Mw   (W in km)
    (M9.0 -> A~1.24e5 km2, L~614 km, W~189 km -- a Cascadia-class full-margin rupture.)"""
    area = 10.0 ** (-3.476 + 0.952 * mw)
    length = 10.0 ** (-2.477 + 0.585 * mw)
    width = 10.0 ** (-0.882 + 0.351 * mw)
    return area, length, width


def target_moment_nm(mw: float) -> float:
    """Seismic moment M0 [N m] from moment magnitude, Hanks & Kanamori (1979):
    Mw = (log10 M0 - 9.1) / 1.5  (M0 in N m; the IASPEI 9.1 constant)."""
    return 10.0 ** (1.5 * mw + 9.1)


def moment_to_mw(m0_nm: float) -> float:
    """Inverse of ``target_moment_nm`` -- the realized Mw of a summed moment (the
    tiling self-check: the tiled+tapered slip must reproduce the target Mw)."""
    if m0_nm <= 0.0:
        return float("nan")
    return (math.log10(m0_nm) - 9.1) / 1.5


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


#: Tapered-cosine (Tukey) edge-taper fraction: the outer 25% at EACH end cosine-tapers
#: from the flat interior down to zero. A flat-interior edge taper (not a fully-peaked
#: Hann) keeps the scenario peak slip realistic (peak/avg ~1.3-1.5, not ~4x) while
#: still tapering the slip to zero at the rupture edges.
_TUKEY_ALPHA: float = 0.5


def _tukey(n: int, alpha: float = _TUKEY_ALPHA) -> np.ndarray:
    """Tapered-cosine (Tukey 1967) window of length n: flat 1.0 across the interior,
    cosine-tapering to 0 over the outer ``alpha/2`` fraction at each edge. ``alpha=0``
    is a boxcar (uniform), ``alpha=1`` is a full Hann. A single sample returns 1.0."""
    if n <= 1:
        return np.ones(1)
    if alpha <= 0.0:
        return np.ones(n)
    x = np.linspace(0.0, 1.0, n)
    w = np.ones(n)
    lo = x < alpha / 2.0
    hi = x > 1.0 - alpha / 2.0
    w[lo] = 0.5 * (1.0 + np.cos(math.pi * (2.0 * x[lo] / alpha - 1.0)))
    w[hi] = 0.5 * (1.0 + np.cos(math.pi * (2.0 * x[hi] / alpha - 2.0 / alpha + 1.0)))
    return w


def resolve_slab2_scenario(
    zone_name: str,
    target_mw: float,
    *,
    epicenter_lonlat: tuple[float, float] | None = None,
    target_resolution_m: float = 20_000.0,
    top_depth_km: float = 5.0,
    grids: Slab2Grids | None = None,
    _http_get_fn: Any = None,
) -> FiniteFaultModel:
    """Distribute a target-Mw tapered slip over the REAL Slab2 interface -> a scenario
    ``FiniteFaultModel`` (the same N-subfault table the measured rung produces).

    Algorithm:
      1. Rupture area/length/width from Strasser et al. (2010) interface scaling.
      2. Along-strike extent = ``length`` centered on ``epicenter_lonlat`` latitude
         (or the interface's finite-latitude midpoint), CLIPPED to the modeled
         interface. Down-dip extent = a depth band [``top_depth_km``, top+dz] whose
         dip-projected width equals ``width`` (dz = width * sin(mean dip)).
      3. Tile into n_as x n_dd patches of edge ~``target_resolution_m``. Each patch
         centroid is placed by TRACING the interface (``interface_lon_at``) so its lon
         migrates with the curve; strike/dip are Slab2-sampled AT THAT centroid -- the
         curvature is real, not a straight rectangle.
      4. Tapered-cosine (Tukey) tapered slip in both directions (flat interior,
         cosine-tapered to zero at the rupture edges -- a standard scenario slip
         taper that keeps peak/avg realistic), scaled so the summed moment
         Sum(mu * area_i * slip_i) EXACTLY equals M0(target_mw). Off-slab patches (NaN
         sample) are dropped.

    Provenance: ``basis="scenario_slab2"`` naming zone / Mw / the scaling+taper laws --
    LOUDLY a scenario, never a real event. Raises ``ScenarioSlab2Error`` on an empty
    tiling (no on-slab patches)."""
    zone = resolve_zone(zone_name)
    if grids is None:
        grids = fetch_slab2_grids(zone_name, _http_get_fn=_http_get_fn)

    area_km2, length_km, width_km = strasser_interface_dimensions(float(target_mw))

    lat_lo_slab, lat_hi_slab = grids.finite_lat_span
    if epicenter_lonlat is not None:
        lat_c = float(epicenter_lonlat[1])
    else:
        lat_c = 0.5 * (lat_lo_slab + lat_hi_slab)
    half_lat = 0.5 * length_km / 111.0
    lat_lo = max(lat_lo_slab, lat_c - half_lat)
    lat_hi = min(lat_hi_slab, lat_c + half_lat)
    if lat_hi <= lat_lo:
        raise ScenarioSlab2Error(
            "SLAB2_RUPTURE_OFF_INTERFACE",
            f"the {target_mw} rupture centered at lat {lat_c:.2f} does not overlap "
            f"the modeled {zone.display} interface (lat {lat_lo_slab:.2f}..{lat_hi_slab:.2f})",
        )

    # Down-dip depth band: width = dz / sin(dip) -> dz = width * sin(dip). Use the
    # interface's representative dip (median of finite cells) for the band thickness.
    dip_finite = grids.dip_deg[np.isfinite(grids.dip_deg)]
    mean_dip = float(np.median(dip_finite)) if dip_finite.size else 12.0
    dz_km = width_km * math.sin(math.radians(max(mean_dip, 1.0)))
    bot_depth_km = top_depth_km + dz_km

    n_as = max(2, int(round(length_km * 1000.0 / float(target_resolution_m))))
    n_dd = max(2, int(round(width_km * 1000.0 / float(target_resolution_m))))

    lat_samples = np.linspace(lat_lo, lat_hi, n_as)
    depth_samples = np.linspace(top_depth_km, bot_depth_km, n_dd)  # positive km
    taper_as = _tukey(n_as)
    taper_dd = _tukey(n_dd)

    patch_len_m = _haversine_km(0.0, lat_lo, 0.0, lat_hi) / max(n_as - 1, 1) * 1000.0
    patch_wid_m = (dz_km / math.sin(math.radians(max(mean_dip, 1.0)))) \
        / max(n_dd - 1, 1) * 1000.0

    raw: list[tuple[FiniteFaultPatch, float]] = []  # (patch, taper weight)
    for i, lat in enumerate(lat_samples):
        for j, dpos in enumerate(depth_samples):
            lon = grids.interface_lon_at(float(lat), -float(dpos))  # negative down
            if lon is None:
                continue
            s = grids.sample(lon, float(lat))
            if s is None:
                continue
            depth_km, strike_deg, dip_deg = s
            weight = float(taper_as[i] * taper_dd[j])
            raw.append((FiniteFaultPatch(
                lon=float(lon), lat=float(lat), depth_m=abs(depth_km) * 1000.0,
                strike_deg=float(strike_deg), dip_deg=float(dip_deg),
                rake_deg=90.0,  # pure thrust: the megathrust interface rake
                slip_m=0.0,  # filled after moment normalization
                length_m=float(patch_len_m), width_m=float(patch_wid_m),
            ), weight))

    if not raw:
        raise ScenarioSlab2Error(
            "SLAB2_TILING_EMPTY",
            f"no on-slab subfaults tiled for the {zone.display} M{target_mw} scenario "
            f"(lat {lat_lo:.2f}..{lat_hi:.2f}, depth {top_depth_km:.0f}..{bot_depth_km:.0f} km)",
        )

    # Moment normalization: Sum(mu * area_i * (scale*weight_i)) = M0  ->  scale.
    m0 = target_moment_nm(float(target_mw))
    denom = sum(RIGIDITY_PA * p.length_m * p.width_m * w for p, w in raw)
    if denom <= 0.0:
        raise ScenarioSlab2Error(
            "SLAB2_TILING_ZERO_TAPER",
            "the tapered weights summed to zero moment -- degenerate rupture geometry",
        )
    scale = m0 / denom

    patches: list[FiniteFaultPatch] = []
    for p, w in raw:
        p.slip_m = float(scale * w)
        patches.append(p)

    realized_m0 = sum(RIGIDITY_PA * p.length_m * p.width_m * p.slip_m for p in patches)
    realized_mw = moment_to_mw(realized_m0)

    lons = [p.lon for p in patches]
    lats = [p.lat for p in patches]
    footprint = (min(lons), min(lats), max(lons), max(lats))

    model = FiniteFaultModel(
        patches=patches,
        magnitude=float(target_mw),
        event_tag=f"scenario_{zone.code}_M{target_mw:.1f}",
        product_id=f"scenario_slab2_{zone.code}",
        product_url=(
            "https://www.sciencebase.gov/catalog/item/" + SLAB2_PARENT_ITEM_ID
        ),
    )
    model._footprint = footprint
    logger.info(
        "resolve_slab2_scenario zone=%s Mw=%.2f -> %d subfaults, "
        "A=%.0f km2 L=%.0f km W=%.0f km, slip %.2f-%.2f m, realized Mw=%.3f, footprint=%s",
        zone.code, target_mw, model.n_subfaults, area_km2, length_km, width_km,
        model.min_slip_m, model.max_slip_m, realized_mw, footprint,
    )
    return model
