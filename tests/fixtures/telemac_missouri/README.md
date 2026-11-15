# telemac_missouri

The Missouri calibration reach's finished TELEMAC-2D run 01M7FBQJGPRDVNFSNN3V4XZS43 (Omaha past Hwy 92, 7 km, 40 m,
889.149 m3/s from USGS 06610000, outflow derived), copied byte for byte from the object store on 2026-11-15, nothing
edited:

- `mesh.2dm` - the accepted mesh's display face (s3://trid3nt-cache/mesh/01M7FBQCSV7A7E0700JN3T63YG/mesh.2dm, EPSG:32615):
  902 nodes, the bed painted from the CENWO survey grid.
- `domain.cli` - the boundary file the run solved with: the prescribed-flowrate face (4 5 5) north, the prescribed-
  elevation face (5 4 4) south.
- `full_listing.txt` - the engine's listing (its full_listing.log, renamed: the repo ignores .log). The run opened at CONSTANT DEPTH 5.896 m and was still draining at its end:
  1664.684 m3/s left through the elevation face against 886.306 m3/s in at t = 1800 s.
