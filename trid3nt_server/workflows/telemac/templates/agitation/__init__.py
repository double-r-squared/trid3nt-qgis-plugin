"""ARTEMIS harbour wave-agitation engine template.

``agitation`` is the recipe, ``declarations`` its contract, and ``barrier`` the
one transform only this question needs - a mapped structure centreline given the
width the mesher can remove water with.
"""
from trid3nt_server.workflows.telemac.templates.agitation.agitation import (  # noqa: F401
    artemis_harbor_agitation,
)
