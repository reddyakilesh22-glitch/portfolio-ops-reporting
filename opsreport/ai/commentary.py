"""LLM-drafted report commentary with numeric guardrails.

The model never sees raw data and never calculates. Python computes every
figure, formats it, and passes a short fact sheet. The model only phrases it.
Every number, fund code and ticket key in the draft is then checked against
the fact sheet; anything not traceable to a fact fails verification.

Flow: facts -> prompt -> draft -> verify -> (one retry with feedback) ->
verified draft, or fall back to the deterministic template.

Backend: a local model through Ollama (http://localhost:11434), so report
data never leaves the machine. No API key or network access required.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import pandas as pd
import requests

log = logging.getLogger(__name__)

DEFAULT_MODEL = "llama3.2"
OLLAMA_URL = "http://localhost:11434"
MAX_BULLETS = 6

SYSTEM_PROMPT = """You write the commentary section of a weekly portfolio \
operations report read by operations leads at an asset manager.

Rules:
- Write 4 to 6 bullet points, each starting with "- ", one sentence each.
- Use ONLY the facts provided. Copy every number exactly as written in the facts.
- Do not calculate, add, subtract, or estimate anything. Do not invent numbers.
- Do not speculate about causes beyond the "likely cause" given.
- Lead with what needs action: the largest breaks, overdue tickets, data issues.
- Write naturally; do not copy the fact labels word for word.
- Plain, factual tone. No greetings, headings, or closing remarks."""


# ---------------------------------------------------------------- formatting
def money(x: float) -> str:
    """$350.2M / $48.3K / $612 style."""
    a = abs(x)
    sign = "-" if x < 0 else ""
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.1f}M"
    if a >= 1e3:
        return f"{sign}${a / 1e3:,.1f}K"
    return f"{sign}${a:,.0f}"


def pct(x: float | None, signed: bool = False) -> str:
    if x is None or pd.isna(x):
        return "n/a"
    return f"{x:+.1%}" if signed else f"{x:.0%}"


# --------------------------------------------------------------------- facts
@dataclass(frozen=True)
class Fact:
    label: str               # what the number means, shown to the model
    value: str               # pre-formatted value, e.g. "$14.9M", "7", "-0.4%"
    must: frozenset[str]     # stems; at least one must appear next to the number
    exclude: frozenset[str] = frozenset()   # qualifiers that must NOT be near it


@dataclass
class FactSheet:
    facts: list[Fact]
    entities: set[str]                    # names the model may mention
    notes: list[str] = field(default_factory=list)   # number-free context lines

    @property
    def text(self) -> str:
        return "\n".join([f"- {f.label}: {f.value}" for f in self.facts]
                         + [f"- {n}" for n in self.notes])

    @property
    def tags(self) -> dict[str, str]:
        """Entity -> letter-only placeholder, so 'FNMA 6.0 2054' or '4385'
        aren't read as numbers but still count as context words."""
        ordered = sorted(self.entities, key=lambda e: (-len(e), e))
        assert len(ordered) < 100, "placeholders are 5-letter stems"
        return {e: _TAG + "".join(chr(97 + int(c)) for c in str(i))
                for i, e in enumerate(ordered)}


def _stems(*words: str) -> frozenset[str]:
    return frozenset(_stem(w) for w in words)


