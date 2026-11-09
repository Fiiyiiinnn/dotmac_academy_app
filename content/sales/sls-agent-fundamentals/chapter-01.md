---
chapter: 1
title: "Working in Sub: Your Role and the Real Access Model"
part: "The Role"
---

# Working in Sub: Your Role and the Real Access Model

Every lead, quote, and order a prospective subscriber touches ends up in one
place: **Sub** (`dotmac_sub`), the same operator record the support and
network teams already work from. As a sales agent you are not using a
separate CRM bolted on the side — you are working the same system, from the
sales-specific screens inside it. This chapter is not about clicking
buttons yet — it is about understanding what those screens are, and being
honest about how access to them actually works today, so nothing in the
rest of this course surprises you later.

**By the end of this chapter you can:**

- Name the sales screens you will use and what each one is for.
- Explain why Sub is the record for sales activity too, not just support.
- Describe, honestly, how sales access is scoped today — including the real
  gap in the role model — rather than assuming a tidy dedicated role exists.
- Explain the "Sales Agent" attribution nuance on a Sales Order, and why it
  is a different thing from "who can create leads and quotes."

## Sub is the record for sales too

The same principle that governs support work governs sales work: if a
lead's details, a quote's terms, or an order's status are not in Sub, they
did not officially happen, no matter how clearly you remember the
conversation or how detailed your own notes are. A prospect who calls back
in three weeks needs their history to be exactly where you left it — not in
a personal notebook, not in a WhatsApp thread, not in your memory of "the
guy who wanted fiber on Example Street."

This matters more in sales than it might first appear, because — as
Chapter 4 will show — a piece of data you enter carelessly today (a lead's
name typed as a company slogan instead of a person's name, an email that
never gets filled in) can silently block a real business event weeks later,
when that lead is ready to convert. Sales data quality is not administrative
overhead; it is the input to a process that runs mostly on its own once you
get it right.

## Where you will spend your day

Under the **Sales** area of the sidebar you will find three connected
screens, each covered in depth over the next four chapters:

- **Leads** — prospects and their qualification state, covered fully in
  Chapter 2.
- **Quotes** — priced proposals built against a lead, covered in Chapter 3.
- **Sales Orders** — the confirmed commercial record created once a quote is
  accepted, covered in Chapters 4 and 5.

These are not three independent tools you juggle — they are three stages of
one continuous record for a single prospect, and (as you will see in
Chapter 4) the system itself moves a prospect from the first stage to the
third in one action, not three separate manual ones.

## The real access model — and its honest gap

Here is the part of this chapter you should not skip past: as of today,
**dotmac_sub does not ship a seeded "sales" role.** The permission keys that
control lead, quote, and sales-order actions (`crm:lead:*`, `crm:quote:*`,
`crm:sales_order:*`) exist in the system and are declared assignable through
the admin UI — but no role carries them by default. The seed script that
sets up standard roles carries an inline comment saying, plainly, that these
permissions "stay admin-implicit (wildcard) until one exists."

What this means in practice: whoever does sales work in a given operation
today most likely does it from an account with broad, admin-like access —
not from a narrowly-scoped role built specifically for sales. That is not
this course inventing a tidy story about "the sales role can do X and not
Y" the way earlier support-focused material might; it is naming a real,
current gap honestly. If your own account can see screens well beyond
Leads/Quotes/Sales Orders, that is very likely why — not evidence that your
job description has quietly expanded.

This is worth remembering the next time someone assumes access always maps
cleanly to a role: it does not, yet, for sales. The fix is a real role
being defined and seeded, not you working around the gap by assuming your
current access is the intended shape of the sales job.

## The "Sales Agent" attribution nuance

Separately from who *can perform* lead/quote/order actions, a Sales Order
carries a **"Sales Agent"** attribution field — who the order is credited
to. That dropdown is populated from a different, narrower thing: it lists
only users holding a role literally named **"Customer Experience."** This
is not the same question as "who can create leads and quotes" — you can
have full ability to work leads and quotes today (via the admin-implicit
access above) without necessarily appearing in that attribution dropdown,
because that dropdown is driven by a specific named role, not by the
permission keys that let you do the underlying sales work.

Do not conflate the two. If you are ever asked why you cannot credit an
order to yourself in that dropdown, the answer is this specific,
narrowly-scoped role check — not a broader statement about your sales
access generally.

## Common failure modes

- **Assuming a dedicated "sales" role already exists and behaves like a
  clean permission boundary.** It does not yet — access today is most
  likely broader, admin-implicit access, not a purpose-built role.
