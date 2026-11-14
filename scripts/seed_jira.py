"""Seed the demo Jira project with realistic reconciliation-break tickets.

Each ticket is a break between the custodian's records and the fund
accounting system, raised on a business date and routed to an ops team.

Jira does not allow back-dating system timestamps (created/resolutiondate),
so history lives in the `Business Date` / `Resolved Business Date` fields,
the same way many ops teams record the trade date a break belongs to.

A few tickets are deliberately messy (missing Fund, Done without a resolved
date) so the validation layer has something real to catch.

    python -m scripts.seed_jira            # create tickets (refuses if seeded)
    python -m scripts.seed_jira --reset    # delete seeded tickets, then create
    python -m scripts.seed_jira --dry-run  # print a sample, touch nothing
"""

from __future__ import annotations

import argparse
import logging
import random
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from opsreport.extract.jira_client import JiraClient

log = logging.getLogger("seed_jira")

SEED_LABEL = "seed-v1"
RANDOM_SEED = 20261114
N_TICKETS = 160
WEEKS_OF_HISTORY = 10
AS_OF = date(2026, 11, 13)  # last business date in the seeded history

FUNDS = {
    "Global Equity Fund": (0.28, ["AAPL", "MSFT", "NESN", "ASML", "TSM", "NOVO-B"]),
    "US Core Bond Fund": (0.22, ["UST 4.25 2034", "UST 3.875 2029", "FNMA 5.5 2053",
                                 "JPM 5.35 2034"]),
    "Emerging Markets Debt Fund": (0.18, ["MEX 6.35 2035", "BRAZIL 6.0 2033",
                                          "INDON 4.65 2032", "ZAR 8.875 2035"]),
    "International Small Cap Fund": (0.17, ["DSY", "LOGN", "SHL", "ENX", "JD-SPORTS"]),
    "Multi-Asset Income Fund": (0.15, ["SPY", "AGG", "LQD", "HYG", "VNQ"]),
}
# break type -> (weight, owning team, typical $ median)
BREAK_TYPES = {
    "Quantity Break": (0.24, "Reconciliation", 180_000),
    "Price Break": (0.18, "NAV Oversight", 90_000),
    "Cash Break": (0.16, "Reconciliation", 60_000),
    "Missing Trade": (0.14, "Trade Support", 400_000),
    "Corporate Action": (0.12, "Corporate Actions", 120_000),
    "FX Rate": (0.09, "NAV Oversight", 45_000),
    "Static Data": (0.07, "Reference Data", 5_000),
}
# priority -> SLA in business days
SLA_DAYS = {"Highest": 1, "High": 2, "Medium": 3, "Low": 5}
FX_HEAVY = {"Emerging Markets Debt Fund", "International Small Cap Fund"}


@dataclass
class Ticket:
    summary: str
    description: str
    fund: str | None
    ops_team: str
    break_type: str
    break_amount_usd: float
    business_date: date
    priority: str
    due_date: date
    status: str
    resolved_business_date: date | None
    labels: list[str] = field(default_factory=lambda: [SEED_LABEL, "custodian-recon"])


def priority_for(amount: float) -> str:
    if amount >= 1_000_000:
        return "Highest"
    if amount >= 250_000:
        return "High"
    if amount >= 50_000:
        return "Medium"
    return "Low"


