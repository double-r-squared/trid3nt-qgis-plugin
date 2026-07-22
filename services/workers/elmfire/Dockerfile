# GRACE-2 ELMFIRE wildfire-spread AWS Batch worker image (FIRE-4).
#
# Successor of the FIRE-1 dev image (trid3nt/elmfire:dev, proven against
# tutorial 01 + verification case 01 - reports/inflight/fire-1-container-proof.md).
# Two deliberate changes from that dev image, everything else preserved:
#
#   1. REPO-ROOT BUILD CONTEXT (CodeBuild parity). The FIRE-1 dev image built
#      FROM the vendor elmfire checkout as context; the shared
#      grace2-worker-builder CodeBuild flow builds every worker with the
#      GRACE-2 repo root as context (docker build --file
#      services/workers/<w>/Dockerfile .). So the ELMFIRE source is now
#      FETCHED at build time: the pinned release tarball
#      (2025.0526 == commit 23a4cbd27fc84b4b194fed48a8d933f7e7c7fdeb, the
#      FIRE-1-proven tag), SHA256-verified (the SWAN worker's digest-pin
#      discipline for non-image assets).
#
#   2. PORTABLE MICROARCHITECTURE. Upstream Makefile_elmfire{,_post} compile
#      with -march=native - fine on a dev box, a SIGILL lottery on Batch
#      (image builds on CodeBuild/dev hardware, runs on whatever Spot host the
#      CE picks). Genericized to -march=x86-64-v3 (AVX2 baseline; every family
#      in the CE pool - c5/c6i/c7i/m7i/r7i, Skylake+ - supports it). The sed
#      is ASSERTED: the build fails loudly if upstream renames the flag.
#
# Runtime notes (verified against the 2025.0526 Fortran source):
#   - gdal-bin is REQUIRED in the final image: elmfire_io.f90 shells out to
#     gdal_translate (EXECUTE_COMMAND_LINE, PATH_TO_GDAL default '/usr/bin')
#     to convert GeoTIFF inputs to ENVI BSQ for reading (and for the
#     CONVERT_TO_GEOTIFF output path). python3-gdal is NOT needed (that was
#     tutorial-script baggage), nor are bc/jq/pigz/csvkit.
#   - NO rasterio: the FIRE-2 deck builder runs AGENT-SIDE
#     (run_elmfire.build_elmfire_deck); this worker stages a READY deck.
#   - boto3 venv: the entrypoint's S3 envelope (manifest read, deck download,
#     outputs + completion.json upload).
#
# Multi-stage stays TRUE multi-stage (the FIRE-1 lesson: upstream's
# same-stage purge reclaims nothing): compilers live only in the builder;
# the final stage gets binaries + runtime libs only.
#
# Base via the AWS ECR Public mirror (NOT docker.io) - the CodeBuild
# anonymous-pull 429 lesson (mirrors the SWAN/openquake workers).

