"""What a solved run's OWN files say, read on the server.

The worker is the engine room: it runs a deck and writes what the engine wrote.
Everything a reader has to derive from those files - the sediment closure GAIA
prints into its listing, the floating slick a drogues track describes - is read
HERE, from the artifacts the supervisor uploaded, so the numbers a run narrates
come out of the run's own evidence rather than out of a second computation
inside the container.

Every function is BEST-EFFORT by the products contract: a parse that fails
returns nothing and the primary layer still stands.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.run_reads")

__all__ = [
    "continuity_rel_error",
    "gaia_mass_balance",
    "oil_slick_features",
    "outlet_hydrograph",
    "parse_drogues",
    "sediment_scalars",
    "surface_d50_spread",
]

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

#: mg/L -> kg/m3, which is the unit GAIA's source keyword reads.
_MGL_TO_KGM3 = 1.0e-3


def gaia_mass_balance(listing_text: str) -> dict[str, Any]:
    """GAIA's own closure out of the solver listing - deposited/eroded/net/lost kg.

    These are the authoritative masses: the deposition MAP is a field and this is
    the engine's own accounting of it, so the run narrates from these rather than
    from an integral a reader recomputed. Any field the listing did not print is
    simply absent.
    """
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
            out[name] = round(float(found.group(1)), places)
        except ValueError:
            continue
    return out


def surface_d50_spread(gaia_slf: str | Path) -> dict[str, Any]:
    """The SORTING signature: the spread of the bed's surface mean diameter, in um.

    A single-class bed is uniform, so sorting is structurally impossible and the
    range is zero; a graded mixture armors where the flow steepens (D50 up) and
    fines where it slackens (D50 down). The spread is therefore the number that
    says whether the bed sorted at all.
    """
    import numpy as np

    from trid3nt_server.workflows.telemac.postprocess_telemac import read_selafin

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


def sediment_scalars(*, listing_text: str, deck: Mapping[str, Any],
                     gaia_slf: str | Path | None = None) -> dict[str, Any]:
    """Every sediment number a GAIA run reports, off its own listing and result.

    The INJECTED mass is the deck's own pulse - discharge x concentration x
    window - so the deposit fraction compares what settled against what was put
    in rather than against an assumed load. The fraction is clamped into [0, 1]:
    a net bed gain larger than the injection is measurement noise on a
    supply-limited run, not more sediment than was released.
    """
    stats = gaia_mass_balance(listing_text)
    injected = round(
        float(deck.get("source_q_m3s", 8.0))
        * max(float(deck.get("dye_conc_mgl", 100.0)) * _MGL_TO_KGM3, 0.0)
        * float(deck.get("pulse_window_s", 300.0)), 3)
    stats["sediment_injected_kg"] = injected
    net = stats.get("sediment_net_bed_mass_kg")
    if net is not None and injected > 0.0:
        stats["sediment_deposit_fraction"] = round(
            min(max(float(net) / injected, 0.0), 1.0), 4)
    from .author import normalize_gradation

    classes = normalize_gradation(deck.get("sediment_gradation") or ())
    # A SORTED bed needs a mixture to sort: a single class is uniform by
    # construction, so the spread is only meaningful - and only reported - when
    # the deck declared two classes or more.
    if len(classes) >= 2:
        stats["sediment_n_classes"] = len(classes)
        if gaia_slf is not None:
            try:
                stats.update(surface_d50_spread(gaia_slf))
            except Exception as exc:  # noqa: BLE001 -- a bonus scalar never voids a run
                logger.warning("gaia surface d50 read failed (%s)", exc)
    return stats


def parse_drogues(path: str | Path) -> list[tuple[float, list[tuple[float, float]]]]:
    """The TecPlot ASCII drogues track -> ``[(t_s, [(x, y), ...]), ...]``.

    One ZONE per written instant, its time on the ZONE header. A row the reader
    cannot parse is skipped rather than aborting the track: a truncated final
    line costs one particle, not the whole slick.
    """
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

    ``oil_particles_exited_domain`` is the honest exit accounting: TELEMAC drops
    a float from the track when its trajectory crosses a LIQUID boundary, which
    is the particle LEAVING through the outlet. Released minus remaining is
    therefore how many left the reach, and a low survivor count reads as the
    plume having passed rather than as a broken tracker.
    """
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


