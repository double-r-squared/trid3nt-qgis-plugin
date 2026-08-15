"""Scenario/fetch reuse short-circuit shim (server-refactor wave 4, ADR 0264).

The reuse *decision* logic (the scenario-index + fetched-layer lookups and the
short-circuit branches) lives inline in ``_core._invoke_tool_via_emitter``,
tightly woven through the dispatch loop and ``SessionState``; it does not move
this wave. What extracts cleanly is the drop-in registry shim the short-circuit
swaps in: :class:`_ReuseEntry`. ``_core`` re-imports it by name so its
``isinstance(entry, _ReuseEntry)`` guards and ``_ReuseEntry(...)`` constructions
resolve unchanged, and the package facade re-exposes it at
``trid3nt_server.server._ReuseEntry``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trid3nt_contracts.execution import LayerURI


@dataclass
class _ReuseEntry:
    """A drop-in ``RegisteredTool``-shaped shim for the reuse short-circuit.

    Carries the real tool's ``metadata`` (so the tool card / telemetry label
    is unchanged) but a ``fn`` that returns the EXISTING layer instead of
    launching the solver. ``_invoke_tool_via_emitter`` swaps the registry
    entry for this so the SAME ``emit_tool_call`` LayerURI gate fires with
    the reused layer.
    """

    metadata: Any
    layer: "LayerURI"

    @property
    def fn(self) -> Any:
        layer = self.layer

        def _return_existing(**_ignored: Any) -> "LayerURI":
            return layer

        return _return_existing
