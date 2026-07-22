# Canopy-height ML-inference AWS Batch worker.
#
# FILE-ONLY SCAFFOLD (NOT built / pushed here). This authors the runtime image
# for the canopy-height tool's CLOUD LANE -- the scale-to-zero CPU AWS Batch
# worker that runs Meta's pretrained HighResCanopyHeight model (a DINOv2 ViT
# backbone + DPT decoder, Apache-2.0) over a sub-metre RGB COG and writes a
# single-band canopy-height-in-metres COG. Built+pushed off this Dockerfile via
# the SHARED grace2-worker-builder CodeBuild project (WORKER_DIR=canopy,
# ECR_REPO=grace2-canopy) in a SEPARATE gated infra step the orchestrator runs
# with NATE -- no docker build / ECR push happens here.
#
# CPU, NOT GPU (spike verdict): the quantized compressed_SSLhuge_aerial.pth is
# explicitly CPU-runnable, so this worker runs on the EXISTING grace2-solvers-spot
# CPU compute environment + the EXISTING solvers queue -- NO GPU compute
# environment, NO g4dn/g5 instance ladder, NO new IAM/quota for v1. We install the
# CPU-ONLY torch wheels (the dedicated download.pytorch.org/whl/cpu index) so the
# image never pulls the ~2 GB CUDA torch payload.
#
# HEAVIEST worker image we ship (container-hygiene norm, the spike's main risk):
#   - CPU torch + torchvision wheels                    ~600-800 MB (installed)
#   - geoai + rasterio + numpy + the scientific stack   ~300-500 MB
#   - Meta weights baked in (compressed_SSLhuge_aerial  ~749 MB + the aerial
#     quantile-normalization ckpt)                       ~750-800 MB
#   - python:3.11-slim base                             ~150 MB
#   ----------------------------------------------------------------------
#   ESTIMATE: ~2.5-3.0 GB compressed-on-push runtime image. Mitigations applied:
#   multi-stage (the pip/wheel/build caches never reach the runtime layer), the
#   CPU-only torch index (no CUDA), and baked weights (no 749 MB cold-start fetch
#   on every SPOT job + no run-time dependence on Meta's bucket staying public --
#   the spike's weights-permanence mitigation). Run the pre-push inspect (docker
#   history + size) per the container-hygiene norm before the first ECR push.
#
# WEIGHTS (gated build step -- orchestrator + NATE): the weights are pulled at
# BUILD time from Meta's public, no-sign-request bucket
# https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/models/saved_checkpoints/
# and baked into /opt/grace2/weights/canopy. VERIFY the exact filenames +
# (ideally) SHA256-pin them before the first build, the same digest discipline the
# SWAN source tarball uses. The model + weights are Apache-2.0 (redistributable in
# a container; no key, no registration).

# Base via the AWS ECR Public mirror of Docker Hub (avoids the Docker Hub
# anonymous-pull 429 rate limit in CodeBuild) -- mirrors the openquake/swan base.

# --------------------------------------------------------------------------- #
# Stage 1 -- builder: resolve + install the CPU torch + geoai stack into a
# self-contained venv. Nothing here except /opt/grace2/.venv reaches the runtime.
# --------------------------------------------------------------------------- #
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS builder

ENV VENV=/opt/grace2/.venv \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# build-essential + libgeos/libspatialindex headers are a belt-and-suspenders
# fallback ONLY for the rare case a pinned dep lacks an x86_64 manylinux wheel
# (torch/rasterio/geoai ship wheels, so this layer is usually a no-op the
# multi-stage split then DISCARDS).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libgeos-dev \
        libspatialindex-dev \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# CPU-ONLY torch first (the dedicated cpu wheel index -- NO CUDA payload), THEN
# the geospatial + geoai stack from PyPI. Splitting the index keeps torch on the
# cpu channel while everything else resolves normally.
RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --no-cache-dir --upgrade pip setuptools wheel \
    && "$VENV/bin/pip" install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch>=2.2,<3" \
        "torchvision>=0.17,<1" \
    && "$VENV/bin/pip" install --no-cache-dir \
        "numpy>=1.26,<3" \
        "rasterio>=1.3,<2" \
        "pillow>=10,<12" \
        "geoai-py>=0.6" \
        "boto3>=1.34,<2" \
    && "$VENV/bin/python" -c "import torch, torchvision, rasterio, numpy, boto3; print('canopy worker deps import OK (torch cpu=%s)' % (not torch.cuda.is_available()))"

# --------------------------------------------------------------------------- #
# Stage 2 -- weights: bake Meta's HighResCanopyHeight weights into the image at
# build time (no runtime download of 749 MB on the SPOT box). Apache-2.0; pulled
# from Meta's public no-sign-request S3 bucket over HTTPS.
#
# GATED-BUILD PLACEHOLDER: confirm the exact filenames + add SHA256 pins before
# the first build (the sha256sum -c gate is the intended interlock -- mirror the
# SWAN tarball discipline). compressed_SSLhuge_aerial.pth is the v1 CPU variant.
# --------------------------------------------------------------------------- #
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS weights

