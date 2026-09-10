# Docstring exemptions

Every `# docstring-exempt: <reason>` marker in the tree, regenerated from the
markers themselves. A docstring past the limit (3 content lines for a function
or class, 5 for a module) carries one, and the reason names the contract the
limit cannot hold. Past roughly ten entries the limit is re-argued rather than
routed around.

| symbol | file:line | reason |
| --- | --- | --- |
| `_example_tool_template.py (module)` | trid3nt_server/tools/_example_tool_template.py:2 | copy-me authoring template; the docstring IS the demonstrated artifact |
| `example_bbox_area` | trid3nt_server/tools/_example_tool_template.py:82 | copy-me authoring template; the docstring IS the demonstrated artifact |
| `set_boundary_roles` | trid3nt_server/workflows/mesh/shared/primitives.py:74 | **roles accepts four face shapes and a run-not-node-set contract the signature cannot carry |
| `reseat_revised` | trid3nt_server/workflows/runtime/resolver.py:141 | the two user-authority doors seat identically (both stamp basis=user) and differ only in what they record, which no signature can say. |
| `rederive_revised` | trid3nt_server/workflows/runtime/resolver.py:172 | the three-element return is under-specified by its type, and the pin on a user-basis row is a rule no signature carries. |
| `snap_release_to_wetted` | trid3nt_server/workflows/telemac/helpers/release_point.py:118 | the engine solves a source at a mesh NODE, so which node and whether it is wet are contracts no argument or return type carries. |
| `plan_vertical_grid` | trid3nt_server/workflows/telemac/modules/telemac3d.py:111 | the refusal, the transform reproduced and the sizing of the returned thickness are three contracts no argument or return type carries. |
| `outlet_hydrograph` | trid3nt_server/workflows/telemac/products/run_reads.py:255 | the 1-based boundary numbering and the outflow-positive sign convention are stated here and nowhere else, and neither is in the signature. |
