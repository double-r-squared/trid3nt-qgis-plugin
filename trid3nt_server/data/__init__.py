"""Category-era fossil - dies when the last engine shim is absorbed.

Only ``.simulation`` still lives under this package: the per-engine bridge
modules that later waves move out engine by engine. The tool registry
(``register_tool``, ``TOOL_REGISTRY``, ``get_registered_tools``) lives in
``trid3nt_server.tools`` and is deliberately NOT re-exported here - there is no
compat shim, every reference names the new home.
"""

from __future__ import annotations

__all__: list[str] = []
