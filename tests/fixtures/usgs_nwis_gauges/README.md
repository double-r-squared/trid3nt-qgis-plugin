# usgs_nwis_gauges

Recorded 2026-11-12 from dataretrieval (`nwis.get_iv(sites="14211720", parameterCd="00060,00065",
start="2026-11-10", end="2026-11-10", multi_index=False)` and the expanded `nwis.get_info` of the same site), each
written with pandas `to_csv` and nothing edited:

- `willamette_14211720_iv_2026-11-10.csv` - USGS 14211720 Willamette River at Portland, 288 five-minute readings.
  Its stage arrives under the site-described column `00065_morrison bridge`, not `00065`.
- `willamette_14211720_site.csv` - the site's location and its zero (1.55 ft, NGVD29).
