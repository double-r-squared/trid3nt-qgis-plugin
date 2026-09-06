"""TELEMAC-3D vertical-structure engine template.

``stratified_flow`` is the recipe and ``declarations`` its contract. The vertical
grid the column needs is the module wrapper's composite, because how a sigma grid
is planned is a fact about TELEMAC-3D and not about this question.
"""
from trid3nt_server.workflows.telemac.templates.stratified_flow.stratified_flow import (  # noqa: F401
    telemac3d_stratified_flow,
)
