# Design decisions

One short note per decision that shapes this repo. Convention: a few lines of
context, the decision, the consequence. Add a new numbered file when a decision
lands; never rewrite history - supersede with a new note that links back.

- [0001 - TRID3NT is a standalone QGIS product](0001-standalone-qgis-product.md)
- [0002 - one daemon, one user, one process](0002-monolith-until-multiuser.md)
- [0003 - 99% coverage with 10x less beats 100% with 10x more](0003-simplicity-over-completeness.md)
- [0004 - TRID3NT everywhere, zero legacy names](0004-zero-legacy-naming.md)
- [0005 - QGIS reads COGs natively; no tile server](0005-qgis-native-rendering.md)
- [0006 - local-only server; cloud code lives elsewhere](0006-local-only-cloud-strip.md)
- [0007 - secrets in a local file vault](0007-file-vault-secrets.md)
- [0008 - discovery is the front door; fetchers are adapters](0008-discovery-vs-fetchers.md)
- [0009 - simulations own their inputs](0009-sim-owned-typed-inputs.md)
- [0010 - analysis is composed, not enumerated](0010-analysis-playground.md)
- [0011 - code execution is user-gated and honestly timed out](0011-code-exec-approval-gate.md)
- [0012 - fix typos in retrieval, never in the prompt](0012-typo-tolerant-retrieval.md)
- [0013 - rules graduate from prompt to code](0013-prompt-to-code-enforcement.md)
- [0014 - the LLM passes handles, never URIs (accepted, to implement)](0014-layer-handles-not-uris.md)
- [0015 - server/wheels holds PyPI-absent deps](0015-vendored-wheel.md)
- [0016 - server uses the src/ layout](0016-src-layout.md)
- [0018 - auto and ask modes (accepted, picker to implement)](0018-auto-ask-modes.md)
- [0019 - search wins at scale; enumerate only what's small and hot](0019-on-demand-capability-search.md)
