"""The sweep guard: naked substitution may not come back.

A naked substitution serves DIFFERENT data than the request named without
declaring a rung, firing the loudness gate or stamping an activation row. Each
known shape gets a structural guard, or a REGISTERED entry whose marker must
still match. Structural on purpose: a lint that greps for a word finds comments."""

from __future__ import annotations

import inspect
import pathlib

import pytest

from trid3nt_server.tools.fetchers._router.registration import _validate_hooks
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.fallbacks import (
    BELOW_PRIMARY_CLASSES,
    DEGRADATION_CLASSES,
    registered_ladders,
)

_REPO = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def specs():
    return compose_specs_from_tree()


# SHAPE 1 -- spec.endpoint_fallback: a SAME-DATA mirror chain, and nothing else.
#
# It used to be spelled ``fallback`` and carried nine lists, eight of which named
# a SIBLING TOOL. ``resolve_endpoints`` indexes the spec's own ``endpoints``, so
# those eight could never execute -- but the spec card printed them to the model
# as fallbacks this source has. A promise no code path can keep is the same lie
# as a silent swap, told the other way round.


def test_every_endpoint_fallback_entry_names_an_endpoint_of_its_own_spec(specs):
    offenders = {
        s.name: [fb for fb in s.endpoint_fallback if fb not in s.endpoints]
        for s in specs.values()
        if any(fb not in s.endpoints for fb in s.endpoint_fallback)
    }
    assert not offenders, (
        "endpoint_fallback is SAME-DATA mirrors of this source's own endpoints. "
        "These entries name something else and can never execute:\n"
        f"  {offenders}\n"
        "A CROSS-DATASET alternative belongs on a declared fallback ladder."
    )


def test_no_endpoint_fallback_entry_names_a_registered_tool(specs):
    """The exact shape that shipped: a sibling TOOL name in the mirror list."""
    tool_names = set(specs)
    offenders = {
        s.name: [fb for fb in s.endpoint_fallback if fb in tool_names]
        for s in specs.values()
        if any(fb in tool_names for fb in s.endpoint_fallback)
    }
    assert not offenders, (
        f"a sibling tool name is not an endpoint mirror: {offenders}"
    )


def test_registration_refuses_a_cross_dataset_endpoint_fallback(specs):
    """The guard has to be at LOAD time, not only in this test file -- a spec
    tree is composed in production too."""
    bad = specs["fetch_gridmet"].model_copy(
        update={"endpoint_fallback": ["fetch_era5_reanalysis"]}
    )
    with pytest.raises(ValueError, match="names no endpoint of this spec"):
        _validate_hooks(bad)


def test_the_spec_card_key_says_what_the_mechanism_is():
    """``fallback`` on a card is ambiguous between a mirror hop and a ladder
    rung; the model reads this text and must not conflate them."""
    from trid3nt_server.tools.fetchers._router import registration

    card_src = inspect.getsource(registration.spec_card)
    assert '"endpoint_fallback": list(spec.endpoint_fallback)' in card_src
    assert '"fallback": list(spec.fallback)' not in card_src


# SHAPE 3 -- an undeclared contributor to a declared result.
#
# A source the ladder does not declare paints part of the answer, so the rows a
# reader gets do not add up to the raster they are reading.


def test_every_ladder_is_a_complete_account_of_its_own_rungs():
    for name, ladder in registered_ladders().items():
        below = ladder.rungs[
            ladder.rungs.index(ladder.primary_rung) + 1:
        ]
        assert below, f"ladder {name} declares no rung below its primary"
        for rung in below:
            assert rung.consequence in BELOW_PRIMARY_CLASSES, (
                f"{name}/{rung.name}: {rung.consequence}"
            )
        assert ladder.terminal.consequence == "refuse"


# THE REGISTER -- naked substitutions that are NOT fixed, with their verdicts.
#
# These are the audit's SILENT physics/data rows. They are not ladders: no
# alternative SOURCE exists to declare, only a default constant or an assumed
# value, so the fix is the loudness class, not F2's
# declared-degradation regime. They are registered here so the set cannot grow
# quietly and so fixing one forces this table to change with it.

#
# Keyed by AUDIT ROW, not by file: a row can live in more than one file, so a
# file key silently loses entries.
#
# A marker is the tightest STABLE anchor at the site -- a constant name, a counter
# increment, a log format string. It is deliberately not the whole line: an
# exact-whitespace marker false-alarms on a reformat. It is still only an anchor,
# not a semantic check: a site could in principle keep its constant and lose its
# defect, so a failure here means LOOK, not "broken".
_PARKED_SILENT_SUBSTITUTIONS: dict[str, tuple[str, str, str]] = {}


def test_the_parked_silent_substitutions_are_still_exactly_these():
    """Each parked site must still be findable by its marker.

    A failure here is good news or a regression, never noise: the site was fixed or
    it moved. What it must never do is drift out of the register unnoticed."""
    missing: list[str] = []
    for row, (rel, marker, _verdict) in _PARKED_SILENT_SUBSTITUTIONS.items():
        path = _REPO / rel
        if not path.exists() or marker not in path.read_text(encoding="utf-8"):
            missing.append(f"{row} :: {rel} :: {marker!r}")
    assert not missing, (
        "a registered naked-substitution site no longer matches its marker. If it "
        "was FIXED, delete the row and name the ADR; if it MOVED, update the "
        "marker:\n  " + "\n  ".join(missing)
    )


def test_the_register_carries_a_verdict_for_every_entry():
    for row, (rel, marker, verdict) in _PARKED_SILENT_SUBSTITUTIONS.items():
        assert rel and marker and len(verdict) > 80, row
        assert "PARKED" in verdict, row
        # A marker that carries its own indentation or line break pins the file's
        # FORMATTING as well as its behaviour, and reformatting is not a finding.
        assert marker == marker.strip() and "\n" not in marker, row


def test_the_register_covers_every_parked_row_the_adr_names():
    """The register must actually hold every parked row that still exists.

    Most left the tree with the code that carried them; the register is the mechanism
    that keeps the REMAINING set from shrinking quietly."""
    # Empty: every parked row left the tree with the code that carried it. Row
    # 11's EPSG:3857 tag rode the grid-to-COG writer, which has no producer now
    # that a field is a dataset group on the mesh it was solved over.
    parked_rows: set[str] = set()
    registered = {
        key.split()[1].rstrip("ab") for key in _PARKED_SILENT_SUBSTITUTIONS
    }
    assert registered == parked_rows, f"missing: {parked_rows - registered}"


def test_the_degradation_classes_are_the_only_gated_ones():
    """``enhancement`` was added to the schema in F2. If it ever joins the gated
    set, a FINER source starts asking permission to be better."""
    assert DEGRADATION_CLASSES == {"same_data", "cross_dataset", "synthetic"}
    assert "enhancement" in BELOW_PRIMARY_CLASSES
    assert "enhancement" not in DEGRADATION_CLASSES
