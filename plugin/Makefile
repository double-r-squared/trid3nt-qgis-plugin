# TRID3NT QGIS plugin -- packaging + test targets.
#
# Usage:
#   make zip       build trid3nt.zip (install-by-zip artifact, trid3nt/ root)
#   make install   unzip straight into the default QGIS profile plugin dir
#   make test      run the pure-python client tests (no QGIS required)
#   make clean     remove build artifacts
#
# The plugin package lives at THIS Makefile's own dir (plugin/); the shipped
# archive re-roots it under trid3nt/ (the installed name) via a staging copy
# and drops the co-located tests/, docs/, Makefile, README (+ caches). LICENSE
# is re-added INSIDE the plugin folder (plugins.qgis.org zip rule).

PLUGIN_NAME = trid3nt
ZIP_FILE    = $(PLUGIN_NAME).zip
STAGE       = dist/_stage
QGIS_PLUGIN_DIR = $(HOME)/.local/share/QGIS/QGIS3/profiles/default/python/plugins
# The trid3nt-local agent venv python (has the `websockets` package the test
# stub server needs). Override: make test PYTHON=/path/to/python
PYTHON ?= ../venvs/agent/bin/python

_SHIP_EXCLUDES = --exclude '/tests' --exclude '/docs' --exclude '/Makefile' \
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
	@echo "Built $(ZIP_FILE) (trid3nt/ root) -- install via QGIS: Plugins > Manage and Install Plugins > Install from ZIP"

install: zip
	mkdir -p $(QGIS_PLUGIN_DIR)
	rm -rf $(QGIS_PLUGIN_DIR)/$(PLUGIN_NAME)
	unzip -o $(ZIP_FILE) -d $(QGIS_PLUGIN_DIR)
	@echo "Installed to $(QGIS_PLUGIN_DIR)/$(PLUGIN_NAME) -- enable it in the QGIS Plugin Manager"

test:
	$(PYTHON) -m unittest discover -s tests -v

clean:
	rm -f $(ZIP_FILE)
	rm -rf $(STAGE)
	find . -name __pycache__ -type d -exec rm -rf {} +
