"""Per-water-body-class bed ladders for the topobathy row, and the classifier.

A bed source is fit for one KIND of water and not another. NOAA BlueTopo is a
compiled surface for navigationally significant water; NOAA CUDEM is a coastal
topo-bathy mosaic; a surveyed cross-section at a streamgage is neither. So the
topobathy row does not have one ladder, it has one PER CLASS, and which ladder
governs a request is decided by what the chain already measured about the water
- never by a default that guesses.

Three classes, and what each one gets:

  coastal_estuary     user bed -> BlueTopo -> the CUDEM composite -> refuse
  navigable_river     refuses: its primary stopped and nothing is left to ladder
  small_inland_stream refuses: no rung ships

WHAT STOPPED, AND WHY. Three rungs the methodology names do not ship, each for a
measured reason found by verifying the source live before writing any of it:

  * eHydro (the navigable primary). Its queryable surface is ONE ArcGIS layer of
    survey-boundary polygons. It carries a horizontal ``sourceprojection`` and NO
    vertical datum field at all - 96 distinct values across 122,203 surveys, not
    one of them naming a vertical datum. The soundings are per-survey bulk ZIPs
    on a separate host. A bed source whose datum is unknowable from its index
    cannot state its datum in provenance, and a bed whose datum nobody carried
    is a bed nobody can merge.
  * NXSDB (the small-stream primary). Its measurement layers carry
    ``Distance_ft`` and ``Depth_ft`` - depth below the water surface at gauging
    time - and no bed elevation and no vertical datum. A bed would have to be
    DERIVED from the gage datum plus the stage at measurement time, which is a
    producer, not a fetch. It is also published as one 655 MB national
    GeoPackage with a layer per measurement, on a host that serves no range
    requests, so there is no per-AOI read of it at all.
  * The synthetic channel producer. Its slot on the small-stream ladder is
    DEFERRED BY RULING, not merely unbuilt: no synthetic bathymetry is produced,
    and whether a fabricated bed may ever stand in for a survey is a user
    decision rather than a gap for an implementation to close. Stated here and
    empty, so a later reader finds a decision where they would otherwise find an
    oversight.

So the small-stream class has no rung, and the navigable class has only its
BlueTopo alternate left once its eHydro primary stopped - which is a source, not
a LADDER: a ladder is a declared DEGRADATION PATH, and one with nothing below its
primary declares no degradation to permit. Both classes therefore REFUSE, naming
what is missing. That is the honest-refusal floor working, not a hole - the
alternative is letting a surface DEM's water top stand in for a channel bottom,
which is the substitution the correct-data-class law exists to prevent.

Falling from BlueTopo to the CUDEM composite is a CROSS-DATASET substitution and
wears that consequence, so the loudness gate asks before it happens.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Mapping, Sequence

from trid3nt_server.fallbacks import (
    Ladder,
    Rung,
    register_ladder,
    register_ladder_selector,
)

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.hooks.topobathy_class"
)

__all__ = [
    "WaterBodyClass",
    "WATER_BODY_CLASSES",
    "WaterBodyClassUnknown",
    "TIDAL_FTYPES",
    "CHANNEL_SURFACE_FTYPES",
    "classify_water_body",
    "COASTAL_ESTUARY_LADDER",
    "CLASS_LADDERS",
    "STOPPED_CLASSES",
    "ladder_for_request",
    "serve_bluetopo_bed",
]

_HOOKS = "trid3nt_server.tools.fetchers._router.hooks"

WaterBodyClass = Literal["coastal_estuary", "navigable_river", "small_inland_stream"]

WATER_BODY_CLASSES: tuple[str, ...] = (
    "coastal_estuary", "navigable_river", "small_inland_stream",
)


class WaterBodyClassUnknown(ValueError):
    """The held rows cannot decide the class, so nothing decides it.

    Carries ``missing``: what evidence would have decided it. A refusal that does
    not name what was missing tells the author nothing they can act on.
    """

    def __init__(self, message: str, *, missing: Sequence[str]) -> None:
        super().__init__(message)
        self.missing = tuple(missing)


# ---------------------------------------------------------------------------
# The classifier. Its ONLY inputs are rows the reach chain already holds.
# ---------------------------------------------------------------------------

#: NHD FType codes for TIDAL / MARINE water surfaces. Their presence in the
#: mapped water of a reach is what makes it coastal or estuarine; it is a fact
#: the water row carries, not an inference from where the AOI sits.
TIDAL_FTYPES: frozenset[int] = frozenset({
    312,  # BayInlet
    364,  # Foreshore
    445,  # SeaOcean
    493,  # Estuary
})

#: NHD FType codes for a mapped inland channel SURFACE - a channel wide enough
#: to have two mapped banks. It establishes that the reach is a wide river, and
#: that is ALL it establishes: it does not say the river is a federally
#: maintained navigation channel, which is what the navigable class means.
CHANNEL_SURFACE_FTYPES: frozenset[int] = frozenset({
    336,  # CanalDitch
    460,  # StreamRiver
})


def _ftypes(water_features: Any) -> set[int]:
    """The FType codes carried by the mapped water features, as ints."""
    out: set[int] = set()
    for feat in water_features or ():
        props = feat.get("properties") if isinstance(feat, Mapping) else None
        raw = (props or feat if isinstance(feat, Mapping) else {}).get("ftype")
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def classify_water_body(
    *,
    water_features: Any = None,
    mapped_water_fraction: float | None = None,
) -> WaterBodyClass:
    """The reach's water-body class, from the rows the chain already holds.

    ``water_features`` are the mapped water-surface features (the NHDArea row,
    each carrying ``ftype``); ``mapped_water_fraction`` is the share of the
    centerline those polygons map, which the reach front already measures.

    Two verdicts are decidable from that evidence and one is not:

    * a TIDAL FType among the mapped water -> ``coastal_estuary``;
    * NO mapped water surface at all -> ``small_inland_stream``. A channel too
      narrow to be mapped as an area is a flowline only, and the water fetcher
      says so in its own caveats - the absence is a real answer about the
      channel, not a fetch failure;
    * a mapped INLAND channel surface -> undecided, and it REFUSES. The evidence
      says the river is wide; the navigable class means a federally maintained
      navigation channel, and no row the chain holds says whether this one is.
      Deciding it would need a row nobody fetches yet, and the two classes get
      different beds, so the refusal names that rather than picking one.
    """
    codes = _ftypes(water_features)
    if codes & TIDAL_FTYPES:
        return "coastal_estuary"
    if not codes and not mapped_water_fraction:
        return "small_inland_stream"
    if codes & CHANNEL_SURFACE_FTYPES:
        raise WaterBodyClassUnknown(
            "the mapped water is an inland channel surface (NHD FType "
            f"{sorted(codes & CHANNEL_SURFACE_FTYPES)}), which says the river is "
            "wide but not whether it is a federally maintained navigation "
            "channel - the two get different beds. Declare water_body_class "
            "explicitly, or supply the bed.",
            missing=("federal navigation-channel status (USACE National Channel "
                     "Framework); no row this chain holds carries it",),
        )
    raise WaterBodyClassUnknown(
        f"the mapped water carries FType {sorted(codes)}, which names no class "
        "this ladder set covers",
        missing=("a tidal FType, a mapped inland channel FType, or the measured "
                 "absence of any mapped water surface",),
    )


# ---------------------------------------------------------------------------
# The rung adapter: BlueTopo served under the topobathy row's ladder.
# ---------------------------------------------------------------------------

#: Coverage at or above this counts as complete (a sliver of a tile edge is not
#: a gap a second source could meaningfully fill).
_COVERAGE_COMPLETE = 0.999


def serve_bluetopo_bed(
    bbox: Any = None,
    target_crs: Any = None,
    min_pixel_m: Any = None,
    timeout_s: Any = None,
    **_ignored: Any,
) -> Any:
    """Serve the BlueTopo rung, reporting a PARTIAL cover as a gap.

    BlueTopo is bathymetry only, so an AOI that includes land is partially
    covered by construction. The share is measured against the published tile
    scheme and handed to the walker as a :class:`LadderGap`, which is what lets
    the next rung fill the rest under the loudness gate rather than this rung
    quietly returning a half-painted bed as a whole one.
    """
    from trid3nt_server.tools import TOOL_REGISTRY

    from .topobathy import TopobathyCoverageGapError

    kwargs: dict[str, Any] = {"bbox": bbox}
    if target_crs is not None:
        kwargs["target_crs"] = target_crs
    if min_pixel_m is not None:
        kwargs["min_pixel_m"] = min_pixel_m
    if timeout_s is not None:
        kwargs["timeout_s"] = timeout_s

    result = TOOL_REGISTRY["fetch_bluetopo"].fn(**kwargs)
    covered = float(getattr(result, "coverage_fraction", 0.0) or 0.0)
    if covered < _COVERAGE_COMPLETE:
        raise TopobathyCoverageGapError(
            f"NOAA BlueTopo covers {covered * 100:.1f}% of this AOI. BlueTopo is "
            "bathymetry only and concentrates on navigationally significant "
            "water, so the remainder is land or water it does not publish.",
            covered_fraction=covered,
            gap_note=(
                f"BlueTopo (NAVD88, measured bathymetry) covers {covered * 100:.1f}% "
                "of the AOI; the rest needs another bed source"
            ),
        )
    return result


# ---------------------------------------------------------------------------
# The ladders, one per class.
# ---------------------------------------------------------------------------

_USER_RUNG = Rung(
    name="user_supplied",
    consequence="user_supplied",
    supplies_param="dem_uri",
    call=f"{_HOOKS}.topobathy:serve_user_supplied_bed",
    describes=(
        "the caller's own topo/bathy raster (an onsite survey, an uploaded "
        "grid); user data outranks every derived rung"
    ),
)

_BLUETOPO_RUNG = Rung(
    name="bluetopo",
    consequence="primary",
    call=f"{_HOOKS}.topobathy_class:serve_bluetopo_bed",
    describes=(
        "NOAA BlueTopo (National Bathymetric Source): the compiled best-available "
        "gridded bed for navigationally significant US waters, NAVD88, with "
        "per-cell uncertainty and contributor attribution"
    ),
)

COASTAL_ESTUARY_LADDER = register_ladder(Ladder(
    capability="fetch_topobathy:coastal_estuary",
    refuse_error_code="TOPOBATHY_COVERAGE_GAP",
    coverage_exempt_params=("force_bathy_base", "skip_cudem"),
    rungs=(
        _USER_RUNG,
        _BLUETOPO_RUNG,
        Rung(
            name="cudem_nearshore",
            consequence="cross_dataset",
            describes=(
                "NOAA NCEI CUDEM 1/9\" (~3 m) nearshore topo-bathy tiles, NAVD88, "
                "with USGS 3DEP painting the land -- a DIFFERENT compilation from "
                "BlueTopo, so taking it crosses datasets"
            ),
        ),
        Rung(
            name="regional_fine",
            consequence="enhancement",
            params={"include_regional_fine": True},
            describes=(
                "NOAA NCEI regional coastal DEM (CoNED, ~1 m) laid under the part "
                "of the AOI CUDEM's 1/9\" collection does not reach -- FINER than "
                "the primary, so taking it costs nothing"
            ),
        ),
        Rung(
            name="etopo_bathy_base",
            consequence="cross_dataset",
            params={"force_bathy_base": True},
            describes=(
                "NOAA ETOPO 2022 15 arc-second global relief (~450 m, EGM2008/MSL "
                "not NAVD88) laid under the whole AOI as the bathy base -- a REAL "
                "below-waterline bed, far coarser and on a different vertical datum"
            ),
        ),
    ),
))

#: Class -> ladder. A class ABSENT here is stopped, and STOPPED_CLASSES says why.
CLASS_LADDERS: Mapping[str, Ladder] = {
    "coastal_estuary": COASTAL_ESTUARY_LADDER,
}

#: Class -> what is missing before it can have a ladder at all. Stated as data so
#: the refusal names the gap and the model can allocate a requirement to it.
STOPPED_CLASSES: Mapping[str, str] = {
    "navigable_river": (
        "no bed ladder ships for a navigable river. Its primary - the USACE "
        "eHydro channel surveys - stopped: the queryable survey index carries a "
        "horizontal projection and no vertical datum field at all, and the "
        "soundings sit behind per-survey bulk archives on another host, so a "
        "bed fetched from it could not state its own datum. What the "
        "methodology leaves below that rung is BlueTopo alone, and one source "
        "is not a degradation path - a ladder with nothing under its primary "
        "declares no alternative for anyone to permit. Fetch BlueTopo directly "
        "for the measured bed here, or supply a surveyed one (dem_uri)."
    ),
    "small_inland_stream": (
        "no bed rung ships for a small inland stream. The surveyed cross-sections "
        "at USGS streamgages (NXSDB) carry depth below the water surface and no "
        "bed elevation and no vertical datum, and are published only as a 655 MB "
        "national GeoPackage on a host that serves no range requests, so they are "
        "a producer's input rather than a bed anyone can fetch. The rung below "
        "them is the synthetic channel producer, which is DEFERRED BY RULING: no "
        "synthetic bathymetry is produced, and a fabricated bed standing in for "
        "a survey is a decision for you rather than a gap this closes on its "
        "own. Supply a surveyed bed (dem_uri), or model a reach whose class has "
        "one."
    ),
}


def ladder_for_request(params: Mapping[str, Any]) -> Ladder | None:
    """The ladder governing this request, or None to use the unclassed default.

    A request that declares no ``water_body_class`` keeps the row's original
    ladder: the per-class ladders govern the row that states which water it is,
    and a class nobody declared is not a class anybody may assume.

    A STOPPED class never reaches a ladder at all - the capability's pre-cache
    input gate refuses it by name before the walk starts, because no source
    ships for it and no later stage could change that.
    """
    declared = params.get("water_body_class")
    if not declared:
        return None
    return CLASS_LADDERS.get(str(declared))


register_ladder_selector("fetch_topobathy", ladder_for_request)
