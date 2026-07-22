"""Pure, deterministic OpenQuake classical-PSHA deck templating (sprint-17).

This module owns the OpenQuake *deck authoring* — turning a ``build_spec`` dict
into the three text files the OpenQuake Engine CLI consumes for a classical PSHA:

  - ``job.ini``                     — the calculation config (calculation_mode =
                                       classical, the region + grid spacing, the
                                       IMT + intensity levels, the maximum
                                       distance, the investigation time + PoEs,
                                       and the two logic-tree file pointers).
  - ``source_model.xml``            — a single AREA source covering the AOI with
                                       a Gutenberg-Richter magnitude-frequency
                                       distribution (the demo seismic source).
  - ``source_model_logic_tree.xml`` — a trivial 1-branch source-model logic tree
                                       pointing at ``source_model.xml``.
  - ``gmpe_logic_tree.xml``         — a trivial 1-branch GMPE logic tree naming a
                                       single ground-motion prediction equation.

It is PURE (no I/O, no OpenQuake import, no network) so it unit-tests in
isolation — the "job.ini templating unit test" acceptance item. The worker
entrypoint (``entrypoint.py``) calls ``render_openquake_deck`` to materialize
these files into the scratch dir before invoking ``oq engine --run job.ini``.

The canonical real-world pipeline this mirrors: an OpenQuake hazard input model
is a ``job.ini`` referencing a source-model logic tree (the seismic sources) and
a GMPE logic tree (the ground-motion models), with the calculation laid over a
regular site grid bounded by ``region`` + ``region_grid_spacing``. We replicate
that exact structure with a single area source + single GMPE for the v0.1 demo
(a real published model swaps in a multi-branch logic tree + a national source
model — the deck shape is identical).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OpenQuakeDeck:
    """The four rendered OpenQuake deck files (in-memory strings).

    Fields:
        job_ini: the ``job.ini`` text.
        source_model_xml: the ``source_model.xml`` (NRML area source) text.
        source_model_logic_tree_xml: the source-model logic-tree XML text.
        gmpe_logic_tree_xml: the GMPE logic-tree XML text.
        filenames: the canonical on-disk filename for each (job.ini is the
            entrypoint the worker runs).
    """

    job_ini: str
    source_model_xml: str
    source_model_logic_tree_xml: str
    gmpe_logic_tree_xml: str
    filenames: dict[str, str] = field(
        default_factory=lambda: {
            "job_ini": "job.ini",
            "source_model_xml": "source_model.xml",
            "source_model_logic_tree_xml": "source_model_logic_tree.xml",
            "gmpe_logic_tree_xml": "gmpe_logic_tree.xml",
        }
    )


#: Default intensity-measure levels (IMLs) for a PGA/SA hazard curve, in g.
#: A log-spaced ladder from 0.005 g to ~2 g — the standard demo curve sampling.
_DEFAULT_IMLS_G: tuple[float, ...] = (
    0.005,
    0.007,
    0.0098,
    0.0137,
    0.0192,
    0.0269,
    0.0376,
    0.0527,
    0.0738,
    0.103,
    0.145,
    0.203,
    0.284,
    0.397,
    0.556,
    0.778,
    1.09,
    1.52,
    2.13,
)


def _bbox_floats(bbox: Any) -> tuple[float, float, float, float]:
    """Coerce a bbox (list/tuple of 4 numbers) to floats, validating order."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    if not (min_lon < max_lon and min_lat < max_lat):
        raise ValueError(
            f"bbox must satisfy min<max on both axes; got {bbox!r}"
        )
    return min_lon, min_lat, max_lon, max_lat


def _km_to_deg(km: float) -> float:
    """Approximate a km spacing as decimal degrees (~111.32 km / deg)."""
    return float(km) / 111.32


def _imls_string(imls: tuple[float, ...]) -> str:
    """Render the IML ladder as the space-separated string job.ini expects."""
    return " ".join(repr(round(v, 6)) for v in imls)


