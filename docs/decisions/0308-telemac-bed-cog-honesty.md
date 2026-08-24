# 0308 - TELEMAC bed-COG manifest gap closed + publish_raster_input_cog existence check

Decision: `stage_manifest` (river_dye/do_sag deck path) now declares
`bed_bathymetry.tif` in its outputs list, matching how
`coastal_tidal_surge`/`wave_field` already declare it - the worker always
attempts the best-effort bed-COG write (`entrypoint.py` step 4b) and records
`bed_cog` in `telemac_metrics.json`, but the manifest never named the file, so
the local-docker supervisor's glob-upload skipped it and the "Input: river bed
bathymetry" context layer published from that record 404s on the client.
`mesh_only` decks omit it (the worker returns before the DEM-bed fetch, so
the file is never written).

`publish_raster_input_cog` (`trid3nt_server/emission/layer_uri_emit.py`) now
HEAD-checks the object before registering it - a `metrics.bed_cog` /
`manifest.outputs` record naming a file the store never actually received
(the dead-COG class, of which this bug is one instance) is skipped with a
loud warning instead of being published as a 404 layer. This closes the
class, not just this one file: any future in-worker COG surfaced through this
seam is checked the same way.
