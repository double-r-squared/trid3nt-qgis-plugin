"""Every canary seed that NAMES a place states that place's kind, and it classifies.

Offline. The kind is a fact of the place, so the machinery reads it off the seed
and never guesses one: a seed that names a river but states no type is an
unclassified place, declares nothing beside its domain, and the template refuses
downstream for a row the declaration looks like it asked for. A bare pair names
nothing and stays exempt - it is the naming that makes the silence a drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DEV = Path(__file__).resolve().parents[2] / "dev"

if not DEV.is_dir():
    pytest.skip("dev/ is absent: the dev tools are not on the remote",
                allow_module_level=True)

from dev.testing.canaries import CANARIES  # noqa: E402
from trid3nt_server.inputs.domain import place_kind  # noqa: E402
from trid3nt_server.inputs.point import _from_mapping  # noqa: E402


def _named_seeds():
    for name in sorted(CANARIES):
        for row, value in sorted(CANARIES[name].args.items()):
            if isinstance(value, dict) and value.get("name") and (
                    "coordinates" in value or "longitude" in value):
                yield name, row, value


@pytest.mark.parametrize("name,row,seed",
                         list(_named_seeds()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_a_named_seed_classifies_as_a_place_the_hydrography_publishes(
        name, row, seed) -> None:
    kind = place_kind(_from_mapping(seed, row, "CANARY_SEED"))
    assert kind, (
        f"{name} names {seed['name']} on {row} but states no place_type that "
        "classifies: the place is unclassified, so nothing is declared beside "
        "its domain and whatever needs that row refuses")
