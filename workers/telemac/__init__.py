"""The TELEMAC worker: the engine room of the local-docker solve seam.

A manifest at ``/data/manifest.json`` names one run with every input staged
beside it, and results land in the mounted run directory the supervisor uploads
from. The container fetches nothing and authors nothing.
"""
