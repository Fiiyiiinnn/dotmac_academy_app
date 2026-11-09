---
chapter: 5
title: "Sales Orders, Fulfillment Handoff, and Professional Standards"
part: "The Work"
---

# Sales Orders, Fulfillment Handoff, and Professional Standards

Once a quote is accepted, the resulting Sales Order becomes the record you
and the customer both live with. This chapter covers what a Sales Order
actually shows you, the one real manual step left after conversion — turning
an Active subscriber into a billed service — the real permission boundary
between what you can see and what you can open, and closes with the
professional standards that keep your quotes and promises honest.

**By the end of this chapter you can:**

- Read a Sales Order's payment status, provenance, and links correctly.
- Explain the real handoff point after conversion: an Active account with
  no active plan yet.
- Recognize the sales role's permission boundary around Project records —
  what you can see versus what you can open.
- Apply professional standards around quote accuracy and timeline
  expectations, especially when a configuration issue is involved.

## Reading a Sales Order

FIGURE `SLS-05-01` *A Sales Order's own detail page — payment fields, Subscriber card, Sales Ownership & Source note, and Links card.*

The Sales Order created by quote acceptance carries several distinct pieces
of information worth reading correctly:

- **Payment status** — one of Pending, Partial, Paid, or Waived. This is
  **derived from recorded payments**, not something you set directly. If a
  customer asks why their order shows Payment: Pending, the honest answer
  is that no payment has been recorded against it yet — not that anything
  is wrong.
- **Sales Ownership & Source** — a provenance note preserving where this
  deal actually came from, for example "Created from an accepted quote.
  Facebook." This is the `lead_source` field from Chapter 2 surfacing here,
  which is exactly why choosing it accurately back at lead intake mattered.
- **Links** — back to both the originating Quote and the Project that
  quote acceptance created. These links are how you navigate the full
  history of one deal from its final commercial record.

## The real handoff point: Active, but no plan yet

FIGURE `SLS-05-02` *The new Subscriber's account page — status Active, but the Plan card reads "No active plan" with a prominent "+ New Subscription" button.*

The Subscriber that quote acceptance creates is real and immediately shows
status **Active** — but its Plan card honestly reads **"No active plan,"**
with a prominent "+ New Subscription" button sitting unused. This is not a
bug or an incomplete conversion — it is the accurate, honest state of
things: account creation is automatic, but activating a real, billed
Subscription is not part of what quote acceptance does.

Name this plainly rather than assuming otherwise: **turning this Active
account into an actual billed service is a separate, later step, handled
outside the sales agent's own quote-to-order workflow.** This course does
not claim the sales agent personally activates the subscription — as far as
the verified facts behind this course go, that activation is genuinely a
different step, likely owned by a different team. Treat the moment a
Sales Order and its linked Project exist as the real handoff point to
fulfillment and provisioning — the same way a support agent hands an
outage-shaped ticket to NOC rather than working it themselves.

## The permission boundary: visible, not openable

FIGURE `SLS-05-03` *A real 403 Access Denied page, produced when this same sales-role account tried to open the Project detail page directly.*

Here is a real, concrete permission boundary worth understanding rather
than working around. On the Sales Order page (SLS-05-01 above) you can see
a **"Project: PROJ-1108"** reference in the Links card — the fulfillment
project that quote acceptance created. But opening that Project's own
detail page directly produces exactly the access-denied page shown above.

This is a genuine, deliberate boundary, not a broken link: **the sales role
can see that a Project exists and reference it, but cannot open its full
fulfillment details.** Full project and install detail belongs to a
different operational role — the same principle Chapter 1 named about
access generally: a missing screen (or, here, a screen you can point to but
not enter) is a boundary to respect, not a bug to route around by asking
someone else to open it for you.

## Professional standards for sales communication

Everything in this course converges on a simple standard: what you tell a
customer should match what Sub can actually show, and what you promise
should match what has actually happened.

- **Keep quotes accurate.** A quote's line items, pricing, and project type
  should reflect what will actually be delivered — not a rough guess you
  intend to correct later.
- **Be honest about timelines.** Do not promise a specific install date
  before a Project and its fulfillment plan actually exist, and especially
  do not promise one while an acceptance is still blocked on a Project
  Template or configuration issue from Chapter 4 — an unresolved
  configuration problem is exactly the wrong moment to give a customer a
  confident date.
- **Do not overpromise around a known gap.** If a Chapter 4-style
  acceptance failure is still being escalated, tell the customer there is a
  short delay being resolved internally — not a fabricated technical
  explanation, and not a specific promised date you cannot actually stand
  behind yet.

