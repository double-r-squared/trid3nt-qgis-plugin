# GRACE-2 OpenQuake Engine PSHA solver AWS Batch worker (sprint-17).
#
# Builds the runtime image for the OpenQuake probabilistic-seismic-hazard (PSHA)
# AWS Batch job — a NEW engine (the multi-hazard workbench's seismic driver,
# pairing with the existing Pelicun impact path). Unlike SFINCS (a thin layer
# over the upstream Deltares image) there is no GRACE-2-pinned OpenQuake base, so
# we build from python:slim and pip-install openquake-engine (AGPL, pip/Docker/
# CLI). The `oq` CLI is the headless entrypoint (`oq engine --run job.ini`); we
# do NOT modify the engine.
#
# RAM SIZING (load-bearing, OpenQuake-specific): the OpenQuake engine is
# RAM-hungry — roughly ~2 GB per worker thread for a classical PSHA. The Batch
# job-def (infra/aws-batch/openquake.tf) sizes MEMORY accordingly (8192 MiB
# baseline for the small demo grid; the agent overrides per-job via
# containerOverrides). OQ_NUM_CORES is pinned LOW in the runtime env so a small
# Batch box does not oversubscribe RAM (cores * ~2 GB must stay under the job
# memory). This is the single biggest difference from the SWMM/MODFLOW workers.
#
# MULTI-STAGE (container-hygiene norm): a `builder` stage installs the wheels
# into a venv at /opt/grace2/.venv; the final stage copies ONLY that venv + the
# entrypoint onto a fresh python:slim. openquake-engine pulls a large scientific
# stack (numpy/scipy/pandas/h5py/shapely/rtree) as manylinux wheels, so no
# compiler is needed at runtime; the multi-stage split keeps pip's wheel/build
# caches OUT of the shipped image.
#
# Version pins (mirror the SFINCS/MODFLOW/SWMM digest discipline — bumps are a
# deliberate, IaC-visible step): python:3.11-slim-bookworm, openquake-engine
# 3.19.x (a recent stable engine release). The pin is loose-bounded
# (>=3.19,<3.21) so a patch bump is allowed without an image rebuild churn while
# a major engine bump stays a deliberate edit.

# --------------------------------------------------------------------------- #
# Stage 1 — builder: resolve + install openquake-engine into a self-contained
# venv. Nothing from this stage except /opt/grace2/.venv reaches the final image.
# --------------------------------------------------------------------------- #
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS builder

ENV VENV=/opt/grace2/.venv \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential + libgeos/libspatialindex headers are a belt-and-suspenders
# fallback ONLY for the rare case a pinned dep lacks an x86_64 manylinux wheel
# (openquake-engine + its scientific stack currently ship wheels, so this layer
# is usually a no-op the multi-stage split then DISCARDS — it never reaches the
# runtime image).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        libgeos-dev \
        libspatialindex-dev \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --no-cache-dir --upgrade pip \
    && "$VENV/bin/pip" install --no-cache-dir \
        "numpy>=1.23,<2" \
        "pandas>=1.5,<2" \
        "openquake.engine>=3.19,<3.21" \
        "fiona>=1.9,<2" \
        "numba" \
        "networkx" \
        "boto3>=1.34,<2" \
        "rasterio>=1.3,<2" \
    && "$VENV/bin/python" -c "import openquake.baselib, boto3; from openquake.commonlib import logs; print('openquake deps import OK')"

# --------------------------------------------------------------------------- #
# Stage 2 — runtime: a fresh minimal python:slim with ONLY the resolved venv +
# the entrypoint. No compiler, no apt caches, no pip caches.
# --------------------------------------------------------------------------- #
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="grace-2-openquake-solver" \
      org.opencontainers.image.source="https://github.com/double-r-squared/GRACE-2" \
      org.opencontainers.image.description="GRACE-2 OpenQuake Engine PSHA solver runtime (AWS Batch). Reads a build_spec from s3://...-cache/, templates a classical-PSHA job.ini + logic trees, runs oq engine --run job.ini headless, writes hazard exports + completion manifest to s3://...-runs/<run_id>/." \
      org.opencontainers.image.licenses="AGPL-3.0"

