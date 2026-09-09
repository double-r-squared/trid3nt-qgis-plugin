"""Render the JSON Schema of every contract into ``contracts/schemas``.

Regeneration is idempotent - an unchanged contract set produces byte-identical
files - so a drift gate can compare committed bytes against a fresh render.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel

from . import catalog, collections, envelope, execution, tool_registry, ws

# (filename stem, model) for every top-level contract we export.
_EXPORTS: list[tuple[str, type[BaseModel]]] = [
    ("assessment_envelope", envelope.AssessmentEnvelope),
    # collections
    ("project_document", collections.ProjectDocument),
    ("run_document", collections.RunDocument),
    ("article_document", collections.ArticleDocument),
    ("session_document", collections.SessionDocument),
    # catalog substrate
    ("catalog_entry_document", collections.CatalogEntryDocument),
    ("catalog_audit_log_document", collections.CatalogAuditLogDocument),
    # Exported standalone as well as inside its parent, so a client mirroring
    # the step surface can type against it on its own.
    ("pipeline_step_summary", collections.PipelineStepSummary),
    ("catalog_entry", catalog.CatalogEntry),
    # solver shapes
    ("model_setup", execution.ModelSetup),
    ("execution_handle", execution.ExecutionHandle),
    ("run_result", execution.RunResult),
    ("layer_uri", execution.LayerURI),
    # atomic-tool registration metadata
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
    # parents[0] is the package, parents[1] the distribution root.
    return Path(__file__).resolve().parents[1] / "schemas"


#: The exact command that rewrites ``contracts/schemas`` from the live models.
#: Quoted verbatim by the drift gate, so it must stay runnable from repo root.
REGEN_COMMAND = "./venvs/agent/bin/python -m trid3nt_contracts.export_schemas"


def render_schemas() -> dict[str, str]:
    """Serialize every contract's JSON Schema IN MEMORY: filename -> file text.
    The one serialization path, and a pure one - no I/O, no mkdir - so a drift
    gate can compare committed bytes against it without touching the disk."""
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
