# Incident log

> **Simulated incidents.** This is a demo environment with synthetic data. Each
> incident below was deliberately reproduced (on the demo Jira site or in the
> source files) to exercise the maintenance workflow: GitHub issue, failing
> test, fix, PR, re-run. The failures and fixes themselves are real.

| ID | Date | Area | Symptom | Root cause | Fix |
|---|---|---|---|---|---|
| INC-001 | 2026-11-15 | Jira extract | Run stopped: `no Jira field named ['Break Amount (USD)']` | Admin renamed the custom field; resolver matched by name only | Pin field ids per site; follow renames by id with a DQ warning (#1) |
| INC-002 | 2026-11-15 | Custodian file | Re-run for 11/13 exited 2 with 9 `duplicate_position` errors | Custodian re-issued statement in v2 format, splitting holdings by depository; we assumed one row per position | Aggregate custodian lots to fund/ISIN with consistency checks (#3) |
| INC-003 | 2026-11-15 | Custodian file / CLI | Run crashed with a 25-line traceback: `time data '11/13/2026' does not match format '%d-%b-%Y'` | One hardcoded date format; parse error escaped as a raw `ValueError` | Configured unambiguous formats; clean one-line errors, traceback to run.log (#5) |

---

## INC-001: Jira custom field renamed (issue #1)

**Detected:** weekly run exited at extract with `FieldMappingError`; no report.
**Investigation:** `GET /rest/api/3/field` showed `customfield_10047` intact, same
type, display name changed from `Break Amount (USD)` to `Break Amount USD`.
**Root cause:** fields were resolved by display name only. Names are editable by
any Jira admin; ids are stable within a site.
**Fix:** `config/jira_field_lock.yaml` pins each field's id and type per site (keyed
by a hash of the site URL, so it never applies to a different Jira). A missing
name now falls back to the pinned id with a `field_renamed` warning in the Data
Quality tab. Still fails hard if the field was deleted, changed type, or if a
new field took over the old name.
**Verified:** reproduced live on the demo site; after the fix the run completed
with the warning; config updated to the new name and the warning cleared.
**Prevention:** 5 regression tests (rename, delete, type change, name reuse,
lock from another site).

## INC-002: custodian holdings split by depository (issue #3)

**Detected:** re-run for 2026-11-13 after the custodian re-issued its statement
exited with code 2: 9 `duplicate_position` ERRORs; nothing published.
**Investigation:** the new file ("format v2") adds a `Location` column. 9 holdings
appear twice, once per depository (e.g. NESN at SIX SIS and Euroclear). Per-row
quantities sum to the full holding and statement totals still tie, so the data
was right; the layout change itself was absorbed by header-name mapping.
**Root cause:** the loader assumed one custodian row per position. The DQ gate
correctly stopped the run; merging as-is would have crashed or double-counted.
**Fix:** `aggregate_lots` collapses custodian rows to fund/ISIN (sum quantity and
MV; price, currency, FX and descriptors must agree or the run stops). Locations
are kept and shown on the Breaks tab. Duplicates in fund accounting remain ERRORs.
**Verified:** same 11 breaks and $14,893,590 absolute difference as the original
statement; answer-key test passes on the v2 file.
**Also found:** a loader test (`renamed column fails loudly`) was passing for the
wrong reason after the new column shifted positions; it now finds the header by
name and asserts the exact missing column. And the report writer didn't handle
pandas `NA` values; fixed.

## INC-003: statement date format change (issue #5)

**Detected:** run exited 1 with a standard-library traceback; the message didn't
say which file or sheet, so the runbook couldn't be followed.
**Root cause:** the preamble date was parsed with one hardcoded format, and the
`ValueError` was never translated into our `SourceFileError`. More broadly, every
known failure type printed a full traceback on the console.
**Fix:** `as_of_formats` list in `config/excel_layouts.yaml` (kept unambiguous: a
test fails if both `%m/%d/%Y` and `%d/%m/%Y` are configured, since 11/12 would
silently become the wrong date). Parse failures name file, sheet, text and formats
tried. The CLI prints one ERROR line for known failures (`SourceFileError`,
`JiraError`, `ConfigError`, `FileNotFoundError`), points to the runbook, exits 1,
and writes the traceback to `run.log`. Unexpected exceptions keep their traceback
because they are bugs.
**Verified:** US-format date: run completes, 11 breaks. Unknown format: one clear
line, exit 1, traceback in `run.log`.
