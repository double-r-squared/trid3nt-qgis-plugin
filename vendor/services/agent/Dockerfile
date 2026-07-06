# GRACE-2 agent image -- the EXISTING grace2_agent WS server, containerized
# BYTE-IDENTICAL for the Fargate-per-session isolation foundation
# (reports/design/agent_isolation_spike.md, verdict HYBRID_FARGATE_NOW).
#
# WHAT THIS IS: one container == one Python process == one user's session. The
# image runs `grace2-agent` (== `grace2_agent.main:run` -> `server.run_server`)
# UNCHANGED. The spike's whole point: today's accidental module-level
# per-session sharing becomes REAL isolation by construction, because a per-
# session Fargate task hosts exactly ONE session. No agent code changes.
#
# PORTS (honored by server.run_server, do NOT renumber):
#   8765  WS server                (serve(...), server.py:9897; GRACE2_AGENT_PORT)
#   8766  catalog + /api/health    (serve_catalog_http; GRACE2_AGENT_HTTP_PORT)
# The broker proxies the WSS to :8765 and the per-task idle reaper polls
# :8766/api/health (the authoritative `busy` from is_busy()/liveness_snapshot()).
# `server.run_server` already binds GRACE2_AGENT_HOST=0.0.0.0 (server.py:9844)
# so the container is reachable on its task ENI with no app rewrite.
#
# BASE CHOICE: ECR Public mirror of the Docker Official python image, NOT Docker
# Hub -- the off-box-codebuild lesson (project_offbox_codebuild_worker_builder):
# CodeBuild pulls base images ANONYMOUSLY and Docker Hub returns 429 on repeat
# pulls. `public.ecr.aws/docker/library/python` is byte-identical with no rate
# limit. python 3.13 matches the agent venv (pyproject targets 3.13 wheels:
# pandas 2.2.x cp313, flopy, etc).
#
# ARCHITECTURE: linux/amd64 (Fargate X86_64 task; matches the agent EC2 box and
# the worker images). NOT arm64 -- that is an AgentCore-LATER concern (spike
# section 6 Gate A), explicitly out of scope for the Fargate-NOW path.
#
# CONTAINER-HYGIENE NORM (feedback_container_image_hygiene): multi-stage (a
# fat builder that compiles wheels -> a slim runtime that carries only the venv +
# the agent src), a .dockerignore (services/agent/.dockerignore) that excludes
# .venv / tests / __pycache__ / evidence / the local .env, and a documented size
# estimate. SIZE ESTIMATE BELOW.
#
# *** DO NOT BUILD ON THE AGENT BOX OR THE DEV BOX *** -- build OFF-box via the
# CodeBuild project authored alongside this Dockerfile
# (infra/aws-agent-isolation/codebuild.tf, buildspec.agent.yml). No docker is
# available on the dev box; the agent box autostop is blind to docker builds.

# --------------------------------------------------------------------------- #
# Stage 1: builder -- compile/resolve the full agent dependency closure into an
# isolated venv. Carries the toolchain (gcc/g++/git) + GDAL/PROJ headers that a
# handful of geo deps need at build time. NONE of this lands in the runtime.
# --------------------------------------------------------------------------- #
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_PREFER_BINARY=1

# Build toolchain + headers. git: a few deps (and any git+ pins) resolve from
# VCS. The geo stack (rasterio/rioxarray/pyproj/shapely/fiona) ships manylinux
# wheels (PIP_PREFER_BINARY keeps us on them, carrying their OWN bundled GDAL/
# PROJ -- the same wheel-GDAL discipline as the sfincs worker), so libgdal-dev is
# a fallback only; build-essential covers the occasional sdist (e.g. a cp313
# wheel gap).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv so the runtime stage copies ONE self-contained directory.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" VIRTUAL_ENV="/opt/venv"
# uv (Astral) is the resolver: pip's backtracking resolver cannot solve the
# agent's large dependency graph (resolution-too-deep after ~19 min). uv resolves
# the same graph in seconds and installs into the active venv (VIRTUAL_ENV).
RUN pip install --upgrade pip setuptools wheel uv

