"""Generate JSON Schema files for every contract.

Run via the ``trid3nt-export-schemas`` console script or:

    python -m trid3nt_contracts.export_schemas [OUTPUT_DIR]

Default OUTPUT_DIR is ``contracts/schemas`` (resolved relative to this
package's repo location). Each top-level contract model is written to
``<name>.json``. Regeneration is idempotent: re-running produces byte-identical
files for an unchanged contract set, so a CI drift check can ``git diff`` them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from . import catalog, collections, envelope, event, execution, tool_registry, ws

# (filename stem, model) for every top-level contract we export.
_EXPORTS: list[tuple[str, type[BaseModel]]] = [
    #
    ("assessment_envelope", envelope.AssessmentEnvelope),
    #
    ("event_metadata", event.EventMetadata),
    ("claim_set", event.ClaimSet),
    ("numeric_claim", event.NumericClaim),
    # collections
    ("project_document", collections.ProjectDocument),
    ("run_document", collections.RunDocument),
    ("article_document", collections.ArticleDocument),
    ("event_document", collections.EventDocument),
    ("session_document", collections.SessionDocument),
    # sprint-08 additions (Mode 1 catalog substrate, §F.1.2)
    ("catalog_entry_document", collections.CatalogEntryDocument),
    ("catalog_audit_log_document", collections.CatalogAuditLogDocument),
    # PipelineStepSummary - exported standalone so the
    # extended field surface (progress_percent / error_code / error_message)
    # is independently inspectable by the client mirror + agent emitter
    # (sprint-06 M4 pre-flight; closes OQ-W-26).
    ("pipeline_step_summary", collections.PipelineStepSummary),
    #
    ("catalog_entry", catalog.CatalogEntry),
    # solver shapes
    ("model_setup", execution.ModelSetup),
    ("execution_handle", execution.ExecutionHandle),
    ("run_result", execution.RunResult),
    ("layer_uri", execution.LayerURI),
    # / atomic-tool registration metadata
    ("atomic_tool_metadata", tool_registry.AtomicToolMetadata),
]


def _ws_message_exports() -> list[tuple[str, type[BaseModel]]]:
    """One schema per WebSocket message payload."""
    out: list[tuple[str, type[BaseModel]]] = []
    for msg_type, model in sorted(ws.ALL_PAYLOADS.items()):
        # ws_<kebab-with-underscores>.json
        stem = "ws_" + msg_type.replace("-", "_")
        out.append((stem, model))
    return out


def default_output_dir() -> Path:
    """``contracts/schemas`` relative to this file."""
    # this file: contracts/trid3nt_contracts/export_schemas.py
    # parents[0]=trid3nt_contracts, parents[1]=contracts -> contracts/schemas
    return Path(__file__).resolve().parents[1] / "schemas"


#: The exact command that rewrites ``contracts/schemas`` from the live models.
#: The drift gate quotes this verbatim, so it must stay runnable from repo root.
REGEN_COMMAND = "./venvs/agent/bin/python -m trid3nt_contracts.export_schemas"


def render_schemas() -> dict[str, str]:
    """Serialize every contract's JSON Schema IN MEMORY: filename -> file text.

    The single serialization path. ``export`` writes exactly what this returns,
    so a drift gate can compare committed bytes against it without touching the
    filesystem. Rendering must therefore be pure: no I/O, no mkdir.
    """
    rendered: dict[str, str] = {}
    for stem, model in [*_EXPORTS, *_ws_message_exports()]:
        schema = model.model_json_schema()
        # sort_keys + trailing newline => stable, diff-friendly output.
        rendered[f"{stem}.json"] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return rendered


def export(output_dir: Path) -> list[Path]:
    """Write every contract's JSON Schema to ``output_dir``. Returns the paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, text in render_schemas().items():
        path = output_dir / filename
        path.write_text(text)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    output_dir = Path(argv[0]) if argv else default_output_dir()
    written = export(output_dir)
    print(f"Wrote {len(written)} schema files to {output_dir}")
    for path in written:
        print(f"  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
