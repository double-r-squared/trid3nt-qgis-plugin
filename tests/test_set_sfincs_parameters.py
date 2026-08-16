"""Unit tests for ``set_sfincs_parameters`` (LANE D, no network).

The parent deck is built ONCE per test session by running the REAL
``build_sfincs_model`` (HydroMT-SFINCS) against the already-committed
``tests/fixtures/sfincs_aoi/{dem.tif,landcover.tif}`` fixture (Mexico Beach,
FL -- the same fixture ``test_sfincs_archetype_decks.py`` uses). This is a
genuine LOCAL file read (rasterio on the fixture GeoTIFFs), so it stays
offline; the capture trick (persisting the builder's internal
``TemporaryDirectory``) mirrors ``test_sfincs_archetype_decks.py`` exactly
(duplicated locally rather than importing a sibling test module's private
helper). Every setter call passes ``_work_dir`` so all writes land under
``tmp_path``, zero network.

Coverage:
1.  ``test_registered`` -- TOOL_REGISTRY entry + metadata.
2.  ``test_manning_land_set`` -- op="set" on ``manning_land`` leaves
    ``manning_sea`` at its prior value; before/after read back from the
    WRITTEN child deck.
3.  ``test_manning_sea_scale`` -- op="scale" on ``manning_sea``.
4.  ``test_qinf_set_then_scale`` -- ``qinf`` starts at the SFINCS
    losses-off baseline (0.0, no grid) -- op="set" creates the grid,
    op="scale" (in a second call) multiplies the now-written value.
5.  ``test_combined_change_in_one_call`` -- manning_land + qinf in one
    ``changes[]`` list.
6a. ``test_out_of_plausible_range_warns`` -- an atypical-but-physical
    Manning's n (0.85, above the 0.011-0.8 band) warns (in_range=false) and
    proceeds, NOT a hard reject (build-contract 3.4).
6b. ``test_meaningless_value_hard_error`` -- a NEGATIVE Manning's n raises the
    typed ``BoundsViolation`` (physically meaningless).
7.  ``test_unknown_parameter_raises``.
8.  ``test_parent_untouched`` -- copy-on-write proof (parent deck's
    sfincs.man mean unchanged after the child write).
9.  ``test_manifest_written``.
"""

from __future__ import annotations

import glob
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

hydromt_sfincs = pytest.importorskip("hydromt_sfincs")
pytest.importorskip("rasterio")

