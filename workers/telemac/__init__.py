"""The TELEMAC worker: the engine room of the local-docker solve seam.

A manifest at ``/data/manifest.json`` names one run, every input it needs is
staged beside it, and the entrypoint solves it in the mounted run directory:
results, listing and ``telemac_metrics.json`` land where the supervisor uploads
from. The container fetches nothing and authors nothing.
"""