- **Treating broad access as license to work outside the Sales screens.**
  Having the technical ability to see other areas of Sub is not the same as
  those areas being your job.
- **Confusing the Sales Agent attribution dropdown with general sales
  permissions.** They are driven by different things: one by a specific
  named role ("Customer Experience"), the other by permission keys with no
  seeded role of their own yet.
- **Treating Sub as optional for sales activity** the way a habit formed on
  a separate CRM might encourage — every lead, quote, and order belongs in
  Sub, the same record everyone else already trusts.

## Do this at work

1. **Open the Sales area of the sidebar and locate Leads, Quotes, and Sales
   Orders**, even before you have live prospects to work — get the shape of
   the three screens fixed in your mind.
2. **Ask your supervisor, plainly, what role your own account currently
   holds** and whether it is a dedicated sales role or broader admin-style
   access. Do not assume; confirm.
3. **If you ever try to attribute a Sales Order to yourself and cannot**,
   recognize this as the Customer Experience role check from this chapter,
   not a bug to escalate as broken software.
4. **Write down one question about your own access** that this chapter did
   not answer, and raise it with your supervisor rather than guessing.

## Practice scenarios

Illustrative situations, not real customer records.

### Scenario: More screens than expected

On your first day you notice your account can open several screens well
beyond Leads, Quotes, and Sales Orders — including some that look like they
belong to billing or account administration. A colleague shrugs: "Yeah,
sales just has full access here."

**What do you do?**

**A)** Treat this as confirmation that the sales role is simply defined
this broadly on purpose, and start using whichever screens seem useful to
your day-to-day prospecting.
**B)** Recognize this as the known gap this chapter describes — no seeded
sales role exists yet, so access is admin-implicit — and keep your actual
work scoped to Leads, Quotes, and Sales Orders regardless of what else is
technically reachable.
**C)** Ask IT to restrict your account down to exactly the three sales
screens immediately, since anything broader must be a mistake in your
provisioning.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. Being able to reach a screen is not the same as it being
  your job — this chapter names the broad access as a real, honest gap in
  the role model, not as an intentional expansion of what a sales agent is
  meant to do day to day.
- **B)** Correct, and the best choice. The chapter is explicit that no
  seeded sales role exists yet, so admin-implicit access is the current,
  known reality — the right response is to keep your actual work inside
  Leads/Quotes/Sales Orders, not to treat wide access as an invitation.
- **C)** Wrong premise, though the instinct toward tighter scoping is
  reasonable. This is not a provisioning mistake to fix ad hoc on one
  account — it is a system-wide gap (no seeded sales role) that needs a
  real role defined and seeded, not a one-off IT ticket.

</details>

### Scenario: The missing name in the dropdown

You close a deal and go to set yourself as the Sales Agent on the resulting
Sales Order, but your name does not appear in the dropdown. A colleague who
does appear in it has never worked a lead in their life.

**What do you do?**

**A)** Conclude the attribution feature is broken, since it clearly is not
tracking who actually does sales work.
**B)** Recognize that the dropdown is populated by the "Customer
Experience" role specifically, not by who has sales permissions, and raise
the mismatch with your supervisor as a role-configuration question rather
than a bug.
**C)** Ask a colleague who does appear in the dropdown to log in and
attribute the order to themselves instead, since someone needs to be
credited.

<details>
<summary>What happens with each choice</summary>

- **A)** Wrong. The feature is working exactly as this chapter describes —
  it deliberately lists only "Customer Experience" role holders, which is a
  different question from who can do sales work at all.
- **B)** Correct, and the best choice. This is precisely the attribution
  nuance from this chapter: the dropdown is role-driven, and a mismatch
  between "who did the work" and "who can be credited" is a role-mapping
  question worth raising, not evidence of a defect.
- **C)** Wrong, and it misrepresents who actually did the work — routing
  attribution to whoever happens to satisfy an unrelated role check, instead
  of raising the real mismatch, buries the problem rather than fixing it.

</details>

## Summary

- Sub is the operator's record for sales activity, exactly as it is for
  support — a lead, quote, or order not entered in Sub did not officially
  happen.
- Your daily work centers on three connected screens: Leads, Quotes, and
  Sales Orders.
- No seeded "sales" role exists yet in dotmac_sub — access today is most
  likely broad, admin-implicit access rather than a purpose-built role, and
  that is a real, current gap worth knowing rather than assuming otherwise.
- The Sales Agent attribution dropdown on a Sales Order is a separate,
  narrower thing driven by the "Customer Experience" role — not the same
  question as who can create leads, quotes, and orders.
