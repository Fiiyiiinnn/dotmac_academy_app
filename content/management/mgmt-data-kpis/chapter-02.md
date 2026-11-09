---
chapter: 2
title: "Choosing the Right KPIs"
part: "Choosing Measures"
---

# Choosing the Right KPIs

If Chapter 1 was about why numbers matter, this one is about the harder
discipline: choosing *which*. The failure mode is almost never too few
metrics — it is a wall of dashboards where everything is measured, nothing
is watched, and the numbers that would have warned you are lost in the
ones that never could. A key performance indicator is exactly what the
name says: one of the small set of numbers that indicates whether the
team's *key* performance is happening. This chapter builds that set.

**By the end of this chapter you can:**

- Write the mission sentence a team's KPIs must fall out of.
- Draw a balanced set from the five families, and say what each family
  stops the others from hiding.
- Write a KPI definition complete enough that two people compute the same
  number from it.
- Show how one undefined term produces two honest answers twenty points
  apart.
- Decide what to do when someone proposes a sixth KPI.

FIGURE `MGT-DK-02` *From the wall of possible measures, the few that reflect the team's real job.*

## Start from the job, not the data

The wrong way to choose KPIs is to open the system and see what it can
report — that produces metrics shaped by what's easy to count. The right
way starts with a sentence: **what does this team exist to produce, for
whom, to what standard?** A dispatch team exists to complete jobs,
correctly, promptly, at sane cost, leaving customers satisfied. A finance
team exists to bill accurately and collect promptly. Write the sentence
first; the candidate measures fall out of it almost mechanically.

Then test each candidate against Chapter 1's bar: a decision depends on
it, it's expressed as a comparable rate, and this team can move it.

## The five families

Across almost any team, the sentence decomposes into five aspects, and a
balanced KPI set usually draws one measure from each:

- **Output** — how much of the real product got produced: jobs completed,
  orders fulfilled, cases resolved. The volume of the mission itself, per
  period or per person — not the volume of activity around it (calls made
  and hours logged are effort, not output).
- **Quality** — how much of it was *right*: error rate, rework rate,
  first-time-fix share, returns. Quality metrics are what keep output
  metrics honest; either alone invites trading one for the other
  invisibly.
- **Speed** — how long the customer or the next process waits: turnaround
  time, time-to-resolve, backlog age. Speed is where problems queue up
  quietly; a stable output number with a growing backlog age is a team
  falling behind while looking fine.
- **Cost** — what a unit of output consumes: cost per job, hours per
  case, overtime share, materials per unit (unit economics from the
  Finance course, worn as a working metric). Cost is the family managers
  most often omit — until the year-end conversation where it is the only
  family anyone discusses.
- **Satisfaction** — what the people served say and do: the CSAT/effort/
  loyalty measures the Customer Experience course covers for external
  customers, or simple internal ratings where your "customer" is the next
  department. Satisfaction is the outside check on the four internal
  families: a team can hit all four and still be failing the humans it
  serves.

The families discipline each other, and that is the deeper point: **any
single metric maximised alone will be achieved at the expense of the
others** — output by cutting corners (quality), speed by cherry-picking
easy work (output mix), cost by understaffing (speed, satisfaction). A
balanced set makes the trade-offs visible instead of silent.

## Few enough to watch

The working rule: **three to six KPIs per team.** Enough to cover the
families that matter for this mission; few enough that every member can
recite them and the weekly review actually looks at each. Everything else
you collect is *diagnostic* — kept available for investigating why a KPI
moved, not reviewed on rhythm. The distinction rescues you from the false
choice between "measure everything" and "lose information": the wall of
gauges still exists in the cupboard; only the chosen few hang where the
team looks daily.

Selection is also a statement of priorities — the team will read the
chosen set as the definition of what management actually wants, whatever
the mission statement says. Choose accordingly, and prune annually: a KPI
that hasn't influenced a decision in six months (Chapter 1's test) is a
diagnostic wearing a KPI's badge, and its slot belongs to whatever you're
currently arguing about from anecdote.

## Definitions: the boring step that decides everything

Every KPI needs a written definition — not bureaucracy, but the difference
between a number and a quarrel: **exactly what counts** (does a reopened
case count as resolved? does a partial delivery count as on-time?), **the
formula and its edges** (from when to when does "turnaround" run — request
received, or work started?), **the source** (which system, whose query),
and **the owner** (who explains it when it moves — one name, per the
single-owner principle that runs through this curriculum).

Undefined metrics generate the worst arguments in management — two people
with different honest numbers for the same thing — and they are the soil
gaming grows in (Chapter 5): every ambiguity in a definition will
eventually be resolved in the direction that flatters. Write the
definition when you adopt the metric, while nobody yet has a score to
defend.

