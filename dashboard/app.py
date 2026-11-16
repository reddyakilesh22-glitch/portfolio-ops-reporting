"""Leadership dashboard: how the operation is doing, daily, weekly or monthly.

    streamlit run dashboard/app.py

Reads only the history store (data/history.db, or $OPSREPORT_DB). Every
number comes from opsreport.dashboard_data, which is unit-tested; this file
only lays out filters, tiles and charts.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from opsreport import dashboard_data as dd  # noqa: E402
from opsreport.report.excel_report import _kind, _label  # noqa: E402
from opsreport.transform.aum import fund_names  # noqa: E402

DB = Path(os.environ.get("OPSREPORT_DB", dd.DB_PATH))

# Validated categorical palette (light, dark): fixed order, colour follows the
# entity and never its rank. Status colours are reserved for run status.
PALETTE = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9"],
}
STATUS = {"OK": "#0ca30c", "Missing": "#ec835a", "FAILED": "#d03b3b"}
SURFACE = {"light": "#ffffff", "dark": "#0e1117"}
PRESETS = ["Last 30 days", "Last 3 months", "Last 6 months", "Month to date", "Custom"]

st.set_page_config(page_title="Portfolio Ops Dashboard", layout="wide")
mode = "dark" if getattr(st.context.theme, "type", None) == "dark" else "light"
series, surface = PALETTE[mode], SURFACE[mode]


@st.cache_data(ttl=300)
def load(path: str, mtime: float) -> dd.History:     # mtime: reload when the file changes
    return dd.load(Path(path))


if not DB.exists():
    st.title("Portfolio Operations")
    st.error(f"No history found at `{DB}`. Build it first:")
    st.code("python -m scripts.generate_daily_data\n"
            "python -m opsreport backfill --from 2026-05-18 --to 2026-11-13")
    st.stop()

h = load(str(DB), DB.stat().st_mtime)
if h.last is None:
    st.error("The history has no successful runs yet.")
    st.stop()
names = fund_names()


# ------------------------------------------------------------------ helpers
def money(x: float | None) -> str:
    if x is None or pd.isna(x):
        return "n/a"
    return f"${x / 1e9:,.2f}B" if abs(x) >= 1e9 else f"${x / 1e6:,.1f}M"


def label(periods: pd.Series, grain: str) -> pd.Series:
    fmt = {"Daily": "%d %b", "Weekly": "w/e %d %b", "Monthly": "%b %Y"}[grain]
    return pd.to_datetime(periods).dt.strftime(fmt)


PCT_COLUMNS = {"sla_met_4wk", "on_time_pct", "share", "exposure_pct_of_aum"}


def table(df: pd.DataFrame, key: str | None = None) -> None:
    """A dataframe with readable headers, $ / % formats and blanks, not 'None'."""
    cfg = {}
    dates = [c for c in df.columns if df[c].map(lambda v: isinstance(v, date)).any()]
    for col in df.columns:
        name = _label(col)
        if col in dates:
            cfg[col] = st.column_config.DateColumn(name, format="YYYY-MM-DD")
        elif col in PCT_COLUMNS:
            cfg[col] = st.column_config.NumberColumn(name, format="percent")
        elif _kind(col) == "money" and pd.api.types.is_numeric_dtype(df[col]) \
                and col not in PCT_COLUMNS:
            cfg[col] = st.column_config.NumberColumn(name, format="dollar")
        else:
            cfg[col] = st.column_config.Column(name)
    text = [c for c in df.columns
            if not pd.api.types.is_numeric_dtype(df[c]) and c not in dates]
    shown = df.astype({c: object for c in text})
    shown[text] = shown[text].where(shown[text].notna(), "")
    for c in dates:                     # real datetimes: empty cells render blank
        shown[c] = pd.to_datetime(shown[c])
    st.dataframe(shown, hide_index=True, width="stretch", column_config=cfg, key=key)


def show_data(df: pd.DataFrame, key: str) -> None:
    with st.expander("Show data"):
        table(df, key)


def x_axis(grain: str) -> alt.Axis:
    """Same period axis everywhere: flat labels, thinned when crowded."""
    return alt.Axis(labelAngle=-45 if grain == "Daily" else 0, labelOverlap="greedy")


def type_scale() -> alt.Scale:
    return alt.Scale(domain=dd.BREAK_TYPES, range=series)


# ------------------------------------------------------------------ header
st.title("Portfolio Operations")
st.caption(f"Reconciliation breaks, ops tickets and AUM from the daily runs · history "
           f"{h.first:%d %b %Y} to {h.last:%d %b %Y} · synthetic data")

# One row of filters above everything they control.
c = st.columns([1.6, 1.2, 1.4, 1.4, 1.4, 1.1])
grain = c[0].segmented_control("Period", dd.GRAINS, default="Weekly") or "Weekly"
preset = c[1].selectbox("Date range", PRESETS, index=2)
fund_sel = c[2].multiselect("Fund", list(names), format_func=lambda k: names[k],
                            placeholder="All funds")
team_sel = c[3].multiselect("Ops team", sorted(h.tickets["ops_team"].dropna().unique()),
                            placeholder="All teams")
type_sel = c[4].multiselect("Break type", dd.BREAK_TYPES, placeholder="All types")
mat_sel = c[5].multiselect("Materiality", dd.MATERIALITY, placeholder="All")
if preset == "Custom":
    default_start, _ = dd.preset_range("Last 3 months", h.last)
    picked = st.date_input("Custom range", (max(default_start, h.first), h.last),
                           min_value=h.first, max_value=h.last)
    start, end = (picked if isinstance(picked, tuple) and len(picked) == 2
                  else (h.first, h.last))
else:
    start, end = dd.preset_range(preset, h.last)
start = max(start, h.first)
f = dd.Filters(start, end, grain, tuple(fund_sel), tuple(team_sel), tuple(type_sel),
               tuple(mat_sel))

k = dd.kpis(h, f)
now, prev = k["now"], k["previous"]
if now["as_of"] is None:
    st.warning("No successful daily run in this range.")
    st.stop()
st.caption(f"As of **{now['as_of']:%A %d %B %Y}** (last run in range {start:%d %b} to "
           f"{end:%d %b}). Changes compare with the previous {(end - start).days + 1} days.")


def delta(key: str, fmt) -> str | None:
    a, b = now[key], prev[key]
    if a is None or b is None:
        return None
    return fmt(a - b)


tiles = st.columns(6)
tiles[0].metric("Open breaks", now["open_breaks"],
                delta("open_breaks", lambda x: f"{x:+d}"), delta_color="inverse",
                help="Actionable breaks open on the as-of day (timing breaks excluded)",
                border=True)
tiles[1].metric("Exposure at risk", money(now["exposure_usd"]),
                delta("exposure_usd", lambda x: f"{'+' if x >= 0 else '-'}{money(abs(x))}"),
                delta_color="inverse", border=True,
                help="Sum of exposure on open actionable breaks")
tiles[2].metric(f"Aged over {dd.AGED_DAYS} days", now["aged_breaks"],
                delta("aged_breaks", lambda x: f"{x:+d}"), delta_color="inverse",
                border=True, help="Open actionable breaks older than 5 business days")
sla_now, sla_prev = now["sla_on_time"], prev["sla_on_time"]
tiles[3].metric("SLA on time", "n/a" if sla_now is None else f"{sla_now:.0%}",
                None if sla_now is None or sla_prev is None
                else f"{(sla_now - sla_prev) * 100:+.0f} pts", border=True,
                help=f"Share of the {now['resolved']} tickets resolved in the range that "
                     "met their due date")
tiles[4].metric("Overdue tickets", now["overdue_tickets"],
                delta("overdue_tickets", lambda x: f"{x:+d}"), delta_color="inverse",
                border=True, help="Open tickets past their due date on the as-of day")
tiles[5].metric("AUM", money(now["aum"]),
                delta("aum", lambda x: f"{'+' if x >= 0 else '-'}{money(abs(x))}"),
                delta_color="off", border=True, help="Fund accounting, incl. accrued income")

tabs = st.tabs(["Overview", "Breaks", "Tickets & SLA", "Funds", "Run health"])

# ---------------------------------------------------------------- overview
with tabs[0]:
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Open breaks over time")
        unit = {"Daily": "day", "Weekly": "week", "Monthly": "month"}[grain]
        st.caption(f"Average actionable breaks open per business day, by {unit}")
        tr = dd.breaks_trend(h, f)
        if tr.empty:
            st.info("No actionable breaks in this selection.")
        else:
            tr = tr.assign(label=label(tr["period"], grain))
            order = list(dict.fromkeys(tr.sort_values("period")["label"]))
            chart = alt.Chart(tr).mark_bar(stroke=surface, strokeWidth=1.5).encode(
                x=alt.X("label:O", sort=order, title=None, axis=x_axis(grain)),
                y=alt.Y("sum(avg_open):Q", title="Avg open per day"),
                color=alt.Color("break_type:N", scale=type_scale(), title=None,
                                legend=alt.Legend(orient="top", columns=4)),
                order=alt.Order("break_type:N"),
                tooltip=[alt.Tooltip("label:O", title="Period"),
                         alt.Tooltip("break_type:N", title="Type"),
                         alt.Tooltip("avg_open:Q", title="Avg open / day", format=".1f"),
                         alt.Tooltip("run_days:Q", title="Business days")])
            st.altair_chart(chart.properties(height=320), width="stretch")
            show_data(tr.pivot_table(index="label", columns="break_type", values="avg_open",
                                     sort=False).reset_index(), "trend")
    with right:
        st.subheader("Top recurring causes")
        st.caption("Separate breaks per root cause in the range: what to fix upstream")
        rc = dd.recurring_causes(h, f)
        if rc.empty:
            st.info("No actionable breaks in this selection.")
        else:
            rc = rc.head(7).assign(share_label=lambda d: (d["share"] * 100).round(0)
                                   .astype(int).astype(str) + "%")
            base = alt.Chart(rc).encode(
                y=alt.Y("cause:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
                x=alt.X("breaks:Q", title="Breaks"),
                tooltip=[alt.Tooltip("cause:N", title="Cause"),
                         alt.Tooltip("breaks:Q", title="Breaks"),
                         alt.Tooltip("break_days:Q", title="Break-days open"),
                         alt.Tooltip("share:Q", title="Share", format=".0%")])
            bars = base.mark_bar(color=series[0], cornerRadiusEnd=4, height=16)
            text = base.mark_text(align="left", dx=4).encode(text="share_label:N")
            st.altair_chart((bars + text).properties(height=320), width="stretch")
            show_data(rc.drop(columns="share_label"), "causes")

    left, right = st.columns(2)
    with left:
        st.subheader("Tickets fixed on time")
        st.caption("Share of tickets resolved in each period that met their due date. "
                   "Periods with few tickets swing widely; hover for the count, or "
                   "switch to Monthly for the trend.")
        sl = dd.sla_trend(h, f)
        if sl.empty:
            st.info("No tickets resolved in this selection.")
        else:
            sl = sl.assign(label=label(sl["period"], grain))
            order = list(sl.sort_values("period")["label"])
            base = alt.Chart(sl).encode(
                x=alt.X("label:O", sort=order, title=None, axis=x_axis(grain)),
                y=alt.Y("on_time_pct:Q", title="On time", scale=alt.Scale(domain=[0, 1]),
                        axis=alt.Axis(format="%")),
                tooltip=[alt.Tooltip("label:O", title="Period"),
                         alt.Tooltip("on_time_pct:Q", title="On time", format=".0%"),
                         alt.Tooltip("resolved:Q", title="Resolved"),
                         alt.Tooltip("on_time:Q", title="Met due date")])
            st.altair_chart((base.mark_line(color=series[0], strokeWidth=2)
                             + base.mark_point(color=series[0], filled=True, size=64,
                                               stroke=surface, strokeWidth=2))
                            .properties(height=260), width="stretch")
            show_data(sl.drop(columns="period"), "sla")
    with right:
        st.subheader("Exposure at risk over time")
        st.caption("Average exposure on open actionable breaks per business day")
        ex = dd.exposure_trend(h, f)
        if ex.empty:
            st.info("No runs in this selection.")
        else:
            ex = ex.assign(label=label(ex["period"], grain))
            order = list(ex.sort_values("period")["label"])
            chart = alt.Chart(ex).mark_bar(color=series[0], cornerRadiusEnd=4).encode(
                x=alt.X("label:O", sort=order, title=None, axis=x_axis(grain)),
                y=alt.Y("avg_exposure_usd:Q", title="USD", axis=alt.Axis(format="$~s")),
                tooltip=[alt.Tooltip("label:O", title="Period"),
                         alt.Tooltip("avg_exposure_usd:Q", title="Avg exposure",
                                     format="$,.0f")])
            st.altair_chart(chart.properties(height=260), width="stretch")
            show_data(ex.drop(columns="period"), "exposure")

    with st.expander("How to read this dashboard"):
        st.markdown(f"""
