# True colour from GOES ABI

ABI has no green band, so a true-colour image is SYNTHESISED. The sequence below
is primitives the session already has; nothing here needs a tool.

1. FETCH the three reflective bands over the window: band 1 (blue, 0.47 um),
   band 2 (red, 0.64 um), band 3 (veggie, 0.86 um).
2. RASTER CALCULATOR, per band, for the Rayleigh correction. The optical depth is
   tau = 0.008569 * lam^-4 * (1 + 0.0113 * lam^-2 + 0.00013 * lam^-4), which gives
   tau = {band 1: 0.1850, band 2: 0.0525, band 3: 0.0159}. Apply it on the SLANT
   path: the view zenith comes from the geostationary orbit radius 42164.0 km over
   the earth radius 6378.137 km, and the solar cosine is floored at 0.05 so a
   terminator pixel does not divide by nothing.
3. RASTER CALCULATOR for the synthetic green:
   green = 0.45 * red + 0.10 * veggie + 0.45 * blue.
4. RASTER CALCULATOR for the display gamma: value^(1/2.2), per band.
5. BUILD VIRTUAL RASTER over the three corrected bands, separate=yes, in the order
   red, green, blue. QGIS paints a three-band raster as RGB with its default
   multiband renderer, so nothing styles it.

The composite is a PICTURE, not a measurement: it carries no physical units and
nothing downstream should sample it.
