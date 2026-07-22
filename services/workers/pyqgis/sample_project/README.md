# `services/workers/pyqgis/sample_project/` — canonical M2 sample QGIS project

**Owner:** `engine` specialist (SRS v0.3 FR-QS-2, FR-QS-5, FR-MP-1, Decision B).

Hosts the canonical sample `.qgs` consumed by the live cloud substrate:

| Artifact | Path | Purpose |
|---|---|---|
| `grace2-sample.qgs` | this directory + `gs://grace-2-hazard-prod-qgs/grace2-sample.qgs` | The single canonical `.qgs` for sprint-04. QGIS Server reads it via `/vsigs/`; the PyQGIS worker (job-0020) round-trips it. |
| `build_sample_project.py` | this directory | Reproducible authoring script. Run inside the `grace2` conda env. Overwrites `grace2-sample.qgs` in place. |
| `styles/basemap.qml` | repo root, not this dir | QML preset stub matching the `basemap-osm-conus` layer name. Baked into the QGIS Server container image by `infra/qgis-server/Dockerfile` (job-0018 mechanism). |

## Project provenance

| Field | Value |
|---|---|
| CRS | EPSG:4326 (WGS84 geographic) |
| WMS-advertised extent | `-125, 24, -66, 50` (CONUS) |
| Advertised CRS list | `EPSG:4326`, `EPSG:3857` |
| Layer count | 1 |
| Layer name | `basemap-osm-conus` |
| Layer provider | `wms` (QGIS XYZ tile provider) |
| Layer source | `https://tile.openstreetmap.org/{z}/{x}/{y}.png` (XYZ template, zoom 0–19) |
| Layer native CRS | EPSG:3857 (Web Mercator, OSM tile pyramid) |

## Authoring path

**PyQGIS scripting in the `grace2` conda env**, not QGIS Desktop GUI. The
authoring script (`build_sample_project.py`) initializes a headless
`QgsApplication`, composes the project via `QgsProject` / `QgsRasterLayer`,
sets WMS metadata via `project.writeEntry`, and writes via `QgsProject.write`.

Rationale: reproducibility. No GUI session state, no manual click trail; the
script is the provenance, the `.qgs` is the snapshot. Re-running yields a
bit-stable file modulo QGIS' internal save timestamp.

This is consistent with the *engine* discipline (`agents/engine.md`,
"PyQGIS-worker is the only `.qgs` writer"): this job AUTHORS the initial `.qgs`
once via a deterministic PyQGIS script and uploads it as the starting state;
all subsequent mutations (e.g. adding layers, applying presets, setting
temporal config) go through the PyQGIS worker round-trip (job-0020).

## Regeneration

From the repo root:

```bash
conda activate grace2
python services/workers/pyqgis/sample_project/build_sample_project.py
```

To re-upload to GCS after regeneration:

```bash
gcloud storage cp \
  services/workers/pyqgis/sample_project/grace2-sample.qgs \
  gs://grace-2-hazard-prod-qgs/grace2-sample.qgs \
  --project=grace-2-hazard-prod
```

## QGIS Server consumption (live verification recipe)

```bash
BASE=https://grace-2-qgis-server-425352658356.us-central1.run.app
MAP=/vsigs/grace-2-hazard-prod-qgs/grace2-sample.qgs

# GetCapabilities — should list <Name>basemap-osm-conus</Name>
curl -s "${BASE}/ogc/wms?MAP=${MAP}&SERVICE=WMS&REQUEST=GetCapabilities" | head -50

# GetMap — should return a non-blank PNG covering CONUS
curl -s "${BASE}/ogc/wms?MAP=${MAP}&SERVICE=WMS&REQUEST=GetMap&VERSION=1.3.0&LAYERS=basemap-osm-conus&CRS=EPSG:4326&BBOX=24,-125,50,-66&WIDTH=800&HEIGHT=400&FORMAT=image/png&STYLES=" \
  -o /tmp/wms-sample.png && file /tmp/wms-sample.png
```

## QML preset bake-into-image flow (follow-up)

`styles/basemap.qml` is content-authored here (this job). The QGIS Server
container is baked with `COPY styles/ /opt/styles/` in
`infra/qgis-server/Dockerfile` (job-0018 mechanism). For the QML to take
effect in the deployed QGIS Server, the image must be rebuilt and the Cloud
Run service redeployed:

```bash
make qgis-server-build
make qgis-server-push
make qgis-server-deploy
```

**Status for M2:** the QML file is authored in source. The bake-and-redeploy
trigger is deferred to a follow-up infra task — see Open Question OQ-19C in
the job-0019 report. At M2 the preset content does NOT affect the WMS render
of `basemap-osm-conus` (PyQGIS worker `apply_style_preset` is the job-0020
codepath); the file's presence in source + the layer-name match is what this
job delivers.

## Consumers

- **job-0020** (engine): PyQGIS worker reads this `.qgs` via `/vsigs/`,
  mutates it (appends a second styled layer), writes it back to GCS,
  publishes a Pub/Sub notify.
- **job-0023** (testing): M2 acceptance — WMS GetCapabilities + GetMap PNG
  round-trip; worker-round-trip transcript; M1 regression rerun (114 tests).
