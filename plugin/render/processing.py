"""A ``processing-request`` from the agent, run in THIS QGIS session.

An algorithm request runs ``processing.run`` over canvas layers named in its
params and adds every output layer to the project; a code request executes the
approved snippet in the session's Python. The response carries the outcome
honestly: an error is the session's own traceback, never a fabricated result.
"""

from __future__ import annotations

import contextlib
import io
import json
import traceback
from typing import Any, Dict, Optional, Tuple

#: The tail of a snippet's stdout and of an error that rides the response.
_TAIL_CHARS = 8000


def run_processing_request(payload: dict, iface: Any = None) -> Dict[str, Any]:
    """The ``processing-response`` wire dict for one ``processing-request``."""
    request_id = payload.get("request_id")
    kind = payload.get("kind")
    try:
        if kind == "algorithm":
            result = run_algorithm(
                str(payload.get("algorithm") or ""), dict(payload.get("params") or {})
            )
            return _response(request_id, "ok", result=result)
        if kind == "code":
            value, stdout = run_code(str(payload.get("code") or ""), iface)
            return _response(request_id, "ok", result={"value": value}, stdout=stdout)
        return _response(request_id, "error", error=f"unknown processing kind {kind!r}")
    except Exception:  # noqa: BLE001 -- the traceback IS the answer
        return _response(request_id, "error", error=_tail(traceback.format_exc()))


def _response(
    request_id: Any, status: str, result: Optional[dict] = None,
    error: Optional[str] = None, stdout: str = "",
) -> Dict[str, Any]:
    return {"request_id": request_id, "status": status, "result": result,
            "error": error, "stdout": stdout}


def run_algorithm(algorithm: str, params: dict) -> Dict[str, Any]:
    """Run one Processing algorithm; every destination left unset is a temporary
    output, and every output layer is added to the project and summarized."""
    from qgis.core import QgsApplication, QgsProcessing, QgsProject

    import processing

    registry = QgsApplication.processingRegistry()
    if not registry.algorithms():
        from processing.core.Processing import Processing

        Processing.initialize()
    alg = registry.algorithmById(algorithm)
    if alg is None:
        raise ValueError(
            f"no Processing algorithm {algorithm!r}; the id is provider:name as "
            "the Processing reference lists it (native:slope, gdal:contour, ...)"
        )
    project = QgsProject.instance()
    resolved = {
        key: _layer_by_name(project, value) if isinstance(value, str) else value
        for key, value in params.items()
    }
    destinations = [p.name() for p in alg.parameterDefinitions() if p.isDestination()]
    for name in destinations:
        resolved.setdefault(name, QgsProcessing.TEMPORARY_OUTPUT)
    outputs = processing.run(alg, resolved)
    layers = []
    for name in destinations:
        label = alg.displayName() if len(destinations) == 1 else f"{alg.displayName()} {name}"
        layer = _as_layer(project, outputs.get(name), label)
        if layer is None:
            continue
        if layer.id() not in project.mapLayers():
            project.addMapLayer(layer)
        layers.append(_summary(layer))
    values = {
        key: value for key, value in outputs.items()
        if key not in destinations and _is_json(value)
    }
    result: Dict[str, Any] = {}
    if layers:
        result.update(layers[0])
        if len(layers) > 1:
            result["layers"] = layers
    if values:
        result["outputs"] = values
    if not result:
        raise ValueError(f"{algorithm} produced no layer and no value; outputs={sorted(outputs)}")
    return result


def run_code(code: str, iface: Any = None) -> Tuple[Any, str]:
    """Execute the approved snippet in the session's Python: the ``qgis.core``
    names, ``iface`` and ``processing`` are in scope; ``result`` is the answer."""
    import qgis.core as qgis_core

    namespace: Dict[str, Any] = {"__name__": "__trid3nt_run_pyqgis__", "iface": iface}
    namespace.update({k: v for k, v in vars(qgis_core).items() if k.startswith("Qgs")})
    try:
        import processing

        namespace["processing"] = processing
    except ImportError:
        pass
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exec(compile(code, "<run_pyqgis>", "exec"), namespace)  # noqa: S102 -- the user approved this code
    return _jsonable(namespace.get("result")), _tail(buffer.getvalue())


def _layer_by_name(project: Any, value: str) -> Any:
    """A canvas layer by its name; any other string passes through as the value."""
    layers = project.mapLayersByName(value)
    return layers[0] if layers else value


def _as_layer(project: Any, output: Any, name: str) -> Any:
    from qgis.core import QgsMapLayer, QgsRasterLayer, QgsVectorLayer

    if isinstance(output, QgsMapLayer):
        return output if output.isValid() else None
    if not isinstance(output, str) or not output:
        return None
    known = project.mapLayer(output)
    if known is not None:
        return known
    raster = QgsRasterLayer(output, name)
    if raster.isValid():
        return raster
    vector = QgsVectorLayer(output, name, "ogr")
    return vector if vector.isValid() else None


def _summary(layer: Any) -> Dict[str, Any]:
    from qgis.core import QgsMapLayer, QgsWkbTypes

    extent = layer.extent()
    out: Dict[str, Any] = {
        "layer_name": layer.name(),
        "layer_id": layer.id(),
        "crs": layer.crs().authid(),
        "extent": [extent.xMinimum(), extent.yMinimum(), extent.xMaximum(), extent.yMaximum()],
        "source": layer.source(),
    }
    if layer.type() == QgsMapLayer.RasterLayer:
        out["kind"] = "raster"
        out["band_count"] = layer.bandCount()
        out["width"] = layer.width()
        out["height"] = layer.height()
    elif layer.type() == QgsMapLayer.VectorLayer:
        out["kind"] = "vector"
        out["feature_count"] = layer.featureCount()
        out["geometry_type"] = QgsWkbTypes.displayString(layer.wkbType())
    else:
        out["kind"] = "mesh"
    return out


def _is_json(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def _jsonable(value: Any) -> Any:
    if _is_json(value):
        return value
    if hasattr(value, "name") and hasattr(value, "extent"):
        return _summary(value)
    return str(value)


def _tail(text: str) -> str:
    if len(text) <= _TAIL_CHARS:
        return text
    keep = _TAIL_CHARS - 40
    return f"...[{len(text) - keep} chars truncated]...\n" + text[-keep:]
