"""Every registered template owes a frozen proof directory.

A template with no pinned renders is a gap rather than a silence: nothing else
in the suite notices that a question class ships without evidence.
"""

from __future__ import annotations

import pytest

from tests.hygiene import _source as source

PROOF_ROOT = source.REPO_ROOT / "docs" / "proof" / "templates"

#: Live templates whose first acceptance packet has not been assembled. The
#: assertion below is xfail while this is non-empty; a name leaves the set the
#: day its packet lands, and the xfail dies with the last one.
AWAITING_FIRST_PACKET = frozenset(
    {"telemac_river_oil_spill", "telemac_river_scour", "telemac_river_sediment_plume"}
)


def _registered_templates() -> frozenset[str]:
    from tests.search.test_door_dissolution import EXPECTED_TEMPLATES

    return frozenset(EXPECTED_TEMPLATES)


@pytest.mark.xfail(
    strict=True,
    reason="three live templates await their first acceptance packet: "
    + ", ".join(sorted(AWAITING_FIRST_PACKET)),
)
def test_every_registered_template_has_a_proof_directory() -> None:
    missing = sorted(t for t in _registered_templates() if not (PROOF_ROOT / t).is_dir())
    assert not missing, "registered templates with no proof directory: " + ", ".join(missing)


def test_the_awaiting_set_names_only_live_templates() -> None:
    stale = sorted(AWAITING_FIRST_PACKET - _registered_templates())
    assert not stale, "AWAITING_FIRST_PACKET names templates that do not register: " + ", ".join(stale)


def test_no_proof_directory_outnames_the_awaiting_set() -> None:
    landed = sorted(t for t in AWAITING_FIRST_PACKET if (PROOF_ROOT / t).is_dir())
    assert not landed, (
        "these templates now have a proof directory and must leave "
        "AWAITING_FIRST_PACKET (the xfail above turns into an unexpected pass): "
        + ", ".join(landed)
    )
