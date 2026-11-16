# Runbook: Portfolio Ops Reporting

**Purpose:** Reconcile custodian holdings against fund accounting every
business day, keep the history, and report it: a daily exceptions file for
analysts, a weekly report for ops leads, and a dashboard for leadership.
**Cadence:** Daily run each business morning for the previous business day.
Weekly report for the Friday date, Monday before the 10:00 ops review.
**Owner:** Reporting analyst. **Consumers:** Ops analysts and leads
(Reconciliation, NAV Oversight, Trade Support, Corporate Actions, Reference
Data), fund controllers, operations leadership.

---

## 0. Daily run checklist

1. **Confirm inputs are present** for the business date:
   ```
   data/daily/<date>/custodian_holdings_<yyyymmdd>.xlsx
   data/daily/<date>/fund_accounting_positions_<yyyymmdd>.xlsx
   data/daily/<date>/fx_rates_<yyyymmdd>.xlsx
   data/daily/<date>/pending_trades_<yyyymmdd>.xlsx
   ```
2. **Run:**
   ```bash
   python -m opsreport daily --as-of <date>
   ```
   | Exit | Meaning | Action |
   |---|---|---|
   | 0 | History updated, exceptions file written | Step 3 |
   | 2 | Data-quality ERROR. The day is saved as FAILED with its errors; nothing published | Section 3, "DQ errors"; fix and re-run the same command |
   | 1 | Missing or unreadable file (also saved as FAILED), or a setup problem | Section 3, match the message |
