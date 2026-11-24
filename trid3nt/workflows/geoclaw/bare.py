"""Build a GeoClaw rundata tree from the caller's native statements."""

from __future__ import annotations

import asyncio

from trid3nt_contracts import new_ulid

from trid3nt.inputs.input import Input
from trid3nt.tools.search.match import Need, match, sources_with_coverage

from ..runtime.bare import BareWorkflow, native_files
from ..runtime.fill import MISSING, Fill, Verdict, production
from ..runtime.module import keyword_floor
from ..runtime.workflow import Deck, RunResult
from ..solver.staging import discard_run
from .workflow import _DISPATCH as _DISPATCH, fill_sheet, publish_outputs, stage_sheet

VERTICAL_FRAME = Input(type=str, desc="The datum the stated topography and water levels are measured on.").stated()

class BareGeoclawWorkflow(BareWorkflow):
    engine = "geoclaw"

    def judged(self, state: Fill) -> dict[str, Verdict]:
        refused = super().judged(state)
        own, _ = keyword_floor(self.wrappers(), state.keywords)
        required = {key for key, slot in self.steering.MODULE_INPUT.items() if slot.is_required}
        required.update({"CLAW_LOWER", "CLAW_UPPER", "CLAW_NUM_CELLS"})
        for key in sorted(required - own.keys()):
            refused[key] = Verdict(MISSING, code="GEOCLAW_KEYWORD_UNSTATED", reason=f"State {key}.")
        if not (own.get("Topography") or own.get("TOPO_TOPOFILES")):
            lower, upper = own.get("CLAW_LOWER"), own.get("CLAW_UPPER")
            located = own.get("GEO_COORDINATE_SYSTEM") == 2 and lower is not None and upper is not None
            point = ({"lon": (lower[0] + upper[0]) / 2, "lat": (lower[1] + upper[1]) / 2}
                     if located else {})
            ranked = match(Need("topography", "bathymetry", **point), sources_with_coverage())
            refused["topography"] = Verdict(MISSING, code="DATA_SOURCE_UNSTATED",
                                             reason="State the topography source as TOPO_TOPOFILES or Topography.",
                                             remedies=tuple(row.fetcher for row in ranked.rows if not row.excluded))
        return refused

    async def prepare(self, state: Fill) -> Deck:
        env = production(state)
        run_id = new_ulid()
        staged = {}
        files = native_files(env.run.get("files"))

        async def write(sheet) -> None:
            staged.update(await stage_sheet(sheet=sheet, run_id=run_id, prefix=self.engine,
                                            name=self.name, assets=files))

        try:
            env.run["sheet"] = await fill_sheet(
                steering=self.steering, produced=env.run, params={}, workflow=self.name,
                title="", keywords=state.keywords, write=write)
        except BaseException:
            await asyncio.to_thread(discard_run, run_id, self.engine, stem=self.engine)
            raise
        return Deck(state, run_id=run_id, prefix=self.engine, staged=staged)

    async def solve(self, deck: Deck) -> RunResult:
        from .engine import solve_case
        from ..runtime.sheet import reference_time

        result = await self._solve_native(deck, solve_case)
        sheet = deck.state.env.run["sheet"]
        stated = sheet.stated()
        if stated.get("GEO_COORDINATE_SYSTEM") == 2 and stated.get("FGMAX_FGMAX_GRIDS"):
            solved = result.results["solve"]
            result.value = await publish_outputs(
                run={**solved, "module": sheet.module, "reference_time": reference_time(None, solved)},
                sheet=sheet, outputs=[], captions={}, params={})
        return result
