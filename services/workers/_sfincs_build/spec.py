"""The agent -> worker SFINCS build ``job_spec`` contract (plain JSON dict).

Mirror of the deck-builder worker's ``validate_build_spec`` (quadtree path). The
agent (``model_flood_scenario._compose_and_upload_flood_build_spec``) composes
this from the already-fetched input COG URIs + the serialized ForcingSpec /
BuildOptions, uploads it to S3, and hands the URI to the ``grace2-sfincs`` worker
via ``--build-spec-uri``. The worker validates + runs
``deck.build_sfincs_deck`` -> solve -> postprocess.

Schema (schema_version 1):

    {
      "schema_version": 1,
      "engine": "sfincs",
      "run_id": "<ulid>",
      "bbox": [min_lon, min_lat, max_lon, max_lat],   # EPSG:4326
      "nlcd_vintage_year": int | null,
      "inputs": {
        "dem_uri":        "s3://.../dem.tif",          # required
        "landcover_uri":  "s3://.../nlcd.tif",         # required
        "river_uri":      "s3://.../rivers.fgb" | null
      },
      "forcing": { ...serialized ForcingSpec... },     # see deck.forcing_spec_from_dict
      "options": { ...serialized BuildOptions... }     # see deck.build_options_from_dict
    }
"""

from __future__ import annotations

from typing import Any

#: Bumped whenever the job_spec shape changes incompatibly.
JOB_SPEC_SCHEMA_VERSION: int = 1

#: The object key the agent stages the spec under (per run prefix).
JOB_SPEC_FILENAME: str = "sfincs_build_spec.json"


def validate_job_spec(spec: Any) -> dict[str, Any]:
    """Validate + normalize the agent-composed SFINCS build job_spec.

    Raises ``ValueError`` on a non-dict body, an unknown schema_version, a bad
    bbox, or missing required inputs (dem_uri + landcover_uri). Returns the dict
    with ``forcing`` / ``options`` defaulted to ``{}`` when absent.
    """
    if not isinstance(spec, dict):
        raise ValueError("job_spec must be a JSON object")
    sv = spec.get("schema_version")
    if sv is None:
        raise ValueError("job_spec missing schema_version")
    if int(sv) != JOB_SPEC_SCHEMA_VERSION:
        raise ValueError(
            f"unknown job_spec schema_version {sv!r} "
            f"(this worker understands {JOB_SPEC_SCHEMA_VERSION})"
        )
    bbox = spec.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError(f"job_spec.bbox must be [w, s, e, n]; got {bbox!r}")
    try:
        bbox_f = [float(v) for v in bbox]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"job_spec.bbox must be numeric: {exc}") from exc

    inputs = spec.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("job_spec.inputs must be an object")
    if not inputs.get("dem_uri") or not inputs.get("landcover_uri"):
        raise ValueError("job_spec.inputs must carry dem_uri + landcover_uri")

    out = dict(spec)
    out["bbox"] = bbox_f
    out["inputs"] = dict(inputs)
    out["forcing"] = dict(spec.get("forcing") or {})
    out["options"] = dict(spec.get("options") or {})
    return out
