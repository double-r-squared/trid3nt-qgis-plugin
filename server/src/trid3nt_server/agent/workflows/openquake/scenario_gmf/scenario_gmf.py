"""Engine template ``openquake_scenario_gmf`` - OpenQuake scenario ground-motion
fields (GMF) for ONE earthquake rupture.

The scenario calculator draws ``number_of_ground_motion_fields`` spatially
correlated realizations (JB2009 within-event correlation) of a SINGLE rupture
over a site grid, then averages them. The average-GMF export gives, per site, the
MEAN motion (``gmv_<IMT>``) and the across-realization geometric standard
deviation (``gsd_<IMT>``). This template maps the mean as the primary COG and the
realization spread as a paired context COG, and returns a
``ScenarioGmfLayerURI``.

This is the deterministic ShakeMap-style companion to the probabilistic
``openquake_psha`` template: PSHA answers "what ground motion is exceeded at a
return period", scenario GMF answers "if THIS fault ruptures at magnitude M, what
shaking hits the region, and how uncertain is it". It is ALSO the ground-motion
field the earthquake secondary-peril screening (``openquake_secondary_perils``)
rides on.

Compute lane: the OpenQuake engine runs on this machine (the installed ``oq``
CLI in the local venv) as a subprocess of the composer - no container image, no
Batch dispatch. The scenario deck is small (one rupture, a coarse site grid,
~100 fields) and completes in seconds.

Determinism boundary (Invariant 1): every scalar the agent narrates comes from
the typed ``ScenarioGmfLayerURI`` fields the postprocess computed from the engine
export - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.openquake_contracts import ScenarioGmfLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.openquake._template_card import TemplateCard
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.openquake.scenario_gmf.scenario_gmf"
)

__all__ = [
    "openquake_scenario_gmf",
    "model_openquake_scenario_gmf",
    "run_scenario_gmf",
    "resolve_scenario_rupture",
    "ScenarioGmfResult",
    "ScenarioRupture",
    "ScenarioGmfError",
    "GMF_MEAN_STYLE_PRESET",
    "GMF_SPREAD_STYLE_PRESET",
    "DEFAULT_SCENARIO_MAGNITUDE",
    "DEFAULT_NUM_GMFS",
    "DEFAULT_SCENARIO_GRID_KM",
]

#: Default scenario moment magnitude when the caller supplies none (a labelled
#: demo value, narrated as such - not a site-calibrated seismic-source number).
DEFAULT_SCENARIO_MAGNITUDE: float = 6.7
#: Default correlated ground-motion-field realization count (~ScenarioCase1 scale).
DEFAULT_NUM_GMFS: int = 100
#: Default scenario site-grid spacing (km). OpenQuake is RAM-hungry; coarse keeps
#: a wide AOI cheap.
DEFAULT_SCENARIO_GRID_KM: float = 4.0
#: Cap on scenario site-grid points; a wider AOI coarsens the grid to stay under
#: this (keeps a live scenario in the seconds regime).
_MAX_SCENARIO_SITES: int = 4000
#: Ground-motion floor (g / cm/s per IMT): mean cells at/below this are masked to
#: NaN so the COG frames only the shaken footprint.
_GMF_FLOOR_VALUE: float = 1e-3

#: Style presets: the mean map reuses the PGA magma ramp; the spread map uses the
#: dedicated dimensionless-spread viridis ramp.
GMF_MEAN_STYLE_PRESET: str = "continuous_seismic_pga"
GMF_SPREAD_STYLE_PRESET: str = "continuous_gmf_spread"

#: The oq CLI binary (overridable for a non-standard install).
_OQ_BIN: str = os.environ.get("TRID3NT_OQ_BIN", "oq")
#: Subprocess wall-clock ceiling for a scenario solve (seconds).
_OQ_TIMEOUT_S: int = 600


class ScenarioGmfError(RuntimeError):
    """Raised when the scenario-GMF chain fails fatally before producing a layer.

    Carries the open-set ``error_code`` so the agent emitter renders a typed
    error frame. Codes: ``SCENARIO_PARAMS_INVALID`` (bad bbox / magnitude),
    ``SCENARIO_OQ_MISSING`` (the ``oq`` binary is not on PATH),
    ``SCENARIO_SOLVE_FAILED`` (the engine exited non-zero),
    ``SCENARIO_GMF_EMPTY`` (the solve produced no average-GMF export)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "scenario earthquake ground-motion (ShakeMap-style) - if a specific fault "
        "ruptures at magnitude M, the mean PGA/PGV shaking over the region plus "
        "the across-realization spread; also the ground-motion field feeding an "
        "earthquake liquefaction / landslide secondary-peril screen"
    ),
    required_inputs=["bbox"],
    knobs=(
        "magnitude, imt (PGA / PGV / SA(<period>)), num_ground_motion_fields, "
        "gsim, vs30, site_grid_spacing_km, rupture_trace"
    ),
)


