# 0014 - the LLM passes handles, never URIs (accepted, to implement)

Context: tools return long object-store URIs the LLM must retype into
downstream args - the single biggest hallucination surface (~30 tokens per
reference; plausible-but-wrong copies).
Decision: the uri registry mints short per-case handles (L1, L2...) at the
emit seam; tool results show the handle, dispatch resolves handles back to
real URIs before the tool fn runs (tools unchanged). Dual-accept (handle or
verbatim-registered URI) keeps old cases working; unknown handles reject
typed. code_exec layer_refs resolve the same way.
Consequence: URI hallucination becomes structurally impossible, large token
savings, and most of the publish-discipline prompt block is deleted.
Sequenced immediately after the Layer B rename.
