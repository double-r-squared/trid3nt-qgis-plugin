# MODFLOW 6 solver worker (sprint-13 / MOD-1 / FR-CE-1 / job-0220).
#
# Builds the runtime image for the MODFLOW 6 Cloud Run Job. Unlike the SFINCS
# worker (a thin layer over the upstream Deltares image), MODFLOW 6 has no
# maintained official Docker image matching the USGS release train, so we
# build from python:3.11-slim and install the version-pinned USGS binary
# explicitly with SHA-256 verification (design doc § 1). The entrypoint shim
# performs the GCS-IN -> mf6-RUN -> GCS-OUT envelope every solver invocation
# needs; we do NOT modify the mf6 binary.
#
# Base-image choice (design doc § 1 + § "Decisions Made"):
#   python:3.11-slim (Debian Bookworm slim). Python is a first-class runtime
#   requirement here — the agent-side postprocess uses FloPy/rasterio and the
#   container's build-verification needs Python — whereas SFINCS needed no
#   Python at run time so it kept the upstream Ubuntu base. Bookworm's pip 23+
#   enforces PEP 668, so we install Python deps into a venv at
#   /opt/grace2/.venv (matches the infra/worker/Dockerfile venv discipline)
#   rather than using --break-system-packages.
#
# MODFLOW binary version pin (design doc § 1, § 9 OQ-MOD-1):
#   MODFLOW 6.5.0 — the combined GWF + GWT binary `mf6` from the USGS GitHub
#   release zip. The design doc cited the asset name `mf6.0_linux.zip`; the
#   ACTUAL release asset is `mf6.5.0_linux.zip` and the canonical
#   MODFLOW-USGS/modflow6 repo path 301-redirects to MODFLOW-ORG/modflow6.
#   Both deviations are documented in report.md § Deviations. We pin the SHA-256
#   of the zip (verified on the host smoke run, 2026-06-09) and verify it
#   before extraction; -fL follows the canonical->ORG redirect.
#
# Image digest discipline (mirror of services/workers/sfincs/Dockerfile +
# infra/sfincs.tf): after `make modflow-build` Cloud Build logs the resolved
# AR image digest; record it in infra/modflow.tf's `modflow_image_digest`
# local so `tofu plan` stays an honest source of truth for what's deployed.
#
# No Artifact Registry pull-through cache at v0.1 — same reasoning as the
# SFINCS Dockerfile: build frequency is low (~1/week max); if Docker Hub /
# GitHub rate limits bite, a one-line FROM/URL change points to an AR mirror.

FROM python:3.11-slim

LABEL org.opencontainers.image.title="grace-2-modflow-solver" \
      org.opencontainers.image.source="https://github.com/double-r-squared/GRACE-2" \
      org.opencontainers.image.description="MODFLOW 6 (mf6 6.5.0) solver runtime (Cloud Run Job). Reads a FloPy-generated GWF+GWT deck from gs://grace-2-hazard-prod-cache/, runs mf6, writes heads/concentration outputs + completion manifest to gs://grace-2-hazard-prod-runs/<run_id>/." \
      org.opencontainers.image.licenses="MIT"

# MODFLOW 6.5.0 Linux release pin (design doc § 1). The zip ships statically
# linked binaries (mf6, libmf6.so, mf5to6, zbud6) — no Fortran runtime apt
# package is required at run time. SHA-256 verified on the host smoke run
# (reports/inflight/job-0220-infra-20260609/evidence/mf6_smoke.log).
ARG MF6_VERSION=6.5.0
ARG MF6_ZIP_URL=https://github.com/MODFLOW-USGS/modflow6/releases/download/6.5.0/mf6.5.0_linux.zip
ARG MF6_ZIP_SHA256=0fac00211c42b7a74c7266abbe50776a6215ea8409c8ce887e5decd4a9335940
# USGS naming is inconsistent: the 6.5.0 release zip + its top-level dir are
# both `mf6.5.0_linux` (the leading "6." is dropped), even though the binary's
# `mf6 --version` reports "6.5.0". So MF6_ZIP_DIR is pinned separately from
# MF6_VERSION rather than derived — `mf6.${MF6_VERSION}_linux` would wrongly
# expand to `mf6.6.5.0_linux`, which is NOT a path in the zip.
ARG MF6_ZIP_DIR=mf6.5.0_linux

