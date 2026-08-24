"""``RunArchetype`` - the shared MODFLOW deck-and-solve step.

The whole archetype family reaches mf6 the same way: assemble ``MODFLOWRunArgs``,
hand it to ``run_modflow_archetype_job``, and check that what came back is the
archetype's own typed layer. The per-question difference is the geometry fields
that go INTO the args, which is the one hook a template overrides - the
template-method shape the design doc names, made structural for MODFLOW.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.modflow_contracts import MODFLOWRunArgs

from trid3nt_server.workflows.lib import Step
from trid3nt_server.emission.pipeline_emitter import current_emitter

from .errors import ModflowArchetypeRunError

__all__ = ["RunArchetype", "run_archetype", "run_id_of"]

logger = logging.getLogger("trid3nt_server.workflows.modflow.steps.archetype")

_STEPS = "trid3nt_server.workflows.modflow.steps.archetype"

#: ``MODFLOWRunArgs`` is the plume-shaped envelope every archetype shares, so the
#: GWF-only archetypes have to fill transport fields their deck writer never
#: reads. They are inert here; the args model is what would have to change.
_INERT_TRANSPORT = {"contaminant": "n/a", "release_rate_kg_s": 1.0,
                    "duration_days": 1.0}


class RunArchetype:
    """MODFLOW archetype solves. One constructor per archetype question."""

    @staticmethod
    def regional_water_budget(**kwargs: Any) -> Step:
        """The steady GWF regional budget: deck -> mf6 -> cell-by-cell partition."""
        return Step(runner=f"{_STEPS}.run_archetype",
                    kwargs={"archetype": "regional_water_budget", **kwargs},
                    consequential=True)


async def run_archetype(
    *,
    archetype: str,
    expected_type: str,
    aoi_latlon: tuple[float, float] | list[float],
    aquifer_k_ms: float,
    porosity: float,
    compute_class: str,
    tool_label: str,
    **archetype_fields: Any,
) -> Any:
    """Assemble the run args, solve, and return the archetype's typed layer.

    Imported lazily inside the body: the run tool pulls in the heavy solver seam,
    and a declared step should not make its module an import-time dependency of
    the workflow package.
    """
    from trid3nt_server.data.simulation.modflow.run_modflow_archetype_tool import (
        run_modflow_archetype_job,
    )
    from trid3nt_server.emission.layer_uri_emit import emit_layer_uri

    layer_type = _load_layer_type(expected_type)
    lat, lon = float(aoi_latlon[0]), float(aoi_latlon[1])
    try:
        run_args = MODFLOWRunArgs(
            spill_location_latlon=(lat, lon), archetype=archetype,
            aquifer_k_ms=float(aquifer_k_ms), porosity=float(porosity),
            **_INERT_TRANSPORT, **archetype_fields,
        )
    except Exception as exc:  # noqa: BLE001 - a bad arg set is the caller's, typed
        raise ModflowArchetypeRunError(
            f"invalid {archetype} run arguments: {exc}",
            error_code="MODFLOW_ARCHETYPE_ARGS_INVALID") from exc

    logger.info("%s: solving archetype=%s at (%.5f, %.5f) k=%.3g m/s n=%.3f",
                tool_label, archetype, lat, lon, run_args.aquifer_k_ms,
                run_args.porosity)
    result = await run_modflow_archetype_job(run_args, compute_class=compute_class)
    if not isinstance(result, layer_type):
        code, message = _failure(result, archetype)
        raise ModflowArchetypeRunError(message, error_code=code)

    # The dispatch's add_loaded_layer gate fires only on a bare-LayerURI tool
    # return, and these templates return an envelope dict - so the archetype layer
    # reaches the map here or not at all. Dedups by layer identity.
    emitter = current_emitter()
    if emitter is not None:
        emit_layer = emit_layer_uri(result)
        if emit_layer is not None:
            await emitter.add_loaded_layer(emit_layer)
    return result


def _failure(result: Any, archetype: str) -> tuple[str, str]:
    if isinstance(result, dict):
        return (str(result.get("error_code") or "MODFLOW_ARCHETYPE_RUN_FAILED"),
                str(result.get("error_message")
                    or f"the {archetype} run produced no layer"))
    return ("MODFLOW_ARCHETYPE_RUN_FAILED",
            f"the {archetype} run did not produce the expected layer")


def _load_layer_type(dotted: str) -> type:
    import importlib

    module_path, _, attr = dotted.rpartition(".")
    return getattr(importlib.import_module(module_path), attr)


def run_id_of(layer: Any) -> str | None:
    """The run prefix a published archetype layer sits under, from its own uri.

    The archetype layers carry no ``run_id`` field, and the run's chart spec and
    metrics have to land beside the artifacts the solve already wrote.
    """
    uri = getattr(layer, "uri", None)
    if not isinstance(uri, str) or not uri.startswith("s3://"):
        return None
    _bucket, _, key = uri[len("s3://"):].partition("/")
    head = key.split("/", 1)[0]
    return head or None
