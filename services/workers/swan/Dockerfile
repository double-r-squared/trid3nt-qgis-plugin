# SWAN (Simulating WAves Nearshore) wave solver AWS Batch worker (Phase 1).
#
# FILE-ONLY SCAFFOLD (NOT built / pushed in Phase 1). This authors the runtime
# image for the SWAN AWS Batch job -- the scale-to-zero CLOUD LANE for the SWAN
# third-generation spectral nearshore wave engine. The image is built+pushed off
# this Dockerfile via the SHARED grace2-worker-builder CodeBuild project
# (WORKER_DIR=swan, ECR_REPO=grace2-swan) in a SEPARATE gated infra step the
# orchestrator runs with NATE -- no docker build / ECR push happens here.
#
# SWAN is a GPL-3.0 Fortran-90 solver from TU Delft. GPL ISOLATION: the SWAN
# binary lives ONLY in this worker image and NEVER enters the agent venv (the
# agent only composes a build_spec JSON + submits over the Batch + S3 seam). The
# entrypoint shim performs the S3/GCS-IN -> author-INPUT (.swn) -> swanrun ->
# S3/GCS-OUT envelope; we do NOT modify the SWAN Fortran.
#
# DESIGN NOTE (single-stage compile-at-build, like the GeoClaw worker, NOT the
# SWMM multi-stage venv-copy): SWAN ships a Fortran source tarball that builds
# headless on Linux with `make config` (needs Perl) then `make ser` or `make omp`
# producing the `swan.exe` binary + the `swanrun` launcher. The shared-memory
# OpenMP build (`make omp`) maps 1:1 onto the single-node Batch sizing ladder
# (AWS_BATCH_COMPUTE_CLASS_SIZING -> OMP_NUM_THREADS); NO multinode/MPI is needed
# for the regional AOIs this system targets (and SOLVER_BATCH_JOBDEF_REGISTRY has no
# MPI/nodeProperties path). We compile swan.exe at BUILD time (unlike GeoClaw,
# which compiles per-deck at run time) so the image carries a ready binary and the
# per-solve cost is just the wave solve, not a compile.
#
# Version pins (mirror the SFINCS/GeoClaw/SWMM digest discipline -- bumps are a
# deliberate, IaC-visible step): debian:bookworm-slim base + SWAN 41.51 (the TU
# Delft source tarball). The source tarball is SHA256-pinned below (the digest
# analogue for a non-image asset; bump it in lockstep with SWAN_VERSION).
#
# NOTE for the gated build step (orchestrator + NATE): VERIFY the SWAN_VERSION +
# SWAN_SRC_SHA256 + the download URL against the live TU Delft / SourceForge
# release before the first build; the placeholder SHA256 below MUST be replaced
# with the real digest of the pinned tarball (the build's sha256sum -c gate will
# fail loudly until it is, which is the intended safety interlock).

# Base via the AWS ECR Public mirror of Docker Hub library images (NOT
# docker.io directly) to avoid the Docker Hub anonymous-pull 429 rate limit in
# CodeBuild -- mirrors the openquake worker's public.ecr.aws/docker/library base.
FROM public.ecr.aws/docker/library/debian:bookworm-slim AS runtime

LABEL org.opencontainers.image.title="grace-2-swan-solver" \
      org.opencontainers.image.source="https://github.com/double-r-squared/GRACE-2" \
      org.opencontainers.image.description="SWAN spectral nearshore wave solver runtime (AWS Batch). Reads a build_spec + bathy DEM from s3://...-cache/, authors a .swn command file, runs headless swanrun, writes the gridded wave-field output + completion manifest to s3://...-runs/<run_id>/." \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

ENV VENV=/opt/grace2/.venv \
    SWAN_HOME=/opt/swan \
    SWAN_VERSION=41.51 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# Build + run toolchain. gfortran + make + perl are REQUIRED to build SWAN
# (`make config` is a Perl script; `make omp` compiles the Fortran with OpenMP).
# perl + libgomp1 stay in the final image (libgomp1 is the OpenMP runtime the
# compiled swan.exe links against). ca-certificates for boto3 HTTPS; python3 +
# venv for the entrypoint + the deck author + boto3/scipy/rasterio I/O. curl:
# fetch the pinned SWAN source tarball at build time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gfortran \
        make \
        perl \
        libgomp1 \
        libexpat1 \
        curl \
        ca-certificates \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# --- SWAN source build (the GPL Fortran OpenMP binary) ----------------------
