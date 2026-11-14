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
| Excel extract (custodian + accounting workbooks) | next |
| Validation, reconciliation, Excel report | planned |
| AI commentary with numeric guardrails | planned |

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env              # fill in Jira URL, email, API token
python -m scripts.jira_setup      # company-managed project + custom fields (idempotent)
python -m scripts.seed_jira       # 160 synthetic break tickets (--reset to reseed)
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
