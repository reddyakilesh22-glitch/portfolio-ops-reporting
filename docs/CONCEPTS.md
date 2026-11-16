# Concepts: the business behind this project

Read this first if you are new to asset management. No finance background
needed. Every example uses real rows from this project's (synthetic) data.

## The one-paragraph version

An investment company keeps **two separate records** of what its funds own:
one kept by the bank that holds the assets, and one kept in the company's own
books. Every business day the two records must be compared. Wherever they disagree,
someone has to find out why and fix it, because a fund reporting the wrong
holdings reports the wrong value to its investors. This project automates that
comparison, keeps the history, and reports on it daily, weekly and monthly.

## An everyday analogy

| Everyday life | This project |
|---|---|
| You | An **asset manager**: a company that invests other people's money |
| Your separate savings pots (holiday, house, retirement) | **Funds**: separate pools of money, each with its own goal |
| Your bank | The **custodian**: the bank that actually holds the fund's assets |
| Your bank statement | The **custodian holdings statement** |
| Your own budgeting spreadsheet | **Fund accounting**: the company's own official books |
| Checking your spreadsheet against the bank statement | **Reconciliation** |
| A line that doesn't match | A **break** |
| A to-do item to chase the mismatch | An **ops ticket** in Jira |

If your spreadsheet says you have $5,000 and the bank says $4,800, one of them
is wrong, or something is still in transit. The same thing happens to funds,
with millions of dollars and hundreds of holdings.

## Who is who

**Asset manager.** The company running the funds (think Vanguard or BlackRock,
at a much smaller scale here). It decides what to buy and sell.

**Fund.** A pool of investors' money. This project has five, such as the
*Global Equity Fund* (code `GEF`), which owns shares in companies worldwide.

**Custodian.** A bank hired to hold the fund's shares, bonds and cash for
safekeeping. *Custodian does not mean customer.* It comes from "custody": the
bank has custody of the assets. The asset manager is not allowed to hold the
investors' assets itself; an independent bank does, which protects investors.

**Custodian holdings statement.** The bank's list of what it is holding for
each fund today: which securities, how many, and what they are worth. In this
project it is the Excel file `custodian_holdings_<date>.xlsx`.

**Fund accounting.** The fund's own official bookkeeping. Every trade the
fund makes is recorded here as soon as it is agreed. These books are used to
calculate what the fund is worth, so they are called the *book of record*.

**Fund accounting positions.** The books' list of what each fund owns today.
A **position** is simply "how much of one thing a fund holds", for example
*Global Equity Fund holds 62,700 NVIDIA shares*. In this project it is
`fund_accounting_positions_<date>.xlsx`.

**Operations (ops) team.** The people who investigate and fix mismatches.
Each mismatch gets a ticket in **Jira** (a task-tracking tool) with a deadline.

## Why would two records of the same thing ever differ?

Mostly timing and human or system error. The seven causes this project
detects, each with the real example it finds in the 13 Nov 2026 data:

| Break type | Plain English | Real example in this data |
|---|---|---|
| **Missing trade** | One side has the holding, the other has nothing | Accounting says GEF owns 62,700 NVIDIA shares ($8.4M). The custodian has none: the purchase was recorded but the shares never arrived |
| **Quantity break** | Both have it, but different amounts | Accounting has 53,400 AstraZeneca shares, custodian 47,700: a purchase of 5,700 is recorded but not yet delivered |
| **Price break** | Same amount, valued at different prices | Accounting prices Euronext 3.5% higher than the custodian: they use different price providers |
| **Corporate action** | A company event changed the holding on one side only | ASML did a 2-for-1 share split. Custodian shows 16,200 shares at €361.59; accounting still shows 8,100 at €723.18. Same total value, wrong share count |
| **FX rate** | Same foreign value, converted to dollars at different exchange rates | Accounting converted Brazilian bonds at 5.41 reais per dollar, a day-old rate. The official rate was 5.57 |
| **Cash break** | Cash balances differ | The custodian received a $48,250 interest payment that accounting has not recorded yet |
| **Static data** | A basic fact about the security is wrong on one side | Accounting has Logitech's currency as euros. It trades in Swiss francs |

Notice the ASML split: the dollar value matches to within $1, so a check on
value alone would miss it. That is why the comparison checks quantity and
value separately.

## Why it matters

- **Investors are affected.** A fund's price per share is calculated from the
  accounting books. If the books are wrong, investors buy or sell at the wrong
  price.
- **Money can go missing.** A trade that never arrived (the NVIDIA example) is
  $8.4M the fund thinks it has and doesn't.
- **Regulators expect it.** Firms must show they compare their records to the
  custodian's and resolve differences promptly.

## Every day: timing, pending trades and the life of a break

**Why daily.** A fund calculates its price per share every business day from
the accounting books. A wrong holding on Tuesday means investors who buy or
sell on Tuesday get the wrong price, so the check runs every day, not weekly.

**Trade date vs settlement date.** When a fund buys shares, the purchase is
*agreed* on the trade date but the shares and cash only *change hands* on the
settlement date: one business day later in the US (T+1), usually two
elsewhere (T+2). Fund accounting records the purchase on the trade date; the
custodian only holds the shares once they settle. So for a day or two the two
records disagree by exactly the trade, and nothing is wrong.

