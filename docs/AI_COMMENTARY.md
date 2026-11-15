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

Each fact carries one value and the keywords that identify it, e.g.
`Reconciliation team overdue tickets = 7` with keywords *overdue / past / late*.
For every number in the draft (digits, or *one* to *twenty* spelled out):

1. **Value:** it must equal a fact's value. Rounding is allowed (`$350 million`
   for `$350.2M`), a dropped sign is allowed (`down 0.4%`), invented numbers fail.
2. **Binding:** a keyword of that fact must be within a few words, or the fact's
   entity (fund code, ticker) in the same sentence. Catches a number put on the
   wrong subject.
3. **Rival labels (counts):** if the number's own keyword isn't in its clause but
   another count's keyword is, it's flagged as mislabelled.
4. **Subset qualifiers:** an all-breaks total can't sit next to *high*,
   *materiality*, *largest*.
5. Ticket keys must exist; 1-6 bullets.

## What it caught (real llama3.2 drafts while building this)

| Draft | Problem | Rule |
|---|---|---|
| "three high-materiality ... breaks totaling **$14.9M**" | $14.9M is the total of all 11 breaks | Subset qualifier |
| "Reconciliation with **7 open** tickets" | 7 is overdue; 12 are open | Rival label |
| "ISC AUM fell **1.3%**" (test case) | 1.3% is GEF's change | Binding |

All 14 cases (including correct sentences that must **pass**) are in
`tests/test_ai_commentary.py`. Early versions of the guardrail were too loose
(passed both real errors above) and then too strict (rejected correct sentences);
the case table is what keeps both directions honest.

## Known limits

- Binding is word proximity, not meaning. It catches invented and mislabelled
  numbers, not every misleading sentence, which is why the draft is reviewed.
- Small local models are deterministic here (`seed`, low temperature) but not
  always well-phrased; the template remains the default.
- The keyword lists are hand-maintained in `build_facts`. A new fact needs keywords
  and a test case.
