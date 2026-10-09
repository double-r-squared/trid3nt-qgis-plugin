"""Qt harness for the Library panel.

Run as a SUBPROCESS by its wrapper, which needs ``qgis.PyQt`` and skips honestly
when absent. Offscreen, no agent, no network: the panel is handed parsed results
exactly as the dock's tasks hand them in. Covers the tree, the entry list, the
detail pane, one-click copy, search and browse, and the honest failure line."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402
from qgis.PyQt.QtGui import QGuiApplication  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication  # noqa: E402

QCoreApplication.setOrganizationName("trid3nt-library-harness")
QCoreApplication.setApplicationName("trid3nt-library-harness")

app = QApplication([])

from library_bodies import LIBRARY_BODY, SEARCH_BODY  # noqa: E402
from plugin.net.trid3nt_client import (  # noqa: E402
    parse_library,
    parse_library_hits,
)
from plugin.ui.library_window import LibraryWindow  # noqa: E402

window = LibraryWindow()
window.set_library(parse_library(LIBRARY_BODY))

tree = window.group_tree
assert tree.topLevelItemCount() == 2, "Tools and Data are the two roots"
assert tree.topLevelItem(0).text(0) == "Tools"
assert tree.topLevelItem(1).text(0) == "Data"
assert [tree.topLevelItem(0).child(i).text(0) for i in range(2)] == [
    "fetchers (2)", "derives (1)"
], "subsystems keep the daemon's order and carry their counts"
data_class = tree.topLevelItem(1).child(0)
assert data_class.text(0) == "elevation"
assert data_class.child(0).text(0) == "raster (2)"
print("[tree] subsystems and data classes painted")

# The first subsystem is selected on load, and the detail pane follows the list.
assert [i.name for i in window.shown_items] == ["fetch_dem", "fetch_landcover"]
assert window.name_label.text() == "fetch_dem"
assert "elevation" in window.description_label.text()
assert "source_class" in window.facts_label.text()
assert window.copy_btn.isEnabled()
window.entry_list.setCurrentRow(1)
assert window.name_label.text() == "fetch_landcover"
print("[browse] the middle list drives the detail pane")

# A data class shows every row under it; a kind shows only its own.
tree.setCurrentItem(data_class)
assert [i.name for i in window.shown_items] == ["3dep", "copernicus_dem"]
assert window.name_label.text() == "3dep"
facts = window.facts_label.text()
assert "fetcher" in facts and "fetch_dem" in facts, facts
assert "resolution_m" in facts
tree.setCurrentItem(data_class.child(0))
assert [i.name for i in window.shown_items] == ["3dep", "copernicus_dem"]
print("[data] rows sit under their class and kind, the fetcher beside each")

# Copy is ONE click and puts the exact name on the clipboard.
window.entry_list.setCurrentRow(1)
window.copy_btn.click()
clipboard = QGuiApplication.clipboard()
assert clipboard.text() == "copernicus_dem", clipboard.text()
assert "copernicus_dem" in window.status_label.text()
print("[copy] one click puts the name on the clipboard")

# Search asks for the query and paints the ranked hits in place.
asked = []
window.searchRequested.connect(asked.append)
window.search_edit.setText("  where is the water  ")
window.search_btn.click()
assert asked == ["where is the water"], asked
window.set_hits("where is the water", parse_library_hits(SEARCH_BODY))
assert [i.name for i in window.shown_items] == ["fetch_dem", "3dep"]
assert "score" in window.facts_label.text()
assert "2 hit(s)" in window.status_label.text(), window.status_label.text()
print("[search] the ranked hits replace the entry list")

# Browse returns to the selected group without re-fetching.
window.browse_btn.click()
assert [i.name for i in window.shown_items] == ["3dep", "copernicus_dem"]
# An empty box browses rather than searching an empty query.
asked.clear()
window.search_edit.setText("   ")
window.search_btn.click()
assert asked == [], asked
print("[browse] search returns to the listing, an empty box searches nothing")

# A failure is an honest line, never an empty list pretending to be a result.
window.set_status("agent HTTP API unreachable at http://127.0.0.1:1")
assert "unreachable" in window.status_label.text()
assert [i.name for i in window.shown_items] == ["3dep", "copernicus_dem"]

# An empty listing leaves the detail pane blank and Copy disabled.
window.set_library(parse_library({}))
assert window.group_tree.topLevelItemCount() == 2
assert window.shown_items == []
assert window.name_label.text() == ""
assert not window.copy_btn.isEnabled()
print("[honesty] a fault states itself; an empty listing disables Copy")

print("LIBRARY-OK")
sys.exit(0)
