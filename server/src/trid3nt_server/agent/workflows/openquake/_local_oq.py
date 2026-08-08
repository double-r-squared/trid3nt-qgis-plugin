"""Shared OpenQuake local-subprocess deck primitives + runner.

Self-contained (no ``services.workers`` import) agent-side helpers shared by the
in-process OpenQuake calculators that run the installed ``oq`` CLI as a
subprocess of the composer (the offline/local lane, like
``openquake_scenario_gmf``): the disaggregation and event-based-PSHA templates,
and the ``openquake_psha`` Vs30 site-response A/B overlay.

Every deck here uses a synthetic Gutenberg-Richter AREA source over the AOI (a
labelled demo source, narrated as such) so the module carries no fault-render
dependency on the worker package. The real-fault physics stays in the classical
map template (``openquake_psha``); these calculators answer source-parameter /
site-condition / catalog questions where the demo area source is the honest,
published-demo-anchored substrate (the GEM Disaggregation and EventBasedPSHA
demos both use an area source).
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("trid3nt_server.agent.workflows.openquake._local_oq")

__all__ = [
    "DEFAULT_IMLS_G",
    "LocalOqError",
    "aoi_centroid",
    "imls_list_str",
    "region_str",
    "render_area_source_model_xml",
    "render_trivial_source_logic_tree_xml",
    "render_trivial_gmpe_logic_tree_xml",
    "render_classical_point_job_ini",
    "run_oq_local",
]

#: The oq CLI binary (overridable for a non-standard install).
_OQ_BIN: str = os.environ.get("TRID3NT_OQ_BIN", "oq")
#: Subprocess wall-clock ceiling for one local solve (seconds).
_OQ_TIMEOUT_S: int = 900

#: Log-spaced intensity-measure-level ladder (g), 0.005..2.13 - the standard demo
#: hazard-curve sampling (matches the classical worker deck byte-for-byte).
DEFAULT_IMLS_G: tuple[float, ...] = (
    0.005, 0.007, 0.0098, 0.0137, 0.0192, 0.0269, 0.0376, 0.0527, 0.0738,
    0.103, 0.145, 0.203, 0.284, 0.397, 0.556, 0.778, 1.09, 1.52, 2.13,
)


class LocalOqError(RuntimeError):
    """Raised when a local ``oq`` solve fails fatally.

    Carries the open-set ``error_code`` so a caller renders a typed error frame.
    Codes: ``OQ_LOCAL_MISSING`` (the ``oq`` binary is not on PATH),
    ``OQ_LOCAL_SOLVE_FAILED`` (the engine exited non-zero or timed out)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def aoi_centroid(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    """(lon, lat) centroid of an EPSG:4326 bbox."""
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    return (min_lon + max_lon) / 2.0, (min_lat + max_lat) / 2.0


def imls_list_str(imls: tuple[float, ...] = DEFAULT_IMLS_G) -> str:
    """Render the IML ladder as the comma-separated list job.ini expects."""
    return ", ".join(repr(round(v, 6)) for v in imls)


def region_str(bbox: tuple[float, float, float, float]) -> str:
    """OpenQuake ``[geometry] region`` string: lon lat pairs round the rectangle."""
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    return (
        f"{min_lon} {min_lat}, {max_lon} {min_lat}, "
        f"{max_lon} {max_lat}, {min_lon} {max_lat}"
    )


def render_area_source_model_xml(
    bbox: tuple[float, float, float, float],
    *,
    a_value: float,
    b_value: float,
    min_magnitude: float,
    max_magnitude: float,
    source_id: str = "1",
    tectonic_region: str = "Active Shallow Crust",
) -> str:
    """Render a single NRML 0.4 area source covering the AOI (demo G-R seismicity).

    The area-source polygon is the bbox rectangle, the seismicity a truncated
    Gutenberg-Richter MFD, and a vertical strike-slip nodal plane the demo
    geometry. ``gml:posList`` is LON LAT (OpenQuake's sourceconverter reads pairs
    lon-first); NRML 0.4 namespace (the engine rejects this 0.4-style body under a
    0.5 declaration) - both proven by real local ``oq`` runs.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    pos_list = (
        f"{min_lon} {min_lat} {max_lon} {min_lat} "
        f"{max_lon} {max_lat} {min_lon} {max_lat}"
    )
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


def render_trivial_source_logic_tree_xml(
    source_model_filename: str = "source_model.xml",
) -> str:
    """Trivial 1-branch source-model logic tree (probability 1.0)."""
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


def render_trivial_gmpe_logic_tree_xml(
    gmpe: str,
    *,
    tectonic_region: str = "Active Shallow Crust",
) -> str:
    """Trivial 1-branch GMPE logic tree naming a single GMPE (probability 1.0)."""
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


def render_classical_point_job_ini(
    *,
    site_lon: float,
    site_lat: float,
    imt: str,
    investigation_time_years: float,
    max_distance_km: float,
    reference_vs30: float,
    gmpe_lt_file: str = "gmpe_logic_tree.xml",
    source_lt_file: str = "source_model_logic_tree.xml",
    description: str = "classical PSHA point",
) -> str:
    """Render a classical-PSHA job.ini at ONE site (a hazard-curve probe).

    Used for cheap single-site classical hazard curves: the event-based
    convergence cross-check and the Vs30 site-response A/B overlay. The hazard
    curve exports by default with ``--exports csv``.
    """
    iml_list = imls_list_str(DEFAULT_IMLS_G)
    return (
        "[general]\n"
        f"description = {description}\n"
        "calculation_mode = classical\n"
        "random_seed = 23\n\n"
        "[geometry]\n"
        f"sites = {site_lon:.6f} {site_lat:.6f}\n\n"
        "[logic_tree]\n"
        "number_of_logic_tree_samples = 0\n\n"
        "[erf]\n"
        "rupture_mesh_spacing = 5\n"
        "width_of_mfd_bin = 0.2\n"
        "area_source_discretization = 10.0\n\n"
        "[site_params]\n"
        "reference_vs30_type = measured\n"
        f"reference_vs30_value = {reference_vs30:g}\n"
        "reference_depth_to_2pt5km_per_sec = 1.0\n"
        "reference_depth_to_1pt0km_per_sec = 50.0\n\n"
        "[calculation]\n"
        f"source_model_logic_tree_file = {source_lt_file}\n"
        f"gsim_logic_tree_file = {gmpe_lt_file}\n"
        f"investigation_time = {investigation_time_years:g}\n"
        f'intensity_measure_types_and_levels = {{"{imt}": [{iml_list}]}}\n'
        "truncation_level = 3\n"
        f"maximum_distance = {max_distance_km:g}\n\n"
        "[output]\n"
        "export_dir = out\n"
        "mean = true\n"
    )


def run_oq_local(files: dict[str, str], *, label: str = "oq") -> Path:
    """Materialize deck ``files`` into a temp dir, run ``oq engine``, return outdir.

    ``files`` maps on-disk filename -> text; ``job.ini`` must be present. Runs
    ``oq engine --run job.ini --exports csv`` with the datastore contained under
    the rundir (so /tmp cleanup is ours). Returns the ``out/`` export directory
    (Path). SYNC (subprocess + file I/O) - callers run it via
    ``asyncio.to_thread`` off the loop.

    Raises ``LocalOqError`` (``OQ_LOCAL_MISSING`` / ``OQ_LOCAL_SOLVE_FAILED``).
    """
    rundir = Path(tempfile.mkdtemp(prefix=f"trid3nt_oq_{label}_"))
    for fname, text in files.items():
        (rundir / fname).write_text(text, encoding="utf-8")

    env = dict(os.environ)
    env["OQ_DATADIR"] = str(rundir / "oqdata")
    try:
        proc = subprocess.run(  # noqa: S603 -- fixed argv, no shell
            [_OQ_BIN, "engine", "--run", "job.ini", "--exports", "csv"],
            cwd=str(rundir), env=env, capture_output=True, text=True,
            timeout=_OQ_TIMEOUT_S, check=False,
        )
    except FileNotFoundError as exc:
        raise LocalOqError(
            "OQ_LOCAL_MISSING",
            f"'{_OQ_BIN}' not found on PATH - install openquake.engine "
            f"or set TRID3NT_OQ_BIN ({exc})",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LocalOqError(
            "OQ_LOCAL_SOLVE_FAILED",
            f"{label} solve exceeded {_OQ_TIMEOUT_S}s wall clock",
        ) from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise LocalOqError(
            "OQ_LOCAL_SOLVE_FAILED",
            f"oq engine exited {proc.returncode}: {' | '.join(tail)}",
        )
    outdir = rundir / "out"
    if not outdir.is_dir():
        # some calculators export straight into the rundir; fall back to it.
        outdir = rundir
    logger.info("run_oq_local[%s]: rc=0 rundir=%s", label, rundir)
    return outdir
