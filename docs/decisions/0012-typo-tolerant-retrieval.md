# 0012 - fix typos in retrieval, never in the prompt

Decision: the LLM always sees the user's raw text. Typo tolerance lives
only inside retrieval scoring: query tokens absent from the corpus
vocabulary gain difflib close-matches (append-never-replace, len>=4,
non-numeric, deterministic, lru-cached) at the shared index seam, so BM25
and the hashed dense channel stop being typo-blind. No small-model
rewriter: latency + a new failure mode to fix a component that was not
broken.