def render_source_model_xml(
    bbox: tuple[float, float, float, float],
    *,
    a_value: float,
    b_value: float,
    min_magnitude: float,
    max_magnitude: float,
    source_id: str = "1",
    tectonic_region: str = "Active Shallow Crust",
) -> str:
    """Render a single NRML area source covering the AOI bbox.

    The area-source polygon is the bbox rectangle (the AOI), the seismicity is a
    truncated Gutenberg-Richter MFD (``a_value`` rate + ``b_value`` slope over
    ``min_magnitude``..``max_magnitude``), and a nodal-plane / hypo-depth
    distribution gives a simple vertical strike-slip demo geometry.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    # NATE 2026-06-26: OQ's area-source gml:posList parser (sourceconverter
    # split_coords_2d) reads pairs as LON LAT, not lat lon. The old "lat lon"
    # order made the engine read a longitude as a latitude ("latitude -122.45 <
    # -90") - proven by a real local oq run. Emit LON LAT going around the bbox.
    pos_list = (
        f"{min_lon} {min_lat} "
        f"{max_lon} {min_lat} "
        f"{max_lon} {max_lat} "
        f"{min_lon} {max_lat}"
    )
    # NATE 2026-06-26: the area-source body (areaSource directly under
    # sourceModel + truncGutenbergRichterMFD) is the NRML 0.4 schema. The engine
    # (oq 3.20) REJECTS this content under an xmlns nrml/0.5 declaration
    # ("InvalidFile: ... should be xmlns=.../nrml/0.4") - proven by a real local
    # `oq engine --run`. NRML 0.5 wraps sources in <sourceGroup>; since the body
    # is 0.4-style, declare 0.4 (the unit tests string-match the XML so never
    # caught this; only a real engine run does).
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nrml xmlns:gml="http://www.opengis.net/gml"
      xmlns="http://openquake.org/xmlns/nrml/0.4">
    <sourceModel name="demo area source">
        <areaSource id="{source_id}"
                    name="AOI area source"
                    tectonicRegion="{tectonic_region}">
            <areaGeometry>
                <gml:Polygon>
                    <gml:exterior>
                        <gml:LinearRing>
                            <gml:posList>
                                {pos_list}
                            </gml:posList>
                        </gml:LinearRing>
                    </gml:exterior>
                </gml:Polygon>
                <upperSeismoDepth>0.0</upperSeismoDepth>
                <lowerSeismoDepth>15.0</lowerSeismoDepth>
            </areaGeometry>
            <magScaleRel>WC1994</magScaleRel>
            <ruptAspectRatio>1.0</ruptAspectRatio>
            <truncGutenbergRichterMFD aValue="{a_value}" bValue="{b_value}"
                                      minMag="{min_magnitude}" maxMag="{max_magnitude}"/>
            <nodalPlaneDist>
                <nodalPlane probability="1.0" strike="0.0" dip="90.0" rake="0.0"/>
            </nodalPlaneDist>
            <hypoDepthDist>
                <hypoDepth probability="1.0" depth="10.0"/>
            </hypoDepthDist>
        </areaSource>
    </sourceModel>
</nrml>
"""


#: Shear modulus (rigidity) mu, Pa. The moment-balance constant: a fault slipping
#: at rate ``s`` (m/yr) over rupture area ``A`` (m^2) releases seismic moment at
#: ``mdot = mu * A * s`` per year. 3e10 Pa is the canonical crustal value (the
#: proven local run + standard PSHA practice).
_MU_PA: float = 3.0e10


def _haversine_trace_length_m(trace: list[list[float]]) -> float:
    """Along-trace length (m) by summing equirectangular segment distances.

    Faithful to the proven local run (``/tmp/oq_realfault_e2e.py``): each
    segment's east-west span is scaled by ``cos(mean_lat)`` and both spans by
    ~111195 m/deg, then ``hypot``-summed. ``trace`` is ``[[lon, lat], ...]``.
    """
    total = 0.0
    for (x1, y1), (x2, y2) in zip(trace[:-1], trace[1:]):
        dx = (x2 - x1) * math.cos(math.radians((y1 + y2) / 2.0)) * 111195.0
        dy = (y2 - y1) * 111195.0
        total += math.hypot(dx, dy)
    return total


