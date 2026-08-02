# 0030 - the AOI is pinned to the solve domain

Context: with no authoritative area-of-interest for a Case, `case.bbox` stayed
None and the LLM free-handed a different bbox for every follow-up tool call - one
observed case ran a SWMM solve on one extent, then fetched buildings on a box
87% as wide / 63% as tall, then rivers/DEM/roads each on yet another smaller box.
The data layers did not line up with the modeled domain.

Decision: the authoritative extent of a Case is its SOLVE domain (the peak-depth
/ mesh LayerURI bbox the workflow already floors and stamps), and it is PINNED to
the Case so every subsequent fetch/solve defaults to it.
- `_pin_case_aoi_from_solve` writes the solve-domain bbox onto the Case as the
  pinned AOI; `_pin_case_aoi_from_tool_bbox` is the fallback when no solve has
  run yet.
- `_maybe_default_fetch_bbox_to_pinned_aoi` and
  `_maybe_default_solver_bbox_to_pinned_aoi` default a call's missing/absent
  bbox to the pinned AOI so follow-ups cover the same extent, unless the user
  explicitly asks for a different area.

Consequence: follow-up data and analysis line up with the modeled domain by
default; the LLM no longer reinvents an extent per call. This is the founding
rationale for the whole AOI-pinning subsystem.
