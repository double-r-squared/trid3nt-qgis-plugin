# HEC-RAS 2025 Beta -- Linux headless-characterization worker (ADR 0127 spike).
#
# HEC-RAS 2025 is HEC's ground-up C#/.NET rewrite: a single-`.h5` project, a new
# explicit solver, a NATIVE headless mesher, and a documented `ras` CLI
# (createterrain -> mesh -> prepare -> solve -> map). NATE's decision banks on
# this line to retire the 6.x M3 STOP (RASMapper's Windows-DLL subgrid tables):
# `ras prepare` computes the subgrid property tables headless.
#
# SPIKE FINDING (2026-08-04): the managed .NET 9 CLI is Linux-PORTABLE, but the
# public release (hec-downloads 1.0.44, HEC-RAS_2025_Beta.zip, sha256
# 0df9cf0d29dc6cd6c50636bca9cf8206c0f83d1fa8a62a03c37f748daf9f5cf5) is a
# win-x64 SELF-CONTAINED publish -- every native compute/geospatial lib
# (RasNativeParallel, gdal_wrap, libhdf5, hecdss) is Windows only, and no
# Linux/container build is published. So a headless SOLVE is NOT yet possible:
# this image is the reproducible CHARACTERIZATION probe + the flip-ready host,
# NOT a registered engine. The entrypoint runs the managed CLI and reports the
# NO-GO-YET verdict live (exit 3). See README.md + docs/decisions/0127.
#
# CONTAINER HYGIENE (hard rule): the 127 MB zip + curl/unzip stay in the `fetch`
# stage; the 159 MB of win-x64 NATIVE DLLs (unusable on Linux) are DROPPED -- only
# the ~181 MB of portable managed .NET assemblies are baked. The GUI (WinUI) still
# rides in that managed set; a headless-only managed trim is a future step once
# the Linux natives exist and the exact solve assembly graph is pinned.
#
# PROVENANCE: HEC-RAS is public-domain U.S. Federal Government software, freely
# redistributable (feasibility report S6, same terms as the 6.x worker). BETA:
# USACE says do NOT use HEC-RAS 2025 for production studies.

# ---------------------------------------------------------------------------
# Stage 1 -- fetch: download HEC-RAS_2025_Beta.zip, SHA-256 verify BEFORE
# extraction, keep ONLY the portable managed .NET assemblies.
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS fetch

ARG RAS2025_ZIP_URL=https://github.com/HydrologicEngineeringCenter/hec-downloads/releases/download/1.0.44/HEC-RAS_2025_Beta.zip
# SHA-256 of HEC-RAS_2025_Beta.zip (hec-downloads release 1.0.44, 2026-03-30),
# verified on the ADR 0127 characterization run 2026-08-04.
ARG RAS2025_ZIP_SHA256=0df9cf0d29dc6cd6c50636bca9cf8206c0f83d1fa8a62a03c37f748daf9f5cf5

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates curl unzip file \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fL "$RAS2025_ZIP_URL" -o /tmp/ras2025.zip \
    && echo "${RAS2025_ZIP_SHA256}  /tmp/ras2025.zip" | sha256sum -c - \
    && unzip -q /tmp/ras2025.zip -d /tmp/x \
    && rm /tmp/ras2025.zip \
    && test -f "/tmp/x/HEC-RAS 2025 Beta/ras.dll"

# Keep only managed .NET assemblies (drop the 159 MB of win-x64 native PE DLLs +
# the .exe apphosts, which cannot run on Linux) + the CLI's config/deps.
RUN mkdir -p /staging/app \
    && cd "/tmp/x/HEC-RAS 2025 Beta" \
    && for f in *.dll; do \
         if file "$f" | grep -q "Mono/.Net"; then cp "$f" /staging/app/; fi; \
       done \
    && cp ras.deps.json /staging/app/ \
    && cp -r en-us /staging/app/ 2>/dev/null || true \
    && echo "managed assemblies staged:" && ls /staging/app/*.dll | wc -l

# ---------------------------------------------------------------------------
# Stage 2 -- runtime: the .NET 9 shared runtime + the managed assemblies +
# the framework-dependent runtimeconfig + the probe entrypoint.
# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/dotnet/runtime:9.0 AS runtime

LABEL org.opencontainers.image.title="trid3nt-hecras2025-probe" \
      org.opencontainers.image.description="HEC-RAS 2025 Beta headless characterization host (ADR 0127 spike). The managed .NET 9 ras CLI is Linux-portable; the native solve payload is win-x64 only, so this is a NO-GO-YET characterization probe + flip-ready host, not a solver. HEC-RAS is public-domain U.S. Federal Government software (U.S. Army Corps of Engineers, Hydrologic Engineering Center). BETA -- not for production studies." \
      org.opencontainers.image.licenses="Public-Domain-US-Gov"

COPY --from=fetch /staging/app/ /opt/hecras2025/app/
# Replace the shipped self-contained runtimeconfig (Windows libhostpolicy.so)
# with a framework-dependent one so the managed CLI loads on the shared runtime.
COPY ras.fwdep.runtimeconfig.json /opt/hecras2025/app/ras.runtimeconfig.json
COPY entrypoint.sh /opt/hecras2025/entrypoint.sh
RUN chmod +x /opt/hecras2025/entrypoint.sh

ENV RAS_DLL=/opt/hecras2025/app/ras.dll \
    TRID3NT_HECRAS2025_OUT_DIR=/data \
    DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1

# Build-time smoke: the managed CLI loads on Linux and prints its build string
# (Spectre.Console may wrap it in ANSI codes, so match the substring, not an anchor).
RUN dotnet /opt/hecras2025/app/ras.dll --version 2>&1 | grep -qi 'ras 0'

WORKDIR /data
ENTRYPOINT ["/opt/hecras2025/entrypoint.sh"]
CMD []
