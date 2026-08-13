"""Confirm-card / gate-card builders extracted from ``server``.

Pure payload/envelope builders (no websocket, no session state) for the
tool-confirmation gates. Transport-coupled orchestration stays in ``server``.
"""
from .credential import _build_credential_request_payload
from .payload_warning import (
    _get_hard_cap_mb,
    _get_warning_threshold_mb,
    _resolve_payload_estimator,
)
from .region_choice import (
    _build_region_candidates,
    _build_region_choice_request_payload,
    _region_admin_level_for,
)
from .solver_confirm import (
    MAX_FETCH_PX,
    _FETCH_MAX_PX_BY_TOOL,
    _LANDCOVER_DEFAULT_RES_M,
    _build_fetch_resolution_envelope,
    _build_fire_confirm_envelope,
    _build_flood_run_settings_envelope,
    _build_geoclaw_confirm_envelope,
    _build_psha_confirm_envelope,
    _build_scenario_confirm_envelope,
    _build_swmm_granularity_envelope,
    _build_telemac_mesh_envelope,
    _clamp_fetch_resolution,
    _clamp_swmm_resolution_to_cap,
    _gate_memory_key,
    _local_compute_lane,
)
from .spatial_input import (
    _build_spatial_input_request_payload,
    _spatial_response_to_result,
)

__all__ = [
    "MAX_FETCH_PX",
    "_FETCH_MAX_PX_BY_TOOL",
    "_LANDCOVER_DEFAULT_RES_M",
    "_build_credential_request_payload",
    "_build_fetch_resolution_envelope",
    "_build_fire_confirm_envelope",
    "_build_flood_run_settings_envelope",
    "_build_geoclaw_confirm_envelope",
    "_build_psha_confirm_envelope",
    "_build_scenario_confirm_envelope",
    "_build_region_candidates",
    "_build_region_choice_request_payload",
    "_build_spatial_input_request_payload",
    "_build_swmm_granularity_envelope",
    "_build_telemac_mesh_envelope",
    "_clamp_fetch_resolution",
    "_clamp_swmm_resolution_to_cap",
    "_gate_memory_key",
    "_get_hard_cap_mb",
    "_get_warning_threshold_mb",
    "_local_compute_lane",
    "_region_admin_level_for",
    "_resolve_payload_estimator",
    "_spatial_response_to_result",
]
