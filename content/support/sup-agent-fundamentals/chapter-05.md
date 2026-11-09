---
chapter: 5
title: "Escalation and Handoff"
part: "The Work"
---

# Escalation and Handoff

Not every ticket ends with you resolving it yourself. Knowing when to hand a
ticket to another team, how to do it so nothing is lost, and how to merge
duplicate tickets cleanly are skills of their own — and done badly, a
handoff is often worse for the customer than no escalation at all. This
chapter covers the real mechanics Sub gives you for escalation, merging,
and closing the loop.

**By the end of this chapter you can:**

- Decide when a ticket needs to move to another team rather than staying
  with you.
- Hand a ticket off using Service Team so the ownership change is on the
  record, not just implied.
- Merge duplicate tickets correctly, and understand what happens to each
  one when you do.
- Write a handoff that the next person can actually act on without
  re-asking the customer everything.

## Deciding to escalate

Escalate when the ticket needs something your role cannot provide — not
when it is merely difficult or slow. From what you already know:

- A confirmed or suspected outage needs NOC, not repeated troubleshooting
  from you (Chapter 4).
- A billing dispute beyond what you are authorized to adjust needs Finance.
- A technical fault needing a site visit or infrastructure change needs the
  technical/field team, via a technician assignment or field work order.

Escalating early on a genuinely misrouted ticket is correct triage, not
giving up. Escalating late, after exhausting options that were never yours
to exhaust, costs the customer time you could have saved them.

## Handoff via Service Team

FIGURE `SUP-05-01` *Merging a ticket into another with a stated reason.*

The **Service Team** field on a ticket is how a handoff is represented —
changing it moves ownership of the ticket to another team, visibly, on the
ticket itself. On the ticket's own detail page, Service Team is shown as
plain, read-only text in the Ticket Metadata panel — there is no dropdown
or input there. To actually change it, open the separate **Edit Ticket**
screen, where the same underlying field appears under a different label:
**"Service / User Group."** That is the real mechanism for a handoff —
not an in-panel edit on the detail page itself.

This is different from simply telling someone in chat "can you take this
one" — a chat message is not on the ticket, and the next person to open it
has no way to see that a handoff even happened, let alone why.

Before you change Service Team, write an internal note explaining *why*
you are handing this off and what you have already tried or ruled out.
The single most common complaint about a bad handoff is not that it
happened — it is that the receiving team had to start from zero, asking
the customer questions you already had answers to.

## Merging duplicate tickets

Customers sometimes create more than one ticket for the same issue — a
follow-up email on top of a phone call, for instance. Rather than working
both independently, merge the duplicate into the canonical one:

- Enter the **target ticket's ID** (the one that should survive) and a
  **reason for the merge**.
- Comments, attachments, and links move from the source ticket to the
  target.
- The source ticket is **canceled** and linked to the target — it is not
  deleted, and its history remains visible through that link.

Always merge *into* whichever ticket has the most complete, accurate
history — usually the older one, but not always. Merging the well-documented
ticket into a nearly-empty duplicate just to keep the higher ticket number
active loses context for no reason.

## SLA awareness during escalation

A handoff does not pause the clock. The **Due** date and breach status from
Chapter 2 keep applying after Service Team changes — a ticket that
breaches shortly after being handed off reflects on the whole chain, not
just whoever is holding it at that moment. If you know a handoff is likely
to push a ticket past its due date, say so in your handoff note rather than
letting it breach silently on someone else's desk.

## Writing a handoff that works

A good internal note before a handoff answers three questions the next
person will otherwise have to ask the customer again:

1. What is the actual problem, in the customer's words and in yours?
2. What have you already checked, tried, or ruled out?
3. What does the customer already know, and what have you already told
   them to expect?

Whether a note is visible to the customer depends on the **Reply to
customer** checkbox on the comment form — leave it unchecked for handoff
notes meant for the next team, and use it deliberately when you do want the
customer to see an update. Confusing the two is how customers see
internal shorthand that was never meant for them, or how a team misses
an update meant for the customer.

