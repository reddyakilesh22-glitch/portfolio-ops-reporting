# Portfolio Ops Reporting

Weekly operations report for an asset manager: reconciliation breaks between
the custodian and fund accounting, AUM by fund, and SLA metrics for the ops
tickets that track those breaks in Jira.

> Self-directed project modeling a financial-services reporting workflow.
> All data is synthetic.

## Status

| Stage | State |
|---|---|
| Jira extract (REST v3, `search/jql` paging, name-based field mapping) | done |
| Excel extract (custodian, fund accounting, FX workbooks) | done |
| Validation, reconciliation, AUM, Jira SLA metrics, Excel report | done |
| Runbook + 4 incidents fixed via issue -> test -> PR | done |
| AI commentary with numeric guardrails | next |

## Operations

- [Runbook](docs/RUNBOOK.md): weekly checklist, re-runs, troubleshooting by error message
- [Incident log](docs/INCIDENTS.md): what broke, root cause, fix, and the PR for each

## Run the weekly report

```bash
python -m opsreport run --as-of 2026-11-13 --prior 2026-11-06            # offline (Jira snapshot)
python -m opsreport run --as-of 2026-11-13 --prior 2026-11-06 --jira live  # pull Jira, save snapshot
```

Writes `output/<date>/portfolio_ops_report_<yyyymmdd>.xlsx` plus `breaks_*.csv`,
`data_quality_*.csv`, and `output/run.log`. Exit code 2 = data-quality ERROR
(nothing published; re-run with `--allow-errors` to force).

**Pipeline:** extract (Excel + Jira) -> validate -> reconcile -> AUM / SLA metrics -> report

| Report tab | Contents |
|---|---|
| Summary | KPI tiles, commentary, AUM by fund with week-over-week change, breaks by type |
| Breaks | Every custodian vs accounting break with type, materiality, likely cause |
| AUM | Securities MV, cash, accrued income, AUM, custodian comparison, chart |
| Ops SLA | Open/overdue by team, aging buckets, 8-week trend chart |
| Overdue Tickets | Open Jira tickets past SLA, oldest first |
| Data Quality | Every ERROR / WARNING with the record it applies to |
| Run Info | Inputs with SHA-256, row counts, git commit, versions (audit trail) |

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env              # fill in Jira URL, email, API token
python -m scripts.jira_setup      # company-managed project + custom fields (idempotent)
python -m scripts.seed_jira       # 160 synthetic break tickets (--reset to reseed)
python -m scripts.generate_excel_data   # synthetic workbooks + answer keys (already committed)
pytest -q                         # offline; no Jira credentials needed
```

## Jira design notes

- **Field mapping by name, not id.** `config/jira_fields.yaml` maps logical names
  to Jira display names; ids (`customfield_XXXXX`) are resolved at runtime. A
  renamed or missing field stops the run with a clear error instead of producing
  empty columns. Old names can be kept under `aliases`.
- **Enhanced search endpoint.** Uses `POST /rest/api/3/search/jql` with
  `nextPageToken`; the legacy `/rest/api/3/search` was retired by Atlassian.
- **Business dates.** Jira won't back-date `created`/`resolutiondate`, so break
  history uses `Business Date` / `Resolved Business Date`. Timestamps are
  normalized to UTC.
- **Retries.** 429/5xx responses are retried, honoring `Retry-After`.

## Source files (`data/raw/<date>/`)

| File | Mimics | Traps the loader handles |
|---|---|---|
| `custodian_holdings_*.xlsx` | Custodian bank holdings statement, one sheet per fund account | Title rows above the header, numbers stored as text, trailing spaces in ISINs, lowercase currency codes, Total row and footer |
| `fund_accounting_positions_*.xlsx` | Fund accounting system extract | Different column names, separate accrued income, its own FX rates |
| `fx_rates_*.xlsx` | 4pm closing FX rates (units per USD) | Missing or duplicate currencies |

`data/expected/breaks_<date>.csv` is the answer key of breaks planted between
custodian and accounting (quantity, price, missing trade, corporate action, FX,
cash, static data). Tests assert the reconciliation finds exactly these.

## Excel design notes

- **Header-name matching.** Columns map by header text (`config/excel_layouts.yaml`),
  so an inserted column can't shift values; a renamed column fails with the list
  of headers actually present.
- **Header row discovery.** The custodian header row is found by scanning for `ISIN`.
- **Date guard.** Each file's internal as-of date must match the run date.
- **Control total.** Loaded custodian market value must tie to the statement's own
  Total row within $1, so dropped or mis-parsed rows stop the run.

## Reconciliation and metrics design notes

- **Classification order.** Missing trade -> cash -> static data -> corporate action ->
  quantity -> price -> FX -> unexplained. Order matters: a currency mismatch also
  moves base MV, so it must be caught as static data before the FX rule.
- **Quantity and value are checked separately.** A 2:1 split shows ~$0 MV difference;
  only the quantity check catches it.
- **Answer-key tests.** The reconciliation must return exactly the planted breaks
  for both dates: no misses, no false positives, and sub-tolerance rounding ignored.
- **Point-in-time SLA metrics.** "Open as of D" uses business dates, so re-running a
  past week reproduces what was reported that week.
- **Offline reproducibility.** Every live Jira pull saves a sanitized snapshot
  (no site URLs or account ids); runs default to the snapshot.
