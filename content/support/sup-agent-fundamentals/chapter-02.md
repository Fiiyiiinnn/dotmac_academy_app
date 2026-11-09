---
chapter: 2
title: "Ticket Intake and Triage"
part: "The Work"
---

# Ticket Intake and Triage

A ticket is the unit of work in Sub. Getting the first few minutes right —
the type, the priority, who owns it — decides whether the next four hours go
smoothly or whether the ticket bounces between three people before anyone
actually helps the customer. This chapter covers the real fields Sub tracks
on a ticket, what they mean, and how to triage one correctly the first time.

**By the end of this chapter you can:**

- Read the Support Tickets queue and know what each status tab means.
- Set an accurate priority and understand what "Due" and "breached" mean.
- Assign a ticket correctly, including when to let auto-assignment handle it.
- Recognise a badly-triaged ticket before it costs someone else time.

FIGURE `SUP-02-01` *The Support Tickets queue: status tabs, filters, and the ticket list.*

## The queue: status tabs and filters

Open **Support → Support Tickets**. The tabs across the top are not four
independent lists — they are views over the same tickets:

- **All** — every ticket, of any age.
- **Open** — tickets actively being worked.
- **Closed** — resolved and closed out.
- **Not closed** — the tickets that matter for your day-to-day: everything
  that has not reached a closed state yet, regardless of exactly which open
  status it is in.

Below the tabs, the filters (Status, Type, Region, Assignee) narrow the list
further. Get comfortable filtering by **Assignee → yourself** first thing
each shift — that is your real, personal queue, distinct from "everything
open across the whole team."

A ticket's real status is one of: `new`, `open`, `pending`,
`waiting_on_customer`, `lastmile_rerun`, `site_under_construction`,
`on_hold`, `pending_confirmation`, `closed`, `canceled`. Two of these are
easy to confuse and easy to get wrong:

- **`pending`** means the ticket is waiting on *your side* — a technician, a
  process, a decision inside the business.
- **`waiting_on_customer`** means the ball is in the *customer's* court —
  you asked them for something and are waiting on their reply.

Getting this distinction right matters for reporting as much as for the
individual ticket: a queue full of tickets wrongly marked `pending` looks
like an internal backlog when it is actually customers who have gone quiet.

## Channel and type: how the ticket arrived, and what it is

**Channel** (`web`, `email`, `phone`, `chat`, `api`) records how the ticket
reached you. It is not decoration — a `phone` ticket usually means you
already spoke to the customer and can move faster to a resolution; a `web`
or `email` ticket may need a clarifying question before you can act at all.

**Type** is your classification of the underlying issue (for example,
`maintenance`, a connectivity fault, a billing dispute, a plan change
request). Set it honestly, even when you are not fully sure yet — a wrong
type can misroute the ticket through automation rules that key off it. It
is easy to leave type blank because "I'll figure it out once I look
closer"; a ticket that never gets typed is a ticket that never gets
reported on correctly either.

## Priority and SLA: reading "Due" correctly

FIGURE `SUP-02-02` *The Ticket Metadata panel: status, assignment, and the SLA due date.*

Priority is one of `lower`, `low`, `medium`, `normal`, `high`, `urgent`.
Resist the pull to set everything `high` — a queue where most tickets are
`high` priority has no priority signal left in it at all, for you or for
anyone triaging after you.

The **Due** field is a real deadline, not a suggestion or an estimate: it is
set explicitly when the ticket is created or triaged, based on its priority
and type. When a ticket passes that deadline still open, Sub marks it
**breached** — you will see the red badge directly on the ticket. A
breached ticket is not automatically your fault, but it is always your
signal to check: is this ticket actually stuck, or does the due date need
resetting because the situation genuinely changed?

**Age** (hours since creation) is a second, independent number worth
glancing at even on a ticket that is not yet breached. A ticket that is
`normal` priority but already 40 hours old deserves a second look before it
becomes a breach statistic.

## Assignment: technician, assignees, and service team

The Ticket Metadata panel carries several distinct assignment fields, and
they are not interchangeable:

- **Assigned Technician** — the field-facing role for a ticket that needs
  physical or on-site work.
- **Assignees** — the person or people actually handling the ticket
  day-to-day.
- **Service Team** — which team currently owns the ticket. This is also how
  a handoff to another team is represented (Chapter 5 covers this in full).
- **Project Manager** / **Site Coordinator** — used on tickets tied to a
  larger installation or project, not on routine support cases.

You will usually either self-assign a ticket you are already handling, or
use **Manual Auto-Assign** to let Sub's assignment rules route it by the
same logic that runs automatically on intake. Do not hand-pick an assignee
"because they're usually free" without checking — that is exactly the kind
of local shortcut that quietly breaks load balancing across the team.