## Common failure modes

- **Escalating by message instead of by Service Team.** If it is not on the
  ticket, the receiving team may never see it was assigned to them at all.
- **Merging into the emptier ticket** rather than the more complete one, for
  no reason beyond ticket number.
- **Handing off with no note**, forcing the customer to repeat everything to
  a new person.
- **Ignoring an approaching SLA breach because "it's not my ticket anymore."**
  It is still one continuous case to the customer.

## Do this at work

1. **Before escalating a ticket, write the three-question handoff note
   first**, then change Service Team — not the other way around.
2. **The next time you spot two tickets from the same customer about the
   same issue, merge them**, choosing the target deliberately rather than by
   habit.
3. **Check the Due date on any ticket you are about to hand off.** If a
   breach is likely soon, say so explicitly in your note.
4. **Practice writing one handoff note and one customer-visible reply for
   the same situation**, side by side, and notice how different they should
   sound.

## Practice scenarios

Illustrative situations, not real customer records.

### Scenario: Diagnosed, but not yet visited

A customer's issue turns out to need a site visit. You have already
diagnosed it correctly, but no field team member has looked at it yet, and
the ticket is still sitting with you.

**What do you do?**

**A)** Message someone on the field team directly asking them to "take a
look when they get a chance," and leave the ticket with yourself for now.
**B)** Change Service Team to hand the ticket to the technical/field team,
after writing a note covering your diagnosis, what you checked, and what
the customer expects next.
**C)** Change Service Team immediately to get the ticket off your desk, and
write the note afterward if there's time.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. A chat message is not on the ticket — the receiving team
  has no way to see that a handoff even happened, let alone why, and the
  ticket's Service Team field still shows you as the owner.
- **B)** Correct, and the best choice. Writing the note first, then
  changing Service Team, means the field team can act immediately using
  your diagnosis instead of re-asking the customer everything — this is
  the sequence Chapter 5 recommends specifically because the note is what
  makes the handoff usable, not just visible.
- **C)** Partially right — the ownership change is on the record, which
  is better than option A, but changing Service Team before writing the
  note risks the handoff arriving with no context. The field team could
  start working (or ask the customer to repeat themselves) before your
  note ever gets written.

</details>

### Scenario: Two tickets, one issue

You find two open tickets from the same customer, three days apart, both
describing the same intermittent drop in service. One has five detailed
comments; the other has none. You need to merge them.

**What do you do?**

**A)** Merge the older ticket into the newer one, since the newer one is
the one the customer most recently touched.
**B)** Merge the empty ticket into the one with five detailed comments,
regardless of which was created first.
**C)** Merge the detailed ticket into the empty one, since it has the lower
comment count and looks like less has been invested in it.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. Ticket age alone isn't the criterion — merging based on
  which is newer, when the older one holds all the useful history, would
  bury five comments' worth of context inside a canceled, less-visible
  source ticket.
- **B)** Correct, and the best choice. The target should be whichever
  ticket already carries the useful, complete history — here, the one
  with five detailed comments — regardless of which was created first or
  which has the lower ticket number.
- **C)** Wrong, and it's the exact mistake this chapter warns against:
  merging the well-documented ticket into a nearly-empty one loses context
  for no reason, just to keep a particular ticket number active.

</details>

## Summary

- Escalate when the task genuinely belongs to another team, not merely when
  it is hard — and escalate through Service Team, not a side-channel
  message, so the handoff is on the record.
- A ticket merge keeps the canonical ticket's history and moves
  comments/attachments/links onto it; the source is canceled and linked,
  not deleted — choose the target for its content, not its ticket number.
- SLA due dates keep running through a handoff; flag a likely breach in
  your note rather than letting it land silently on someone else.
- A good handoff note answers what the problem is, what you already tried,
  and what the customer already knows — write it before you escalate, not
  after someone asks.
