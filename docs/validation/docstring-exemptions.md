# Docstring exemptions

Every `# docstring-exempt: <reason>` marker in the tree. A docstring past the
limit (3 physical lines for a function or class, 5 for a module) carries one, and
the reason has to name the contract the limit cannot hold. Past roughly ten
entries the limit is re-argued rather than routed around.

| symbol | file:line | reason |
| --- | --- | --- |
| `reseat_revised` | trid3nt_server/workflows/runtime/resolver.py:139 | The two user-authority doors seat identically - both stamp `basis=user` - and differ only in what they record, which no signature can say; the undeclared-name rule and the changed-names return are separate facts. |
| `rederive_revised` | trid3nt_server/workflows/runtime/resolver.py:170 | The three-element return is under-specified by `tuple[ResolvedParams, list[str], list[str]]`, and the pin on a `basis=user` row is a rule no signature carries. |
| `set_boundary_roles` | trid3nt_server/workflows/mesh/shared/primitives.py:72 | `**roles` accepts four face shapes - a transect, its two end coordinates, a point, a ring - and the run-not-node-set contract that decides what each one names; none of it is reachable from `**roles: Any`. |
| `_example_tool_template` (module) | trid3nt_server/tools/_example_tool_template.py:1 | The copy-me authoring template: the module docstring IS the demonstrated artifact, walking the metadata, signature and registration seams a new tool copies. |
| `example_bbox_area` | trid3nt_server/tools/_example_tool_template.py:81 | Same file: this docstring is the demonstration of the routing-block shape a real tool's LLM-facing docstring takes, so trimming it removes the thing being taught. |