def fault_mfd_a_value(
    slip_mm_yr: float,
    area_m2: float,
    *,
    b_value: float = 0.9,
    min_mag: float = 5.0,
    max_mag: float = 7.8,
    bin_width: float = 0.1,
) -> float | None:
    """Moment-balanced ``a`` value for a fault's truncated GR MFD.

    Solves for the GR ``a`` so the MFD's TOTAL seismic-moment rate equals the
    fault's tectonic moment rate ``mdot = mu * A * slip``. Numerically: sum, over
    0.1-mag bins in ``[min_mag, max_mag]``, the per-bin incremental rate factor
    ``(10^(-b*(m-dm/2)) - 10^(-b*(m+dm/2)))`` times the per-event moment
    ``10^(1.5m + 9.05)`` (Hanks & Kanamori), giving the moment per unit ``10^a``;
    then ``a = log10(mdot / munit)``.

    Returns ``None`` when the balance is undefined (zero/negative slip, area, or
    bin sum) so the caller skips the source rather than emitting a bad MFD.
    Ported verbatim from the proven local run.
    """
    s = slip_mm_yr * 1e-3  # mm/yr -> m/yr
    mdot = _MU_PA * area_m2 * s
    n = max(int(round((max_mag - min_mag) / bin_width)), 1)
    mags = [min_mag + bin_width * (i + 0.5) for i in range(n)]
    munit = sum(
        (
            10 ** (-b_value * (m - bin_width / 2.0))
            - 10 ** (-b_value * (m + bin_width / 2.0))
        )
        * 10 ** (1.5 * m + 9.05)
        for m in mags
    )
    if munit <= 0 or mdot <= 0:
        return None
    return math.log10(mdot / munit)


def is_fault_record_renderable(
    rec: dict[str, Any], *, b_value: float = 0.9, min_mag: float = 5.0
) -> bool:
    """True iff ``rec`` will yield a usable ``simpleFaultSource``.

    This is the SINGLE gate shared by the worker renderer
    (``render_fault_source_model_xml``) AND the agent composer
    (``resolve_fault_sources`` filters fetched faults through it). Keeping ONE
    predicate is the honesty floor: the composer must only stamp a run
    ``real-fault`` for faults the worker will actually render, or a run could be
    labelled real while the engine ran the synthetic fallback (the divergence the
    fetcher's looser >=2-point/slip>0 filter alone allowed). Mirrors exactly the
    per-record checks in ``render_fault_source_model_xml``: a >=2-point trace, a
    positive slip rate, a positive haversine length, and a finite moment-balanced
    a-value.
    """
    trace = rec.get("geometry") or []
    if not isinstance(trace, (list, tuple)) or len(trace) < 2:
        return False
    slip = rec.get("net_slip_rate_mm_yr")
    if slip is None or float(slip) <= 0:
        return False
    slip = float(slip)
    dip = float(rec.get("dip_deg", 90.0))
    usd = float(rec.get("upper_seis_depth_km", 0.0))
    lsd = float(rec.get("lower_seis_depth_km", usd + 12.0))
    if lsd <= usd:
        lsd = usd + 12.0
    try:
        length_m = _haversine_trace_length_m(
            [[float(p[0]), float(p[1])] for p in trace]
        )
    except (TypeError, ValueError, IndexError):
        return False
    if length_m <= 0:
        return False
    width_m = (lsd - usd) * 1000.0 / max(math.sin(math.radians(dip)), 0.2)
    area_m2 = length_m * width_m
    m_max = round(min(8.0, max(6.0, 4.07 + 0.98 * math.log10(max(area_m2 / 1e6, 1.0)))), 1)
    a_value = fault_mfd_a_value(slip, area_m2, b_value=b_value, min_mag=min_mag, max_mag=m_max)
    return a_value is not None and math.isfinite(a_value)


