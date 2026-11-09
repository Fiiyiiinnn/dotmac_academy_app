---
chapter: 6
title: "Understanding Customer Behavior and Professional Communication"
part: "The Work"
---

# Understanding Customer Behavior and Professional Communication

The first five chapters taught you the fields, the screens, and the
mechanics: how a ticket is structured, how a customer record is read, how an
outage is tracked, how a handoff works. This chapter is about what happens
around all of that — how fast tickets actually move in this operation, why
so many of them breach their SLA before anyone touches them, and how to
write and act in a way that a customer, and the next agent, can trust. None
of what follows is generic customer-service advice: it is drawn from a
90-day, aggregate operational pull from this operation's own Sub instance,
and the single biggest finding in it should change how you think about
ownership on every ticket you touch.

**By the end of this chapter you can:**

- Explain why response-time discipline matters here specifically, using this
  operation's own numbers rather than a general "be fast" instinct.
- Recognise why an unowned ticket is the biggest single risk to SLA in this
  operation, and act accordingly.
- Read the real signal in a ticket's priority, channel, account state, and
  history instead of reacting to how many tickets are in the queue.
- Write a reply that sets an honest expectation and closes the loop, rather
  than one that merely sounds polite.

## Why response-time discipline matters here

Over a recent 90-day window, **60.69% of the 1,557 SLA clocks running in
this operation's Sub instance breached** — the ticket passed its Due date
before it reached a state that stopped the clock. That is not a rounding
error or a handful of hard cases; it is the majority outcome. Response-time
discipline is not a soft virtue in this operation — it is the difference
between a queue that is basically under control and one that is not.

The clearest evidence of what "good" looks like sits in Team Inbox
conversation data from the same period: **Technical Support averaged about
46 minutes (2,785 seconds) of first-response time across 4,140
conversations** — genuinely high volume, and still fast. That combination
matters: a team that fields the most conversations in the operation is also
the one responding quickest, which means speed at volume is achievable here,
not a theoretical target. By contrast, some teams with very low conversation
volume in the same window showed multi-day average first-response times —
one averaged roughly 9.3 days across only 5 conversations. Read that
correctly: a handful of conversations averaging days is far more likely to
reflect a near-idle queue nobody was actively checking than a team that is
somehow forty times slower than Technical Support. Do not treat it as a
typical failure mode to fear — treat the Technical Support number as the
one to learn from, because it is the one built on real volume.

## Ownership is the real lever: the unassigned-ticket problem

If you take one number from this chapter, take this one. Of the 1,557 SLA
clocks in the 90-day window, **982 — 63% — were sitting with "Unassigned
Team"**, more than every named team's tickets combined. Those unassigned
tickets breached at a **59.9% rate**, essentially the same as the overall
60.69% breach rate for the whole operation. In other words: the single
largest driver of missed SLAs in this operation is not any one team being
slow — it is tickets that have no team, and therefore no one, actually
holding them.

This is exactly why Chapter 2's instructions on assignment and Chapter 5's
instructions on Service Team are not procedural nitpicking. A ticket without
an owner is not merely "not yet started" — it is the single largest category
of at-risk work in this operation, larger than the combined workload of
every team that does have a name attached. Every time you use Manual
Auto-Assign instead of leaving a ticket to sit, or change Service Team
instead of mentioning a handoff in chat, you are acting directly against the
biggest, best-evidenced failure mode this operation has. Conversely, every
ticket you notice sitting unassigned in a shared queue and pick up or route
correctly is closing the single largest gap, not a minor one.

## Reading the real signal, not the volume

A busy queue creates a strong pull toward triaging by "how many are there"
rather than "what is each one actually telling me." Resist it. Everything
you need to read a ticket's real urgency, you already learned in earlier
chapters — this section is about combining those signals rather than
reacting to raw ticket count:

- **Priority and channel (Chapter 2).** A `phone` ticket from a customer who
  told you their whole street is down carries more first-hand signal than a
  `web` ticket with no detail yet — channel tells you how much you already
  know, priority tells you how urgent the customer's stated problem is. A
  queue where you triage by "which one is easiest" rather than by these
  signals is exactly the pattern that lets an unassigned, high-priority
  ticket sit while an easy, low-priority one gets picked up first.
