# 0035 - local solver execution I/O contract

Context: the tested local-docker solver envelope bind-mounts the run directory at
`/data` and the AGENT-side supervisor reads/uploads the mounted outputs. One
engine spec (GeoClaw) instead had the container do its own object-store I/O over
`--network host`; on the local seam that left the supervisor's `output_uris`
empty because it globbed an unmounted run directory. Separately, layer artifacts
written into the deck staging dir vanished on cleanup, so a re-emit on reconnect
hit "No such file or directory" and the layer disappeared.

Decision: local solver runs use the volume-mount I/O contract and write durable
artifacts to the runs bucket.
- The container writes to the bind-mounted rundir (`/data`); the agent-side
  supervisor is the only component that talks to object storage, so the worker
  image needs no storage SDK. New engines follow this pattern, not container-side
  self-I/O.
- Layer artifacts a re-emit may re-read (e.g. `mesh.geojson`) are uploaded to the
  durable runs bucket (`s3://<runs_bucket>/<run_id>/...`), NOT the ephemeral deck
  staging dir the composer deletes on cleanup.
- Autoscaler budget models are calibrated to a REAL live run anchor, not a
  synthetic spike (the SWMM adaptive-mesh budget was re-fit after a real urban
  DEM ran ~16x slower than the synthetic fit predicted), with a near-linear
  exponent kept as a safety margin against super-linear cost growth.

Consequence: local solves produce durable, re-emittable outputs with no
container-side storage credentials, and the autoscaler does not under-coarsen off
an optimistic synthetic anchor. Related: 0009 (simulations own their inputs).

## Lifted here 2026-09-09, from the record that measured it

ADR 0295's SWMM half is gone; its honesty half is this contract's and lives
here: **the supervisor must not eat the metrics pointer.** A worker that does
its own object I/O writes its own `completion.json`, carrying the manifest
pointer, the output uris and its engine `extra`. The supervisor then wrote
`completion.json` unconditionally on container exit and overwrote it, so the
pointer died with the worker's copy - and the reader, which resolves
`completion.json.publish_manifest_uri` and never globs by design, answered
`None`. A consumer that then presented `.get("max_depth_m", 0.0)` narrated four
defaults as answers over a peak COG holding 19.99 m of water. Two rules follow:
a supervisor never overwrites a completion record it did not author, and a
metrics carrier that reads absent is a refusal, never a zero.
