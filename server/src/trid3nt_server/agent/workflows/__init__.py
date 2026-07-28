"""Deterministic workflows that compose atomic tools (FR-TA-1, Decision G).

Per the SRS two-layer tool architecture (Decision G + FR-TA-1, §2.3 Engine
catalog), workflows are **orchestrator-style Python functions** that compose
the engine's atomic tools (defined under ``server/src/trid3nt_server/
tools/``) into deterministic chains.

Workflows are **not atomic tools** -- they don't use ``@register_tool`` and they
don't have an ``AtomicToolMetadata`` of their own. The cache shim
(``tools/cache.py``) only mediates atomic-tool calls; workflows compose
already-cached + already-emitted atomic tools.

LLM exposure: a thin atomic-tool wrapper (the ``sfincs_flood`` engine template in
``sfincs/flood/flood.py``) lives in the registry so the LLM sees a single
invocable tool that triggers the workflow. The wrapper:

- declares ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
  ``source_class="workflow_dispatch"`` (a new FR-DC-6 source class for the
  workflow exposure surface - same shape as ``solver_dispatch``);
- forwards its arguments verbatim to the workflow body;
- returns the workflow's ``AssessmentEnvelope`` shape directly.

Invariant 2 (Deterministic workflows): workflows are LLM-free, stable-signature
Python composing atomic tools in tested sequences. Same inputs → byte-identical
SFINCS deck per the HydroMT determinism cited in
``docs/decisions/oq-4-hydromt-depth.md`` §3.

Workflows authored under this package:

- ``model_flood_scenario(bbox?, location_query?, event_id?, return_period_yr=100,
   duration_hr=24, compute_class="medium") → AssessmentEnvelope`` -- composes
  geocode → fetch_dem → fetch_landcover → fetch_river_geometry →
  lookup_precip_return_period → build_sfincs_model → run_solver →
  wait_for_completion → postprocess_flood. The §4 Invariant-7 NLCD
  validation gate (``LULC_MAPPING_MISMATCH``) fires inside
  ``build_sfincs_model`` (``sfincs_builder.py``) before HydroMT's roughness
  component runs.
"""

from __future__ import annotations

# Import the workflow modules so their @register_tool decorators fire at
# package import time and the LLM-facing wrappers land in TOOL_REGISTRY.
from .sfincs.flood.flood import sfincs_flood as _sfincs_flood  # noqa: F401  -- engine-door refactor (SFINCS slice): the run_model_flood_scenario wrapper is now the sfincs_flood template (engine=sfincs, tier=template); the run_sfincs door is imported in tools/__init__.py
from .modflow.model_groundwater_contamination_scenario import model_groundwater_contamination_scenario as _model_groundwater_contamination_scenario  # noqa: F401 - Case 2 composer (news → MODFLOW → plume)
# engine-door refactor: run_model_contamination_affected_fields is CUT (composer
# removed). Its plume half IS modflow_contaminant_plume; the zonal field-scoring
# half re-homes to a playground recipe (docs/playbooks/modflow-affected-fields-recipe.md).
from .shared.model_news_event_ingest import model_news_event_ingest as _model_news_event_ingest  # noqa: F401 - Case 2 composer
from .sfincs.model_nws_flood_event_scenario import model_nws_flood_event_scenario as _model_nws_flood_event_scenario  # noqa: F401 - Case 3 composer
# PELICUN fold: the former pelicun_damage_with_buildings composer folded into the
# pelicun_damage_assessment template's bbox AUTO-FETCH input mode; the template is
# registered via tools/__init__.py's import of
# workflows/pelicun/damage_assessment/damage_assessment.py (no separate import here).
from .shared.model_conservation_priority import model_conservation_priority as _model_conservation_priority  # noqa: F401  -- conservation micro-North-Star composer (NAIP base + NDVI + MoBI + GBIF + IUCN); registers run_model_conservation_priority
from .telemac import run_telemac as _run_telemac  # noqa: F401  -- P2: registers the telemac_river_dye local-docker solve spec (SOLVER_WORKFLOW_REGISTRY + LOCAL_SOLVER_SPEC_REGISTRY); no LLM tool yet (P4)

__all__: list[str] = []
