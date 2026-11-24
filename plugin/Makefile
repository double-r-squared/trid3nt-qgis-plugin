# TRID3NT QGIS plugin -- packaging + test targets.
#
# Usage:
#   make zip       build trid3nt.zip (install-by-zip artifact, trid3nt_qgis/ root)
#   make install   sync and migrate the default QGIS profile
#   make test      run the packages slice (this suite + contracts) from the root
#   make clean     remove build artifacts
#
# The plugin package lives at THIS Makefile's own dir (plugin/); the shipped
# archive re-roots it under trid3nt_qgis/ (the installed name) via a staging copy
# and drops the co-located docs/, Makefile, README (+ caches). LICENSE
# is re-added INSIDE the plugin folder (plugins.qgis.org zip rule).

PLUGIN_NAME = trid3nt_qgis
ZIP_FILE    = trid3nt.zip
STAGE       = dist/_stage
# The trid3nt-local agent venv python (has the `websockets` package the test
# stub server needs). Override: make test PYTHON=/path/to/python
PYTHON ?= ../venvs/agent/bin/python

_SHIP_EXCLUDES = --exclude '/docs' --exclude '/Makefile' \
                 --exclude '/README.md' --exclude '/LICENSE' --exclude '/dist' \
                 --exclude '__pycache__' --exclude '*.pyc' --exclude '.*'

.PHONY: zip install test clean

zip:
	rm -f $(ZIP_FILE)
	rm -rf $(STAGE)
	mkdir -p $(STAGE)/$(PLUGIN_NAME)
	rsync -a $(_SHIP_EXCLUDES) ./ $(STAGE)/$(PLUGIN_NAME)/
	cp LICENSE $(STAGE)/$(PLUGIN_NAME)/LICENSE
	cd $(STAGE) && zip -qr ../../$(ZIP_FILE) $(PLUGIN_NAME)
	rm -rf $(STAGE)
	@echo "Built $(ZIP_FILE) (trid3nt_qgis/ root) -- install via QGIS: Plugins > Manage and Install Plugins > Install from ZIP"

install:
	../scripts/install_plugin.sh

test:
	cd .. && $(MAKE) test-packages

clean:
	rm -f $(ZIP_FILE)
	rm -rf $(STAGE)
	find . -name __pycache__ -type d -exec rm -rf {} +