def generate_tickets(n: int = N_TICKETS, as_of: date = AS_OF,
                     seed: int = RANDOM_SEED) -> list[Ticket]:
    rng = random.Random(seed)
    bdays = list(pd.bdate_range(end=as_of, periods=WEEKS_OF_HISTORY * 5).date)
    bday_index = {d: i for i, d in enumerate(bdays)}
    funds, fund_w = zip(*[(k, v[0]) for k, v in FUNDS.items()], strict=True)

    def bday_add(d: date, k: int) -> date:
        return (pd.Timestamp(d) + pd.offsets.BDay(k)).date()

    tickets = []
    for _ in range(n):
        fund = rng.choices(funds, fund_w)[0]
        bt_names = list(BREAK_TYPES)
        bt_w = [BREAK_TYPES[b][0] * (3 if b == "FX Rate" and fund in FX_HEAVY else 1)
                for b in bt_names]
        break_type = rng.choices(bt_names, bt_w)[0]
        _, team, median = BREAK_TYPES[break_type]
        amount = round(median * rng.lognormvariate(0, 1.1), 2)
        security = rng.choice(FUNDS[fund][1])
        biz = rng.choice(bdays)
        prio = priority_for(amount)
        due = bday_add(biz, SLA_DAYS[prio])

        # Resolution effort in business days: mostly inside SLA, long tail out.
        effort = max(0, int(rng.expovariate(1 / (SLA_DAYS[prio] * 0.8))))
        if rng.random() < 0.15:  # stuck: awaiting counterparty / escalated
            effort += rng.randint(5, 40)
        resolved = bday_add(biz, effort)
        if resolved <= as_of:
            status, resolved_date = "Done", resolved
        else:
            status = rng.choices(["In Progress", "Selected for Development", "Backlog"],
                                 [0.6, 0.25, 0.15])[0]
            resolved_date = None

        age = len(bdays) - 1 - bday_index[biz]
        tickets.append(Ticket(
            summary=f"{break_type}: {security} | {fund} | {biz:%Y-%m-%d}",
            description=(
                f"{break_type} between custodian and fund accounting for {security} "
                f"in the {fund} on business date {biz:%Y-%m-%d}. "
                f"Absolute break value USD {amount:,.2f}. Routed to {team}. "
                f"Age at seed: {age} business days."
            ),
            fund=fund,
            ops_team=team,
            break_type=break_type,
            break_amount_usd=amount,
            business_date=biz,
            priority=prio,
            due_date=due,
            status=status,
            resolved_business_date=resolved_date,
        ))

    # Deliberate data-quality problems for the validation layer to catch.
    for t in rng.sample(tickets, 4):
        t.fund = None
        t.labels = [*t.labels, "dq-missing-fund"]
    done = [t for t in tickets if t.status == "Done" and t.fund]
    for t in rng.sample(done, 2):
        t.resolved_business_date = None
        t.labels = [*t.labels, "dq-missing-resolved-date"]
    return tickets


def _adf(text: str) -> dict:
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


def to_fields(t: Ticket, project_key: str, fids: dict[str, str]) -> dict:
    f = {
        "project": {"key": project_key},
        "issuetype": {"name": "Task"},
        "summary": t.summary,
        "description": _adf(t.description),
        "priority": {"name": t.priority},
        "duedate": t.due_date.isoformat(),
        "labels": t.labels,
        fids["ops_team"]: {"value": t.ops_team},
        fids["break_type"]: {"value": t.break_type},
        fids["break_amount_usd"]: t.break_amount_usd,
        fids["business_date"]: t.business_date.isoformat(),
    }
    if t.fund:
        f[fids["fund"]] = {"value": t.fund}
    if t.resolved_business_date:
        f[fids["resolved_business_date"]] = t.resolved_business_date.isoformat()
    return f


def seeded_keys(jira: JiraClient) -> list[str]:
    jql = f"project = {jira.settings.project_key} AND labels = {SEED_LABEL}"
    return [i["key"] for i in jira.search(jql, ["key"])]


def transition_ids(jira: JiraClient, key: str) -> dict[str, str]:
    data = jira.get(f"/rest/api/3/issue/{key}/transitions")
    return {t["to"]["name"]: t["id"] for t in data["transitions"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    tickets = generate_tickets()
    if args.dry_run:
        df = pd.DataFrame([t.__dict__ for t in tickets])
        print(df.drop(columns=["description"]).head(10).to_string())
        print("\nstatus:", df.status.value_counts().to_dict())
        print("priority:", df.priority.value_counts().to_dict())
        return

    jira = JiraClient()
    fids = jira.resolve_custom_fields()

    existing = seeded_keys(jira)
    if existing and not args.reset:
        raise SystemExit(f"{len(existing)} seeded issues already exist; use --reset")
    for key in existing:
        jira.request("DELETE", f"/rest/api/3/issue/{key}")
    if existing:
        log.info("Deleted %d previously seeded issues", len(existing))

    created: list[tuple[str, Ticket]] = []
    for i in range(0, len(tickets), 50):
        batch = tickets[i:i + 50]
        resp = jira.post("/rest/api/3/issue/bulk", {
            "issueUpdates": [{"fields": to_fields(t, jira.settings.project_key, fids)}
                             for t in batch]})
        if resp.get("errors"):
            raise SystemExit(f"Bulk create errors: {resp['errors'][:3]}")
        created += [(iss["key"], t) for iss, t in zip(resp["issues"], batch, strict=True)]
        log.info("Created %d/%d", len(created), len(tickets))

    tids = transition_ids(jira, created[0][0])
    moved = 0
    for key, t in created:
        if t.status != "Backlog":
            jira.post(f"/rest/api/3/issue/{key}/transitions",
                      {"transition": {"id": tids[t.status]}})
            moved += 1
    log.info("Transitioned %d issues. Done.", moved)


if __name__ == "__main__":
    main()