## Worked example: two honest answers, twenty-three points apart

A dispatch team is asked for its on-time delivery rate. Last month it
promised 100 jobs, and this is what happened:

| Outcome | Jobs |
| --- | ---: |
| Completed by the date first promised | 71 |
| Rescheduled at the customer's request, met the new date | 15 |
| Rescheduled by us, met the new date | 8 |
| Late against every date | 6 |
| **Total** | **100** |

Two people now compute "on-time delivery":

| Definition | Counts as on time | Result |
| --- | --- | ---: |
| **A** — met the current promised date | 71 + 15 + 8 | **94%** |
| **B** — met the date first promised | 71 | **71%** |

Neither is wrong. Neither person is dishonest. The 23-point gap is *entirely*
definitional, and it sits in a single unstated question: does re-promising
reset the clock?

Notice which way each definition leans. Definition A means a team can protect
its score by rescheduling — the measure quietly rewards the thing the
customer experiences as being let down. Definition B counts the customer's
actual experience but punishes the team for reschedules it did not ask for.
The honest resolution is usually to publish **both**, or to define A while
separately reporting reschedule volume so the escape hatch stays visible.

This is why the definition step is not bureaucracy. Adopt this metric without
settling the question and you have not adopted a metric — you have adopted a
future argument, and it will be resolved by whoever most needs the number to
look good.

## A worked shape

Pulling it together for an illustrative service team. This is the artefact —
five numbers on one page, each with a definition precise enough to survive
the argument above, a named owner, and a target from Chapter 3:

| Family | KPI | Definition and edges | Source | Owner |
| --- | --- | --- | --- | --- |
| Output | Cases resolved per person per week | Cases moved to *resolved* and not reopened within 7 days ÷ FTE on the rota that week | Ticket system, weekly export | Service lead |
| Quality | Reopen rate | Cases reopened within 30 days of resolution ÷ cases resolved in the same window | Ticket system | Service lead |
| Speed | Median time to resolution; oldest open case | Clock starts at *first customer contact*, not first assignment; pauses only while awaiting customer reply | Ticket system | Team supervisor |
| Cost | Hours per resolved case | Logged hours on resolved cases ÷ cases resolved; includes travel, excludes training | Timesheets | Operations manager |
| Satisfaction | Post-interaction rating | Mean score of responses received within 7 days of closure; response rate published alongside | Survey tool | Service lead |

Two details in that table do real work. The speed clock starts at *first
customer contact* rather than first assignment, which stops the queue before
assignment from being invisible. And satisfaction publishes its response rate
alongside the score, because a 4.8 from 6% of customers and a 4.3 from 60%
are not the same fact.

A different mission — a production crew, a sales desk, a back office — swaps
the instances but keeps the shape. When someone proposes a sixth and a
seventh, the question is not "is it interesting?" but **"which of these five
does it replace, and what decision needs it?"**

## Do this at work

1. **Write your mission sentence.** What does your team exist to produce,
   for whom, to what standard? One sentence. If it takes three, the team
   may be doing two jobs.
2. **Sort your current metrics into the five families.** Output, quality,
   speed, cost, satisfaction. The empty column is the finding — and it is
   cost or satisfaction more often than not.
3. **Find your undefined term.** Take your most-quoted KPI and ask two
   colleagues to state exactly what counts. If their answers differ, you
   have found a future argument while nobody yet has a score to defend.
4. **Compute one metric two ways.** Pick one where a reasonable second
   definition exists and calculate both. The gap tells you how much of your
   reported performance is definitional.
5. **Write one definition properly.** What counts, the formula's edges, the
   source, the owner. Then have someone else compute it from your writing
   alone and compare.
6. **Prune one.** Find a metric on your wall that no decision depends on
   and take it down. Note who objects and why — that conversation usually
   reveals what the number was really for.

## Summary

- Derive KPIs from the mission sentence — what the team produces, for
  whom, to what standard — never from what the system happens to report.
- Draw from the five families — output, quality, speed, cost,
  satisfaction — because each alone is gameable and together they expose
  trade-offs.
- Keep three to six per team, reviewed on rhythm; everything else is
  diagnostic in the cupboard, not on the wall.
- Write definitions at adoption: what counts, the formula's edges, the
  source, one owner. Ambiguity becomes flattery later — one unstated
  question ("does re-promising reset the clock?") produced two honest
  on-time figures 23 points apart.
- The chosen set IS the team's read of your priorities — choose it as
  deliberately as you'd write them down, and prune it annually.
