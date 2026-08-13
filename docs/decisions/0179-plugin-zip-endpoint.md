# ADR 0179 - QGIS plugin zip: on-demand fresh-build endpoint

Date: 2026-08-07
Status: accepted

## Context

NATE reported: QGIS Plugin Manager's "Install from ZIP" needs a real zip, but
clicking the version in Plugin Manager on his macOS box yielded an
uncompressed download.

Investigation of the daemon-hosted repo (`plugin_repo.py` +
`tool_catalog_http.py`'s `/plugin-repo/*` routes):

- The CURRENT code path (`package_plugin_repo` at deploy time -> `GET
  /plugin-repo/plugins.xml` + `GET /plugin-repo/<versioned-zip>` at serve
  time) already produces a correct artifact: verified live against the
  running daemon (`curl` + `zipfile.testzip()` + `unzip -l`) -- single
  top-level `trid3nt/` dir, `Content-Type: application/zip`,
  `Content-Disposition: attachment`, correct `Content-Length`, clean
  `testzip()`. Not the bug.
- BUT this path has a real fragility the module docstring already named as
  the historical failure mode: the zip is only as fresh as the last
  `package_plugin_repo()` call (`scripts/package_plugin.sh`, wired into
  `make agent`). A deploy that skips that step, or a served directory left
  over from before a code change, silently serves a stale artifact.
- A second, MUCH more likely culprit was found running live on this box: a
  leftover `python3 -m http.server 8767` process (background shell from
  2026-08-03) serving `~/.local/share/trid3nt/plugin-repo/` -- the ORPHANED
  static-file repo from the pre-ADR-0103 `/plugins/` mechanism (ADR 0103:
  "daemon /plugins/ replaced by metadata-driven /plugin-repo/"). No code in
  this checkout references that directory anymore. Its `plugins.xml` carries
  a hardcoded `http://100.92.163.46:8767/...` download_url -- literally the
  "stopgap static server" bug `plugin_repo.py`'s own module docstring warns
  about (a hardcoded host that breaks when the client dials a different
  one). That server also sends no `Content-Disposition` header. If NATE's
  QGIS "Add repository" entry (or a browser click on a rendered
  `plugins.xml`) is still pointed at that stale :8767 repo, a Safari-style
  auto-decompress-on-download (client-side, "open safe files after
  downloading") would produce exactly the symptom reported, independent of
  anything the daemon serves on :8766.

## Decision

Two changes, orthogonal to which of the above was the actual trigger:

1. **New fixed-path endpoint `GET /plugin-repo/trid3nt.zip`** (the daemon's
   OWN :8766, nothing to do with the orphaned :8767 process) --
   `plugin_repo.build_fresh_zip()` builds straight from
   `qgis-plugin/trid3nt/` on the daemon's own checkout, in memory, on every
   request, with an mtime-based cache (`_source_signature`: cheap
   `(relpath, size, mtime_ns)` stat walk, no file reads, no prior
   `package_plugin_repo()` deploy step required). This makes "stale packaged
   artifact" structurally impossible for the endpoint plugins.xml now
   advertises -- the deploy-time PACKAGE path (`package_plugin_repo`,
   `served_zip_path`, the versioned `trid3nt-<version>.zip`) is kept as a
   manual-QA/fallback artifact only.
2. **Build-time provenance stamp.** The fresh zip carries
   `trid3nt/installed_version.txt` (git short-sha + branch, two lines) --
   the SAME format `scripts/install_plugin.sh` writes into an rsync-dev-
   installed profile. Previously only a dev-rsync install got this; a
   zip-installed user had zero eyeball-able provenance (the deploy-time
   PACKAGE zip deliberately excludes the file, since it's meaningless before
   install). The fresh-build path now stamps it fresh at build time instead.
3. **`plugins.xml`'s `download_url`** now points at
   `/plugin-repo/trid3nt.zip?v=<version>` (fixed path, `?v=` cache-busting
   hint -- server does not read or validate it, always serves current
   content).
4. Headers unchanged from the already-correct existing route:
   `Content-Type: application/zip`,
   `Content-Disposition: attachment; filename="trid3nt-<version>.zip"`,
   correct `Content-Length`.

Not fixed by this ADR (flagged for NATE, not touched): the orphaned
`:8767` `python3 -m http.server` process and
`~/.local/share/trid3nt/plugin-repo/` directory. Neither is referenced by
any code in this checkout; recommend NATE (a) check QGIS Plugin Manager's
repository list on the Mac and remove/replace any entry pointing at
`:8767` with the daemon's real `http://<tailnet-host>:8766/plugin-repo/plugins.xml`,
and (b) kill that stray process and delete that directory as dead ADR-0103
leftovers. Left alone here since it is a live, unrelated background process
this job did not spawn.

## Consequences

- The zip Plugin Manager / Install-from-ZIP downloads is always built from
  today's `qgis-plugin/trid3nt/` source, with no separate packaging step in
  the loop, at the cost of a small in-memory rebuild (mtime-cached, so
  effectively free on repeat requests with no source changes).
- A zip install now carries the same git-provenance visibility a dev-rsync
  install always had.
- Safari's client-side "open safe files after downloading" auto-unzip
  behavior (if NATE fetches `download_url` directly via a browser rather
  than through QGIS's own Plugin Manager downloader) is reduced by correct
  headers but cannot be eliminated server-side -- documented in
  `plugin_repo.py`'s module docstring and this ADR's Context, not a defect
  in this endpoint.
- The old versioned `/plugin-repo/<zip>` route and `package_plugin_repo()`
  deploy step are unchanged and still work (manual-QA / fallback path); no
  script or Makefile wiring needed updating.