FROM public.ecr.aws/ubuntu/ubuntu:22.04 AS builder
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -y && \
    apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        gfortran \
        libopenmpi-dev \
        make \
        openmpi-bin && \
    rm -rf /var/lib/apt/lists/*

# Pinned source: release tag 2025.0526 (the FIRE-1-proven tag; commit
# 23a4cbd27fc84b4b194fed48a8d933f7e7c7fdeb). SHA256 of the GitHub tag archive
# verified 2026-07-08; bump BOTH in lockstep on a release bump.
ENV ELMFIRE_VER=2025.0526 \
    ELMFIRE_SRC_SHA256=6f043180124ee574cc586c6ae89d43292500730bff4f9f30820fba9b0da21590
RUN curl -fsSL -o /tmp/elmfire-src.tar.gz \
        "https://github.com/lautenberger/elmfire/archive/refs/tags/${ELMFIRE_VER}.tar.gz" && \
    echo "${ELMFIRE_SRC_SHA256}  /tmp/elmfire-src.tar.gz" | sha256sum -c - && \
    mkdir -p /elmfire/elmfire && \
    tar -xzf /tmp/elmfire-src.tar.gz -C /elmfire/elmfire --strip-components=1 && \
    rm -f /tmp/elmfire-src.tar.gz

# Genericize the microarchitecture (FIRE-1 portability caveat), ASSERTED so an
# upstream Makefile change cannot silently reintroduce -march=native.
WORKDIR /elmfire/elmfire/build/linux
RUN sed -i 's/-march=native/-march=x86-64-v3/' Makefile_elmfire Makefile_elmfire_post && \
    ! grep -q 'march=native' Makefile_elmfire Makefile_elmfire_post && \
    grep -q 'march=x86-64-v3' Makefile_elmfire && \
    grep -q 'march=x86-64-v3' Makefile_elmfire_post

# Short serial Fortran compile (~1 min) via the repo's own build script.
RUN ./make_gnu.sh && test -x "bin/elmfire_${ELMFIRE_VER}"

FROM public.ecr.aws/ubuntu/ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -y && \
    apt-get install --no-install-recommends -y \
        ca-certificates \
        gdal-bin \
        libgfortran5 \
        libgomp1 \
        openmpi-bin \
        python3 \
        python3-venv && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Solver binaries only (release-named + convenience symlinks) - no repo copy;
# the tutorials/verification trees belong to the dev image, not the Batch worker.
COPY --from=builder /elmfire/elmfire/build/linux/bin /opt/elmfire/bin
ENV ELMFIRE_VER=2025.0526 \
    GRACE2_ELMFIRE_BINARY=elmfire_2025.0526 \
    PATH=/opt/elmfire/bin:$PATH

# Python venv for the entrypoint's S3 envelope (boto3 ONLY - no rasterio, the
# deck builds agent-side).
ENV VENV=/opt/grace2/venv \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python3 -m venv "$VENV" && \
    "$VENV/bin/pip" install --no-cache-dir --upgrade pip setuptools wheel && \
    "$VENV/bin/pip" install --no-cache-dir "boto3>=1.34,<2" && \
    "$VENV/bin/python" -c "import boto3; print('elmfire worker deps import OK')"

# The worker package (entrypoint + deck_builder for provenance/tests) on a
# stable runtime path; repo-root-relative COPY (CodeBuild context parity).
RUN mkdir -p /opt/grace2/services/workers/elmfire
COPY services/workers/elmfire/ /opt/grace2/services/workers/elmfire/
RUN find /opt/grace2/services -name __pycache__ -type d -prune -exec rm -rf {} + || true

# WORKDIR scratch: the entrypoint clears + recreates /opt/grace2/work per run.
WORKDIR /opt/grace2
ENV PYTHONPATH=/opt/grace2 \
    PYTHONUNBUFFERED=1 \
    GRACE2_ELMFIRE_SCRATCH=/opt/grace2/work

# Runtime config - overridden from the AWS Batch job-definition env block.
ENV GRACE2_OBJECT_STORE=s3 \
    GRACE2_RUNS_BUCKET="" \
    GRACE2_RUN_ID="" \
    GRACE2_MANIFEST_URI="" \
    AWS_REGION=us-west-2

# Build-time smoke: entrypoint imports + solver binary executes (prints usage).
RUN "$VENV/bin/python" -c "from services.workers.elmfire.entrypoint import main; print('entrypoint import OK')" && \
    test -x "/opt/elmfire/bin/elmfire_${ELMFIRE_VER}" && \
    command -v gdal_translate

# One-shot solver job: AWS Batch containerOverrides command feeds straight
# through as argv (--run-id / --manifest-uri; env fallbacks parsed by the
# entrypoint) - the SFINCS/GeoClaw/SWAN pattern.
ENTRYPOINT ["/opt/grace2/venv/bin/python", "-m", "services.workers.elmfire.entrypoint"]
CMD []
