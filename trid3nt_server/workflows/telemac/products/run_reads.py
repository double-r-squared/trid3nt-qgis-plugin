"""What a solved run's OWN files say, read on the server.

The worker is the engine room: it runs a steering file and writes what the engine
wrote, and everything derived from those files is read HERE. Every function is
BEST-EFFORT: a parse that fails returns nothing and the primary layer stands."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("trid3nt_server.workflows.telemac.products.run_reads")

__all__ = [
    "continuity_rel_error",
    "engine_demand",
    "gaia_mass_balance",
    "oil_slick_features",
    "outlet_hydrograph",
    "parse_drogues",
    "sediment_scalars",
    "surface_d50_spread",
    "wetted_fraction",
]

#: The depth an element has to hold to count as wet. TELEMAC's own tidal-flat
#: treatment leaves films thinner than this on a drying bar, and counting them as
#: conveyance is what would make the heuristic agree with the domain by
#: construction.
_WET_TOL_M = 0.02

#: GAIA prints its closure once per class under this heading, in kg. The block is
#: cut at the end-of-run marker so a run that printed intermediate balances is
#: read at its FINAL one.
_GAIA_HEADING = "FINAL MASS-BALANCE OF SEDIMENTS"
_GAIA_BLOCK_END = r"END OF TIME LOOP|CORRECT END OF RUN"
#: The listing label -> the metric name, and how many places it survives at.
_GAIA_FIELDS: tuple[tuple[str, str, int], ...] = (
    ("CUMULATED DEPOSITION", "sediment_deposited_mass_kg", 4),
    ("CUMULATED EROSION", "sediment_eroded_mass_kg", 4),
    ("CUMULATED BED EVOLUTIONS", "sediment_net_bed_mass_kg", 6),
    ("CUMULATED LOST MASS", "sediment_mass_lost_kg", 8),
)


#: How LECDON asks for a keyword it will not start without. The engine names the
#: keyword itself, on the line the phrase opens or on the ones under it, so what
#: reaches a reader is the engine's own sentence rather than a set this code
#: decided a run needs.
_LECDON_DEMAND = re.compile(
    r"IS MANDATORY|GIVE THE KEY-?WORDS?|GIVE THE CORRESPONDING|GIVE A VALUE"
    r"|NO FRICTION LAW IS PRESCRIBED")
#: Where a demand block ends: the banner the engine stops under.
_PLANTE = "PLANTE:"
#: How many lines under a demand carry its keyword names.
_DEMAND_LINES = 6


def engine_demand(listing_text: str) -> str | None:
    """What the engine ASKED FOR before it stopped, in its own words.

    Only the engine knows which open keyword THIS deck cannot run without."""
    lines = [line.rstrip() for line in (listing_text or "").splitlines()]
    starts = [i for i, line in enumerate(lines) if _LECDON_DEMAND.search(line)]
    if not starts:
        return None
    block: list[str] = []
    for line in lines[starts[-1]:starts[-1] + _DEMAND_LINES]:
        if not line.strip() or _PLANTE in line:
            break
        block.append(line.strip())
    return "; ".join(block) or None


def gaia_mass_balance(listing_text: str) -> dict[str, Any]:
    """GAIA's own closure out of the solver listing - deposited/eroded/net/lost kg.

    The authoritative masses; a field the listing did not print is absent."""
    # ZERO HAS NO SIGN, and the sign is fixed HERE so no consumer has to know: a
    # residual the listing prints as a tiny negative rounds to ``-0.0``, which
    # survives ``max(value, 0.0)`` unchanged and would reach the reader as a
    # negative deposited mass beside a map showing deposition. Adding 0.0
    # collapses the negative zero onto the positive one; a genuinely negative
    # mass is untouched.
    start = re.search(_GAIA_HEADING, listing_text or "")
    if start is None:
        return {}
    block = (listing_text or "")[start.end():]
    end = re.search(_GAIA_BLOCK_END, block)
    if end is not None:
        block = block[:end.start()]
    out: dict[str, Any] = {}
    for label, name, places in _GAIA_FIELDS:
        found = re.search(re.escape(label) + r"\s*=\s*([-\d.Ee+]+)", block)
        if found is None:
            continue
        try:
            out[name] = round(float(found.group(1)), places) + 0.0
        except ValueError:
            continue
    return out


def surface_d50_spread(gaia_slf: str | Path) -> dict[str, Any]:
    """The SORTING signature: the spread of the bed's surface mean diameter, in um.

    A single-class bed cannot sort, so its range is structurally zero."""
    import numpy as np

    from trid3nt_server.workflows.telemac.modules.outputs import read_selafin

    slf = read_selafin(str(gaia_slf))
    picked = next((v for v in slf["varnames"]
                   if "DIAMETER" in v.upper() or v.strip().upper().startswith("D50")),
                  None)
    frames = slf["data"].get(picked) if picked is not None else None
    if frames is None or not len(frames):
        return {}
    values = np.asarray(frames[-1], dtype=float)
    values = values[np.isfinite(values) & (values > 0.0)]
    if not values.size:
        return {}
    microns = values * 1.0e6
    return {"sediment_surface_d50_min_um": round(float(microns.min()), 1),
            "sediment_surface_d50_max_um": round(float(microns.max()), 1),
            "sediment_surface_d50_range_um":
                round(float(microns.max() - microns.min()), 1)}


def sediment_scalars(*, listing_text: str, injected_kg: float,
                     n_classes: int = 1,
                     gaia_slf: str | Path | None = None) -> dict[str, Any]:
    """Every sediment number a GAIA run reports, off its own listing and result.

    The deposit fraction is clamped into [0, 1]; sorting needs two classes."""
    stats = gaia_mass_balance(listing_text)
    stats["sediment_injected_kg"] = round(float(injected_kg), 3)
    net = stats.get("sediment_net_bed_mass_kg")
    if net is not None and injected_kg > 0.0:
        stats["sediment_deposit_fraction"] = round(
            min(max(float(net) / float(injected_kg), 0.0), 1.0), 4)
    if n_classes >= 2:
        stats["sediment_n_classes"] = n_classes
        if gaia_slf is not None:
            try:
                stats.update(surface_d50_spread(gaia_slf))
            except Exception as exc:  # noqa: BLE001 -- a bonus scalar never voids a run
                logger.warning("gaia surface d50 read failed (%s)", exc)
    return stats


def parse_drogues(path: str | Path) -> list[tuple[float, list[tuple[float, float]]]]:
    """The TecPlot ASCII drogues track -> ``[(t_s, [(x, y), ...]), ...]``.

    One ZONE per written instant; an unparsable row is skipped, not fatal."""
    zones: list[tuple[float, list[tuple[float, float]]]] = []
    time_s: float | None = None
    points: list[tuple[float, float]] = []
    for line in Path(path).read_text(errors="replace").splitlines():
        if line.startswith("ZONE"):
            if time_s is not None:
                zones.append((time_s, points))
            stamp = re.search(r"SOLUTIONTIME=\s*([\d.]+)", line)
            time_s, points = (float(stamp.group(1)) if stamp else 0.0), []
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            try:
                points.append((float(parts[1]), float(parts[2])))
            except ValueError:
                continue
    if time_s is not None:
        zones.append((time_s, points))
    return zones


#: How many snapshots the renderable slick keeps: the release, the middle of the
#: run and the end. Every instant would be one layer per written frame.
_SLICK_SNAPSHOTS = 3


def oil_slick_features(drogues_path: str | Path, *, utm_epsg: int
                       ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The oil track -> ``(particles, slick, stats)`` in lon/lat.

    ``oil_particles_exited_domain`` is released minus remaining at the outlet."""
    import numpy as np
    from pyproj import Transformer

    zones = parse_drogues(drogues_path)
    to_lonlat = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True).transform
    snapshots: list[dict[str, Any]] = []
    for time_s, points in zones:
        if not points:
            snapshots.append({"t_s": time_s, "lonlat": []})
            continue
        xs, ys = zip(*points)
        lons, lats = to_lonlat(xs, ys)
        snapshots.append({"t_s": time_s,
                          "lonlat": [[round(a, 6), round(b, 6)]
                                     for a, b in zip(lons, lats)]})

    keep = ([snapshots[0], snapshots[len(snapshots) // 2], snapshots[-1]]
            if len(snapshots) >= _SLICK_SNAPSHOTS else snapshots)
    slick = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "MultiPoint", "coordinates": snap["lonlat"]},
         "properties": {"kind": "oil-slick", "t_s": snap["t_s"],
                        "n": len(snap["lonlat"])}}
        for snap in keep if snap["lonlat"]]}

    stats: dict[str, Any] = {}
    if zones and zones[0][1] and zones[-1][1]:
        first = np.mean(zones[0][1], axis=0)
        last = np.mean(zones[-1][1], axis=0)
        released, remaining = len(zones[0][1]), len(zones[-1][1])
        stats = {
            "oil_particles": remaining,
            "oil_particles_released": released,
            "oil_particles_exited_domain": max(0, released - remaining),
            "oil_snapshots": len(zones),
            "oil_drift_m": round(float(np.hypot(*(last - first))), 1),
        }
    return {"snapshots": snapshots}, slick, stats


