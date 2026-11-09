---
chapter: 3
title: "Building and Sending a Quote"
part: "The Work"
---

# Building and Sending a Quote

A quote is the priced proposal that turns a qualified lead into something a
prospect can actually say yes to. This chapter covers the real fields the
quote form requires (including the one that trips agents up at submit
time), how line items work, the quote status lifecycle, and the difference
between marking a quote Sent and actually sending it.

**By the end of this chapter you can:**

- Build a quote from a lead, including a custom line item, without
  guessing at required fields.
- Avoid the project-type submission error that catches agents who have not
  seen it before.
- Explain the quote status lifecycle and why Draft must go through Sent
  before Accepted will work.
- Distinguish the Send Email action from setting status to Sent — two
  different actions that usually, but not always, happen together.

## Starting a quote from a lead

From a lead's own detail page, **Create Quote** opens the quote form
already tied to that lead — there is no separate quote-creation path that
starts from nothing, which is deliberate: a quote always traces back to the
lead it came from.

FIGURE `SLS-03-01` *The quote-creation form, filled in with currency, project type, a custom line item, expiry date, and notes.*

## The field that catches people: project type

The quote form requires a **project type**, chosen from a fixed set:
`cable_rerun`, `fiber_optics_relocation`, `air_fiber_relocation`,
`fiber_optics_installation`, `air_fiber_installation`, and
`cross_connect`. Here is the real gotcha, worth knowing before it costs you
a submit attempt: **the form does not visually flag project type as
required until you actually try to submit without it** — at which point
you get a plain "Please select an item in the list" message rather than an
inline warning next to the field itself. If you hit that message, this is
almost certainly why. Pick a project type before you submit, not after the
first rejection teaches you to.

## Line items: custom entries are fully valid

A quote's line items are not limited to catalog-linked offers. A plain,
descriptive custom line item — `item_description`, `item_quantity`, and
`item_unit_price` filled in directly — is a completely normal, valid way to
build a quote. Do not feel you need to force every quote into a
catalog-offer shape if what you are actually proposing is better described
in your own words with your own price. Fill in currency, expiry date, and
any notes the customer should see alongside the priced items.

## The quote status lifecycle — Draft must go through Sent

A quote's status is one of: `draft`, `sent`, `accepted`, `rejected`,
`expired`. This is a real state machine, not a label you can jump around in
freely: **a quote must move through `sent` before `accepted` will succeed.**
Trying to set status directly from `draft` to `accepted` is rejected. If you
are ever troubleshooting why an acceptance will not go through, the very
first thing to check is whether the quote actually passed through `sent`
first — a quote that skipped straight from draft toward accepted is not a
valid transition, regardless of how ready the customer actually is to say
yes.

FIGURE `SLS-03-02` *A quote detail page with status "Sent."*

## Send Email is not the same action as setting status to Sent

This is worth calling out explicitly because the two usually travel
together but are mechanically two separate actions: setting the quote's
**status** to `sent` records the quote's own state in Sub, while **Send
Email** is a separate action that actually delivers the quote to the
customer's inbox. In the normal flow you do both — set status to Sent and
use Send Email — but they do not happen automatically as a pair. If you set
status to Sent without also sending the email, the customer has not
actually received anything yet, no matter what the quote's status field
now says. Conversely, sending the email does not by itself change the
status field for you.

## Common failure modes

- **Submitting a quote with no project type selected**, then being confused
  by a generic "Please select an item in the list" error instead of
  recognizing the field that needs attention.
- **Assuming every line item needs a catalog offer behind it.** A plain
  custom description, quantity, and unit price is a normal, valid line
  item.
- **Trying to mark a Draft quote Accepted directly.** The state machine
  requires passing through Sent first.
- **Setting status to Sent and assuming the customer has been emailed**, or
  the reverse — sending the email and assuming the status updated itself.

## Do this at work

1. **Select a project type before you submit a new quote**, every time —
   do not wait for the generic error to remind you.
2. **Use a custom line item whenever the work does not map cleanly to a
   catalog offer.** Do not force-fit a description into an offer that does
   not match.
3. **When you set a quote to Sent, also use Send Email in the same pass**,
   unless you have a specific reason to delay delivery — do not let the two
   drift apart by habit.
4. **Before trying to accept a quote, confirm it is already in Sent
   status.** If it is not, that is the actual blocker, not something wrong
   with the acceptance action itself.

## Practice scenarios

Illustrative situations, not real customer records.

### Scenario: The generic error

You fill in a new quote's currency, line items, expiry date, and notes, and
click submit. You get back "Please select an item in the list" with no
obvious indication of which field is the problem.

**What do you do?**

**A)** Assume the line items are malformed and start deleting and
re-entering them one at a time to find the fault.
**B)** Check whether project type has been selected, since this is the
field this course names as required but not visually flagged until submit.
**C)** Resubmit the form unchanged, assuming it was a transient error.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong, and it wastes time chasing the wrong field. Line items were
  described as filled in correctly; the generic error is the known
  signature of a missing project type, not malformed line items.
- **B)** Correct, and the best choice. This chapter names project type
  specifically as the field that produces exactly this generic,
  unhelpful-looking error when it is left unselected — checking it first
  is the fast, correct diagnosis.
- **C)** Wrong. A required field being empty is not a transient condition —
  resubmitting without changing anything will fail again the same way.

</details>

### Scenario: Sent but not received

A colleague asks why a customer says they "never got the quote," even
though the quote's status in Sub already reads `sent`.

**What do you do?**

**A)** Tell the colleague the customer must be mistaken, since the system
already shows the quote as sent.
**B)** Check whether Send Email was actually used, since status and email
delivery are two separate actions that do not trigger each other
automatically.
**C)** Change the status back to `draft` and then immediately to `sent`
again, assuming this will force a fresh email.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. Status `sent` records the quote's own state; it does not by
  itself prove the customer received an email, since Send Email is a
  separate action.
- **B)** Correct, and the best choice. This chapter is explicit that
  setting status to Sent and using Send Email are two different actions
  that usually happen together but are not the same thing — checking
  whether the email action actually ran is the real diagnosis.
- **C)** Wrong, and it misunderstands what the status field does. Toggling
  status does not trigger an email send on its own — the fix is to use the
  Send Email action directly, not to cycle the status field hoping for a
  side effect.

</details>

## Summary

- Quotes are created from a lead's own detail page via Create Quote — there
  is no separate, lead-independent creation path.
- Project type is required but not visually flagged until you submit
  without it — pick one up front to avoid the generic error.
- Custom line items (description, quantity, unit price) are a fully valid
  way to quote, not a fallback for when a catalog offer doesn't fit.
- The status lifecycle is `draft → sent → accepted/rejected/expired`, and
  Draft cannot jump straight to Accepted.
- Setting status to Sent and using Send Email are two distinct actions —
  make sure both happen when you intend the customer to actually receive
  the quote.
