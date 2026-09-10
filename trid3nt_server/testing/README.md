# `testing/` - driving the product's own protocol

A scripted client rather than a mock: a live run declares the tool, its
arguments, how each gate is answered and what the result must carry, and the
harness drives the daemon over the same WebSocket the plugin uses. The canaries
are the frozen version of that - one named invocation per template, so "same
question, same answer" is evidence rather than two hand-typed command lines.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The harness surface: `LiveRun`, `GateAnswers`, `run_live`. |
| `canaries.py` | The declared canary runs, one per template, and the packet each owes. |
| `live_run.py` | One live run declared end to end; nothing the assertions read is re-derived. |
| `proof_animations.py` | Which field a template's animation paints, declared with its mask and its reason. |
| `proof_paths.py` | Where a template's proofs and evidence live; the one place that decides those paths. |
| `ws_client.py` | The wire primitives a scripted client needs - envelopes, handshake, cases. |
