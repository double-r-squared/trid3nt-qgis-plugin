# The GeoClaw worker image: one staged run directory of .data files in, one
# solved run directory out.
#
# The launcher bind-mounts a run directory at /data (``docker run -v
# <rundir>:/data -w /data``) carrying manifest.json and every staged .data file
# (claw.data included). The entrypoint runs the PREBUILT xgeoclaw once and
# writes geoclaw_metrics.json back into the same directory. The container does
# no network and no object-store I/O: the supervisor uploads whatever it wrote.
#
# ENGINE + PIN DISCIPLINE:
#   * xgeoclaw is compiled ONCE at build from chile2010's own ``make .exe``
#     against the clawpack meta-repo at tag v5.14.0 - the same sources pip
#     installs (clawpack==5.14.0). Nothing compiles at run; the toolchain and
#     the source tree live only in the builder stage below.
#   * No netcdf anywhere: GeoClaw's own refusal stays readable by name
#     (``ERROR:  NetCDF library was not included in this build``) and the
#     product stages ascii topotypes only - topotype 4 is refused, never
#     silently read.
#   * setrun never runs here: .data files are authored on the daemon side and
#     staged into the mount. The entrypoint is stdlib python and reads
#     claw.data, never clawpack.
# A version skew here is a deliberate, IaC-visible bump.
#
# Build context = THIS worker dir, not the repo root:
#   docker build -t trid3nt-local/geoclaw:latest workers/geoclaw/

FROM python:3.12-slim-bookworm AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gfortran make git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "clawpack==5.14.0" numpy

RUN git clone --branch v5.14.0 --recurse-submodules \
        https://github.com/clawpack/clawpack.git /opt/claw

ENV CLAW=/opt/claw \
    CLAW_PYTHON=python3 \
    FC=gfortran

WORKDIR /opt/claw/geoclaw/examples/tsunami/chile2010
RUN make .exe

RUN cp xgeoclaw /usr/local/bin/xgeoclaw

# The daemon authors .data with Clawpack's own writers, so the wheel is a repo
# artifact: wheels/ carries it and pyproject pins clawpack to this release,
# installed by `uv pip install --find-links wheels`. Built in THIS stage - the
# stage that already installs clawpack from its sdist - so a host never builds
# one. Placed after make .exe so the compile layer stays cached. Extraction
# (absolute -f/context paths, REPO the repo root's absolute path):
#   docker build --target builder -f $REPO/workers/geoclaw/Dockerfile \
#       -t trid3nt-local/geoclaw-builder $REPO/workers/geoclaw
#   docker create --name geoclaw-wheel trid3nt-local/geoclaw-builder
#   docker cp geoclaw-wheel:/wheelhouse/. $REPO/wheels/
#   docker rm geoclaw-wheel
RUN pip wheel --no-cache-dir --no-deps -w /wheelhouse "clawpack==5.14.0"


FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="trid3nt-geoclaw-worker" \
      org.opencontainers.image.description="TRID3NT GeoClaw worker: a staged run directory of .data files in, a solved run directory out (prebuilt xgeoclaw over claw.data)."

# libgfortran5 is the compiled exe's own runtime - slim ships none. No compiler
# and no netcdf ever land in this stage: the exe needs the first's shared
# library only, and refusing netcdf keeps type-4 refusals legible.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgfortran5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/xgeoclaw /usr/local/bin/xgeoclaw

# --- the worker package ------------------------------------------------------
# The entrypoint is stdlib-only python; it lives beside nothing else.
COPY --chown=root:root entrypoint.py /opt/trid3nt/workers/geoclaw/entrypoint.py

ENV PYTHONUNBUFFERED=1 \
    TRID3NT_GEOCLAW_DATA_DIR=/data

WORKDIR /data

# Loader smoke: every shared object of the exe must resolve in THIS stage - the
# builder had the compiler, this one must not need it.
RUN ldd /usr/local/bin/xgeoclaw > /tmp/ldd.txt 2>&1 \
    && ! grep -q "not found" /tmp/ldd.txt \
    && rm /tmp/ldd.txt \
    && echo "xgeoclaw loader OK: no unresolved shared object"

# Build-time smoke: the entrypoint imports, its parser version and dispatch are
# the ones this image was built against, and the verdict sees both a solved run
# (mass line + tfinal reached) and the netcdf refusal through its own words. A
# path/import/pin mistake surfaces at BUILD time instead of mid-solve.
RUN python -c "\
import sys;\
sys.path.insert(0, '/opt/trid3nt/workers/geoclaw');\
import entrypoint as E;\
assert E._PARSER_VERSION == 'geoclaw-case-1', E._PARSER_VERSION;\
assert sorted(E._DISPATCH) == ['case'];\
ok = E._verdict(' Total mass at initial time:    1.0575487000653194E+017\n Done integrating to time    32400.000000000000\n', 32400.0, [], 0);\
assert ok['correct_end'] and ok['initial_mass'] == 1.0575487000653194e17, ok;\
ref = E._verdict(' ERROR:  NetCDF library was not included in this build\n', 32400.0, [], 0);\
assert not ref['correct_end'] and 'NetCDF' in ref['error'], ref;\
print('geoclaw worker OK: parser', E._PARSER_VERSION, '| verdict sees success and refusal')"
# The negative half runs in a separate process and is grepped, so an unrelated
# crash cannot pass for the refusal.
RUN (python -c "\
import sys;\
sys.path.insert(0, '/opt/trid3nt/workers/geoclaw');\
import entrypoint as E;\
E._strict_section('case', {'results': [], 'bogus_key': 1}, E._CASE_FIELDS)" \
        > /tmp/strict_gate.log 2>&1; echo "exit=$?" >> /tmp/strict_gate.log) \
    && grep -q "^exit=1$" /tmp/strict_gate.log \
    && grep -q "UnknownManifestFieldError" /tmp/strict_gate.log \
    && rm -f /tmp/strict_gate.log \
    && echo "geoclaw strict gate OK: an unknown case field refused"

# One-shot solver: the entrypoint reads /data/manifest.json and writes its
# outputs into /data. The launcher appends nothing.
CMD ["python", "/opt/trid3nt/workers/geoclaw/entrypoint.py"]
