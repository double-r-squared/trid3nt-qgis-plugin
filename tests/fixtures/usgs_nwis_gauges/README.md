# usgs_nwis_gauges

Recorded 2026-11-12 from dataretrieval (`nwis.get_iv(sites="14211720", parameterCd="00060,00065",
start="2026-11-10", end="2026-11-10", multi_index=False)` and the expanded `nwis.get_info` of the same site), each
written with pandas `to_csv` and nothing edited:

- `willamette_14211720_iv_2026-11-10.csv` - USGS 14211720 Willamette River at Portland, 288 five-minute readings.
  Its stage arrives under the site-described column `00065_morrison bridge`, not `00065`.
- `willamette_14211720_site.csv` - the site's location and its zero (1.55 ft, NGVD29).
- `etowah_bbox_2026-09-21.fgb` - what `fetch_usgs_nwis_gauges` wrote for the box [-85.240008, 33.991712, -84.727005,
  34.423822] on 2026-09-21 (the Etowah reach between 02395000 Kingston and 02395980 GA 1 Loop), copied byte for byte
  from the object-store cache on 2026-11-12: 12 sites, Richland Creek 02394682 first, Kingston sixth.
- `willamette_portland_2026-11-10.fgb` - what `fetch_usgs_nwis_gauges` wrote for the box [-122.69, 45.505, -122.65,
  45.53] on 2026-11-10, copied byte for byte from the object-store cache on 2026-11-13 (cache key
  bd147a70612de96c5105ff6e35ca50f1): one site, 14211720 Willamette River at Portland.
