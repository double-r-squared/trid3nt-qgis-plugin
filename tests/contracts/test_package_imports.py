"""Imports run one way: trid3nt imports neither other package, trid3nt_harness imports trid3nt only.

Every import statement in the three packages is read off the source, a relative one resolved against
its own package, and an edge that reaches a higher package fails unless ALLOWED names it. ALLOWED is
the set of known upward edges (importing file -> imported module); an entry that no longer matches an
edge fails too, so the list only ever shrinks.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAYERS = {"trid3nt": 0, "trid3nt_harness": 1, "trid3nt_server": 2}

ALLOWED: frozenset[tuple[str, str]] = frozenset({
    ("trid3nt/inputs/point.py", "trid3nt_server.render.layer_uri_emit"),
    ("trid3nt/inputs/run.py", "trid3nt_harness.inputs.gate.input_review"),
    ("trid3nt/inputs/slots.py", "trid3nt_harness.inputs.gate.draw_input"),
    ("trid3nt/inputs/slots.py", "trid3nt_harness.inputs.gate.input_review"),
    ("trid3nt/inputs/slots.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/inputs/user_layer.py", "trid3nt_server.render.layer_uri_emit"),
    ("trid3nt/inputs/user_layer.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/inputs/user_layer.py", "trid3nt_server.server.protocol.connections"),
    ("trid3nt/inputs/user_layer.py", "trid3nt_server.server.session.persistence_ref"),
    ("trid3nt/inputs/user_layer.py", "trid3nt_server.server.session.state"),
    ("trid3nt/inputs/user_layer.py", "trid3nt_server.server.turn.wire"),
    ("trid3nt/render/formats.py", "trid3nt_server.render.layer_uri_emit"),
    ("trid3nt/render/formats.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/render/restyle.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/settings/reader.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/tools/__init__.py", "trid3nt_harness.inputs.gate.spatial_input_tool"),
    ("trid3nt/tools/__init__.py", "trid3nt_harness.tools.search.fetch.fetch"),
    ("trid3nt/tools/__init__.py", "trid3nt_harness.tools.search.find_sources.find_sources"),
    ("trid3nt/tools/__init__.py", "trid3nt_harness.tools.search.search_tools.search_tools"),
    ("trid3nt/tools/derive/probe_point/probe_point.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/tools/derive/probe_point/probe_point.py", "trid3nt_server.server.session.persistence_ref"),
    ("trid3nt/tools/derive/run_pyqgis/run_pyqgis.py", "trid3nt_server.server.processing"),
    ("trid3nt/tools/derive/run_qgis_algorithm/run_qgis_algorithm.py", "trid3nt_server.server.processing"),
    ("trid3nt/tools/fetchers/_router/emit_on_fetch.py", "trid3nt_server.render.layer_uri_emit"),
    ("trid3nt/tools/fetchers/_router/emit_on_fetch.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/tools/fetchers/_router/executors/qgis_provider.py", "trid3nt_harness.inputs.gate.pending"),
    ("trid3nt/tools/fetchers/_router/executors/qgis_provider.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/tools/fetchers/_router/executors/qgis_provider.py", "trid3nt_server.server.processing"),
    ("trid3nt/tools/fetchers/socioeconomic/geocode_location/geocode_location.py", "trid3nt_harness.inputs.gate.input_review"),
    ("trid3nt/tools/mesh/gate.py", "trid3nt_harness.inputs.gate.input_review"),
    ("trid3nt/tools/mesh/gate.py", "trid3nt_server.render.layer_uri_emit"),
    ("trid3nt/tools/mesh/gate.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/tools/mesh/step.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/tools/mesh/tool.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/tools/meta/set_config/set_config.py", "trid3nt_harness.inputs.gate.errors"),
    ("trid3nt/tools/meta/set_config/set_config.py", "trid3nt_harness.inputs.gate.input_review"),
    ("trid3nt/tools/meta/set_config/set_config.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/workflows/geoclaw/workflow.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/workflows/modflow/workflow.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/workflows/pestpp/workflow.py", "trid3nt_harness.inputs.gate.input_review"),
    ("trid3nt/workflows/pestpp/workflow.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/workflows/runtime/fill.py", "trid3nt_harness.inputs.gate.input_review"),
    ("trid3nt/workflows/runtime/fill.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/workflows/runtime/review.py", "trid3nt_harness.inputs.gate.input_review"),
    ("trid3nt/workflows/runtime/workflow.py", "trid3nt_harness.inputs.gate.input_review"),
    ("trid3nt/workflows/runtime/workflow.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/workflows/solver/solve_progress.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/workflows/solver/solver.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/workflows/telemac/authoring/release_point.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt/workflows/telemac/workflow.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt_harness/inputs/gate/confirm.py", "trid3nt_server.server.session.state"),
    ("trid3nt_harness/inputs/gate/confirm.py", "trid3nt_server.server.turn.wire"),
    ("trid3nt_harness/inputs/gate/draw_input.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt_harness/inputs/gate/input_review.py", "trid3nt_server.render.pipeline_emitter"),
    ("trid3nt_harness/tools/search/tool_retrieval.py", "trid3nt_server.main"),
})


def _is_module(dotted: str) -> bool:
    path = REPO.joinpath(*dotted.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def _imported(path: Path) -> set[str]:
    rel = path.relative_to(REPO).with_suffix("")
    package = list(rel.parts[:-1])
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join(base + (node.module.split(".") if node.module else []))
            for alias in node.names:
                sub = f"{module}.{alias.name}"
                found.add(sub if _is_module(sub) else module)
    return found


def upward_edges() -> set[tuple[str, str]]:
    edges = set()
    for top, layer in LAYERS.items():
        for path in sorted((REPO / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for module in _imported(path):
                if LAYERS.get(module.split(".")[0], -1) > layer:
                    edges.add((path.relative_to(REPO).as_posix(), module))
    return edges


def test_no_import_reaches_a_higher_package_unless_allowed() -> None:
    unlisted = sorted(upward_edges() - ALLOWED)
    assert not unlisted, f"upward imports not in ALLOWED: {unlisted}"


def test_every_allowed_edge_is_still_an_import() -> None:
    stale = sorted(ALLOWED - upward_edges())
    assert not stale, f"ALLOWED entries no longer imported (delete them): {stale}"
