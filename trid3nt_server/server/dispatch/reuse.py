"""Scenario/fetch reuse short-circuit shim.

The reuse decision lives in the dispatch loop; what lives here is the registry
entry the short-circuit swaps in, so the same emit-tool-call gate fires with the
reused layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trid3nt_contracts.execution import LayerURI


@dataclass
class _ReuseEntry:
    """A drop-in ``RegisteredTool``-shaped shim for the reuse short-circuit: the
    real tool's ``metadata``, so the card and telemetry label are unchanged, with
    an ``fn`` that returns the EXISTING layer instead of launching the solver."""

    metadata: Any
    layer: "LayerURI"

    @property
    def fn(self) -> Any:
        layer = self.layer

        def _return_existing(**_ignored: Any) -> "LayerURI":
            return layer

        return _return_existing
