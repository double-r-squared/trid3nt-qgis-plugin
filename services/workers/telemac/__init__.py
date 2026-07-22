"""TELEMAC-2D river-dye local worker (PHASE 2).

Packages the proven P1 pipeline (``telemac_river_dye_build``) as a
manifest-driven docker worker that plugs into the agent's local-docker solve
seam exactly like the SFINCS/GeoClaw workers: a manifest.json (mounted at
``/data/manifest.json``) carries the reach config, the entrypoint runs the
pipeline, and the mesh/result ``.slf`` + a ``telemac_metrics.json`` land in the
mounted rundir for the agent-side supervisor to upload + summarize into the
run's ``completion.json``.
"""