#: TELEMAC-2D closes its water-volume balance once per listing period. The block
#: OPENS with its heading, carries one flux line per liquid boundary, and closes
#: with the relative error stamped with the time the whole block belongs to. The
#: heading is read too, so a tracer balance's own flux lines - printed under a
#: different heading and closed with a different error - cannot leak into it.
_BALANCE_HEAD = r"BALANCE OF WATER VOLUME"
_FLUX_BOUNDARY = r"FLUX BOUNDARY\s+(\d+)\s*:\s*([-+\d.Ee]+)"
_BALANCE_TIME = r"RELATIVE ERROR IN VOLUME AT T\s*=\s*([-+\d.Ee]+)\s*S"
_VOLUME_ERROR = _BALANCE_TIME + r"\s*:\s*([-+\d.Ee]+)"


def continuity_rel_error(listing_text: str) -> float | None:
    """The engine's OWN volume closure, off the last one it printed.

    The solver prints one every listing period; the LAST figure is the run's."""
    found = re.findall(_VOLUME_ERROR, listing_text or "")
    if not found:
        return None
    try:
        return float(found[-1][1])
    except ValueError:
        return None


def outlet_hydrograph(listing_text: str, *, boundary: int) -> dict[str, Any]:
    """Discharge through one LIQUID BOUNDARY, as the engine itself measured it.

    TELEMAC integrates the flux across every liquid boundary as part of its own
    water-volume balance and prints the result each listing period; that number
    is the hydrograph, never a server-side re-derivation.

    ``boundary`` is the 1-based liquid-boundary number - the position the role
    takes in the accepted topology's ``liquid_boundary_order``, which is the
    order the solver numbers its boundaries in.

    ONE SIGN CONVENTION, stated here and nowhere else: **outflow is positive**.
    The listing's own convention is the opposite, so it is negated once, at the
    read, and every consumer downstream reads a rising outflow as a rising number.
    """
    import numpy as np

    series: list[tuple[float, float]] = []
    pending: dict[int, float] | None = None
    for line in (listing_text or "").splitlines():
        if re.search(_BALANCE_HEAD, line):
            pending = {}
            continue
        if pending is None:
            continue
        flux = re.search(_FLUX_BOUNDARY, line)
        if flux is not None:
            try:
                pending[int(flux.group(1))] = float(flux.group(2))
            except ValueError:
                continue
            continue
        stamp = re.search(_BALANCE_TIME, line)
        if stamp is None:
            continue
        if int(boundary) in pending:
            try:
                series.append((float(stamp.group(1)), -pending[int(boundary)]))
            except ValueError:
                pass
        pending = None
    if not series:
        return {}

    times = np.asarray([t for t, _q in series], dtype=float)
    flows = np.asarray([q for _t, q in series], dtype=float)
    volume = float(np.trapezoid(flows, times)) if times.size > 1 else 0.0
    peak = int(np.argmax(flows))
    return {
        "t_s": [round(float(t), 3) for t in times],
        "q_m3s": [round(float(q), 6) for q in flows],
        "peak_discharge_m3s": round(float(flows[peak]), 6),
        "peak_discharge_time_s": round(float(times[peak]), 3),
        # A maximum that lands on the LAST sample is not a peak, it is where the
        # window closed: the series was still rising when the run ended, so the
        # real peak, the runoff volume and every ratio built on them are floors,
        # not measurements. The read says so; nothing downstream has to infer it.
        "peak_is_window_truncated": bool(times.size > 1 and peak == times.size - 1),
        "runoff_volume_m3": round(max(volume, 0.0), 3),
        "outlet_boundary": int(boundary),
    }


