# Runbook: Portfolio Ops Weekly Report

**Purpose:** Reconcile custodian holdings against fund accounting, report AUM by
fund, and report SLA performance of the ops tickets (Jira) that track breaks.
**Cadence:** Weekly, for the Friday business date (`as_of`), run Monday morning
before the 10:00 ops review.
**Owner:** Reporting analyst. **Consumers:** Ops leads (Reconciliation, NAV
Oversight, Trade Support, Corporate Actions, Reference Data), fund controllers.

---

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
5. **Publish** `output/<as_of>/portfolio_ops_report_<yyyymmdd>.xlsx` to the ops
   distribution, with one line on anything unusual.
6. **Commit the Jira snapshot** (`data/snapshots/jira_<as_of>.json`) so the week
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

Read the **last ERROR line** in `output/run.log`; messages name the file,
sheet, and record.

| Message contains | Likely cause | Action |
|---|---|---|
| `file not found` | Upstream file late or misnamed | Check the drop location; do not rename someone else's file without confirming it is the right date |
| `statement is as of X, expected Y` / `position dates` / `rate dates` | Wrong day's file delivered | Request the correct file. Never edit dates inside a source file |
| `expected column(s) [...] not found` | Vendor renamed a column | Confirm the meaning is unchanged with the vendor, then update `config/excel_layouts.yaml` |
| `no header row containing 'ISIN'` | Layout change or wrong file | Open the file; check it is the holdings statement |
| `unknown account sheet(s)` | New fund/account at custodian | Add it to `config/funds.yaml` once the fund code is confirmed |
| `lots of the same holding disagree on [...]` | Custodian reports one holding at several depositories with different prices/FX | Genuine data issue at the custodian: raise with them; do not pick one lot |
| `loaded base MV ... != statement total` | Rows dropped or mis-parsed | Look for rows with a blank or changed Account value; do not loosen the tolerance |
| `missing or non-positive rates` / `unknown_currency` | FX file incomplete | Request a corrected FX file. Do not substitute a rate yourself |
| `Custom field mapping failed` | Jira field renamed/deleted | See Incident INC-001 in `docs/INCIDENTS.md` |
| `Jira ... 401` | API token expired or revoked | Create a new token, update `.env`, never paste it in chat or tickets |
| `Jira ... 429` (repeated) | Rate limiting | The client retries automatically; if it still fails, re-run later |
| `No Jira snapshot on/after` | Running offline for a week with no snapshot | Run with `--jira live` |

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
| Credentials | `.env` (never committed); template `.env.example` |
| Planted-break answer keys (tests) | `data/expected/` |