- **As of** is the last successful daily run in the selected range. Tiles show the
  position on that day; arrows compare with the same-length range just before.
- **Open breaks** are *actionable* breaks. Breaks explained by trades that haven't
  reached their settlement date are **Expected** and excluded
  ({now['expected']} on the as-of day).
- **Exposure** is the dollar difference, or the whole position for an unprocessed
  corporate action (the values agree but the share count is wrong).
- **Weekly / Monthly** views are the same daily rows grouped: open breaks are averaged
  per business day; AUM is the last day of each period.
- **Recurring causes** count separate breaks, not days, so one stuck break doesn't
  dominate. The vendor named on price breaks is the accounting pricing source.
""")

# ------------------------------------------------------------------ breaks
with tabs[1]:
    ob = dd.open_breaks(h, f)
    st.subheader(f"Breaks open on {now['as_of']:%d %b %Y}")
    cols = ["status", "fund_code", "ticker", "break_type", "materiality", "exposure_usd",
            "days_open", "ticket_key", "ticket_status", "ticket_due", "ops_team",
            "explanation", "likely_cause", "pricing_source", "first_seen", "break_id"]
    table(ob[cols])
    st.download_button("Download CSV", ob[cols].to_csv(index=False),
                       f"breaks_{now['as_of']}.csv", "text/csv")
    st.subheader("Repeat offenders")
    st.caption("Securities that broke more than once in the range")
    table(dd.repeat_offenders(h, f))

# ----------------------------------------------------------------- tickets
with tabs[2]:
    team_tbl, aging, overdue = dd.teams(h, f)
    st.subheader(f"Ops teams as of {end:%d %b %Y}")
    if team_tbl.empty:
        st.info("No tickets in this selection.")
    else:
        table(team_tbl)
        st.subheader("Age of open tickets (business days)")
        table(aging)
        st.subheader(f"Overdue tickets ({len(overdue)})")
        table(overdue)

# ------------------------------------------------------------------- funds
with tabs[3]:
    ft = dd.funds(h, f)
    st.subheader(f"Funds as of {now['as_of']:%d %b %Y}")
    table(ft)
    at = dd.aum_trend(h, f)
    if len(at):
        st.subheader("AUM by fund")
        st.caption(f"Value on the last business day of each {grain.lower()} period")
        at = at.assign(label=label(at["period"], grain))
        order = list(dict.fromkeys(at.sort_values("period")["label"]))
        fund_order = [names[c] for c in names]
        base = alt.Chart(at).encode(
            x=alt.X("label:O", sort=order, title=None, axis=x_axis(grain)),
            y=alt.Y("aum:Q", title="AUM (USD)", axis=alt.Axis(format="$~s"),
                    scale=alt.Scale(zero=False)),
            color=alt.Color("fund_name:N", title=None,
                            scale=alt.Scale(domain=fund_order, range=series[:5]),
                            legend=alt.Legend(orient="top", labelLimit=300)),
            tooltip=[alt.Tooltip("label:O", title="Period"),
                     alt.Tooltip("fund_name:N", title="Fund"),
                     alt.Tooltip("aum:Q", title="AUM", format="$,.0f")])
        st.altair_chart((base.mark_line(strokeWidth=2)
                         + base.mark_point(filled=True, size=64, stroke=surface,
                                           strokeWidth=2)).properties(height=320),
                        width="stretch")
        show_data(at.drop(columns="period"), "aum")

# -------------------------------------------------------------- run health
with tabs[4]:
    rh = dd.run_health(h, f)
    m = st.columns(4)
    m[0].metric("Days run OK", f"{rh['ok_days']} of {rh['expected_days']}", border=True)
    m[1].metric("Failed days", len(rh["failed"]), border=True)
    m[2].metric("Missing days", len(rh["missing_days"]), border=True,
                help="Business days with no run recorded at all")
    m[3].metric("Last run", f"{h.last:%a %d %b}", border=True)
    strip = pd.concat([
        rh["runs"][["run_date", "status", "message"]],
        pd.DataFrame({"run_date": rh["missing_days"], "status": "Missing",
                      "message": "no run recorded"})], ignore_index=True)
    if len(strip):
        strip = strip.assign(day=pd.to_datetime(strip["run_date"]), lane="runs",
                             message=strip["message"].fillna(""))
        chart = alt.Chart(strip).mark_rect(stroke=surface, strokeWidth=2).encode(
            x=alt.X("yearmonthdate(day):O", title=None,
                    axis=alt.Axis(labels=False, ticks=False)),
            y=alt.Y("lane:N", title=None, axis=None),
            color=alt.Color("status:N", title="Run status",
                            scale=alt.Scale(domain=list(STATUS), range=list(STATUS.values())),
                            legend=None),
            tooltip=[alt.Tooltip("day:T", title="Date", format="%a %d %b %Y"),
                     alt.Tooltip("status:N", title="Status"),
                     alt.Tooltip("message:N", title="Detail")])
        st.caption("One block per business day: :green[■] OK · :orange[■] missing · "
                   ":red[■] failed. Hover for details.")
        st.altair_chart(chart.properties(height=36), width="stretch")
    if len(rh["failed"]):
        st.subheader("Failed days")
        table(rh["failed"])
    st.subheader("Data-quality findings")
    st.caption("How many days each rule fired in the range")
    table(rh["rules"])
    st.subheader("Run log")
    table(rh["runs"])

st.caption(f"Source: `{DB.name}` · today {date.today():%d %b %Y}")
