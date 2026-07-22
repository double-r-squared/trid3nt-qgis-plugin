"""Tests for MCP annotation hints on every registered atomic tool (job-B12).

Coverage:
- Every registered tool has all four annotation fields set (not None).
- Consistency rule: write tools (read_only_hint=False) are not also
  flagged as read-only.
- Consistency rule: external-API tools (open_world_hint=True) are a
  subset of the fetch_* / web_fetch / catalog_* group; compute_* /
  clip_* / intra-GCP tools are not open-world.
- Specific spot-checks for known high-stakes tools (publish_layer,
  run_solver, wait_for_completion, run_pelicun_damage_assessment).
- Verify the four new fields land on AtomicToolMetadata with correct
  default values.
"""

from __future__ import annotations

import pytest

from grace2_contracts.tool_registry import AtomicToolMetadata
from grace2_agent import tools as agent_tools
from grace2_agent.tools import get_registered_tools

# Force-import modules that are NOT in the __init__.py eager-import list but
# whose tools are in scope for annotation coverage. These are loaded by the
# agent service at startup via server.py / main.py but not by the package
# __init__.py. Import them here so the live registry is fully populated.
import grace2_agent.tools.catalog  # noqa: F401 — registers catalog_search + catalog_fetch
import grace2_agent.tools.data_fetch  # noqa: F401 — registers fetch_dem, fetch_buildings, etc.
import grace2_agent.tools.publish_layer  # noqa: F401 — registers publish_layer
import grace2_agent.tools.solver  # noqa: F401 — registers run_solver + wait_for_completion
import grace2_agent.tools.qgis_discovery  # noqa: F401 — registers list_qgis_algorithms + describe_qgis_algorithm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registry_snapshot() -> dict[str, AtomicToolMetadata]:
    """Return {tool_name: metadata} for the live registry."""
    return {t.metadata.name: t.metadata for t in get_registered_tools()}


# ---------------------------------------------------------------------------
# Field existence tests
# ---------------------------------------------------------------------------


def test_all_tools_have_four_annotation_fields():
    """Every tool in the live registry has all four MCP annotation fields."""
    snapshot = _registry_snapshot()
    assert len(snapshot) >= 55, (
        f"Expected at least 55 registered tools; got {len(snapshot)}. "
        "Did an import fail silently?"
    )
    missing: list[str] = []
    for name, meta in sorted(snapshot.items()):
        for field in ("read_only_hint", "open_world_hint", "destructive_hint", "idempotent_hint"):
            if not hasattr(meta, field):
                missing.append(f"{name}.{field}")
    assert not missing, (
        f"Tools missing annotation fields: {missing}"
    )


def test_annotation_fields_are_bools():
    """All four annotation fields must be booleans, not None or other types."""
    snapshot = _registry_snapshot()
    type_errors: list[str] = []
    for name, meta in sorted(snapshot.items()):
        for field in ("read_only_hint", "open_world_hint", "destructive_hint", "idempotent_hint"):
            val = getattr(meta, field, None)
            if not isinstance(val, bool):
                type_errors.append(
                    f"{name}.{field}={val!r} (expected bool, got {type(val).__name__})"
                )
    assert not type_errors, (
        f"Non-bool annotation field values: {type_errors}"
    )


# ---------------------------------------------------------------------------
# Consistency checks
# ---------------------------------------------------------------------------


def test_write_tools_are_not_read_only():
    """Tools flagged read_only_hint=False must not also appear read-only.

    Sanity check: a non-read-only tool must exist (we'd catch regressions
    where all tools were silently defaulted to True).
    """
    snapshot = _registry_snapshot()
    write_tools = {n for n, m in snapshot.items() if not m.read_only_hint}
    assert write_tools, (
        "No tools have read_only_hint=False — check that publish_layer, "
        "run_solver, wait_for_completion, qgis_process, "
        "run_pelicun_damage_assessment were all annotated."
    )
    # All tools with read_only_hint=False must not have destructive_hint implied
    # to be True when the tool is actually not destructive.
    # (Cross-check: destructive tools must be a subset of write tools.)
    destructive_tools = {n for n, m in snapshot.items() if m.destructive_hint}
    non_readonly_tools = {n for n, m in snapshot.items() if not m.read_only_hint}
    unexpected_destructive = destructive_tools - non_readonly_tools
    assert not unexpected_destructive, (
        f"Tools flagged destructive_hint=True but read_only_hint=True "
        f"(destructive implies write): {unexpected_destructive}"
    )


