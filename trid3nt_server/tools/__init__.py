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


#: Keyed by ``metadata.name``; populated at import time by ``@register_tool``.
TOOL_REGISTRY: dict[str, RegisteredTool] = {}


# An atomic tool is a fetcher or an irreducible primitive. An ANALYSIS (tools composed, a threshold applied)
# is code run in the box, not a registration: it freezes one question's answer into the surface and
# every extra tool is a name retrieval must rank.
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

    # ``model_copy`` re-runs the validators (``validate_assignment``), so a bad combination fails at import.
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


#: Tools a live session MOUNTED rather than an import registering. Visibility floors carry them by name:
#: the retrieval index predates them and can never rank one.
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


# Eager import so a registration error surfaces at startup. Explicit and sorted; row-driven fetchers
# register through the tree walk below and need no line here.

from .fetchers.climate.lookup_precip_return_period import lookup_precip_return_period  # noqa: E402,F401


from .fetchers.socioeconomic.geocode_location import geocode_location  # noqa: E402,F401

from .fetchers._router.registration import register_specs_from_tree as _register_router_specs  # noqa: E402,F401

_register_router_specs()

from .derive.charts.generate_chart import generate_chart  # noqa: E402,F401
from .derive.fill_nodata import fill_nodata  # noqa: E402,F401
from .derive.merge_rasters import merge_rasters  # noqa: E402,F401
from .derive.probe_point import probe_point  # noqa: E402,F401
from .derive.restyle_layer import restyle_layer  # noqa: E402,F401
from .derive.run_pyqgis import run_pyqgis  # noqa: E402,F401
from .derive.run_qgis_algorithm import run_qgis_algorithm  # noqa: E402,F401

from trid3nt_server.workflows.solver.diagnostics import read_run_diagnostics  # noqa: E402,F401
from trid3nt_server.workflows.solver import solver  # noqa: E402,F401

from .search.search_tools import search_tools  # noqa: E402,F401
from .search.find_sources import find_sources  # noqa: E402,F401
from .search.describe_keywords.describe_keywords import describe_keywords  # noqa: E402,F401
from trid3nt_server.inputs.gate import spatial_input_tool  # noqa: E402,F401

from trid3nt_server.workflows.telemac.templates.dye_release.dye_release import telemac_dye_release as _telemac_dye_release  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.do_sag.do_sag import telemac_do_sag as _telemac_do_sag  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.oil_spill.oil_spill import telemac_oil_spill as _telemac_oil_spill  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.bed_scour.bed_scour import telemac_bed_scour as _telemac_bed_scour  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.sediment_plume.sediment_plume import telemac_sediment_plume as _telemac_sediment_plume  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.water_temperature.water_temperature import telemac_water_temperature as _telemac_water_temperature  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.micropollutant_release.micropollutant_release import telemac_micropollutant_release as _telemac_micropollutant_release  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.eutrophication.eutrophication import telemac_eutrophication as _telemac_eutrophication  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.ice_cover.ice_cover import telemac_ice_cover as _telemac_ice_cover  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.channel_dredging.channel_dredging import telemac_channel_dredging as _telemac_channel_dredging  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.rain_on_grid.rain_on_grid import telemac_rain_on_grid as _telemac_rain_on_grid  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.agitation.agitation import artemis_harbor_agitation as _artemis_harbor_agitation  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.nearshore_waves.nearshore_waves import tomawac_nearshore_waves as _tomawac_nearshore_waves  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.wave_driven_currents.wave_driven_currents import tomawac_wave_driven_currents as _tomawac_wave_driven_currents  # noqa: E402,F401
from trid3nt_server.workflows.telemac.templates.stratified_flow.stratified_flow import telemac3d_stratified_flow as _telemac3d_stratified_flow  # noqa: E402,F401
from trid3nt_server.tools.mesh.tool import build_mesh as _build_mesh  # noqa: E402,F401
from trid3nt_server.tools.mesh.op_tool import mesh_op as _mesh_op  # noqa: E402,F401