import trid3nt_server.workflows.sfincs.sfincs_builder as _builder  # noqa: E402
from trid3nt_server.workflows.sfincs.sfincs_builder import (  # noqa: E402
    BuildOptions,
    ForcingSpec,
    build_sfincs_model,
)
from trid3nt_server.data import TOOL_REGISTRY  # noqa: E402
from trid3nt_server.data.simulation._setter_envelope import (  # noqa: E402
    BoundsViolation,
    SetterInputError,
)
from trid3nt_server.data.simulation.sfincs.set_sfincs_parameters.set_sfincs_parameters import (  # noqa: E402
    set_sfincs_parameters,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sfincs_aoi"
_DEM = str(_FIXTURE_DIR / "dem.tif")
_LANDCOVER = str(_FIXTURE_DIR / "landcover.tif")
_BBOX = (-85.42, 29.93, -85.39, 29.96)

# Manning's n for exactly the 11 NLCD classes present in the fixture landcover
# (subset of the version-pinned manning_mapping.csv -- same values
# test_sfincs_archetype_decks.py uses, so the OQ-4 vintage-gate passes).
_FIXTURE_MANNING_ROWS = (
    (11, 0.025), (21, 0.035), (22, 0.060), (23, 0.100), (24, 0.150),
    (31, 0.030), (42, 0.150), (52, 0.080), (71, 0.040), (90, 0.120), (95, 0.080),
)


def _build_parent_deck(capture_root: Path) -> Path:
    """Build a real, small (100 m) pluvial SFINCS deck into a PERSISTED temp
    dir (the builder's own TemporaryDirectory is normally destroyed on
    return for a local/non-s3 manifest_uri, so it is monkeypatched here --
    the exact capture method ``test_sfincs_archetype_decks.py`` uses)."""
    manning_csv = capture_root / "manning_fixture_subset.csv"
    lines = ["nlcd_class,manning_n,description"]
    lines += [f"{c},{n},d" for c, n in _FIXTURE_MANNING_ROWS]
    manning_csv.write_text("\n".join(lines) + "\n")

    root = tempfile.mkdtemp(prefix="capture-", dir=str(capture_root))

    class _PersistTmp:
        def __init__(self, prefix: str = "", **_kw: object) -> None:
            self.name = tempfile.mkdtemp(prefix=prefix, dir=root)

        def __enter__(self) -> str:
            return self.name

        def __exit__(self, *_a: object) -> bool:
            return False

    opts = BuildOptions(
        grid_resolution_m=100.0,
        autoscale_grid=False,
        simulation_hours=24.0,
        output_setup_uri=os.path.join(str(capture_root), "out", "manifest.json"),
    )
    forcing = ForcingSpec(forcing_type="pluvial_synthetic", precip_inches=8.0, duration_hours=24.0)

    with mock.patch.object(_builder.tempfile, "TemporaryDirectory", _PersistTmp):
        build_sfincs_model(
            dem_uri=_DEM, landcover_uri=_LANDCOVER, river_geometry_uri=None,
            forcing=forcing, bbox=_BBOX, options=opts, nlcd_vintage_year=2021,
            manning_mapping_csv=str(manning_csv),
        )

    inps = glob.glob(os.path.join(root, "**", "deck", "sfincs.inp"), recursive=True)
    assert inps, f"no captured deck/sfincs.inp under {root!r}"
    return Path(inps[0]).parent


@pytest.fixture(scope="module")
def parent_deck(tmp_path_factory: pytest.TempPathFactory) -> Path:
    capture_root = tmp_path_factory.mktemp("sfincs_setter_parent")
    return _build_parent_deck(capture_root)


def _grid_means(deck_dir: Path) -> dict[str, float]:
    """Read (land_mean, sea_mean, qinf_mean) straight from a deck dir, same
    masking convention set_sfincs_parameters itself uses, for assertions."""
    from hydromt_sfincs import SfincsModel

    m = SfincsModel(root=str(deck_dir), mode="r")
    m.read()
    active = m.grid["msk"] >= 1
    land_mask = active & (m.grid["dep"] >= 0.0)
    sea_mask = active & (m.grid["dep"] < 0.0)
    out = {}
    man = m.grid.get("manning")
    out["land"] = float(man.where(land_mask).mean()) if man is not None else None
    out["sea"] = float(man.where(sea_mask).mean()) if man is not None else None
    qinf = m.grid.get("qinf")
    out["qinf"] = float(qinf.where(active).mean()) if qinf is not None else 0.0
    return out


def test_registered() -> None:
    assert "set_sfincs_parameters" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["set_sfincs_parameters"]
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
    assert entry.metadata.read_only_hint is False
    assert entry.metadata.idempotent_hint is False


def test_manning_land_set(parent_deck: Path, tmp_path: Path) -> None:
    before = _grid_means(parent_deck)
    result = set_sfincs_parameters(
        parent_model_uri=str(parent_deck),
        changes=[{"parameter": "manning_land", "op": "set", "value": 0.08}],
        _work_dir=str(tmp_path / "work"),
    )
    assert result["engine"] == "sfincs"
    [entry] = result["changes_applied"]
    assert entry["param"] == "manning_land"
    assert entry["scope"] == "global"
    assert entry["before"] == pytest.approx(before["land"], abs=1e-3)
    assert entry["after"] == pytest.approx(0.08, abs=1e-3)
    [plaus] = result["plausibility"]
    assert plaus["in_range"] is True

    child_model_dir = Path(result["child_setup_uri"][len("file://"):]).parent / "model"
    after = _grid_means(child_model_dir)
    assert after["land"] == pytest.approx(0.08, abs=1e-3)
    # manning_sea untouched by a manning_land-only change.
    assert after["sea"] == pytest.approx(before["sea"], abs=1e-3)


def test_manning_sea_scale(parent_deck: Path, tmp_path: Path) -> None:
    before = _grid_means(parent_deck)
    result = set_sfincs_parameters(
        parent_model_uri=str(parent_deck),
        changes=[{"parameter": "manning_sea", "op": "scale", "factor": 1.5}],
        _work_dir=str(tmp_path / "work"),
    )
    [entry] = result["changes_applied"]
    assert entry["param"] == "manning_sea"
    assert entry["before"] == pytest.approx(before["sea"], abs=1e-3)
    assert entry["after"] == pytest.approx(before["sea"] * 1.5, abs=1e-3)


def test_qinf_set_then_scale(parent_deck: Path, tmp_path: Path) -> None:
    result1 = set_sfincs_parameters(
        parent_model_uri=str(parent_deck),
        changes=[{"parameter": "qinf", "op": "set", "value": 4.0}],
        _work_dir=str(tmp_path / "work1"),
    )
    [entry1] = result1["changes_applied"]
    assert entry1["before"] == pytest.approx(0.0)  # SFINCS losses-off baseline
    assert entry1["after"] == pytest.approx(4.0, abs=1e-3)
    assert entry1["unit"] == "mm/hr"

    child_model_dir = Path(result1["child_setup_uri"][len("file://"):]).parent / "model"
    result2 = set_sfincs_parameters(
        parent_model_uri=str(child_model_dir),
        changes=[{"parameter": "qinf", "op": "scale", "factor": 2.0}],
        _work_dir=str(tmp_path / "work2"),
    )
    [entry2] = result2["changes_applied"]
    assert entry2["before"] == pytest.approx(4.0, abs=1e-3)
    assert entry2["after"] == pytest.approx(8.0, abs=1e-3)


def test_combined_change_in_one_call(parent_deck: Path, tmp_path: Path) -> None:
    result = set_sfincs_parameters(
        parent_model_uri=str(parent_deck),
        changes=[
            {"parameter": "manning_land", "op": "set", "value": 0.06},
            {"parameter": "qinf", "op": "set", "value": 2.5},
        ],
        _work_dir=str(tmp_path / "work"),
    )
    by_param = {c["param"]: c for c in result["changes_applied"]}
    assert set(by_param) == {"manning_land", "qinf"}
    assert by_param["manning_land"]["after"] == pytest.approx(0.06, abs=1e-3)
    assert by_param["qinf"]["after"] == pytest.approx(2.5, abs=1e-3)
    assert len(result["plausibility"]) == 2


def test_out_of_plausible_range_warns(parent_deck: Path, tmp_path: Path) -> None:
    # 0.85 is above the plausible band (0.011-0.8) but physically valid (a
    # user may intentionally set a very rough surface): warn-and-proceed with
    # in_range=false, NOT a hard reject (build-contract 3.4).
    result = set_sfincs_parameters(
        parent_model_uri=str(parent_deck),
        changes=[{"parameter": "manning_land", "op": "set", "value": 0.85}],
        _work_dir=str(tmp_path / "work"),
    )
    [entry] = result["changes_applied"]
    assert entry["after"] == pytest.approx(0.85, abs=1e-3)
    [plaus] = result["plausibility"]
    assert plaus["in_range"] is False
    assert plaus["range"] == [0.011, 0.8]
    assert "WARNING" in plaus["note"]


def test_meaningless_value_hard_error(parent_deck: Path, tmp_path: Path) -> None:
    # A NEGATIVE Manning's n is physically meaningless -> hard BoundsViolation.
    with pytest.raises(BoundsViolation) as excinfo:
        set_sfincs_parameters(
            parent_model_uri=str(parent_deck),
            changes=[{"parameter": "manning_land", "op": "set", "value": -0.1}],
            _work_dir=str(tmp_path / "work"),
        )
    assert excinfo.value.error_code == "PARAM_BOUNDS_VIOLATION"
    assert excinfo.value.retryable is False


def test_unknown_parameter_raises(parent_deck: Path, tmp_path: Path) -> None:
    with pytest.raises(SetterInputError):
        set_sfincs_parameters(
            parent_model_uri=str(parent_deck),
            changes=[{"parameter": "not_a_real_param", "op": "set", "value": 1.0}],
            _work_dir=str(tmp_path / "work"),
        )


def test_parent_untouched(parent_deck: Path, tmp_path: Path) -> None:
    before = _grid_means(parent_deck)
    set_sfincs_parameters(
        parent_model_uri=str(parent_deck),
        changes=[{"parameter": "manning_land", "op": "set", "value": 0.5}],
        _work_dir=str(tmp_path / "work"),
    )
    after = _grid_means(parent_deck)
    assert after["land"] == pytest.approx(before["land"], abs=1e-6)


def test_manifest_written(parent_deck: Path, tmp_path: Path) -> None:
    result = set_sfincs_parameters(
        parent_model_uri=str(parent_deck),
        changes=[{"parameter": "manning_land", "op": "set", "value": 0.07}],
        _work_dir=str(tmp_path / "work"),
    )
    manifest_path = Path(result["child_setup_uri"][len("file://"):])
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["engine"] == "sfincs"
    assert manifest["parent_model"] == str(parent_deck)


def _parse_inp_config(inp_path: Path) -> dict[str, str]:
    """Parse a sfincs.inp into a {config_key: raw_value_string} dict (SFINCS'
    key column is whitespace-padded ``key = value``)."""
    out: dict[str, str] = {}
    for line in inp_path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def test_child_inp_epsg_bare_int_and_crs_survive(parent_deck: Path, tmp_path: Path) -> None:
    """Regression (the child deck must SOLVE, not merely parse):
    ``hydromt_sfincs.SfincsModel.write_config()`` used to rewrite the child
    deck's native ``epsg`` from the bare integer SFINCS v2.3.3's Fortran reader
    requires (``sfincs_input.f90`` line 837 list-directed integer read) into a
    CRS *string* ("EPSG:3857") and DROP the separate ``crs = ...`` passthrough
    line -- either regression makes EVERY child deck unsolvable ("Bad integer
    for item 1 in list input", exit 2). The setter now restores the parent
    deck's exact CRS lines after write_config. Assert (1) the child ``epsg``
    line is a bare integer parseable as ``int`` (not a CRS string), (2) it
    equals the parent's grid-CRS code (copy-on-write, unchanged), (3) the
    ``crs`` line survives, and (4) the child's config-key SET matches the
    parent's exactly -- a manning-only change touches the sfincs.man grid, not
    any sfincs.inp config key, so no key is added or dropped (formatting-stable
    copy-on-write)."""
    result = set_sfincs_parameters(
        parent_model_uri=str(parent_deck),
        changes=[{"parameter": "manning_land", "op": "scale", "factor": 0.85}],
        _work_dir=str(tmp_path / "work"),
    )
    child_inp = Path(result["child_setup_uri"][len("file://"):]).parent / "model" / "sfincs.inp"
    parent_cfg = _parse_inp_config(parent_deck / "sfincs.inp")
    child_cfg = _parse_inp_config(child_inp)

    # (1) child epsg is a bare integer the SFINCS Fortran reader can parse.
    assert "epsg" in child_cfg, "child sfincs.inp dropped the epsg line entirely"
    assert not child_cfg["epsg"].upper().startswith("EPSG"), (
        f"child epsg is a CRS string, not a bare int: {child_cfg['epsg']!r}"
    )
    int(child_cfg["epsg"])  # raises ValueError on a non-integer -> would be the bug
    # (2) epsg matches the parent's grid CRS code (copy-on-write leaves it be).
    assert child_cfg["epsg"] == parent_cfg["epsg"], (
        f"child epsg {child_cfg['epsg']!r} != parent epsg {parent_cfg['epsg']!r}"
    )
    # (3) the crs passthrough line survives when the parent build deck had one.
    assert "crs" in parent_cfg, "test precondition: build deck should carry a crs line"
    assert child_cfg.get("crs") == parent_cfg["crs"], (
        f"child crs {child_cfg.get('crs')!r} != parent crs {parent_cfg['crs']!r}"
    )
    # (4) no config key added/dropped by the manning-only setter run.
    assert set(child_cfg) == set(parent_cfg), (
        "child sfincs.inp config keys differ from parent: "
        f"only-in-child={sorted(set(child_cfg) - set(parent_cfg))} "
        f"only-in-parent={sorted(set(parent_cfg) - set(child_cfg))}"
    )