# Fetch the pinned SWAN source tarball, SHA256-verify it (the digest-pin analogue
# for a non-image asset; bump in lockstep with SWAN_VERSION), then build the
# OpenMP target. The canonical SWAN build is:
#   make config        # generates macros.inc for the platform (Perl)
#   make omp           # compiles swan.exe + the swanrun launcher (OpenMP)
# We install the resulting swan.exe + swanrun onto $SWAN_HOME on PATH.
#
# GATED-BUILD PLACEHOLDER: replace SWAN_SRC_SHA256 + SWAN_SRC_URL with the real
# pinned values at the first build (the sha256sum -c gate fails until then -- the
# intended interlock so an unverified binary never ships).
# sha256 updated 2026-07-05: SourceForge tarball bytes drifted (same URL/version);
# verified VERNUM=41.51 in swanmain.ftn + Makefile present + 79 Fortran sources / 95 entries.
ENV SWAN_SRC_SHA256=2e27367dd8c6e8316ce4f446ffad790e1cfca0a6649f7bc2291a81899ac0c4d0 \
    SWAN_SRC_URL=https://swanmodel.sourceforge.io/download/zip/swan4151.tar.gz
RUN mkdir -p "$SWAN_HOME/src" \
    && curl -fsSL -o /tmp/swan-src.tar.gz "$SWAN_SRC_URL" \
    && echo "${SWAN_SRC_SHA256}  /tmp/swan-src.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/swan-src.tar.gz -C "$SWAN_HOME/src" --strip-components=1 \
    && rm -f /tmp/swan-src.tar.gz \
    && cd "$SWAN_HOME/src" \
    && make config \
    && make omp \
    && install -m 0755 swan.exe "$SWAN_HOME/swan.exe" \
    && install -m 0755 swanrun "$SWAN_HOME/swanrun" \
    && test -x "$SWAN_HOME/swan.exe" \
    && test -x "$SWAN_HOME/swanrun"

# swanrun expects swan.exe on PATH (it invokes `swan.exe`); put $SWAN_HOME first.
ENV PATH="$SWAN_HOME:$PATH"

# --- Python venv (the entrypoint + S3 envelope + deck author) ---------------
# The deck author (deck_builder) is SWAN-free pure Python; boto3 for the S3
# envelope; numpy + rasterio to sample the staged bathy DEM onto the SWAN bottom
# input grid; scipy for reading the SWAN .mat output if the entrypoint ever
# self-checks (the agent-side postprocess_swan is the primary path). No GPL SWAN
# symbol is imported. libexpat1 (in the apt layer) backs rasterio's GDAL XML
# driver (the job-0240 lesson the MODFLOW/GeoClaw images hit).
RUN python3 -m venv "$VENV" \
    && "$VENV/bin/pip" install --no-cache-dir --upgrade pip setuptools wheel \
    && "$VENV/bin/pip" install --no-cache-dir \
        "numpy>=1.26,<3" \
        "scipy>=1.11,<2" \
        "rasterio>=1.3,<2" \
        "boto3>=1.34,<2" \
    && "$VENV/bin/python" -c "import numpy, scipy, rasterio, boto3; print('swan worker deps import OK')"

ENV PATH="$VENV/bin:$PATH"

# COPY the worker package (entrypoint + deck_builder) onto a stable runtime path.
# Build context is the repo root (mirrors the SFINCS/GeoClaw/SWMM context); paths
# below are relative to repo root.
RUN mkdir -p /opt/grace2/services/workers/swan
COPY services/workers/swan/ /opt/grace2/services/workers/swan/

# WORKDIR controls where SWAN writes its scratch output (INPUT + swan_out.mat)
# before the entrypoint uploads it. /opt/grace2/work is writable scratch; the
# entrypoint clears + re-creates it per invocation.
WORKDIR /opt/grace2

ENV PYTHONPATH=/opt/grace2 \
    PYTHONUNBUFFERED=1 \
    GRACE2_SWAN_SCRATCH=/opt/grace2/work

# Runtime config -- overridable from the AWS Batch container env block.
ENV GRACE2_OBJECT_STORE=s3 \
    GRACE2_RUNS_BUCKET="" \
    GRACE2_RUN_ID="" \
    GRACE2_MANIFEST_URI="" \
    OMP_NUM_THREADS=8 \
    AWS_REGION=us-west-2

# Build-time smoke: the (swan-free) deck-author module imports cleanly AND renders
# a .swn from a synthetic build_spec (no SWAN run), AND swan.exe is executable.
# Catches a path/import/build mistake at build time instead of at Batch exec time.
RUN python3 -c "from services.workers.swan.entrypoint import main; print('entrypoint import OK')" \
    && python3 -c "from services.workers.swan.deck_builder import render_swn_command_file, parse_build_spec; \
print('swn render OK len=', len(render_swn_command_file(parse_build_spec({'mode':'stationary','bbox':[-85.75,29.55,-85.25,30.20],'bottom_file':'bottom.bot'}))))" \
    && test -x "$SWAN_HOME/swan.exe" \
    && echo "SWAN binary OK at $SWAN_HOME"

# ENTRYPOINT runs this image as a one-shot solver job. CMD is empty so AWS Batch
# containerOverrides command feeds straight through as argv; env-var fallbacks
# (GRACE2_RUN_ID, GRACE2_MANIFEST_URI) are parsed by the entrypoint if no CLI
# flags are passed (mirror of the SFINCS/GeoClaw/SWMM pattern).
ENTRYPOINT ["python3", "-m", "services.workers.swan.entrypoint"]
CMD []
