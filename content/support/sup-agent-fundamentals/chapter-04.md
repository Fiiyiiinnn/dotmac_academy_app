---
chapter: 4
title: "Service Issues and Outage Awareness"
part: "The Work"
---

# Service Issues and Outage Awareness

Some tickets are one customer's problem. Others are the visible edge of a
wider outage affecting many customers at once, and treating the second kind
like the first wastes everyone's time — yours, the customer's, and Network
Operations' (NOC). This chapter covers what Sub actually tracks about
outages, the real self-service check you can run yourself before you ask
anyone else, and what genuinely does stay outside your access, and why.

**By the end of this chapter you can:**

- Check a customer's own record for a known incident before you do anything
  else, and act correctly on what you find.
- Recognise when a ticket might be part of a wider outage rather than an
  isolated fault, even when the customer's own record shows nothing yet.
- Explain why the network monitoring console is outside your access, and
  why that is by design.
- Link a ticket to a known outage correctly, using the reference the
  customer's own record already gave you.

## Check the customer's own record first

Before you ask anyone, check the customer whose ticket you are holding.
Open their account and go to the **Network tab** — this is available to
you through `customer:read`, the same access you already use to read any
account, so nothing extra is needed to look.

Each subscription card on that tab shows a real, live status panel:

- If there is a known incident affecting this customer, you will see a red
  **"Known incident"** or **"Known area outage"** banner. It carries the
  incident's own customer-facing message, the connection state, and — most
  importantly — a **"Open the covering ticket"** button that takes you
  straight to a real support ticket already covering this outage.
- If nothing is known, the panel instead shows a green line: **"No known
  outage · Last checked <time>"**. This is not silence — it is an explicit,
  time-stamped statement that nothing is currently known, which is itself
  useful information.

This is the first, real, self-service step, and it changes how you should
open almost every connectivity ticket: check the customer's own Network tab
before you troubleshoot their equipment and before you ask NOC anything.

## If it shows a known incident: do not raise a duplicate

If the panel shows "Known incident" or "Known area outage," the message on
it says exactly what to do: **this customer is already covered — do not
raise a duplicate.** Use the **"Open the covering ticket"** button to go
straight to the real ticket already handling this outage. That covering
ticket's own ID is what you use back on your own ticket (see "Linking the
ticket" below) — you do not need NOC to give you a reference separately;
the customer's own record just gave you the real one.

A **"View incidents"** link may also appear alongside the panel, but only
when the incident carries a formal incident ID. That link points to
`/admin/network/outages` — the NOC console — and it is genuinely outside
your access (see below). Seeing the link is not an invitation to follow
it; the panel above it already gave you everything you need to act.

## If it shows no known outage: watch for the pattern

A "No known outage" reading is honest, current information — not proof
that nothing is wrong yet. If you are looking at one customer with one
unusual complaint, treat it as an individual case. But if you are seeing
the pattern below, that is a stronger signal than any one customer's own
"No known outage" reading, precisely because an outage can exist before
anyone — classifier or human — has confirmed it against this customer's
specific service:

- Multiple tickets arriving close together, describing the same symptom, in
  the same area or region.
- A customer reporting a total loss of service rather than a specific,
  narrow complaint.
- A ticket where the customer mentions neighbours or nearby businesses
  having the same problem.

None of these prove an outage on their own — but any of them, especially
alongside a "No known outage" reading on the individual record, is a good
reason to ask NOC before you spend an hour troubleshooting one customer's
equipment for a problem that may not be on their end at all.

## The console is NOC's, not yours — and that is by design

FIGURE `SUP-04-02` *Reaching the network monitoring console without the right access.*

If you try to open the network monitoring console directly — including by
following a "View incidents" link — you will see exactly this: an
access-denied page, not a bug. Your support role does not carry
`monitoring:read`, the permission that opens NOC's console, because that
console controls a different kind of risk (declaring and managing live
infrastructure incidents) that is deliberately kept with the team that owns
it. This is the same principle from Chapter 1: a missing screen is a
boundary, not an oversight, and the fix is never to find a side door
through it.

This boundary sits alongside, not instead of, the self-service check above:
you can always see whether a customer's own service is already covered by
a known incident, but you cannot browse the console that declares and
manages incidents in general. Escalating to NOC is for the harder question
— "is something happening here that hasn't been confirmed against this
customer yet" — not for the question the Network tab already answers for
you.

## What you can do: link the ticket

FIGURE `SUP-04-01` *Linking a ticket to its outage reference from the ticket detail screen.*

Once you have a real covering ticket — whether from the customer's own
"Open the covering ticket" link or from something NOC told you directly —
open your ticket and use the **Related Outage / Links** panel to record it:
paste the covering ticket's ID and link it. This one action is the
difference between "I saw an outage banner (or was told about one in a
chat message that will scroll away)" and "this ticket carries a permanent,
visible link to the incident it belongs to."

Do this every time you learn a ticket is outage-related, even if you
already told the customer verbally, and even if you already used "Open the
covering ticket" to look at it. The link is what lets anyone who opens this
ticket later — another agent, a supervisor, an auditor — see the connection
without having to dig through chat history or retrace your steps.

## Outage lifecycles, for context

Sub has a real, structured record of outages: an incident against a
network node, base station, or cabinet, with its own lifecycle. Two kinds
of outage exist in that record, and it is worth knowing both exist even
though the console that manages them belongs to NOC:

- **Operator-declared** — NOC has confirmed it from the console. Lifecycle:
  `open` → `resolved`.
