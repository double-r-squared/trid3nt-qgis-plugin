# 0004 - TRID3NT everywhere, zero legacy names

Decision (2026-07-21/22): the product name is TRID3NT across all versions.
Zero literal mentions of the pre-rebrand name anywhere - no history-note
exceptions (inclusion only by explicit ask). Layer A (prose/docstrings/UA
strings/fixtures) is done; Layer B renames the identifiers themselves
(packages, env vars, logger namespaces, persistence dir with data migration).
Consequence: env reads are ATOMIC (code reads only the new TRID3NT_* names;
.env files flip in the same change). The one compatibility seam is the
container-env boundary: the local-docker specs (geoclaw/swan) dual-EMIT the
legacy env names alongside the new ones so already-built worker images keep
working until rebuilt from this tree.
Exclusions (external names, not this repo's identifiers): cloud/infra
resource names (S3 buckets, DynamoDB tables/prefix value, Batch job-defs/
queues, ECR repos, the /opt mount + in-container scratch paths baked into
already-built worker images), the dev-machine conda env name, the headless
QGIS docker image tag, the cloud web SPA's storage keys/test-ids quoted by
live-drive tests and docs, the cloud repo's own name in the sync-script
reference, and frozen report artifacts' historical error text.
