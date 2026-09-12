"""The atomic-tool registry: ``@register_tool`` collects decorated functions into
``TOOL_REGISTRY`` at import time, keyed by ``metadata.name``. Importing this package
eagerly imports every tool module so a registration-time ``ValidationError`` or
``ToolRegistrationError`` surfaces at startup rather than at first use. The cache
shim that mediates external-API calls lives in ``.cache``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

__all__ = [
    "RegisteredTool",
    "ToolRegistrationError",
    "TOOL_REGISTRY",
    "MOUNTED_TOOLS",
    "mount_tool",
    "mounted_tool_names",
    "unmount_tool",
    "register_tool",
    "get_registered_tools",
    "clear_registry_for_tests",
]


class ToolRegistrationError(RuntimeError):
    """Raised when a tool fails registration (duplicate name, bad metadata)."""


@dataclass(frozen=True)
class RegisteredTool:
    """One entry in ``TOOL_REGISTRY``. ``fn`` is the ORIGINAL undecorated callable -
    the registry deliberately never wraps it - and ``module`` is its ``__module__``
    at registration time."""

    metadata: AtomicToolMetadata
    fn: Callable[..., Any]
    module: str


#: Module-level registry, keyed by ``metadata.name``. Populated at import time by
#: ``@register_tool`` calls in submodules; the agent service reads it once at
#: startup through ``get_registered_tools()`` to build its tool declarations.
TOOL_REGISTRY: dict[str, RegisteredTool] = {}


# WHAT MAY BE A TOOL. An atomic tool is a fetcher or an irreducible primitive -
# a thing that reads the world, or an operation the caller cannot assemble out
# of the ones already here. An ANALYSIS is neither: several tools composed, a
# threshold applied, a number derived from two layers. It is written as code and
# run in the box, where the caller can vary it, and it is not registered.
#
# A registered analysis is one question's answer frozen into the surface. It
# takes the arguments its author thought of, it answers the neighbouring
# question wrong or not at all, and every tool beside it is a name retrieval has
# to rank. The surface stays small so the model can see it whole.
def register_tool(
    metadata: AtomicToolMetadata,
    *,
    supports_global_query: bool | None = None,
    payload_mb_estimator_name: str | None = None,
    read_only_hint: bool | None = None,
    open_world_hint: bool | None = None,
    destructive_hint: bool | None = None,
    idempotent_hint: bool | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """A decorator recording ``fn`` + ``metadata`` in ``TOOL_REGISTRY`` and giving
    back ``fn`` UNCHANGED. A kwarg left ``None`` keeps what the metadata declares;
    a duplicate name raises ``ToolRegistrationError`` at IMPORT time."""
    if not isinstance(metadata, AtomicToolMetadata):
        raise TypeError(
            f"register_tool expects AtomicToolMetadata, got {type(metadata).__name__}"
        )

    # Decorator-level flags fold into a fresh metadata. ``model_copy(update=...)``
    # re-runs the validators because ``GraceModel`` sets ``validate_assignment``,
    # so a bad combination still fails fast at import time.
    overrides: dict[str, Any] = {}
    if supports_global_query is not None:
        overrides["supports_global_query"] = supports_global_query
    if payload_mb_estimator_name is not None:
        overrides["payload_mb_estimator_name"] = payload_mb_estimator_name
    if read_only_hint is not None:
        overrides["read_only_hint"] = read_only_hint
    if open_world_hint is not None:
        overrides["open_world_hint"] = open_world_hint
    if destructive_hint is not None:
        overrides["destructive_hint"] = destructive_hint
    if idempotent_hint is not None:
        overrides["idempotent_hint"] = idempotent_hint
    if overrides:
        metadata = metadata.model_copy(update=overrides)

    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = metadata.name
        existing = TOOL_REGISTRY.get(name)
        if existing is not None:
            raise ToolRegistrationError(
                f"tool {name!r} is already registered "
                f"(existing from module {existing.module!r}, "
                f"new from module {fn.__module__!r}); duplicate registrations "
                f"are rejected at import time per FR-CE-8."
            )
        TOOL_REGISTRY[name] = RegisteredTool(
            metadata=metadata, fn=fn, module=fn.__module__
        )
        return fn

    return _decorator


#: Names in ``TOOL_REGISTRY`` that a live session MOUNTED rather than an import
#: registered. They come and go with the thing they act on, so every visibility
#: floor must carry them by name: the retrieval index is built from the tools
#: that existed when it was built and can never rank one of these.
MOUNTED_TOOLS: set[str] = set()


def mount_tool(metadata: AtomicToolMetadata,
               fn: Callable[..., Any]) -> str:
    """Add one session-scoped tool to the registry -> its name. A name already
    registered, mounted or imported, is REFUSED rather than replaced: the caller
    would be shadowing a tool it does not own."""
    name = metadata.name
    existing = TOOL_REGISTRY.get(name)
    if existing is not None:
        raise ToolRegistrationError(
            f"tool {name!r} is already registered (from module "
            f"{existing.module!r}); a mounted tool cannot shadow it.")
    TOOL_REGISTRY[name] = RegisteredTool(
        metadata=metadata, fn=fn, module=getattr(fn, "__module__", "<mounted>"))
    MOUNTED_TOOLS.add(name)
    return name


def unmount_tool(name: str) -> None:
    """Remove a MOUNTED tool. An imported tool is never removed by this seam."""
    if name not in MOUNTED_TOOLS:
        return
    MOUNTED_TOOLS.discard(name)
    TOOL_REGISTRY.pop(name, None)


def mounted_tool_names() -> frozenset[str]:
    """The currently mounted tool names, as a visibility floor."""
    return frozenset(MOUNTED_TOOLS)


def get_registered_tools() -> list[RegisteredTool]:
    """A snapshot of the registry sorted by ``metadata.name``, so the declaration
    order is deterministic across runs."""
    return sorted(TOOL_REGISTRY.values(), key=lambda t: t.metadata.name)


def clear_registry_for_tests() -> None:
    """Empty the registry. ONLY for tests; never call from product code."""
    TOOL_REGISTRY.clear()
    MOUNTED_TOOLS.clear()


# Eager submodule import (fail-fast).
#
# Importing ``trid3nt_server.tools`` populates ``TOOL_REGISTRY`` with EVERY
# atomic tool the service supports: each module below carries at least one
# ``@register_tool`` decorator that fires at import time, so a registration-time
# ``ValidationError`` / ``ToolRegistrationError`` surfaces at startup rather than
# at first use. The block is EXPLICIT (no pkgutil walk), sorted, and grouped by
# subpackage; regenerate it when adding a tool module.
#
# Almost every fetcher is SPEC-DRIVEN - a co-located source.yaml plus its hooks,
# registered by the tree walk below - so adding a source is adding a YAML, not an
# import line here. Only a fetcher with a hand-written module appears in this
# block.

# -- fetchers/climate --
from .fetchers.climate.lookup_precip_return_period import lookup_precip_return_period  # noqa: E402,F401

# -- fetchers/socioeconomic --
from .fetchers.socioeconomic.geocode_location import geocode_location  # noqa: E402,F401

# -- fetchers/_router: the walk over fetchers/**/source.yaml. Each promoted spec
# registers under its own tool name at tier="general", the default retrieval pool.
from .fetchers._router.registration import register_specs_from_tree as _register_router_specs  # noqa: E402,F401

_register_router_specs()

# -- derive (compute / clip / extract / vector-edit / charts) --
# The two generic geometry composition links: one document out of several layers
# (``combine``), and the two ends of a line (``endpoints``).
from .derive.combine import combine  # noqa: E402,F401
from .derive.compute_cross_section import compute_cross_section  # noqa: E402,F401
from .derive.compute_exposure_summary import compute_exposure_summary  # noqa: E402,F401
from .derive.compute_flood_depth_damage import compute_flood_depth_damage  # noqa: E402,F401
# flood-extent skill (raster/vector confusion).
from .derive.compute_flood_extent_skill import compute_flood_extent_skill  # noqa: E402,F401
from .derive.compute_idf_curve import compute_idf_curve  # noqa: E402,F401
from .derive.compute_layer_bounds import compute_layer_bounds  # noqa: E402,F401
from .derive.compute_model_residuals import compute_model_residuals  # noqa: E402,F401
from .derive.compute_sediment_yield import compute_sediment_yield  # noqa: E402,F401
# model-fit skill metrics (spotpy).
from .derive.compute_skill_metrics import compute_skill_metrics  # noqa: E402,F401
from .derive.delineate_watershed import delineate_watershed  # noqa: E402,F401
# A line through a shape's centroid along a bearing: what a profile is read along
# when the read runs ACROSS a feature rather than down the domain's own axis.
from .derive.derive_transect import derive_transect  # noqa: E402,F401
from .derive.digitize_water_body import digitize_water_body  # noqa: E402,F401
from .derive.endpoints import endpoints  # noqa: E402,F401
# model-vs-observation pairing primitive.
from .derive.extract_model_at_observations import extract_model_at_observations  # noqa: E402,F401
from .derive.extract_stream_network import extract_stream_network  # noqa: E402,F401
from .derive.extract_timeseries_at_point import extract_timeseries_at_point  # noqa: E402,F401
from .derive.charts.generate_chart import generate_chart  # noqa: E402,F401
from .derive.probe_point import probe_point  # noqa: E402,F401
from .derive.query_point_hazard import query_point_hazard  # noqa: E402,F401
from .derive.restyle_layer import restyle_layer  # noqa: E402,F401 - DISPLAY-state re-emission of an already-published layer
# The two session tools: each is a request on the plugin wire, run in the
# user's own QGIS session; the code one never runs without the approval card.
from .derive.run_pyqgis import run_pyqgis  # noqa: E402,F401
from .derive.run_qgis_algorithm import run_qgis_algorithm  # noqa: E402,F401
from .derive.section import section  # noqa: E402,F401

# -- simulation (engine bridges, model_* engines, solver seam) --
# Run-diagnostics dispatcher: one registered tool over the per-engine parser
# modules under workflows/solver/diagnostics/, which are NOT themselves registered.
from trid3nt_server.workflows.solver.diagnostics import read_run_diagnostics  # noqa: E402,F401
from trid3nt_server.tools.derive.model_debris_flow import model_debris_flow  # noqa: E402,F401
# Derive a run from a run, with named values moved: the recalibration interface.
from trid3nt_server.workflows.runtime.rerun import rerun_workflow  # noqa: E402,F401
from trid3nt_server.workflows.solver import solver  # noqa: E402,F401
# -- engine templates: tier=template members are ordinary retrieval-pool tools,
# registered by their own @register_tool in the workflow-composer block below and
# callable DIRECTLY. The solver seam they import is a separate module and registers
# no tool of its own.

# -- discovery (dataset/tool retrieval) --
from .search.search_tools import search_tools  # noqa: E402,F401
from .search.search_spatial_functions import search_spatial_functions  # noqa: E402,F401
# ESRI Living Atlas: a scoped search over the harvested catalog plus a generic
# fetch bridge. Registered here, in-process, so both surface in the tool-retrieval
# index for their corpus queries. The two harvested YAML catalogs are DATA.
from .search.search_living_atlas import search_living_atlas  # noqa: E402,F401
from .search.fetch_living_atlas_layer import fetch_living_atlas_layer  # noqa: E402,F401

# -- meta (web fetch, case utilities) --
from .meta.compose_case_report import compose_case_report  # noqa: E402,F401
from .meta.list_run_frames import list_run_frames  # noqa: E402,F401
# describe_keywords: the READ over the TELEMAC module catalogs - the only way the
# keyword surface is reached, since no docstring budget carries it.
from trid3nt_server.workflows.telemac.modules.describe import describe_keywords  # noqa: E402,F401
from .meta.spatial_input_tool import spatial_input_tool  # noqa: E402,F401
from .search.web_fetch import web_fetch  # noqa: E402,F401

# Workflow-composer registrations; each module carries its OWN @register_tool.
# The five REACH templates (engine="telemac", tier="template"). ONE TEMPLATE PER
# QUESTION: a tracer, an oil slick, a moving bed, a settling class and an oxygen
# sag fill DIFFERENT slots of the same deck, so routing picks a template rather
# than a substance word picking a branch. All five LIST the shared river part,
# whose two end faces are the transects the inflow and the outflow are prescribed
# on; the edge length is an explicit sheet value on every one of them.
from trid3nt_server.workflows.telemac.templates.river_dye.river_dye import telemac_river_dye as _telemac_river_dye  # noqa: E402,F401 - reach conservative-plume front (engine=telemac, tier=template)
from trid3nt_server.workflows.telemac.templates.do_sag.do_sag import telemac_do_sag as _telemac_do_sag  # noqa: E402,F401 - reach dissolved-oxygen front (engine=telemac, tier=template)
from trid3nt_server.workflows.telemac.templates.river_oil_spill.river_oil_spill import telemac_river_oil_spill as _telemac_river_oil_spill  # noqa: E402,F401 - reach oil-slick front (engine=telemac, tier=template)
from trid3nt_server.workflows.telemac.templates.river_scour.river_scour import telemac_river_scour as _telemac_river_scour  # noqa: E402,F401 - reach mobile-bed front (engine=telemac, tier=template)
from trid3nt_server.workflows.telemac.templates.river_sediment_plume.river_sediment_plume import telemac_river_sediment_plume as _telemac_river_sediment_plume  # noqa: E402,F401 - reach suspended-sediment front (engine=telemac, tier=template)
# The CATCHMENT front. Its one liquid boundary is declared on the mesh ask at the
# delineation's snapped pour point, so the outlet hydrograph is the flux through
# the nodes that role landed on.
from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import telemac_rain_on_grid as _telemac_rain_on_grid  # noqa: E402,F401 - catchment rainfall-runoff front (engine=telemac, tier=template)
# The ARTEMIS phase-resolving elliptic mild-slope engine, asked ONE question: does
# the declared structure shelter the water behind it. The domain is cut from the
# real shoreline with the structure punched out conformally and every deep boundary
# stretch designated open; the bed is surveyed topobathy.
from trid3nt_server.workflows.telemac.templates.agitation.agitation import artemis_harbor_agitation as _artemis_harbor_agitation  # noqa: E402,F401 - ARTEMIS agitation front (engine=telemac, tier=template)
# The TELEMAC-3D baroclinic engine, asked ONE question: what the column does over
# the depth a 2D model averages away. Its domain is the water body's own mapped
# polygon cut to the AOI, a CLOSED basin.
from trid3nt_server.workflows.telemac.templates.stratified_flow.stratified_flow import telemac3d_stratified_flow as _telemac3d_stratified_flow  # noqa: E402,F401 - TELEMAC-3D stratified front (engine=telemac, tier=template)
# build_mesh: the one mesh router. A RECIPE - three mesher-agnostic params plus an
# ordered list of verbatim calls on the wrapped mesh library and on the shared
# primitives. Declared in a template it is a frozen lazy ask; called standalone it
# builds now and stashes the artifact in the case. Importing it registers every
# mesher behind it.
from trid3nt_server.workflows.mesh.tool import build_mesh as _build_mesh  # noqa: E402,F401 - mesh domain primitive (tier=general)
# mesh_op: append, alter or remove one call on the recipe of the mesh open at the
# gate, then regenerate. The whole of the mesh-refinement loop.
from trid3nt_server.workflows.mesh.op_tool import mesh_op as _mesh_op  # noqa: E402,F401 - mesh domain primitive (tier=general)


# COPY-ME authoring template. Importing it is always safe: its @register_tool call
# is gated behind TRID3NT_ENABLE_EXAMPLE_TOOL, so by default the module is
# imported-but-inert and never pollutes the production catalog.
from . import _example_tool_template  # noqa: E402,F401 - INERT unless TRID3NT_ENABLE_EXAMPLE_TOOL is set
