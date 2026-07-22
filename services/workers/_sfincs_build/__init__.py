"""Worker-side SFINCS regular-grid deck build (heavy-compute offload).

Vendored pure build logic + the ``build_sfincs_deck`` orchestrator the
``grace2-sfincs`` worker calls when handed an agent-composed build job_spec.
See ``deck.py`` for the sync-with-agent note.
"""

from .deck import (  # noqa: F401
    BuildOptions,
    ForcingSpec,
    SFINCSSetupError,
    build_options_from_dict,
    build_sfincs_deck,
    forcing_spec_from_dict,
)
from .spec import validate_job_spec  # noqa: F401
