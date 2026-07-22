# 0001 - TRID3NT is a standalone QGIS product

Context: the system began as a cloud monorepo (web SPA + cloud infra + agent),
with this repo vendoring a synced copy of the server.
Decision (2026-07-21): three products (QGIS / web / cloud) live in separate
repos. THIS repo is the QGIS product: the plugin + its local server, first-class,
no upstream sync. Deep QGIS integration over trying to be everything.
Consequence: the vendor sync seam was deleted; server code is edited here
directly (edit server/ then `make agent`); publishing on plugins.qgis.org is a
target.
