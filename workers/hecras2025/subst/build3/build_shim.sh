#!/bin/bash
set -uo pipefail
micromamba create -y -p /opt/g -c conda-forge "gdal=3.11" swig cxx-compiler make curl >/tmp/mm.log 2>&1
export PATH=/opt/g/bin:$PATH; export CONDA_PREFIX=/opt/g
GDAL_VER=$(gdal-config --version); echo "GDAL_VER=$GDAL_VER"
cd /tmp
for try in 1 2 3; do
  curl -fsSL "https://github.com/OSGeo/gdal/releases/download/v${GDAL_VER}/gdal-${GDAL_VER}.tar.gz" -o s.tgz && tar xzf s.tgz && break
  echo "download retry $try"; sleep 3
done
SRC=/tmp/gdal-${GDAL_VER}
test -f "$SRC/swig/include/gdal.i" && echo "SRC OK: $SRC" || { echo "SRC MISSING"; ls /tmp | head; exit 1; }
cd "$SRC/swig/csharp"; mkdir -p /out /tmp/cs
CF="-I/opt/g/include"; LF="-L/opt/g/lib -lgdal"
gen(){ swig -c++ -csharp -namespace "$2" -dllimport "$3" -I"${SRC}/swig/include" -I"${SRC}/swig/include/csharp" -o /tmp/${1}_wrap.cpp -outdir /tmp/cs "${SRC}/swig/include/${1}.i"; }
gen gdalconst OSGeo.GDAL gdalconst_wrap
gen gdal      OSGeo.GDAL gdal_wrap
gen osr       OSGeo.OSR  osr_wrap
gen ogr       OSGeo.OGR  ogr_wrap
cat > /tmp/aliases.cpp <<'CPP'
extern "C" {
void* CSharp_OSGeofGDAL_OpenShared___(char* p, int a);
void* CSharp_OSGeofGDAL_Open___(char* p, int a);
void* CSharp_OSGeofGDAL_OpenShared__SWIG_1___(char* p, int a){ return CSharp_OSGeofGDAL_OpenShared___(p,a); }
void* CSharp_OSGeofGDAL_Open__SWIG_1___(char* p, int a){ return CSharp_OSGeofGDAL_Open___(p,a); }
}
CPP
g++ -shared -fPIC -o /out/libgdal_wrap.so /tmp/gdal_wrap.cpp /tmp/aliases.cpp $CF $LF 2>/tmp/g.err && echo "OK gdal_wrap+shim" || { echo GPP-FAIL; tail -20 /tmp/g.err; }
g++ -shared -fPIC -o /out/libgdalconst_wrap.so /tmp/gdalconst_wrap.cpp $CF $LF 2>/dev/null && echo "OK gdalconst"
g++ -shared -fPIC -o /out/libosr_wrap.so /tmp/osr_wrap.cpp $CF $LF 2>/dev/null && echo "OK osr"
g++ -shared -fPIC -o /out/libogr_wrap.so /tmp/ogr_wrap.cpp $CF $LF 2>/dev/null && echo "OK ogr"
nm -D --defined-only /out/libgdal_wrap.so | grep -E "OpenShared__SWIG_1|Open__SWIG_1"
ls -la /out/*.so
