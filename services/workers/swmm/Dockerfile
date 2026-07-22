# SWMM solver AWS Batch worker (sprint-16 / P7 / FR-CE-1).
#
# Builds the runtime image for the SWMM AWS Batch job — the CLOUD LANE for the
# urban PySWMM engine (the dev primary path runs pyswmm IN-PROCESS in the agent
# venv; this image is the scale-beyond-local path). Unlike SFINCS (a thin layer
# over the upstream Deltares image) there is NO public SWMM container: pyswmm
# bundles the SWMM5 C engine as a pip wheel, so we build from python:slim and
# pip-install pyswmm + swmm-api. The entrypoint shim performs the
# S3/GCS-IN -> pyswmm-RUN -> S3/GCS-OUT envelope every solver invocation needs;
# we do NOT modify the SWMM5 engine.
#
# MULTI-STAGE (container-hygiene norm): a `builder` stage compiles the wheels
# into a venv at /opt/grace2/.venv; the final stage copies ONLY that venv +
# the entrypoint onto a fresh python:slim. This keeps build toolchain
# (gcc/build-essential pulled in transitively by any wheel that needs a C
# build) and pip's wheel/build caches OUT of the shipped image — only the
# resolved site-packages travels. pyswmm + swmm-api ship manylinux wheels for
# x86_64 so no compiler is actually needed; the multi-stage split is kept as the
# hygiene contract (and a guard if a future dep has no wheel).
#
# Version pins (mirror the SFINCS/MODFLOW digest discipline — bumps are a
# deliberate, IaC-visible step): python:3.11-slim-bookworm, pyswmm 2.x,
# swmm-api 0.4.x. These match the agent venv (pyswmm 2.1.0 + swmm-api 0.4.73)
# so the cloud lane solves byte-identically to the in-process dev path.

# --------------------------------------------------------------------------- #
# Stage 1 — builder: resolve + install the Python deps into a self-contained
# venv. Nothing from this stage except /opt/grace2/.venv reaches the final
# image, so pip caches + any transient build toolchain are discarded.
# --------------------------------------------------------------------------- #
FROM python:3.11-slim-bookworm AS builder

ENV VENV=/opt/grace2/.venv \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential is a belt-and-suspenders fallback ONLY for the rare case a
# pinned dep lacks an x86_64 manylinux wheel (pyswmm/swmm-api currently ship
# wheels, so this is usually a no-op layer that the multi-stage split then
# DISCARDS — it never reaches the runtime image).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        build-essential \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --no-cache-dir --upgrade pip \
    && "$VENV/bin/pip" install --no-cache-dir \
        "pyswmm>=2.0,<3" \
        "swmm-api>=0.4,<0.5" \
        "boto3>=1.34,<2" \
        "numpy>=1.23,<2" \
        "rasterio>=1.3,<2" \
    && "$VENV/bin/python" -c "import pyswmm, swmm_api, boto3, numpy, rasterio; print('swmm deps import OK — pyswmm', pyswmm.__version__)"

# --------------------------------------------------------------------------- #
# Stage 2 — runtime: a fresh minimal python:slim with ONLY the resolved venv +
# the entrypoint. No compiler, no apt caches, no pip caches.
# --------------------------------------------------------------------------- #
FROM python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="trid3nt-swmm-solver" \
      org.opencontainers.image.source="https://github.com/double-r-squared/trid3nt-qgis-plugin" \
      org.opencontainers.image.description="SWMM (pyswmm) solver runtime (AWS Batch). Reads a quasi-2D SWMM deck from s3://...-cache/, runs pyswmm headless, writes outputs + completion manifest to s3://...-runs/<run_id>/." \
      org.opencontainers.image.licenses="MIT"

# ca-certificates so boto3's HTTPS calls to S3 verify (the slim base ships none).
# libexpat1 backs rasterio's GDAL XML parser (opened at import; absent -> crash).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# Copy ONLY the resolved venv from the builder — the hygiene payoff.
ENV VENV=/opt/grace2/.venv
COPY --from=builder /opt/grace2/.venv /opt/grace2/.venv
ENV PATH="$VENV/bin:$PATH"

# COPY the entrypoint into a stable runtime path. Build context is the repo root
# (mirrors the SFINCS/MODFLOW cloudbuild context); paths below are relative to
# repo root.
RUN mkdir -p /opt/grace2/services/workers/swmm
COPY services/workers/swmm/ /opt/grace2/services/workers/swmm/

# WORKDIR controls where pyswmm writes its scratch outputs before the entrypoint
# uploads them. /opt/grace2/work is a writable scratch dir; the entrypoint
# clears it on start and re-creates it per invocation.
WORKDIR /opt/grace2

ENV PYTHONPATH=/opt/grace2 \
    PYTHONUNBUFFERED=1 \
    TRID3NT_SWMM_SCRATCH=/opt/grace2/work

# Runtime config — overridable from the AWS Batch container env block.
#   TRID3NT_OBJECT_STORE  — s3 (AWS Batch) routes all object I/O through boto3.
#   TRID3NT_RUNS_BUCKET   — destination bucket (this image is the sanctioned
#                          writer of s3://...-runs/<run_id>/).
#   TRID3NT_RUN_ID        — the run ID under which outputs land; set per job by
#                          the agent's submit_job containerOverrides.
#   TRID3NT_MANIFEST_URI  — s3:// URI of the JSON setup manifest the entrypoint
#                          reads (lists input objects + swmm_args + outputs).
#   AWS_REGION           — region for the boto3 S3 client.
ENV TRID3NT_OBJECT_STORE=s3 \
    TRID3NT_RUNS_BUCKET="" \
    TRID3NT_RUN_ID="" \
    TRID3NT_MANIFEST_URI="" \
    AWS_REGION=us-west-2

# Build-time smoke (mirror of the SFINCS/MODFLOW Dockerfile assert): pyswmm
# imports + the entrypoint module imports cleanly. Catches a path shift or
# import mistake at build time instead of at Batch exec time.
RUN python3 -c "import pyswmm; print('pyswmm OK', pyswmm.__version__)" \
    && python3 -c "from services.workers.swmm.entrypoint import main; print('entrypoint import OK')"

# ENTRYPOINT runs this image as a one-shot solver job. CMD is empty so AWS Batch
# containerOverrides command feeds straight through as argv to the module;
# env-var fallbacks (TRID3NT_RUN_ID, TRID3NT_MANIFEST_URI) are parsed by the
# entrypoint if no CLI flags are passed (mirror of the SFINCS/MODFLOW pattern).
ENTRYPOINT ["python3", "-m", "services.workers.swmm.entrypoint"]
CMD []
