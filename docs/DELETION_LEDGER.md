# Deletion Ledger

Every deletion candidate is REGISTERED here at decision time and stays
until DELETED (with the commit hash) - never silently dropped (NATE
2026-07-31). Rules:
1. A candidate enters with its CONDITION - the specific thing that makes
   it redundant. "Someday" is not a condition.
2. Every wave close-out checks this ledger: conditions newly met ->
   deletion executes in that wave or the next hygiene batch.
3. Status flow: QUEUED -> CONDITION-MET -> DELETED(commit). Rejected
   candidates move to the bottom with the reason (decision record).
4. Standing ratchet (hooks): a hook pattern seen twice = directive
   candidate; three times = mandatory promotion review - promoted
   directives DELETE their hooks (entries added per occurrence).

| Candidate | Scope | Condition to delete | Status | Source |
|---|---|---|---|---|
| /api/export-qgis + /file legacy routes | tool_catalog_http | NATE plugin reinstall confirms /api/case-layers | QUEUED | hygiene batch 0058 |
| Remote materialize+download hydration fallback | cases/hydrate_case_layers | remote store access ships (presigned or agent-proxied ranges) | QUEUED | 0058 amendment |
| secrets_handler file-vault + persistence secrets CRUD + server.py vault handlers (~935 LOC) | credentials/ + server.py + persistence.py | QGIS-store push seam + resolver.py ship (chop-plan wave); auth_handshake OUT of scope (session identity, not creds); credential_registry KEEPS | QUEUED | credentials-chop-plan.md |
| Legacy vault schemes (aws-ssm/gcp-sm/local-file) + GCP Secret Manager docstrings | secrets_handler | zero live use confirmed - dies with the vault slice | CONDITION-MET | chop-plan audit |
| Inert source.yaml auth: blocks (27 specs) | specs + contract | decide: wire to resolver or delete the field (TOOL_PROVIDER is the real surface) | QUEUED | chop-plan audit |
| Cloud Run Jobs submitter binding | agent/tools/meta/passthroughs.py | verify zero live use (GCP-era); delete with on-box path as the only lane | QUEUED | 2026-07-31 qgis_process inspection |
| compute_blended_composite | processing/ | QGIS-native per-layer blend modes verified to cover the product need | QUEUED | 0057 conflict (3) |
| fetch_copernicus_dem ambient declaration | declarable pool | wave 11 item 0 (absorption into fetch_dem; internal seam stays) | CONDITION-MET (wave-11 ADR 0059: tier="internal" -- declaration removed from the declarable pool AND search index; the spec/seam is retained + registry-resolvable by design, so this is a declaration removal, not a py deletion; awaiting commit) | NATE 2026-07-31 |
| TRID3NT_CATALOG_ARM flag scaffolding (arms 1-3) | _router/stratified.py + flags | capable-model re-run decides: rollout -> baseline per-source declarations die instead; no-rollout decision -> scaffolding dies | QUEUED | ADR 0050/0055-era |
| 14+ per-source ambient declarations | declarable pool | Design-3-class arm ADVANCES on a capable model | QUEUED | pools architecture |
| grace2_* identifiers | repo-wide | Layer B dual-read rename executes | QUEUED | rebrand scope |
| env-var credential paths (as co-equal) | credentials resolution | QGIS store + broker ship; env demotes to last-resort (NOT deleted - demoted) | QUEUED | chop-plan direction |
| Job-numbered test filenames + ~166 test-comment markers | server/tests | next test-hygiene wave (no functional condition - scheduling only) | QUEUED | deferred hygiene debt |
| Deferred station-sibling twins (asos/raws/snotel/airnow/openaq) | fetchers | their ingestion modes get built (fold, not bare delete) | QUEUED | wave-4 deferrals |
| gzip/vsizip native-GDAL collapse | _router modes | REJECTED - empirically refuted (36MB for 8x5px window) | REJECTED 2026-07-31 | gdal-collapse verdict |
| jrc colormap-ramp DSL | would-be mode | REJECTED - one-consumer DSL fails generalization bar | REJECTED 2026-07-31 | ADR 0047/0053 |
| project.qgz generation in case hydration | open_case_in_qgis | none - dead by module's own docstring | DELETED (ea36191) | NATE 2026-07-31 |
| meta/probe_point.py deregistered route-server | agent/tools/meta | relocate to cases/ (same posture as hydration relocation) | QUEUED | 0058 open issue |
| _strip_query/_unwrap_tile_template platform import from agent tools | cases/ vs agent | hoist to a shared agent URI util | QUEUED | 0058 open issue |
