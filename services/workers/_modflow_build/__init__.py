"""Worker-side MODFLOW build job_spec contract (heavy-compute offload).

Mirror of ``services/workers/_sfincs_build`` for MODFLOW. Unlike SFINCS (whose
build logic was vendored here), the MODFLOW deck build ALREADY lives in the
worker (``services/workers/modflow/gwt_adapter.build_modflow_deck``), so this
package only owns the agent -> worker ``job_spec`` schema + its validation. The
worker entrypoint's ``--build-spec-uri`` mode calls ``validate_job_spec`` then
feeds ``spec['run_args']`` (via ``build_deck_kwargs_from_spec``) straight into
``build_modflow_deck``.

See ``spec.py`` for the schema.
"""

from .spec import (  # noqa: F401
    JOB_SPEC_FILENAME,
    JOB_SPEC_SCHEMA_VERSION,
    build_deck_kwargs_from_spec,
    validate_job_spec,
)