- **Account and subscription state (Chapter 3).** A customer who sounds
  frustrated is not always describing a fault — check whether the account or
  subscription is actually in a payment-related state before assuming the
  tone means "urgent technical problem." The right response to an annoyed
  customer with a delinquent subscription is an honest explanation of the
  billing state, not an escalation to a team that cannot fix a balance.
- **Outage patterns (Chapter 4).** Several tickets with the same symptom,
  arriving close together, is a stronger signal than any one customer's
  tone — it tells you the volume itself is the data point, not something to
  push through ticket by ticket. This is the one case where "several
  tickets at once" *is* the real signal, precisely because it points to one
  shared cause rather than many individual ones.
- **Escalation history (Chapter 5).** A ticket that has already been handed
  off, or merged from a duplicate, carries context you should read before
  reacting to its current state — a ticket that looks freshly urgent may
  actually be a well-documented case someone already diagnosed.

None of this is about guessing at a customer's mood. It is about reading
the fields Sub already gives you, in combination, instead of treating
"the queue is long" as its own instruction on what to do next.

## Professional communication standards

A reply is part of the permanent record the same way a ticket status is
(Chapter 1). Three standards apply to every customer-facing reply you write:

- **Be clear about what is actually happening.** State the real cause when
  you know it (a billing status, a confirmed outage, a scheduled visit) —
  vague reassurance that avoids naming the cause reads as evasive even when
  it is well-intentioned, and it leaves the next agent guessing what the
  customer was actually told.
- **Set an expectation you can actually keep.** "We're looking into it" with
  no timeframe is not an expectation, it is a placeholder — and against a
  60.69% breach rate, a vague promise is more likely than not to be broken.
  If you do not know the timeframe, say what you do know: what has been
  checked, what happens next, and when the customer will hear from you
  again, even if that next update is "by end of day tomorrow" rather than a
  resolution.
- **Close the loop, explicitly.** A ticket that goes quiet after a fix is
  applied, with no confirming reply to the customer, leaves them to wonder
  whether anyone is still handling it. Confirm resolution in writing, on the
  ticket, the same way you would document a handoff — silence is not the
  same as "done."

Two data points from the same 90-day window are worth stating honestly
rather than working around. First, **CSAT collection produced zero
submissions in this window** — there is currently no customer-satisfaction
data to learn from, so this course does not claim to know how customers
rated these interactions, and you should be skeptical of anyone who claims
otherwise from this period. Second, of **2,250 NCC complaint records**,
**Quality of Service (Data) accounted for 1,393 (62%)** — by a wide margin
the largest category — while **309 (14%) were left unclassified**. That
unclassified share is a live version of the exact lesson from Chapter 2:
a ticket that never gets an honest type is a ticket that never gets reported
on correctly, and at the regulatory-complaint level that gap is large enough
to be worth naming here, not just in triage.

## Common failure modes

- **Triaging by queue length instead of by signal.** Working tickets
  "easiest first" or "newest first" instead of by priority, channel, and
  account state is how a genuinely urgent unassigned ticket sits behind a
  routine one.
- **Leaving a ticket unassigned "for now."** Given that unassigned tickets
  are 63% of all SLA clocks and breach at nearly the operation's overall
  rate, "I'll get to it" on an unassigned ticket is not a neutral delay — it
  is the single biggest lever in this operation, left unpulled.
- **Replying with reassurance instead of a fact.** A friendly-sounding reply
  that names no cause and no timeframe reads well in the moment and fails
  the customer later when nothing has actually been committed to.
- **Treating a quiet ticket as a closed one.** Chapter 2 already covered the
  `pending` vs. `waiting_on_customer` distinction — the same discipline
  applies to closing the loop: silence is not confirmation.
- **Leaving type unset on a complaint that clearly has one.** The 14%
  unclassified share among NCC complaints is not an abstract statistic — it
  is the accumulated result of exactly this shortcut, one ticket at a time.

## Do this at work

1. **Before triaging your queue by "what's easiest," sort by priority and
   Due date first**, and treat any ticket with no Service Team as a
   candidate for immediate assignment, not something to leave for later.
2. **On every ticket, ask whether the tone you're reading matches the
   account/subscription state you can see on screen.** If they disagree,
   trust the record, and explain the real cause to the customer.
