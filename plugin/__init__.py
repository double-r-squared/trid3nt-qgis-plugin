# Package entry point. Keep heavy imports out of this module: they happen
# lazily inside the factory so a broken optional dependency cannot brick discovery.


def classFactory(iface):  # noqa: N802 -- QGIS-mandated name
    """Load the Trid3ntPlugin class."""
    from .plugin import Trid3ntPlugin

    return Trid3ntPlugin(iface)