def render_fault_source_model_xml(
    fault_records: list[dict[str, Any]],
    *,
    model_name: str = "GEM GAF real faults",
    b_value: float = 0.9,
    min_mag: float = 5.0,
    tectonic_region: str = "Active Shallow Crust",
) -> str:
    """Render an NRML 0.4 sourceModel of ``simpleFaultSource`` from fault records.

    Each record (the shape ``fetch_fault_sources`` emits) is turned into a
    physics-based ``simpleFaultSource`` carrying a moment-balanced truncated
    Gutenberg-Richter MFD derived from the fault's slip rate -- the REAL-source
    path that produces hazard PEAKING ON the fault trace (proven by a real local
    ``oq engine`` run: a 530-site SF map, max 1.23 g, peaking on the San Andreas
    trace). This is the companion to the synthetic ``render_source_model_xml``
    area source, which stays the FALLBACK for AOIs with no mapped active faults.

    The recipe (ported faithfully from the proven local run):

      - L = haversine length along the trace.
      - W = (lsd - usd) * 1000 / sin(dip)  with sin floored at 0.2.
      - A = L * W.
      - Mmax = round(min(8.0, max(6.0, 4.07 + 0.98*log10(A/1e6))), 1)
        (Wells & Coppersmith 1994 area-magnitude scaling).
      - a-value: moment-balanced so the GR MFD's total moment rate equals
        mu*A*slip (``fault_mfd_a_value``), with b = ``b_value`` (0.9),
        minMag = ``min_mag`` (5.0), maxMag = Mmax.

    NRML 0.4 (the same xmlns the area-source renderer uses -- engine-verified):
    ``simpleFaultSource`` with the trace ``gml:posList``, ``dip`` /
    ``upperSeismoDepth`` / ``lowerSeismoDepth``, ``magScaleRel`` = WC1994,
    ``ruptAspectRatio`` = 1.5, a ``truncGutenbergRichterMFD``, and ``rake``.

    Records missing a usable slip rate, a >=2-point trace, or a definable
    moment balance are SKIPPED (the same guard the fetcher applies). Raises
    ``ValueError`` only when NO record yields a usable source.
    """
    sources: list[str] = []
    for idx, rec in enumerate(fault_records):
        # The SINGLE shared usability gate (same one resolve_fault_sources filters
        # through) -- a record that fails here is skipped, and the composer will
        # have already excluded it from its real-fault count so the two cannot
        # disagree about whether the run is real-fault vs synthetic.
        if not is_fault_record_renderable(rec, b_value=b_value, min_mag=min_mag):
            continue
        trace = rec.get("geometry") or []
        slip = float(rec.get("net_slip_rate_mm_yr"))
        dip = float(rec.get("dip_deg", 90.0))
        rake = float(rec.get("rake_deg", 180.0))
        usd = float(rec.get("upper_seis_depth_km", 0.0))
        lsd = float(rec.get("lower_seis_depth_km", usd + 12.0))
        if lsd <= usd:
            lsd = usd + 12.0

        length_m = _haversine_trace_length_m([[float(p[0]), float(p[1])] for p in trace])
        if length_m <= 0:
            continue
        # Down-dip width: seismogenic thickness / sin(dip), dip floored so a
        # near-horizontal fault does not blow the width up.
        width_m = (lsd - usd) * 1000.0 / max(math.sin(math.radians(dip)), 0.2)
        area_m2 = length_m * width_m
        # Wells & Coppersmith 1994 area-magnitude relation, clamped to [6.0, 8.0].
        m_max = round(
            min(8.0, max(6.0, 4.07 + 0.98 * math.log10(max(area_m2 / 1e6, 1.0)))),
            1,
        )
        a_value = fault_mfd_a_value(
            slip, area_m2, b_value=b_value, min_mag=min_mag, max_mag=m_max
        )
        if a_value is None or not math.isfinite(a_value):
            continue

        pos_list = " ".join(
            f"{float(p[0]):.5f} {float(p[1]):.5f}" for p in trace
        )
        name = _xml_escape(str(rec.get("name") or f"fault{idx}"))
        sources.append(
            f'        <simpleFaultSource id="f{idx}" name="{name}" '
            f'tectonicRegion="{tectonic_region}">\n'
            f"            <simpleFaultGeometry>\n"
            f"                <gml:LineString>\n"
            f"                    <gml:posList>{pos_list}</gml:posList>\n"
            f"                </gml:LineString>\n"
            f"                <dip>{dip}</dip>\n"
            f"                <upperSeismoDepth>{usd}</upperSeismoDepth>\n"
            f"                <lowerSeismoDepth>{lsd}</lowerSeismoDepth>\n"
            f"            </simpleFaultGeometry>\n"
            f"            <magScaleRel>WC1994</magScaleRel>\n"
            f"            <ruptAspectRatio>1.5</ruptAspectRatio>\n"
            f'            <truncGutenbergRichterMFD aValue="{a_value:.4f}" '
            f'bValue="{b_value}" minMag="{min_mag}" maxMag="{m_max}"/>\n'
            f"            <rake>{rake}</rake>\n"
            f"        </simpleFaultSource>"
        )

    if not sources:
        raise ValueError(
            "no usable simpleFaultSource could be built from the supplied fault "
            "records (all lacked a positive slip rate, a >=2-point trace, or a "
            "definable moment balance); fall back to the synthetic area source"
        )

    body = "\n".join(sources)
    # NRML 0.4 namespace (engine-verified: OQ rejects this 0.4-style source body
    # under an 0.5 declaration), matching render_source_model_xml.
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<nrml xmlns:gml="http://www.opengis.net/gml"\n'
        '      xmlns="http://openquake.org/xmlns/nrml/0.4">\n'
        f'    <sourceModel name="{_xml_escape(model_name)}">\n'
        f"{body}\n"
        "    </sourceModel>\n"
        "</nrml>\n"
    )