**Pending trades.** The firm's trading system lists every trade that is booked
but not yet settled. The daily run uses that list to sort breaks:

| The break is... | Status | What happens |
|---|---|---|
| Fully explained by pending trades whose settlement date is still ahead | **Expected** | Shown separately. No action unless it is still there after the settlement date |
| Explained by a trade whose settlement date has passed | **Actionable**: settlement failed | The shares didn't arrive on time: chase the custodian or broker |
| Only partly explained (for example 600 of 1,000 shares) | **Actionable** | The note says how much is unexplained. Nothing unexplained can hide behind a trade |
| Anything else (price, cash, FX, a split, wrong currency) | **Actionable** | Investigate |

Real example from the sample data, 13 Nov 2026: the Emerging Markets Debt Fund
bought 2.8M of a Brazilian bond that day. Accounting has it, the custodian
doesn't yet, a $2.7M difference. The pending-trades list shows it settles on
Monday 16 Nov, so it is Expected: no analyst time spent on it.

**The life of a break.** A break gets an **ID** the first day it appears and
keeps it every day until it clears, so the run can say how many business days
it has been open and link it to its Jira ticket. One that clears appears on
the "Cleared" tab the next morning.

```
Mon  price difference appears  -> new break BRK-...-ULVR, ticket raised, due Thu
Tue  still there               -> same ID, open 1 day
...  still there after Thu     -> ticket Overdue: top of the analysts' list
Fri  accounting fixes the price -> break gone: listed as Cleared, ticket Resolved
```

**Daily, weekly, monthly.** Analysts work day by day. Managers want to know
whether the team is keeping up, which needs weeks and months. The weekly and
monthly views are the same daily results grouped, so they can never disagree.

**Root causes.** Fixing breaks one at a time never ends if the same thing keeps
causing them. Counting breaks by cause shows what to fix upstream. In the
sample data one pricing vendor ("VendorB") is behind 40% of all breaks: a
conversation with that vendor would prevent more breaks than any amount of
faster fixing.

## The numbers on the report

**Market value (MV).** What a holding is worth: quantity × price.
62,700 NVIDIA shares × $134.52 = $8.43M.

**Local vs base currency.** ASML trades in euros (its *local* currency). The
funds report in US dollars (the *base* currency), so every foreign holding is
converted using that day's **FX rate** (foreign exchange rate).

**AUM (assets under management).** The total value of everything the funds
hold: $350.2M across the five funds here. It is the headline number, and a
big weekly change needs an explanation.

**Accrued income.** Interest a bond has earned but not paid out yet. It is
part of what the fund is worth, so it is added into AUM.

**Materiality.** How much a break matters, by dollar size: High (over $1M),
Medium (over $100K), Low.

**Tolerance.** Tiny differences from rounding are ignored. Here, anything
under $25 is not a break.

**SLA (service level agreement).** The deadline for fixing a ticket: 1
business day for the most urgent, up to 5 for the least. The report shows
what share of tickets were fixed in time.

**Business day.** A day the stock market is open: not weekends, not market
holidays such as Thanksgiving.

## Glossary

| Term | Meaning |
|---|---|
| As-of date | The date the data describes (the Friday being reported), not the day the report is run |
| Book of record | The official version of the truth; here, fund accounting |
| Actionable break | A break someone has to investigate and fix |
| Aged break | An actionable break open more than 5 business days |
| Break | A difference between the custodian's record and the accounting record |
| Break ID | The name a break keeps from the day it appears until it clears, e.g. `BRK-20261113-EMD-<ISIN>` |
| Control total | The total printed on a statement, used to check that every row was read |
| Custodian | The bank that holds a fund's assets for safekeeping |
| Expected break | A break fully explained by a trade that hasn't reached its settlement date yet |
| Depository | The central institution where securities are ultimately held (e.g. DTC in the US, Euroclear in Europe). A custodian may hold one position across several |
| ISIN | A 12-character code that uniquely identifies a security worldwide; the last character is a check digit |
| Lot | One row of a holding on the custodian statement, when a holding is split across locations |
| NAV | Net asset value: what a fund is worth; the basis for its price per share |
| Failed settlement | A trade whose settlement date passed without the shares or cash arriving |
| Pending trade | A trade booked in the accounting books but not yet settled at the custodian |
| Position | How much of one security a fund holds |
| Security | Anything tradeable a fund holds: a share, a bond, an ETF |
| Security master | The reference list of facts about each security (name, currency, type) |
| Root cause | Why a break happened (a vendor's price, a late settlement); counting them shows what to fix upstream |
| Settlement | The moment shares and cash actually change hands, usually one business day after the trade is agreed |
| Snapshot | A saved copy of the Jira data as it was when pulled, so a report can be reproduced |
| T+1 / T+2 | Settlement one or two business days after the trade date |
| Ticker | A short market code for a security, e.g. `NVDA` for NVIDIA |
| Trade date | The day a trade is agreed and recorded in the accounting books |

Next: [how the code turns these inputs into the report](ARCHITECTURE.md).