def build_facts(d) -> FactSheet:
    """Turn ReportData into atomic, pre-formatted facts (one number each)."""
    facts: list[Fact] = []
    entities: set[str] = set()
    notes: list[str] = [f"Report date: {d.as_of:%d %B %Y}"]

    def add(label: str, value: str, *must: str, exclude: tuple[str, ...] = ()) -> None:
        facts.append(Fact(label, value, _stems(*must), _stems(*exclude)))

    change_words = ("change", "changed", "down", "up", "fell", "rose", "decline",
                    "declined", "increase", "increased", "decrease", "decreased",
                    "lower", "higher", "week")
    aum = d.aum.set_index("fund_code")
    total = aum.loc["TOTAL"]
    add("Total AUM", money(total["aum"]), "aum", "assets")
    if "aum_change" in aum:
        add("Total AUM change vs prior week", money(total["aum_change"]), *change_words)
        add("Total AUM change vs prior week (%)", pct(total["aum_change_pct"], True),
            *change_words)
        for code, row in aum.drop(index="TOTAL").iterrows():
            entities |= {code, row["fund_name"]}
            add(f"{code} ({row['fund_name']}) AUM", money(row["aum"]), f"<{code}>")
            add(f"{code} ({row['fund_name']}) AUM week-over-week change",
                pct(row["aum_change_pct"], True), f"<{code}>")

    b = d.breaks
    add("Reconciliation breaks (count)", str(len(b)), "breaks", "break")
    # A total over all breaks can't be described with a subset qualifier
    # ("3 high-materiality breaks totaling $14.9M" is wrong).
    add("Absolute difference across all breaks", money(b["abs_base_mv_diff"].sum()),
        "absolute", "gross", "difference", "exposure", "total", "totaling", "breaks",
        exclude=("high", "medium", "low", "materiality", "largest"))
    add("High-materiality breaks (count)", str(int((b["materiality"] == "High").sum())),
        "high")
    for t, n in b["break_type"].value_counts().items():
        add(f"{t} breaks (count)", str(n), t.split()[0].lower())  # distinctive word
    for _, r in b.nlargest(3, "abs_base_mv_diff").iterrows():
        entities |= {r["fund_code"], str(r["ticker"])}
        add(f"Large break {r['fund_code']} {r['ticker']} ({r['break_type']}), absolute "
            "difference", money(r["abs_base_mv_diff"]), f"<{r['ticker']}>")
        notes.append(f"Likely cause for {r['fund_code']} {r['ticker']}: "
                     + re.sub(r"\s*\([^)]*\d[^)]*\)", "", r["likely_cause"]))

    k = d.jira_kpis
    add("Open ops tickets", str(k["open_tickets"]), "open")
    add("Ops tickets past SLA", str(k["overdue_tickets"]), "past", "overdue", "late",
        "breach", "breached")
    add("Oldest open ticket age (business days)", str(k["oldest_open_bdays"]), "oldest",
        "old", "days", "aged")
    add("SLA met this week", pct(k["sla_met_this_week"]), "this", "week", "weekly")
    add("SLA met over the last 4 weeks", pct(k["sla_met_4wk"]), "last", "four", "weeks",
        "month", "trailing", "rolling")
    add("Look-back window for SLA (weeks)", "4", "last", "weeks", "week")
    if len(d.jira_by_team):
        top = d.jira_by_team.iloc[0]
        entities.add(top["ops_team"])
        add(f"{top['ops_team']} team overdue tickets", str(int(top["overdue"])),
            "overdue", "past", "late", "breach")
        add(f"{top['ops_team']} team open tickets", str(int(top["open"])), "open")

    iss = d.issues
    n_err = int((iss["severity"] == "ERROR").sum()) if len(iss) else 0
    add("Data-quality errors", str(n_err), "error", "errors")
    add("Data-quality warnings", str(len(iss) - n_err), "warning", "warnings")
    fx = iss[iss["check"] == "fx_rate_vs_official"]
    for _, r in fx.iterrows():
        m = re.match(r"(\w+) rate ([\d.]+) vs official ([\d.]+) \(([-+\d.]+%)\) on "
                     r"(\d+) position", r["detail"])
        if m:
            ccy, used, off, diff, npos = m.groups()
            add(f"{ccy} FX rate used by fund accounting", used, ccy, "rate", "used")
            add(f"{ccy} official FX rate", off, ccy, "official", "rate")
            add(f"{ccy} rate difference vs official", diff, ccy, "rate", "stale",
                "difference", "off")
            add(f"Positions affected by the {ccy} rate", npos, "position", "positions")
    for _, r in iss[iss["check"] == "field_renamed"].iterrows():
        notes.append(f"Data-quality note: {r['detail']}")
    return FactSheet(facts, entities, notes)


# -------------------------------------------------------------- guardrails
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "mm": 1e6,
          "bn": 1e9, "b": 1e9, "billion": 1e9}
_NUM = re.compile(
    r"(?P<neg>[-−])?\$?(?P<num>\d[\d,]*(?:\.\d+)?)\s?"
    r"(?P<unit>%|million|thousand|billion|mm|bn|[kKmMbB](?![a-zA-Z]))?")
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}\s+(?:January|February|March|April|May|June|"
    r"July|August|September|October|November|December)(?:\s+\d{4})?\b|"
    r"\b(?:January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2}(?:,?\s+\d{4})?\b")
_WORD_NUMS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_STOP = set("a an and are as at be by for from has have in is it its of on or "
            "the this that to was were which with".split()) - {"this"}
_WINDOW = 4
_TAG = "ent"   # prefix of entity placeholders
_CLAUSE = re.compile(r"[,;()]|\b(?:and|while|but|with|whereas)\b", re.I)


def _stem(w: str) -> str:
    return w.lower().strip("<>")[:5] if not w.startswith("<") else w


