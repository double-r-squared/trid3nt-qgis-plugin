"""A ``layer-request`` from the agent, opened in THIS QGIS session.

The daemon borrows the session's data providers: it hands over a provider name
and the datasource string that provider takes, and the session opens the layer.
``mode: open`` leaves it on the map as context and answers at once; ``mode:
materialise`` windows it to the asked bbox, exports it and uploads it through the
ingest route, so the daemon can land it in the store as any fetch does. The
request carries the row's own ask: an absent bbox is the whole published layer,
and a stated spacing is the grid the export is written at. An error is the
provider's own text, never a fabricated success.
"""

from __future__ import annotations

import os
import traceback
from typing import Any, Dict, Optional, Tuple

from ..case.push_layer import upload_layer_bytes
from ..net.auth_broker import AuthBroker
from .export_layer import export_to_tempfile

#: The tail of an error that rides the response.
_TAIL_CHARS = 8000

#: The providers that publish a raster. Every other provider name opens as a
#: vector layer, which is what the registry's own decode expects.
_RASTER_PROVIDERS = frozenset(
    {"wms", "wcs", "gdal", "arcgismapserver", "arcgisimageserver", "virtualraster"}
)


def run_layer_request(
    payload: dict, base_url: str = "", iface: Any = None, broker: Any = None
) -> Dict[str, Any]:
    """The ``layer-response`` wire dict for one ``layer-request``."""
    key = payload.get("key")
    try:
        provider = str(payload.get("provider") or "")
        uri = str(payload.get("uri") or "")
        name = str(payload.get("name") or "layer")
        mode = str(payload.get("mode") or "")
        bbox = _bbox(payload.get("bbox"))
        resolution_m = payload.get("resolution_m")
        credential = payload.get("credential") or None
        if credential:
            cfg_id = (broker or AuthBroker()).config_id(str(credential))
            if not cfg_id:
                return _response(key, error=_no_key(str(credential), name))
            uri = f"{uri} authcfg={cfg_id}"
        layer = open_provider_layer(provider, uri, name)
        if mode == "open":
            add_to_map(layer, bbox, iface)
            return _response(key)
        if mode == "materialise":
            return _response(
                key,
                uri=materialise(layer, bbox, key, base_url, resolution_m),
            )
        return _response(key, error=f"unknown layer mode {mode!r}")
    except Exception:  # noqa: BLE001 -- the provider's own text IS the answer
        return _response(key, error=_tail(traceback.format_exc()))


def _response(
    key: Any, uri: Optional[str] = None, error: Optional[str] = None
) -> Dict[str, Any]:
    return {"key": key, "uri": uri, "error": error}


def _no_key(credential: str, name: str) -> str:
    """The refusal for a keyed row with nothing stored under its credential.
    Opening without the key would fail in the provider with the upstream's own
    wording, which names neither the credential nor where a key is entered."""
    return (
        f"{name} is published through a keyed service and no key is stored "
        f"under {credential!r}. Open the plugin's Settings -> Keys and enter "
        f"the {credential} key there; the key stays in this QGIS session."
    )


def _bbox(value: Any) -> Optional[Tuple[float, float, float, float]]:
    """The asked window, or ``None`` for the whole published layer."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError(f"a layer request carries a four-value bbox; got {value!r}")
    return tuple(float(v) for v in value)  # type: ignore[return-value]


def open_provider_layer(provider: str, uri: str, name: str) -> Any:
    """Open ``uri`` through the named QGIS data provider. An invalid layer raises
    with the provider's own message, which is all the daemon can honestly say."""
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    if provider in _RASTER_PROVIDERS:
        layer = QgsRasterLayer(uri, name, provider)
    else:
        layer = QgsVectorLayer(uri, name, provider)
    if not layer.isValid():
        detail = layer.dataProvider().error().summary() if layer.dataProvider() else ""
        raise ValueError(
            f"the {provider} provider did not open {name}: {detail or 'invalid layer'}"
        )
    return layer


def add_to_map(
    layer: Any, bbox: Optional[Tuple[float, float, float, float]], iface: Any
) -> None:
    """Put the overlay on the map and look at what was asked about. Nothing was
    asked about in particular when the request carries no bbox, so the camera
    stays where the user left it."""
    from qgis.core import QgsProject

    QgsProject.instance().addMapLayer(layer)
    if iface is None or bbox is None:
        return
    from .layers import zoom_to_bbox4326

    zoom_to_bbox4326(iface.mapCanvas(), bbox)


def window_to_bbox(
    layer: Any, bbox: Optional[Tuple[float, float, float, float]]
) -> Any:
    """The layer restricted to the asked window: a materialised row carries the
    AOI, never the provider's whole published coverage. A row whose ask IS the
    whole published layer carries no bbox, and then there is no window to cut."""
    from qgis.core import QgsProcessing, QgsRasterLayer, QgsVectorLayer

    import processing

    if bbox is None:
        return layer
    extent = f"{bbox[0]},{bbox[2]},{bbox[1]},{bbox[3]} [EPSG:4326]"
    if isinstance(layer, QgsRasterLayer):
        clipped = processing.run("gdal:cliprasterbyextent", {
            "INPUT": layer, "PROJWIN": extent,
            "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
        })["OUTPUT"]
        return QgsRasterLayer(clipped, layer.name()) if isinstance(clipped, str) else clipped
    extracted = processing.run("native:extractbyextent", {
        "INPUT": layer, "EXTENT": extent, "CLIP": False,
        "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
    })["OUTPUT"]
    if isinstance(extracted, str):
        return QgsVectorLayer(extracted, layer.name(), "ogr")
    return extracted


def materialise(
    layer: Any,
    bbox: Optional[Tuple[float, float, float, float]],
    key: Any,
    base_url: str,
    resolution_m: Optional[float] = None,
) -> str:
    """Export the windowed layer at the asked spacing and upload it through the
    ingest route; the staged object's uri is what the daemon reads the bytes back
    from."""
    path, _kind = export_to_tempfile(window_to_bbox(layer, bbox), resolution_m)
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    finally:
        os.unlink(path)
    return upload_layer_bytes(base_url, f"{key}{os.path.splitext(path)[1]}", data)


def _tail(text: str) -> str:
    if len(text) <= _TAIL_CHARS:
        return text
    keep = _TAIL_CHARS - 40
    return f"...[{len(text) - keep} chars truncated]...\n" + text[-keep:]
