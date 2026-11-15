# AI-drafted commentary

The Summary tab's commentary can be drafted by a local LLM instead of the fixed
template. It is opt-in, and it never decides a number.

```bash
python -m opsreport run --as-of 2026-11-13 --prior 2026-11-06 --commentary local-llm
```

## Design

```
report tables ──► atomic facts ──► local LLM (Ollama) ──► draft ──► guardrail ──► report
 (Python)         one number each   phrases only                     │ fail
                  + keywords                                         ▼
                                                     retry once with the specific problems
                                                                     │ fail again / LLM down
                                                                     ▼
                                                           deterministic template
```

| Principle | How |
|---|---|
| The model never calculates | Python computes and formats every figure; the model only phrases a fact sheet |
| Data stays on the machine | Model runs locally through Ollama (default `llama3.2`); no API key, no network |
| Every number is traceable | Guardrail checks each number against the fact sheet (below) |
| Fail safe | One retry with specific feedback, then the template; report is never blocked |
| Auditable | `commentary_audit_<date>.json` keeps every draft and every rejection; Run Info records the source |
| Human in the loop | Report labels the section "AI draft ... Analyst review required before sending" |

## Guardrail

Each fact carries one value plus what identifies it, e.g.
`Reconciliation team overdue tickets = 7` with keywords *overdue / past / late*.
The same value can belong to two facts (total AUM -0.4%, one fund +0.4%), so a
number in the draft passes if it **fits at least one** fact with that value.
For every number (digits, or *one* to *twenty* spelled out), fitting means:

| Rule | Catches |
|---|---|
| **Value**: equals a fact's value; rounding and a dropped sign are allowed | Invented numbers |
| **Label**: a keyword of the fact is within a few words, or its fund/ticker is in the sentence. Unit words like "days" are not labels | "35 business days of delay" when 35 is the oldest ticket's age |
| **Subject**: if the number's clause names a fund or ticker, it is the fact's | "GEF fell 4.3% while ISC rose 1.3%" with the figures swapped |
| **Rival label** (counts): no other count's label sits closer when its own is absent | "Reconciliation with 7 open tickets" when 7 are overdue |
| **Qualifier**: a total over everything isn't given a subset word; an actual isn't called a target | "3 high-materiality breaks totaling $14.9M"; "the 40% SLA target" |
| **Direction**: a rise isn't called a fall | "AUM rose $1.5M" when it fell |
| **Rank**: only the largest break is called the largest | "The largest break is the $3.4M ..." (it is second) |

Checked as text, because no number is involved:

| Rule | Catches |
|---|---|
| Nothing is played down | "immaterial", "needs no action" |
| A stated cause must echo a likely cause in the facts | "caused by a custodian system outage" |
| Ticket keys exist; 1-6 bullets | Invented ticket ids |

## What it caught

Every row is either real `llama3.2` output from building this, or a case
written during the code audit. All are tests in `tests/test_ai_commentary.py`,
alongside correct sentences that must **pass**, so the checker is held to both
directions.

| Draft | Problem |
|---|---|
| "three high-materiality ... breaks totaling **$14.9M**" (llama) | $14.9M is all 11 breaks |
| "Reconciliation with **7 open** tickets" (llama) | 7 are overdue; 12 are open |
| "12 past SLA, totaling **35 business days of delay**" (llama) | 35 is the oldest ticket's age |
| "to meet the **40%** SLA met this week **target**" (llama) | 40% is the result, not a target |
| "The **largest** materiality break is the $3.4M ISC 4385" (llama) | It is the second largest |
| "Total AUM **rose** $1.5M" (audit) | It fell |
| "GEF fell 4.3% while ISC rose 1.3%" (audit) | Swapped |
| "... which is **immaterial**" / "**caused by** a system outage" (audit) | Unsupported |

The checker went through four rounds: too loose (passed real errors), too
strict (rejected correct sentences), then two more after an audit and after
re-reading "verified" drafts by hand and finding misleading lines. One of those
was caused by the fact sheet itself: listing "look-back window: 4 weeks" as a
fact led the model to write that a ticket "exceeds the recommended window of 4
weeks". That number is now checked against but not shown to the model.

## Current behaviour, honestly

With the rules above, `llama3.2` (a small 3-billion-parameter model) usually
**fails** verification on the sample week, and the report uses the template.
That is the design working: a draft with a wrong or misleading number does not
reach the report. A larger local model can be tried with `--model`. The
template remains the default, and it is itself tested to pass the checker for
every sample week.

## Known limits

- The checker is word proximity and phrase lists, not understanding. It catches
  invented, mislabelled, misdirected and mis-ranked numbers and the commonest
  unsupported claims. It does not catch every misleading sentence (for example
  a fund's weekly change described as a "break"), which is why the report
  labels the section for analyst review.
- Keyword lists are hand-maintained in `build_facts`. A new fact needs keywords
  and a test case.
- Text from source files and Jira (tickers, team names, a renamed field's name)
  appears in the prompt. A crafted value could steer the wording, though not
  the numbers, which are checked afterwards.
