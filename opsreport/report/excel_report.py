"""Write the weekly Portfolio Ops report workbook (XlsxWriter)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd

NAVY, GREY, RED, AMBER, GREEN = "#1F3A5F", "#F2F4F7", "#C0392B", "#B9770E", "#1E7B4C"
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Copy of `df` safe to open in a spreadsheet: text cells that a spreadsheet
    would run as a formula get a leading apostrophe. Numbers are untouched."""
    out = df.copy()
    for col in out.columns:
        if not pd.api.types.is_numeric_dtype(out[col]):
            out[col] = out[col].map(
                lambda v: "'" + v if isinstance(v, str) and v.startswith(_FORMULA_PREFIXES)
                else v)
    return out


@dataclass
class ReportData:
    as_of: date
    prior: date | None
    aum: pd.DataFrame
    breaks: pd.DataFrame
    break_summary: pd.DataFrame
    jira_kpis: dict
    jira_by_team: pd.DataFrame
    jira_aging: pd.DataFrame
    jira_trend: pd.DataFrame
    jira_overdue: pd.DataFrame
    issues: pd.DataFrame
    run_info: pd.DataFrame
    commentary: list[str] = field(default_factory=list)
    commentary_source: str = "template"


_ACRONYMS = {"aum": "AUM", "mv": "MV", "usd": "USD", "fx": "FX", "sla": "SLA",
             "isin": "ISIN", "pct": "%", "4wk": "(4 wk)", "bdays": "(bus. days)",
             "cust": "Cust", "acct": "Acct", "abs": "Abs"}


def _label(col: str) -> str:
    return " ".join(_ACRONYMS.get(w, w.capitalize()) for w in col.split("_"))


def _kind(col: str) -> str:
    c = col.lower()
    if c.endswith("_pct") or c.startswith("sla_"):
        return "pct"
    if any(k in c for k in ("_usd", "_mv", "mv_", "aum", "cash", "accrued", "diff",
                            "securities_mv")) and "quantity" not in c:
        return "money"
    if c.startswith(("quantity", "price", "fx_rate")):
        return "num"
    if "date" in c or c in ("week_ending", "as_of"):
        return "date"
    return "text"


class _Writer:
    def __init__(self, path: Path):
        # Text in this report comes from Jira and vendor files, so it is never
        # trusted: a string starting with "=" must stay text, not become a formula.
        self.xw = pd.ExcelWriter(
            path, engine="xlsxwriter", date_format="yyyy-mm-dd",
            datetime_format="yyyy-mm-dd hh:mm",
            engine_kwargs={"options": {"strings_to_formulas": False,
                                       "strings_to_urls": False}})
        wb = self.wb = self.xw.book
        self.f = {
            "title": wb.add_format({"bold": True, "font_size": 16, "font_color": NAVY}),
            "subtitle": wb.add_format({"italic": True, "font_color": "#555555"}),
            "h2": wb.add_format({"bold": True, "font_size": 12, "font_color": NAVY,
                                 "bottom": 2, "bottom_color": NAVY}),
            "head": wb.add_format({"bold": True, "bg_color": NAVY, "font_color": "white",
                                   "text_wrap": True, "valign": "top", "border": 1}),
            "money": wb.add_format({"num_format": "#,##0;[Red](#,##0)"}),
            "pct": wb.add_format({"num_format": "0.0%;[Red]-0.0%"}),
            "num": wb.add_format({"num_format": "#,##0.####"}),
            "date": wb.add_format({"num_format": "yyyy-mm-dd"}),
            "text": wb.add_format({}),
            "kpi_label": wb.add_format({"font_color": "#555555", "bg_color": GREY,
                                        "text_wrap": True, "valign": "top"}),
            "kpi_money": wb.add_format({"bold": True, "font_size": 14, "bg_color": GREY,
                                        "num_format": "$#,##0.0,,\"M\""}),
            "kpi_int": wb.add_format({"bold": True, "font_size": 14, "bg_color": GREY,
                                      "num_format": "#,##0"}),
            "kpi_pct": wb.add_format({"bold": True, "font_size": 14, "bg_color": GREY,
                                      "num_format": "0%"}),
            "wrap": wb.add_format({"text_wrap": True, "valign": "top"}),
            "bold": wb.add_format({"bold": True}),
            "high": wb.add_format({"font_color": RED, "bold": True}),
            "medium": wb.add_format({"font_color": AMBER, "bold": True}),
            "low": wb.add_format({"font_color": GREEN}),
            "error": wb.add_format({"bg_color": "#F9D6D5"}),
            "warning": wb.add_format({"bg_color": "#FDF0D5"}),
        }

    def sheet(self, name: str):
        ws = self.wb.add_worksheet(name)
        ws.hide_gridlines(2)
        ws.set_landscape()
        ws.fit_to_pages(1, 0)
        ws.set_paper(1)  # US Letter
        return ws

    def table(self, ws, df: pd.DataFrame, row: int, col: int = 0,
              widths: dict[str, int] | None = None, autofilter: bool = False) -> int:
        """Write df with typed formats; return the next free row."""
        widths = widths or {}
        for j, name in enumerate(df.columns):
            ws.write(row, col + j, _label(name), self.f["head"])
            kind = _kind(name)
            width = widths.get(name, 14 if kind != "text" else 18)
            ws.set_column(col + j, col + j, width)
            for i, val in enumerate(df[name].tolist(), start=1):
                if val is None or (pd.api.types.is_scalar(val) and pd.isna(val)):
                    ws.write_blank(row + i, col + j, None)
                elif kind == "date" and isinstance(val, (date, datetime, pd.Timestamp)):
                    ws.write_datetime(row + i, col + j, pd.Timestamp(val).to_pydatetime(),
                                      self.f["date"])
                elif kind in ("money", "pct", "num") and isinstance(val, (int, float)):
                    ws.write_number(row + i, col + j, float(val), self.f[kind])
                else:
                    ws.write(row + i, col + j, val, self.f["text"])
        if autofilter and len(df):
            ws.autofilter(row, col, row + len(df), col + len(df.columns) - 1)
        return row + len(df) + 2

    def kpi(self, ws, row: int, col: int, label: str, value, kind: str) -> None:
        ws.merge_range(row, col, row, col + 1, label, self.f["kpi_label"])
        fmt = {"money": "kpi_money", "int": "kpi_int", "pct": "kpi_pct"}[kind]
        if value is None:
            ws.merge_range(row + 1, col, row + 1, col + 1, "n/a", self.f["kpi_int"])
        else:
            ws.merge_range(row + 1, col, row + 1, col + 1, value, self.f[fmt])

    def close(self) -> None:
        self.xw.close()


