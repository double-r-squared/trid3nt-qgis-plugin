# 0032 - measured prompt-fit context compaction

Context: a local model (Ollama) silently clips a prompt that exceeds its context
window, corrupting the turn with no error. A naive compaction ladder also had a
falsy-empty-list bug (an `if working:` guard that skipped compaction when the
working set was empty-but-over-budget), and unbounded generation could hang a
turn for many minutes.

Decision: the context budget is enforced by MEASURED prompt-fit, not by trust.
- Estimate the assembled prompt's token count against the model's context window
  and compact (drop/summarize oldest turns) until it provably fits BEFORE the
  call, so a silent-clip model never receives an over-window prompt.
- The compaction ladder must handle the empty-but-over-budget case explicitly
  (do not gate the step on a non-empty working set).
- Always cap generation length (`max_tokens`) so a runaway decode cannot hang the
  turn; the abort path persists partial state in the correct order and wires the
  fabrication backstop the same as the success path.

Consequence: turns are robust to a silently-clipping local backend and to
runaway generation; compaction is deterministic and testable. Supports the
pluggable-LLM direction (cloud API or local model).
