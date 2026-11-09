---
chapter: 2
title: "Lead Intake and Qualification"
part: "The Work"
---

# Lead Intake and Qualification

A lead is where every deal starts, and the quality of what you enter here
is not a formality — it is the actual data that will, weeks later, be
copied directly onto a real subscriber account the moment a quote is
accepted (Chapter 4 covers exactly how). This chapter covers the real
fields Sub tracks on a lead, the status model, and the one button that
turns "a prospect I'm talking to" into "a priced proposal."

**By the end of this chapter you can:**

- Read the Leads list and understand its filters and status counts.
- Enter a lead's fields accurately, including the ones that quietly cause
  problems later if they are wrong.
- Explain the lead status lifecycle, including why "Won" is never something
  you set yourself.
- Use the "Create Quote" button as the real next step in working a lead.

FIGURE `SLS-02-01` *The Leads list: filters and status counts.*

## The Leads list

Open **Sales → Leads**. The list gives you filters and status counts across
your pipeline — a fast way to see how many prospects sit in each stage
without opening each one individually. Get comfortable filtering this list
early in your day the same way a support agent filters their ticket queue:
knowing the shape of your pipeline before you dive into individual leads
tells you where your time is actually needed.

## Lead status: a real lifecycle, with one exception

A lead's status is one of: `new`, `contacted`, `qualified`, `proposal`,
`negotiation`, `won`, `lost`. Move a lead through these honestly as the
conversation actually progresses — a lead sitting in `new` for two weeks
after three real conversations is a lead whose record no longer reflects
reality, and reporting built on the pipeline will be wrong for it.

There is one important exception to memorize now: **`won` is never
something you select from the dropdown yourself.** As Chapter 4 explains in
full, a lead only becomes `won` automatically, as one consequence of its
quote being accepted. If you find yourself looking for "Won" in the status
dropdown to mark a deal closed, stop — that is not how it happens, and
trying to force it manually would be working around the real mechanism
rather than using it.

## The fields that matter — and the one that bites

A lead carries real, structured fields: `display_name`, `status`,
`owner_agent_id`, contact details (`emails`/`primary_email`,
`phones`/`primary_phone`), address (`address_line1`/`address_line2`,
`city`, `postal_code`, `country_code`), `organization_id` for a business
lead, `lead_source` (a dropdown covering Facebook, Instagram, Whatsapp,
Email, Referrer, Instagram Ads, Facebook Ads, Google, Website, and Portal),
`estimated_value`, `currency`, `expected_close_date`, `probability`, and
free-text `notes`.

Two things are worth knowing before you fill this in on your first real
lead:

- **The email field validates realistically, and rejects some
  placeholder-looking addresses outright** — specifically, reserved-invalid
  top-level domains such as `.invalid` are rejected by the validator. If you
  are ever entering a synthetic or placeholder address for training or
  testing purposes, use something realistic-looking, like an `@example.com`
  address, rather than an address ending in a domain that is deliberately
  reserved as never-valid.
- **`display_name` matters more than it looks like it should.** It is easy
  to treat this as a throwaway label, but Chapter 4 will show you exactly
  why a name that does not read as a real person's name (a business slogan,
  a partial name, initials only) can cause a real, later failure at the
  moment this lead is meant to convert. Enter it the way you would want to
  see a real customer's name on their own account.

`lead_source` is not decorative either — it is the field that later shows
up, verbatim, on the resulting Sales Order's provenance note (Chapter 5
covers this), so an accurately chosen source is how the business eventually
understands which channels actually convert.

## From lead to quote: the real next step

FIGURE `SLS-02-02` *A lead's detail page, showing the Create Quote button and the lead's own status control.*

Once a lead is qualified enough to price, open the lead's own detail page
and use the **Create Quote** button. There is no separate "convert lead"
action to look for — quoting a lead *is* how you move it forward. This is
worth internalizing now, because it sets up the whole shape of the rest of
this course: Leads, Quotes, and Sales Orders are one continuous flow, not
three destinations you choose between.

## Common failure modes

