"""Audit group B (issue #13): untrusted text must never become a formula; no
personal names in snapshots; credentials only over HTTPS."""

import json

import pandas as pd
import pytest
from openpyxl import load_workbook

from opsreport import config
from opsreport.extract.jira_client import _sanitize
from opsreport.report.excel_report import _Writer, csv_safe

PAYLOADS = ['=HYPERLINK("http://evil.example","Click")', "=1+1", "+1+1", "-2+3",
            "@SUM(1)", "\t=1+1"]


def test_text_is_never_written_as_a_formula(tmp_path):
    w = _Writer(tmp_path / "t.xlsx")
    ws = w.sheet("t")
    w.table(ws, pd.DataFrame({"summary": PAYLOADS + ["normal text"]}), 0)
    w.close()
    cells = load_workbook(tmp_path / "t.xlsx")["t"]["A"][1:]
    assert all(c.data_type == "s" for c in cells), [c.data_type for c in cells]
    assert [c.value for c in cells][:len(PAYLOADS)] == PAYLOADS      # text kept as-is


def test_urls_in_text_are_not_turned_into_links(tmp_path):
    w = _Writer(tmp_path / "t.xlsx")
    ws = w.sheet("t")
    w.table(ws, pd.DataFrame({"summary": ["http://evil.example/x"]}), 0)
    w.close()
    assert load_workbook(tmp_path / "t.xlsx")["t"]["A2"].hyperlink is None


def test_csv_output_neutralises_formula_prefixes():
    df = pd.DataFrame({"summary": PAYLOADS + ["normal"], "amount": [-5.0] * 7})
    out = csv_safe(df)
    assert all(v.startswith("'") for v in out["summary"][:len(PAYLOADS)])
    assert out["summary"].iloc[-1] == "normal"
    assert (out["amount"] == -5.0).all()          # real negative numbers untouched


def test_snapshot_replaces_display_names_with_stable_pseudonyms():
    raw = {"issues": [
        {"key": "A-1", "fields": {"assignee": {"displayName": "Jane Doe", "accountId": "x"},
                                  "status": {"name": "Done"}}},
        {"key": "A-2", "fields": {"assignee": {"displayName": "Jane Doe"}}}]}
    clean = _sanitize(raw)
    text = json.dumps(clean)
    assert "Jane" not in text and "accountId" not in text
    a1, a2 = (i["fields"]["assignee"]["displayName"] for i in clean["issues"])
    assert a1 == a2 and a1.startswith("user-")     # same person, same pseudonym
    assert clean["issues"][0]["fields"]["status"]["name"] == "Done"


@pytest.mark.parametrize("url, ok", [
    ("https://example.atlassian.net", True),
    ("http://example.atlassian.net", False),
    ("example.atlassian.net", False),
    ("http://localhost:8080", True),
])
def test_jira_token_only_sent_over_https(monkeypatch, tmp_path, url, ok):
    monkeypatch.setattr(config, "ROOT", tmp_path)      # no real .env
    for k, v in {"JIRA_BASE_URL": url, "JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "t",
                 "JIRA_PROJECT_KEY": "X"}.items():
        monkeypatch.setenv(k, v)
    if ok:
        assert config.load_jira_settings().base_url == url
    else:
        with pytest.raises(config.ConfigError, match="https"):
            config.load_jira_settings()


def test_warns_when_env_file_is_readable_by_others(monkeypatch, tmp_path, caplog):
    env = tmp_path / ".env"
    env.write_text("JIRA_BASE_URL=https://x.atlassian.net\nJIRA_EMAIL=a@b.c\n"
                   "JIRA_API_TOKEN=t\nJIRA_PROJECT_KEY=X\n")
    env.chmod(0o644)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"):
        monkeypatch.delenv(k, raising=False)
    with caplog.at_level("WARNING"):
        config.load_jira_settings()
    assert "chmod 600" in caplog.text
    for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY"):
        monkeypatch.delenv(k, raising=False)       # load_dotenv set them; clean up
