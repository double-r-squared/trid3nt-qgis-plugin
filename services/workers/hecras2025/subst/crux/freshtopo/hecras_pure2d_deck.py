"""Pure-2D HEC-RAS deck authors -- the .xNN (link c4) + .bNN (c5) for a fresh AOI.

The ADR 0134/0135 pure-2D deck needs, besides the geometry HDF (the
``hecras_geometry_writer``), two ASCII intermediates the Linux engines read:

* the ``.xNN`` geometry-preprocessor file -- declares the 2D area as a Storage
  Area, a minimal FAKE 1D reach (the engine requires >=1 reach), the Arrays
  Sizes, and the PropertyTableOptions. Authored here from the shipped pure-2D
  ``BaldEagleDamBrk.x09`` skeleton (``pure2d_reference/``), with the dam /
  gate / storage-area-connection stanzas REMOVED (we have no structures) and
  the SA name + perimeter-point count patched to the fresh mesh. This is the
  ONE remaining deck author ADR 0135 named (link c4, shrunk to the .xNN).

* the ``.bNN`` boundary file -- a BARE ``Upstream Flow Hydrograph`` (no
  ``River:/Reach:/RS:`` suffix -- that suffix marks a 1D inflow; its absence
  marks a 2D-BC-line inflow, mapped POSITIONALLY to the geometry's BC-line
  list) + a ``Downstream Normal Depth``. Authored here from the shipped
  ``BaldEagleDamBrk.b06`` pattern, carrying Muncie's proven Job Control header.

Both are validated by an offline round-trip test (``test_pure2d_deck.py``) and,
decisively, by the fresh-topology SOLVE they drive.
"""
from __future__ import annotations

import re
from pathlib import Path

_REF = Path(__file__).resolve().parents[1] / "pure2d_reference"
_X09 = _REF / "BaldEagleDamBrk.x09"
_B06 = _REF / "BaldEagleDamBrk.b06"

_CHIP = Path(__file__).resolve().parents[1] / "chippewa_reference"
_CHIP_X01 = _CHIP / "Chippewa_2D.x01"
_CHIP_B02 = _CHIP / "Chippewa_2D.b02"


def _fixed8(v: float) -> str:
    """HEC 8-char fixed field (matches the b06 ordinate formatting)."""
    s = f"{v:g}"
    return f"{s:>8}"[:8]


def _i8(v: object) -> str:
    return f"{v:>8}"


def patch_chippewa_xnn(area_name: str, n_perimeter: int) -> str:
    """Author a CLEAN pure-2D ``.xNN`` by patching the shipped Chippewa ``x01``.

    ``chippewa_reference/Chippewa_2D.x01`` (HEC-RAS 6.4) is the DAM-FREE pure-2D
    geometry preprocessor deck ADR 0136 said the distribution lacked: a single 2D
    area declared as a Storage Area, an EMPTY Storage-Area-Connection section, and
    a minimal ``Fake River``/``Fake Reach`` (2 dummy cross sections) satisfying the
    engine's >=1-reach requirement -- with NONE of the ``x09`` Sayers-Dam
    entanglement that made a dam-free reduction impossible.

    We patch only the two mesh-tracking fields: the Storage-Area (2D area) NAME and
    the perimeter-point COUNT. The Arrays-Sizes A/B rows encode the fake reach (2
    XS), NOT the 2D cell/perimeter count (proven: byte-identical to Weise's despite
    a different mesh), so they are carried verbatim and stay valid.

    CRITICAL: the perimeter-count line is HEC 8-char fixed-width. A naive
    ``str.replace("39", str(n))`` widens the field and desynchronises the parse
    (``error reading header information for a storage area``); this rewrites the
    whole line to proper 8-wide fields.

    Proven: the deck this authors SOLVES end-to-end through the production 6.6
    ``RasGeomPreprocess`` + ``RasUnsteady`` on a fresh carved mesh (ADR 0137, vol
    err 0.0). The 2D area stays DRY -- routing the fake-reach inflow ONTO the 2D BC
    line needs the plan-HDF 2D-BC ``/Event Conditions`` schema, which no shipped
    reference in this distribution exposes (OI-FT1, the precise open item).
    """
    out: list[str] = []
    for ln in _CHIP_X01.read_text().splitlines():
        if ln.startswith("SA ") and "Perimeter 1" in ln:
            ln = ln.replace("Perimeter 1     ", f"{area_name:<16}"[:16])
        elif re.match(r"\s*0\s+0\s+0\s+39\s+T\s*$", ln):
            ln = _i8(0) + _i8(0) + _i8(0) + _i8(int(n_perimeter)) + _i8("T")
        out.append(ln)
    return "\n".join(out) + "\n"