- **Leaving a lead's status stale.** A pipeline where every lead still
  shows `new` after real conversations have happened is invisible to
  anyone trying to read the actual state of the business.
- **Typing a placeholder email that gets rejected**, then wasting time
  wondering why the save failed instead of recognizing the reserved-TLD
  rule.
- **Entering a business-style or partial name into `display_name`**,
  treating it as a mere label rather than data that a later, automatic
  process depends on.
- **Looking for a "Won" option in the status dropdown.** It is not there
  for you to pick — Chapter 4 explains why.

## Do this at work

1. **Filter the Leads list by your own ownership** first thing, the same
   discipline a support agent applies to their own ticket queue.
2. **On every new lead, fill in a real, person-shaped `display_name` and a
   real, deliverable email** before doing anything else — treat this as
   non-negotiable, not optional detail.
3. **Move a lead's status forward as soon as the conversation actually
   changes it** — do not let the record drift behind reality.
4. **When a lead is ready to price, use Create Quote from the lead's own
   detail page** rather than looking for a separate conversion step.

## Practice scenarios

Illustrative situations, not real customer records.

### Scenario: The company-name lead

You are entering a new business lead and, without thinking much about it,
type the company's marketing tagline into `display_name` because that is
what the prospect's email signature emphasized. Everything else about the
lead — email, phone, source — is filled in correctly.

**What do you do?**

**A)** Leave it as entered — `display_name` is just a label, and the
important structured fields (email, phone, source) are all correct anyway.
**B)** Go back and enter a real, person-shaped name for the lead's contact
instead of the tagline, since this field is read later as if it were a
real name.
**C)** Leave it as entered for now, planning to fix it later if it ever
actually causes a problem.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. `display_name` is not a throwaway label — this chapter
  names it explicitly as a field that Chapter 4's automatic conversion
  depends on being person-shaped, so a tagline sitting there is a real data
  problem, not a cosmetic one.
- **B)** Correct, and the best choice. Fixing the name now, while the lead
  is still easy to edit, avoids exactly the later failure this chapter
  foreshadows — the moment this lead's quote is accepted and Sub tries to
  create a real subscriber from this data.
- **C)** Wrong, and it is the more expensive version of the same mistake as
  A — "fix it later" usually means fixing it at the worst possible moment,
  when a deal is ready to close and the bad data blocks it.

</details>

### Scenario: Looking for "Won"

A deal you have been working closes — the customer verbally agrees and you
want to reflect this in Sub immediately. You open the lead's status
dropdown looking for "Won" to select it.

**What do you do?**

**A)** Since "Won" is not selectable, assume the dropdown is missing an
option and report it as a bug.
**B)** Recognize that lead status moves to Won automatically as a
consequence of a different action, and proceed to create and work the
quote instead of trying to set status directly.
**C)** Set the status to `negotiation` instead, since that is the closest
available option, and leave a note saying the deal is actually closed.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong premise. This is not a missing option or a bug — this
  chapter explicitly states that `won` is never manually selectable and is
  set automatically elsewhere in the flow.
- **B)** Correct, and the best choice. The real path to a lead reaching Won
  is quote acceptance, which Chapter 4 covers in full — the right move here
  is to move forward with the quote, not to hunt for a manual status option
  that does not exist by design.
- **C)** Wrong, and it leaves the record actively misleading — a lead
  marked `negotiation` with a note claiming it is closed is worse than an
  accurate `proposal` or `negotiation` status, because anyone reading the
  status field alone gets the wrong picture.

</details>

## Summary

- The Leads list gives you filters and pipeline counts — check it before
  diving into individual leads.
- Lead status moves through `new → contacted → qualified → proposal →
  negotiation`, but `won` is set automatically by quote acceptance, never
  by you directly.
- `display_name` and email quality are not cosmetic — they are the exact
  data a later, automatic process depends on, and the email validator
  rejects reserved-invalid domains like `.invalid` outright.
- `lead_source` carries forward onto the eventual Sales Order's provenance
  note — choose it accurately.
- Create Quote, from the lead's own detail page, is the real way a lead
  moves forward — there is no separate "convert lead" action.
