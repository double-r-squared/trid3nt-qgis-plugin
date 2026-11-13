# etopo

What `fetch_etopo` wrote (NOAA ETOPO 2022 15 arc-second surface, EGM2008, on the box's own UTM grid), copied byte
for byte from the object-store cache on 2026-11-12, nothing edited:

- `etowah_bbox.tif` - the box [-85.240008, 33.991712, -84.727005, 34.423822] around the Etowah River between Kingston
  and Rome, GA (cache key a4d9e12fc34a50983b2391ffb67e6b00): 14,995 cells, every one above its zero.
- `washington_coast_bbox.tif` - a box on the Pacific shore at about (-124.17, 47.13), Washington (cache key
  a03f741d23d9ab591295f34ef4ec7139): 25 cells at or below its zero, 227 above.