# Install the agent package from its own pyproject so the dependency set stays
# the SINGLE source of truth (services/agent/pyproject.toml) -- never re-listed
# here, so it cannot drift from the live venv. Build context is the repo ROOT
# (the buildspec sets it) so the COPY paths are repo-root-relative, matching the
# worker images.
#
# LOCAL contracts package FIRST. services/agent/pyproject.toml declares
# ``grace2-contracts>=0.1,<0.2`` but that package is NOT on PyPI -- it lives in
# this repo at packages/contracts/ and is consumed verbatim (the on-box deploy,
# scripts/deploy_agent_bundle.sh, bundles + installs it the same way). Install it
# here BEFORE the agent so that when ``pip install /build/services/agent`` resolves
# its dependency set the grace2-contracts requirement is ALREADY satisfied by the
# locally-built 0.1.0 wheel (pip never reaches PyPI for an already-satisfied pin).
# Copy ONLY the install inputs (pyproject + README + src) -- NOT the package's
# .venv / .pytest_cache / egg-info -- so the build context layer stays slim.
COPY packages/contracts/pyproject.toml /build/packages/contracts/pyproject.toml
COPY packages/contracts/README.md /build/packages/contracts/README.md
COPY packages/contracts/src /build/packages/contracts/src
RUN uv pip install --no-cache /build/packages/contracts

# Copy ONLY the install inputs first (pyproject + the package source) so the
# expensive dependency layer caches across code-only edits.
COPY services/agent/pyproject.toml /build/services/agent/pyproject.toml
COPY services/agent/README.md /build/services/agent/README.md
COPY services/agent/src /build/services/agent/src
COPY services/agent/vendor /build/services/agent/vendor
RUN uv pip install --no-cache --find-links /build/services/agent/vendor /build/services/agent

# Prune byte-compiled caches only (NOT package tests/ -- numpy/scipy import some
# at runtime; same rationale as the sfincs worker). Hygiene saving on the heavy
# numba/scipy/rasterio binaries is marginal, so we only strip __pycache__/.pyc.
RUN find /opt/venv -depth -type d -name '__pycache__' -exec rm -rf {} + \
    && find /opt/venv -type f -name '*.pyc' -delete

# --------------------------------------------------------------------------- #
# Stage 2: runtime -- slim base + ONLY the venv (which contains the installed
# grace2_agent package + all deps). No toolchain, no headers, no build context.
# --------------------------------------------------------------------------- #
FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="grace-2-agent" \
      org.opencontainers.image.source="https://github.com/double-r-squared/GRACE-2" \
      org.opencontainers.image.description="GRACE-2 agent WS server (grace2_agent), containerized byte-identical for Fargate-per-session isolation. One container = one session. WS :8765, /api/health :8766. Runs grace2-agent (main:run -> server.run_server)." \
      org.opencontainers.image.licenses="proprietary"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Minimal runtime OS deps. ca-certificates: HTTPS to Bedrock/Cognito-JWKS/S3/the
# data sources. libexpat1: GDAL/rasterio link libexpat.so.1 and the slim base
# does NOT ship it (importing the tools registry crashes without it). libgomp1:
# OpenMP runtime that scipy/numba/rasterio load at import/call time on slim.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libexpat1 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# The whole app + its deps live in the venv; copy it verbatim from the builder.
COPY --from=builder /opt/venv /opt/venv

# Run as a non-root user (the EC2 systemd unit runs as root for the editable
# /opt/grace2 mount; the container needs no host mount so it drops privileges).
RUN useradd --create-home --uid 10001 grace2
USER grace2
WORKDIR /home/grace2