@dataclass(frozen=True)
class Num:
    value: float      # in base units (dollars, or percentage points for %)
    is_pct: bool
    step: float       # precision of the written number, in base units
    raw: str
    near: tuple[tuple[str, int], ...] = ()   # (stem, word distance), same sentence
    sentence: frozenset[str] = frozenset()   # every stem in the sentence
    clause: tuple[tuple[str, int], ...] = ()  # like `near`, within the clause only

    def dist(self, stems: frozenset[str]) -> int | None:
        """Closest distance of any of `stems` to this number, or None.
        Entity placeholders bind anywhere in the sentence (subject first,
        number later is normal English); other keywords must be near."""
        ds = [d for w, d in self.near if w in stems]
        ds += [_WINDOW + 1 for w in stems if w.startswith(_TAG) and w in self.sentence]
        return min(ds) if ds else None

    def clause_dist(self, stems: frozenset[str]) -> int | None:
        ds = [d for w, d in self.clause if w in stems]
        return min(ds) if ds else None

    @property
    def is_count(self) -> bool:
        return (not self.is_pct and "$" not in self.raw and self.step == 1.0
                and float(self.value).is_integer())


def parse_value(text: str) -> Num:
    nums = extract_numbers(text)
    if len(nums) != 1:
        raise ValueError(f"expected one number in {text!r}")
    return nums[0]


def extract_numbers(text: str, tags: dict[str, str] | None = None) -> list[Num]:
    """Numbers in `text` with the words near them. Dates and ticket keys aren't
    numbers; entity names are replaced by placeholders; one..twenty count."""
    for name, tag in (tags or {}).items():
        text = re.sub(rf"(?<![\w-]){re.escape(name)}(?![\w-])", f" {tag} ", text)
    text = _DATE.sub(" ", text)
    text = re.sub(r"\b[A-Z]+-\d+\b", " ", text)
    text = re.sub(r"\b(" + "|".join(_WORD_NUMS) + r")\b",
                  lambda m: _WORD_NUMS[m.group(1).lower()], text, flags=re.I)
    out = []
    for sentence in re.split(r";|\.(?:\s|$)", text):
        words = [(m.start(), m.end(), m.group().lower()[:5])
                 for m in re.finditer(r"[A-Za-z]+", sentence)
                 if m.group().lower() not in _STOP]
        cuts = [c.start() for c in _CLAUSE.finditer(sentence)]
        for m in _NUM.finditer(sentence):
            num = m.group("num").replace(",", "")
            unit = (m.group("unit") or "").lower()
            scale = _SCALE.get(unit, 1.0)
            decimals = len(num.split(".")[1]) if "." in num else 0
            val = float(num) * scale
            before = [w for _, e0, w in words if e0 <= m.start()][-_WINDOW:]
            after = [w for s0, _, w in words if s0 >= m.end()][:_WINDOW]
            near = tuple((w, len(before) - i) for i, w in enumerate(before)) + \
                tuple((w, i + 1) for i, w in enumerate(after))
            lo = max([b for b in cuts if b <= m.start()], default=0)
            hi = min([b for b in cuts if b >= m.end()], default=len(sentence))
            clause = tuple((w, d) for w, d in near
                           if any(lo <= s0 < hi and ww == w for s0, _, ww in words))
            out.append(Num(-val if m.group("neg") else val, unit == "%",
                           scale * 10 ** -decimals, m.group(0).strip(), near,
                           frozenset(w for _, _, w in words), clause))
    return out


@dataclass
class Verdict:
    ok: bool
    problems: list[str]


def _same_value(a: Num, n: Num) -> bool:
    return a.is_pct == n.is_pct and \
        abs(abs(a.value) - abs(n.value)) <= max(n.step, a.step) / 2 + 1e-9


