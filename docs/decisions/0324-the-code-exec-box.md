# 0324 - The code-exec box

The `code_exec` sandbox was 2,081 lines across three modules built for a
cloud that no longer exists: a Cloud Run Job that staged its payload to GCS,
printed a marker-prefixed envelope to stdout for a Cloud Logging readback, and
was contained by a VPC egress firewall. When the cloud went, the containment
went with it, and what remained was a stack of substitutes for the boundary the
VPC used to be - an in-process `socket.connect` monkeypatch, a proxy-env strip,
an allowlisted child environment, a `SIGALRM` watchdog, a process-group kill, a
`setrlimit` set, and a bubblewrap namespace jail underneath all of it.

That is a lot of code standing in for one thing this repo already has: a box.

## The decision

The sandbox is `docker run --rm --network none` against an image the tree
already builds, and nothing else. 341 lines in three files:

- `sandbox/box.py` - the host end. Stages every ref into a private run
  directory, builds the one launch line, reads back the result the driver
  wrote.
- `sandbox/driver.py` - the engine room inside the container. Opens the staged
  paths, runs the snippet, converts and bounds the result. Imports nothing from
  the server package.
- `sandbox/__init__.py` - the seam, `submit_sandbox_job`, unchanged in
  signature and return shape so `code_exec_request` did not move.

The image is `trid3nt-local/mesh:latest` - already on the box, already carrying
numpy, pandas, scipy, rasterio, geopandas, shapely and matplotlib, and the
lightest image in the tree that carries all of them. Nothing was built; the
alternative (a new playground image) would have added 1.3 GB to say the same
thing. `TRID3NT_SANDBOX_IMAGE` overrides it.

## What the container replaced, and why each substitute could go

| substitute | replaced by |
| --- | --- |
| in-process socket monkeypatch + proxy-env strip | `--network none`: no interfaces, so no egress by any route |
| allowlisted credential-scrubbed child env | a container env built from nothing, plus three explicit `-e` |
| bubblewrap jail (`--unshare-net/pid/ipc/uts`, ro-binds, tmpfs) | the container, which is all of that |
| `SIGALRM` watchdog + outer subprocess kill + `killpg` | one `subprocess.run(timeout=cap)` and a `docker kill` on the named container |
| `setrlimit` AS / CPU / FSIZE | `--memory`, `--cpus`, `--pids-limit` |
| stdout envelope marker + parse-then-bound + `MAX_ENVELOPE_BYTES` | the driver writes `result.json` into the mounted run dir; there is nothing to scrape |
| `SandboxExecutionHandle`, `read_sandbox_result`, two typed cloud errors | nothing - the run is synchronous and always was, after the cloud left |

The wallclock story is now one cap instead of three racing ones. The old
belt-and-suspenders existed because the in-process alarm was defeatable by user
code; killing the container is not.

## Data enters staged

The box has no network, so it cannot fetch - which is the point. Every ref is
materialized into the run directory by the host before the container starts,
and the payload the driver reads names local paths only. Fetch first on the
gate-visible path, analyze second.

One behaviour moved: an `s3://` or `gs://` ref used to be openable from inside
the executor through GDAL's `/vsi` drivers on the un-jailed dev path. It is not
any more. An `s3://` ref is staged by the host; anything else that is not a
local file is handed through as its string and named in `layer_errors`.

## Honesty

`status` still carries the four terminal outcomes the wire contract declares. A
snippet that reaches for the network comes back `blocked` - detected from the
errno the kernel actually returned, walking the exception chain so a rewrapped
`URLError` still reads as what it is - and the failure travels verbatim rather
than restated in our own words.

## The model

This seam opens the TOOL plane: `docs/model/tool-plane.sysml`, with
`SandboxIsNetworkNone`, `DataEntersStaged` and
`OffloadKeepsTheLoopUnblocked` as requirements, each verified by tests that run
real containers. The network-none law also carries a `forbid:` rule - the
driver may not import the server package - so the boundary is checked as a
dependency edge and not only as behaviour.

The plane's other systems (the registry, retrieval, the processing tools) are
unmodeled and the index says so.