# --- Runtime env: CARRIED OVER from the live systemd unit (deploy facts) -------
# These are the SAME values the box's systemd grace2-agent service sets. They are
# baked as honest defaults; the Fargate task definition
# (infra/aws-agent-isolation/ecs.tf) overrides per-environment from variables so
# a tfvars change never needs an image rebuild. Secrets (none here -- Bedrock/S3/
# DynamoDB use the task IAM role, NOT keys) are never baked.
ENV GRACE2_AGENT_HOST=0.0.0.0 \
    GRACE2_AGENT_PORT=8765 \
    GRACE2_AGENT_HTTP_PORT=8766 \
    MODEL_PROVIDER=bedrock \
    GRACE2_AWS_REGION=us-west-2 \
    AWS_REGION=us-west-2 \
    GRACE2_PERSISTENCE_BACKEND=dynamodb \
    GRACE2_DYNAMO_TABLE_PREFIX=grace2_ \
    GRACE2_STORAGE_BACKEND=s3 \
    GRACE2_CACHE_BUCKET=grace2-hazard-cache-226996537797 \
    GRACE2_RUNS_BUCKET=grace2-hazard-runs-226996537797 \
    GRACE2_COGNITO_USER_POOL_ID=us-west-2_mIpKrr727 \
    GRACE2_COGNITO_CLIENT_ID=43ovkrtt97oh6gsnl006aecera

# WS server + catalog/health. The broker proxies to 8765; the idle reaper polls
# 8766/api/health.
EXPOSE 8765 8766

# Lightweight liveness for ECS container health checks (the AUTHORITATIVE
# session-busy signal stays /api/health on 8766, polled by the idle reaper; this
# is only "is the process serving"). python over curl -> no extra OS dep.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('GRACE2_AGENT_HTTP_PORT','8766')+'/api/health', timeout=4)" || exit 1

# Entrypoint = the console script (pyproject [project.scripts] grace2-agent =
# grace2_agent.main:run), identical to what the systemd ExecStart runs.
ENTRYPOINT ["grace2-agent"]
CMD []

# --------------------------------------------------------------------------- #
# IMAGE SIZE / LAYER ESTIMATE (NO docker on the dev box -- this is an estimate to
# validate on the first CodeBuild run via `docker history` + `docker images`):
#
#   runtime base (python:3.11-slim-bookworm, ECR Public)   ~150 MB
#   ca-certificates layer                                  ~5 MB
#   /opt/venv (the heavy part -- the agent dep closure):
#     boto3/botocore + s3fs + requests                     ~120 MB
#     rasterio + rioxarray + py3dep + pyproj + shapely     ~180 MB  (manylinux GDAL/PROJ wheels)
#     pandas 2.2 + numpy + xarray + scipy                  ~190 MB
#     hydromt-sfincs + hydromt + flopy + pystac-client     ~140 MB
#     google-genai (Bedrock types carve-out) + google-cloud-storage/run  ~90 MB
#     PyJWT[crypto] + cryptography + the rest              ~60 MB
#   ------------------------------------------------------------
#   ESTIMATED total uncompressed:                          ~1.2-1.6 GB
#
# This is HEAVY -- it carries the full 94-tool DEFINITION closure + the geo deps.
# The cold-start watch-item from the spike (section 9.3) is the IMAGE PULL: a
# ~1.4 GB image is the dominant term in Fargate task start (~30-40s). Mitigations
# the IaC scaffold leaves room for: (a) Fargate ephemeral image caching, (b) an
# optional 1-task warm pool (ecs.tf desired_count toggle), (c) a LATER split of
# the heavy geo/tool-EXEC deps off the loop image (spike Gate A: the loop only
# needs Bedrock + the 94 tool DEFINITIONS + the WS layer; heavy geo already runs
# on Batch/QGIS-worker) -- deferred, not done here, because the NOW goal is the
# agent BYTE-IDENTICAL.
#
# vs the 2 GB AgentCore Runtime ceiling: this image is plausibly UNDER it already,
# but AgentCore also mandates ARM64 -- a separate rebuild that is explicitly
# LATER (spike section 6). Not a concern for Fargate.
# --------------------------------------------------------------------------- #
