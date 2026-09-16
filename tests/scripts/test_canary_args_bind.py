"""Every declared canary's args against the tool it names: nothing lands nowhere.

A generated template signature absorbs unknown keywords into ``**_extra_ignored``,
so a declaration that names a row the template no longer has is not an error - it
is a canary whose stated value silently became a fetch. The bind is checked here
because the DECLARATION is where that drift appears.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[2] / "dev"

if not DEV.is_dir():
    pytest.skip("dev/ is absent: the dev tools are not on the remote",
                allow_module_level=True)

from dev.testing import accepted_args  # noqa: E402
from dev.testing.canaries import CANARIES  # noqa: E402


@pytest.mark.parametrize("name", sorted(CANARIES))
def test_every_arg_a_canary_states_is_one_its_tool_declares(name):
    declared = CANARIES[name]
    unread = sorted(set(declared.args) - accepted_args(declared.tool))
    assert not unread, (
        f"{name} states {unread}, which {declared.tool} does not declare: the "
        "values are swallowed and whatever row they were meant to fill fetches "
        "instead")