- **Classifier-detected** — an automated system flags a likely outage before
  a human confirms it. Lifecycle: `suspected` → `confirmed` → `clearing` →
  `resolved` (or `discarded`, if it turns out to be a false alarm).

Each incident tracks how many customers are affected and when it started —
useful vocabulary if you need to ask NOC a precise question ("is this
confirmed or still suspected?") rather than a vague one, once you have
already checked what the customer's own record shows.

## Common failure modes

- **Skipping the customer's own Network tab and going straight to NOC.**
  The known-incident panel is the fastest, most direct answer for this one
  customer — check it before asking anyone else anything.
- **Raising a duplicate ticket when the customer is already covered.** The
  panel says so explicitly — use "Open the covering ticket" and link to it
  instead of creating new work.
- **Troubleshooting a single account for an area-wide problem** that the
  customer's own record has not caught up to yet. "No known outage" is not
  the same as "definitely nothing wrong" — watch for the clustered pattern.
- **Getting outage information and never linking it.** If it is not linked
  on the ticket, the next person has no way to know, however you learned it.
- **Trying to reach the NOC console anyway** — through the "View incidents"
  link, a bookmarked URL, a shared login, or asking someone else to check
  "quickly." The access boundary exists for the same reason your own scoped
  access does.
- **Escalating every connectivity complaint as a suspected outage** without
  first checking the customer's own record or the clustering pattern. That
  trains NOC to discount your reports.

## Do this at work

1. **On every connectivity complaint, open the customer's Network tab
   first**, before you troubleshoot their equipment or ask anyone else
   anything.
2. **If you see "Known incident" or "Known area outage," use "Open the
   covering ticket" immediately** and link your own ticket to it — do not
   raise a duplicate.
3. **When you get two or more similar tickets in a short window, check for a
   common area or region**, even if each customer's own record still shows
   "No known outage."
4. **The next time you confirm an outage — from the customer's record or
   from NOC — link every affected ticket you are handling to it**, not just
   the first one.
5. **If you hit the access-denied page on the console**, treat it as
   confirmation you are in the right place asking the right question of the
   right team, not as an obstacle to solve yourself.

## Practice scenarios

Illustrative situations, not real customer records.

### Scenario: The banner you almost missed

A customer calls reporting no internet. You open their account to start
troubleshooting the connection settings, and, before doing anything else,
you notice a red banner on their subscription card reading "Known area
outage."

**What do you do?**

**A)** Ignore the banner and continue troubleshooting the customer's
equipment, since you have not yet confirmed anything with NOC.
**B)** Use "Open the covering ticket" to see the real ticket already
handling this, then link your own ticket to it instead of raising a
duplicate.
**C)** Message NOC to ask if there is an outage in the area, since that is
the correct way to confirm one.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. The banner is telling you directly that this customer is
  already covered by a known incident — troubleshooting their individual
  equipment first ignores information that was already on screen and wastes
  time on a cause that isn't local to them.
- **B)** Correct, and the best choice. The banner's own message says not to
  raise a duplicate; "Open the covering ticket" gives you the real ticket
  ID directly, and linking your own ticket to it is the complete, correct
  response — no need to ask NOC anything further for this customer.
- **C)** Wrong, and unnecessary. The customer's own record already answered
  the question NOC would otherwise have to answer for you — asking NOC here
  duplicates work the panel already did.

</details>

### Scenario: Four tickets in twenty minutes

Within twenty minutes you receive four tickets, all from the same region,
all reporting "connected but can't browse." Each customer's own Network
tab shows "No known outage." You are about to triage the fourth one the
same way you triaged the first three: as an individual equipment problem.

**What do you do?**

**A)** Triage all four independently and start troubleshooting each
customer's equipment one at a time, since each customer's own record shows
no known outage.
**B)** Ask NOC whether there is a known or suspected incident in that
region before troubleshooting any of them individually, even though no
single customer's record shows one yet.
**C)** Try to check the network monitoring console yourself to confirm
whether there's an outage before asking anyone.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. "No known outage" on each individual record does not rule
  out an outage that has not yet been confirmed against these specific
  customers — same symptom, same area, clustered in time is exactly the
  outage-shaped pattern this chapter describes, and it is a stronger signal
  than four individually "clean" readings.
- **B)** Correct, and the best choice. This is exactly the case the
  per-customer check cannot resolve on its own — asking NOC before assuming
  four unrelated equipment faults is the real, supported response to a
  clustered pattern, and if NOC confirms an incident, all four tickets
  should be linked to it.
- **C)** Wrong, and it repeats a mistake from Chapter 1. Your role does not
  carry `monitoring:read` by design; trying to reach the console yourself
  is exactly the "side door" behavior that boundary exists to prevent —
  asking NOC is the supported path, not a workaround to it.

</details>

## Summary

- The customer's own **Network tab** shows a live "Known incident" / "Known
  area outage" panel, or an explicit "No known outage · Last checked
  <time>" line — this is real, immediate self-service, reachable through
  the `customer:read` access you already have.
- A "Known incident" banner means do not raise a duplicate: use "Open the
  covering ticket" to reach the real ticket already handling it, and use
  that ticket's own ID when you link yours.
- "No known outage" on one record is not proof nothing is wrong — watch for
  clustered, same-symptom tickets across customers as a stronger signal,
  and ask NOC when you see that pattern.
- The NOC console (and any "View incidents" link into it) stays outside
  your access by design — that boundary is about the console generally, not
  about whether you can check one customer's own known-incident status.
- The Related Outage / Links panel on a ticket is your real, supported tool
  for recording the connection permanently, using the covering ticket's ID
  from wherever you got it — the customer's own record or NOC directly.
