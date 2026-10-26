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
#   * xgeoclaw_bouss, the Boussinesq (SGN / Madsen-Sorensen) executable, is
#     compiled ONCE beside it from examples/bouss/radial_flat's own Makefile
#     against PETSc 3.23.7 (Clawpack 5.14 requires 3.23 or later; bookworm
#     ships 3.18, so PETSc is built from its release tarball) and MPICH. The
#     Makefile runs as written in a copy of the directory whose one custom
#     source, radial_flat's own qinit.f90 (a Gaussian hump at the origin that
#     would raise a 5 m hump in every run), is replaced by a link to GeoClaw's
#     standard qinit.f90. The worker runs the executable under mpiexec with
#     Clawpack's own petscMPIoptions.
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
        gfortran gcc make git ca-certificates curl pkg-config mpich libmpich-dev \
        liblapack-dev libblas-dev \
    && rm -rf /var/lib/apt/lists/*

# PETSc, once: shared, optimised, MPI through MPICH's wrappers, no C++, no
# downloads. Installed under /opt/petsc, so PETSC_ARCH stays empty.
ARG PETSC_VERSION=3.23.7
ARG PETSC_SHA256=f4fb2bad8c80319e723987868e87d8384f2ae2162e07917d4c41d6e467b1d254
RUN curl -fsSL -o /tmp/petsc.tar.gz \
        https://web.cels.anl.gov/projects/petsc/download/release-snapshots/petsc-${PETSC_VERSION}.tar.gz \
    && echo "${PETSC_SHA256}  /tmp/petsc.tar.gz" | sha256sum -c - \
    && mkdir /tmp/petsc-src && tar -xzf /tmp/petsc.tar.gz -C /tmp/petsc-src --strip-components=1 \
    && rm /tmp/petsc.tar.gz
WORKDIR /tmp/petsc-src
RUN ./configure --prefix=/opt/petsc --with-debugging=0 --with-shared-libraries=1 \
        --with-cc=mpicc --with-fc=mpifort --with-cxx=0 \
        --COPTFLAGS=-O2 --FOPTFLAGS=-O2 --with-x=0 \
        --with-blaslapack-lib="-llapack -lblas"
RUN make -j"$(nproc)" PETSC_DIR=/tmp/petsc-src PETSC_ARCH=arch-linux-c-opt all \
    && make PETSC_DIR=/tmp/petsc-src PETSC_ARCH=arch-linux-c-opt install \
    && rm -rf /tmp/petsc-src
WORKDIR /

RUN pip install --no-cache-dir "clawpack==5.14.0" numpy

RUN git clone --branch v5.14.0 --recurse-submodules \
        https://github.com/clawpack/clawpack.git /opt/claw

ENV CLAW=/opt/claw \
    CLAW_PYTHON=python3 \
    FC=gfortran

WORKDIR /opt/claw/geoclaw/examples/tsunami/chile2010
RUN make .exe

RUN cp xgeoclaw /usr/local/bin/xgeoclaw

# The Boussinesq executable. The environment is the Makefile's own demand (see
# examples/bouss/setenv.sh); the compile happens here and never at run.
ENV PETSC_DIR=/opt/petsc \
    PETSC_ARCH= \
    PETSC_OPTIONS="-options_file /opt/claw/geoclaw/examples/bouss/petscMPIoptions" \
    CLAW_MPIEXEC=mpiexec \
    CLAW_MPIFC=mpif90
WORKDIR /opt/claw/geoclaw/examples/bouss
RUN cp -r radial_flat bouss_build && cd bouss_build \
    && ln -sf /opt/claw/geoclaw/src/2d/shallow/qinit.f90 qinit.f90 \
    && make new && cp xgeoclaw /usr/local/bin/xgeoclaw_bouss

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


# Never part of the shipped image: radial_flat's Makefile as written, its own
# qinit.f90 compiled in, so Clawpack's setrun and the sheet can be solved
# through the executable that setrun was written for.
#   docker build --target radial-flat -f $REPO/workers/geoclaw/Dockerfile \
#       -t trid3nt-local/geoclaw-radial-flat $REPO/workers/geoclaw
FROM builder AS radial-flat
RUN cd /opt/claw/geoclaw/examples/bouss/radial_flat && make new \
    && cp xgeoclaw /usr/local/bin/xgeoclaw_radial_flat


FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="trid3nt-geoclaw-worker" \
      org.opencontainers.image.description="TRID3NT GeoClaw worker: a staged run directory of .data files in, a solved run directory out (prebuilt xgeoclaw over claw.data)."

# libgfortran5 is the compiled exe's own runtime - slim ships none. No compiler
# and no netcdf ever land in this stage: the exe needs the first's shared
# library only, and refusing netcdf keeps type-4 refusals legible.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgfortran5 libgomp1 mpich \
        libmpich12 liblapack3 libblas3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/bin/xgeoclaw /usr/local/bin/xgeoclaw
COPY --from=builder /usr/local/bin/xgeoclaw_bouss /usr/local/bin/xgeoclaw_bouss
COPY --from=builder /opt/petsc/lib/libpetsc.so* /opt/petsc/lib/
COPY --from=builder /opt/claw/geoclaw/examples/bouss/petscMPIoptions /opt/petsc/petscMPIoptions
ENV LD_LIBRARY_PATH=/opt/petsc/lib \
    PETSC_OPTIONS="-options_file /opt/petsc/petscMPIoptions"

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
RUN ldd /usr/local/bin/xgeoclaw_bouss > /tmp/ldd.txt 2>&1 \
    && ! grep -q "not found" /tmp/ldd.txt \
    && rm /tmp/ldd.txt \
    && command -v mpiexec \
    && echo "xgeoclaw_bouss loader OK: no unresolved shared object"

# Build-time smoke: the entrypoint imports, its parser version and dispatch are
# the ones this image was built against, and the verdict sees both a solved run
# (mass line + tfinal reached) and the netcdf refusal through its own words. A
# path/import/pin mistake surfaces at BUILD time instead of mid-solve.
RUN python -c "\
import sys;\
sys.path.insert(0, '/opt/trid3nt/workers/geoclaw');\
import entrypoint as E;\
assert E._PARSER_VERSION == 'geoclaw-case-2', E._PARSER_VERSION;\
assert sorted(E._DISPATCH) == ['case'];\
ok = E._verdict(' Total mass at initial time:    1.0575487000653194E+017\n Done integrating to time    32400.000000000000\n', 32400.0, [], 0);\
assert ok['correct_end'] and ok['initial_mass'] == 1.0575487000653194e17, ok;\
rest = E._verdict(' Done integrating to time    600.0\n', 600.0, [], 0, True);\
assert rest['correct_end'], rest;\
assert not E._verdict(' Done integrating to time    600.0\n', 600.0, [], 0)['correct_end'];\
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