def wetted_fraction(mesh: Mapping[str, Any], *, wet_tol_m: float = _WET_TOL_M
                    ) -> dict[str, Any]:
    """How much of the solved domain still held water at the final frame.

    By element, a HEURISTIC; ``mesh`` is the record the postprocess ALREADY read."""
    # The reach domain is the mapped ACTIVE CHANNEL, which at bankfull includes
    # the gravel bars a low flow leaves dry: TELEMAC wets and dries them natively,
    # so a low-flow run is correct and its conveyance width is still narrower than
    # the domain it solved on. Nothing about the result says so, and a reader
    # looking at a ribbon inside a wider mesh has no number to read it against.
    import numpy as np

    # SELAFIN pads a variable name to 32 chars with its unit trailing ('WATER
    # DEPTH     M'), so an exact-key lookup never matches a real result.
    picked = next((v for v in mesh["varnames"]
                   if v.strip().upper().startswith("WATER DEPTH")), None)
    depth = mesh["data"].get(picked) if picked is not None else None
    ikle = np.asarray(mesh["ikle"], dtype=int)
    if depth is None or np.asarray(depth).size == 0 or ikle.size == 0:
        return {}
    x, y = np.asarray(mesh["x"]), np.asarray(mesh["y"])
    a, b, c = ikle[:, 0], ikle[:, 1], ikle[:, 2]
    area = 0.5 * np.abs((x[b] - x[a]) * (y[c] - y[a])
                        - (x[c] - x[a]) * (y[b] - y[a]))
    final = np.asarray(depth)[-1]
    wet = area[final[ikle].mean(axis=1) > float(wet_tol_m)]
    total = float(area.sum())
    if total <= 0.0:
        return {}
    return {"mesh_area_m2": total, "wet_area_m2": float(wet.sum()),
            "wetted_fraction": round(float(wet.sum()) / total, 4),
            "wet_tol_m": float(wet_tol_m)}
