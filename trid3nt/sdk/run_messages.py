"""Messages exchanged by a reviewed run and its host."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_core import to_jsonable_python

from trid3nt_contracts import ULIDStr
from trid3nt_contracts.execution import LayerURI


class RunInstruction(BaseModel):
    kind: Literal["run"] = "run"
    case: ULIDStr | None
    run_id: ULIDStr
    script: Literal["case.py", "calibrate.py", "optimize.py", "tradeoff.py", "assimilate.py"]
    served: str
    files: dict[str, str] = Field(default_factory=dict)
    inputs: dict[str, str] = Field(default_factory=dict)


class StopRun(BaseModel):
    kind: Literal["stop"] = "stop"


class RunEvent(BaseModel):
    kind: Literal["checked", "stage-start", "stage-end", "dispatch", "progress", "terminal",
                  "chart", "review", "complete", "stopped", "refused"]
    args: list[Any] = Field(default_factory=list)

    @classmethod
    def event(cls, kind: str, *args: Any) -> RunEvent:
        return cls(kind=kind, args=to_jsonable_python(args, fallback=_encode))

    def unpack(self) -> tuple:
        args = list(self.args)
        if self.kind == "dispatch":
            args[2] = SimpleNamespace(**args[2])
        if self.kind == "terminal" and args[0] is not None:
            args[0] = SimpleNamespace(**args[0])
        if self.kind in {"complete", "stopped"} and args[0] is not None:
            if "cycles" in args[0] and "phi_trace" in args[0]:
                from trid3nt.workflows.pestpp.assimilation import AssimilateResult

                args[0] = AssimilateResult.model_validate(args[0])
            elif "candidates" in args[0] and "outcome" in args[0]:
                from trid3nt.workflows.pestpp.tradeoff import TradeoffResult

                args[0] = TradeoffResult.model_validate(args[0])
            elif "bindings" in args[0] and "outcome" in args[0]:
                from trid3nt.workflows.pestpp.optimization import OptimizeResult

                args[0] = OptimizeResult.model_validate(args[0])
            elif "study_id" in args[0]:
                from trid3nt.workflows.pestpp.study import StudyResult

                args[0] = StudyResult.model_validate(args[0])
            else:
                args[0] = LayerURI.model_validate(args[0])
        return self.kind, *args


def _encode(value: Any) -> Any:
    if isinstance(value, SimpleNamespace):
        return vars(value)
    raise TypeError(f"Unsupported reviewed event value {type(value).__name__}.")