## Common failure modes

- **Treating "Payment: Pending" as a problem to apologize for**, rather
  than the normal, accurate state of an order with no payment recorded yet.
- **Assuming the sales agent is responsible for activating the new
  Subscription.** That is a separate, later step outside this workflow —
  do not promise a customer you personally will "turn on" their plan.
- **Asking someone else to open a Project on your behalf** when you hit the
  access-denied page yourself, which defeats the purpose of the boundary
  exactly as Chapter 1 warned.
- **Giving a confident install date before the Project actually exists**, or
  while an acceptance issue from Chapter 4 is still unresolved.

## Do this at work

1. **On every new Sales Order, read Payment status, Sales Ownership &
   Source, and Links** before telling a customer anything about their
   order's state.
2. **When a new Subscriber shows "No active plan," say so honestly** if a
   customer asks — and point them to the right next step or team rather
   than guessing at one.
3. **If you hit the Project access-denied page, treat it as the real
   boundary it is** — do not ask a colleague to open it for you "just this
   once."
4. **Before quoting a timeline to a customer, confirm the Project actually
   exists** (via the Sales Order's Links card) rather than promising a date
   based on assumption.

## Practice scenarios

Illustrative situations, not real customer records.

### Scenario: "Why hasn't my plan started?"

A customer whose quote was accepted yesterday calls, confused that their
account shows Active but they have no internet service yet. You open their
Subscriber page and see "No active plan" with the "+ New Subscription"
button unused.

**What do you do?**

**A)** Tell the customer this is a system error on Sub's part and that you
will personally fix it by clicking the "+ New Subscription" button right
now.
**B)** Explain honestly that account creation happened automatically but
activating their billed plan is a separate step handled by another team,
and direct them accordingly rather than promising you will do it yourself.
**C)** Tell the customer nothing is wrong and that their service should
already be working, since the account shows Active.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong on the facts. This chapter is explicit that plan activation
  is a separate, later step outside the sales agent's own workflow — this
  is not a bug for you to personally resolve by clicking a button in an
  area this course does not describe as part of your job.
- **B)** Correct, and the best choice. This is the honest handoff point
  this chapter names directly: the account is genuinely Active, the plan
  genuinely is not yet, and saying so plainly (rather than promising
  personal action you are not described as taking) is the professional
  standard this chapter sets.
- **C)** Wrong. "Active" describes the account relationship, not whether a
  billed, working service exists — telling the customer nothing is wrong
  when their Plan card clearly reads "No active plan" contradicts what is
  actually on screen.

</details>

### Scenario: The link you can see but can't open

While reviewing a Sales Order with a customer on the phone, you click the
"Project: PROJ-1108" link out of curiosity to check on installation
progress, and get an access-denied page. The customer is waiting on the
line.

**What do you do?**

**A)** Ask a colleague with broader access to quickly open the Project and
read you the details over chat while the customer waits.
**B)** Tell the customer that full installation/fulfillment detail is
tracked by a different team, and offer to follow up once you have checked
with them, rather than trying to reach the screen yourself.
**C)** Assume your account is broken and ask IT to grant you Project
access immediately so this does not happen again.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong, and it repeats the exact mistake Chapter 1 warns against —
  routing around an access boundary by having someone else open it for you
  defeats the purpose of the boundary just as much as opening it yourself
  would.
- **B)** Correct, and the best choice. This chapter names the Project
  boundary as deliberate — visible reference, not full access — and the
  honest, professional response is to say so and follow up through the
  right channel, not to find a side door under time pressure.
- **C)** Wrong premise. This is not a broken account — it is the same
  documented boundary shown in this chapter's figure (a genuine 403 for
  this same sales-role account), and requesting broader access does not
  address the actual boundary, which belongs to a different operational
  role by design.

</details>

## Summary

- A Sales Order's Payment status is derived from recorded payments, not set
  directly; Sales Ownership & Source preserves the original lead source;
  Links connect back to the Quote and the Project.
- The new Subscriber is genuinely Active but has no active plan yet —
  activating a real billed Subscription is a separate, later step outside
  the sales agent's own workflow, not something to promise you will do
  yourself.
- The sales role can see a Project reference on the Sales Order but cannot
  open the Project's own detail page — a real, deliberate boundary to
  respect, not a bug to route around.
- Keep quotes accurate and timelines honest — especially don't promise a
  date while a Chapter 4-style acceptance issue is still being resolved.