def patch_chippewa_bnn(peak_cfs: float, *, hydrograph_node: int = 1,
                       hydrograph_hours: float = 8760.0) -> str:
    """Author a CLEAN pure-2D ``.bNN`` by patching the shipped Chippewa ``b02``.

    The 6.6-correct 2D-BC-line inflow header is the SUFFIXED fake-reach form
    ``Upstream Flow Hydrograph - River: Fake River  Reach: Fake Reach  RS: 100``
    (NOT the bare ``b06`` form, which is 6.2-only and maps to the 1D reach in a 6.6
    engine -- the ADR 0136 mis-map). We keep b02's exact header/section skeleton and
    patch two fields: the inflow ordinates to a constant ``peak_cfs`` hold, and
    ``HYDROGRAPH LOCATIONS`` from the shipped ``0`` (which divide-by-zeros in the 1D
    output-block ``hdf_set_compression``) to one valid node.
    """
    b = _CHIP_B02.read_text().splitlines()
    out: list[str] = []
    i = 0
    while i < len(b):
        ln = b[i]
        if ln.strip().startswith("Upstream Flow Hydrograph"):
            out.append(ln)               # suffixed fake-reach header
            out.append(b[i + 1])         # count "       2"
            out.append(
                f"{_fixed8(0.0)}{_fixed8(peak_cfs)}"
                f"{_fixed8(hydrograph_hours)}{_fixed8(peak_cfs)}")
            i += 3
            continue
        if ln.strip() == "HYDROGRAPH LOCATIONS":
            out.append(ln)
            out.append(" 1 ")
            out.append(_i8(int(hydrograph_node)))
            i += 2                        # skip the shipped " 0 "
            continue
        out.append(ln)
        i += 1
    return "\n".join(out) + "\n"


def patch_xnn(area_name: str, n_perimeter: int) -> str:
    """Author a pure-2D ``.xNN`` by PATCHING the shipped x09 reference.

    The shipped ``BaldEagleDamBrk.x09`` is a working, HEC-authored pure-2D
    geometry preprocessor deck (valid-by-construction, and confirmed to parse +
    finish under the production 6.6 ``RasGeomPreprocess``). We patch only the two
    fields that must track the fresh mesh: the Storage-Area (2D area) NAME and
    the perimeter-point COUNT. Everything structural -- the Arrays Sizes, the
    fake ``Fake River``/``Fake Reach`` (the engine's required >=1 reach), the
    PropertyTableOptions -- is carried verbatim.

    Rationale (ADR 0135): re-authoring the Arrays Sizes / section skeleton by
    hand mis-set the 1D htab counts (``end-of-file during read`` in htabreal);
    the shipped reference's counts already match its fake reach exactly, so
    patching is both simpler AND proven.
    """
    out = []
    for ln in _X09.read_text().splitlines():
        if ln.startswith("SA ") and "BaldEagleCr" in ln:
            ln = ln.replace("BaldEagleCr", f"{area_name:<16}"[:16])
        elif ln.strip().endswith("537       T") and "537" in ln:
            ln = ln.replace("537", f"{n_perimeter}", 1)
        out.append(ln)
    return "\n".join(out) + "\n"


def patch_bnn(
    peak_cfs: float,
    *,
    ds_normal_slope: float = 0.001,
    hydrograph_hours: float = 8760.0,
    plan_title: str = "Fresh Topology 2D Carve",
    short_id: str = "FreshTopo2D",
) -> str:
    """Author a pure-2D ``.bNN`` by PATCHING the shipped b06 reference.

    ``BaldEagleDamBrk.b06`` is HEC's own preprocessed Linux boundary file for a
    pure-2D plan: a BARE ``Upstream Flow Hydrograph`` (positional -> the
    geometry's first BC line) + a ``Downstream Normal Depth``. We keep its exact
    header / Job Control / section skeleton and patch only the inflow ordinates
    to a constant ``peak_cfs`` (the b06 pattern is ``0 <flow> <hours> <flow>`` --
    a constant hold; scaling ``peak_cfs`` is the ADR-style forcing knob) and the
    normal-depth slope. Number formats therefore stay byte-faithful to a proven
    file -- hand-authoring them tripped an ``input conversion error`` in
    read_un_beg.
    """
    src = _B06.read_text().splitlines()
    out = []
    i = 0
    while i < len(src):
        ln = src[i]
        if ln.strip() == "Upstream Flow Hydrograph":
            out.append(ln)                        # header
            out.append(src[i + 1])                # count "       2"
            # replace the single ordinate line: 0 <flow> <hours> <flow>
            out.append(
                f"{_fixed8(0.0)}{_fixed8(peak_cfs)}"
                f"{_fixed8(hydrograph_hours)}{_fixed8(peak_cfs)}"
            )
            i += 3                                 # skip header, count, old ords
            continue
        if ln.strip() == "Downstream Normal Depth":
            out.append(ln)
            out.append(f"{_fixed8(ds_normal_slope).rstrip()[-8:]:>8}")
            i += 2
            continue
        out.append(ln)
        i += 1
    return "\n".join(out) + "\n"