# --- System deps (single layer) ------------------------------------------
# ca-certificates (HTTPS), curl (binary download), unzip (mf6 zip extract).
# libexpat1: rasterio's manylinux wheel dlopen()s libexpat.so.1 at import
# (GDAL's XML/GML driver chain) but does NOT bundle it; python:3.11-slim ships
# no expat, so `import rasterio` fails with
# `ImportError: libexpat.so.1: cannot open shared object file` unless we add it
# (caught at build time by the venv import smoke — job-0240 first build).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        unzip \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# --- Python venv + deps (single layer, design doc § 3) -------------------
# venv at /opt/grace2/.venv to sidestep PEP 668 on Bookworm. flopy is needed
# by the agent-side deck builder + postprocess (and for in-container
# verification); the entrypoint object-store envelope is SCHEME-AWARE
# (google-cloud-storage drives gs://; boto3 drives s3:// on AWS Batch — the
# job-0289 boto3-not-s3fs lesson, mirroring the SFINCS worker image);
# numpy/rasterio back the postprocess COG conversion.
ENV VENV=/opt/grace2/.venv
RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --no-cache-dir --upgrade pip \
    && "$VENV/bin/pip" install --no-cache-dir \
        "flopy>=3.7,<4" \
        "google-cloud-storage>=2.18,<4" \
        "boto3>=1.34,<2" \
        "numpy>=1.26,<3" \
        "rasterio>=1.3,<2" \
        "pyproj>=3.6,<4" \
    && "$VENV/bin/python" -c "import flopy, numpy, rasterio, boto3, pyproj; from google.cloud import storage; print('python deps import OK — flopy', flopy.__version__)"

# venv on PATH so `python3`/`pip` resolve to the venv for all later layers
# and at run time.
ENV PATH="$VENV/bin:$PATH"

# --- MODFLOW binary download + verify + install (single layer) -----------
# Pinned-SHA verify BEFORE extraction (design doc § 3.4). -fL follows the
# MODFLOW-USGS -> MODFLOW-ORG canonical redirect. We install only the `mf6`
# binary + its shared lib to /usr/local/bin (mf5to6/zbud6 are not used by the
# GWF+GWT demo path).
RUN curl -fL "$MF6_ZIP_URL" -o /tmp/mf6.zip \
    && echo "${MF6_ZIP_SHA256}  /tmp/mf6.zip" | sha256sum -c - \
    && unzip -o /tmp/mf6.zip "${MF6_ZIP_DIR}/bin/mf6" "${MF6_ZIP_DIR}/bin/libmf6.so" -d /tmp/mf6_extracted \
    && install -m 755 "/tmp/mf6_extracted/${MF6_ZIP_DIR}/bin/mf6" /usr/local/bin/mf6 \
    && install -m 755 "/tmp/mf6_extracted/${MF6_ZIP_DIR}/bin/libmf6.so" /usr/local/lib/libmf6.so \
    && rm -rf /tmp/mf6.zip /tmp/mf6_extracted \
    && mf6 --version

# --- COPY entrypoint + fixtures ------------------------------------------
# Build context is the repo root (see infra/modflow/cloudbuild.yaml); paths
# below are relative to repo root.
RUN mkdir -p /opt/grace2/services/workers/modflow
COPY services/workers/modflow/ /opt/grace2/services/workers/modflow/