def _xml_escape(text: str) -> str:
    """Escape the five XML-significant characters for safe attribute/text use."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def render_source_model_logic_tree_xml(
    source_model_filename: str = "source_model.xml",
) -> str:
    """Render a trivial 1-branch source-model logic tree pointing at the source
    model (probability 1.0)."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nrml xmlns:gml="http://www.opengis.net/gml"
      xmlns="http://openquake.org/xmlns/nrml/0.5">
    <logicTree logicTreeID="lt1">
        <logicTreeBranchingLevel branchingLevelID="bl1">
            <logicTreeBranchSet uncertaintyType="sourceModel"
                                branchSetID="bs1">
                <logicTreeBranch branchID="b1">
                    <uncertaintyModel>{source_model_filename}</uncertaintyModel>
                    <uncertaintyWeight>1.0</uncertaintyWeight>
                </logicTreeBranch>
            </logicTreeBranchSet>
        </logicTreeBranchingLevel>
    </logicTree>
</nrml>
"""


def render_gmpe_logic_tree_xml(
    gmpe: str,
    *,
    tectonic_region: str = "Active Shallow Crust",
) -> str:
    """Render a trivial 1-branch GMPE logic tree naming a single ground-motion
    prediction equation (probability 1.0) for the source's tectonic region."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nrml xmlns:gml="http://www.opengis.net/gml"
      xmlns="http://openquake.org/xmlns/nrml/0.5">
    <logicTree logicTreeID="lt1">
        <logicTreeBranchingLevel branchingLevelID="bl1">
            <logicTreeBranchSet uncertaintyType="gmpeModel"
                                branchSetID="bs1"
                                applyToTectonicRegionType="{tectonic_region}">
                <logicTreeBranch branchID="b1">
                    <uncertaintyModel>{gmpe}</uncertaintyModel>
                    <uncertaintyWeight>1.0</uncertaintyWeight>
                </logicTreeBranch>
            </logicTreeBranchSet>
        </logicTreeBranchingLevel>
    </logicTree>
