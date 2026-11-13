# nhd_water_surface

What `fetch_nhd_water_surface` wrote (NHDPlus HR NHDArea, StreamRiver and CanalDitch, `ftype` kept), copied byte for
byte from the object-store cache on 2026-11-13, nothing edited:

- `etowah_release_2km.fgb` - seed (-84.978229605, 34.207328998), the Etowah release 224 m above USGS 02395000,
  radius_km 2.0 (cache key fa333c8bcb9f4a15c4e2eae202d07a21): one StreamRiver polygon (3.717 km2) holding the seed.
- `allatoona_lake_3km.fgb` - seed (-84.72, 34.166) on Allatoona Lake (NHDWaterbody 390 LakePond), radius_km 3.0
  (cache key 1b2ff27d6707e4c63b0ccf3f567c7e36): the same Etowah StreamRiver polygon, 1.03 km from the seed.
- `willamette_portland_1km.fgb` - seed (-122.669784, 45.518485), the Willamette at Portland, radius_km 1.0 (cache
  key 5efb3f52ed2cc732131ce6a4e74e85f6): one StreamRiver polygon (26.99 km2) holding the seed.
