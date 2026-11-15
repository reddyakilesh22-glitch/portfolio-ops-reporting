# Incident log

> **Simulated incidents.** This is a demo environment with synthetic data. Each
> incident below was deliberately reproduced (on the demo Jira site or in the
> source files) to exercise the maintenance workflow: GitHub issue, failing
> test, fix, PR, re-run. The failures and fixes themselves are real.

| ID | Date | Area | Symptom | Root cause | Fix |
|---|---|---|---|---|---|
| INC-001 | 2026-11-15 | Jira extract | Run stopped: `no Jira field named ['Break Amount (USD)']` | Admin renamed the custom field; resolver matched by name only | Pin field ids per site; follow renames by id with a DQ warning (#1) |

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
