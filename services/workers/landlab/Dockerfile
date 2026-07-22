# Landlab solver AWS Batch worker (sprint-17 — NEW engine / FR-CE-1).
#
# Builds the runtime image for the Landlab AWS Batch job — the scale-to-zero
# CLOUD LANE for the Landlab surface-process engine (landslide susceptibility /
# overland flow). Landlab is a pure-Python, pip-native CSDMS library (MIT) with
# no external solver binary: pip-install landlab and the component chain runs
# in-process. The entrypoint shim performs the S3-IN -> build-grid-from-DEM-COG
# -> run-chain -> COG-OUT envelope every solver invocation needs.
#
# Like SWMM, Landlab is light pip (no upstream container), so we build from
# python:slim and pip-install landlab + rasterio + boto3. We still build a Batch
# worker (NOT only an in-process lane) to honor the scale-to-zero island norm:
# heavy/sandboxed compute runs on Batch (Spot, scale-to-zero), the agent box
# stays tiny.
#
# MULTI-STAGE (container-hygiene norm): a `builder` stage compiles the wheels
# into a venv at /opt/grace2/.venv; the final stage copies ONLY that venv + the
# worker source onto a fresh python:slim, keeping the build toolchain + pip
# caches OUT of the shipped image. landlab + rasterio ship manylinux wheels for
# x86_64, so no compiler is normally needed; the multi-stage split is the hygiene
# contract (and a guard if a future dep lacks a wheel).
#
# Version pins (mirror the SWMM/MODFLOW digest discipline — bumps are a
# deliberate, IaC-visible step): python:3.12-slim-bookworm, landlab 2.x,
# rasterio 1.3.x. Landlab carries numpy/scipy transitively.

# --------------------------------------------------------------------------- #
# Stage 1 — builder: resolve + install the Python deps into a self-contained
# venv. Nothing from this stage except /opt/grace2/.venv reaches the final
# image, so pip caches + any transient build toolchain are discarded.
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS builder

ENV VENV=/opt/grace2/.venv \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential + a Cython/numpy toolchain are a belt-and-suspenders fallback
# ONLY for the rare case a pinned dep lacks an x86_64 manylinux wheel (landlab
# carries compiled Cython extensions but ships wheels; the multi-stage split
# DISCARDS this layer from the runtime image regardless).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --no-cache-dir --upgrade pip \
    && "$VENV/bin/pip" install --no-cache-dir \
        "landlab>=2.6,<3" \
        "rasterio>=1.3,<2" \
        "boto3>=1.34,<2" \
    && "$VENV/bin/python" -c "import landlab, rasterio, boto3; print('landlab deps import OK — landlab', landlab.__version__)"

# --------------------------------------------------------------------------- #
# Stage 2 — runtime: a fresh minimal python:slim with ONLY the resolved venv +
# the worker source. No compiler, no apt caches, no pip caches.
# --------------------------------------------------------------------------- #
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="grace-2-landlab-solver" \
      org.opencontainers.image.source="https://github.com/double-r-squared/GRACE-2" \
      org.opencontainers.image.description="Landlab (CSDMS) solver runtime (AWS Batch). Reads a build_spec + DEM COG from s3://...-cache/, builds a RasterModelGrid + runs a documented component chain (LandslideProbability / OverlandFlow), writes a field COG + completion manifest to s3://...-runs/<run_id>/." \
      org.opencontainers.image.licenses="MIT"

# ca-certificates so boto3's HTTPS calls to S3 verify; rasterio's bundled GDAL
# carries its own PROJ/GDAL data in the wheel.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# Copy ONLY the resolved venv from the builder — the hygiene payoff.
ENV VENV=/opt/grace2/.venv
COPY --from=builder /opt/grace2/.venv /opt/grace2/.venv
ENV PATH="$VENV/bin:$PATH"

# COPY the worker source into a stable runtime path. Build context is the repo
# root (mirrors the SWMM/MODFLOW cloudbuild context); paths below are relative to
# repo root. component_chain.py is imported as
# services.workers.landlab.component_chain by the entrypoint.
RUN mkdir -p /opt/grace2/services/workers/landlab
COPY services/workers/landlab/ /opt/grace2/services/workers/landlab/

# WORKDIR controls where the entrypoint writes its scratch field COG before it
# uploads. /opt/grace2/work is a writable scratch dir; the entrypoint clears it
# on start and re-creates it per invocation.
WORKDIR /opt/grace2

ENV PYTHONPATH=/opt/grace2 \
    PYTHONUNBUFFERED=1 \
    GRACE2_LANDLAB_SCRATCH=/opt/grace2/work

# Runtime config — overridable from the AWS Batch container env block.
#   GRACE2_OBJECT_STORE  — s3 (AWS Batch) routes all object I/O through boto3.
#   GRACE2_RUNS_BUCKET   — destination bucket (this image is the sanctioned
#                          writer of s3://...-runs/<run_id>/).
#   GRACE2_RUN_ID        — the run ID under which outputs land; set per job by
#                          the agent's submit_job containerOverrides.
#   GRACE2_MANIFEST_URI  — s3:// URI of the JSON build_spec manifest.
#   AWS_REGION           — region for the boto3 S3 client.
ENV GRACE2_OBJECT_STORE=s3 \
    GRACE2_RUNS_BUCKET="" \
    GRACE2_RUN_ID="" \
    GRACE2_MANIFEST_URI="" \
    AWS_REGION=us-west-2

# Build-time smoke (mirror of the SWMM/MODFLOW Dockerfile assert): landlab +
# rasterio import + the entrypoint + component_chain modules import cleanly.
RUN python3 -c "import landlab, rasterio; print('landlab OK', landlab.__version__)" \
    && python3 -c "from services.workers.landlab.entrypoint import main; print('entrypoint import OK')" \
    && python3 -c "from services.workers.landlab.component_chain import run_component_chain; print('component_chain import OK')"

# ENTRYPOINT runs this image as a one-shot solver job. CMD is empty so AWS Batch
# containerOverrides command feeds straight through as argv to the module;
# env-var fallbacks (GRACE2_RUN_ID, GRACE2_MANIFEST_URI) are parsed by the
# entrypoint if no CLI flags are passed (mirror of the SWMM/MODFLOW pattern).
ENTRYPOINT ["python3", "-m", "services.workers.landlab.entrypoint"]
CMD []
