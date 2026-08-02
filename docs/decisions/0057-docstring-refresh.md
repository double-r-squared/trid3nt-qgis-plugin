# 0057 - mass docstring refresh + the verbatim-carry supersession

Context: NATE's directive (2026-07-31) -- "a lot [of docstrings] are stale and
unedited." Agent-facing docstrings are LOAD-BEARING ROUTING TEXT: the model reads
them to select and use tools, and Bedrock truncates at ~1000 chars, so the
what-it-IS / what-it-is-NOT block must lead. Across the 190 registered tools the
docstrings had accreted four decay classes: dead-tool cross-refs (culled siblings
still recommended in prose), dead-era infra prose (TiTiler / QGIS-Server-WMS / GCS
/ AWS-Batch / DynamoDB / Vertex describing a cloud stack the local build no longer
is), stale facts (behavior the code no longer has), and sprint/job/wave narration.
The dominant single offender was ``compute_zonal_statistics`` (demoted to the
code_exec playground) named as a downstream in ~50 docstrings.

Decision (2026-07-31):

1. **The fold-parity verbatim-carry rule is SUPERSEDED for docstrings.** Earlier
   fold/migration gates required a tool's docstring to carry VERBATIM across a
   landing (byte-identity was the parity proof). That rule assumed the docstring
   was already correct. Once docstrings drift, verbatim-carry FREEZES the drift.
   For docstring prose the gate now shifts from byte-identity to RETRIEVAL
   VERIFICATION: an edit is legitimate iff every corpus query's expected tool still
   ranks top-8 (model-free ``retrieve_ranked_tools`` over the composed corpus,
   compared against a before-edit snapshot). No routing regression ships; a
   docstring edit that demotes its own tool below top-8 is reverted or refixed.

2. **AS-WE-GO standing rule.** Any future wave that TOUCHES a tool (new param, new
   behavior, a sibling cull) re-judges that tool's docstring in the same landing --
   the four decay classes above are now a landing checklist item, not a periodic
   sweep. A cull that removes a tool MUST sweep the surviving docstrings for
   cross-refs to the removed name in the same change.

3. **Names are frozen; prose is not.** Docstring TEXT is editable by judgment;
   tool NAMES and param names never change under a docstring refresh. The registry
   stays at 190 with zero name changes. Spec-served tools (27) edit the
   ``docstring:`` field in source.yaml (fn.__doc__ derives from it, so the
   router-promotion identity ``fn.__doc__ == spec.docstring`` holds automatically).

4. **NOT a style pass.** Healthy docstrings are left byte-for-byte. This is
   staleness surgery, not prose taste -- the regex-census lesson stands: every
   docstring is READ FULLY and edited by judgment, never swept by pattern.

Consequence: routing text now describes the live local stack (QGIS plugin client +
local FastAPI daemon + MinIO s3://trid3nt-cache + local Docker solvers + pluggable
LLM) and points only at live tools. Content-pinning tests that asserted now-edited
phrases are updated as EXPECTED churn (listed in the wave report), not regressions.
Code-vs-doc conflicts that could not be resolved confidently were flagged, not
guessed. Supersedes the verbatim-carry clause for docstring prose only; the parity
rule still governs non-docstring migration artifacts.