def remove_lateral_weirs(x04_text: str, new_perimeter: int) -> str:
    """Strip the 1D<->2D lateral-weir coupling from a Muncie-style ``.xNN``.

    THE WORKING .xNN PATH (ADR 0136). Muncie's shipped ``x04`` is a proven,
    fully-parsing geometry for the 6.6 engine, but it is a COMBINED 1D/2D deck:
    its White River reach carries two ``NODE`` type-6 lateral-weir structures
    (at RS 13214 + RS 7300) whose ``DS SA/2D`` is ``2D Interior Area``. Left in
    place, ``RasUnsteady`` crashes in ``jobinit_lw_q2d`` trying to couple the
    weir to a carved mesh whose boundary no longer lies on the weir line. This
    transform removes both type-6 NODE blocks, decrements the node counts in the
    Arrays Sizes B-row + the Reach Boundaries downstream node, and patches the SA
    perimeter-point count to ``new_perimeter`` -- yielding a standalone White
    River reach + a bare 2D flow area, which the 6.6 engine solves end-to-end
    (ADR 0136: vol err 0.0021%). This is the durable ``.xNN`` author the ADR 0135
    charter asked for -- built by patching a proven reference, not blind.

    Preferred over the from-scratch x09 patch (``patch_xnn``): the shipped x09's
    "fake reach" is entangled with its Sayers-Dam SA connection; removing the dam
    leaves the reach header unreadable (a dam-coupled node numbering), so x09
    cannot be reduced to a clean pure-2D reach without a node-topology rebuild.
    """
    lines = x04_text.splitlines()
    node_idx = [i for i, l in enumerate(lines) if l.startswith("NODE")]

    def _end(i: int) -> int:
        later = [n for n in node_idx if n > i]
        return later[0] if later else len(lines)

    weirs = [i for i in node_idx if len(lines[i]) > 7 and lines[i][7] == "6"]
    removed = len(weirs)
    for a in sorted(weirs, reverse=True):
        del lines[a:_end(a)]

    def f8(*v: object) -> str:
        return "".join(f"{x:>8}" for x in v)

    # Arrays Sizes B-row (line index 4): node counts drop by the weirs removed.
    br = lines[4].split()
    br[0] = str(int(br[0]) - removed)
    br[1] = str(int(br[1]) - removed)
    lines[4] = f8(*br)
    # Reach Boundaries: decrement the downstream node index by the weirs removed.
    for i, l in enumerate(lines):
        if l.strip().startswith("T       T") and l.strip().endswith("F       F"):
            t = l.split()
            lines[i] = f8("T", "T", t[2], str(int(t[3]) - removed), "") + \
                "        " + " ".join(t[4:])
            break
    # SA perimeter-point count -> the carved mesh's perimeter.
    for i, l in enumerate(lines):
        if l.strip().endswith("170       T"):
            lines[i] = l.replace("170", str(new_perimeter), 1)
            break
    return "\n".join(lines) + "\n"


def patch_muncie_bnn(muncie_b04_text: str, *, flow_scale: float = 1.0,
                     hydrograph_node: int = 30) -> str:
    """Author the WORKING ``.bNN`` by patching Muncie's shipped ``b04`` (ADR 0136).

    Three edits make the shipped combined-deck boundary file solve on a
    lateral-weir-stripped deck: (1) zero the ``Breach Data`` (the weir breach is
    gone); (2) point ``HYDROGRAPH LOCATIONS`` at ONE valid 1D node -- a count of
    zero makes ``RasUnsteady`` divide by zero in the 1D output-block compression
    setup (``hdf_set_compression``); (3) optionally scale the White River inflow
    ordinates by ``flow_scale`` (the ADR-style forcing knob). The 2D flow area is
    carried in the deck and solved; directing an inflow to its BC line is the
    named open item (no combined-deck 2D-BC-line .bNN reference exists).
    """
    b = muncie_b04_text.splitlines()
    out: list[str] = []
    i = 0
    in_flow = False
    while i < len(b):
        l = b[i]
        if l.strip() == "Breach Data":
            out.append(l); out.append("       0"); i += 1
            while b[i].strip() != "Hydrograph Data":
                i += 1
            continue
        if l.strip() == "HYDROGRAPH LOCATIONS":
            out.append(l); out.append(" 1 "); out.append(f"{hydrograph_node:>8}")
            i += 1
            while not b[i].startswith("Rules"):
                i += 1
            continue
        if flow_scale != 1.0 and l.startswith("Upstream Flow Hydrograph"):
            in_flow = True; out.append(l); i += 1; continue
        if in_flow:
            if l.strip() == "3.4E+38" or l.startswith(" 3.4E+38"):
                in_flow = False; out.append(l); i += 1; continue
            toks = l.split()
            if toks and all(_is_num(t) for t in toks) and len(toks) >= 2:
                # (time, flow) pairs: scale the flow column (odd indices)
                sc = []
                for j, t in enumerate(toks):
                    v = float(t) * flow_scale if j % 2 == 1 else float(t)
                    sc.append(f"{v:g}")
                out.append("".join(f"{s:>8}" for s in sc)); i += 1; continue
        out.append(l); i += 1
    return "\n".join(out) + "\n"


def _is_num(s: str) -> bool:
    try:
        float(s); return True
    except ValueError:
        return False
