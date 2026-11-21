"""PEST++'s control options as the engine's dictionary: read, stated by name, locked, refused.

The dictionary is extracted from the 5.2.27 manual's tables and the 5.2.27 binary's own records of
parse-only studies (tests/fixtures/pestpp/defaults); these tests hold the committed rows to those records.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from trid3nt.tools.search.describe_keywords.describe_keywords import describe_keywords
from trid3nt.workflows.pestpp.authoring.control import Observed, Parameter, write_control
from trid3nt.workflows.pestpp.errors import PestppError
from trid3nt.workflows.pestpp.modules import LOCKED, PESTPP
from trid3nt.workflows.pestpp.staging import interface_files
from trid3nt.workflows.runtime.module import SlotRefused, accept

_ROOT = Path(__file__).resolve().parents[2]
_RECORDS = _ROOT / "tests" / "fixtures" / "pestpp" / "defaults"


def _extractor():
    spec = importlib.util.spec_from_file_location("extract_pestpp_dictionary",
                                                  _ROOT / "scripts" / "extract_pestpp_dictionary.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows() -> list[dict]:
    return json.loads((_ROOT / "trid3nt" / "workflows" / "pestpp" / "modules" / "module_input"
                       / "pestpp.json").read_text())["keywords"]


def _study(tmp_path: Path, **stated) -> str:
    write_control(tmp_path, name="pestpp.pst", parameters=[Parameter("k", 1.0, 0.1, 10.0, "k")],
                  observations=[Observed("o1", 2.0, "heads", 1.0, "cited")], templates=[("m.tpl", "m.in")],
                  instructions=[("m.ins", "m.out")], command="mf6", stated=stated)
    return (tmp_path / "pestpp.pst").read_text()


def test_describe_keywords_reads_an_option_with_the_default_the_binary_takes_unstated():
    entry = describe_keywords(module="pestpp", keyword="ies_num_reals")["entry"]
    assert entry["engine_default"] == 50
    assert entry["rubrique"] == ["pestpp-ies"]
    assert entry["source"].startswith("PEST++ 5.2.27 users manual: 9.4")


def test_where_the_binary_and_the_manual_differ_the_binary_s_value_is_the_default():
    row = next(r for r in _rows() if r["keyword"] == "ies_subset_size")
    assert (row["default"], row["manual_default"]) == (-10, 4)
    assert describe_keywords(module="pestpp", keyword="IES_SUBSET_SIZE")["entry"]["engine_default"] == -10


def test_every_default_is_the_value_the_binary_prints_for_it_unstated():
    tool = _extractor()
    printed = {**tool.printed((_RECORDS / "pestpp.rec").read_text()),
               **tool.controlled((_RECORDS / "control.rec").read_text())}
    back = tool.stated_back((_RECORDS / "names.pst").read_text())
    checked = 0
    for row in _rows():
        said = printed.get(row["keyword"])
        if said is None or (row["keyword"] in back and back[row["keyword"]].strip(",") != said.strip(",")):
            continue
        if row["keyword"] not in tool.controlled((_RECORDS / "control.rec").read_text()) \
                and row["keyword"] not in back:
            continue
        assert tool._same(row["default"], tool._typed(row["type"], said)), row["keyword"]
        checked += 1
    assert checked > 100


def test_no_row_is_a_name_the_binary_does_not_read():
    unread = _extractor().unread((_RECORDS / "names.rec").read_text())
    assert {"da_hostart_cycle", "glm_norm_form", "ies_mda_decl_fac", "mou_crossover_prob", "mou_mutation_prob",
            "mou_risk_obj", "opt_external_dec_var_groups", "opt_iter_toll"} <= unread
    assert not unread & {row["keyword"] for row in _rows()}


def test_noptmax_unstated_is_the_binary_s_zero_iterations():
    assert describe_keywords(module="pestpp", keyword="noptmax")["entry"]["engine_default"] == 0


def test_a_stated_option_reaches_the_control_file(tmp_path):
    text = _study(tmp_path, IES_NUM_REALS=20, ies_lambda_mults=[0.1, 1.0], ies_enforce_bounds=False)
    lines = text.split("* control data keyword\n", 1)[1].split("*", 1)[0].splitlines()
    assert {"ies_num_reals 20", "ies_lambda_mults 0.1,1.0", "ies_enforce_bounds false"} <= set(lines)
    assert interface_files(tmp_path, "pestpp.pst") == ([("m.tpl", "m.in")], [("m.ins", "m.out")])


def test_an_option_stated_by_name_is_held_to_its_row():
    assert accept(PESTPP, "ies_num_reals", 20)[:2] == ("IES_NUM_REALS", 20)
    with pytest.raises(SlotRefused, match="ies_num_reals is INTEGER"):
        accept(PESTPP, "ies_num_reals", "twenty")
    with pytest.raises(SlotRefused, match="phi_based"):
        accept(PESTPP, "ies_subset_how", "best")


@pytest.mark.parametrize("name", sorted(LOCKED))
def test_a_locked_option_refuses_naming_why(name, tmp_path):
    with pytest.raises(SlotRefused, match=f"{name} is the study's own to state: "):
        accept(PESTPP, name, PESTPP.MODULE_INPUT[name.upper()].engine_default)
    with pytest.raises(PestppError, match="the study's own to state") as refused:
        _study(tmp_path, **{name: PESTPP.MODULE_INPUT[name.upper()].engine_default})
    assert refused.value.error_code == "PESTPP_OPTION_LOCKED"


def test_every_locked_option_is_a_row_of_the_dictionary():
    assert {name.upper() for name in LOCKED} <= set(PESTPP.MODULE_INPUT)


def test_an_unknown_name_refuses_in_one_line(tmp_path):
    with pytest.raises(SlotRefused) as refused:
        accept(PESTPP, "ies_num_realz", 2)
    assert str(refused.value) == "pestpp has no keyword 'ies_num_realz'."
    with pytest.raises(PestppError, match="pestpp has no option 'ies_num_realz'"):
        _study(tmp_path, ies_num_realz=2)


_CONTROL = {"relparmax", "facparmax", "facorig", "phiredswh", "noptswitch", "splitswh", "noptmax", "phiredstp",
            "nphistp", "nphinored", "relparstp", "nrelpar"}
_GROUP = {"inctyp", "derinc", "derinclb", "forcen", "derincmul", "dermthd"}


def _section(name: str) -> dict[str, dict]:
    return {row["keyword"]: row for row in _rows() if row["rubrique"] == [name]}


def test_the_control_data_variables_the_binary_reads_are_rows_with_the_default_it_prints():
    tool = _extractor()
    rows = _section("control data")
    assert set(rows) == _CONTROL
    printed = tool.controlled((_RECORDS / "control.rec").read_text())
    assert set(printed) <= set(rows)
    for name, said in printed.items():
        assert tool._same(rows[name]["default"], tool._typed(rows[name]["type"], said)), name
    assert [rows[name].get("default") for name in ("noptswitch", "splitswh")] == [None, None]
    assert not _CONTROL & tool.unread((_RECORDS / "names.rec").read_text())


def test_the_step_limits_default_to_the_values_the_binary_prints_not_ten():
    entry = describe_keywords(module="pestpp", keyword="facparmax")["entry"]
    assert (entry["engine_default"], entry["rubrique"]) == (1.1, ["control data"])
    assert describe_keywords(module="pestpp", keyword="relparmax")["entry"]["engine_default"] == 1.0
    assert describe_keywords(module="pestpp", keyword="phiredswh")["entry"]["engine_default"] == 0.1
    assert accept(PESTPP, "facparmax", 3.0)[:2] == ("FACPARMAX", 3.0)


def test_the_group_and_parameter_variables_the_binary_echoes_are_rows():
    tool = _extractor()
    groups, columns = _section("parameter groups"), _section("parameter data")
    assert set(groups) == _GROUP and set(columns) == {"partrans", "parchglim"}
    default = tool.grouped((_RECORDS / "control.rec").read_text())
    for name, said in default.items():
        assert tool._same(groups[name]["default"], tool._typed(groups[name]["type"], said)), name
    assert [name for name in _GROUP if "default" not in groups[name]] == ["dermthd"]
    assert not any("default" in row for row in columns.values())
    stated = tool.grouped((_RECORDS / "groups.rec").read_text())
    assert stated == {"inctyp": "absolute", "derinc": "0.05", "derinclb": "0.3", "forcen": "always_3",
                      "derincmul": "1.5"}
    assert (_RECORDS / "groups_grp.csv").read_text().splitlines()[0] == "pargpnme," + ",".join(
        ("inctyp", "derinc", "derinclb", "forcen", "derincmul", "dermthd"))
    assert "p1           none                  RELATIVE" in (_RECORDS / "groups.rec").read_text()


def _tables(tmp_path: Path, **stated) -> tuple[str, list[str], list[str]]:
    text = _study(tmp_path, **stated)
    read = lambda name: (tmp_path / name).read_text().splitlines()
    groups = read("study_groups.csv") if (tmp_path / "study_groups.csv").exists() else []
    return text, groups, read("study_parameters.csv")


def test_a_group_variable_stated_by_name_writes_the_group_table_with_the_dictionary_default_beside_it(tmp_path):
    text, groups, parameters = _tables(tmp_path, derinc=0.05, FORCEN="always_3")
    assert groups == ["pargpnme,inctyp,derinc,derinclb,forcen,derincmul,dermthd",
                      "k,relative,0.05,0.0,always_3,2.0,parabolic"]
    assert "* parameter groups external\nstudy_groups.csv\n* parameter data external" in text
    assert "derinc" not in text.split("* parameter groups external")[0]
    assert parameters[1].startswith("k,log,factor,")


def test_an_unstated_group_variable_leaves_the_binary_to_its_own_group(tmp_path):
    text, groups, _ = _tables(tmp_path, facparmax=3.0)
    assert groups == [] and "parameter groups" not in text
    assert "facparmax 3.0" in text.split("* control data keyword\n", 1)[1].splitlines()


def test_a_parameter_column_stated_by_name_replaces_it_for_every_parameter(tmp_path):
    _, _, parameters = _tables(tmp_path, partrans="none", parchglim="relative")
    assert parameters[1].startswith("k,none,relative,")
    with pytest.raises(SlotRefused, match="partrans"):
        accept(PESTPP, "partrans", "fixed")


def test_a_log_parameter_stated_a_relative_change_limit_refuses(tmp_path):
    with pytest.raises(PestppError, match="log-transformed") as refused:
        _study(tmp_path, parchglim="relative")
    assert refused.value.error_code == "PESTPP_OPTION_CONFLICT"