def verify(draft: list[str], facts: FactSheet) -> Verdict:
    """Fact-check a draft against the fact sheet.

    Each number (digits, or one..twenty spelled out) must
      1. equal some fact's value, allowing rounding and a dropped sign, and
      2. be *bound* to that fact: one of the fact's keywords (or its entity)
         must appear within a few words of the number in the same sentence.
    For counts whose own keyword isn't in the same clause, a different count's
    keyword must not be closer: "... most overdue tickets is Reconciliation with
    7 open tickets" fails when 7 is the overdue count. All-breaks totals reject
    subset qualifiers ("3 high-materiality breaks totaling $14.9M").
    Ticket keys must exist; bullet count must be within limits.

    Known limit: binding is word proximity, not meaning. It catches invented and
    mislabelled numbers, not every misleading sentence; the analyst still
    reviews the draft before sending.
    """
    problems: list[str] = []
    if not 1 <= len(draft) <= MAX_BULLETS:
        problems.append(f"expected 1-{MAX_BULLETS} bullets, got {len(draft)}")

    tags = facts.tags
    # "<GEF>" in a fact's keywords means "the placeholder for entity GEF".
    must = {f: frozenset(tags.get(w[1:-1], w) if w.startswith("<") else w
                         for w in f.must)
            for f in facts.facts}
    parsed = [(f, parse_value(f.value)) for f in facts.facts]
    for bullet in draft:
        for n in extract_numbers(bullet, tags):
            same = [f for f, v in parsed if _same_value(v, n)]
            if not same:
                problems.append(f"'{n.raw}' is not a value in the facts")
                continue
            same = [f for f in same if n.clause_dist(f.exclude) is None] or same
            qualified = all(n.clause_dist(f.exclude) is not None for f in same)
            d_match = [d for f in same if (d := n.dist(must[f])) is not None
                       and not qualified]
            if qualified:
                problems.append(f"'{n.raw}' is described with a qualifier the fact "
                                f"doesn't have; facts: "
                                + " | ".join(f"{f.label} = {f.value}" for f in same))
                continue
            if not d_match:
                problems.append(f"'{n.raw}' is not next to what it measures; facts: "
                                + " | ".join(f"{f.label} = {f.value}" for f in same))
                continue
            own_in_clause = any(n.clause_dist(must[f]) is not None for f in same)
            if n.is_count and not own_in_clause:
                # The number's own label isn't in its clause, but a different
                # count's label is: probably mislabelled ("... most overdue tickets
                # is Reconciliation with 7 open tickets" when 7 is the overdue count).
                rivals = [(d, f) for f, v in parsed if v.is_count and f not in same
                          and (d := n.clause_dist(must[f])) is not None
                          and d < min(d_match)]
                if rivals:
                    f = min(rivals, key=lambda x: x[0])[1]
                    problems.append(f"'{n.raw}' sits next to '{f.label}' "
                                    f"(= {f.value}); facts: "
                                    + " | ".join(f"{g.label} = {g.value}" for g in same))
    for key in set(re.findall(r"\b[A-Z]+-\d+\b", "\n".join(draft))) - facts.entities:
        problems.append(f"ticket '{key}' is not in the facts")
    return Verdict(not problems, problems)


# ------------------------------------------------------------------ backend
class LLMUnavailable(RuntimeError):
    pass


def ollama_chat(messages: list[dict], model: str = DEFAULT_MODEL,
                url: str = OLLAMA_URL, timeout: float = 120.0) -> str:
    try:
        resp = requests.post(f"{url}/api/chat", timeout=timeout, json={
            "model": model, "messages": messages, "stream": False,
            "options": {"temperature": 0.1, "seed": 7}})
    except requests.RequestException as exc:
        raise LLMUnavailable(f"Ollama not reachable at {url}: {exc}") from exc
    if resp.status_code != 200:
        raise LLMUnavailable(f"Ollama error {resp.status_code}: {resp.text[:200]}")
    return resp.json()["message"]["content"]


def parse_bullets(text: str) -> list[str]:
    bullets = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^(?:[-*•]|\d+[.)])\s+(.*)", line)
        if m and m.group(1).strip():
            bullets.append(m.group(1).strip())
    return bullets


# --------------------------------------------------------------- orchestrate
@dataclass
class CommentaryResult:
    bullets: list[str]
    source: str                       # "llm:<model> verified" or "template (...)"
    attempts: list[dict] = field(default_factory=list)


def generate(d, template: list[str], model: str = DEFAULT_MODEL, chat=ollama_chat,
             max_attempts: int = 2) -> CommentaryResult:
    """Draft with the LLM, verify, retry once with feedback, else use template."""
    facts = build_facts(d)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Facts:\n" + facts.text}]
    attempts: list[dict] = []
    for attempt in range(1, max_attempts + 1):
        try:
            raw = chat(messages, model=model)
        except LLMUnavailable as exc:
            log.warning("AI commentary unavailable (%s); using template", exc)
            return CommentaryResult(template, f"template (LLM unavailable: {exc})",
                                    attempts)
        bullets = parse_bullets(raw)
        verdict = verify(bullets, facts)
        attempts.append({"attempt": attempt, "bullets": bullets,
                         "problems": verdict.problems})
        if verdict.ok:
            log.info("AI commentary verified on attempt %d (%s)", attempt, model)
            return CommentaryResult(bullets, f"llm:{model} (local), numbers verified",
                                    attempts)
        log.warning("AI draft %d failed verification: %s", attempt,
                    "; ".join(verdict.problems))
        messages += [{"role": "assistant", "content": raw},
                     {"role": "user", "content":
                      "Your draft failed fact-checking: " + "; ".join(verdict.problems)
                      + ". Rewrite it using only numbers copied exactly from the facts."}]
    return CommentaryResult(template, f"template (LLM draft failed verification "
                                      f"{max_attempts}x)", attempts)