3. **Write one reply today that states a cause and a concrete next check-in
   time**, even if the cause is "still confirming" and the check-in is
   tomorrow — avoid the vague "we're on it" pattern entirely.
4. **When you resolve a ticket, write the closing reply before you move on**,
   not after the next ticket pulls your attention away.
5. **Set an accurate type on every ticket you close**, even ones that feel
   obvious — the unclassified share in regulatory complaints exists because
   this step got skipped, one ticket at a time.

## Practice scenarios

Illustrative situations, not real customer records.

### Scenario: The unassigned ticket in the shared queue

You are working your own assigned tickets when you notice a ticket in the
shared "All" view has no Service Team set and no assignee, and has been
sitting for over a day. It is not currently on your Workqueue, and none of
your own tickets are breaching.

**What do you do?**

**A)** Leave it — it is not assigned to you, so picking it up is not your
responsibility, and your own queue is already clear.
**B)** Use Manual Auto-Assign (or self-assign, if it is clearly in your
area) to give it an owner now, rather than leaving it in the unassigned
pool.
**C)** Mention it to a supervisor in chat so someone knows it exists, and
continue with your own queue.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. Unassigned tickets are 63% of all SLA clocks in this
  operation and breach at almost the overall rate — leaving one sitting
  because it "isn't yours" is exactly the pattern behind the single largest
  driver of missed SLAs here, not a neutral choice.
- **B)** Correct, and the best choice. Getting a real owner onto the ticket —
  through Manual Auto-Assign or a direct self-assign — is the specific,
  supported action that closes the largest gap this operation's own data
  shows. It does not require it to already be "yours"; it requires someone
  to make it someone's.
- **C)** Partially right at best. A verbal mention is the same failure mode
  Chapter 1 warned against: if the ticket is not actually assigned in Sub,
  it stays exactly as unowned as before, however many people now know about
  it.

</details>

### Scenario: The reassuring reply that says nothing

A customer's connectivity ticket needs another day before a technician can
confirm the fix. You want to reply quickly so the customer does not feel
ignored, and you are drafting a message before you finish your other
tickets.

**What do you do?**

**A)** Reply: "Thanks for your patience, our team is looking into this and
we appreciate your understanding," and move to your next ticket.
**B)** Reply with the actual cause you know so far, state that a technician
visit is expected, and give a specific day you will follow up even if the
visit hasn't happened by then.
**C)** Wait to reply until the technician visit is actually complete, so you
only need to send one message with the real outcome.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. This reads politely but commits to nothing — no cause, no
  timeframe, no next check-in. Against a 60.69% breach rate, a vague
  "looking into it" is more likely than not to be followed by silence,
  which is worse for the customer than an honest, specific update.
- **B)** Correct, and the best choice. Naming the real cause and a concrete
  next check-in date is exactly the standard this chapter teaches: clear,
  a real expectation, and a stated point at which the loop gets closed —
  even if the answer at that point is "still waiting," the customer knows
  when to expect it.
- **C)** Wrong. Waiting a full day with no interim update leaves the
  customer with no information in the meantime, and if the technician visit
  slips further, there is still no reply at all — this is the "ticket goes
  quiet" failure mode from earlier in the chapter, just delayed rather than
  avoided.

</details>

## Summary

- Across a 90-day window, 60.69% of 1,557 SLA clocks breached — response-time
  discipline is the majority-outcome problem in this operation, not an edge
  case.
- Unassigned tickets made up 982 of those 1,557 clocks (63%), breaching at
  59.9% — the single largest driver of missed SLAs, bigger than every named
  team combined. Getting a real owner onto a ticket is the highest-leverage
  action you can take.
- Technical Support's ~46-minute average first response across 4,140
  conversations shows speed at real volume is achievable here; a handful of
  multi-day averages on near-idle queues are not a representative failure to
  imitate or fear.
- Read priority, channel, account/subscription state, and escalation history
  together to find the real signal in a ticket — do not triage by how many
  tickets are in the queue.
- A professional reply names the real cause, sets a timeframe you can keep,
  and explicitly closes the loop; CSAT produced no data this window, and 14%
  of NCC complaints went unclassified — both are real, current gaps to name,
  not to paper over.