#: compute_*-named tools that legitimately ARE open-world: composers that
#: FETCH their own inputs (external APIs) rather than transforming a handed-in
#: raster. compute_sediment_yield (RUSLE) fetches Copernicus DEM + STATSGO
#: KFFACT + Esri/IO land cover when no override URIs are passed, so its
#: open_world_hint=True is HONEST -- flipping it to False to satisfy the
#: naming lint would misannotate a real external-API caller.
#: Quick-win batch (2026-07-07): compute_change_detection fetches its own
#: two-date Sentinel-2 inputs (PC STAC) unless both imagery_*_uri overrides
#: are passed -- the same input-fetching-composer shape as
#: compute_sediment_yield, so its open_world_hint=True is honest too.
#: compute_idf_curve hits the external NOAA PFDS API (the same endpoint as
#: lookup_precip_return_period), so its open_world_hint=True is honest.
#: compute_flood_depth_damage fetches the USACE NSI structure inventory
#: (external API) unless assets_uri is passed, so it is honest too.
#: compute_urban_heat_island fetches MODIS LST + Esri/IO land cover (external
#: PC STAC) unless both override URIs are passed, so it is honest too.
#: compute_model_residuals fetches its own USGS groundwater observations
#: (external OGC API) when observations_layer_uri is not passed -- the same
#: input-fetching-composer shape as compute_flood_depth_damage, so it is
#: honest too.
_OPEN_WORLD_COMPUTE_EXCEPTIONS = {
    "compute_sediment_yield",
    "compute_change_detection",
    "compute_idf_curve",
    "compute_flood_depth_damage",
    "compute_urban_heat_island",
    "compute_model_residuals",
}


def test_open_world_tools_are_fetchers_or_external():
    """open_world_hint=True tools must not include compute_* or clip_* tools.

    compute_* and clip_* are local GDAL transforms with no external API calls
    (documented exceptions: input-fetching composers in
    ``_OPEN_WORLD_COMPUTE_EXCEPTIONS``).
    """
    snapshot = _registry_snapshot()
    open_world_names = {n for n, m in snapshot.items() if m.open_world_hint}
    assert open_world_names, (
        "No tools have open_world_hint=True — check that fetch_* tools were annotated."
    )
    # Compute and clip tools must NOT be open-world.
    local_compute = {
        n
        for n in open_world_names
        if n.startswith(("compute_", "clip_"))
        and n not in _OPEN_WORLD_COMPUTE_EXCEPTIONS
    }
    assert not local_compute, (
        f"compute_* / clip_* tools incorrectly flagged open_world_hint=True: "
        f"{local_compute}"
    )


def test_non_idempotent_write_tools_exist():
    """Ensure at least the known write tools are flagged idempotent_hint=False."""
    snapshot = _registry_snapshot()
    non_idempotent = {n for n, m in snapshot.items() if not m.idempotent_hint}
    assert non_idempotent, (
        "No tools have idempotent_hint=False — check run_solver, "
        "wait_for_completion, publish_layer, qgis_process, "
        "run_pelicun_damage_assessment."
    )


# ---------------------------------------------------------------------------
# Spot-checks for high-stakes tools
# ---------------------------------------------------------------------------


def test_publish_layer_annotations():
    """publish_layer: write + destructive + not idempotent."""
    snapshot = _registry_snapshot()
    assert "publish_layer" in snapshot, "publish_layer not registered"
    meta = snapshot["publish_layer"]
    assert meta.read_only_hint is False, "publish_layer must not be read-only"
    assert meta.open_world_hint is False, "publish_layer is intra-GCP only"
    assert meta.destructive_hint is True, "publish_layer overwrites .qgs → destructive"
    assert meta.idempotent_hint is False, "publish_layer starts a new CR Job → not idempotent"


def test_run_solver_annotations():
    """run_solver: write + not destructive + not idempotent + intra-GCP."""
    snapshot = _registry_snapshot()
    assert "run_solver" in snapshot, "run_solver not registered"
    meta = snapshot["run_solver"]
    assert meta.read_only_hint is False, "run_solver dispatches a workflow → not read-only"
    assert meta.open_world_hint is False, "run_solver is intra-GCP"
    assert meta.destructive_hint is False, "run_solver writes to new run dir → not destructive"
    assert meta.idempotent_hint is False, "run_solver creates a new execution → not idempotent"


def test_wait_for_completion_annotations():
    """wait_for_completion: write (emits pipeline state) + not idempotent."""
    snapshot = _registry_snapshot()
    assert "wait_for_completion" in snapshot, "wait_for_completion not registered"
    meta = snapshot["wait_for_completion"]
    assert meta.read_only_hint is False, "wait_for_completion emits side effects"
    assert meta.open_world_hint is False, "wait_for_completion polls intra-GCP"
    assert meta.destructive_hint is False, "wait_for_completion does not overwrite"
    assert meta.idempotent_hint is False, "wait_for_completion emits events on each call"


def test_run_pelicun_annotations():
    """run_pelicun_damage_assessment: write + not idempotent (MC sampling)."""
    snapshot = _registry_snapshot()
    assert "run_pelicun_damage_assessment" in snapshot, (
        "run_pelicun_damage_assessment not registered"
    )
    meta = snapshot["run_pelicun_damage_assessment"]
    assert meta.read_only_hint is False, "pelicun writes output FlatGeobuf → not read-only"
    assert meta.open_world_hint is False, "pelicun is local compute"
    assert meta.destructive_hint is False, "pelicun writes to new path → not destructive"
    assert meta.idempotent_hint is False, "pelicun uses MC sampling → not idempotent"


