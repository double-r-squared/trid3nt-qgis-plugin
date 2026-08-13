#!/bin/bash
set -euo pipefail
micromamba create -y -p /opt/g -c conda-forge "gdal=3.11" swig cxx-compiler make curl >/tmp/mm.log 2>&1
export PATH=/opt/g/bin:$PATH
export CONDA_PREFIX=/opt/g
GDAL_VER=$(gdal-config --version)
echo "== conda GDAL: $GDAL_VER  swig: $(swig -version | grep -i version)"
cd /tmp
curl -fsSL "https://github.com/OSGeo/gdal/releases/download/v${GDAL_VER}/gdal-${GDAL_VER}.tar.gz" -o gdal-src.tar.gz
tar xzf gdal-src.tar.gz
SRC=/tmp/gdal-${GDAL_VER}
cd "$SRC/swig/csharp"
mkdir -p /out
CF="-I/opt/g/include $(gdal-config --cflags 2>/dev/null || true)"
LF="-L/opt/g/lib -lgdal"
for mod in gdalconst gdal osr ogr; do
  case $mod in
    gdal) ns=OSGeo.GDAL; dll=gdal_wrap;; gdalconst) ns=OSGeo.GDAL; dll=gdalconst_wrap;;
    osr) ns=OSGeo.OSR; dll=osr_wrap;; ogr) ns=OSGeo.OGR; dll=ogr_wrap;;
  esac
  mkdir -p /tmp/cs_${mod}
  echo "== swig $mod =="
  swig -c++ -csharp -namespace "$ns" -dllimport "$dll" -I"${SRC}/swig/include" -I"${SRC}/swig/include/csharp" \
       -o ${mod}_wrap.cpp -outdir /tmp/cs_${mod} "../include/${mod}.i" 2>/tmp/swig_${mod}.err || { echo "SWIG FAIL $mod"; tail -15 /tmp/swig_${mod}.err; continue; }
  g++ -shared -fPIC -o /out/lib${dll}.so ${mod}_wrap.cpp $CF $LF 2>/tmp/gpp_${mod}.err || { echo "GPP FAIL $mod"; tail -25 /tmp/gpp_${mod}.err; continue; }
  echo "OK lib${dll}.so ($(stat -c%s /out/lib${dll}.so) bytes)"
done
echo "GDAL_VER_BUILT=$GDAL_VER" > /out/VERSION.txt
ls -la /out/
echo "== exported CSharp_OSGeof symbol coverage vs required =="
for so in /out/*.so; do nm -D --defined-only $so 2>/dev/null; done | grep -oE "CSharp_OSGeof[A-Za-z0-9_]+" | sort -u > /out/have.txt
echo "built exports: $(wc -l < /out/have.txt)"
