# TRID3NT QGIS plugin -- package entry point.
#
# QGIS calls ``classFactory(iface)`` on plugin load. Keep this module free of
# heavy imports: the real plugin module (and everything Qt) is imported lazily
# inside the factory so a broken optional dependency cannot brick plugin
# discovery.


def classFactory(iface):  # noqa: N802 -- QGIS-mandated name
    """Load the Trid3ntPlugin class.

    :param iface: QgisInterface instance handed in by QGIS.
    """
    from .plugin import Trid3ntPlugin

    return Trid3ntPlugin(iface)