# ca-certificates so boto3's HTTPS calls to S3 verify; libgeos-c1v5 +
# libspatialindex (shapely/rtree dlopen these at import) — the slim base ships
# none. Caught at build time by the venv import smoke if missing.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        libgeos-c1v5 \
        libspatialindex6 \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# Copy ONLY the resolved venv from the builder — the hygiene payoff.
ENV VENV=/opt/grace2/.venv
COPY --from=builder /opt/grace2/.venv /opt/grace2/.venv
ENV PATH="$VENV/bin:$PATH"

# COPY the entrypoint into a stable runtime path. Build context is the repo root
# (mirrors the SWMM/SFINCS/MODFLOW context); paths below are relative to repo root.
RUN mkdir -p /opt/grace2/services/workers/openquake
COPY services/workers/openquake/ /opt/grace2/services/workers/openquake/

# WORKDIR controls where the engine writes its scratch deck + exports before the
# entrypoint uploads them. /opt/grace2/work is a writable scratch dir the
# entrypoint clears + re-creates per invocation.
WORKDIR /opt/grace2

ENV PYTHONPATH=/opt/grace2 \
    PYTHONUNBUFFERED=1 \
    GRACE2_OQ_SCRATCH=/opt/grace2/work \
    GRACE2_OQ_BIN=oq \
    # OpenQuake runs out of an HDF5 datastore under $OQ_DATADIR; point it inside
    # the writable scratch tree so the read-only image layers stay clean.
    OQ_DATADIR=/opt/grace2/work/oqdata \
    # RAM guard: cap the engine's worker cores so cores*~2GB stays under the
    # Batch job memory. The agent may raise this per-job via containerOverrides
    # when it sizes a larger box.
    OQ_NUM_CORES=2 \
    OQ_DISTRIBUTE=no

# Runtime config — overridable from the AWS Batch container env block.
#   GRACE2_OBJECT_STORE  — s3 (AWS Batch) routes all object I/O through boto3.
#   GRACE2_RUNS_BUCKET   — destination bucket (this image is the sanctioned
#                          writer of s3://...-runs/<run_id>/).
#   GRACE2_RUN_ID        — the run ID under which outputs land; set per job by
#                          the agent's submit_job containerOverrides.
#   GRACE2_MANIFEST_URI  — s3:// URI of the JSON build_spec the entrypoint reads.
#   AWS_REGION           — region for the boto3 S3 client.
ENV GRACE2_OBJECT_STORE=s3 \
    GRACE2_RUNS_BUCKET="" \
    GRACE2_RUN_ID="" \
    GRACE2_MANIFEST_URI="" \
    AWS_REGION=us-west-2

# Build-time smoke (mirror of the SWMM/SFINCS/MODFLOW Dockerfile assert): the oq
# CLI runs + the entrypoint + deck-templating modules import cleanly. Catches a
# path shift or import mistake at build time instead of at Batch exec time.
RUN oq --version \
    && python3 -c "from services.workers.openquake.entrypoint import main; print('entrypoint import OK')" \
    && python3 -c "from services.workers.openquake.job_ini import render_openquake_deck; print('job_ini import OK')"

# ENTRYPOINT runs this image as a one-shot solver job. CMD is empty so AWS Batch
# containerOverrides command feeds straight through as argv to the module;
# env-var fallbacks (GRACE2_RUN_ID, GRACE2_MANIFEST_URI) are parsed by the
# entrypoint if no CLI flags are passed (mirror of the SWMM/SFINCS pattern).
ENTRYPOINT ["python3", "-m", "services.workers.openquake.entrypoint"]
CMD []
