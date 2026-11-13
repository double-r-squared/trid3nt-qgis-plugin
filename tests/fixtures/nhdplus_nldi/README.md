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