</nrml>
"""


#: levers STEP 3 -- UHS spectral-acceleration periods. When uniform_hazard_spectra
#: is enabled the IML map must carry an SA(period) ladder (a UHS is the SA value
#: at each period for a fixed PoE), so we inject this ladder alongside the
#: requested IMT. A standard short->long period ladder (Sa at 0.1..2.0 s).
_UHS_SA_PERIODS: tuple[float, ...] = (0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0)


def render_job_ini(
    bbox: tuple[float, float, float, float],
    *,
    imt: str,
    poe: float,
    investigation_time_years: float,
    site_grid_spacing_km: float,
    max_distance_km: float,
    imls: tuple[float, ...] = _DEFAULT_IMLS_G,
    description: str = "classical PSHA",
    source_model_logic_tree_filename: str = "source_model_logic_tree.xml",
    gmpe_logic_tree_filename: str = "gmpe_logic_tree.xml",
    # --- advanced-physics overrides (levers STEP 3; ADDITIVE, default-match) - #
    truncation_level: float = 3.0,
    rupture_mesh_spacing_km: float = 5.0,
    width_of_mfd_bin: float = 0.2,
    area_source_discretization_km: float = 10.0,
    uniform_hazard_spectra: bool = False,
) -> str:
    """Render the classical-PSHA ``job.ini`` config text.

    ``region`` is the bbox closed-rectangle (lon lat pairs going round); the site
    grid spacing is converted from km to decimal degrees. The intensity_measure_
    types_and_levels maps the requested ``imt`` to the IML ladder; ``poes`` picks
    the hazard-map return period.

    levers STEP 3: ``truncation_level`` / ``rupture_mesh_spacing_km`` /
    ``width_of_mfd_bin`` / ``area_source_discretization_km`` are the
    advanced-physics overrides (defaults reproduce the pre-STEP-3 literals
    byte-for-byte). ``uniform_hazard_spectra`` flips UHS export on (the classical
    run already computes hazard curves, so they export by default with
    ``--exports csv``; UHS additionally needs this flag + an SA(period) IML
    ladder, which is injected when enabled).
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    # NATE 2026-06-26: OpenQuake's [geometry] region_grid_spacing is in KM, NOT
    # degrees. The old km->deg conversion wrote ~0.18 (deg for 20 km) which OQ
    # then read as 0.18 KM -> a ~100x-too-fine site grid (12477 sites for a 0.2deg
    # AOI vs the intended ~4) - proven by a real local oq run (absurdly slow +
    # costly on a real AOI). Pass the km value directly.
    grid_spacing_km = float(site_grid_spacing_km)
    # region = lon lat going round the rectangle (OpenQuake's region order is
    # lon lat, comma-separated vertices).
    region = (
        f"{min_lon} {min_lat}, {max_lon} {min_lat}, "
        f"{max_lon} {max_lat}, {min_lon} {max_lat}"
    )
    iml_str = _imls_string(imls)
    iml_list = iml_str.replace(" ", ", ")
    # The IML map carries the requested IMT; when UHS is on, ALSO carry the SA
    # period ladder (each on the same IML list) so the UHS export has spectra.
    imt_levels = {imt: f"[{iml_list}]"}
    if uniform_hazard_spectra:
        for _p in _UHS_SA_PERIODS:
            imt_levels[f"SA({_p})"] = f"[{iml_list}]"
    imtl_str = "{" + ", ".join(
        f'"{k}": {v}' for k, v in imt_levels.items()
    ) + "}"

    # Preserve the pre-STEP-3 integer literal ``truncation_level = 3`` byte-for-
    # byte when the (default) value is a whole number; a fractional override
    # renders as the float. OpenQuake parses both identically.
    def _num(v: float) -> str:
        f = float(v)
        return str(int(f)) if f.is_integer() else repr(f)

    trunc_str = _num(truncation_level)
    return f"""[general]
description = {description}
calculation_mode = classical
random_seed = 23

[geometry]
region = {region}
region_grid_spacing = {_num(grid_spacing_km)}

[logic_tree]
number_of_logic_tree_samples = 0

[erf]
rupture_mesh_spacing = {rupture_mesh_spacing_km}
width_of_mfd_bin = {width_of_mfd_bin}
area_source_discretization = {area_source_discretization_km}

[site_params]
reference_vs30_type = measured
reference_vs30_value = 760.0
reference_depth_to_2pt5km_per_sec = 1.0
reference_depth_to_1pt0km_per_sec = 50.0

[calculation]
source_model_logic_tree_file = {source_model_logic_tree_filename}
gsim_logic_tree_file = {gmpe_logic_tree_filename}
investigation_time = {investigation_time_years}
intensity_measure_types_and_levels = {imtl_str}
truncation_level = {trunc_str}
maximum_distance = {max_distance_km}

[output]
export_dir = output
mean = true
quantiles =
hazard_maps = true
uniform_hazard_spectra = {"true" if uniform_hazard_spectra else "false"}
poes = {poe}
"""