Your role can also view and edit the automation rules themselves, on the
relevant admin screen — this is not someone else's job to reach for. If the
rules are routing tickets wrongly, you are not stuck working around them;
you can go fix the rule directly.

## Common failure modes

- **Leaving type and priority at their defaults.** A ticket with no real
  classification is invisible to reporting and to automation rules that key
  off type.
- **Confusing `pending` and `waiting_on_customer`.** This single mix-up is
  the most common reason a queue's "who is this actually waiting on"
  picture is wrong.
- **Resetting Due instead of investigating a breach.** The due date exists
  to surface a problem. Quietly pushing it out defeats the entire point of
  having it.
- **Hand-assigning around the auto-assign rules** instead of trusting them,
  or fixing the rule directly if it is genuinely routing tickets wrongly.

## Do this at work

1. **Filter the queue by Assignee → yourself** and work it top to bottom by
   Due date, not by whichever ticket looks easiest.
2. **On every new ticket, set Type and Priority before doing anything else** —
   triage is not optional busywork, it is the first real step.
3. **Before marking a ticket `pending`, ask: pending on what, and on whom?**
   If the honest answer is "the customer," use `waiting_on_customer`
   instead.
4. **When you see a breached ticket, read it before touching it.** Understand
   why it is late before you change its status or its due date.
5. **Use Manual Auto-Assign before hand-picking an assignee.** Only override
   it when you have a specific, statable reason.

## Practice scenarios

These are illustrative situations, not real customer records — practice
reasoning through them before you see the equivalent in your live queue.

### Scenario: The whole-street call

A ticket arrives by `phone`, type unset, priority left at `normal`. The
customer told you directly on the call that their entire street has no
service. You need to triage this ticket before moving to the next one in
your queue.

**What do you do?**

**A)** Leave priority at `normal` and set type to a connectivity fault —
the customer already told you what's wrong, so the priority does not need
to change.
**B)** Raise the priority (reflecting that this looks wider than one
account) and set an accurate type now, using the first-hand detail from the
call.
**C)** Set the type to `maintenance` since something is clearly wrong with
infrastructure, and leave priority for whoever picks it up next.

<details>
<summary>What happens with each choice</summary>

- **A)** Partially right — setting type is correct, but leaving priority at
  `normal` ignores the strongest signal in the call: a whole street with no
  service is not a routine single-account complaint.
- **B)** Correct, and the best choice. A whole-street outage is a strong
  signal to raise priority and set an accurate type immediately, and
  because the ticket came in by `phone` you already have first-hand detail
  a `web` ticket would have lacked — there is no reason to wait for a
  clarifying question you don't need.
- **C)** Wrong on both counts. `maintenance` is not the same as an
  unclassified connectivity fault the customer is reporting live, and
  leaving priority for "whoever picks it up next" is exactly the kind of
  deferred triage this chapter warns against — a ticket with no real
  classification is invisible to reporting and automation until someone
  fixes it.

</details>

### Scenario: The silent three

You open your queue and find three tickets marked `pending`, none of which
have moved in six days. Each one's last comment is you asking the customer
a question, with no reply since.

**What do you do?**

**A)** Leave them as `pending` — the ticket is still waiting on an answer,
and pending covers "waiting on something" generally.
**B)** Change all three to `waiting_on_customer`, and consider a follow-up
contact attempt on each.
**C)** Close all three, since a customer who has not replied in six days
has probably resolved the issue themselves.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. `pending` means the ticket is waiting on *your side* — a
  technician, a process, an internal decision — not on the customer. Left
  this way, these three tickets misreport as an internal backlog you are
  sitting on, when the real story is customers who have gone quiet.
- **B)** Correct, and the best choice. These should be `waiting_on_customer`
  since the ball is genuinely in the customer's court, and six days of
  silence is a reasonable trigger to attempt another contact rather than
  wait indefinitely.
- **C)** Wrong, and risky. Six days of silence is not evidence the issue is
  resolved — closing without confirmation could leave a real, unresolved
  problem marked done, which is worse for the customer than an accurately
  labeled ticket still open.

</details>

## Summary

- The status tabs are views over one ticket list; "Not closed" is your real
  working set, and `pending` vs `waiting_on_customer` describes who the
  ball is actually with.
- Channel records how a ticket arrived; Type classifies what it is. Both
  feed reporting and automation — set them honestly and promptly.
- Due is a real deadline, not an estimate; a breach is a signal to
  investigate, not a badge to make disappear.
- Assignment fields (Technician, Assignees, Service Team) mean different
  things — use Manual Auto-Assign before hand-picking, and understand
  Service Team as the mechanism for a handoff.
