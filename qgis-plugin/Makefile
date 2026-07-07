# TRID3NT QGIS plugin -- packaging + test targets.
#
# Usage:
#   make zip       build trid3nt.zip (install-by-zip artifact)
#   make install   unzip straight into the default QGIS profile plugin dir
#   make test      run the pure-python client tests (no QGIS required)
#   make clean     remove build artifacts

PLUGIN_NAME = trid3nt
ZIP_FILE    = $(PLUGIN_NAME).zip
QGIS_PLUGIN_DIR = $(HOME)/.local/share/QGIS/QGIS3/profiles/default/python/plugins
# The trid3nt-local agent venv python (has the `websockets` package the test
# stub server needs). Override: make test PYTHON=/path/to/python
PYTHON ?= ../venvs/agent/bin/python

.PHONY: zip install test clean

zip:
	rm -f $(ZIP_FILE)
	zip -r $(ZIP_FILE) $(PLUGIN_NAME) \
		-x "$(PLUGIN_NAME)/__pycache__/*" -x "*.pyc"
	@echo "Built $(ZIP_FILE) -- install via QGIS: Plugins > Manage and Install Plugins > Install from ZIP"

install: zip
	mkdir -p $(QGIS_PLUGIN_DIR)
	rm -rf $(QGIS_PLUGIN_DIR)/$(PLUGIN_NAME)
	unzip -o $(ZIP_FILE) -d $(QGIS_PLUGIN_DIR)
	@echo "Installed to $(QGIS_PLUGIN_DIR)/$(PLUGIN_NAME) -- enable it in the QGIS Plugin Manager"

test:
	$(PYTHON) -m unittest discover -s tests -v

clean:
	rm -f $(ZIP_FILE)
	find . -name __pycache__ -type d -exec rm -rf {} +