def render_openquake_deck(build_spec: dict[str, Any]) -> OpenQuakeDeck:
    """Render the full OpenQuake classical-PSHA deck from a ``build_spec`` dict.

    The ``build_spec`` is the JSON the agent composer stages to S3 (mirrors the
    SWMM/SFINCS manifest). Required + defaulted keys:

        bbox: (min_lon, min_lat, max_lon, max_lat) EPSG:4326     [required]
        imt: "PGA" / "PGV" / "SA(<period>)"                       [default PGA]
        poe: probability of exceedance, (0,1)                     [default 0.10]
        investigation_time_years: years                          [default 50]
        site_grid_spacing_km: km                                  [default 5]
        max_distance_km: km                                       [default 300]
        gmpe: GMPE class name                       [default BooreAtkinson2008]
        a_value / b_value: Gutenberg-Richter                  [default 4.0/1.0]
        min_magnitude / max_magnitude                          [default 5.0/7.5]

    Returns an :class:`OpenQuakeDeck` carrying the four rendered text files. PURE
    — no I/O. The worker writes ``deck.filenames`` to the scratch dir.

    Raises:
        ValueError: the bbox is missing / malformed (the only hard requirement).
    """
    bbox = _bbox_floats(build_spec.get("bbox"))

    imt = str(build_spec.get("imt", "PGA"))
    poe = float(build_spec.get("poe", 0.10))
    inv_time = float(build_spec.get("investigation_time_years", 50.0))
    grid_km = float(build_spec.get("site_grid_spacing_km", 5.0))
    max_dist = float(build_spec.get("max_distance_km", 300.0))
    gmpe = str(build_spec.get("gmpe", "BooreAtkinson2008"))

    a_value = float(build_spec.get("a_value", 4.0))
    b_value = float(build_spec.get("b_value", 1.0))
    min_mag = float(build_spec.get("min_magnitude", 5.0))
    max_mag = float(build_spec.get("max_magnitude", 7.5))

    # task #199: REAL-fault source model. When the agent composer attached
    # ``fault_sources`` (the records ``fetch_fault_sources`` emits for the AOI),
    # build a physics-based ``simpleFaultSource`` model so the hazard PEAKS ON the
    # actual fault traces. ADDITIVE: a build_spec with no (or an empty)
    # ``fault_sources`` renders the synthetic AOI area source byte-for-byte as
    # before, so every existing run is unchanged. The honesty floor is the
    # composer's (it only attaches fault_sources when the fetcher returned faults
    # AND narrates "synthetic-area" otherwise); if the records somehow yield no
    # usable source the renderer raises and we fall back to the area source here
    # rather than failing the run.
    fault_sources = build_spec.get("fault_sources")
    source_model_xml: str | None = None
    if isinstance(fault_sources, list) and fault_sources:
        try:
            source_model_xml = render_fault_source_model_xml(
                fault_sources,
                b_value=b_value,
                min_mag=min_mag,
            )
        except ValueError:
            # No usable fault source could be built -> honest area-source fallback.
            source_model_xml = None
    if source_model_xml is None:
        source_model_xml = render_source_model_xml(
            bbox,
            a_value=a_value,
            b_value=b_value,
            min_magnitude=min_mag,
            max_magnitude=max_mag,
        )
    smlt_xml = render_source_model_logic_tree_xml("source_model.xml")
    gmpelt_xml = render_gmpe_logic_tree_xml(gmpe)
    # levers STEP 3: advanced-physics overrides + UHS flag (all default-match,
    # so a build_spec without them renders byte-identically). The agent merges
    # the validated PHYSICS_REGISTRY["openquake"] keys into the build_spec.
    job_ini = render_job_ini(
        bbox,
        imt=imt,
        poe=poe,
        investigation_time_years=inv_time,
        site_grid_spacing_km=grid_km,
        max_distance_km=max_dist,
        truncation_level=float(build_spec.get("truncation_level", 3.0)),
        rupture_mesh_spacing_km=float(
            build_spec.get("rupture_mesh_spacing_km", 5.0)
        ),
        width_of_mfd_bin=float(build_spec.get("width_of_mfd_bin", 0.2)),
        area_source_discretization_km=float(
            build_spec.get("area_source_discretization_km", 10.0)
        ),
        uniform_hazard_spectra=bool(
            build_spec.get("uniform_hazard_spectra", False)
        ),
    )
    return OpenQuakeDeck(
        job_ini=job_ini,
        source_model_xml=source_model_xml,
        source_model_logic_tree_xml=smlt_xml,
        gmpe_logic_tree_xml=gmpelt_xml,
    )


def return_period_years(poe: float, investigation_time_years: float) -> float:
    """Return period (years) implied by a PoE over an investigation time.

    RP = -investigation_time / ln(1 - poe). The canonical 10%/50yr -> ~475 yr.
    """
    if not (0.0 < poe < 1.0):
        raise ValueError(f"poe must be in (0,1); got {poe!r}")
    if investigation_time_years <= 0.0:
        raise ValueError(
            f"investigation_time_years must be > 0; got {investigation_time_years!r}"
        )
    return -float(investigation_time_years) / math.log(1.0 - float(poe))