3. **Sanity-check** `output/daily/<date>/ops_exceptions_<yyyymmdd>.xlsx`:
   - **Expected** tab: every row settles on a future date. A row whose
     settlement date has passed would be on Action Today instead; if not, stop
     and investigate.
   - **Action Today**: overdue tickets are at the top. "No ticket yet" rows need
     a ticket raised today.
   - A sudden jump in actionable breaks usually has one shared cause (an FX
     rate, one fund's file): check before raising many tickets.
4. **Send** the exceptions file to the ops analysts' channel.

### Backfill and re-running a day
```bash
python -m opsreport backfill --from 2026-05-18 --to 2026-11-13   # every business day
python -m opsreport daily --as-of 2026-11-04                      # one corrected day
```
- Re-running a day replaces that day's rows; it is safe to repeat.
- Break IDs are carried forward from the previous successful day. After
  correcting an *earlier* day, re-run the backfill from that day to the latest
  so later days chain their IDs from the corrected data.
- Backfill exits 2 if any day failed and lists them; the others are saved.

### Dashboard
```bash
streamlit run dashboard/app.py                         # reads data/history.db
OPSREPORT_DB=/path/to/history.db streamlit run dashboard/app.py
```
It shows whatever is in the history: run the daily job first. The **Run
health** tab shows every business day as OK, FAILED or missing; check it
before presenting numbers. "As of" on the dashboard is the last *successful*
run in the selected range.

## 1. Weekly run checklist

1. **Confirm inputs are present** for the Friday date:
   ```
   data/raw/<as_of>/custodian_holdings_<yyyymmdd>.xlsx
   data/raw/<as_of>/fund_accounting_positions_<yyyymmdd>.xlsx
   data/raw/<as_of>/fx_rates_<yyyymmdd>.xlsx
   ```
2. **Run** (pulls Jira live and saves a snapshot for reproducibility):
   ```bash
   source .venv/bin/activate
   python -m opsreport run --as-of <friday> --prior <previous friday> --jira live
   ```
   If you need to run again the same morning (say, after a corrected file
   arrives), drop `--jira live`: the run reuses the snapshot, so the ticket
   numbers don't shift between runs. Use `--refresh-snapshot` only when you
   deliberately want Jira as it is now.
3. **Check the exit code and log** (`output/run.log`):
   | Exit | Meaning | Action |
   |---|---|---|
   | 0 | Report written | Continue to step 4 |
   | 2 | Data-quality ERROR; nothing published | Section 3, "DQ errors" |
   | 1 | Crash or source-file problem | Section 3, match the message |
4. **Sanity-check the Summary tab before sending:**
   - Total AUM moved by a believable amount (more than ±5% WoW needs a reason).
   - Each fund's `Acct vs Custodian MV` is explained by the Breaks tab.
   - Break count isn't zero (zero usually means a file was duplicated, not a perfect week).
   - Data Quality tab: read every WARNING; none should be new without explanation.
5. **If AI commentary was used** (`--commentary local-llm`): read every bullet
   against the Summary numbers. The guardrail checks numbers, not judgement.
   If the label says `template (...)`, the AI draft was rejected or Ollama was
   down; the template is fine to send. Details: `commentary_audit_<date>.json`.
6. **Publish** `output/<as_of>/portfolio_ops_report_<yyyymmdd>.xlsx` to the ops
   distribution, with one line on anything unusual.
7. **Commit the Jira snapshot** (`data/snapshots/jira_<as_of>.json`) so the week
   can be reproduced later.

## 2. Re-running a past week

Metrics are point-in-time, so a re-run reproduces what was reported:
```bash
python -m opsreport run --as-of 2026-11-06            # uses the snapshot on/after that date
python -m opsreport run --as-of 2026-11-06 --snapshot data/snapshots/jira_2026-11-13.json
```
If a re-run gives different numbers than were published, compare the
**Run Info** tab of both workbooks first: input SHA-256s, snapshot, git commit.

## 3. Troubleshooting

Read the **ERROR lines** on the console (also in `output/run.log`); messages name
the file, sheet, and record. Known failures print one line and exit 1; the full
traceback is in `run.log` if you need it. A raw traceback on the console means an
unexpected error (a bug): open an issue with the log attached.

| Message contains | Likely cause | Action |
|---|---|---|
| `file not found` | Upstream file late or misnamed | Check the drop location; do not rename someone else's file without confirming it is the right date |
| `statement is as of X, expected Y` / `position dates` / `rate dates` | Wrong day's file delivered | Request the correct file. Never edit dates inside a source file |
| `expected column(s) [...] not found` | Vendor renamed a column | Confirm the meaning is unchanged with the vendor, then update `config/excel_layouts.yaml` |
| `can't read statement date` | Custodian changed the date format | If the date is unambiguous, add the format to `as_of_formats`; never add a day-first format alongside `%m/%d/%Y` |
| `no header row containing 'ISIN'` | Layout change or wrong file | Open the file; check it is the holdings statement |
| `unknown account sheet(s)` | New fund/account at custodian | Add it to `config/funds.yaml` once the fund code is confirmed |
| `lots of the same holding disagree on [...]` | Custodian reports one holding at several depositories with different prices/FX | Genuine data issue at the custodian: raise with them; do not pick one lot |
| `loaded base MV ... != statement total` | Rows dropped or mis-parsed | Look for rows with a blank or changed Account value; do not loosen the tolerance |
| `missing or non-positive rates` / `unknown_currency` | FX file incomplete | Request a corrected FX file. Do not substitute a rate yourself |
| `Custom field mapping failed` | Jira field renamed/deleted | See Incident INC-001 in `docs/INCIDENTS.md` |
| `has N non-numeric value(s): 'TBC'` | Text where a number belongs in a source file | Ask the provider for the value; do not type a number into their file |
| `Jira ... network error after N attempt(s)` | No connection, VPN down, or Jira unreachable | Check the connection and re-run; or run without `--jira live` to use the saved snapshot |
| `calendar_gap` (DQ warning) | An open ticket was raised in a year missing from the holiday calendar | Add that year to `config/calendar.yaml`; until then that ticket's age is slightly overstated |
| `forced_run` (DQ warning) | Report was produced with `--allow-errors` | For investigation only; say so if it is shared |
| `Jira ... 401` | API token expired or revoked | Create a new token, update `.env`, never paste it in chat or tickets |
| `Jira ... 429` (repeated) | Rate limiting | The client retries automatically; if it still fails, re-run later |
| `Holiday calendar ... has no entries for [YYYY]` | The report date's own year is not in the calendar | Add the exchange's published closures to `config/calendar.yaml` (PR) |
| `A Jira snapshot for <date> already exists` | `--jira live` used for a week that already has a saved pull | Re-run without `--jira live`; add `--refresh-snapshot` only to replace it on purpose |
| `duplicate_lot` (DQ error) | The custodian statement lists the same fund, security and location twice | Ask the custodian for a corrected statement; do not delete the row yourself |
| `No Jira snapshot on/after` | Running offline for a week with no snapshot | Run with `--jira live` |
| `pending_trades file not found` (daily) | Trade report late from the order management system | Request it. Do not run without it: every trade-date break would show as actionable |
| `report dates [...], expected` / `trade date is after the report date` (pending trades) | Wrong day's trade report | Request the correct file |
| `'Side' must be Buy or Sell` / `'Quantity' must be a positive number` / `trade id repeated` | Malformed trade report | Raise with the trading-system owners; do not edit the file |
| `pending_trade_without_break` (DQ warning) | An open trade on a position where the two records agree | Either the trade report is stale (trade already settled) or the trade was never booked in accounting: check with Trade Support |
| `Settlement failed: ... was due` (Action Today) | Shares or cash didn't arrive on the settlement date | Chase the custodian or executing broker; the ticket is Trade Support's |
| `Unknown (ticket data incomplete)` (ticket status) | The linked Jira ticket is Done with no Resolved Business Date | Fill in the date in Jira; the ticket is also on the Data Quality tab |
| `no_ticket_snapshot` (DQ warning) | The Jira snapshot file is missing | Breaks are still reported, without tickets; restore the snapshot |
| `HistoryError: ... schema version` | `history.db` was built by a different version of the code | Rebuild: delete it and run `backfill` (inputs are kept, so nothing is lost) |
| Dashboard says `No history found` | `data/history.db` not built yet, or `OPSREPORT_DB` points elsewhere | Run `backfill`, or fix the path |

### DQ errors (exit 2)
The run stops before publishing when inputs can't be trusted (bad ISIN,
duplicate position, blank required field, currency without an FX rate). The log
lists every ERROR with its record. Fix at source and re-run. `--allow-errors`
exists for investigation only; a report produced with it must not be sent
without saying so.

