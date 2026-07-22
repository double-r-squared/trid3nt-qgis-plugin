"""OpenQuake Engine PSHA AWS Batch worker package (sprint-17).

A thin shim around the OpenQuake Engine CLI (``oq engine --run job.ini``). The
worker contract is solver-agnostic (mirrors the SWMM/SFINCS/MODFLOW workers): it
reads an S3 ``build_spec``, templates a classical-PSHA ``job.ini`` + source-model
/ GMPE logic-tree XML for an AOI site grid, runs the OpenQuake engine headless,
exports the hazard curves / hazard map, and writes outputs + ``completion.json``
to ``s3://<runs_bucket>/<run_id>/`` in the SAME completion schema the agent's
``wait_for_completion`` polls.
"""