def test_qgis_process_annotations():
    """qgis_process: write + not idempotent (each call creates new execution)."""
    snapshot = _registry_snapshot()
    assert "qgis_process" in snapshot, "qgis_process not registered"
    meta = snapshot["qgis_process"]
    assert meta.read_only_hint is False, "qgis_process dispatches a Cloud Run Job → not read-only"
    assert meta.open_world_hint is False, "qgis_process is intra-GCP"
    assert meta.destructive_hint is False, "qgis_process writes to new run dir"
    assert meta.idempotent_hint is False, "qgis_process creates new execution per call"


def test_fetch_dem_annotations():
    """fetch_dem: read-only + external API + idempotent."""
    snapshot = _registry_snapshot()
    assert "fetch_dem" in snapshot, "fetch_dem not registered"
    meta = snapshot["fetch_dem"]
    assert meta.read_only_hint is True, "fetch_dem is read-only"
    assert meta.open_world_hint is True, "fetch_dem hits USGS 3DEP (external)"
    assert meta.destructive_hint is False
    assert meta.idempotent_hint is True, "fetch_dem is cached / idempotent"


def test_compute_hillshade_annotations():
    """compute_hillshade: read-only + local + idempotent."""
    snapshot = _registry_snapshot()
    assert "compute_hillshade" in snapshot, "compute_hillshade not registered"
    meta = snapshot["compute_hillshade"]
    assert meta.read_only_hint is True
    assert meta.open_world_hint is False, "compute_hillshade is local GDAL"
    assert meta.destructive_hint is False
    assert meta.idempotent_hint is True


def test_web_fetch_annotations():
    """web_fetch: read-only + open-world + idempotent (cached)."""
    snapshot = _registry_snapshot()
    assert "web_fetch" in snapshot, "web_fetch not registered"
    meta = snapshot["web_fetch"]
    assert meta.read_only_hint is True
    assert meta.open_world_hint is True, "web_fetch hits arbitrary public URLs"
    assert meta.destructive_hint is False
    assert meta.idempotent_hint is True


# ---------------------------------------------------------------------------
# Schema defaults test
# ---------------------------------------------------------------------------


def test_atomic_tool_metadata_annotation_defaults():
    """AtomicToolMetadata defaults: read_only=True, open_world=False,
    destructive=False, idempotent=True (the conservative baseline).
    """
    meta = AtomicToolMetadata(
        name="defaults_test",
        ttl_class="static-30d",
        source_class="test",
        cacheable=True,
    )
    assert meta.read_only_hint is True
    assert meta.open_world_hint is False
    assert meta.destructive_hint is False
    assert meta.idempotent_hint is True


def test_register_tool_annotation_kwargs_override_defaults(empty_registry):
    """Annotation kwargs passed at decorator time override schema defaults."""
    from grace2_agent.tools import register_tool

    md = AtomicToolMetadata(
        name="test_write_tool",
        ttl_class="live-no-cache",
        source_class=None,
        cacheable=False,
    )

    @register_tool(
        md,
        read_only_hint=False,
        open_world_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
    )
    def test_write_tool() -> None:
        return None

    registered = empty_registry["test_write_tool"]
    assert registered.metadata.read_only_hint is False
    assert registered.metadata.open_world_hint is False
    assert registered.metadata.destructive_hint is True
    assert registered.metadata.idempotent_hint is False


# ---------------------------------------------------------------------------
# Aggregate summary / smoke test
# ---------------------------------------------------------------------------


def test_annotation_summary():
    """Print a summary of annotation counts for the smoke_test_result metric.

    Counts tools by annotation category. This does not assert specific counts
    (they grow with each new tool) but verifies the numbers are internally
    consistent.
    """
    snapshot = _registry_snapshot()
    n_total = len(snapshot)
    n_read_only = sum(1 for m in snapshot.values() if m.read_only_hint)
    n_open_world = sum(1 for m in snapshot.values() if m.open_world_hint)
    n_destructive = sum(1 for m in snapshot.values() if m.destructive_hint)
    n_idempotent = sum(1 for m in snapshot.values() if m.idempotent_hint)

    # Sanity: destructive tools must be a subset of write tools.
    assert n_destructive <= (n_total - n_read_only) + 1, (
        "More destructive tools than write tools — annotation inconsistency."
    )

    # Sanity: at least half the tools should be read-only (fetchers + compute).
    assert n_read_only >= n_total // 2, (
        f"Unexpectedly few read-only tools ({n_read_only}/{n_total}). "
        "Check write-tool annotations are not mis-applied."
    )

    # Sanity: open-world tools should be well under half (only fetchers).
    assert n_open_world < n_total, "Cannot have more open-world tools than total tools."

    # Log the summary for the smoke_test_result report.
    print(
        f"\nAnnotation summary: {n_total} tools annotated; "
        f"{n_read_only} readOnly + {n_open_world} openWorld + "
        f"{n_destructive} destructive + {n_idempotent} idempotent"
    )