#: TELEMAC-2D prints its volume closure once per listing under this label, as a
#: percentage of the volume that entered.
_VOLUME_ERROR = r"RELATIVE ERROR IN VOLUME\s*:?\s*=?\s*([-\d.Ee+]+)"


def continuity_rel_error(listing_text: str) -> float | None:
    """The engine's OWN volume closure, off the last one it printed.

    The solver accounts for its own mass and says so every listing period; the
    LAST figure is the run's, and a reader that integrated the depth field
    instead would be answering with a second computation the engine never made.
    """
    found = re.findall(_VOLUME_ERROR, listing_text or "")
    if not found:
        return None
    try:
        return float(found[-1])
    except ValueError:
        return None


def outlet_hydrograph(result_slf: str | Path, *, outlet_nodes: Any
                      ) -> dict[str, Any]:
    """Discharge THROUGH the declared outlet, per written frame.

    The flux integral the run's answer is: over every boundary segment whose two
    ends both took the outlet role, the depth-weighted normal velocity times the
    segment length, summed. The normal points AWAY from the element the segment
    belongs to, so water leaving the basin arrives positive and the sign never
    depends on which way the boundary was walked.

    A boundary segment is an element edge no second element shares - the mesh's
    own definition of its rim - so the outlet is measured on the geometry the
    solver integrated over rather than on a polyline redrawn beside it.
    """
    import numpy as np

    from trid3nt_server.workflows.telemac.postprocess_telemac import read_selafin

    slf = read_selafin(str(result_slf))
    names = {v.strip().upper(): v for v in slf["varnames"]}

    def _pick(*keys: str) -> Any:
        for key in keys:
            for upper, original in names.items():
                if upper.startswith(key):
                    return np.asarray(slf["data"][original], dtype=float)
        return None

    depth = _pick("WATER DEPTH", "H ")
    vel_u = _pick("VELOCITY U", "U ")
    vel_v = _pick("VELOCITY V", "V ")
    if depth is None or vel_u is None or vel_v is None:
        return {}

    xy = np.column_stack([np.asarray(slf["x"], dtype=float),
                          np.asarray(slf["y"], dtype=float)])
    edges = _outlet_edges(np.asarray(slf["ikle"], dtype=np.int64),
                          {int(n) for n in outlet_nodes})
    if not edges:
        return {}

    times = np.asarray(slf["times"], dtype=float)
    flows = np.zeros(times.size, dtype=float)
    for a, b, apex in edges:
        span = xy[b] - xy[a]
        length = float(np.hypot(*span))
        if length <= 0.0:
            continue
        # Outward is the side the apex is NOT on.
        normal = np.array([span[1], -span[0]]) / length
        if float(np.dot(xy[apex] - 0.5 * (xy[a] + xy[b]), normal)) > 0.0:
            normal = -normal
        flux = 0.5 * ((depth[:, a] * (vel_u[:, a] * normal[0]
                                      + vel_v[:, a] * normal[1]))
                      + (depth[:, b] * (vel_u[:, b] * normal[0]
                                        + vel_v[:, b] * normal[1])))
        flows += flux * length

    volume = float(np.trapezoid(flows, times)) if times.size > 1 else 0.0
    peak = int(np.argmax(flows)) if flows.size else 0
    return {
        "t_s": [round(float(t), 3) for t in times],
        "q_m3s": [round(float(q), 6) for q in flows],
        "peak_discharge_m3s": round(float(flows[peak]), 6) if flows.size else None,
        "peak_discharge_time_s": round(float(times[peak]), 3) if flows.size else None,
        "runoff_volume_m3": round(max(volume, 0.0), 3),
        "outlet_segments": len(edges),
    }


def _outlet_edges(ikle: Any, outlet: set[int]) -> list[tuple[int, int, int]]:
    """Boundary edges whose BOTH ends took the outlet role -> ``(a, b, apex)``.

    ``apex`` is the element's third node, which is what makes the outward side
    measurable without walking the boundary loop in any particular direction.
    """
    seen: dict[tuple[int, int], tuple[int, int, int]] = {}
    shared: set[tuple[int, int]] = set()
    for tri in ikle:
        nodes = [int(n) for n in tri[:3]]
        for i in range(3):
            a, b = nodes[i], nodes[(i + 1) % 3]
            key = (min(a, b), max(a, b))
            if key in seen:
                shared.add(key)
                continue
            seen[key] = (a, b, nodes[(i + 2) % 3])
    return [row for key, row in seen.items()
            if key not in shared and key[0] in outlet and key[1] in outlet]
