"""Multi-vertex canvas capture: the polygon / polyline half of the draw gate.

The stock tools cover a single click (``QgsMapToolEmitPoint``) and a single drag
(``QgsMapToolExtent``); a shape needs vertices. This is the smallest tool that
adds them, in the same discipline the point and extent picks already follow -
the caller saves and restores the previous canvas tool, and nothing here touches
the project or any layer.

Left click adds a vertex, right click (or double click) finishes, Backspace
removes the last one, Escape abandons. Coordinates leave in the CANVAS CRS; the
card transforms them to EPSG:4326 through the same seam the point pick uses.
"""

from __future__ import annotations

from qgis.core import QgsPointXY, QgsWkbTypes
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor

__all__ = ["VertexCaptureTool"]

_RUBBER_COLOR = QColor(220, 120, 30)
_RUBBER_FILL = QColor(220, 120, 30, 45)


class VertexCaptureTool(QgsMapTool):
    """Capture a polygon ring or a polyline, one click per vertex.

    ``captured`` carries the vertices in canvas-CRS order when the user finishes;
    ``cancelled`` fires on Escape. ``changed`` reports the running count so the
    card can enable Submit only once the shape has enough vertices to BE one.
    """

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
