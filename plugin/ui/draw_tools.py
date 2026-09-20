"""Canvas drawing: the multi-vertex capture tool, the map-tool borrow, and the
bbox overlay every drawn or remembered extent is painted with.

Left click adds a vertex, right or double click finishes, Backspace removes one,
Escape abandons. Nothing here touches the project, and coordinates leave in the
CANVAS CRS."""

from __future__ import annotations

from qgis.core import QgsPointXY, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

__all__ = ["VertexCaptureTool", "borrow_map_tool", "paint_bbox_overlay",
           "clear_bbox_overlay"]


def borrow_map_tool(canvas, tool, checked: bool, previous):
    """Install ``tool`` while ``checked`` and hand back the tool it displaced,
    which the caller keeps and passes here again to restore. OFF restores only
    while the borrowed tool is still active, so a tool the user picked by hand
    on the canvas is never yanked out from under them."""
    if checked:
        previous = canvas.mapTool()
        canvas.setMapTool(tool)
        return previous
    if canvas.mapTool() is tool:
        canvas.setMapTool(previous)
    return None


def paint_bbox_overlay(canvas, band, bbox4326, color: str, *,
                       dotted: bool = False):
    """Paint one EPSG:4326 box as an OUTLINE-ONLY rubber band in the canvas CRS,
    building the band on first use and returning it. Cosmetic: any failure
    leaves the canvas as it was and hands the band back unchanged."""
    try:
        from qgis.core import QgsGeometry, QgsRectangle

        from ..render.layers import reproject

        lon_min, lat_min, lon_max, lat_max = bbox4326
        rect = reproject(QgsRectangle(lon_min, lat_min, lon_max, lat_max),
                         "EPSG:4326", canvas.mapSettings().destinationCrs())
        if rect is None:
            return band
        if band is None:
            band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
            band.setColor(QColor(color))
            band.setWidth(2)
            # Outline-only: a fully transparent fill leaves just the ring.
            band.setFillColor(QColor(0, 0, 0, 0))
            if dotted:
                try:
                    band.setLineStyle(Qt.PenStyle.DotLine)
                except Exception:  # noqa: BLE001 -- older builds lack it
                    pass
        band.setToGeometry(QgsGeometry.fromRect(rect), None)
        band.show()
    except Exception:  # noqa: BLE001 -- overlay is cosmetic; never crash
        pass
    return band


def clear_bbox_overlay(band) -> None:
    """Empty and hide an overlay band. A no-op on None and on a dead canvas."""
    if band is None:
        return
    try:
        band.reset(QgsWkbTypes.PolygonGeometry)
        band.hide()
    except Exception:  # noqa: BLE001 -- best-effort teardown
        pass

_RUBBER_COLOR = QColor(220, 120, 30)
_RUBBER_FILL = QColor(220, 120, 30, 45)


class VertexCaptureTool(QgsMapTool):
    """Capture a polygon ring or a polyline, one click per vertex. ``changed``
    reports the running COUNT, so a card can enable Submit only once the shape
    has enough vertices to be one."""

    captured = pyqtSignal(list)
    cancelled = pyqtSignal()
    changed = pyqtSignal(int)

    def __init__(self, canvas, draw_kind: str = "polygon"):
        super().__init__(canvas)
        self._canvas = canvas
        self._polygon = draw_kind != "polyline"
        self._points: list = []
        geometry = (QgsWkbTypes.PolygonGeometry if self._polygon
                    else QgsWkbTypes.LineGeometry)
        self._band = QgsRubberBand(canvas, geometry)
        self._band.setColor(_RUBBER_COLOR)
        self._band.setFillColor(_RUBBER_FILL)
        self._band.setWidth(2)

    # -- capture ----------------------------------------------------------- #

    def canvasReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.RightButton:
            self._finish()
            return
        self._points.append(QgsPointXY(self.toMapCoordinates(event.pos())))
        self._redraw()
        self.changed.emit(len(self._points))

    def canvasDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt override
        # The release that precedes the double click already added a vertex, so
        # finishing here would keep a duplicate of the last one.
        if self._points:
            self._points.pop()
        self._finish()

    def canvasMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self._points:
            return
        self._redraw(hover=QgsPointXY(self.toMapCoordinates(event.pos())))

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self.reset()
            self.cancelled.emit()
        elif event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            if self._points:
                self._points.pop()
                self._redraw()
                self.changed.emit(len(self._points))

    # -- state ------------------------------------------------------------- #

    def vertices(self) -> list:
        return list(self._points)

    def reset(self) -> None:
        """Drop the shape and its overlay. Safe to call more than once."""
        self._points = []
        self._band.reset(QgsWkbTypes.PolygonGeometry if self._polygon
                         else QgsWkbTypes.LineGeometry)

    def deactivate(self) -> None:
        # The overlay belongs to the pick, not to the canvas: leaving it behind
        # would draw a shape over a map the user has moved on from.
        self._band.reset(QgsWkbTypes.PolygonGeometry if self._polygon
                         else QgsWkbTypes.LineGeometry)
        super().deactivate()

    # -- internals --------------------------------------------------------- #

    def _finish(self) -> None:
        if not self._points:
            self.cancelled.emit()
            return
        self.captured.emit(list(self._points))

    def _redraw(self, hover=None) -> None:
        self._band.reset(QgsWkbTypes.PolygonGeometry if self._polygon
                         else QgsWkbTypes.LineGeometry)
        for point in self._points:
            self._band.addPoint(point, False)
        if hover is not None:
            self._band.addPoint(hover, False)
        self._band.updatePosition()
        self._band.show()