### Reconciliation looks wrong
- **Break count jumped:** check the Breaks tab for one shared cause (one FX rate,
  one fund's file) before raising individual tickets.
- **Break type "Unexplained":** outside tolerance but no rule matched; investigate
  manually and add a rule + test if it recurs.
- **Tolerances** live in `config/recon.yaml`. Changing them needs a PR with the
  reason; never change them to make a break disappear.

## 4. Making a change

1. Open a GitHub issue describing the symptom and impact.
2. Branch from `main`, reproduce the problem in a failing test first.
3. Fix, run `ruff check . && pytest -q`, open a PR referencing the issue.
4. After merge, re-run the affected week and compare against what was published.
5. Record production incidents in `docs/INCIDENTS.md`.

## 5. Reference

| Item | Location |
|---|---|
| Field mappings (Jira) | `config/jira_fields.yaml` |
| Source layouts (Excel) | `config/excel_layouts.yaml` |
| Funds and custodian accounts | `config/funds.yaml` |
| Tolerances, materiality | `config/recon.yaml` |
| Business-day calendar | `config/calendar.yaml` (update each December) |
| Credentials | `.env` (never committed); template `.env.example` |
| Planted-break answer keys (tests) | `data/expected/` (weekly), `data/daily/expected/` (daily, generated) |
| Pending-trades layout | `config/excel_layouts.yaml` (`pending_trades`) |
| Daily history | `data/history.db` (rebuilt by `backfill`; not in git) |
| Dashboard settings | `.streamlit/config.toml` |
