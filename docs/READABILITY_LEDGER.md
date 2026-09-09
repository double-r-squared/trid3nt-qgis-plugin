# Readability ledger

Comments and docstrings that a better NAME or a small EXTRACTION would make
unnecessary. The docs and docstring wave RECORDS these and applies NONE of them:
renaming or extracting inside a function is a behavior surface, reviewed as code
by whichever wave owns the module.

| file | line | the comment as it stands | the change that would remove it | risk |
| --- | --- | --- | --- | --- |
| trid3nt_server/workflows/runtime/data.py | 30 | `_CoversAOI`: "a domain must be BOUND and have an extent ... the artifact's own extent is never read" | rename the sentinel to what it actually tests (`DomainIsBound`), so the class name stops promising a coverage check the docstring has to walk back | rename |
| trid3nt_server/workflows/runtime/data.py | 186 | `#: How a supplied artifact is checked against the domain - BOUND-DOMAIN-ONLY under ``CoversAOI``` | same rename: the field comment exists only to correct the sentinel's name | rename |
| trid3nt_server/workflows/runtime/data.py | 237 | `refuse_wrong_shape`: "Suffix-deep and no deeper; an unclassifiable artifact passes." | the depth is `artifact_class`'s contract, not this function's; a name saying suffix (`class_from_suffix`) would carry it at the call site | rename |
| trid3nt_server/workflows/runtime/journal.py | 139 | `_row`: "One resolved param, WITH its door and basis - the sheet, not just the values." | rename to `_sheet_row`, so the returned shape is named rather than described | rename |
| trid3nt_server/workflows/runtime/ledger.py | 58 | `_stable`: the digest's contribution rules live in `inputs_digest`'s docstring because `_stable` is unnamed for what it does | rename to `_digest_contribution` and the rules read off the name | rename |
| trid3nt_server/workflows/runtime/rerun/reuse.py | 62 | `_node_key`: "A branch marker is not Ref-able and every one shares a placeholder step, so branches are keyed by INDEX" | extract the branch case into `_branch_key(index)`, and the two key spaces stop needing prose to keep apart | extract |
| trid3nt_server/workflows/runtime/interpreter.py | 465 | `_refuse_invented_physics`: the three-clause comment enumerating which mode/emitter/review combinations refuse | extract the predicate as `_has_review_surface(...)`, and the enumeration becomes the function's own body | extract |
| trid3nt_server/workflows/runtime/params.py | 290 | `wire_value`: the six-significant-figures comment block at the float branch | extract `_six_significant_figures(value)`; the constraint moves onto the name and the rounding rule stops being a comment in a dispatch chain | extract |
| trid3nt_server/workflows/shared/cog_io.py | 391 | `gs_fallback_to_file` and `gs_backend`: the comment block explaining that `gs_backend` selects nothing and the branch is only reachable when a caller forces a scheme | the parameter names outlived their backend; renaming to `fallback_to_file` and dropping the inert `gs_backend` removes the comment entirely | restructure |
| trid3nt_server/workflows/solver/solver.py | 285 | `_download_object`: "Dispatch on the URI SCHEME, never on the manifest field name: the input entries are keyed ``gs_uri``" | rename the manifest field to `uri` in the worker contract; the comment exists purely to defuse the stale key name | restructure |
| trid3nt_server/workflows/solver/solver.py | 668 | `input_uri = item["gs_uri"]  # the field NAME only; the value is a uri` | same contract rename | restructure |
| trid3nt_server/workflows/solver/solver.py | 62 | `NFR_P_4_TARGET_SECONDS`: its comment has to say what the constant is a target FOR, the name carrying only a spec label | rename to `PROGRESS_RAMP_TARGET_SECONDS` and the comment collapses to the ramp formula | rename |
| trid3nt_server/workflows/solver/solver.py | 871 | `_try_get_completion_s3`: "``None`` when the object is absent or transiently unreadable ... malformed JSON RAISES" | extract the absent-vs-corrupt discrimination into `_completion_or_none`, so the two outcomes are two named paths | extract |
| trid3nt_server/workflows/runtime/workflow.py | 464 | `_wire_signature`: the comment block explaining that CONSTANT-door params are absent from the signature but still seatable through `**wire` | extract `_seatable_but_unoffered(params)`; the carve-out is currently only discoverable as prose | extract |
| trid3nt_server/workflows/shared/aoi.py | 28 | `location_or_bbox`: the three-behaviour comment block above `_coerce` | extract each branch (`_bbox_that_is_a_place_name`, `_refuse_no_aoi`, `_drop_bbox_for_location`), and the block becomes three names | extract |
