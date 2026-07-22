# services/workers/ — PyQGIS workers + SFINCS solver containers

**Owner (code inside):** `engine` specialist. **Image build/push + Cloud Run
Jobs:** `infra`.

Short-lived Cloud Run Jobs (SRS v0.3 Decision C, FR-QS-6, FR-CE-1):

- **PyQGIS workers** — read a `.qgs` from GCS, mutate it (add layers, apply QML
  presets, set temporal config via `qgis_process` / PyQGIS), write it back, and
  notify. **Nothing else mutates a `.qgs`** (Invariant 4).
- **SFINCS solver containers** — read forcing/config from GCS, run the flood
  solver (SFINCS via HydroMT), write COG output back to GCS, emit completion.
  Always scale to zero (NFR-C-2).

`engine` authors the worker/solver tool code and the Dockerfile contents;
`infra` builds and pushes the images and wires the Cloud Run Jobs and the
Cloud Workflows that drive them (including the cancellation `terminate` path,
Invariant 8).

Empty scaffold until the worker/solver jobs land in a later sprint.
