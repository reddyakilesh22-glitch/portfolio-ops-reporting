"""INC-003 (issue #5): statement date formats and actionable CLI failures."""

from datetime import date

import pytest

from opsreport.__main__ import main
from opsreport.config import load_yaml
from opsreport.extract.excel_loader import SourceFileError, parse_statement_date

FORMATS = load_yaml("excel_layouts.yaml")["custodian"]["as_of_formats"]


@pytest.mark.parametrize("text", ["13-Nov-2026", "11/13/2026", "2026-11-13",
                                  "13 November 2026"])
def test_statement_date_formats(text):
    assert parse_statement_date(text, FORMATS, "f") == date(2026, 11, 13)


def test_unknown_date_format_names_file_and_formats():
    with pytest.raises(SourceFileError, match=r"f\[sheet\]: can't read statement date "
                                              r"'2026\.11\.13'; tried formats"):
        parse_statement_date("2026.11.13", FORMATS, "f[sheet]")


def test_configured_formats_are_unambiguous():
    assert not {"%m/%d/%Y", "%d/%m/%Y"} <= set(FORMATS), \
        "both US and day-first formats configured: 11/12 would be ambiguous"


def test_known_failure_is_one_line_with_traceback_in_log(tmp_path, capsys):
    code = main(["run", "--as-of", "2026-11-13", "--out", str(tmp_path),
                 "--snapshot", str(tmp_path / "missing.json")])
    err = capsys.readouterr().err
    assert code == 1
    assert "FileNotFoundError" in err and "RUNBOOK" in err
    assert "Traceback" not in err
    assert "Traceback" in (tmp_path / "run.log").read_text()


def test_successful_run_exits_zero(tmp_path, capsys):
    assert main(["run", "--as-of", "2026-11-13", "--prior", "2026-11-06",
                 "--out", str(tmp_path)]) == 0
    assert "Breaks: 11" in capsys.readouterr().out
