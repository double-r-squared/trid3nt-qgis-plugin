"""Open a configured calibration model for individual PEST++ stages."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from trid3nt.inputs.bound_domain import bind_domain, reset_domain
from trid3nt.inputs.coverage import strictness
from trid3nt.inputs.review import unattended
from trid3nt.render.emission import bind_turn_case, current_turn_case
from trid3nt.settings.reader import bind_run, reset_run
from trid3nt.workflows.pestpp.errors import PestppError
from trid3nt.workflows.pestpp.study import Study, StudyResult, calibrate, resolve, screen
from trid3nt.workflows.pestpp.workflow import PestppWorkflow

from .model import Model

__all__ = ["Study", "StudyResult", "calibrate", "open_study", "resolve", "screen"]


@asynccontextmanager
async def open_study(model: Model) -> AsyncIterator[Study]:
    """Fill a calibrate model in its case and hold the study files until the caller leaves."""
    workflow = model._workflow
    if not isinstance(workflow, PestppWorkflow):
        raise PestppError("open_study requires a calibrate model.", error_code="PESTPP_MODEL_REQUIRED")
    previous_case = current_turn_case()
    bind_turn_case(model._case.name)
    try:
        with model._case.bound(), unattended(), strictness(model.strict):
            state, _deck, refusal = await model._filling(model._template_warning())
            if refusal is not None or not state.ready:
                code, sentence = refusal or state.refusal()
                raise PestppError(sentence, error_code=code)
            domain, settings = bind_domain(state.domain), bind_run(state.settings)
            try:
                async with workflow.study(state) as study:
                    yield study
            finally:
                reset_run(settings)
                reset_domain(domain)
    finally:
        bind_turn_case(previous_case)