# --- COPY the shared offload substrate packages --------------------------
# Heavy-compute offload (reports/design/heavy-compute-offload-2026-07-02.md):
# the --build-spec-uri path imports the agent-import-free job_spec contract
# (_modflow_build), the plume postprocess (_modflow_postprocess), and the shared
# raster manifest/upload helpers (_raster_postprocess). All are PEP-420 namespace
# subpackages under services/workers/ (no __init__ at services/ level). Mirrors
# the SFINCS worker image's _raster_postprocess + _sfincs_build COPYs.
RUN mkdir -p /opt/grace2/services/workers/_raster_postprocess \
    && mkdir -p /opt/grace2/services/workers/_modflow_build \
    && mkdir -p /opt/grace2/services/workers/_modflow_postprocess
COPY services/workers/_raster_postprocess/ /opt/grace2/services/workers/_raster_postprocess/
COPY services/workers/_modflow_build/ /opt/grace2/services/workers/_modflow_build/
COPY services/workers/_modflow_postprocess/ /opt/grace2/services/workers/_modflow_postprocess/

# WORKDIR controls where mf6 writes its scratch outputs before the entrypoint
# uploads them. /opt/grace2/work is a writable scratch dir; the entrypoint
# clears it on start and re-creates it per invocation.
WORKDIR /opt/grace2

ENV PYTHONPATH=/opt/grace2 \
    PYTHONUNBUFFERED=1 \
    GRACE2_MF6_BIN=/usr/local/bin/mf6 \
    GRACE2_MF6_SCRATCH=/opt/grace2/work

# Runtime config. GCP is decommissioned: on AWS, MODFLOW runs the mf6 binary
# directly via the agent's local-exec supervisor (run_modflow.py) — there is NO
# Batch/Cloud-Run job-def for MODFLOW. This image is KEPT only as the SHA-pinned
# record of the USGS mf6 6.5.0 install the EC2 box reuses; its GCS-only entrypoint
# is effectively retired. The bucket defaults below are pinned to the AWS S3
# names for honesty (inert on the live local-exec path).
#   GRACE2_CACHE_BUCKET  — source bucket (FloPy deck + manifest under modflow/<run_id>/).
#   GRACE2_RUNS_BUCKET   — destination bucket for outputs (<run_id>/completion.json).
#   GRACE2_RUN_ID        — the run ID under which outputs land (set per run).
#   GRACE2_MANIFEST_URI  — URI of the JSON setup manifest the entrypoint reads.
ENV GRACE2_CACHE_BUCKET=grace2-hazard-cache-226996537797 \
    GRACE2_RUNS_BUCKET=grace2-hazard-runs-226996537797 \
    GRACE2_RUN_ID="" \
    GRACE2_MANIFEST_URI=""

# Build-time smoke (mirror of the SFINCS Dockerfile build-time assert): the
# mf6 binary exists + runs, and the entrypoint module imports cleanly. Catches
# a path shift or import mistake at build time instead of at Cloud Run exec.
RUN test -x /usr/local/bin/mf6 \
    && mf6 --version \
    && echo "mf6 binary OK at /usr/local/bin/mf6" \
    && python3 -c "from services.workers.modflow.entrypoint import main; print('entrypoint import OK')" \
    && python3 -c "from services.workers._modflow_build import validate_job_spec, build_deck_kwargs_from_spec; print('modflow_build (offload) substrate import OK')" \
    && python3 -c "from services.workers._modflow_postprocess import run_plume_postprocess; from services.workers._raster_postprocess import manifest, upload; print('modflow_postprocess substrate import OK')" \
    && python3 -c "from services.workers.modflow import gwt_adapter; print('gwt_adapter (build) import OK — has build_modflow_deck:', hasattr(gwt_adapter, 'build_modflow_deck'))"

# ENTRYPOINT runs this image as a one-shot solver Job. CMD is empty so Cloud
# Run Job task args feed straight through as argv to the module; env-var
# fallbacks (GRACE2_RUN_ID, GRACE2_MANIFEST_URI) are parsed by the entrypoint
# if no CLI flags are passed (mirror of the SFINCS pattern).
ENTRYPOINT ["python3", "-m", "services.workers.modflow.entrypoint"]
CMD []