def write_report(data: ReportData, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    w = _Writer(path)
    _summary(w, data)
    _breaks(w, data)
    _aum(w, data)
    _sla(w, data)
    _dq(w, data)
    ws = w.sheet("Run Info")
    ws.write(0, 0, "Run information", w.f["title"])
    w.table(ws, data.run_info, 2, widths={"item": 28, "value": 90})
    w.close()
    return path


def _summary(w: _Writer, d: ReportData) -> None:
    ws = w.sheet("Summary")
    ws.set_column(0, 11, 16)
    ws.write(0, 0, "Portfolio Operations Weekly Report", w.f["title"])
    prior = f" | prior week {d.prior:%d %b %Y}" if d.prior else ""
    ws.write(1, 0, f"As of {d.as_of:%A %d %B %Y}{prior} | synthetic data", w.f["subtitle"])

    total = d.aum[d.aum["fund_code"] == "TOTAL"].iloc[0]
    high = int((d.breaks["materiality"] == "High").sum())
    k = d.jira_kpis
    tiles = [
        ("Total AUM", total["aum"], "money"),
        ("AUM change WoW", total.get("aum_change"), "money"),
        ("Recon breaks", len(d.breaks), "int"),
        ("High-materiality breaks", high, "int"),
        ("Total break difference", d.breaks["abs_base_mv_diff"].sum(), "money"),
        ("Open ops tickets", k["open_tickets"], "int"),
        ("Overdue vs SLA", k["overdue_tickets"], "int"),
        ("SLA met, last 4 weeks", k["sla_met_4wk"], "pct"),
    ]
    for i, (label, value, kind) in enumerate(tiles):
        r, c = 3 + (i // 4) * 3, (i % 4) * 2
        w.kpi(ws, r, c, label, None if pd.isna(value) else value, kind)

    row = 10
    ws.merge_range(row, 0, row, 7, "Commentary", w.f["h2"])
    lines = d.commentary or default_commentary(d)
    row += 1
    label = (f"AI draft ({d.commentary_source.removeprefix('llm:')}): every number "
             "checked against this report. Analyst review required before sending."
             if d.commentary_source.startswith("llm:")
             else f"Source: {d.commentary_source}")
    ws.merge_range(row, 0, row, 7, label, w.f["subtitle"])
    for i, line in enumerate(lines, start=1):
        ws.merge_range(row + i, 0, row + i, 7, f"• {line}", w.f["wrap"])
        ws.set_row(row + i, 18 * (1 + len(line) // 130))   # ~130 chars per line
    row += len(lines) + 2

    ws.merge_range(row, 0, row, 7, "AUM by fund (fund accounting, USD)", w.f["h2"])
    cols = [c for c in ["fund_code", "fund_name", "aum", "aum_prior", "aum_change",
                        "aum_change_pct", "acct_vs_custodian_mv"] if c in d.aum.columns]
    row = w.table(ws, d.aum[cols], row + 1,
                  widths={c: 16 for c in cols} | {"fund_name": 32})

    ws.merge_range(row, 0, row, 7, "Reconciliation breaks by type", w.f["h2"])
    by_type = (d.breaks.groupby("break_type", as_index=False)
               .agg(breaks=("isin", "count"), abs_base_mv_diff=("abs_base_mv_diff", "sum"))
               .sort_values("abs_base_mv_diff", ascending=False))
    w.table(ws, by_type, row + 1, widths={"break_type": 16, "breaks": 32,
                                          "abs_base_mv_diff": 16})
    ws.activate()


def default_commentary(d: ReportData) -> list[str]:
    """Deterministic template commentary (fallback for the AI draft)."""
    errors = int((d.issues["severity"] == "ERROR").sum()) if len(d.issues) else 0
    warnings = len(d.issues) - errors
    total = d.aum[d.aum["fund_code"] == "TOTAL"].iloc[0]
    lines = []
    if "aum_change" in d.aum:
        lines.append(f"Total AUM ${total['aum'] / 1e6:,.1f}M, "
                     f"{total['aum_change'] / 1e6:+,.1f}M ({total['aum_change_pct']:+.1%}) "
                     "week over week.")
    if len(d.breaks):
        top = d.breaks.nlargest(1, "abs_base_mv_diff").iloc[0]
        lines.append(f"{len(d.breaks)} reconciliation breaks, "
                     f"${d.breaks['abs_base_mv_diff'].sum() / 1e6:,.1f}M absolute; largest is "
                     f"{top['fund_code']} {top['ticker']} ({top['break_type']}, "
                     f"${top['abs_base_mv_diff'] / 1e6:,.2f}M).")
    k = d.jira_kpis
    sla = f"{k['sla_met_4wk']:.0%}" if k["sla_met_4wk"] is not None else "n/a"
    lines.append(f"{k['open_tickets']} open ops tickets, {k['overdue_tickets']} past SLA; "
                 f"SLA met {sla} over the last 4 weeks.")
    lines.append(f"Data quality: {errors} error(s), {warnings} warning(s); see the "
                 "Data Quality tab.")
    return lines


def _breaks(w: _Writer, d: ReportData) -> None:
    ws = w.sheet("Breaks")
    ws.write(0, 0, f"Custodian vs fund accounting breaks, {d.as_of:%d %b %Y}", w.f["title"])
    ws.write(1, 0, "Sorted by exposure: the $ difference, or the whole position for an "
                   "unprocessed corporate action. Positive diff = custodian higher.",
             w.f["subtitle"])
    cols = ["fund_code", "ticker", "break_type", "materiality", "exposure_usd",
            "likely_cause",
            "quantity_cust", "quantity_acct", "quantity_diff", "price_cust", "price_acct",
            "base_mv_cust", "base_mv_acct", "base_mv_diff", "isin", "description",
            "custodian_locations"]
    df = d.breaks[[c for c in cols if c in d.breaks]]
    end = w.table(ws, df, 3, autofilter=True,
                  widths={"likely_cause": 60, "ticker": 16, "description": 32,
                          "break_type": 17, "isin": 15})
    ws.freeze_panes(4, 2)
    mcol = cols.index("materiality")
    for level in ("high", "medium", "low"):
        ws.conditional_format(4, mcol, end, mcol, {
            "type": "cell", "criteria": "==", "value": f'"{level.title()}"',
            "format": w.f[level]})
    w.table(ws, d.break_summary, end + 1, widths={"break_type": 17})


def _aum(w: _Writer, d: ReportData) -> None:
    ws = w.sheet("AUM")
    ws.write(0, 0, "AUM by fund", w.f["title"])
    ws.write(1, 0, "AUM = securities MV + cash + accrued income (fund accounting). "
                   "Custodian MV excludes accruals.", w.f["subtitle"])
    end = w.table(ws, d.aum, 3, widths={"fund_name": 32})
    funds = len(d.aum) - 1
    chart = w.wb.add_chart({"type": "bar"})
    col = list(d.aum.columns).index("aum")
    chart.add_series({"name": "AUM", "categories": ["AUM", 4, 1, 3 + funds, 1],
                      "values": ["AUM", 4, col, 3 + funds, col], "fill": {"color": NAVY}})
    chart.set_title({"name": "AUM by fund (USD)"})
    chart.set_legend({"none": True})
    chart.set_x_axis({"num_format": "$#,##0,,\"M\""})
    ws.insert_chart(end + 1, 1, chart, {"x_scale": 1.4})


def _sla(w: _Writer, d: ReportData) -> None:
    ws = w.sheet("Ops SLA")
    ws.write(0, 0, "Ops tickets (Jira): SLA and aging", w.f["title"])
    ws.write(1, 0, "Point-in-time as of the report date, from Business Date / Resolved "
                   "Business Date.", w.f["subtitle"])
    row = 3
    ws.merge_range(row, 0, row, 7, "By team", w.f["h2"])
    ws.set_column(0, 7, 15)
    ws.set_column(0, 0, 20)
    row = w.table(ws, d.jira_by_team, row + 1, widths={c: 15 for c in d.jira_by_team}
                  | {"ops_team": 20})
    ws.merge_range(row, 0, row, 7, "Open ticket aging (business days)", w.f["h2"])
    row = w.table(ws, d.jira_aging, row + 1, widths={c: 15 for c in d.jira_aging}
                  | {"ops_team": 20})
    ws.merge_range(row, 0, row, 7, "Weekly trend", w.f["h2"])
    trend_row = row + 1
    row = w.table(ws, d.jira_trend, trend_row, widths={c: 15 for c in d.jira_trend}
                  | {"week_ending": 20})

    n = len(d.jira_trend)
    chart = w.wb.add_chart({"type": "column"})
    for j, (name, color) in enumerate([("raised", "#8FA9C8"), ("resolved", NAVY)], start=1):
        chart.add_series({"name": name.title(),
                          "categories": ["Ops SLA", trend_row + 1, 0, trend_row + n, 0],
                          "values": ["Ops SLA", trend_row + 1, j, trend_row + n, j],
                          "fill": {"color": color}})
    line = w.wb.add_chart({"type": "line"})
    line.add_series({"name": "SLA met %",
                     "categories": ["Ops SLA", trend_row + 1, 0, trend_row + n, 0],
                     "values": ["Ops SLA", trend_row + 1, 3, trend_row + n, 3],
                     "y2_axis": True, "line": {"color": RED, "width": 2.25},
                     "marker": {"type": "circle"}})
    chart.combine(line)
    chart.set_title({"name": "Tickets raised/resolved and SLA met"})
    chart.set_y_axis({"name": "Tickets"})
    line.set_y2_axis({"name": "SLA met", "num_format": "0%", "min": 0, "max": 1})
    chart.set_legend({"position": "bottom"})
    ws.insert_chart(3, 9, chart, {"x_scale": 1.3, "y_scale": 1.2})

    ws = w.sheet("Overdue Tickets")
    ws.write(0, 0, f"Open ops tickets past SLA as of {d.as_of:%d %b %Y}", w.f["title"])
    ws.write(1, 0, "Oldest first. Business Date = date the break relates to.",
             w.f["subtitle"])
    w.table(ws, d.jira_overdue, 3, autofilter=True,
            widths={"key": 12, "summary": 58, "ops_team": 18, "fund": 28,
                    "break_type": 16, "priority": 10, "age_bdays": 10})
    ws.freeze_panes(4, 1)


def _dq(w: _Writer, d: ReportData) -> None:
    ws = w.sheet("Data Quality")
    ws.write(0, 0, "Data quality checks", w.f["title"])
    if d.issues.empty:
        ws.write(2, 0, "All checks passed.", w.f["bold"])
        return
    df = d.issues.sort_values(["severity", "source", "check"])
    end = w.table(ws, df, 2, autofilter=True,
                  widths={"check": 26, "detail": 70, "record": 40, "source": 12})
    for sev in ("error", "warning"):
        ws.conditional_format(3, 0, end, 4, {
            "type": "formula", "criteria": f'=$A4="{sev.upper()}"', "format": w.f[sev]})
