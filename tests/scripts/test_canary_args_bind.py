"""Every declared canary's args against the tool it names: nothing lands nowhere.

A generated template signature absorbs unknown keywords into ``**_extra_ignored``,
so a declaration that names a row the template no longer has is not an error - it
is a canary whose stated value silently became a fetch. The bind is checked here
because the DECLARATION is where that drift appears.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[2] / "dev"

if not DEV.is_dir():
    pytest.skip("dev/ is absent: the dev tools are not on the remote",
                allow_module_level=True)

from dev.testing.canaries import CANARIES  # noqa: E402

#: The keywords every generated signature carries beside the declaration.
_CONTROLS = ("input_mode", "restart_clean", "keywords")


def _accepted(tool: str) -> set[str]:
    from trid3nt_server.tools import TOOL_REGISTRY

    signature = inspect.signature(TOOL_REGISTRY[tool].fn)
    return {name for name, parameter in signature.parameters.items()
            if parameter.kind is not inspect.Parameter.VAR_KEYWORD}


@pytest.mark.parametrize("name", sorted(CANARIES))
def test_every_arg_a_canary_states_is_one_its_tool_declares(name):
    declared = CANARIES[name]
    accepted = _accepted(declared.tool) | set(_CONTROLS)
    unread = sorted(set(declared.args) - accepted)
    assert not unread, (
        f"{name} states {unread}, which {declared.tool} does not declare: the "
        "values are swallowed and whatever row they were meant to fill fetches "
        "instead")