ENV DEBIAN_FRONTEND=noninteractive \
    CANOPY_WEIGHTS=/opt/grace2/weights/canopy \
    META_WEIGHTS_BASE=https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/models/saved_checkpoints

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p "$CANOPY_WEIGHTS" \
    && curl -fsSL -o "$CANOPY_WEIGHTS/compressed_SSLhuge_aerial.pth" \
        "$META_WEIGHTS_BASE/compressed_SSLhuge_aerial.pth" \
    && curl -fsSL -o "$CANOPY_WEIGHTS/aerial_normalization_quantiles_predictor.ckpt" \
        "$META_WEIGHTS_BASE/aerial_normalization_quantiles_predictor.ckpt" \
    && test -s "$CANOPY_WEIGHTS/compressed_SSLhuge_aerial.pth" \
    && test -s "$CANOPY_WEIGHTS/aerial_normalization_quantiles_predictor.ckpt"

# --------------------------------------------------------------------------- #
# Stage 3 -- runtime: a fresh minimal python:slim with ONLY the resolved venv +
# the baked weights + the entrypoint. No compiler, no apt/pip caches.
# --------------------------------------------------------------------------- #
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="trid3nt-canopy-solver" \
      org.opencontainers.image.source="https://github.com/double-r-squared/trid3nt-qgis-plugin" \
      org.opencontainers.image.description="Canopy-height ML-inference worker (AWS Batch, CPU). Reads a build_spec + sub-metre RGB COG from s3://...-cache/, runs Meta HighResCanopyHeight (geoai) on CPU, writes a canopy-height-in-metres COG + completion manifest to s3://...-runs/<run_id>/." \
      org.opencontainers.image.licenses="Apache-2.0"

# ca-certificates so boto3 HTTPS verifies; libgeos-c1v5 + libspatialindex6
# (rasterio/shapely dlopen these); libexpat1 backs rasterio's GDAL XML driver
# (the job-0240 lesson the MODFLOW/GeoClaw/SWAN images hit); libgomp1 is the
# OpenMP runtime torch links against.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        libgeos-c1v5 \
        libspatialindex6 \
        libexpat1 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy ONLY the resolved venv + the baked weights -- the hygiene payoff.
ENV VENV=/opt/grace2/.venv
COPY --from=builder /opt/grace2/.venv /opt/grace2/.venv
COPY --from=weights /opt/grace2/weights /opt/grace2/weights
ENV PATH="$VENV/bin:$PATH"

# COPY the worker package onto a stable runtime path. Build context is the repo
# root (mirrors the openquake/swan context); paths below are relative to repo root.
RUN mkdir -p /opt/grace2/services/workers/canopy
COPY services/workers/canopy/ /opt/grace2/services/workers/canopy/

# WORKDIR controls where the entrypoint writes its scratch (downloaded RGB +
# canopy COG) before upload. /opt/grace2/work is writable scratch the entrypoint
# clears + re-creates per invocation.
WORKDIR /opt/grace2

ENV PYTHONPATH=/opt/grace2 \
    PYTHONUNBUFFERED=1 \
    GRACE2_CANOPY_SCRATCH=/opt/grace2/work \
    GRACE2_CANOPY_MODEL_VARIANT=compressed_SSLhuge_aerial \
    GEOAI_CANOPY_CACHE=/opt/grace2/weights/canopy \
    # Cap torch CPU threads to the per-job vCPU the agent sets via
    # containerOverrides (OMP_NUM_THREADS); a sane default keeps a small box from
    # oversubscribing. The agent overrides this per-job.
    OMP_NUM_THREADS=8 \
    TORCH_NUM_THREADS=8

# Runtime config -- overridable from the AWS Batch container env block.
#   GRACE2_OBJECT_STORE  -- s3 (AWS Batch) routes all object I/O through boto3.
#   GRACE2_RUNS_BUCKET   -- destination bucket (the sanctioned writer of
#                           s3://...-runs/<run_id>/).
#   GRACE2_RUN_ID        -- the run ID under which outputs land; set per job by
#                           the agent's submit_job containerOverrides.
#   GRACE2_MANIFEST_URI  -- s3:// URI of the JSON build_spec the entrypoint reads.
#   AWS_REGION           -- region for the boto3 S3 client.
ENV GRACE2_OBJECT_STORE=s3 \
    GRACE2_RUNS_BUCKET="" \
    GRACE2_RUN_ID="" \
    GRACE2_MANIFEST_URI="" \
    AWS_REGION=us-west-2

# Build-time smoke: the entrypoint + the (inference-free) COG-write import
# cleanly, the deps import, AND the baked weights are present + non-empty.
# Catches a path/import/weights mistake at build time instead of at Batch exec.
RUN python3 -c "from services.workers.canopy.entrypoint import main, write_canopy_cog, canopy_cog_is_nonempty; print('canopy entrypoint import OK')" \
    && python3 -c "import torch, torchvision, rasterio; print('torch/rasterio import OK')" \
    && test -s /opt/grace2/weights/canopy/compressed_SSLhuge_aerial.pth \
    && test -s /opt/grace2/weights/canopy/aerial_normalization_quantiles_predictor.ckpt \
    && echo "canopy weights baked OK"

# ENTRYPOINT runs this image as a one-shot solver job. CMD is empty so AWS Batch
# containerOverrides command feeds straight through as argv; env-var fallbacks
# (GRACE2_RUN_ID, GRACE2_MANIFEST_URI) are parsed by the entrypoint if no CLI
# flags are passed (mirror of the openquake/swan pattern).
ENTRYPOINT ["python3", "-m", "services.workers.canopy.entrypoint"]
CMD []