_SCENARIO_GMF_METADATA = AtomicToolMetadata(
    name="openquake_scenario_gmf",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="openquake",
    tier="template",
)


@register_tool(
    _SCENARIO_GMF_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def openquake_scenario_gmf(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    magnitude: float = DEFAULT_SCENARIO_MAGNITUDE,
    imt: str = "PGA",
    num_ground_motion_fields: int = DEFAULT_NUM_GMFS,
    gsim: str = "BooreAtkinson2008",
    vs30: float | None = None,
    site_grid_spacing_km: float = DEFAULT_SCENARIO_GRID_KM,
    max_distance_km: float = 200.0,
    rupture_trace: list[list[float]] | None = None,
    rake: float = 0.0,
    dip: float = 90.0,
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> ScenarioGmfLayerURI | dict[str, Any]:
    """Run a scenario (single-rupture) ground-motion-field calculation over an AOI.

    Fidelity: OpenQuake ``scenario`` calculator - N spatially correlated
    (JB2009 within-event) ground-motion realizations of ONE rupture, averaged to
    a per-site mean + across-realization geometric-standard-deviation field. When
    a real GEM Global Active Fault intersects the AOI the rupture is placed on its
    trace (rupture_kind ``"real-fault"``); otherwise a demo fault through the AOI
    centre (or a caller-supplied ``rupture_trace``) is used (``"synthetic"``) - the
    returned ``rupture_kind`` must be narrated honestly. The scenario MAGNITUDE is
    a user/prompt-supplied planning value, not a source-calibrated number.

    Use this when: the user asks for a scenario / deterministic earthquake, a
    ShakeMap-style "if fault X ruptures at magnitude M" ground-motion map, the
    shaking from a NAMED historical or hypothetical event, or the realization
    spread / uncertainty of a single-event ground-motion field. This is ALSO the
    ground-motion field the earthquake secondary-peril screen
    (``openquake_secondary_perils``) rides on. Do NOT use for: return-period /
    probabilistic "10% in 50 years" hazard (``openquake_psha``); building damage
    (``pelicun_damage_assessment``); ground-failure susceptibility maps directly
    (``openquake_secondary_perils`` for EQ-triggered liquefaction / landslide,
    ``landlab_susceptibility`` for rainfall-driven).

    Params:
        bbox: AOI, EPSG:4326; a regular site grid is laid over it.
        magnitude: scenario rupture moment magnitude (default 6.7, labelled demo).
        imt: ``"PGA"`` (default, g), ``"PGV"`` (cm/s), or ``"SA(<period>)"``.
        num_ground_motion_fields: correlated GMF realizations (default 100).
        gsim: ground-motion model, default "BooreAtkinson2008".
        vs30: reference site Vs30 (m/s). Unset -> the 760 rock demo default.
        site_grid_spacing_km: default 4 (coarsened for wide AOIs).
        max_distance_km: rupture-to-site integration distance, default 200.
        rupture_trace: optional ``[[lon,lat], ...]`` fault trace to rupture (a
            prompt-interpreted geometry); unset -> real-fault-or-synthetic default.
        rake: rupture rake degrees, default 0 (strike-slip).
        dip: rupture dip degrees, default 90 (vertical).
        input_mode: run-mode lever. ``"user_gated"`` presents the rupture geometry
            + magnitude for review before the solve; ``"auto"`` (default) proceeds
            with them labelled.

    Returns:
        On success: ``ScenarioGmfLayerURI`` (mean COG primary + spread COG context)
        with ``magnitude``, ``max_mean_value``, ``median_spread_factor``,
        ``n_sites``, ``rupture_kind``, ``rupture_note``.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached.
    """
    coerced = coerce_bbox_value(bbox)
    if coerced is None:
        return {
            "status": "error",
            "error_code": "SCENARIO_PARAMS_INVALID",
            "error_message": (
                "openquake_scenario_gmf requires a bbox "
                "(min_lon, min_lat, max_lon, max_lat) in EPSG:4326."
            ),
        }
    try:
        mag = float(magnitude)
        if not (4.0 <= mag <= 9.5):
            raise ValueError(f"magnitude {mag} out of the plausible 4.0-9.5 range")
        n_gmf = int(num_ground_motion_fields)
        if n_gmf < 2:
            raise ValueError("number_of_ground_motion_fields must be >= 2")
    except (TypeError, ValueError) as exc:
        return {
            "status": "error",
            "error_code": "SCENARIO_PARAMS_INVALID",
            "error_message": f"invalid scenario arguments: {exc}",
        }

    ref_vs30 = float(vs30) if vs30 is not None else 760.0

    # Resolve the rupture geometry (real GEM fault / caller trace / synthetic
    # demo) off the loop; it does a network fetch when no trace was supplied.
    rupture = await asyncio.to_thread(
        resolve_scenario_rupture,
        list(coerced), mag, rupture_trace, float(rake), float(dip),
    )

    # ADR 0107 input-review gate: the rupture geometry + magnitude are the
    # physically dominant, prompt-interpreted scenario inputs -> label them and,
    # in user_gated mode, present them for review before the solve.
    _entries = [
        SyntheticInput(
            param="magnitude", value=round(mag, 2), units="Mw",
            basis="user" if magnitude != DEFAULT_SCENARIO_MAGNITUDE else "default_demo",
            note=(None if magnitude != DEFAULT_SCENARIO_MAGNITUDE
                  else "labelled scenario magnitude demo default (not source-calibrated)"),
        ),
        SyntheticInput(
            param="rupture_geometry",
            value=rupture.kind, units=None,
            basis=("prompt_interpreted" if rupture_trace is not None
                   else ("fetched" if rupture.kind == "real-fault" else "default_demo")),
            note=rupture.note,
        ),
    ]
    _review = await gate_input_review(
        tool_name="openquake_scenario_gmf", mode=input_mode,
        entries=_entries, params={"magnitude": mag},
    )
    if _review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"openquake_scenario_gmf {_review.cancel_reason}",
        }
    _rv_mag = _review.params.get("magnitude")
    if _rv_mag is not None and float(_rv_mag) != mag:
        mag = float(_rv_mag)
        rupture = rupture.with_magnitude(mag)

    logger.info(
        "openquake_scenario_gmf bbox=%s M=%.2f imt=%s n_gmf=%d gsim=%s "
        "rupture_kind=%s grid=%.1fkm",
        list(coerced), mag, imt, n_gmf, gsim, rupture.kind, site_grid_spacing_km,
    )

    try:
        layer = await model_openquake_scenario_gmf(
            bbox=tuple(coerced),
            magnitude=mag,
            imt=str(imt),
            num_gmfs=n_gmf,
            gsim=str(gsim),
            reference_vs30=ref_vs30,
            site_grid_spacing_km=float(site_grid_spacing_km),
            max_distance_km=float(max_distance_km),
            rupture=rupture,
        )
        layer = layer.model_copy(update={"synthetic_inputs": _review.entries})
        return layer
    except asyncio.CancelledError:
        raise
    except ScenarioGmfError as exc:
        logger.warning("openquake_scenario_gmf failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("openquake_scenario_gmf unexpected failure")
        return {
            "status": "error",
            "error_code": "SCENARIO_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# Rupture resolution (real GEM fault / caller trace / synthetic demo).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ScenarioRupture:
    """A resolved scenario rupture: a fault trace + the source parameters the
    scenario deck's ``simpleFaultRupture`` needs."""

    trace: list[list[float]]  # [[lon, lat], ...], >= 2 vertices
    magnitude: float
    rake: float
    dip: float
    upper_depth_km: float
    lower_depth_km: float
    hypocenter_depth_km: float
    kind: str  # "real-fault" | "synthetic"
    note: str

    def with_magnitude(self, magnitude: float) -> "ScenarioRupture":
        return ScenarioRupture(
            trace=self.trace, magnitude=float(magnitude), rake=self.rake,
            dip=self.dip, upper_depth_km=self.upper_depth_km,
            lower_depth_km=self.lower_depth_km,
            hypocenter_depth_km=self.hypocenter_depth_km, kind=self.kind,
            note=self.note,
        )


def resolve_scenario_rupture(
    bbox: list[float],
    magnitude: float,
    rupture_trace: list[list[float]] | None,
    rake: float,
    dip: float,
) -> ScenarioRupture:
    """Resolve the scenario rupture geometry for ``bbox`` (sync; run off the loop).

    Precedence: an explicit caller ``rupture_trace`` (prompt-interpreted) wins;
    else the longest real GEM Global Active Fault trace intersecting the AOI
    (``real-fault``); else a synthetic demo fault through the AOI centre
    (``synthetic``). NEVER raises for the no-fault / fetch-failed case - a missing
    real fault is an honest fallback to the synthetic geometry, not an error.
    """
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox[:4])
    cx = (min_lon + max_lon) / 2.0
    cy = (min_lat + max_lat) / 2.0

    def _clean_trace(coords: Any) -> list[list[float]]:
        out: list[list[float]] = []
        for p in coords or []:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                out.append([float(p[0]), float(p[1])])
        return out

    # 1) explicit caller trace.
    if rupture_trace is not None:
        trace = _clean_trace(rupture_trace)
        if len(trace) >= 2:
            return ScenarioRupture(
                trace=trace, magnitude=magnitude, rake=rake, dip=dip,
                upper_depth_km=2.0, lower_depth_km=14.0, hypocenter_depth_km=8.0,
                kind="synthetic",
                note="Rupture placed on the caller-supplied fault trace.",
            )

    # 2) longest real GEM active fault in the AOI (best-effort).
    try:
        from trid3nt_server.agent.tools import TOOL_REGISTRY
        from trid3nt_server.agent.tools.fetchers._fetch_common import FetchError

        fetch_fault_sources = TOOL_REGISTRY["fetch_fault_sources"].fn
        try:
            result = fetch_fault_sources(bbox=list(bbox))
        except FetchError as exc:
            logger.info(
                "resolve_scenario_rupture: fault fetch failed (%s); synthetic demo",
                exc,
            )
            result = None
        faults: list[dict[str, Any]] = []
        if isinstance(result, dict):
            faults = list(result.get("faults") or [])
        elif result is not None:
            faults = list(getattr(result, "faults", None) or [])
        best: list[list[float]] | None = None
        best_len = 0.0
        best_name = "fault"
        for rec in faults:
            trace = _clean_trace(rec.get("geometry"))
            if len(trace) < 2:
                continue
            length = _trace_length_deg(trace)
            if length > best_len:
                best_len, best, best_name = length, trace, str(rec.get("name") or "fault")
        if best is not None:
            return ScenarioRupture(
                trace=best, magnitude=magnitude, rake=rake, dip=dip,
                upper_depth_km=2.0, lower_depth_km=14.0, hypocenter_depth_km=8.0,
                kind="real-fault",
                note=(
                    f"Rupture placed on the real GEM active-fault trace '{best_name}' "
                    f"intersecting the AOI (scenario magnitude {magnitude:g})."
                ),
            )
    except Exception as exc:  # noqa: BLE001 - fault resolution is best-effort
        logger.info("resolve_scenario_rupture: fault path skipped (%s)", exc)

    # 3) synthetic demo fault: a trace through the AOI centre spanning ~60% of the
    #    AOI diagonal (SW->NE), kept inside the region grid.
    half_lon = 0.30 * (max_lon - min_lon)
    half_lat = 0.30 * (max_lat - min_lat)
    trace = [[cx - half_lon, cy - half_lat], [cx + half_lon, cy + half_lat]]
    return ScenarioRupture(
        trace=trace, magnitude=magnitude, rake=rake, dip=dip,
        upper_depth_km=2.0, lower_depth_km=14.0, hypocenter_depth_km=8.0,
        kind="synthetic",
        note=(
            "No mapped active fault intersects this AOI; used a synthetic demo "
            f"fault through the AOI centre (scenario magnitude {magnitude:g})."
        ),
    )


def _trace_length_deg(trace: list[list[float]]) -> float:
    """Cumulative planar length of a lon/lat trace in degrees (comparison only)."""
    total = 0.0
    for (lo0, la0), (lo1, la1) in zip(trace, trace[1:]):
        total += math.hypot(lo1 - lo0, la1 - la0)
    return total


# --------------------------------------------------------------------------- #
# Scenario deck rendering (self-contained; classical-PSHA worker untouched).
# --------------------------------------------------------------------------- #
def render_scenario_rupture_xml(rupture: ScenarioRupture) -> str:
    """Render a NRML ``simpleFaultRupture`` XML for the scenario deck."""
    pos = "\n".join(
        f"                        {lo:.6f} {la:.6f}" for lo, la in rupture.trace
    )
    # Hypocentre at the trace midpoint.
    mid = rupture.trace[len(rupture.trace) // 2]
    return (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<nrml xmlns:gml=\"http://www.opengis.net/gml\"\n"
        "      xmlns=\"http://openquake.org/xmlns/nrml/0.4\">\n"
        "    <simpleFaultRupture>\n"
        f"        <magnitude>{rupture.magnitude:g}</magnitude>\n"
        f"        <rake>{rupture.rake:g}</rake>\n"
        f"        <hypocenter lat=\"{mid[1]:.6f}\" lon=\"{mid[0]:.6f}\" "
        f"depth=\"{rupture.hypocenter_depth_km:g}\"/>\n"
        "        <simpleFaultGeometry>\n"
        "            <gml:LineString>\n"
        "                <gml:posList>\n"
        f"{pos}\n"
        "                </gml:posList>\n"
        "            </gml:LineString>\n"
        f"            <dip>{rupture.dip:g}</dip>\n"
        f"            <upperSeismoDepth>{rupture.upper_depth_km:g}</upperSeismoDepth>\n"
        f"            <lowerSeismoDepth>{rupture.lower_depth_km:g}</lowerSeismoDepth>\n"
        "        </simpleFaultGeometry>\n"
        "    </simpleFaultRupture>\n"
        "</nrml>\n"
    )


def render_scenario_job_ini(
    *,
    bbox: tuple[float, float, float, float],
    imt: str,
    gsim: str,
    num_gmfs: int,
    reference_vs30: float,
    grid_spacing_km: float,
    max_distance_km: float,
) -> str:
    """Render the scenario ``job.ini`` (region grid + JB2009 correlation + N GMFs).

    The IMT list always includes PGA and PGV (the secondary-peril models need
    both); a caller SA(period) IMT is appended so the requested map is exported.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    imts = ["PGA", "PGV"]
    if imt.upper() not in ("PGA", "PGV"):
        imts.append(imt)
    imt_line = ", ".join(dict.fromkeys(imts))
    region = (
        f"{min_lon:.6f} {min_lat:.6f}, {max_lon:.6f} {min_lat:.6f}, "
        f"{max_lon:.6f} {max_lat:.6f}, {min_lon:.6f} {max_lat:.6f}"
    )
    return (
        "[general]\n"
        "description = TRID3NT scenario ground-motion field\n"
        "calculation_mode = scenario\n"
        "ses_seed = 42\n\n"
        "[geometry]\n"
        f"region = {region}\n"
        f"region_grid_spacing = {grid_spacing_km:g}\n\n"
        "[erf]\n"
        "rupture_mesh_spacing = 2.0\n\n"
        "[site_params]\n"
        "reference_vs30_type = measured\n"
        f"reference_vs30_value = {reference_vs30:g}\n"
        "reference_depth_to_2pt5km_per_sec = 2.0\n"
        "reference_depth_to_1pt0km_per_sec = 100.0\n\n"
        "[calculation]\n"
        "rupture_model_file = rupture_model.xml\n"
        f"intensity_measure_types = {imt_line}\n"
        "truncation_level = 3.0\n"
        f"maximum_distance = {max_distance_km:g}\n"
        f"gsim = {gsim}\n"
        "ground_motion_correlation_model = JB2009\n"
        "ground_motion_correlation_params = {\"vs30_clustering\": True}\n"
        f"number_of_ground_motion_fields = {num_gmfs}\n\n"
        "[output]\n"
        "export_dir = out\n"
    )


# --------------------------------------------------------------------------- #
# In-process scenario runner (subprocess of the installed oq; off the loop).
# --------------------------------------------------------------------------- #
@dataclass
class ScenarioGmfResult:
    """The parsed average-GMF field of a scenario run.

    ``sites`` is one dict per site with keys ``lon`` / ``lat`` and, per IMT, the
    mean ``gmv_<IMT>`` + across-realization geometric std ``gsd_<IMT>``."""

    sites: list[dict[str, float]]
    imts: list[str]
    magnitude: float
    rupture_kind: str
    rupture_note: str
    num_gmfs: int
    rundir: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _adaptive_grid_km(bbox: tuple[float, float, float, float], grid_km: float) -> float:
    """Coarsen the site grid so the AOI stays under ``_MAX_SCENARIO_SITES`` points."""
    min_lon, min_lat, max_lon, max_lat = bbox
    mean_lat = math.radians((min_lat + max_lat) / 2.0)
    w_km = abs(max_lon - min_lon) * 111.32 * max(math.cos(mean_lat), 0.05)
    h_km = abs(max_lat - min_lat) * 111.32
    if w_km <= 0 or h_km <= 0:
        return grid_km
    n = (w_km / grid_km) * (h_km / grid_km)
    if n <= _MAX_SCENARIO_SITES:
        return grid_km
    return grid_km * math.sqrt(n / _MAX_SCENARIO_SITES)


def run_scenario_gmf(
    *,
    bbox: tuple[float, float, float, float],
    magnitude: float,
    imt: str = "PGA",
    num_gmfs: int = DEFAULT_NUM_GMFS,
    gsim: str = "BooreAtkinson2008",
    reference_vs30: float = 760.0,
    site_grid_spacing_km: float = DEFAULT_SCENARIO_GRID_KM,
    max_distance_km: float = 200.0,
    rupture: ScenarioRupture | None = None,
) -> ScenarioGmfResult:
    """Run one scenario GMF calculation in-process and parse the average-GMF field.

    Renders a self-contained scenario deck into a temp dir, runs the installed
    ``oq engine --run job.ini`` (a subprocess of THIS venv - no image, no Batch),
    and parses ``out/avg_gmf_*.csv`` into per-site mean + spread values. SYNC (does
    subprocess + file I/O) - callers run it via ``asyncio.to_thread`` off the loop.

    Reused by BOTH the scenario-GMF template and the secondary-perils screen.

    Raises ``ScenarioGmfError`` (``SCENARIO_OQ_MISSING`` / ``SCENARIO_SOLVE_FAILED``
    / ``SCENARIO_GMF_EMPTY``).
    """
    if rupture is None:
        rupture = resolve_scenario_rupture(list(bbox), magnitude, None, 0.0, 90.0)
    grid_km = _adaptive_grid_km(bbox, float(site_grid_spacing_km))

    rundir = Path(tempfile.mkdtemp(prefix="trid3nt_oq_scenario_"))
    (rundir / "rupture_model.xml").write_text(
        render_scenario_rupture_xml(rupture), encoding="utf-8"
    )
    (rundir / "job.ini").write_text(
        render_scenario_job_ini(
            bbox=bbox, imt=imt, gsim=gsim, num_gmfs=int(num_gmfs),
            reference_vs30=reference_vs30, grid_spacing_km=grid_km,
            max_distance_km=max_distance_km,
        ),
        encoding="utf-8",
    )

    env = dict(os.environ)
    # Contain the oq datastore under the rundir so we own /tmp cleanup.
    env["OQ_DATADIR"] = str(rundir / "oqdata")
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [_OQ_BIN, "engine", "--run", "job.ini", "--exports", "csv"],
            cwd=str(rundir), env=env, capture_output=True, text=True,
            timeout=_OQ_TIMEOUT_S, check=False,
        )
    except FileNotFoundError as exc:
        raise ScenarioGmfError(
            "SCENARIO_OQ_MISSING",
            f"'{_OQ_BIN}' not found on PATH - install openquake.engine "
            f"or set TRID3NT_OQ_BIN ({exc})",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ScenarioGmfError(
            "SCENARIO_SOLVE_FAILED",
            f"scenario solve exceeded {_OQ_TIMEOUT_S}s wall clock",
        ) from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise ScenarioGmfError(
            "SCENARIO_SOLVE_FAILED",
            f"oq engine exited {proc.returncode}: {' | '.join(tail)}",
        )

    avg_csvs = sorted((rundir / "out").glob("avg_gmf_*.csv"))
    if not avg_csvs:
        raise ScenarioGmfError(
            "SCENARIO_GMF_EMPTY",
            "scenario solve produced no average-GMF export (out/avg_gmf_*.csv)",
        )
    sites, imts = _parse_avg_gmf_csv(avg_csvs[0].read_text(encoding="utf-8"))
    if not sites:
        raise ScenarioGmfError(
            "SCENARIO_GMF_EMPTY", "the average-GMF export carried no site rows"
        )
    logger.info(
        "run_scenario_gmf: M=%.2f n_sites=%d imts=%s grid=%.2fkm rundir=%s",
        magnitude, len(sites), imts, grid_km, rundir,
    )
    return ScenarioGmfResult(
        sites=sites, imts=imts, magnitude=float(magnitude),
        rupture_kind=rupture.kind, rupture_note=rupture.note,
        num_gmfs=int(num_gmfs), rundir=str(rundir),
    )


def _parse_avg_gmf_csv(text: str) -> tuple[list[dict[str, float]], list[str]]:
    """Parse an OpenQuake ``avg_gmf`` CSV into per-site dicts + the IMT list.

    Header (after the ``#`` provenance line): ``custom_site_id,lon,lat,
    gmv_<IMT>,gsd_<IMT>,...``. Returns ``(sites, imts)`` where each site dict
    carries ``lon`` / ``lat`` + every ``gmv_*`` / ``gsd_*`` column as a float.
    """
    import csv
    import io

    lines = [ln for ln in text.splitlines() if ln and not ln.lstrip().startswith("#")]
    if not lines:
        return [], []
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    cols = reader.fieldnames or []
    imts = [c[len("gmv_"):] for c in cols if c.startswith("gmv_")]
    sites: list[dict[str, float]] = []
    for row in reader:
        try:
            rec: dict[str, float] = {
                "lon": float(row["lon"]), "lat": float(row["lat"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        for c in cols:
            if c.startswith("gmv_") or c.startswith("gsd_"):
                try:
                    rec[c] = float(row[c])
                except (TypeError, ValueError):
                    rec[c] = float("nan")
        sites.append(rec)
    return sites, imts


# --------------------------------------------------------------------------- #
# Composer: run scenario GMF -> mean + spread COGs -> publish + chart.
# --------------------------------------------------------------------------- #
async def model_openquake_scenario_gmf(
    *,
    bbox: tuple[float, float, float, float],
    magnitude: float,
    imt: str,
    num_gmfs: int,
    gsim: str,
    reference_vs30: float,
    site_grid_spacing_km: float,
    max_distance_km: float,
    rupture: ScenarioRupture,
) -> ScenarioGmfLayerURI:
    """Run the scenario GMF end-to-end and publish the mean + spread COGs.

    Raises ``ScenarioGmfError`` on any solve / rasterize / write failure.
    """
    from trid3nt_server.agent.workflows.openquake.postprocess_openquake import (
        rasterize_hazard_sites,
    )
    from trid3nt_server.emission.layer_uri_emit import publish_input_layer

    run_id = new_ulid()
    begin_substeps(current_emitter(), 3)

    async with substep(current_emitter(), "run_scenario_gmf"):
        result = await asyncio.to_thread(
            run_scenario_gmf,
            bbox=bbox, magnitude=magnitude, imt=imt, num_gmfs=num_gmfs,
            gsim=gsim, reference_vs30=reference_vs30,
            site_grid_spacing_km=site_grid_spacing_km,
            max_distance_km=max_distance_km, rupture=rupture,
        )

    gmv_col = f"gmv_{imt}" if f"gmv_{imt}" in (result.sites[0] if result.sites else {}) else "gmv_PGA"
    gsd_col = "gsd_" + gmv_col[len("gmv_"):]
    imt_used = gmv_col[len("gmv_"):]

    async with substep(current_emitter(), "rasterize_and_publish"):
        mean_rows = [(s["lon"], s["lat"], s.get(gmv_col, float("nan"))) for s in result.sites]
        spread_rows = [(s["lon"], s["lat"], s.get(gsd_col, float("nan"))) for s in result.sites]
        mean_grid, bbox_grid, cell_deg = rasterize_hazard_sites(mean_rows)
        spread_grid, _sb, _sc = rasterize_hazard_sites(spread_rows)

        mean_uri, mean_bbox = await asyncio.to_thread(
            _write_publish_cog, mean_grid, bbox_grid, run_id, "mean",
            GMF_MEAN_STYLE_PRESET, floor=_GMF_FLOOR_VALUE,
        )
        spread_uri, _ = await asyncio.to_thread(
            _write_publish_cog, spread_grid, bbox_grid, run_id, "spread",
            GMF_SPREAD_STYLE_PRESET, floor=None,
        )

    max_mean, area_km2, n_sites = _grid_metrics(mean_grid, cell_deg, mean_bbox)
    median_spread = _finite_median(spread_grid)
    units = "cm/s" if imt_used.upper().startswith("PGV") else "g"

    mean_layer = ScenarioGmfLayerURI(
        layer_id=f"scenario-gmf-mean-{run_id}",
        name=f"Scenario ground motion (mean {imt_used}, M{magnitude:g})",
        layer_type="raster", uri=mean_uri, style_preset=GMF_MEAN_STYLE_PRESET,
        role="primary", units=units, bbox=mean_bbox,
        imt=imt_used, magnitude=float(magnitude),
        num_ground_motion_fields=int(result.num_gmfs),
        max_mean_value=max_mean, median_spread_factor=median_spread,
        n_sites=n_sites, rupture_kind=result.rupture_kind,
        rupture_note=result.rupture_note,
    )

    # Spread COG as a context layer (bbox forced None so it does not fight the
    # mean-map camera). Best-effort surface.
    spread_layer = ScenarioGmfLayerURI(
        layer_id=f"scenario-gmf-spread-{run_id}",
        name=f"Scenario ground motion spread (geometric std, {imt_used})",
        layer_type="raster", uri=spread_uri, style_preset=GMF_SPREAD_STYLE_PRESET,
        role="context", units="factor", bbox=None,
        imt=imt_used, magnitude=float(magnitude),
        num_ground_motion_fields=int(result.num_gmfs),
        max_mean_value=max_mean, median_spread_factor=median_spread,
        n_sites=n_sites, rupture_kind=result.rupture_kind,
        rupture_note=result.rupture_note,
    )
    await publish_input_layer(current_emitter(), spread_layer, role="context")

    # Realization-spread chart (mean +- spread band across the AOI).
    await _emit_scenario_spread_chart(
        result.sites, gmv_col, gsd_col, imt_used, units, magnitude,
        source_layer_uri=mean_layer.uri,
    )

    logger.info(
        "model_openquake_scenario_gmf complete run_id=%s M=%.2f imt=%s "
        "max_mean=%.4g median_spread=%.3f n_sites=%d kind=%s",
        run_id, magnitude, imt_used, max_mean, median_spread, n_sites,
        result.rupture_kind,
    )
    return mean_layer


def _write_publish_cog(
    grid: Any,
    bbox: tuple[float, float, float, float],
    run_id: str,
    tag: str,
    style_preset: str,
    *,
    floor: float | None,
) -> tuple[str, tuple[float, float, float, float]]:
    """Write ``grid`` to an EPSG:4326 COG, upload, publish; return (uri, bbox)."""
    import numpy as np

    from trid3nt_server.agent.workflows.shared import cog_io

    from rasterio.transform import from_bounds

    height, width = np.asarray(grid).shape
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], width, height)

    def _mask(a: Any) -> Any:
        arr = np.asarray(a, dtype="float32")
        if floor is not None:
            return np.where(arr > floor, arr, np.nan).astype("float32")
        return arr

    cog_path = cog_io.write_cog_4326_from_grid(
        grid, src_crs="EPSG:4326", src_transform=transform, reproject=False,
        mask=_mask,
    )
    cog_bbox = cog_io.cog_bbox_4326(cog_path) or bbox
    cog_uri = cog_io.upload_cog(
        cog_path, run_id, None, dest_filename=f"scenario_gmf_{tag}_4326.tif",
        content_type=None, gs_backend="gcs_client", gs_fallback_to_file=True,
        runs_bucket_default=None, log_label=f"scenario GMF {tag} COG",
    )
    final_uri = cog_uri
    if cog_uri.startswith("s3://") or cog_uri.startswith("gs://"):
        try:
            from trid3nt_server.agent.tools.publish_layer.publish_layer import publish_layer

            wms = publish_layer(
                layer_uri=cog_uri, layer_id=f"scenario-gmf-{tag}-{run_id}",
                style_preset=style_preset,
            )
            if wms:
                final_uri = wms
        except Exception as exc:  # noqa: BLE001 - publish is non-fatal
            logger.warning("scenario GMF %s publish failed: %s", tag, exc)
    return final_uri, cog_bbox


def _grid_metrics(
    grid: Any, cell_deg: float, bbox: tuple[float, float, float, float]
) -> tuple[float, float, int]:
    """Return (max, footprint_km2_above_floor, n_finite_sites) for a mean grid."""
    import numpy as np

    arr = np.asarray(grid, dtype="float64")
    finite = np.isfinite(arr)
    n = int(np.count_nonzero(finite))
    if n == 0:
        return 0.0, 0.0, 0
    mean_lat = (bbox[1] + bbox[3]) / 2.0
    km = 111.32
    cell_area = (cell_deg * km) * (cell_deg * km * abs(math.cos(math.radians(mean_lat))))
    area = float(np.count_nonzero(finite & (arr > _GMF_FLOOR_VALUE))) * cell_area
    return float(np.nanmax(arr)), area, n


def _finite_median(grid: Any) -> float:
    import numpy as np

    arr = np.asarray(grid, dtype="float64")
    finite = arr[np.isfinite(arr)]
    return float(np.median(finite)) if finite.size else 0.0


async def _emit_scenario_spread_chart(
    sites: list[dict[str, float]],
    gmv_col: str,
    gsd_col: str,
    imt: str,
    units: str,
    magnitude: float,
    *,
    source_layer_uri: str | None,
) -> None:
    """Build + side-emit the realization-spread chart (best-effort, no-op safe).

    Sorts the sites by mean motion and draws the mean line with a shaded band at
    ``mean / gsd`` .. ``mean * gsd`` (gsd is a geometric std), so the single
    figure shows the AOI ground-motion distribution AND the across-realization
    spread envelope. Emits nothing when the series is absent (the honesty floor).
    """
    try:
        vals = sorted(
            (s.get(gmv_col), s.get(gsd_col)) for s in sites
            if s.get(gmv_col) is not None and s.get(gmv_col) == s.get(gmv_col)
        )
        if len(vals) < 3:
            return
        n = len(vals)
        rows: list[dict[str, float]] = []
        for i, (mean, gsd) in enumerate(vals):
            pct = 100.0 * i / (n - 1)
            g = gsd if (gsd and gsd == gsd and gsd > 0) else 1.0
            rows.append({
                "site_percentile": round(pct, 2),
                "mean": round(float(mean), 6),
                "lo": round(float(mean) / g, 6),
                "hi": round(float(mean) * g, 6),
            })
        from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

        spec = {
            "data": {"values": rows},
            "layer": [
                {
                    "mark": {"type": "area", "opacity": 0.28, "color": "#7aa8d8"},
                    "encoding": {
                        "x": {"field": "site_percentile", "type": "quantitative",
                              "title": "AOI site percentile (%)"},
                        "y": {"field": "lo", "type": "quantitative",
                              "title": f"{imt} ({units})"},
                        "y2": {"field": "hi"},
                    },
                },
                {
                    "mark": {"type": "line", "color": "#1f5fbf"},
                    "encoding": {
                        "x": {"field": "site_percentile", "type": "quantitative"},
                        "y": {"field": "mean", "type": "quantitative"},
                    },
                },
            ],
        }
        payload = build_chart_payload(
            vega_lite_spec=spec,
            title=f"Scenario {imt} across the AOI (M{magnitude:g}) - mean and realization spread",
            caption=(
                f"Per-site mean {imt} sorted low-to-high across AOI sites (line); "
                f"shaded band = mean / gsd .. mean x gsd (across-realization "
                f"geometric standard deviation)."
            ),
            source_layer_uri=source_layer_uri,
        )
        await emit_chart_payloads(payload)
    except Exception as exc:  # noqa: BLE001 - chart is best-effort
        logger.warning("scenario spread chart emit failed (non-fatal): %s", exc)
