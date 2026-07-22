# 0009 - simulations own their inputs

Decision: scenario tools fetch their own inputs through typed fallback
chains (e.g. 3DEP 1m -> 10m -> typed error) encoding physics requirements.
Discovery NEVER routes sim inputs - text relevance cannot judge resolution/
CRS/coverage. User-supplied overrides are allowed but validated against the
sim's input contract; on failure the sim errors typed rather than silently
degrading.
