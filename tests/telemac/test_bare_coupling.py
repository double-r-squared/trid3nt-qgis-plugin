from __future__ import annotations

import pytest

from trid3nt.workflows.runtime.module import SlotRefused
from trid3nt.workflows.runtime.sheet import fill, fill_coupled
from trid3nt.workflows.telemac.modules import ART, T2D
from trid3nt.workflows.telemac.modules.coupling import bare_coupling


def test_attach_uses_native_composite_and_stages_qualified_keywords():
    coupled = bare_coupling(T2D, [{"module": "waqtel", "process": 1}])
    sheet = fill(T2D, coupling=coupled)
    sheet = fill_coupled(sheet, {"waqtel": {"WATER_TEMPERATURE": 19.0}})
    assert sheet.stated()["COUPLING_WITH"] == "WAQTEL"
    assert sheet.stated()["WATER_QUALITY_PROCESS"] == 1
    assert sheet.stated()["WAQTEL_STEERING_FILE"] == "waqtel.cas"
    assert sheet.files["waqtel.cas"]["slots"]["WATER_TEMPERATURE"] == 19.0


def test_coupling_refuses_any_missing_process_or_unsupported_body():
    with pytest.raises(SlotRefused, match="WATER QUALITY PROCESS"):
        bare_coupling(T2D, [{"module": "waqtel"}])
    with pytest.raises(SlotRefused, match="waqtel"):
        bare_coupling(ART, [{"module": "waqtel", "process": 1}])
    assert bare_coupling(T2D, [{"module": "gaia", "steering": "my-gaia.cas"}])[0]["steering"] == "my-gaia.cas"
