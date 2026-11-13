# nhdplus_nldi

What `fetch_nhdplus_nldi_navigate` wrote (NLDI flowlines tagged `nhdplus_comid`), copied byte for byte from the
object-store cache on 2026-11-13, nothing edited:

- `etowah_release_dm_2km.fgb` - seed_point (-84.978229605, 34.207328998), direction DM, distance_km 2.0 (cache key
  1c682f992cbdcbd7491b3d24792cce58).
- `etowah_release_um_24km.fgb` / `etowah_release_dm_24km.fgb` - the same seed, direction UM / DM, distance_km 24.0
  (cache keys 3ece016d2906cd18b810d91775b171bf / 700b6164841c1cca22d59766f843e9f3): the Etowah's main stem from the
  release's reach 6498944, 12 and 15 flowlines.
- `nwis_02395000_dm_0km.fgb` / `nwis_02394682_dm_0km.fgb` - nwis_site 02395000 (Etowah River near Kingston) / 02394682
  (Richland Creek at Old Dallas Rd), direction DM, distance_km 0.0 (cache keys 4c142fb2e88a6d4f920186afafc8cf7a /
  f674c26592525faae418dd003dd43ceb): the one reach NLDI's NWIS index lists each gauge on, 6498944 and 6498058.

Recorded from the NLDI API itself on 2026-11-13 (curl, the bytes as served):

- `nwissite_USGS-02395000.json` - `GET /nldi/linked-data/nwissite/USGS-02395000`: the gauge on comid 6498944.
- `linked_data_sources.json` - `GET /nldi/linked-data/`: the feature-source catalogue dataretrieval validates against.

Recorded through `fetch_nhdplus_nldi_navigate` on 2026-11-13 and copied byte for byte from the object-store cache, nothing
edited (the Willamette at Portland, all on the one 14 km flowline 23815040, digitized from its upstream end):

- `willamette_release_um_3km.fgb` / `willamette_release_dm_3km.fgb` - seed_point (-122.669784, 45.518485), the
  calibration reach's release about 120 m below USGS 14211720, direction UM / DM, distance_km 3.0 (cache keys
  988ef3eef3fe6df3358bc3777d44bd38 / a847071dce2ec006f4e2dfec0ae6b69e).
- `willamette_south_um_3km.fgb` / `willamette_south_dm_3km.fgb` - seed_point (-122.667, 45.508), about 1.1 km above
  the gauge on the same flowline, direction UM / DM, distance_km 3.0 (cache keys d0c8de3c64da16dc4fe3c068ae974e50 /
  9ffa21405b4051154741d0e2cf0b3ede).
- `nwis_14211720_dm_0km.fgb` - nwis_site 14211720 (Willamette River at Portland), direction DM, distance_km 0.0
  (cache key 39b6cbbf97ef469080cfc984cfaa37e7): NLDI's NWIS index lists it on 23815040.
- `nwis_<site>_dm_0km.fgb` for the ten other sites of the recorded Etowah gauge layer (02394670, 02394000, 02393501,
  02395120, 02388350, 02388500, 02388525, 02395996, 02395980, 02394820) - nwis_site <site>, direction DM,
  distance_km 0.0, recorded the same way on 2026-11-13: the one reach NLDI's NWIS index lists each on, none of them
  on the Etowah release's 24 km UM walk.
