# 0008 - discovery is the front door; fetchers are adapters

Decision: for open-ended data needs the order is: named tool -> our-tools
retrieval (discover_dataset) -> external-source catalog (catalog_search /
catalog_fetch) -> offer-to-add (user-gated catalog growth). Hand-written
fetchers exist only where generic access cannot work: bespoke APIs (auth,
station ids, variable codes) or semantic shaping (computed answers, not
files). Catalogued, self-describing sources need no code at all.
